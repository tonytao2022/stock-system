#!/usr/bin/env python3
"""
V13.2 多轮随机抽样验证 — 74线固定BASE
====================================
从849只中随机抽取200只×20轮，验证收益稳定性
"""

import os, sys, json, time, math, random
from datetime import datetime
import pandas as pd
import numpy as np
sys.path.insert(0, '/opt/stock-analyzer')
import db_config

# --- 参数 ---
BUY_MIN = 74
MAX_POS = 8; MAX_DAILY_BUY = 5; BASE = 100000
COMM = 0.001; STAMP = 0.0005
ADD_POS_PCT = -8; ADD_STOP_PCT = 15
INITIAL = 1_000_000

SEASON_ORDER = ['summer','spring','weak_spring','chaos_spring','chaos','chaos_autumn','weak_autumn','autumn','winter']
LABEL = {'summer':'☀️夏季','spring':'🌸春季','weak_spring':'⛅弱春','chaos_spring':'🌤️混沌春','chaos':'🌪️混沌','chaos_autumn':'☁️混沌秋','weak_autumn':'⛅弱秋','autumn':'🍂秋季','winter':'❄️冬季'}

PARAMS = {
    'summer':{'buy_min':BUY_MIN,'max_hold':30,'sl_t1':12,'sl_t2':9,'trail':18,'sgl':50,'ttl':50},
    'spring':{'buy_min':BUY_MIN,'max_hold':30,'sl_t1':12,'sl_t2':9,'trail':15,'sgl':35,'ttl':40},
    'weak_spring':{'buy_min':BUY_MIN,'max_hold':25,'sl_t1':11,'sl_t2':8,'trail':15,'sgl':35,'ttl':40},
    'chaos_spring':{'buy_min':BUY_MIN,'max_hold':25,'sl_t1':11,'sl_t2':8,'trail':15,'sgl':20,'ttl':35},
    'chaos':{'buy_min':BUY_MIN,'max_hold':25,'sl_t1':10,'sl_t2':8,'trail':12,'sgl':20,'ttl':30},
    'chaos_autumn':{'buy_min':BUY_MIN,'max_hold':20,'sl_t1':8,'sl_t2':6,'trail':10,'sgl':15,'ttl':20},
    'weak_autumn':{'buy_min':BUY_MIN,'max_hold':20,'sl_t1':8,'sl_t2':6,'trail':12,'sgl':20,'ttl':25},
    'autumn':{'buy_min':BUY_MIN,'max_hold':20,'sl_t1':10,'sl_t2':8,'trail':12,'sgl':30,'ttl':35},
    'winter':{'buy_min':85,'max_hold':10,'sl_t1':5,'sl_t2':4,'trail':8,'sgl':5,'ttl':10},
}


