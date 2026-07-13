#!/usr/bin/env python3
"""
bt_v13_baseline.py — V13.2基准回测（与方案E同条件）
用于对比：剔除Alpha加分的纯M1引擎表现
2026-07-13 by Main
"""

import pymysql, numpy as np
from collections import defaultdict
from datetime import datetime, date, timedelta
import sys, os, warnings
warnings.filterwarnings('ignore')

def get_db_pass():
    try:
        with open('/etc/mysql/debian.cnf') as f:
            for line in f:
                if line.strip().startswith('password'):
                    return line.split('=')[1].strip()
    except: pass
    return 'iXve1rVBXfdA4tL9'

import pymysql.cursors as cursors
DB_CONFIG = {
    'host': 'localhost',
    'user': 'debian-sys-maint',
    'password': get_db_pass(),
    'database': 'stock_db_v2',
    'charset': 'utf8mb4',
    'cursorclass': cursors.DictCursor,
}

class BaselineBacktest:
    def __init__(self, start_capital=1000000):
        self.cap = start_capital
        self.start_cap = start_capital
        self.holdings = {}
        self.trades = []
        self.equity = []
        self.db = pymysql.connect(**DB_CONFIG)
    
    def close(self):
        if self.db: self.db.close()
    
    def get_pool(self):
        with self.db.cursor() as c:
            c.execute("SELECT ts_code FROM watch_pool")
            return [r['ts_code'] for r in c.fetchall()]
    
    def get_dates(self, start, end):
        with self.db.cursor() as c:
            c.execute("SELECT DISTINCT trade_date FROM daily_kline WHERE trade_date>=%s AND trade_date<=%s ORDER BY trade_date", (start, end))
            return [str(r['trade_date']) for r in c.fetchall()]
    
    def get_season(self, d):
        with self.db.cursor() as c:
            c.execute("SELECT season FROM season_state WHERE trade_date=%s", (d,))
            r = c.fetchone()
        season = r['season'] if r else 'chaos'
        mapping = {'summer':'summer','spring':'spring','weak_spring':'spring','chaos_spring':'chaos_spring','chaos':'chaos','chaos_autumn':'chaos_autumn','weak_autumn':'weak_autumn','autumn':'autumn','winter':'winter'}
        return mapping.get(season, 'chaos')
    
    def get_config(self, season):
        # V13.2 原始参数
        params = {
            'summer':      {'buy':68,'hold':55,'stop':0.08,'max_pos':0.50,'per':0.30,'cooldown':0},
            'chaos_spring':{'buy':70,'hold':55,'stop':0.08,'max_pos':0.40,'per':0.25,'cooldown':0},
            'spring':      {'buy':70,'hold':55,'stop':0.07,'max_pos':0.40,'per':0.25,'cooldown':0},
            'chaos':       {'buy':72,'hold':55,'stop':0.08,'max_pos':0.35,'per':0.20,'cooldown':3},
            'chaos_autumn':{'buy':72,'hold':50,'stop':0.06,'max_pos':0.20,'per':0.12,'cooldown':5},
            'weak_autumn': {'buy':75,'hold':50,'stop':0.05,'max_pos':0.15,'per':0.08,'cooldown':7},
            'autumn':      {'buy':78,'hold':45,'stop':0.05,'max_pos':0.10,'per':0.05,'cooldown':10},
            'winter':      {'buy':85,'hold':40,'stop':0.03,'max_pos':0.05,'per':0.03,'cooldown':15},
        }
        return params.get(season, params['chaos'])
    
    def get_m1(self, d, codes):
        if not codes:
            return {}
        res = {}
        with self.db.cursor() as c:
            for i in range(0, len(codes), 200):
                chunk = codes[i:i+200]
                ids = ','.join(["'%s'" % c for c in chunk])
                c.execute("SELECT ts_code, composite_score FROM strategy_signal WHERE trade_date='%s' AND ts_code IN (%s)" % (d, ids))
                for r in c.fetchall():
                    res[r['ts_code']] = float(r['composite_score'] or 50)
        return res
    
    def get_close(self, d, codes):
        if not codes:
            return {}
        res = {}
        with self.db.cursor() as c:
            for i in range(0, len(codes), 300):
                chunk = codes[i:i+300]
                ids = ','.join(["'%s'" % c for c in chunk])
                c.execute("SELECT ts_code, close FROM daily_kline WHERE trade_date='%s' AND ts_code IN (%s)" % (d, ids))
                for r in c.fetchall():
                    res[r['ts_code']] = float(r['close'])
        return res
    
    def get_high(self, d, codes):
        if not codes:
            return {}
        res = {}
        with self.db.cursor() as c:
            for i in range(0, len(codes), 300):
                chunk = codes[i:i+300]
                ids = ','.join(["'%s'" % c for c in chunk])
                c.execute("SELECT ts_code, high FROM daily_kline WHERE trade_date='%s' AND ts_code IN (%s)" % (d, ids))
                for r in c.fetchall():
                    res[r['ts_code']] = float(r['high'])
        return res
    
    def run(self, start='2024-09-01', end='2026-07-10'):
        print("=" * 60)
        print("V13.2基准回测（无Alpha加分）")
        print("资金: %.0f | 时间: %s ~ %s" % (self.start_cap, start, end))
        print("=" * 60)
        
        pool = self.get_pool()
        pool_set = set(pool)
        print("股票: %d只" % len(pool))
        
        dates = self.get_dates(start, end)
        print("交易日: %d天" % len(dates))
        date_set = set(dates)
        
        total_buys = 0
        
        for idx, d in enumerate(dates):
            season = self.get_season(d)
            cfg = self.get_config(season)
            
            # 止损 + 续持检查
            to_sell = []
            for ts, h in list(self.holdings.items()):
                cp = self.get_close(d, [ts]).get(ts, None)
                hp = self.get_high(d, [ts]).get(ts, None)
                if cp is None or hp is None:
                    continue
                h['cur'] = cp
                if hp > h.get('peak', 0):
                    h['peak'] = hp
                
                dd = (h['peak'] - cp) / h['peak'] if h['peak'] > 0 else 0
                buy_idx = dates.index(h['buy_date'])
                days_held = idx - buy_idx
                
                if days_held >= 60:
                    to_sell.append((ts, 'expire'))
                    continue
                if dd >= cfg['stop']:
                    to_sell.append((ts, 'stop'))
                    continue
                if days_held % 10 == 0:
                    m1 = self.get_m1(d, [ts]).get(ts, 50)
                    if m1 < cfg['hold']:
                        to_sell.append((ts, 'score'))
            
            for ts, reason in to_sell:
                h = self.holdings.pop(ts, None)
                if not h:
                    continue
                price = h.get('cur', h['buy'])
                pnl = h['qty'] * (price - h['buy'])
                pnl_p = (price - h['buy']) / h['buy'] * 100
                self.cap += h['qty'] * price
                self.trades.append({'date':d,'ts':ts,'act':'SELL','qty':h['qty'],'price':price,'pnl':round(pnl,2),'pnl_p':round(pnl_p,2),'reason':reason})
                total_buys += 0  # count in buy
            
            # 买入
            used = sum(h['qty'] * h.get('cur', h['buy']) for h in self.holdings.values())
            equity = self.cap + used
            avail = equity * cfg['max_pos'] - used
            
            in_cd = any(t['date'] > str(date.fromisoformat(d) - timedelta(days=cfg['cooldown'])) for t in self.trades[-5:] if t['act']=='SELL' and t['reason']=='stop')
            
            if not in_cd and avail > 0:
                cands = [ts for ts in pool if ts not in self.holdings]
                m1s = self.get_m1(d, cands)
                cands = [ts for ts in cands if ts in m1s and m1s[ts] >= cfg['buy']]
                cands.sort(key=lambda ts: -m1s[ts])
                
                closes = self.get_close(d, cands[:3])
                for ts in cands[:3]:
                    if avail <= 0:
                        break
                    price = closes.get(ts, 0)
                    if price <= 0:
                        continue
                    qty = int(avail * cfg['per'] / price)
                    if qty <= 0:
                        continue
                    cost = qty * price
                    avail -= cost
                    self.cap -= cost
                    self.holdings[ts] = {'qty':qty,'buy':price,'buy_date':d,'peak':price,'cur':price,'score':m1s.get(ts,0)}
                    self.trades.append({'date':d,'ts':ts,'act':'BUY','qty':qty,'price':price,'score':m1s.get(ts,0),'pnl':0,'pnl_p':0})
                    total_buys += 1
            
            # equity
            total = self.cap
            for h in self.holdings.values():
                total += h['qty'] * h.get('cur', h['buy'])
            self.equity.append({'date':d,'equity':total})
            
            if (idx+1)%50==0:
                print("  %d/%d | 持仓%d | 资金%.0f" % (idx+1, len(dates), len(self.holdings), self.cap))
        
        # 平仓
        for ts, h in list(self.holdings.items()):
            self.cap += h['qty'] * h.get('cur', h['buy'])
            self.trades.append({'date':dates[-1],'ts':ts,'act':'FINAL','qty':h['qty'],'price':h.get('cur',h['buy']),'pnl':h['qty']*(h.get('cur',h['buy'])-h['buy']),'pnl_p':0,'reason':'end'})
        self.holdings = {}
        
        # 统计
        final = self.equity[-1]['equity']
        ret = (final/self.start_cap-1)*100
        
        sells = [t for t in self.trades if t['act'] in ('SELL','FINAL')]
        wins = [t for t in sells if t['pnl']>0]
        losses = [t for t in sells if t['pnl']<=0]
        wr = len(wins)/max(len(sells),1)*100
        aw = np.mean([t['pnl_p'] for t in wins]) if wins else 0
        al = np.mean([t['pnl_p'] for t in losses]) if losses else 0
        
        peak = 0
        mdd = 0
        for e in self.equity:
            if e['equity']>peak: peak=e['equity']
            dd=(peak-e['equity'])/peak*100
            if dd>mdd: mdd=dd
        
        rets = []
        prev = self.equity[0]['equity']
        for e in self.equity[1:]:
            rets.append((e['equity']-prev)/prev)
            prev=e['equity']
        sharpe = np.mean(rets)/max(np.std(rets),0.0001)*np.sqrt(252) if rets else 0
        
        print("\n"+"="*70)
        print("📈 V13.2基准回测结果")
        print("="*70)
        print("  总收益率: %+.2f%%" % ret)
        print("  最大回撤: %.2f%%" % mdd)
        print("  夏普比: %.2f" % sharpe)
        print("  卡玛比: %.2f" % (ret/max(mdd,0.01)))
        print("  交易笔数: %d" % (total_buys + len(sells)))
        print("  胜率: %.1f%% (%d/%d)" % (wr, len(wins), len(sells)))
        print("  平均盈利: %+.2f%%" % aw)
        print("  平均亏损: %+.2f%%" % al)
        print("  盈亏比: %.2f" % (abs(aw/max(al,0.01)) if al else float('inf')))
        
        self.close()

if __name__ == '__main__':
    bt = BaselineBacktest(1000000)
    bt.run()
