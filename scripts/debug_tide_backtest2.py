#!/usr/bin/env python3
"""Debug: TIDE逐笔交易明细 + 净值跟踪"""
import sys, time
sys.path.insert(0, '/opt/stock-analyzer')
from db_config import get_connection
from collections import defaultdict

conn = get_connection()
cur = conn.cursor()

# 取TIDE评分
cur.execute("""
    SELECT ts_code, tide_score as score, trade_date
    FROM tide_score_signal
    WHERE trade_date >= '2026-04-01' AND trade_date <= '2026-07-03'
      AND tide_score >= 82
    ORDER BY trade_date
""")
tide_rows = cur.fetchall()
print(f"TIDE >=82: {len(tide_rows)}条")

# 取K线
dates_avail = sorted(set(str(r['trade_date']) for r in tide_rows))
print(f"日期: {dates_avail[0]} ~ {dates_avail[-1]} ({len(dates_avail)}天)")

kline_map = {}
for code in sorted(set(str(r['ts_code']) for r in tide_rows)):
    cur.execute(f"""
        SELECT trade_date, close FROM daily_kline
        WHERE ts_code = '{code}' AND trade_date >= '2026-04-01' AND trade_date <= '2026-07-03'
    """)
    for row in cur.fetchall():
        kline_map[(code, str(row['trade_date']))] = float(row['close']) if row['close'] else 0
cur.close()
conn.close()

# 参数
INIT = 1_000_000
buy = 82; hold_max = 30; stop_t1 = -7; stop_t2 = -5; trail_pct = 15
max_positions = 8; max_single = 0.20; comm = 0.0008

cash = INIT
positions = {}  # code -> {buy_price, shares, cost, hwm, days_held, score}
trades = []
equity_curve = []

# 按日期组织tide数据
daily_data = defaultdict(list)
for r in tide_rows:
    daily_data[str(r['trade_date'])].append({
        'ts_code': str(r['ts_code']),
        'score': float(r['score']) if r['score'] else 0,
    })

all_dates = sorted(daily_data.keys())

for today in all_dates:
    stocks_today = daily_data[today]
    
    # 持仓检查 - 先收集要卖的，再统一处理
    to_sell = []
    for code, pos in positions.items():
        pos['days_held'] += 1
        cur_price = kline_map.get((code, today), 0)
        if cur_price == 0: continue
        if cur_price > pos.get('hwm', pos['buy_price']):
            pos['hwm'] = cur_price
        if pos['days_held'] <= 1: continue  # T+1
        
        hwm = pos.get('hwm', pos['buy_price'])
        dd = (cur_price - hwm) / hwm * 100
        if dd <= -trail_pct:
            to_sell.append((code, cur_price, f'回撤{dd:.1f}%'))
            continue
        
        pnl = (cur_price - pos['buy_price']) / pos['buy_price'] * 100
        stop = stop_t2 if pos['days_held'] >= 2 else stop_t1
        if pnl <= stop:
            to_sell.append((code, cur_price, f'止损{stop}({pnl:.1f}%)'))
            continue
        if pos['days_held'] >= hold_max:
            to_sell.append((code, cur_price, f'到期{pos["days_held"]}d'))
            continue
    
    for code, price, reason in to_sell:
        pos = positions.pop(code)
        proceeds = pos['shares'] * price * (1 - comm)
        pnl = proceeds - pos['cost']
        pnl_pct = (price - pos['buy_price']) / pos['buy_price'] * 100
        cash += proceeds
        trades.append({
            'code': code, 'buy_date': pos['buy_date'], 'sell_date': today,
            'buy_price': pos['buy_price'], 'sell_price': price,
            'pnl': round(pnl, 2), 'pnl_pct': round(pnl_pct, 2),
            'days_held': pos['days_held'], 'reason': reason,
            'score': pos.get('score', 0),
        })
    
    # 买入
    if len(positions) < max_positions:
        cands = [s for s in stocks_today 
                if s['score'] >= buy and s['ts_code'] not in positions 
                and kline_map.get((s['ts_code'], today), 0) > 0]
        cands.sort(key=lambda x: x['score'], reverse=True)
        
        for s in cands[:max_positions - len(positions)]:
            if cash <= 0: break
            cur_price = kline_map.get((s['ts_code'], today), 0)
            if cur_price == 0: continue
            buy_amt = cash * max_single
            if buy_amt < 10000: continue
            shares = int(buy_amt / cur_price / 100) * 100
            if shares <= 0: continue
            cost = shares * cur_price * (1 + comm)
            if cost > cash: continue
            positions[s['ts_code']] = {
                'buy_date': today, 'buy_price': cur_price, 'shares': shares,
                'cost': cost, 'hwm': cur_price, 'days_held': 0, 'score': s['score'],
            }
            cash -= cost
    
    # 日净值
    pos_val = sum(
        kline_map.get((code, today), pos['buy_price']) * pos['shares']
        for code, pos in positions.items()
    )
    equity_curve.append({
        'date': today, 'cash': round(cash, 2),
        'pos_val': round(pos_val, 2),
        'total': round(cash + pos_val, 2),
        'pos_cnt': len(positions),
    })

