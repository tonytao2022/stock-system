#!/usr/bin/env python3
"""
V8 全量回测 — 夏季增强版 + P6完整评分引擎
==========================================
V7基础上夏季参数优化：买入线70/止损-12%/-9%/持有60天/P4延15天
调用 p6_dual_track_engine.track_momentum() 做真实评分。

用法: python3 backtest_v7_p6engine.py [--limit N]
"""
import os, sys, json, time, math, argparse
from datetime import datetime, date
import pandas as pd
import numpy as np
import pymysql
from pymysql.cursors import DictCursor

# ── Monkey patch: fake engine modules ──
from types import ModuleType
for mod_name in ['engine','engine.sentiment_scorer','engine.block_weights']:
    if mod_name not in sys.modules:
        m = ModuleType(mod_name)
        if mod_name == 'engine.sentiment_scorer':
            m.score_sentiment = lambda *a,**kw: None
            m.SentimentResult = None
        elif mod_name == 'engine.block_weights':
            m.adjust_weights_by_season = lambda *a,**kw: None
        sys.modules[mod_name] = m

sys.path.insert(0, '/opt/stock-analyzer')
from p6_dual_track_engine import track_momentum, MarketContext
import db_config

# ── 季节参数矩阵 ──
SEASON_PARAMS = {
    'summer':          {'buy_min_score':70,'max_hold':60,'stop_loss_t1':12,'stop_loss_t2':9,'p4_enabled':True,'p4_min_score':55,'p4_extension_days':15,'trailing_stop_pct':18,'t2_enabled':True},
    'autumn':          {'buy_min_score':75,'max_hold':20,'stop_loss_t1':7,'stop_loss_t2':5,'p4_enabled':False,'p4_min_score':60,'p4_extension_days':5,'trailing_stop_pct':10,'t2_enabled':True},
    'chaos_spring':    {'buy_min_score':75,'max_hold':25,'stop_loss_t1':10,'stop_loss_t2':8,'p4_enabled':True,'p4_min_score':65,'p4_extension_days':5,'trailing_stop_pct':12,'t2_enabled':False},
    'chaos_autumn':    {'buy_min_score':75,'max_hold':25,'stop_loss_t1':10,'stop_loss_t2':8,'p4_enabled':True,'p4_min_score':65,'p4_extension_days':5,'trailing_stop_pct':12,'t2_enabled':False},
    'chaos':           {'buy_min_score':75,'max_hold':25,'stop_loss_t1':10,'stop_loss_t2':8,'p4_enabled':True,'p4_min_score':65,'p4_extension_days':5,'trailing_stop_pct':12,'t2_enabled':False},
    'spring':          {'buy_min_score':70,'max_hold':20,'stop_loss_t1':8,'stop_loss_t2':6,'p4_enabled':True,'p4_min_score':60,'p4_extension_days':5,'trailing_stop_pct':12,'t2_enabled':True},
    'winter':          {'buy_min_score':85,'max_hold':10,'stop_loss_t1':5,'stop_loss_t2':4,'p4_enabled':False,'p4_min_score':80,'p4_extension_days':3,'trailing_stop_pct':8,'t2_enabled':False},
}
SEASON_ORDER = ['summer','autumn','spring','chaos_spring','chaos','chaos_autumn','winter']
SEASON_LABELS = {'summer':'☀️夏季','autumn':'🍂秋季','spring':'🌸春季','chaos_spring':'🌤️弱春','chaos':'🌪️混沌','chaos_autumn':'🌥️弱秋','winter':'❄️冬季'}

MYSQL_PWD = 'iXve1rVBXfdA4tL9'

def get_conn():
    return pymysql.connect(host='127.0.0.1',port=3306,user='debian-sys-maint',password=MYSQL_PWD,database='stock_db_v2',charset='utf8mb4',cursorclass=DictCursor)

