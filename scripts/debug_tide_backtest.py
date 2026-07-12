#!/usr/bin/env python3
"""Debug: 打印TIDE逐笔交易明细"""
import sys, time
sys.path.insert(0, '/opt/stock-analyzer/scripts')
import sys
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
print(f"📋 TIDE >=82: {len(tide_rows)}条")

# 取K线
dates_avail = sorted(set(str(r['trade_date']) for r in tide_rows))
print(f"📅 日期: {dates_avail[0]} ~ {dates_avail[-1]} ({len(dates_avail)}天)")

kline_map = {}
for code in sorted(set(str(r['ts_code']) for r in tide_rows)):
    cur.execute(f"""
        SELECT trade_date, close
        FROM daily_kline
        WHERE ts_code = '{code}' AND trade_date >= '2026-04-01' AND trade_date <= '2026-07-03'
    """)
    for row in cur.fetchall():
        kline_map[(code, str(row['trade_date']))] = float(row['close']) if row['close'] else 0

cur.close()
conn.close()

# 回测（TIDE参数）
buy = 82; hold = 30; stop_t1 = -7; stop_t2 = -5; trail = 15; total_pct = 80
cash = 1_000_000; max_pos = 8; max_single = 0.20; comm = 0.0008

daily_data = defaultdict(list)
for r in tide_rows:
    daily_data[str(r['trade_date'])].append({
        'ts_code': str(r['ts_code']),
        'score': float(r['score']) if r['score'] else 0,
        'close': kline_map.get((str(r['ts_code']), str(r['trade_date'])), 0),
    })

all_dates = sorted(daily_data.keys())
positions = {}
trades = []

for today in all_dates:
    # 持仓检查
    for code, pos in list(positions.items()):
        pos['days_held'] += 1
        cur_close = kline_map.get((code, today), 0)
        if cur_close == 0: continue
        if cur_close > pos.get('hwm', pos['buy_price']):
            pos['hwm'] = cur_close
        if pos['days_held'] <= 1: continue
        
        hwm = pos.get('hwm', pos['buy_price'])
        dd = (cur_close - hwm) / hwm * 100
        if dd <= -abs(trail):
            pos.pop(code, None); cash += pos['shares'] * cur_close * (1-comm)
            trades.append({'code':code,'pnl':(cur_close-pos['buy_price'])/pos['buy_price']*100,'d':pos['days_held'],'r':'回撤'})
            continue
        
        pnl = (cur_close - pos['buy_price']) / pos['buy_price'] * 100
        stop = stop_t2 if pos['days_held'] >= 2 else stop_t1
        if pnl <= stop:
            trades.append({'code':code,'pnl':pnl,'d':pos['days_held'],'r':f'止损{stop}'})
            cash += pos['shares'] * cur_close * (1-comm); continue
        if pos['days_held'] >= hold:
            trades.append({'code':code,'pnl':pnl,'d':pos['days_held'],'r':'到期'})
            cash += pos['shares'] * cur_close * (1-comm); continue
    
    # 买入
    if len(positions) < max_pos:
        cands = [s for s in daily_data[today] if s['score'] >= buy and s['close'] > 0 and s['ts_code'] not in positions]
        cands.sort(key=lambda x: x['score'], reverse=True)
        for s in cands[:max_pos - len(positions)]:
            if cash <= 0: break
            buy_amt = cash * max_single
            if buy_amt < 10000: continue
            shares = int(buy_amt / s['close'] / 100) * 100
            if shares <= 0: continue
            cost = shares * s['close'] * (1 + comm)
            if cost > cash: continue
            positions[s['ts_code']] = {'buy_price':s['close'],'shares':shares,'cost':cost,'hwm':s['close'],'days_held':0,'score':s['score']}
            cash -= cost

# 期末
last_date = all_dates[-1]
for code, pos in list(positions.items()):
    lp = kline_map.get((code, last_date), pos['buy_price'])
    pnl = (lp - pos['buy_price']) / pos['buy_price'] * 100
    trades.append({'code':code,'pnl':pnl,'d':pos['days_held'],'r':'期末强平'})

total_value = cash + sum(kline_map.get((c, last_date), p['buy_price']) * p['shares'] for c, p in positions.items())
return_pct = (total_value - 1_000_000) / 1_000_000 * 100

print(f"\n💰 最终: {total_value:.0f} ({return_pct:+.2f}%)")
print(f"\n📝 逐笔交易明细:")
wins = 0; losses = 0; win_pnl = 0; loss_pnl = 0
for i, t in enumerate(trades, 1):
    wl = '🟢' if t['pnl'] > 0 else '🔴'
    if t['pnl'] > 0: wins += 1; win_pnl += t['pnl']
    else: losses += 1; loss_pnl += t['pnl']
    print(f"  {i:2d}. {t['code']:>10s} | {t['r']:<8s} | {t['d']:2d}d | {t['pnl']:+.1f}% {wl}")

print(f"\n📊 统计: {len(trades)}笔 | 胜{wins}({wins/len(trades)*100:.0f}%) 负{losses}({losses/len(trades)*100:.0f}%)")
print(f"  平均盈利: {win_pnl/wins:.2f}%" if wins > 0 else "")
print(f"  平均亏损: {loss_pnl/losses:.2f}%" if losses > 0 else "")
print(f"  盈亏比: {win_pnl/wins/abs(loss_pnl/losses):.2f}" if losses > 0 and loss_pnl != 0 else "")
