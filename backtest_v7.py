#!/usr/bin/env python3
"""
V7 全量回测 — 季节敏感参数矩阵
================================
每笔交易记录季节标签，按四季矩阵动态参数执行。

季节参数矩阵（May建议 + Tony确认）：
  ☀️ Summer:  买入线65, 45d, P4评分>55延10d, T1/-10%, T2/-7%
  🍂 Autumn:  买入线75, 20d, T1/-7%, T2/-5%
  🌪️ Chaos:   买入线75(T1 only), 15d, T1/-8%
  ❄️ Winter:  买入线85/空仓

数据源: strategy_signal.composite_score + season_state

用法: python3 backtest_v7.py [--dry-run] [--limit N]
"""

import os, sys, json, time, math, argparse
from datetime import datetime, timedelta, date
from typing import Optional
from decimal import Decimal

import pandas as pd
import numpy as np
import pymysql

sys.path.insert(0, '/opt/stock-analyzer')
import db_config

TUSHARE_TOKEN = os.environ.get('TUSHARE_TOKEN', '')

# ── 季节参数矩阵 ──────────────────────────────────────
SEASON_PARAMS = {
    'summer': {
        'buy_min_score': 65,
        'max_hold': 45,
        'stop_loss_pct_t1': 10,
        'stop_loss_pct_t2': 7,
        'p4_enabled': True,
        'p4_min_score': 55,
        'p4_extension_days': 10,
        'trailing_stop_pct': 15,
        'max_positions': 8,
        't2_enabled': True,
    },
    'autumn': {
        'buy_min_score': 75,
        'max_hold': 20,
        'stop_loss_pct_t1': 7,
        'stop_loss_pct_t2': 5,
        'p4_enabled': False,
        'p4_min_score': 60,
        'p4_extension_days': 5,
        'trailing_stop_pct': 10,
        'max_positions': 6,
        't2_enabled': True,
    },
    'chaos_spring': {
        'buy_min_score': 75,
        'max_hold': 15,
        'stop_loss_pct_t1': 8,
        'stop_loss_pct_t2': 6,
        'p4_enabled': False,
        'p4_min_score': 65,
        'p4_extension_days': 5,
        'trailing_stop_pct': 12,
        'max_positions': 4,
        't2_enabled': False,        # T1 only
    },
    'chaos_autumn': {
        'buy_min_score': 75,
        'max_hold': 15,
        'stop_loss_pct_t1': 8,
        'stop_loss_pct_t2': 6,
        'p4_enabled': False,
        'p4_min_score': 65,
        'p4_extension_days': 5,
        'trailing_stop_pct': 12,
        'max_positions': 4,
        't2_enabled': False,
    },
    'chaos': {
        'buy_min_score': 75,
        'max_hold': 15,
        'stop_loss_pct_t1': 8,
        'stop_loss_pct_t2': 6,
        'p4_enabled': False,
        'p4_min_score': 65,
        'p4_extension_days': 5,
        'trailing_stop_pct': 12,
        'max_positions': 4,
        't2_enabled': False,
    },
    'spring': {
        'buy_min_score': 70,
        'max_hold': 20,
        'stop_loss_pct_t1': 8,
        'stop_loss_pct_t2': 6,
        'p4_enabled': True,
        'p4_min_score': 60,
        'p4_extension_days': 5,
        'trailing_stop_pct': 12,
        'max_positions': 6,
        't2_enabled': True,
    },
    'winter': {
        'buy_min_score': 85,         # 接近空仓
        'max_hold': 10,
        'stop_loss_pct_t1': 5,
        'stop_loss_pct_t2': 4,
        'p4_enabled': False,
        'p4_min_score': 80,
        'p4_extension_days': 3,
        'trailing_stop_pct': 8,
        'max_positions': 2,
        't2_enabled': False,
    },
}

SEASON_ORDER = ['summer', 'autumn', 'spring', 'chaos_spring', 'chaos', 'chaos_autumn', 'winter']
SEASON_LABELS = {
    'summer': '☀️夏季', 'autumn': '🍂秋季', 'spring': '🌸春季',
    'chaos_spring': '🌤️弱春', 'chaos': '🌪️混沌', 'chaos_autumn': '🌥️弱秋', 'winter': '❄️冬季'
}