def get_season(conn, d):
    with conn.cursor() as cur:
        cur.execute("SELECT season FROM season_state WHERE trade_date=%s ORDER BY id DESC LIMIT 1", (d,))
        r = cur.fetchone()
    return r['season'] if r else 'chaos'

def get_kline(conn, ts_code, start, end):
    with conn.cursor() as cur:
        cur.execute("SELECT trade_date,`open`,high,low,`close`,pre_close,vol,amount FROM daily_kline WHERE ts_code=%s AND trade_date>=%s AND trade_date<=%s ORDER BY trade_date", (ts_code,start,end))
        rows = cur.fetchall()
    if not rows: return None
    df = pd.DataFrame(rows)
    for c in ['open','high','low','close','pre_close','vol','amount']:
        df[c] = pd.to_numeric(df[c], errors='coerce')
    df['trade_date'] = pd.to_datetime(df['trade_date'])
    return df.set_index('trade_date').sort_index()

def get_tier(score, season):
    p = SEASON_PARAMS.get(season, SEASON_PARAMS['chaos'])
    if score>=75: return 'T1'
    if p['t2_enabled'] and score>=p['buy_min_score']: return 'T2'
    return None

# ── Main ──
def main(start, end, limit=None):
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT ts_code,name FROM backtest_pool ORDER BY ts_code")
        pool = cur.fetchall()
        if limit: pool = pool[:limit]
    print(f"📦 回测池: {len(pool)} 只 | 📅 {start} ~ {end}")

    trades = []
    season_cache = {}
    score_cache = {}   # (ts_code, date) -> score
    start_ts = time.time()

    for i, stock in enumerate(pool):
        ts_code = stock['ts_code']
        name = stock.get('name', ts_code)

        if (i+1)%10==0:
            el = time.time()-start_ts
            print(f"  [{i+1}/{len(pool)}] {ts_code} ... {el:.0f}s ({len(trades)}笔)")

        kline = get_kline(conn, ts_code, start, end)
        if kline is None or len(kline)<60: continue

        pos=0.0; entry_date=None; entry_price=0.0; entry_score=0.0
        entry_season=None; entry_tier=None; hold_days=0
        highest=0.0; trailing_stop=0.0; hard_stop=0.0
        p4_extended=False; p4_extend_days=0; p4_base_max_hold=0

        for idx, (td, row) in enumerate(kline.iterrows()):
            d_str = td.strftime('%Y-%m-%d')
            c = float(row['close']); h=float(row['high']); l=float(row['low'])

            # ── P6引擎评分（带缓存） ──
            cache_key = (ts_code, d_str)
            if cache_key not in score_cache:
                try:
                    ctx = MarketContext({
                        'market_season': 'summer', 'market_regime': 'range',
                        'market_confidence': 0.5, 'market_scoring_strategy': 'momentum',
                        'trade_date': d_str
                    })
                    result = track_momentum(ts_code, ctx)
                    s = result.get('score', 50)
                except Exception as e:
                    s = 50
                score_cache[cache_key] = s if not math.isnan(s) else 50
            score = score_cache[cache_key]
            if score<=0: continue

            # ──── 买入 ────
            if pos==0:
                if d_str not in season_cache:
                    season_cache[d_str]=get_season(conn,d_str)
                cur_season=season_cache[d_str]
                params=SEASON_PARAMS.get(cur_season,SEASON_PARAMS['chaos'])
                tier=get_tier(score,cur_season)
                if tier and score>=params['buy_min_score']:
                    pos=100000.0; entry_date=d_str; entry_price=c; entry_score=score
                    entry_season=cur_season; entry_tier=tier; hold_days=0
                    highest=c; p4_extended=False; p4_extend_days=0
                    sl=params['stop_loss_t1'] if tier=='T1' else params['stop_loss_t2']
                    hard_stop=c*(1-sl/100)
                    trailing_stop=c*(1-params['trailing_stop_pct']/100)
                    p4_base_max_hold=params['max_hold']

            if pos==0: continue

            # ──── 持仓 ────
            hold_days+=1
            if c>highest:
                highest=c
                if d_str not in season_cache:
                    season_cache[d_str]=get_season(conn,d_str)
                params=SEASON_PARAMS.get(season_cache[d_str],SEASON_PARAMS['chaos'])
                trailing_stop=c*(1-params['trailing_stop_pct']/100)

            if d_str not in season_cache:
                season_cache[d_str]=get_season(conn,d_str)
            params=SEASON_PARAMS.get(season_cache[d_str],SEASON_PARAMS['chaos'])
            max_hold_eff=p4_base_max_hold+(p4_extend_days if p4_extended else 0)

            should_sell=False; reason=''; exit_px=c
            if l<=hard_stop: should_sell=True; reason='硬止损'; exit_px=max(hard_stop,c*0.97)
            if c<=trailing_stop and c<highest*0.98 and not should_sell: should_sell=True; reason='移动止盈'
            if hold_days>=max_hold_eff and not should_sell:
                if params['p4_enabled'] and score>=params['p4_min_score'] and not p4_extended:
                    p4_extended=True; p4_extend_days+=params['p4_extension_days']
                    reason=f'P4延期{params["p4_extension_days"]}d'
                else: should_sell=True; reason=f'持有上限{int(max_hold_eff)}d平仓'
            if entry_tier=='T2' and hold_days>2 and score<60 and not should_sell:
                should_sell=True; reason='T2跌破60'

            if not should_sell: continue

            rpct=(exit_px-entry_price)/entry_price*100
            rpnl=pos*(exit_px-entry_price)/entry_price
            trades.append({'ts_code':ts_code,'name':name,'entry_date':entry_date,
                'entry_price':round(entry_price,2),'exit_date':d_str,'exit_price':round(exit_px,2),
                'hold_days':hold_days,'pnl_pct':round(rpct,2),'pnl':round(rpnl,2),
                'entry_season':entry_season,'entry_tier':entry_tier,'entry_score':round(entry_score,1),
                'exit_score':round(score,1),'sell_reason':reason})
            pos=0.0

    conn.close()
    elapsed=time.time()-start_ts
    print(f"\n✅ 完成! {len(trades)} 笔交易, {elapsed:.0f}s")
    return trades

