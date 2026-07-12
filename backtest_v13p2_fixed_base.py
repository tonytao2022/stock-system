#!/usr/bin/env python3
"""
V13.2 回测 — 固定BASE(10万) + 补仓V2 + 准确报表
"""
import os, sys, json, time, math
from datetime import datetime
import pandas as pd
import numpy as np
sys.path.insert(0, '/opt/stock-analyzer')
import db_config

MAX_POS = 8
MAX_DAILY_BUY = 5
BASE = 100000; COMM = 0.001; STAMP = 0.0005
ADD_POS_PCT = -8; ADD_STOP_PCT = 15
SEASON_ORDER = ['summer','spring','weak_spring','chaos_spring','chaos','chaos_autumn','weak_autumn','autumn','winter']
LABEL = {'summer':'☀️夏季','spring':'🌸春季','weak_spring':'⛅弱春','chaos_spring':'🌤️混沌春','chaos':'🌪️混沌','chaos_autumn':'☁️混沌秋','weak_autumn':'⛅弱秋','autumn':'🍂秋季','winter':'❄️冬季'}

def make_params(buy_min):
    return {
        'summer':{'buy_min':buy_min,'max_hold':30,'sl_t1':12,'sl_t2':9,'trail':18,'sgl':50,'ttl':50},
        'spring':{'buy_min':buy_min,'max_hold':30,'sl_t1':12,'sl_t2':9,'trail':15,'sgl':35,'ttl':40},
        'weak_spring':{'buy_min':buy_min,'max_hold':25,'sl_t1':11,'sl_t2':8,'trail':15,'sgl':35,'ttl':40},
        'chaos_spring':{'buy_min':buy_min,'max_hold':25,'sl_t1':11,'sl_t2':8,'trail':15,'sgl':20,'ttl':35},
        'chaos':{'buy_min':buy_min,'max_hold':25,'sl_t1':10,'sl_t2':8,'trail':12,'sgl':20,'ttl':30},
        'chaos_autumn':{'buy_min':buy_min,'max_hold':20,'sl_t1':8,'sl_t2':6,'trail':10,'sgl':15,'ttl':20},
        'weak_autumn':{'buy_min':buy_min,'max_hold':20,'sl_t1':8,'sl_t2':6,'trail':12,'sgl':20,'ttl':25},
        'autumn':{'buy_min':buy_min,'max_hold':20,'sl_t1':10,'sl_t2':8,'trail':12,'sgl':30,'ttl':35},
        'winter':{'buy_min':85,'max_hold':10,'sl_t1':5,'sl_t2':4,'trail':8,'sgl':5,'ttl':10},
    }