# ── DB 工具 ──────────────────────────────────────────
def get_season(trade_date, conn):
    """获取指定日期的季节状态"""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT season FROM season_state
            WHERE trade_date = %s
            ORDER BY id DESC LIMIT 1
        """, (trade_date,))
        row = cur.fetchone()
    return row['season'] if row else 'chaos'


def load_daily_scores(conn, ts_code, start_date, end_date):
    """加载指定股票的每日评分"""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT ts_code, trade_date, composite_score
            FROM strategy_signal
            WHERE ts_code = %s
              AND trade_date >= %s AND trade_date <= %s
            ORDER BY trade_date
        """, (ts_code, start_date, end_date))
        rows = cur.fetchall()
    result = {}
    for r in rows:
        d = r['trade_date']
        if isinstance(d, (datetime, date)):
            d = d.strftime('%Y-%m-%d')
        score = float(r['composite_score']) if r['composite_score'] is not None else 0
        result[d] = score
    return result


def load_kline(conn, ts_code, start_date, end_date):
    """加载复权K线"""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT trade_date, `open`, high, low, `close`, pre_close, vol, amount
            FROM daily_kline_qfq
            WHERE ts_code = %s
              AND trade_date >= %s AND trade_date <= %s
            ORDER BY trade_date
        """, (ts_code, start_date, end_date))
        rows = cur.fetchall()
    if not rows:
        return None
    df = pd.DataFrame(rows)
    for col in ['open', 'high', 'low', 'close', 'pre_close', 'vol', 'amount']:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    df['trade_date'] = pd.to_datetime(df['trade_date'])
    df = df.set_index('trade_date').sort_index()
    return df


def load_moneyflow(conn, ts_code, trade_date):
    """加载资金流向数据"""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COALESCE(net_mf_amount,0) as net_mf_amount,
                   COALESCE(buy_lg_amount,0) as buy_lg_amount,
                   COALESCE(sell_lg_amount,0) as sell_lg_amount,
                   COALESCE(buy_elg_amount,0) as buy_elg_amount,
                   COALESCE(sell_elg_amount,0) as sell_elg_amount
            FROM money_flow
            WHERE ts_code = %s AND trade_date = %s
            LIMIT 1
        """, (ts_code, trade_date))
        row = cur.fetchone()
    if not row:
        return 50  # 默认分
    net = float(row['net_mf_amount'])
    buy_lg = float(row['buy_lg_amount'])
    sell_lg = float(row['sell_lg_amount'])
    buy_elg = float(row['buy_elg_amount'])
    sell_elg = float(row['sell_elg_amount'])
    turnover = buy_lg + sell_lg + buy_elg + sell_elg
    if turnover == 0:
        return 50
    net_ratio = net / turnover
    return max(0, min(100, 50 + net_ratio * 100))