# 期末
last_date = all_dates[-1]
for code, pos in list(positions.items()):
    lp = kline_map.get((code, last_date), 0)
    if lp == 0: lp = pos['buy_price']
    pnl = (lp - pos['buy_price']) / pos['buy_price'] * 100
    proceeds = pos['shares'] * lp * (1 - comm)
    total_pnl = proceeds - pos['cost']
    trades.append({
        'code': code, 'buy_date': pos['buy_date'], 'sell_date': last_date,
        'buy_price': pos['buy_price'], 'sell_price': lp,
        'pnl': round(total_pnl, 2), 'pnl_pct': round(pnl, 2),
        'days_held': pos['days_held'], 'reason': '期末强平',
        'score': pos.get('score', 0),
    })

# 最终价值
final_pos = sum(
    kline_map.get((c, last_date), p['buy_price']) * p['shares']
    for c, p in positions.items()
) if positions else 0
total = cash + final_pos
ret = (total - INIT) / INIT * 100

# Print明细
print(f"\n{'='*70}")
print(f"💰 最终净值: {total:.0f} ({ret:+.2f}%)")
print(f"💰 现金: {cash:.0f} | 持仓: {final_pos:.0f} | 总笔数: {len(trades)}")
print(f"{'='*70}")
print(f"{'#':>3} {'代码':>10s} {'买入':>10s} {'卖出':>10s} {'涨跌':>8s} {'天数':>4s} {'原因':<14s} {'买入分':>6s}")
print(f"   {'-'*62}")

wins = losses = 0
win_pnl = loss_pnl = 0.0
for i, t in enumerate(trades, 1):
    wl = '🟢' if t['pnl_pct'] > 0 else '🔴'
    if t['pnl_pct'] > 0: wins += 1; win_pnl += t['pnl_pct']
    else: losses += 1; loss_pnl += t['pnl_pct']
    print(f"  {i:2d}. {t['code']:>10s} {t['buy_date']:>10s} {t['sell_date']:>10s} {t['pnl_pct']:>+7.1f}% {t['days_held']:3d}d {t['reason']:<14s} {t.get('score',0):>5.0f} {wl}")

# 按收益率排序
sorted_trades = sorted(trades, key=lambda t: t['pnl_pct'])
print(f"\n📈 TOP3 赢家:")
for t in sorted_trades[-3:]:
    print(f"  {t['code']:>10s} {t['pnl_pct']:+.1f}% ({t['days_held']}d) {t['reason']}")
print(f"\n📉 TOP3 输家:")
for t in sorted_trades[:3]:
    print(f"  {t['code']:>10s} {t['pnl_pct']:+.1f}% ({t['days_held']}d) {t['reason']}")

wr = wins/len(trades)*100 if trades else 0
avg_w = win_pnl/wins if wins else 0
avg_l = loss_pnl/losses if losses else 0
ratio = avg_w/abs(avg_l) if losses and avg_l else 0

# 最大回撤
peak = INIT
mdd = 0
for e in equity_curve:
    if e['total'] > peak: peak = e['total']
    dd = (peak - e['total']) / peak * 100
    if dd > mdd: mdd = dd

print(f"\n{'='*70}")
print(f"📊 最终统计")
print(f"  总收益: {ret:+.2f}%")
print(f"  最大回撤: {mdd:.2f}%")
print(f"  交易笔数: {len(trades)}")
print(f"  胜率: {wr:.1f}% ({wins}/{len(trades)})")
print(f"  平均盈利: {avg_w:.2f}%")
print(f"  平均亏损: {avg_l:.2f}%")
print(f"  盈亏比: {ratio:.2f}")
# 总盈亏校验
total_pnl_sum = sum(t['pnl_pct'] for t in trades)
print(f"  ∑pnl_pct: {total_pnl_sum:+.1f}%")
total_pnl_abs = sum(t['pnl'] for t in trades)
print(f"  ∑pnl(元): {total_pnl_abs:+.0f}")
print(f"  初始: {INIT} → 最终: {total:.0f} (差价: {total-INIT:+.0f})")