def main(start_date, end_date, buy_min):
    pwd = db_config._get_password()  # 先读取密码
    conn = pymysql.connect(host='127.0.0.1', port=3306, user='debian-sys-maint',
                           password=pwd, database='stock_db_v2',
                           charset='utf8mb4', cursorclass=pymysql.cursors.DictCursor)
    cur = conn.cursor()
    PARAMS = make_params(buy_min)
    INITIAL = 1_000_000; TOTAL = INITIAL

    cur.execute("SELECT ts_code, name FROM backtest_pool ORDER BY ts_code")
    pool = {s['ts_code']: s['name'] for s in cur.fetchall()}
    codes = list(pool.keys())
    print(f"📦 {len(codes)}只 | {start_date}~{end_date} | 买入线{buy_min} | BASE{BASE/1e4:.0f}万")

    cur.execute("SELECT ts_code,trade_date,composite_score FROM strategy_signal WHERE trade_date>=%s AND trade_date<=%s AND composite_score IS NOT NULL", (start_date,end_date))
    scores = {}
    for r in cur.fetchall():
        scores[(r['ts_code'],r['trade_date'].strftime('%Y-%m-%d'))] = float(r['composite_score'])

    cur.execute("SELECT trade_date,season FROM season_state WHERE trade_date>=%s AND trade_date<=%s", (start_date,end_date))
    seasons = {}
    for r in cur.fetchall():
        td = r['trade_date'].strftime('%Y-%m-%d')
        seasons[td] = r['season']
    seasons.setdefault

    ph = ','.join(['%s']*len(codes))
    cur.execute(f"SELECT ts_code,trade_date,`open`,high,low,`close` FROM daily_kline WHERE ts_code IN ({ph}) AND trade_date>=%s AND trade_date<=%s ORDER BY trade_date,ts_code", (*codes,start_date,end_date))
    kline = {}
    dset = set()
    for r in cur.fetchall():
        td = r['trade_date'].strftime('%Y-%m-%d'); dset.add(td)
        kline[(r['ts_code'],td)] = {'o':float(r['open']),'h':float(r['high']),'l':float(r['low']),'c':float(r['close'])}
    alld = sorted(dset)
    conn.close()

    add_triggered=0; add_executed=0; half_sell_triggered=0
    trades=[]; pf={}; cash=INITIAL; pv=0

    for di,dt in enumerate(alld):
        if di%20==0 and di>0:
            el=time.time()-st
        season=seasons.get(dt,'chaos'); p=PARAMS.get(season,PARAMS['chaos'])
        sell_list=[]
        for code,pos in list(pf.items()):
            k = kline.get((code,dt))
            if k is None: pos['hd']+=1; continue
            o,h,l,c=k['o'],k['h'],k['l'],k['c']
            bp=pos['bp']; entry_bp=pos.get('entry_bp',bp)
            hd=pos['hd']+1; pos['hd']=hd; hi=pos['hi']
            if h>hi: hi=h
            if c>hi: hi=c
            pos['hi']=hi
            if not pos.get('added',False):
                if l<=entry_bp*(1+ADD_POS_PCT/100) and c>0:
                    add_triggered+=1
                    if scores.get((code,dt),0)>=p['buy_min']:
                        add_shares=int(pos['amt']/(c*1.005)/100)*100
                        if add_shares>=100:
                            add_actual=add_shares*c*1.005
                            if add_actual+add_actual*COMM<=cash:
                                cash-=add_actual+add_actual*COMM; pv+=add_actual
                                ns=pos['shares']+add_shares; nc=pos['amt']+add_actual
                                pos['bp']=nc/ns; pos['shares']=ns; pos['amt']=nc; pos['added']=True; pos['add_price']=c
                                add_executed+=1
                                trades.append({'code':code,'name':pos['name'],'buy_dt':dt,'sell_dt':dt,'hd':0,'bp':round(c*1.005,2),'sp':round(c*1.005,2),'amt':round(add_actual),'ppct':0.0,'pcny':0,'fee':round(add_actual*COMM),'rsn':f'补仓均价{pos["bp"]:.2f}','es':season,'tier':'ADD'})
            sl_pct=ADD_STOP_PCT if pos.get('added',False) else (p['sl_t1'] if pos['tier']=='T1' else p['sl_t2'])
            spx=bp*(1-sl_pct/100); sell=False; ex_px=c; reason=''
            if l<=spx: sell=True; reason=f'止损-{sl_pct}%'; ex_px=spx
            elif c<=hi*(1-p['trail']/100) and c<hi*0.98:
                ts=scores.get((code,dt),0)
                if ts>=p['buy_min'] and not pos.get('halved',False):
                    hs=pos['shares']//2
                    if hs>=100:
                        hr=hs*ex_px; hf=hr*COMM+hr*STAMP; cb=hr-hf
                        rs=pos['shares']-hs; rc=pos['amt']*(rs/pos['shares'])
                        pos['shares']=rs; pos['amt']=rc; pos['hi']=c; pos['halved']=True
                        cash+=cb; pv-=rc; half_sell_triggered+=1
                        trades.append({'code':code,'name':pos['name'],'buy_dt':pos['buy_dt'],'sell_dt':dt,'hd':hd,'bp':round(bp,2),'sp':round(ex_px,2),'amt':round(hr),'ppct':round((ex_px/bp-1)*100,2),'pcny':round(hr-(hr*hs/(hs+rs))),'fee':round(hf),'rsn':f'半仓止盈{p["trail"]}%','es':season,'tier':'HALF'})
                        continue
                sell=True; reason=f'止盈回撤{p["trail"]}%'; ex_px=c
            elif hd>=p['max_hold']: sell=True; reason=f'上限{p["max_hold"]}d'; ex_px=c
            if not sell: continue
            ppct=(ex_px/bp-1)*100; pcny=pos['amt']*(ex_px/bp-1)
            rev=pos['shares']*ex_px; fee=rev*COMM+rev*STAMP
            cash+=rev-fee; pv-=pos['amt']
            trades.append({'code':code,'name':pos['name'],'buy_dt':pos['buy_dt'],'sell_dt':dt,'hd':hd,'bp':round(bp,2),'sp':round(ex_px,2),'amt':round(pos['amt']),'ppct':round(ppct,2),'pcny':round(pcny),'fee':round(fee),'rsn':reason,'es':season,'tier':pos.get('tier','?')})
            sell_list.append(code)
        for c in sell_list: del pf[c]
        if len(pf)<MAX_POS:
            cand=[]
            for code in codes:
                if code in pf: continue
                sc=scores.get((code,dt),0)
                if sc<p['buy_min']: continue
                kk=kline.get((code,dt))
                if kk is None: continue
                cand.append((code,sc,kk['c']))
            cand.sort(key=lambda x:x[1],reverse=True)
            for code,sc,cp in cand[:min(MAX_DAILY_BUY,MAX_POS-len(pf))]:
                if len(pf)>=MAX_POS: break
                tier='T1' if sc>=75 else 'T2'
                sm=TOTAL*p['sgl']/100; tr=TOTAL*p['ttl']/100-pv
                amt=min(BASE,sm,max(0,tr))
                bpx=cp*1.005; shares=int(amt/bpx/100)*100
                if shares<100: continue
                actual=shares*bpx
                if actual+actual*COMM>cash: continue
                cash-=actual+actual*COMM; pv+=actual
                pf[code]={'name':pool[code],'buy_dt':dt,'bp':bpx,'entry_bp':bpx,'shares':shares,'amt':actual,'hi':cp,'hd':0,'tier':tier,'es':season,'added':False,'add_price':0.0,'halved':False}

    # 准确报表
    final_equity = cash + sum(pos['amt'] for pos in pf.values())
    tp = final_equity - INITIAL
    rp = tp / INITIAL * 100

    df = pd.DataFrame(trades)
    df_main = df[~df['tier'].isin(['ADD','HALF'])].copy()
    n = len(df_main)
    wins = df_main[df_main['ppct']>0]; loses = df_main[df_main['ppct']<=0]
    wr = len(wins)/n*100 if n>0 else 0
    pf_ratio = abs(wins['pcny'].sum()/loses['pcny'].sum()) if len(loses)>0 and loses['pcny'].sum()!=0 else float('inf')
    sp = df_main['ppct'].mean()/df_main['ppct'].std()*math.sqrt(252) if df_main['ppct'].std()>0 else 0
    df_s = df_main.sort_values('sell_dt')
    cum = df_s['pcny'].cumsum()
    cmx = cum.cummax()
    dd = (cmx-cum)/INITIAL*100
    mdd = dd.max() if len(dd)>0 else 0
    car = (rp/mdd) if mdd>0 else float('inf')

    print(f"\n{'='*65}")
    print(f"  📊 V13.2 回测（固定BASE·买入线{buy_min}·补仓V2）{start_date}~{end_date}")
    print(f"{'='*65}")
    print(f"  💰 起始{INITIAL/1e4:.0f}万 | 期末{final_equity:,.0f} | 净 {tp:+,.0f} ({rp:+.2f}%)")
    print(f"  📈 {n}笔主交易 | 补仓触发{add_triggered}次/执行{add_executed}次 | 半仓{half_sell_triggered}次")
    print(f"  {n}笔 | {wr:.1f}% | 盈亏比{pf_ratio:.2f} | 回撤{mdd:.2f}%")
    print(f"  📊 夏普{sp:.2f} | 卡玛{car:.2f}")
    print(f"  均盈{wins['ppct'].mean():+.2f}% | 均亏{loses['ppct'].mean():+.2f}% | 均持{df_main['hd'].mean():.1f}d")
    print(f"\n{'─'*60}\n  🎯 卖出理由\n{'─'*60}")
    for rsn in df_main['rsn'].unique():
        sd=df_main[df_main['rsn']==rsn]; sw=sd[sd['ppct']>0]
        print(f"  {rsn:18s} {len(sd):4d} {len(sw)/len(sd)*100:5.1f}% {sd['pcny'].sum():+10,.0f}")
    print(f"\n{'─'*60}\n  🌍 入场季节\n{'─'*60}")
    for s in SEASON_ORDER:
        sd=df_main[df_main['es']==s]
        if len(sd)==0: continue
        sw=sd[sd['ppct']>0]
        print(f"  {LABEL.get(s,s):12s} {len(sd):4d} {len(sw)/len(sd)*100:5.1f}% {sd['pcny'].sum():+10,.0f}")
    print(f"\n{'─'*60}\n  ⏱ 持有天数\n{'─'*60}")
    for lbl,lo,hi in [('1-5日',1,5),('6-10日',6,10),('11-15日',11,15),('16-20日',16,20),('21-25日',21,25),('26日+',26,999)]:
        sd=df_main[(df_main['hd']>=lo)&(df_main['hd']<=hi)]
        if len(sd)==0: continue
        sw=sd[sd['ppct']>0]
        print(f"  {lbl:10s} {len(sd):4d} {len(sw)/len(sd)*100:5.1f}% {sd['pcny'].sum():+10,.0f}")
    print(f"\n{'─'*60}\n  📊 补仓明细\n{'─'*60}")
    print(f"  补仓触发: {add_triggered}次 | 补仓执行: {add_executed}次 | 半仓止盈: {half_sell_triggered}次")
    print(f"\n{'─'*60}\n  🏆 Top 5\n{'─'*60}")
    for _,r in df_main.nlargest(5,'pcny').iterrows():
        print(f"  {r['name'][:12]:12s} {r['code'][:8]:>8s} {LABEL.get(r['es'],r['es']):8s} T{r['tier'][1]} {r['hd']:2d}d {r['ppct']:+6.2f}% {r['pcny']:+8,.0f} | {r['rsn']}")
    print(f"\n{'─'*60}\n  💀 Bot 5\n{'─'*60}")
    for _,r in df_main.nsmallest(5,'pcny').iterrows():
        print(f"  {r['name'][:12]:12s} {r['code'][:8]:>8s} {LABEL.get(r['es'],r['es']):8s} T{r['tier'][1]} {r['hd']:2d}d {r['ppct']:+6.2f}% {r['pcny']:+8,.0f} | {r['rsn']}")

    fp = f'/tmp/v13p2_fix{buy_min}_{start_date}_{end_date}_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json'
    with open(fp,'w') as f:
        json.dump({'n':n,'pnl':round(tp),'ret':round(rp,2),'wr':round(wr,1),'mdd':round(mdd,2),
                   'final_equity':round(final_equity),'buy_min':buy_min,
                   'add_triggered':add_triggered,'add_executed':add_executed,'half_sell':half_sell_triggered}, f)
    print(f"\n📁 {fp}")

if __name__=='__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--start',default='2024-01-02')
    ap.add_argument('--end',default='2026-07-10')
    ap.add_argument('--buy-min',type=int,default=74)
    a = ap.parse_args()
    import pymysql
    st = time.time()
    main(a.start, a.end, a.buy_min)
    print(f"⏱ {(time.time()-st)/60:.1f}分钟")