def gen_report(trades):
    if not trades: print("⚠️ 无交易"); return
    df=pd.DataFrame(trades)
    n=len(df); wins=df[df['pnl_pct']>0]; losses=df[df['pnl_pct']<=0]
    nw=len(wins); nl=len(losses)
    wr=nw/n*100; tr=df['pnl'].sum()/(n*100000)*100
    pf=abs(wins['pnl'].sum()/losses['pnl'].sum()) if nl and losses['pnl'].sum()!=0 else float('inf')
    sharpe=df['pnl_pct'].mean()/df['pnl_pct'].std()*math.sqrt(252) if df['pnl_pct'].std()>0 else 0
    print(f"\n{'='*60}\n  📊 V7 P6引擎 — 总览\n{'='*60}")
    print(f"  交易: {n}笔 | 胜率: {wr:.2f}% | 总收益: {tr:+.4f}%")
    print(f"  盈利因子: {pf:.2f} | 夏普: {sharpe:.2f}")
    print(f"  均盈: {wins['pnl_pct'].mean():+.2f}% | 均亏: {losses['pnl_pct'].mean():+.2f}% | 均持: {df['hold_days'].mean():.1f}d")

    print(f"\n{'─'*55}\n  🌍 按季节\n{'─'*55}")
    print(f"  {'季节':8s} {'笔数':>5s} {'胜率':>6s} {'总收益':>9s} {'均盈':>6s} {'均亏':>6s} {'PF':>5s} {'均持':>5s}")
    for s in SEASON_ORDER:
        sd=df[df['entry_season']==s]
        if len(sd)==0: continue
        sw=sd[sd['pnl_pct']>0]; sl=sd[sd['pnl_pct']<=0]
        sr=sd['pnl'].sum()/(len(sd)*100000)*100
        spf=abs(sw['pnl'].sum()/sl['pnl'].sum()) if len(sl) and sl['pnl'].sum()!=0 else float('inf')
        print(f"  {SEASON_LABELS.get(s,s):8s} {len(sd):5d} {len(sw)/len(sd)*100:5.1f}% {sr:+8.4f}% {sw['pnl_pct'].mean():+5.2f}% {sl['pnl_pct'].mean():+5.2f}% {spf:5.2f} {sd['hold_days'].mean():4.1f}d")

    print(f"\n{'─'*55}\n  📈 评分段\n{'─'*55}")
    for t in ['T1','T2']:
        td=df[df['entry_tier']==t]
        if len(td)==0: continue
        tw=td[td['pnl_pct']>0]; tl=td[td['pnl_pct']<=0]
        ttr=td['pnl'].sum()/(len(td)*100000)*100
        tpf=abs(tw['pnl'].sum()/tl['pnl'].sum()) if len(tl) and tl['pnl'].sum()!=0 else float('inf')
        print(f"  {t:5s} {len(td):5d} {len(tw)/len(td)*100:5.1f}% {ttr:+8.4f}% {tw['pnl_pct'].mean():+5.2f}% {tl['pnl_pct'].mean():+5.2f}% {tpf:5.2f}")

    print(f"\n{'─'*55}\n  ⏱ 持有区间\n{'─'*55}")
    for label,lo,hi in [('1-5日',1,5),('6-10日',6,10),('11-20日',11,20),('21-30日',21,30),('31-45日',31,45),('45日+',46,999)]:
        hd=df[(df['hold_days']>=lo)&(df['hold_days']<=hi)]
        if len(hd)==0: continue
        hw=hd[hd['pnl_pct']>0]; hl=hd[hd['pnl_pct']<=0]
        htr=hd['pnl'].sum()/(len(hd)*100000)*100
        print(f"  {label:8s} {len(hd):4d}笔 {len(hw)/len(hd)*100:5.1f}% {htr:+8.4f}% 均盈{hw['pnl_pct'].mean():+5.2f}% 均亏{hl['pnl_pct'].mean():+5.2f}%")

    print(f"\n{'─'*55}\n  🏆 Top10\n{'─'*55}")
    for _,r in df.nlargest(10,'pnl_pct').iterrows():
        print(f"  {r['name']:10s}({r['ts_code'][:8]:>8s}) {r['entry_season']:10s} {r['entry_tier']:4s} {r['hold_days']:2d}d {r['pnl_pct']:+6.2f}%")
    print(f"\n{'─'*55}\n  💀 Bottom10\n{'─'*55}")
    for _,r in df.nsmallest(10,'pnl_pct').iterrows():
        print(f"  {r['name']:10s}({r['ts_code'][:8]:>8s}) {r['entry_season']:10s} {r['entry_tier']:4s} {r['hold_days']:2d}d {r['pnl_pct']:+6.2f}%")

    now=datetime.now().strftime('%Y%m%d_%H%M%S')
    out=f'/tmp/v7_p6engine_result_{now}.json'
    summary={'strategy':'V8_夏季增强版_P6引擎','n_trades':n,'win_rate':round(wr,2),'return_pct':round(tr,4),'profit_factor':round(pf,2),'sharpe':round(sharpe,2)}
    with open(out,'w') as f: json.dump(summary,f,ensure_ascii=False,indent=2)
    print(f"\n📁 {out}")

if __name__=='__main__':
    ap=argparse.ArgumentParser()
    ap.add_argument('--limit',type=int,default=None)
    ap.add_argument('--start',default='2023-01-03')
    ap.add_argument('--end',default='2026-06-12')
    args=ap.parse_args()
    trades=main(args.start,args.end,limit=args.limit)
    if trades: gen_report(trades)
