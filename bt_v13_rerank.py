#!/usr/bin/env python3
"""
V13.3回测 — 用Alpha因子做二次排序（不是加分）
策略改动：M1过线的候选，按Alpha因子排名做二次排序选Top3
"""
import pymysql, numpy as np
from collections import defaultdict
from datetime import datetime, date, timedelta
import sys, os, warnings
warnings.filterwarnings('ignore')

def get_db_pass():
    import pymysql.cursors as cursors
    try:
        with open('/etc/mysql/debian.cnf') as f:
            for line in f:
                if line.strip().startswith('password'):
                    return line.split('=')[1].strip()
    except: pass
    return 'iXve1rVBXfdA4tL9'

PWD = get_db_pass()
import pymysql.cursors as cursors
DB_CONFIG = {'host':'localhost','user':'debian-sys-maint','password':PWD,'database':'stock_db_v2','charset':'utf8mb4','cursorclass':cursors.DictCursor}

SEASON_CFG = {
    'summer':{'buy':68,'hold':55,'stop':0.08,'max':0.50,'per':0.30,'cd':0,'alpha':{'alpha052':0.35,'alpha122':0.25,'alpha093':0.20}},
    'chaos_spring':{'buy':70,'hold':55,'stop':0.08,'max':0.40,'per':0.25,'cd':0,'alpha':{'alpha062':0.35,'alpha001':0.25,'alpha052':0.20}},
    'spring':{'buy':70,'hold':55,'stop':0.07,'max':0.40,'per':0.25,'cd':0,'alpha':{'alpha062':0.30,'alpha001':0.30,'alpha052':0.20}},
    'chaos':{'buy':72,'hold':55,'stop':0.08,'max':0.35,'per':0.20,'cd':3,'alpha':{'alpha169':0.35,'alpha013':0.30,'alpha052':0.35}},
    'chaos_autumn':{'buy':72,'hold':50,'stop':0.06,'max':0.20,'per':0.12,'cd':5,'alpha':{'alpha031':0.35,'alpha162':0.30,'alpha168':0.25}},
    'weak_autumn':{'buy':75,'hold':50,'stop':0.05,'max':0.15,'per':0.08,'cd':7,'alpha':{'alpha062':0.40}},
    'autumn':{'buy':78,'hold':45,'stop':0.05,'max':0.10,'per':0.05,'cd':10,'alpha':{'alpha062':0.40}},
    'winter':{'buy':85,'hold':40,'stop':0.03,'max':0.05,'per':0.03,'cd':15,'alpha':{}},
}

SEASON_MAP = {'summer':'summer','spring':'spring','weak_spring':'spring','chaos_spring':'chaos_spring','chaos':'chaos','chaos_autumn':'chaos_autumn','weak_autumn':'weak_autumn','autumn':'autumn','winter':'winter'}

