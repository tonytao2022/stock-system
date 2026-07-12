#!/usr/bin/env python3
"""
真实模拟回测 V13.2 — 按实盘规则
================================
总资金100万 | 最多持有8只 | 每天最多买入5只 | V13.2季节参数矩阵
- 买入：评分≥季节买入线，按评分从高到低，直到仓位/资金用满
- 卖出：硬止损(-T1%/T2%) / 移动止盈(从最高回落) / 持有上限
- T+1日出金

用法: python3 backtest_realistic_v13.py [--start 2024-01-01] [--end 2026-07-09] [--limit N]
"""

import os, sys, json, time, math, argparse
from datetime import datetime, date
import pandas as pd
import numpy as np

sys.path.insert(0, '/opt/stock-analyzer')
import db_config

# ── V13.2 最终参数矩阵 ──────────────────────────────
V13_PARAMS = {
    'summer':         {'buy_min':65,'max_hold':30,'sl_t1':12,'sl_t2':9,'trail':18,'single_pos':50,'total_pos':50},
    'spring':         {'buy_min':65,'max_hold':30,'sl_t1':12,'sl_t2':9,'trail':15,'single_pos':35,'total_pos':40},
    'weak_spring':    {'buy_min':68,'max_hold':25,'sl_t1':11,'sl_t2':8,'trail':15,'single_pos':35,'total_pos':40},
    'chaos_spring':   {'buy_min':72,'max_hold':25,'sl_t1':11,'sl_t2':8,'trail':15,'single_pos':20,'total_pos':35},
    'chaos':          {'buy_min':80,'max_hold':25,'sl_t1':10,'sl_t2':8,'trail':12,'single_pos':20,'total_pos':30},
    'chaos_autumn':   {'buy_min':72,'max_hold':20,'sl_t1':8,'sl_t2':6,'trail':10,'single_pos':15,'total_pos':20},
    'weak_autumn':    {'buy_min':70,'max_hold':20,'sl_t1':8,'sl_t2':6,'trail':12,'single_pos':20,'total_pos':25},
    'autumn':         {'buy_min':68,'max_hold':20,'sl_t1':10,'sl_t2':8,'trail':12,'single_pos':30,'total_pos':35},
    'winter':         {'buy_min':85,'max_hold':10,'sl_t1':5,'sl_t2':4,'trail':8,'single_pos':5,'total_pos':10},
}

SEASON_ORDER = ['summer','spring','weak_spring','chaos_spring','chaos','chaos_autumn','weak_autumn','autumn','winter']
SEASON_LABELS = {
    'summer':'☀️夏季','spring':'🌸春季','weak_spring':'⛅弱春','chaos_spring':'🌤️混沌春',
    'chaos':'🌪️混沌','chaos_autumn':'☁️混沌秋','weak_autumn':'⛅弱秋','autumn':'🍂秋季','winter':'❄️冬季'
}

# ── 实盘约束 ─────────────────────────────────────────
TOTAL_CAPITAL = 1_000_000
MAX_POSITIONS = 8
MAX_DAILY_BUYS = 5
SINGLE_ENTRY = 100_000  # 每笔基础10万

import pymysql

def get_conn():
    pwd = db_config._get_password()
    return pymysql.connect(host='127.0.0.1', port=3306, user='debian-sys-maint',
                           password=pwd, database='stock_db_v2',
                           charset='utf8mb4', cursorclass=pymysql.cursors.DictCursor)