def run_one_round(all_codes, pool_dict, scores, seasons, kline, alld, seed):
    """跑一轮随机抽样回测"""
    random.seed(seed)

    # 从全量池随机抽取200只作为可操作池
    subset = random.sample(all_codes, min(200, len(all_codes)))
    subset_set = set(subset)

    add_triggered=0; add_executed=0; half_sell_triggered=0
    trades=[]; pf={}; cash=INITIAL; pv=0

    for di, dt in enumerate(alld):
        season = seasons.get(dt, 'chaos')
        p = PARAMS.get(season, PARAMS['chaos'])
        sell_list = []

        for code, pos in list(pf.items()):
            k = kline.get((code, dt))
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
                                pos['bp']=nc/ns; pos['shares']=ns; pos['amt']=nc
                                pos['added']=True; pos['add_price']=c
                                add_executed+=1
                                trades.append({'tier':'ADD'})

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
                        trades.append({'tier':'HALF'})
                        continue
                sell=True; reason=f'止盈回撤{p["trail"]}%'; ex_px=c
            elif hd>=p['max_hold']: sell=True; reason=f'上限{p["max_hold"]}d'; ex_px=c
            if not sell: continue
            ppct=(ex_px/bp-1)*100; pcny=pos['amt']*(ex_px/bp-1)
            rev=pos['shares']*ex_px; fee=rev*COMM+rev*STAMP
            cash+=rev-fee; pv-=pos['amt']
            trades.append({'tier':'MAIN','amd':ppct,'amt':round(pos['amt']),'pcny':round(pcny),'hd':hd})
            sell_list.append(code)
        for c in sell_list: del pf[c]

        if len(pf)<MAX_POS:
            cand=[]
            for code in subset:
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
                sm=INITIAL*p['sgl']/100; tr=INITIAL*p['ttl']/100-pv
                amt=min(BASE,sm,max(0,tr))
                bpx=cp*1.005; shares=int(amt/bpx/100)*100
                if shares<100: continue
                actual=shares*bpx
                if actual+actual*COMM>cash: continue
                cash-=actual+actual*COMM; pv+=actual
                pf[code]={'name':pool_dict.get(code,''),'buy_dt':dt,'bp':bpx,'entry_bp':bpx,
                          'shares':shares,'amt':actual,'hi':cp,'hd':0,'tier':tier,
                          'es':season,'added':False,'add_price':0.0,'halved':False}

    final_equity = cash + sum(pos['amt'] for pos in pf.values())
    tp = final_equity - INITIAL
    rp = tp / INITIAL * 100

    # 最大回撤（从交易记录估算）
    df = pd.DataFrame([t for t in trades if t['tier']=='MAIN'])
    mdd = 0
    if len(df) > 0:
        cum = df['pcny'].cumsum()
        cmx = cum.cummax()
        dd_series = (cmx-cum)/INITIAL*100
        mdd = dd_series.max()

    n = len(df)
    wins = df[df['amd']>0] if len(df)>0 else pd.DataFrame()
    loses = df[df['amd']<=0] if len(df)>0 else pd.DataFrame()
    wr = len(wins)/n*100 if n>0 else 0
    pf_ratio = abs(wins['pcny'].sum()/loses['pcny'].sum()) if len(loses)>0 and loses['pcny'].sum()!=0 else 0
    avg_hold = df['hd'].mean() if len(df)>0 else 0

    return {
        'seed': seed,
        'n': n,
        'pnl': round(tp),
        'ret': round(rp, 2),
        'mdd': round(mdd, 2),
        'wr': round(wr, 1),
        'pf_ratio': round(pf_ratio, 2),
        'avg_hold': round(avg_hold, 1),
        'final_equity': round(final_equity),
        'add_executed': add_executed,
        'half_sell': half_sell_triggered,
    }


