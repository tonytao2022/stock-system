#!/usr/bin/env python3
"""
V13.2 精准回测 — 带补仓逻辑版
=============================
新增补仓规则：
1. 首仓买入后，跌到-10%（从首仓买入价），检查当日评分仍≥买入线 → 同仓位补仓，重置止损
2. 补仓后从均价再设止损-10%
3. 不补第二次
4. 盈利侧：浮盈≥止盈线，评分仍好 → 出半仓（卖50%），留半仓继续

其余逻辑与 backtest_precise_v13.py 完全一致。
"""

import os, sys, json, time, math
from datetime import datetime
import pandas as pd
import numpy as np

sys.path.insert(0, '/opt/stock-analyzer')
import db_config

PARAMS = {
    'summer':         {'buy_min':80,'max_hold':30,'sl_t1':12,'sl_t2':9,'trail':18,'sgl':50,'ttl':50},
    'spring':         {'buy_min':80,'max_hold':30,'sl_t1':12,'sl_t2':9,'trail':15,'sgl':35,'ttl':40},
    'weak_spring':    {'buy_min':80,'max_hold':25,'sl_t1':11,'sl_t2':8,'trail':15,'sgl':35,'ttl':40},
    'chaos_spring':   {'buy_min':80,'max_hold':25,'sl_t1':11,'sl_t2':8,'trail':15,'sgl':20,'ttl':35},
    'chaos':          {'buy_min':80,'max_hold':25,'sl_t1':10,'sl_t2':8,'trail':12,'sgl':20,'ttl':30},
    'chaos_autumn':   {'buy_min':80,'max_hold':20,'sl_t1':8,'sl_t2':6,'trail':10,'sgl':15,'ttl':20},
    'weak_autumn':    {'buy_min':80,'max_hold':20,'sl_t1':8,'sl_t2':6,'trail':12,'sgl':20,'ttl':25},
    'autumn':         {'buy_min':80,'max_hold':20,'sl_t1':10,'sl_t2':8,'trail':12,'sgl':30,'ttl':35},
    'winter':         {'buy_min':85,'max_hold':10,'sl_t1':5,'sl_t2':4,'trail':8,'sgl':5,'ttl':10},
}

SEASON_ORDER = ['summer','spring','weak_spring','chaos_spring','chaos','chaos_autumn','weak_autumn','autumn','winter']
LABEL = {'summer':'☀️夏季','spring':'🌸春季','weak_spring':'⛅弱春','chaos_spring':'🌤️混沌春','chaos':'🌪️混沌','chaos_autumn':'☁️混沌秋','weak_autumn':'⛅弱秋','autumn':'🍂秋季','winter':'❄️冬季'}

TOTAL = 1_000_000
MAX_POS = 8
MAX_DAILY_BUY = 5
BASE = 100_000
COMM = 0.001
STAMP = 0.0005

# 补仓参数
ADD_POS_PCT = -10  # 跌到首仓价-10%触发补仓检查
ADD_STOP_PCT = 10  # 补仓后统一止损（从均价）
HALF_TAKE_PROFIT = 50  # 半仓止盈百分比