def main(start_date, end_date, limit=None):
    conn = get_conn()
    cur = conn.cursor()

    # ── 1. 加载回测池 ──
    cur.execute("SELECT ts_code, name FROM backtest_pool ORDER BY ts_code")
    pool = cur.fetchall()
    if limit: pool = pool[:limit]
    pool_codes = [s['ts_code'] for s in pool]
    pool_names = {s['ts_code']: s['name'] for s in pool}
    print(f"📦 回测池: {len(pool)} 只 | 📅 {start_date} ~ {end_date}")
    print(f"💰 总资金: {TOTAL_CAPITAL/1e4:.0f}万 | 最多持有{MAX_POSITIONS}只 | 日买入上限{MAX_DAILY_BUYS}只\n")

    # ── 2. 批量加载评分数据 ──
    print("⏳ 加载评分数据...")
    score_start = time.time()
    # 从strategy_signal表取calibrated_score，因为只有它有历史数据
    cur.execute("""
        SELECT ts_code, trade_date, calibrated_score, season
        FROM strategy_signal
        WHERE trade_date>=%s AND trade_date<=%s
          AND calibrated_score IS NOT NULL
        ORDER BY trade_date, ts_code
    """, (start_date, end_date))
    score_rows = cur.fetchall()
    scores = {}  # (ts_code, trade_date_str) -> score
    seasons = {} # trade_date_str -> season (取第一个)
    for r in score_rows:
        td = r['trade_date'].strftime('%Y-%m-%d') if hasattr(r['trade_date'], 'strftime') else str(r['trade_date'])
        key = (r['ts_code'], td)
        scores[key] = float(r['calibrated_score']) if r['calibrated_score'] else 0
        if td not in seasons and r.get('season'):
            seasons[td] = r['season']
    print(f"  ✅ {len(scores)} 条评分, {len(seasons)} 个交易日 ({(time.time()-score_start):.1f}s)")

    # 如果season_state没有覆盖到的日期，用season_state补
    cur.execute("SELECT trade_date, season FROM season_state WHERE trade_date>=%s AND trade_date<=%s",
                (start_date, end_date))
    for r in cur.fetchall():
        td = r['trade_date'].strftime('%Y-%m-%d') if hasattr(r['trade_date'], 'strftime') else str(r['trade_date'])
        if td not in seasons:
            seasons[td] = r['season']

    # ── 3. 批量加载日K线（只需要开盘高收低 + close） ──
    print("⏳ 加载K线数据...")
    kline_start = time.time()
    # 构建IN子句
    code_placeholders = ','.join(['%s'] * len(pool_codes))
    cur.execute(f"""
        SELECT ts_code, trade_date, `open`, high, low, `close`
        FROM daily_kline
        WHERE ts_code IN ({code_placeholders})
          AND trade_date>=%s AND trade_date<=%s
        ORDER BY trade_date, ts_code
    """, (*pool_codes, start_date, end_date))
    kline_rows = cur.fetchall()
    kline_data = {}  # (ts_code, trade_date_str) -> {open, high, low, close}
    all_dates = set()
    for r in kline_rows:
        td = r['trade_date'].strftime('%Y-%m-%d')
        all_dates.add(td)
        kline_data[(r['ts_code'], td)] = {
            'open': float(r['open']),
            'high': float(r['high']),
            'low': float(r['low']),
            'close': float(r['close']),
        }
    all_dates = sorted(all_dates)
    print(f"  ✅ {len(kline_data)} 条K线, {len(all_dates)} 个交易日 ({(time.time()-kline_start):.1f}s)")

    conn.close()

    # ── 4. 逐日模拟 ──
    print(f"\n⏳ 开始逐日模拟 ({len(all_dates)} 个交易日)...")
    sim_start = time.time()

    portfolio = {}      # ts_code -> {name, buy_date, buy_price, amount, shares, highest, hold_days, entry_season}
    trades = []
    daily_cash = TOTAL_CAPITAL
    total_pos_value = 0  # 当前持仓市值（按买入成本算，简化）

    for di, tdate in enumerate(all_dates):
        if di % 100 == 0 and di > 0:
            el = time.time() - sim_start
            print(f"  [{di}/{len(all_dates)}] {tdate} | 持仓{len(portfolio)}只 | 现金¥{daily_cash:.0f} | 交易{len(trades)}笔 ({el:.0f}s)")

        # 取当日季节
        cur_season = seasons.get(tdate, 'chaos')
        params = V13_PARAMS.get(cur_season, V13_PARAMS['chaos'])

        # ── 第一步：处理卖出 ──
        to_sell = []
        for ts_code, pos in list(portfolio.items()):
            k = kline_data.get((ts_code, tdate))
            if k is None:
                # 当天休市或无数据，增加持有天数但不出售
                pos['hold_days'] += 1
                continue

            open_px = k['open']
            high_px = k['high']
            low_px = k['low']
            close_px = k['close']
            buy_price = pos['buy_price']
            hold_days = pos['hold_days'] + 1
            pos['hold_days'] = hold_days
            highest = pos['highest']

            # 更新最高价
            if high_px > highest:
                highest = high_px
                pos['highest'] = highest
            if close_px > highest:
                highest = close_px
                pos['highest'] = highest

            # ── 卖出判定 ──
            sl_t1 = params['sl_t1'] / 100
            stop_price_t1 = buy_price * (1 - sl_t1)

            should_sell = False
            exit_px = close_px

            # ① 硬止损：最低价触及买入价的 (1 - T1%)
            if low_px <= stop_price_t1:
                should_sell = True
                reason = f'止损-{params["sl_t1"]}%'
                exit_px = stop_price_t1  # 模拟限价单成交
            # ② 移动止盈：收盘价从最高点回落超过trail%
            elif close_px <= highest * (1 - params['trail'] / 100):
                should_sell = True
                reason = f'止盈回落{params["trail"]}%'
                exit_px = close_px
            # ③ 持有上限
            elif hold_days >= params['max_hold']:
                should_sell = True
                reason = f'持有上限{params["max_hold"]}d'
                exit_px = close_px

            if not should_sell:
                continue

            # 计算盈亏
            pnl_pct = (exit_px / buy_price - 1) * 100
            pnl_cny = pos['amount'] * (exit_px / buy_price - 1)
            revenue = pos['shares'] * exit_px
            fee = revenue * 0.001 + revenue * 0.0005  # 佣金千一 + 印花税万分之五（现在减半了）
            daily_cash += revenue - fee
            total_pos_value -= pos['amount']

            trades.append({
                'ts_code': ts_code,
                'name': pos['name'],
                'buy_date': pos['buy_date'],
                'sell_date': tdate,
                'hold_days': hold_days,
                'buy_price': round(buy_price, 2),
                'sell_price': round(exit_px, 2),
                'amount': round(pos['amount']),
                'shares': int(pos['shares']),
                'pnl_pct': round(pnl_pct, 2),
                'pnl': round(pnl_cny),
                'reason': reason,
                'entry_season': pos['entry_season'],
                'exit_season': cur_season,
            })
            to_sell.append(ts_code)

        for ts_code in to_sell:
            del portfolio[ts_code]

        # ── 第二步：处理买入 ──
        if len(portfolio) < MAX_POSITIONS:
            # 收集满足评分条件的候选
            candidates = []
            for ts_code in pool_codes:
                if ts_code in portfolio:
                    continue
                score = scores.get((ts_code, tdate), 0)
                if score >= params['buy_min']:
                    k = kline_data.get((ts_code, tdate))
                    if k:
                        candidates.append((ts_code, score, k['close']))

            candidates.sort(key=lambda x: x[1], reverse=True)

            max_can_buy = min(MAX_DAILY_BUYS, MAX_POSITIONS - len(portfolio))
            buys_today = 0

            for ts_code, score, close_px in candidates:
                if buys_today >= max_can_buy:
                    break
                if len(portfolio) >= MAX_POSITIONS:
                    break

                # 计算可买入金额
                # 单票上限
                single_max = TOTAL_CAPITAL * params['single_pos'] / 100
                # 总仓剩余
                total_remaining = TOTAL_CAPITAL * params['total_pos'] / 100 - total_pos_value
                # 实际可买 = min(10万, 单票上限, 总仓剩余)
                buy_amount = min(SINGLE_ENTRY, single_max, max(0, total_remaining))

                # T+1：当天买入以收盘价+0.5%滑点
                buy_price = close_px * 1.005
                shares = int(buy_amount / buy_price / 100) * 100
                if shares < 100:
                    continue  # 不够一手

                actual_cost = shares * buy_price
                if actual_cost > daily_cash:
                    continue  # 现金不够

                # 扣钱（含佣金）
                daily_cash -= actual_cost
                daily_cash -= actual_cost * 0.001  # 佣金
                total_pos_value += actual_cost

                portfolio[ts_code] = {
                    'name': pool_names[ts_code],
                    'buy_date': tdate,
                    'buy_price': buy_price,
                    'shares': shares,
                    'amount': actual_cost,
                    'highest': close_px,
                    'hold_days': 0,
                    'entry_season': cur_season,
                    'entry_score': score,
                }
                buys_today += 1

    el = time.time() - sim_start
    print(f"\n✅ 模拟完成! {len(trades)} 笔交易, {el:.0f}s")

    # ── 5. 生成报告 ──
    gen_report(trades)