def main():
    pwd = db_config._get_password()
    conn = pymysql.connect(host='127.0.0.1', port=3306, user='debian-sys-maint',
                           password=pwd, database='stock_db_v2',
                           charset='utf8mb4', cursorclass=pymysql.cursors.DictCursor)
    cur = conn.cursor()

    start_date='2024-01-02'; end_date='2026-07-10'

    cur.execute("SELECT ts_code, name FROM backtest_pool ORDER BY ts_code")
    pool = {s['ts_code']: s['name'] for s in cur.fetchall()}
    codes = list(pool.keys())
    print(f"📦 全量{len(codes)}只 | 每次随机抽样200只 × 20轮")

    cur.execute("SELECT ts_code,trade_date,composite_score FROM strategy_signal WHERE trade_date>=%s AND trade_date<=%s AND composite_score IS NOT NULL", (start_date,end_date))
    scores = {}
    for r in cur.fetchall():
        scores[(r['ts_code'],r['trade_date'].strftime('%Y-%m-%d'))] = float(r['composite_score'])

    cur.execute("SELECT trade_date,season FROM season_state WHERE trade_date>=%s AND trade_date<=%s", (start_date,end_date))
    seasons = {}
    for r in cur.fetchall():
        td = r['trade_date'].strftime('%Y-%m-%d'); seasons[td] = r['season']

    ph = ','.join(['%s']*len(codes))
    cur.execute(f"SELECT ts_code,trade_date,`open`,high,low,`close` FROM daily_kline WHERE ts_code IN ({ph}) AND trade_date>=%s AND trade_date<=%s ORDER BY trade_date,ts_code", (*codes,start_date,end_date))
    kline = {}; dset = set()
    for r in cur.fetchall():
        td = r['trade_date'].strftime('%Y-%m-%d'); dset.add(td)
        kline[(r['ts_code'],td)] = {'o':float(r['open']),'h':float(r['high']),'l':float(r['low']),'c':float(r['close'])}
    alld = sorted(dset)
    conn.close()
    print(f"📊 {len(scores)}条评分/{len(seasons)}天季节/{len(kline)}条K线")

    # 跑20轮随机抽样
    results = []
    N_ROUNDS = 20
    overall_start = time.time()

    for rnd in range(N_ROUNDS):
        seed = 1000 + rnd
        t0 = time.time()
        res = run_one_round(codes, pool, scores, seasons, kline, alld, seed)
        res['round'] = rnd + 1
        results.append(res)
        elapsed = time.time() - t0
        print(f"  [{rnd+1}/{N_ROUNDS}] ret={res['ret']:+.2f}% | n={res['n']}笔 | mdd={res['mdd']:.2f}% | 补{res['add_executed']}/半{res['half_sell']} | {elapsed:.1f}s")

    # 统计
    df = pd.DataFrame(results)
    rets = df['ret'].values
    mdds = df['mdd'].values
    ns = df['n'].values

    print(f"\n{'='*60}")
    print(f"  📊 74线·20轮随机抽样验证")
    print(f"{'='*60}")
    print(f"  🏆 收益率:")
    print(f"     均值: {rets.mean():+.2f}% | 中位: {np.median(rets):+.2f}%")
    print(f"     最高: {rets.max():+.2f}% | 最低: {rets.min():+.2f}%")
    print(f"     标准差: {rets.std():.2f}% | 变异系数: {rets.std()/abs(rets.mean()):.2f}")
    print(f"  📈 交易笔数:")
    print(f"     均值: {ns.mean():.0f} | 最高: {ns.max()} | 最低: {ns.min()}")
    print(f"  ⚠️ 最大回撤:")
    print(f"     均值: {mdds.mean():.2f}% | 最高: {mdds.max():.2f}% | 最低: {mdds.min():.2f}%")
    print(f"  ✅ 正收益轮数: {len([r for r in rets if r>0])}/{N_ROUNDS}")

    print(f"\n{'─'*60}")
    print(f"  {'轮':>3s} {'收益':>7s} {'笔数':>4s} {'回撤':>6s} {'胜率':>5s} {'盈亏比':>6s} {'均持':>4s}")
    print(f"{'─'*60}")
    for r in results:
        m = '✅' if r['ret'] > 0 else '❌'
        print(f"  {r['round']:3d} {r['ret']:+6.2f}%{m} {r['n']:4d} {r['mdd']:5.2f}% {r['wr']:5.1f}% {r['pf_ratio']:5.2f} {r['avg_hold']:4.1f}d")

    fp = f'/tmp/v13p2_monte74_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json'
    with open(fp,'w') as f:
        json.dump({'N':N_ROUNDS,'ret_mean':round(rets.mean(),2),'ret_std':round(rets.std(),2),
                   'ret_max':round(rets.max(),2),'ret_min':round(rets.min(),2),
                   'positive_rounds':len([r for r in rets if r>0]),
                   'mdd_mean':round(mdds.mean(),2),'mdd_max':round(mdds.max(),2)}, f)
    print(f"\n⏱ {(time.time()-overall_start)/60:.1f}分钟")
    print(f"📁 {fp}")


if __name__=='__main__':
    import pymysql
    main()