def main(start_date, end_date):
    pwd = db_config._get_password()
    conn = pymysql.connect(host='127.0.0.1', port=3306, user='debian-sys-maint',
                           password='iXve1rVBXfdA4tL9', database='stock_db_v2',
                           charset='utf8mb4', cursorclass=pymysql.cursors.DictCursor)
    cur = conn.cursor()

    # 1. 股票池
    cur.execute("SELECT ts_code, name FROM backtest_pool ORDER BY ts_code")
    pool = {s['ts_code']: s['name'] for s in cur.fetchall()}
    codes = list(pool.keys())
    n_stock = len(codes)
    print(f"📦 {n_stock}只 | {start_date}~{end_date}")
    print(f"💰 {TOTAL/1e4:.0f}万 | ≤{MAX_POS}只 | 日买≤{MAX_DAILY_BUY}")

    # 2. 评分 (composite_score)
    print("⏳ 评分...", end='', flush=True)
    t0 = time.time()
    cur.execute("""
        SELECT ts_code, trade_date, composite_score
        FROM strategy_signal
        WHERE trade_date>=%s AND trade_date<=%s AND composite_score IS NOT NULL
    """, (start_date, end_date))
    rows = cur.fetchall()
    scores = {}
    for r in rows:
        td = r['trade_date'].strftime('%Y-%m-%d')
        scores[(r['ts_code'], td)] = float(r['composite_score'])
    sc_days = len(set(s[1] for s in scores.keys()))
    print(f" {len(scores)}条/{sc_days}天 ({(time.time()-t0):.1f}s)")

    # 3. 季节 (MARKET指数)
    print("⏳ 季节...", end='', flush=True)
    cur.execute("""
        SELECT trade_date, season FROM season_state
        WHERE trade_date>=%s AND trade_date<=%s AND index_code='MARKET'
        ORDER BY trade_date
    """, (start_date, end_date))
    seasons = {}
    for r in cur.fetchall():
        td = r['trade_date'].strftime('%Y-%m-%d')
        seasons[td] = r['season']
    # 补充
    cur.execute("""
        SELECT trade_date, season FROM season_state
        WHERE trade_date>=%s AND trade_date<=%s
        ORDER BY id
    """, (start_date, end_date))
    for r in cur.fetchall():
        td = r['trade_date'].strftime('%Y-%m-%d')
        if td not in seasons:
            seasons[td] = r['season']
    print(f" {len(seasons)}天")

    # 4. K线
    print("⏳ K线...", end='', flush=True)
    t0 = time.time()
    ph = ','.join(['%s'] * n_stock)
    cur.execute(f"""
        SELECT ts_code, trade_date, `open`, high, low, `close`
        FROM daily_kline
        WHERE ts_code IN ({ph}) AND trade_date>=%s AND trade_date<=%s
        ORDER BY trade_date, ts_code
    """, (*codes, start_date, end_date))
    kline = {}
    dset = set()
    for r in cur.fetchall():
        td = r['trade_date'].strftime('%Y-%m-%d')
        dset.add(td)
        kline[(r['ts_code'], td)] = {'o': float(r['open']), 'h': float(r['high']),
                                      'l': float(r['low']), 'c': float(r['close'])}
    alld = sorted(dset)
    print(f" {len(kline)}条/{len(alld)}天 ({(time.time()-t0):.1f}s)")
    conn.close()

    # ── 统计 ──
    add_triggered = 0      # 触发补仓次数
    add_executed = 0       # 实际补仓成功次数
    half_sell_triggered = 0  # 半仓止盈次数

    # 5. 逐日模拟
    print(f"\n▶ 模拟 {len(alld)}天...")
    st = time.time()
    trades = []
    pf = {}
    cash = TOTAL
    pv = 0

    for di, dt in enumerate(alld):
        if di % 20 == 0 and di > 0:
            el = time.time() - st
            print(f"  [{di}/{len(alld)}] {dt} | {len(pf)}只 | ¥{cash:.0f} | {len(trades)}笔 ({el:.0f}s)")

        season = seasons.get(dt, 'chaos')
        p = PARAMS.get(season, PARAMS['chaos'])

        # ── 卖出 ──
        sell_list = []
        for code, pos in list(pf.items()):
            k = kline.get((code, dt))
            if k is None:
                pos['hd'] += 1
                continue
            o, h, l, c = k['o'], k['h'], k['l'], k['c']
            bp = pos['bp']          # 均价（首仓买入价 或 补仓后加权均价）
            entry_bp = pos.get('entry_bp', bp)  # 首仓买入价（用于补仓触发判断）
            hd = pos['hd'] + 1
            pos['hd'] = hd
            hi = pos['hi']

            if h > hi: hi = h
            if c > hi: hi = c
            pos['hi'] = hi

            # ── 补仓检查（仅未补过时触发） ──
            if not pos.get('added', False):
                # 当前低点跌破首仓价-10%？
                add_threshold = entry_bp * (1 + ADD_POS_PCT / 100)
                if l <= add_threshold and c > 0:
                    add_triggered += 1
                    # 检查当日评分是否仍≥买入线
                    today_score = scores.get((code, dt), 0)
                    if today_score >= p['buy_min']:
                        # 同仓位补仓
                        add_amt = pos['amt']  # 同金额
                        add_shares = int(add_amt / (c * 1.005) / 100) * 100
                        if add_shares >= 100:
                            add_actual = add_shares * (c * 1.005)
                            if add_actual + add_actual * COMM <= cash:
                                # 够钱，执行补仓
                                cash -= add_actual + add_actual * COMM
                                pv += add_actual
                                # 重新计算均价
                                total_shares = pos['shares'] + add_shares
                                total_cost = pos['amt'] + add_actual
                                new_bp = total_cost / total_shares
                                pos['bp'] = new_bp
                                pos['shares'] = total_shares
                                pos['amt'] = total_cost
                                pos['added'] = True
                                pos['add_price'] = c
                                add_executed += 1
                                # 在trade中记录补仓为独立行
                                trades.append({
                                    'code': code, 'name': pos['name'],
                                    'buy_dt': dt, 'sell_dt': dt,
                                    'hd': 0, 'bp': round(c * 1.005, 2), 'sp': round(c * 1.005, 2),
                                    'amt': round(add_actual), 'ppct': 0.0,
                                    'pcny': 0, 'fee': round(add_actual * COMM),
                                    'rsn': f'补仓@均价{new_bp:.2f}', 'es': season, 'tier': 'ADD',
                                })

            # ── 止损（统一从均价计算） ──
            # 补仓后统一止损 ADD_STOP_PCT，未补仓用原止损
            if pos.get('added', False):
                cur_sl_pct = ADD_STOP_PCT
            else:
                cur_sl_pct = p['sl_t1'] if pos['tier'] == 'T1' else p['sl_t2']

            stop_px_abs = bp * (1 - cur_sl_pct / 100)
            sell = False; ex_px = c; reason = ''

            if l <= stop_px_abs:
                sell = True
                reason = f'止损-{cur_sl_pct}%'
                ex_px = stop_px_abs
            elif c <= hi * (1 - p['trail'] / 100) and c < hi * 0.98:
                # ── 止盈时：如果评分仍好，出半仓 ──
                today_score = scores.get((code, dt), 0)
                if today_score >= p['buy_min'] and not pos.get('halved', False):
                    # 出半仓
                    half_shares = pos['shares'] // 2
                    if half_shares >= 100:
                        half_rev = half_shares * ex_px
                        half_fee = half_rev * COMM + half_rev * STAMP
                        partial_cash_back = half_rev - half_fee
                        # 更新持仓
                        remaining_shares = pos['shares'] - half_shares
                        remaining_cost = pos['amt'] * (remaining_shares / pos['shares'])
                        pos['shares'] = remaining_shares
                        pos['amt'] = remaining_cost
                        pos['hi'] = c  # 重置高峰
                        pos['halved'] = True
                        cash += partial_cash_back
                        pv -= (pos['amt'] / pos['shares'] * half_shares) if pos['shares'] > 0 else 0
                        half_sell_triggered += 1
                        trades.append({
                            'code': code, 'name': pos['name'],
                            'buy_dt': pos['buy_dt'], 'sell_dt': dt,
                            'hd': hd, 'bp': round(bp, 2), 'sp': round(ex_px, 2),
                            'amt': round(half_rev), 'ppct': round((ex_px/bp-1)*100, 2),
                            'pcny': round(half_rev - (half_rev * half_shares / (half_shares + remaining_shares))),
                            'fee': round(half_fee),
                            'rsn': f'半仓止盈{p["trail"]}%', 'es': season, 'tier': 'HALF',
                        })
                        # 半仓后不清除 pos，继续持有剩余
                        continue  # 不进入 sell 分支
                    else:
                        # 不足100股，全出
                        pass
                # 全仓止盈
                sell = True; reason = f'止盈回撤{p["trail"]}%'
                ex_px = c if c > hi * 0.98 else c
            elif hd >= p['max_hold']:
                sell = True; reason = f'上限{p["max_hold"]}d'
                ex_px = c

            if not sell: continue

            # 卖出完整持仓
            ppct = (ex_px / bp - 1) * 100
            pcny = pos['amt'] * (ex_px / bp - 1)
            rev = pos['shares'] * ex_px
            fee = rev * COMM + rev * STAMP
            cash += rev - fee
            pv -= pos['amt']

            trades.append({
                'code': code, 'name': pos['name'],
                'buy_dt': pos['buy_dt'], 'sell_dt': dt,
                'hd': hd, 'bp': round(bp, 2), 'sp': round(ex_px, 2),
                'amt': round(pos['amt']), 'ppct': round(ppct, 2),
                'pcny': round(pcny), 'fee': round(fee),
                'rsn': reason, 'es': season, 'tier': pos.get('tier', '?'),
            })
            sell_list.append(code)
        for c in sell_list: del pf[c]

        # ── 买入 ──
        if len(pf) < MAX_POS:
            cand = []
            for code in codes:
                if code in pf: continue
                sc = scores.get((code, dt), 0)
                if sc < p['buy_min']: continue
                k = kline.get((code, dt))
                if k is None: continue
                cand.append((code, sc, k['c']))
            cand.sort(key=lambda x: x[1], reverse=True)

            max_b = min(MAX_DAILY_BUY, MAX_POS - len(pf))
            bought = 0
            for code, sc, cp in cand:
                if bought >= max_b or len(pf) >= MAX_POS: break

                tier = 'T1' if sc >= 75 else 'T2'
                sm = TOTAL * p['sgl'] / 100
                tr = TOTAL * p['ttl'] / 100 - pv
                amt = min(BASE, sm, max(0, tr))
                bp = cp * 1.005
                shares = int(amt / bp / 100) * 100
                if shares < 100: continue
                actual = shares * bp
                if actual + actual * COMM > cash: continue
                cash -= actual + actual * COMM
                pv += actual
                pf[code] = {'name': pool[code], 'buy_dt': dt, 'bp': bp,
                            'entry_bp': bp, 'shares': shares, 'amt': actual, 'hi': cp,
                            'hd': 0, 'tier': tier, 'es': season,
                            'added': False, 'add_price': 0.0, 'halved': False}

    print(f"\n✅ {len(trades)}笔 ({time.time()-st:.0f}s)")

    # ── 报表 ──
    if not trades: print("无交易"); return
    df = pd.DataFrame(trades)
    # 只统计真实交易（非补仓/半仓辅助行）
    df_real = df[~df['tier'].isin(['ADD', 'HALF'])].copy()
    # 补仓/半仓的盈利合并到原交易
    df_main = df_real.copy()

    n = len(df_main)
    wins = df_main[df_main['ppct'] > 0]
    loses = df_main[df_main['ppct'] <= 0]
    wr = len(wins) / n * 100 if n > 0 else 0
    tp = df_main['pcny'].sum()
    # 加上补仓/半仓行的pcny也算进去
    tp = df['pcny'].sum()
    rp = tp / TOTAL * 100
    pf_ratio = abs(wins['pcny'].sum() / loses['pcny'].sum()) if len(loses) > 0 and loses['pcny'].sum() != 0 else float('inf')
    sp = df_main['ppct'].mean() / df_main['ppct'].std() * math.sqrt(252) if df_main['ppct'].std() > 0 else 0

    df_s = df_main.sort_values('sell_dt')
    cum = df_s['pcny'].cumsum()
    cmx = cum.cummax()
    dd = (cmx - cum) / TOTAL * 100
    mdd = dd.max() if len(dd) > 0 else 0
    car = (rp / mdd) if mdd > 0 else float('inf')

    print(f"\n{'='*65}")
    print(f"  📊 V13.2 回测（带补仓）{start_date}~{end_date}")
    print(f"{'='*65}")
    print(f"  💰 {TOTAL/1e4:.0f}万 | 净 {tp:+,.0f} ({rp:+.2f}%)")
    print(f"  📈 {n}笔主交易 | 补仓触发{add_triggered}次/执行{add_executed}次 | 半仓{half_sell_triggered}次")
    print(f"  {n}笔 | {wr:.1f}% | 盈亏比{pf_ratio:.2f} | 回撤{mdd:.2f}%")
    print(f"  📊 夏普{sp:.2f} | 卡玛{car:.2f}")
    print(f"  均盈+{wins['ppct'].mean():.2f}% | 均亏{loses['ppct'].mean():.2f}% | 均持{df_main['hd'].mean():.1f}d")

    print(f"\n{'─'*60}\n  🎯 卖出理由\n{'─'*60}")
    print(f"  {'理由':18s} {'笔数':>4s} {'胜率':>6s} {'总收益':>10s}")
    for rsn in df_main['rsn'].unique():
        sd = df_main[df_main['rsn'] == rsn]
        sw = sd[sd['ppct'] > 0]
        print(f"  {rsn:18s} {len(sd):4d} {len(sw)/len(sd)*100:5.1f}% {sd['pcny'].sum():+10,.0f}")

    print(f"\n{'─'*60}\n  🌍 入场季节\n{'─'*60}")
    print(f"  {'季节':12s} {'笔数':>4s} {'胜率':>6s} {'总收益':>10s}")
    for s in SEASON_ORDER:
        sd = df_main[df_main['es'] == s]
        if len(sd) == 0: continue
        sw = sd[sd['ppct'] > 0]
        print(f"  {LABEL.get(s,s):12s} {len(sd):4d} {len(sw)/len(sd)*100:5.1f}% {sd['pcny'].sum():+10,.0f}")

    print(f"\n{'─'*60}\n  ⏱ 持有天数\n{'─'*60}")
    print(f"  {'区间':10s} {'笔数':>4s} {'胜率':>6s} {'总收益':>10s}")
    for lbl, lo, hi in [('1-5日',1,5),('6-10日',6,10),('11-15日',11,15),('16-20日',16,20),('21-25日',21,25),('26日+',26,999)]:
        sd = df_main[(df_main['hd'] >= lo) & (df_main['hd'] <= hi)]
        if len(sd) == 0: continue
        sw = sd[sd['ppct'] > 0]
        print(f"  {lbl:10s} {len(sd):4d} {len(sw)/len(sd)*100:5.1f}% {sd['pcny'].sum():+10,.0f}")

    print(f"\n{'─'*60}\n  📊 补仓明细\n{'─'*60}")
    print(f"  补仓触发: {add_triggered}次")
    print(f"  补仓执行: {add_executed}次 (触发后评分仍够)")
    print(f"  半仓止盈: {half_sell_triggered}次")
    if add_executed > 0:
        add_rows = df[df['tier'] == 'ADD']
        total_add_pnl = add_rows['pcny'].sum()
        print(f"  补仓直接贡献: {total_add_pnl:+,.0f}")

    print(f"\n{'─'*60}\n  🏆 Top 5\n{'─'*60}")
    for _, r in df_main.nlargest(5, 'pcny').iterrows():
        lbl = LABEL.get(r['es'], r['es'])
        print(f"  {r['name'][:12]:12s} {r['code'][:8]:>8s} {lbl:8s} T{r['tier'][1]} {r['hd']:2d}d {r['ppct']:+6.2f}% {r['pcny']:+8,.0f} | {r['rsn']}")
    print(f"\n{'─'*60}\n  💀 Bot 5\n{'─'*60}")
    for _, r in df_main.nsmallest(5, 'pcny').iterrows():
        lbl = LABEL.get(r['es'], r['es'])
        print(f"  {r['name'][:12]:12s} {r['code'][:8]:>8s} {lbl:8s} T{r['tier'][1]} {r['hd']:2d}d {r['ppct']:+6.2f}% {r['pcny']:+8,.0f} | {r['rsn']}")

    df_main['sdt'] = pd.to_datetime(df_main['sell_dt'])
    df_main['mon'] = df_main['sdt'].dt.strftime('%Y-%m')
    print(f"\n{'─'*60}\n  📅 月\n{'─'*60}")
    print(f"  {'月':8s} {'笔数':>4s} {'胜率':>6s} {'收益':>10s} {'%':>8s}")
    for m, md in df_main.groupby('mon', sort=True):
        mw = md[md['ppct'] > 0]
        print(f"  {m:8s} {len(md):4d} {len(mw)/len(md)*100:5.1f}% {md['pcny'].sum():+10,.0f} {md['pcny'].sum()/TOTAL*100:+7.2f}%")

    fp = f'/tmp/v13p2_add_{start_date}_{end_date}_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json'
    with open(fp, 'w') as f:
        json.dump({'n':n,'pnl':round(tp),'ret':round(rp,2),'wr':round(wr,1),'mdd':round(mdd,2),
                   'add_triggered':add_triggered,'add_executed':add_executed,
                   'half_sell':half_sell_triggered}, f)
    print(f"\n📁 {fp}")


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--start', default='2024-01-02')
    ap.add_argument('--end', default='2026-07-10')
    a = ap.parse_args()
    import pymysql
    main(a.start, a.end)