# ── V7 回测引擎 ──────────────────────────────────────
class BacktestV7:
    def __init__(self, conn, start_date, end_date, dry_run=False, limit=None):
        self.conn = conn
        self.start = start_date
        self.end = end_date
        self.dry_run = dry_run
        self.limit = limit
        self.trades = []          # 所有交易记录
        self.trade_id = 0
        self._season_cache = {}

        # 获取回测池
        with conn.cursor() as cur:
            cur.execute("SELECT ts_code, name FROM backtest_pool ORDER BY ts_code")
            self.pool = cur.fetchall()
            if limit:
                self.pool = self.pool[:limit]

        print(f"📦 回测池: {len(self.pool)} 只股票")
        print(f"📅 区间: {start_date} ~ {end_date}")
        print(f"🔧 模式: {'dry-run(预览)' if dry_run else '全量回测'}")

    def season(self, trade_date):
        d = str(trade_date)[:10]
        if d not in self._season_cache:
            self._season_cache[d] = get_season(d, self.conn)
        return self._season_cache[d]

    def get_tier(self, score, season):
        """判断评分段: T1(>=75) / T2(>=买入线)"""
        params = SEASON_PARAMS.get(season, SEASON_PARAMS['chaos'])
        if score >= 75:
            return 'T1'
        if params['t2_enabled'] and score >= params['buy_min_score']:
            return 'T2'
        return None

    def run_one_stock(self, ts_code, name, kline, scores):
        """对一只股票全区间模拟交易"""
        trades = []
        pos = 0.0           # 持仓市值
        entry_date = None
        entry_price = 0.0
        entry_score = 0.0
        entry_season = None
        entry_tier = None
        hold_days = 0
        highest_price = 0.0
        trailing_stop_price = 0.0
        hard_stop_price = 0.0
        p4_extended = False
        p4_extend_days = 0
        p4_base_max_hold = 0

        for idx, (td, row) in enumerate(kline.iterrows()):
            d_str = td.strftime('%Y-%m-%d')
            c = float(row['close'])
            h = float(row['high'])
            l = float(row['low'])
            score = scores.get(d_str, 0)

            # ─── 买入判断 ────
            if pos == 0:
                cur_season = self.season(d_str)
                params = SEASON_PARAMS.get(cur_season, SEASON_PARAMS['chaos'])
                buy_min = params['buy_min_score']
                tier = self.get_tier(score, cur_season)

                if tier is not None and score >= buy_min and not math.isnan(score) and score > 0:
                    capital = 100000.0
                    pos = capital
                    entry_date = d_str
                    entry_price = c
                    entry_score = score
                    entry_season = cur_season
                    entry_tier = tier
                    hold_days = 0
                    highest_price = c
                    p4_extended = False
                    p4_extend_days = 0

                    p = SEASON_PARAMS.get(cur_season, SEASON_PARAMS['chaos'])
                    sl = p['stop_loss_pct_t1'] if tier == 'T1' else p['stop_loss_pct_t2']
                    hard_stop_price = c * (1 - sl / 100)
                    trailing_stop_price = c * (1 - p['trailing_stop_pct'] / 100)
                    p4_base_max_hold = p['max_hold']

            # ─── 持仓管理 ────
            if pos > 0:
                hold_days += 1
                pnl_pct = (c - entry_price) / entry_price * 100

                # 更新最高价 / 移动止盈
                if c > highest_price:
                    highest_price = c
                    cur_season_now = self.season(d_str)
                    p_now = SEASON_PARAMS.get(cur_season_now, SEASON_PARAMS['chaos'])
                    trailing_stop_price = c * (1 - p_now['trailing_stop_pct'] / 100)

                cur_season_now = self.season(d_str)
                p_now = SEASON_PARAMS.get(cur_season_now, SEASON_PARAMS['chaos'])
                max_hold_eff = p4_base_max_hold + (p4_extend_days if p4_extended else 0)

                # ──── 卖出判定 ────
                should_sell = False
                reason = ''
                exit_price = c

                # ① 硬止损
                if l <= hard_stop_price:
                    should_sell = True
                    reason = f'硬止损({(c-entry_price)/entry_price*100:.1f}%)'
                    exit_price = max(hard_stop_price, c * 0.97)

                # ② 移动止盈（高位回落）
                if c <= trailing_stop_price and c < highest_price * 0.98:
                    if not should_sell:
                        should_sell = True
                        reason = f'移动止盈(高位回落{(highest_price-c)/highest_price*100:.1f}%)'

                # ③ 持有上限平仓（含P4延期检查）
                effective_max = max_hold_eff
                if hold_days >= effective_max and not should_sell:
                    # P4延期检查
                    if p_now['p4_enabled'] and score >= p_now['p4_min_score'] and not p4_extended:
                        p4_extended = True
                        p4_extend_days += p_now['p4_extension_days']
                        effective_max = p4_base_max_hold + p4_extend_days
                        reason = f'P4延期{p_now["p4_extension_days"]}d(评分{score:.0f}≥{p_now["p4_min_score"]})'
                    else:
                        should_sell = True
                        reason = f'持有上限{int(effective_max)}d平仓'

                # ④ T2评分跌破60（快速淘汰）
                if entry_tier == 'T2' and hold_days > 2 and score < 60:
                    if not should_sell and not (p4_extended and score >= p4_extend_days * 0):
                        should_sell = True
                        reason = f'T2评分跌破60({score:.0f})'

                # ──── 执行卖出 ────
                if should_sell:
                    realized_pnl = pos * (exit_price - entry_price) / entry_price
                    realized_pnl_pct = (exit_price - entry_price) / entry_price * 100

                    self.trade_id += 1
                    trades.append({
                        'trade_id': self.trade_id,
                        'ts_code': ts_code,
                        'name': name,
                        'entry_date': entry_date,
                        'entry_price': round(entry_price, 2),
                        'exit_date': d_str,
                        'exit_price': round(exit_price, 2),
                        'hold_days': hold_days,
                        'pnl_pct': round(realized_pnl_pct, 2),
                        'pnl': round(realized_pnl, 2),
                        'entry_season': entry_season,
                        'entry_tier': entry_tier,
                        'entry_score': round(entry_score, 1),
                        'exit_score': round(score, 1),
                        'exit_season': cur_season_now,
                        'sell_reason': reason,
                        'hard_stop_pct': round((hard_stop_price/entry_price-1)*100, 1),
                        'trailing_stop_pct': round((trailing_stop_price/entry_price-1)*100, 1),
                        'max_hold_eff': int(max_hold_eff),
                        'p4_extended': p4_extended,
                        'high_pnl': round((highest_price-entry_price)/entry_price*100, 2),
                    })
                    self.trades.append(trades[-1])

                    # 重置
                    pos = 0.0
                    entry_date = None
                    hold_days = 0

        return trades

    def run(self):
        start_ts = time.time()
        total = 0

        for i, stock in enumerate(self.pool):
            ts_code = stock['ts_code']
            name = stock.get('name', ts_code)

            if (i+1) % 10 == 0:
                elapsed = time.time() - start_ts
                rate = (i+1) / elapsed if elapsed > 0 else 0
                print(f"  [{i+1}/{len(self.pool)}] {ts_code} ... {elapsed:.0f}s ({rate:.1f}只/s)")

            kline = load_kline(self.conn, ts_code, self.start, self.end)
            if kline is None or len(kline) < 60:
                continue

            scores = load_daily_scores(self.conn, ts_code, self.start, self.end)
            if not scores:
                continue

            trades = self.run_one_stock(ts_code, name, kline, scores)
            total += len(trades)

        elapsed = time.time() - start_ts
        print(f"\n✅ 完成! {total} 笔交易, {elapsed:.0f}s")
        return self.trades


