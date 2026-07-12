#!/usr/bin/env python3
"""V13.1 vs TIDE 同区间对比 + 交易明细"""
import sys, time; sys.path.insert(0,'/opt/stock-analyzer')
from db_config import get_connection; from collections import defaultdict
INIT=1000000; MP=8; MS=0.20; CM=0.0008; DF='2026-04-01'; DT='2026-07-03'

def bt(label,buy,hm,st1,st2,tr,tp,tbl,scol,hs=False):
    print(f"\n{'='*70}")
    print(f"  {label}: 买入≥{buy} 持有{hm}d 止损{st1}/{st2}% 移动止盈{tr}% 总仓{tp}%")
    conn=get_connection(); cur=conn.cursor()
    if hs:
        cur.execute(f"SELECT s.ts_code,s.{scol} score,s.season,s.trade_date FROM {tbl} s WHERE s.trade_date>='{DF}' AND s.trade_date<='{DT}' AND s.{scol}>={buy} ORDER BY s.trade_date")
    else:
        cur.execute(f"SELECT s.ts_code,s.{scol} score,s.trade_date FROM {tbl} s WHERE s.trade_date>='{DF}' AND s.trade_date<='{DT}' AND s.{scol}>={buy} ORDER BY s.trade_date")
    rows=cur.fetchall(); print(f"  评分≥{buy}: {len(rows)}条")
    codes=sorted(set(str(r['ts_code']) for r in rows))
    kl={}
    for c in codes:
        cur.execute(f"SELECT trade_date,close FROM daily_kline WHERE ts_code='{c}' AND trade_date>='{DF}' AND trade_date<='{DT}'")
        for r in cur.fetchall(): kl[(c,str(r['trade_date']))]=float(r['close']) if r['close'] else 0
    sm={}
    if not hs:
        for c in codes:
            cur.execute(f"SELECT trade_date,season FROM backtest_score_daily WHERE ts_code='{c}' AND trade_date>='{DF}' AND trade_date<='{DT}'")
            for r in cur.fetchall(): sm[(c,str(r['trade_date']))]=str(r['season'] or 'chaos')
    cur.close(); conn.close()
    dl=defaultdict(list)
    for r in rows:
        c=str(r['ts_code']); td=str(r['trade_date'])
        dl[td].append({'c':c,'s':float(r['score']) if r['score'] else 0,'sn':str(r['season'] if hs else sm.get((c,td),'chaos'))})
    ad=sorted(dl.keys()); print(f"  {ad[0]}~{ad[-1]} ({len(ad)}交易天)")
    cash=INIT; pos={}; trs=[]; t0=time.time()
    for today in ad:
        ts=[]
        for code,p in list(pos.items()):
            p['d']+=1; cp=kl.get((code,today),0)
            if cp==0: continue
            if cp>p.get('h',p['b']): p['h']=cp
            if p['d']<=1: continue
            dd=(cp-p['h'])/p['h']*100
            if dd<=-tr: ts.append((code,cp,f'止盈回撤{dd:.0f}%')); continue
            pnl=(cp-p['b'])/p['b']*100
            sk=st2 if p['d']>=2 else st1
            if pnl<=sk: ts.append((code,cp,f'止损{sk}%({pnl:.1f}%)')); continue
            if p['d']>=hm: ts.append((code,cp,f'到期{p["d"]}d')); continue
        for code,price,reason in ts:
            p=pos.pop(code); proc=p['sh']*price*(1-CM); pnl=proc-p['cost']; pnl_p=(price-p['b'])/p['b']*100
            cash+=proc; trs.append({'c':code,'pnl':round(pnl_p,2),'pnl_val':round(pnl,2),'d':p['d'],'r':reason,'sn':p.get('sn','?'),'s':p.get('s',0),'bd':p.get('bd',''),'sp':price})
        if len(pos)<MP:
            cd=[s for s in dl[today] if s['c'] not in pos and kl.get((s['c'],today),0)>0]
            cd.sort(key=lambda x:x['s'],reverse=True)
            for s in cd[:MP-len(pos)]:
                if cash<=0: break; cp=kl.get((s['c'],today),0)
                if cp==0: continue; ba=cash*MS
                if ba<10000: continue; sh=int(ba/cp/100)*100
                if sh<=0: continue; cost=sh*cp*(1+CM)
                if cost>cash: continue
                pos[s['c']]={'b':cp,'sh':sh,'cost':cost,'h':cp,'d':0,'sn':s['sn'],'s':s['s'],'bd':today}; cash-=cost
    last=ad[-1]
    for code,p in list(pos.items()):
        lp=kl.get((code,last),0) or p['b']
        pnl=(lp-p['b'])/p['b']*100; pnl_val=(p['sh']*lp*(1-CM))-p['cost']
        trs.append({'c':code,'pnl':round(pnl,2),'pnl_val':round(pnl_val,2),'d':p['d'],'r':'期末强平','sn':p.get('sn','?'),'s':p.get('s',0),'bd':p.get('bd',''),'sp':lp})
    fp=sum(kl.get((c,last),p['b'])*p['sh'] for c,p in pos.items()) if pos else 0
    TV=cash+fp; ret=(TV-INIT)/INIT*100; el=time.time()-t0
    wins=[t for t in trs if t['pnl']>0]; losses=[t for t in trs if t['pnl']<=0]
    wr=len(wins)/len(trs)*100 if trs else 0
    aw=sum(t['pnl'] for t in wins)/len(wins) if wins else 0
    al=sum(t['pnl'] for t in losses)/len(losses) if losses else 0
    rt=aw/abs(al) if losses and al!=0 else 0
    pf_=sum(t['pnl_val'] for t in wins)/abs(sum(t['pnl_val'] for t in losses)) if losses and sum(t['pnl_val'] for t in losses)!=0 else 0
    agd=sum(t['d'] for t in trs)/len(trs) if trs else 0
    # 最大回撤
    peak=INIT; mdd=0
    cash2=INIT; pos2={}
    for today in ad:
        tss=[]
        for code,p in list(pos2.items()):
            p['d']+=1; cp=kl.get((code,today),0)
            if cp==0: continue
            if cp>p.get('h',p['b']): p['h']=cp
            if p['d']<=1: continue
            dd=(cp-p['h'])/p['h']*100
            if dd<=-tr: tss.append((code,cp)); continue
            pnl=(cp-p['b'])/p['b']*100
            sk=st2 if p['d']>=2 else st1
            if pnl<=sk: tss.append((code,cp)); continue
            if p['d']>=hm: tss.append((code,cp)); continue
        for code,price in tss:
            p=pos2.pop(code); cash2+=p['sh']*price*(1-CM)
        if len(pos2)<MP:
            cd=[s for s in dl[today] if s['c'] not in pos2 and kl.get((s['c'],today),0)>0]
            cd.sort(key=lambda x:x['s'],reverse=True)
            for s in cd[:MP-len(pos2)]:
                if cash2<=0: break; cp=kl.get((s['c'],today),0)
                if cp==0: continue; ba=cash2*MS
                if ba<10000: continue; sh=int(ba/cp/100)*100
                if sh<=0: continue; cost=sh*cp*(1+CM)
                if cost>cash2: continue
                pos2[s['c']]={'b':cp,'sh':sh,'cost':cost,'h':cp,'d':0}; cash2-=cost
        pv=sum(kl.get((c,today),p['b'])*p['sh'] for c,p in pos2.items())
        nv=cash2+pv
        if nv>peak: peak=nv
        dd_=(peak-nv)/peak*100
        if dd_>mdd: mdd=dd_
    # 明细
    print(f"\n  交易明细:")
    print(f"  {'#':>3s} {'代码':>10s} {'买入':>10s} {'卖出':>10s} {'收益率':>8s} {'收益额':>10s} {'天数':>4s} {'原因':<18s} {'季节':<10s}")
    print(f"  {'-'*85}")
    sw=0; sl=0
    for i,t in enumerate(trs,1):
        wl='🟢' if t['pnl']>0 else '🔴'
        if t['pnl']>0: sw+=1
        else: sl+=1
        print(f"  {i:3d}. {t['c']:>10s} {t.get('bd','?'):>10s} {str(t.get('sp',0)):>10s} {t['pnl']:>+7.1f}% {t.get('pnl_val',0):>9.0f} {t['d']:>3d}d {t['r']:<18s} {t.get('sn','?'):<10s} {wl}")
    print(f"\n  === {label} 结果 ===")
    print(f"  净收益: {TV:.0f} ({ret:+.2f}%)")
    print(f"  年化: {ret/len(ad)*252:+.2f}%")
    print(f"  最大回撤: {mdd:.2f}%")
    print(f"  交易: {len(trs)}笔 | 胜{sw}({sw/len(trs)*100:.0f}%) 负{sl}({sl/len(trs)*100:.0f}%)" )
    print(f"  盈亏比: {rt:.2f} | 盈利因子: {pf_:.2f} | 平均持有: {agd:.1f}d")
    print(f"  用时: {el:.0f}s")
    return {'ret':ret,'tr':len(trs),'wr':sw/len(trs)*100 if trs else 0,'rt':rt,'d':len(ad),'mdd':mdd,'pf':pf_,'avgd':agd,'sw':sw,'sl':sl}