class BT:
    def __init__(self, cap=1000000):
        self.cap=cap; self.start_cap=cap
        self.h={}; self.trades=[]; self.equity=[]
        self.db=pymysql.connect(**DB_CONFIG)
    def close(self): self.db.close()
    def pool(self):
        with self.db.cursor() as c:
            c.execute("SELECT ts_code FROM watch_pool")
            return [r['ts_code'] for r in c.fetchall()]
    def dates(self, s, e):
        with self.db.cursor() as c:
            c.execute("SELECT DISTINCT trade_date FROM daily_kline WHERE trade_date>=%s AND trade_date<=%s ORDER BY trade_date", (s,e))
            return [str(r['trade_date']) for r in c.fetchall()]
    def season(self, d):
        with self.db.cursor() as c:
            c.execute("SELECT season FROM season_state WHERE trade_date=%s", (d,))
            r=c.fetchone()
        return SEASON_MAP.get(r['season'] if r else 'chaos', 'chaos')
    def load_m1(self, d, codes):
        if not codes: return {}
        res={}
        with self.db.cursor() as c:
            for i in range(0,len(codes),200):
                ids=','.join(["'%s'"%c for c in codes[i:i+200]])
                c.execute("SELECT ts_code,composite_score FROM strategy_signal WHERE trade_date='%s' AND ts_code IN (%s)"%(d,ids))
                for r in c.fetchall(): res[r['ts_code']]=float(r['composite_score'] or 50)
        return res
    def load_alpha(self, d, codes, fns):
        if not codes or not fns: return {}
        res={fn:{} for fn in fns}
        for fn in fns:
            for i in range(0,len(codes),300):
                ids=','.join(["'%s'"%c for c in codes[i:i+300]])
                with self.db.cursor() as c:
                    q=f"SELECT ts_code, factor_score FROM alpha_factor_score WHERE trade_date='{d}' AND ts_code IN ({ids}) AND factor_name='{fn}'"
                    c.execute(q)
                    for r in c.fetchall(): res[fn][r['ts_code']]=float(r['factor_score'])
        return res
    def rank(self, dct):
        if not dct: return {}
        keys=list(dct.keys()); vals=np.array([dct[k] for k in keys],dtype=float)
        order=np.argsort(vals); ranks=np.empty(len(keys))
        ranks[order]=np.arange(len(keys))/max(len(keys)-1,1)
        return {keys[i]:float(ranks[i]) for i in range(len(keys))}
    def alpha_score(self, alpha_scores, alpha_cfg, ts):
        """计算某只股票的Alpha综合得分（加权rank和）"""
        raw=0; ws=0
        for fn,w in alpha_cfg.items():
            ranks=self.rank(alpha_scores.get(fn,{}))
            rk=ranks.get(ts,0.5)
            raw+=rk*w; ws+=w
        return raw/ws if ws>0 else 0.5
    def close_px(self, d, codes):
        res={}
        with self.db.cursor() as c:
            for i in range(0,len(codes),300):
                ids=','.join(["'%s'"%c for c in codes[i:i+300]])
                c.execute("SELECT ts_code,close FROM daily_kline WHERE trade_date='%s' AND ts_code IN (%s)"%(d,ids))
                for r in c.fetchall(): res[r['ts_code']]=float(r['close'])
        return res
    def high_px(self, d, codes):
        res={}
        with self.db.cursor() as c:
            for i in range(0,len(codes),300):
                ids=','.join(["'%s'"%c for c in codes[i:i+300]])
                c.execute("SELECT ts_code,high FROM daily_kline WHERE trade_date='%s' AND ts_code IN (%s)"%(d,ids))
                for r in c.fetchall(): res[r['ts_code']]=float(r['high'])
        return res
    def run(self, s='2024-09-01', e='2026-07-10'):
        print("="*60); print("V13.3 — Alpha二次排序（替代加分）"); print("="*60)
        pool=self.pool(); ds=self.dates(s,e)
        print(f"股票{len(pool)}只 交易日{len(ds)}天")
        buys=0
        for idx,d in enumerate(ds):
            sn=self.season(d); cfg=SEASON_CFG.get(sn,SEASON_CFG['chaos'])
            to_sell=[]
            for ts,h in list(self.h.items()):
                cp=self.close_px(d,[ts]).get(ts); hp=self.high_px(d,[ts]).get(ts)
                if cp is None or hp is None: continue
                h['cur']=cp
                if hp>h.get('peak',0): h['peak']=hp
                dd=(h['peak']-cp)/h['peak'] if h['peak']>0 else 0
                bi=ds.index(h['bd'])
                dh=idx-bi
                if dh>=60: to_sell.append((ts,'exp'))
                elif dd>=cfg['stop']: to_sell.append((ts,'stop'))
                elif dh%10==0:
                    m1=self.load_m1(d,[ts]).get(ts,50)
                    if m1<cfg['hold']: to_sell.append((ts,'score'))
            for ts,reason in to_sell:
                h=self.h.pop(ts,None)
                if not h: continue
                pr=h.get('cur',h['bp'])
                pnl=h['qty']*(pr-h['bp']); pnl_p=(pr-h['bp'])/h['bp']*100
                self.cap+=h['qty']*pr
                self.trades.append({'d':d,'ts':ts,'a':'S','qty':h['qty'],'pr':pr,'pnl':round(pnl,2),'p':round(pnl_p,2),'r':reason})
            # 买入（M1过线 + Alpha二次排序）
            used=sum(h['qty']*h.get('cur',h['bp']) for h in self.h.values())
            eq=self.cap+used; avail=eq*cfg['max']-used
            in_cd=any(t['d']>str(date.fromisoformat(d)-timedelta(days=cfg['cd'])) for t in self.trades[-5:] if t['a']=='S' and t['r']=='stop')
            if not in_cd and avail>0:
                cands=[ts for ts in pool if ts not in self.h]
                m1s=self.load_m1(d,cands)
                cands=[ts for ts in cands if ts in m1s and m1s[ts]>=cfg['buy']]
                if cands and cfg['alpha']:
                    alpha_scores=self.load_alpha(d,cands,list(cfg['alpha'].keys()))
                    alpha_ranked=sorted(cands, key=lambda ts: -self.alpha_score(alpha_scores,cfg['alpha'],ts))
                else:
                    alpha_ranked=sorted(cands, key=lambda ts: -m1s[ts])
                closes=self.close_px(d,alpha_ranked[:3])
                for ts in alpha_ranked[:3]:
                    if avail<=0: break
                    pr=closes.get(ts,0)
                    if pr<=0: continue
                    qty=int(avail*cfg['per']/pr)
                    if qty<=0: continue
                    self.cap-=qty*pr; avail-=qty*pr
                    self.h[ts]={'qty':qty,'bp':pr,'bd':d,'peak':pr,'cur':pr,'sc':m1s.get(ts,0)}
                    self.trades.append({'d':d,'ts':ts,'a':'B','qty':qty,'pr':pr,'sc':m1s.get(ts,0),'pnl':0,'p':0}); buys+=1
            tot=self.cap+sum(h['qty']*h.get('cur',h['bp']) for h in self.h.values())
            self.equity.append({'d':d,'eq':tot})
            if (idx+1)%50==0: print(f"  {idx+1}/{len(ds)} 持仓{len(self.h)} 资金{self.cap:.0f}")
        for ts,h in list(self.h.items()):
            self.cap+=h['qty']*h.get('cur',h['bp']); self.trades.append({'d':ds[-1],'ts':ts,'a':'F','qty':h['qty'],'pr':h.get('cur',h['bp']),'pnl':h['qty']*(h.get('cur',h['bp'])-h['bp']),'p':0,'r':'end'})
        self.h={}
        final=self.equity[-1]['eq']; ret=(final/self.start_cap-1)*100
        sells=[t for t in self.trades if t['a'] in ('S','F')]
        wins=[t for t in sells if t['pnl']>0]; losses=[t for t in sells if t['pnl']<=0]
        wr=len(wins)/max(len(sells),1)*100
        aw=np.mean([t['p'] for t in wins]) if wins else 0
        al=np.mean([t['p'] for t in losses]) if losses else 0
        peak=0; mdd=0
        for e in self.equity:
            if e['eq']>peak: peak=e['eq']
            dd=(peak-e['eq'])/peak*100
            if dd>mdd: mdd=dd
        rets=[]; prev=self.equity[0]['eq']
        for e in self.equity[1:]:
            rets.append((e['eq']-prev)/prev); prev=e['eq']
        sharpe=np.mean(rets)/max(np.std(rets),0.0001)*np.sqrt(252) if rets else 0
        print(f"\n{'='*70}"); print("📈 V13.3 Alpha二次排序结果"); print('='*70)
        print(f"  总收益率: %+.2f%%" % ret)
        print(f"  最大回撤: %.2f%%" % mdd)
        print(f"  夏普比: %.2f" % sharpe)
        print(f"  卡玛比: %.2f" % (ret/max(mdd,0.01)))
        print(f"  胜率: %.1f%% (%d/%d)" % (wr,len(wins),len(sells)))
        print(f"  平均盈利: %+.2f%%" % aw)
        print(f"  平均亏损: %+.2f%%" % al)
        print(f"  盈亏比: %.2f" % (abs(aw/max(al,0.01)) if al else float('inf')))
        self.close()

if __name__=='__main__':
    bt=BT(1000000); bt.run()