# ── 报表 ──────────────────────────────────────────────
def gen_report(trades):
    if not trades:
        print("⚠️ 无交易")
        return

    df = pd.DataFrame(trades)
    n = len(df)
    wins = df[df['pnl_pct'] > 0]
    losses = df[df['pnl_pct'] <= 0]
    nw = len(wins)
    nl = len(losses)
    wr = nw / n * 100
    tr = df['pnl'].sum() / (n * 100000) * 100
    pf = abs(wins['pnl'].sum() / losses['pnl'].sum()) if nl and losses['pnl'].sum() != 0 else float('inf')
    sharpe = df['pnl_pct'].mean() / df['pnl_pct'].std() * math.sqrt(252) if df['pnl_pct'].std() > 0 else 0

    print(f"\n{'='*60}")
    print(f"  📊 V7 季节矩阵回测 — 总览")
    print(f"{'='*60}")
    print(f"  交易: {n}笔 | 胜率: {wr:.2f}% | 总收益: {tr:+.4f}%")
    print(f"  盈利因子: {pf:.2f} | 夏普: {sharpe:.2f}")
    print(f"  均盈: {wins['pnl_pct'].mean():+.2f}% | 均亏: {losses['pnl_pct'].mean():+.2f}%")
    print(f"  均持: {df['hold_days'].mean():.1f}d | 总盈亏: ¥{df['pnl'].sum():+.2f}")

    # ── 按季节 ──
    print(f"\n{'─'*55}")
    print(f"  🌍 按季节")
    print(f"{'─'*55}")
    print(f"  {'季节':8s} {'笔数':>5s} {'胜率':>6s} {'总收益':>9s} {'均盈':>6s} {'均亏':>6s} {'PF':>5s} {'均持':>5s}")
    print(f"  {'─'*50}")
    for s in SEASON_ORDER:
        sd = df[df['entry_season'] == s]
        if len(sd) == 0: continue
        sw = sd[sd['pnl_pct'] > 0]
        sl = sd[sd['pnl_pct'] <= 0]
        sr = sd['pnl'].sum() / (len(sd)*100000) * 100
        spf = abs(sw['pnl'].sum()/sl['pnl'].sum()) if len(sl) and sl['pnl'].sum() != 0 else float('inf')
        print(f"  {SEASON_LABELS.get(s,s):8s} {len(sd):5d} {len(sw)/len(sd)*100:5.1f}% {sr:+8.4f}% "
              f"{sw['pnl_pct'].mean():+5.2f}% {sl['pnl_pct'].mean():+5.2f}% {spf:5.2f} {sd['hold_days'].mean():4.1f}d")

    # ── 按评分段 ──
    print(f"\n{'─'*55}")
    print(f"  📈 按评分段")
    print(f"{'─'*55}")
    print(f"  {'段位':5s} {'笔数':>5s} {'胜率':>6s} {'总收益':>9s} {'均盈':>6s} {'均亏':>6s} {'PF':>5s}")
    print(f"  {'─'*45}")
    for t in ['T1', 'T2']:
        td = df[df['entry_tier'] == t]
        if len(td) == 0: continue
        tw = td[td['pnl_pct'] > 0]
        tl = td[td['pnl_pct'] <= 0]
        tpf = abs(tw['pnl'].sum()/tl['pnl'].sum()) if len(tl) and tl['pnl'].sum() != 0 else float('inf')
        ttr = td['pnl'].sum() / (len(td)*100000) * 100
        print(f"  {t:5s} {len(td):5d} {len(tw)/len(td)*100:5.1f}% {ttr:+8.4f}% "
              f"{tw['pnl_pct'].mean():+5.2f}% {tl['pnl_pct'].mean():+5.2f}% {tpf:5.2f}")

    # ── 按持有区间 ──
    print(f"\n{'─'*55}")
    print(f"  ⏱ 按持有区间")
    print(f"{'─'*55}")
    for label, lo, hi in [('1-5日',1,5),('6-10日',6,10),('11-20日',11,20),
                          ('21-30日',21,30),('31-45日',31,45),('45日+',46,999)]:
        hd = df[(df['hold_days']>=lo)&(df['hold_days']<=hi)]
        if len(hd)==0: continue
        hw = hd[hd['pnl_pct']>0]
        hl = hd[hd['pnl_pct']<=0]
        htr = hd['pnl'].sum()/(len(hd)*100000)*100
        print(f"  {label:8s} {len(hd):4d}笔 {len(hw)/len(hd)*100:5.1f}% {htr:+8.4f}% "
              f"均盈{hw['pnl_pct'].mean():+5.2f}% 均亏{hl['pnl_pct'].mean():+5.2f}%")

    # ── Top/Bottom ──
    print(f"\n{'─'*55}")
    print(f"  🏆 收益 Top 10")
    print(f"{'─'*55}")
    for _, r in df.nlargest(10,'pnl_pct').iterrows():
        print(f"  {r['name']:10s}({r['ts_code'][:8]:>8s}) | {r['entry_season']:10s} | {r['entry_tier']:4s} | {r['hold_days']:2d}d | {r['pnl_pct']:+6.2f}%")

    print(f"\n{'─'*55}")
    print(f"  💀 亏损 Top 10")
    print(f"{'─'*55}")
    for _, r in df.nsmallest(10,'pnl_pct').iterrows():
        print(f"  {r['name']:10s}({r['ts_code'][:8]:>8s}) | {r['entry_season']:10s} | {r['entry_tier']:4s} | {r['hold_days']:2d}d | {r['pnl_pct']:+6.2f}%")

    return df