r1=bt('V13.1',75,30,-7,-5,15,30,'backtest_score_daily','composite_score',True)
r2=bt('TIDE',82,30,-7,-5,15,80,'tide_score_signal','tide_score',False)
print(f"\n{'='*70}")
print(f"V13.1 vs TIDE 最终对比 ({DF}~{DT})")
print('='*70)
print(f"{'指标':<18s} {'V13.1':>10s} {'TIDE':>10s} {'差距':>10s}")
print(f"  {'-'*50}")
for k in ['ret','mdd','tr','sw','sl','wr','rt','pf','avgd']:
    v=r1[k]; t=r2[k]
    suf='%' if k in ('ret','wr') else ''
    if isinstance(v,(int,float)):
        print(f"  {k:<18s} {v:>9.1f}{suf:>1s} {t:>9.1f}{suf:>1s} {t-v:>+9.1f}")
    else:
        print(f"  {k:<18s} {v:>9} {t:>9} {t-v:>+9}")
print(f"\nV13.1: 买入≥75 | 持30d | 止损-7%/-5% | 移动止盈15% | 单票≤20% | 总仓≤30%")
print(f"TIDE:  买入≥82 | 持30d | 止损-7%/-5% | 移动止盈15% | 单票≤20% | 总仓≤80%")
print(f"同区间: {DF}~{DT}")