def gen_report(trades):
    if not trades:
        print("⚠️ 无交易")
        return

    df = pd.DataFrame(trades)
    n = len(df)
    wins = df[df['pnl_pct'] > 0]
    losses = df[df['pnl_pct'] <= 0]
    nw = len(wins); nl = len(losses)
    wr = nw / n * 100
    total_pnl = df['pnl'].sum()
    return_pct = total_pnl / TOTAL_CAPITAL * 100
    pf = abs(wins['pnl'].sum() / losses['pnl'].sum()) if nl > 0 and losses['pnl'].sum() != 0 else float('inf')
    sharpe = df['pnl_pct'].mean() / df['pnl_pct'].std() * math.sqrt(252) if df['pnl_pct'].std() > 0 else 0

    # 最大回撤（按卖出日期累积）
    df_sorted = df.sort_values('sell_date')
    cumulative = df_sorted['pnl'].cumsum()
    running_max = cumulative.cummax()
    dd_series = (running_max - cumulative) / TOTAL_CAPITAL * 100
    max_drawdown = dd_series.max()
    calmar = (return_pct / max_drawdown) if max_drawdown > 0 else float('inf')

    avg_profit = wins['pnl_pct'].mean() if nw > 0 else 0
    avg_loss = losses['pnl_pct'].mean() if nl > 0 else 0

    print(f"\n{'='*65}")
    print(f"  📊 V13.2 实盘规则回测 — 总览")
    print(f"{'='*65}")
    print(f"  💰 总资金: {TOTAL_CAPITAL/1e4:.0f}万 | 净收益: {total_pnl:+.0f} ({return_pct:+.2f}%)")
    print(f"  📈 交易: {n}笔 | 胜率: {wr:.2f}% | 盈亏比: {pf:.2f}")
    print(f"  📉 最大回撤: {max_drawdown:.2f}% | 夏普: {sharpe:.2f} | 卡玛: {calmar:.2f}")
    print(f"  📊 均盈: {avg_profit:+.2f}% | 均亏: {avg_loss:+.2f}% | 均持: {df['hold_days'].mean():.1f}d")

    # ── 按卖出理由 ──
    print(f"\n{'─'*60}")
    print(f"  🎯 按卖出理由")
    print(f"{'─'*60}")
    print(f"  {'理由':22s} {'笔数':>5s} {'胜率':>6s} {'总收益':>10s}")
    for reason in df['reason'].unique():
        sd = df[df['reason'] == reason]
        sw = sd[sd['pnl_pct'] > 0]
        sr = sd['pnl'].sum()
        print(f"  {reason:22s} {len(sd):5d} {len(sw)/len(sd)*100:5.1f}% {sr:+10.0f}")

    # ── 按入场季节 ──
    print(f"\n{'─'*60}")
    print(f"  🌍 按入场季节")
    print(f"{'─'*60}")
    print(f"  {'季节':12s} {'笔数':>5s} {'胜率':>6s} {'总收益':>10s} {'均盈':>6s}")
    for s in SEASON_ORDER:
        sd = df[df['entry_season'] == s]
        if len(sd) == 0: continue
        sw = sd[sd['pnl_pct'] > 0]
        sl = sd[sd['pnl_pct'] <= 0]
        sr = sd['pnl'].sum()
        label = SEASON_LABELS.get(s, s)
        print(f"  {label:12s} {len(sd):5d} {len(sw)/len(sd)*100:5.1f}% {sr:+10.0f} {sw['pnl_pct'].mean():+6.2f}%")

    # ── 按持有天数 ──
    print(f"\n{'─'*60}")
    print(f"  ⏱ 按持有天数")
    print(f"{'─'*60}")
    print(f"  {'区间':10s} {'笔数':>5s} {'胜率':>6s} {'总收益':>10s}")
    for label, lo, hi in [('1-5日',1,5),('6-10日',6,10),('11-20日',11,20),('21-30日',21,30),('31日+',31,999)]:
        hd = df[(df['hold_days'] >= lo) & (df['hold_days'] <= hi)]
        if len(hd) == 0: continue
        hw = hd[hd['pnl_pct'] > 0]
        hr = hd['pnl'].sum()
        print(f"  {label:10s} {len(hd):5d} {len(hw)/len(hd)*100:5.1f}% {hr:+10.0f}")

    # ── Top盈亏 ──
    print(f"\n{'─'*60}")
    print(f"  🏆 收益 Top 5")
    print(f"{'─'*60}")
    for _, r in df.nlargest(5, 'pnl').iterrows():
        lbl = SEASON_LABELS.get(r['entry_season'], r['entry_season'])
        print(f"  {r['name'][:12]:12s} {r['ts_code'][:8]:8s} {lbl:8s} {r['hold_days']:2d}d {r['pnl_pct']:+6.2f}% {r['pnl']:+8.0f} | {r['reason']}")

    print(f"\n{'─'*60}")
    print(f"  💀 亏损 Top 5")
    print(f"{'─'*60}")
    for _, r in df.nsmallest(5, 'pnl').iterrows():
        lbl = SEASON_LABELS.get(r['entry_season'], r['entry_season'])
        print(f"  {r['name'][:12]:12s} {r['ts_code'][:8]:8s} {lbl:8s} {r['hold_days']:2d}d {r['pnl_pct']:+6.2f}% {r['pnl']:+8.0f} | {r['reason']}")

    # ── 按月 ──
    df['sell_date_dt'] = pd.to_datetime(df['sell_date'])
    df['month'] = df['sell_date_dt'].dt.strftime('%Y-%m')
    print(f"\n{'─'*60}")
    print(f"  📅 按月收益")
    print(f"{'─'*60}")
    print(f"  {'月份':8s} {'笔数':>5s} {'胜率':>6s} {'总收益':>10s} {'收益%':>8s}")
    for month, md in df.groupby('month', sort=True):
        mw = md[md['pnl_pct'] > 0]
        mr = md['pnl'].sum()
        mr_pct = mr / TOTAL_CAPITAL * 100
        print(f"  {month:8s} {len(md):5d} {len(mw)/len(md)*100:5.1f}% {mr:+10.0f} {mr_pct:+7.2f}%")

    # ── 季度收益 ──
    df['quarter'] = df['sell_date_dt'].dt.to_period('Q').astype(str)
    print(f"\n{'─'*60}")
    print(f"  🗓️ 按季度收益")
    print(f"{'─'*60}")
    for q, qd in df.groupby('quarter', sort=True):
        qw = qd[qd['pnl_pct'] > 0]
        qr = qd['pnl'].sum()
        qr_pct = qr / TOTAL_CAPITAL * 100
        print(f"  {q:10s} {len(qd):4d}笔 {len(qw)/len(qd)*100:5.1f}% {qr:+10.0f} ({qr_pct:+6.2f}%)")

    # ── 保存结果 ──
    now = datetime.now().strftime('%Y%m%d_%H%M%S')
    out = f'/tmp/v13_realistic_backtest_{now}.json'
    summary = {
        'strategy': 'V13.2_实盘规则',
        'total_capital': TOTAL_CAPITAL,
        'max_positions': MAX_POSITIONS,
        'max_daily_buys': MAX_DAILY_BUYS,
        'n_trades': n,
        'win_rate': round(wr, 2),
        'total_pnl': round(total_pnl),
        'return_pct': round(return_pct, 2),
        'max_drawdown_pct': round(max_drawdown, 2),
        'profit_factor': round(pf, 2),
        'sharpe': round(sharpe, 2),
        'calmar': round(calmar, 2),
    }
    with open(out, 'w') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"\n📁 {out}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='V13.2 实盘规则回测')
    parser.add_argument('--start', default='2024-01-01', help='开始日期')
    parser.add_argument('--end', default='2026-07-09', help='结束日期')
    parser.add_argument('--limit', type=int, default=None, help='仅测前N只')
    args = parser.parse_args()
    main(args.start, args.end, args.limit)