def save_to_db(df):
    conn = db_config.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM backtest_score_daily WHERE strategy='v7_season_matrix'")
            for _, r in df.iterrows():
                cur.execute("""
                    INSERT INTO backtest_score_daily
                    (ts_code, name, trade_date, score, strategy, params)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (
                    r['ts_code'],
                    r['name'],
                    r['exit_date'],
                    r['pnl_pct'],
                    'v7_season_matrix',
                    json.dumps({
                        'entry_date': str(r['entry_date']),
                        'hold_days': int(r['hold_days']),
                        'entry_season': r['entry_season'],
                        'entry_tier': r['entry_tier'],
                        'entry_score': float(r['entry_score']),
                        'sell_reason': r['sell_reason'],
                    })
                ))
            conn.commit()
        print(f"\n✅ 已写入 backtest_score_daily ({len(df)} 条)")
    finally:
        conn.close()


# ── 入口 ──────────────────────────────────────────────
if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--limit', type=int, default=None)
    ap.add_argument('--start', default='2023-01-03')
    ap.add_argument('--end', default='2026-06-09')
    args = ap.parse_args()

    conn = db_config.get_connection()
    try:
        engine = BacktestV7(conn, args.start, args.end,
                           dry_run=args.dry_run, limit=args.limit)
        trades = engine.run()

        if not trades:
            print("⚠️ 无交易记录")
            sys.exit(0)

        df = gen_report(trades)

        if not args.dry_run:
            save_to_db(df)
            print("\n✅ V7 回测结果已持久化")
    except Exception as e:
        print(f"❌ 错误: {e}")
        import traceback
        traceback.print_exc()
    finally:
        conn.close()
