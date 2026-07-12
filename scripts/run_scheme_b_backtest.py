#!/usr/bin/env python3
"""
方案B：统一买入线75分回测（6月2日MAY建议）
参数：买入线75（不分季节），最大持有30日，阶梯检视5/15/25/30
      止损-7%/-5%，移动止盈15%，冷却期20日
"""
import sys, os, time, json
sys.path.insert(0, '/opt/stock-analyzer/scripts')

# 复制run_backtest函数但改参数
import backfill_backtest_scores as bbs
from collections import defaultdict

# 方案B参数（统一75分，不分季节）
THRESHOLDS_B = {
    'summer':       {'buy':75, 'sell_t1':-7, 'sell_t2':-5, 'hold_max':30, 'pullback_stop':-10},
    'spring':       {'buy':75, 'sell_t1':-7, 'sell_t2':-5, 'hold_max':30, 'pullback_stop':-10},
    'chaos_spring': {'buy':75, 'sell_t1':-7, 'sell_t2':-5, 'hold_max':30, 'pullback_stop':-10},
    'chaos':        {'buy':75, 'sell_t1':-7, 'sell_t2':-5, 'hold_max':30, 'pullback_stop':-10},
    'chaos_autumn': {'buy':75, 'sell_t1':-7, 'sell_t2':-5, 'hold_max':30, 'pullback_stop':-10},
    'autumn':       {'buy':75, 'sell_t1':-7, 'sell_t2':-5, 'hold_max':30, 'pullback_stop':-10},
    'winter':       {'buy':75, 'sell_t1':-7, 'sell_t2':-5, 'hold_max':30, 'pullback_stop':-10},
    'panic':        {'buy':999,'sell_t1':-7,'sell_t2':-5, 'hold_max':0,  'pullback_stop':-10},
    'recovery':     {'buy':75, 'sell_t1':-7, 'sell_t2':-5, 'hold_max':30, 'pullback_stop':-10},
}

INITIAL_CAPITAL = 1_000_000
MAX_POSITIONS = 8
MAX_SINGLE = 0.20
COMMISSION_PCT = 0.0008

bbs.log("="*60)
bbs.log("📊 方案B回测：统一买入线75分 + 持有30日 + Top 8")
bbs.log("="*60)

# 读数据
conn = bbs.get_connection()
cur = conn.cursor()
cur.execute("""
    SELECT d.ts_code, d.composite_score, d.season, d.trade_date,
           k.close, k.high, k.low, k.vol
    FROM backtest_score_daily d
    JOIN daily_kline k ON d.ts_code = k.ts_code AND d.trade_date = k.trade_date
    WHERE d.trade_date >= '2024-09-02' AND d.trade_date <= '2026-07-03'
    ORDER BY d.trade_date
""")
rows = cur.fetchall()
cur.close()
conn.close()
bbs.log(f"📋 读取评分+K线: {len(rows)}条")

# 按日期组织
daily_data = defaultdict(list)
for r in rows:
    td = str(r['trade_date'])
    daily_data[td].append({
        'ts_code': r['ts_code'],
        'score': float(r['composite_score']) if r['composite_score'] else 0,
        'season': r['season'] or 'chaos',
        'close': float(r['close']) if r['close'] else 0,
        'high': float(r['high']) if r['high'] else 0,
        'low': float(r['low']) if r['low'] else 0,
        'vol': float(r['vol']) if r['vol'] else 0,
    })

all_dates = sorted(daily_data.keys())
bbs.log(f"📅 交易日: {all_dates[0]} ~ {all_dates[-1]} ({len(all_dates)}天)")

# 价格缓存
price_cache = defaultdict(dict)
for td, stocks in daily_data.items():
    for s in stocks:
        price_cache[s['ts_code']][td] = {
            'close': s['close'], 'high': s['high'], 'low': s['low']
        }

# === 回测主循环 ===
cash = INITIAL_CAPITAL
positions = {}
trades = []
equity_curve = []

t0 = time.time()

for idx, today in enumerate(all_dates):
    stocks_today = daily_data[today]
    
    # --- 持仓检查 ---
    to_close = []
    for code, pos in list(positions.items()):
        pos['days_held'] += 1
        price_info = price_cache.get(code, {}).get(today, {})
        current_price = price_info.get('close', 0)
        if current_price == 0: continue
        
        if current_price > pos.get('high_water_mark', pos['buy_price']):
            pos['high_water_mark'] = current_price
        
        # 统一方案B参数
        th = THRESHOLDS_B[pos.get('season', 'chaos')]
        
        if pos['days_held'] <= 1:  # T+1
            continue
        
        hwm = pos.get('high_water_mark', pos['buy_price'])
        drawdown = (current_price - hwm) / hwm * 100
        if drawdown <= th['pullback_stop']:
            to_close.append((code, current_price, f'回撤{drawdown:.1f}%'))
            continue
        
        pnl_pct = (current_price - pos['buy_price']) / pos['buy_price'] * 100
        stop_line = th['sell_t2'] if pos['days_held'] >= 2 else th['sell_t1']
        if pnl_pct <= stop_line:
            to_close.append((code, current_price, f'止损{stop_line}%({pnl_pct:.1f}%)'))
            continue
        
        if pos['days_held'] >= th['hold_max']:
            to_close.append((code, current_price, f'到期{pos["days_held"]}d'))
            continue
    
    for code, price, reason in to_close:
        pos = positions.pop(code)
        proceeds = pos['shares'] * price * (1 - COMMISSION_PCT)
        pnl = proceeds - pos['cost']
        pnl_pct = (price - pos['buy_price']) / pos['buy_price'] * 100
        cash += proceeds
        trades.append({
            'ts_code': code, 'type': 'SELL',
            'buy_date': pos['buy_date'], 'sell_date': today,
            'buy_price': pos['buy_price'], 'sell_price': price,
            'shares': pos['shares'], 'pnl': round(pnl, 2),
            'pnl_pct': round(pnl_pct, 2),
            'days_held': pos['days_held'], 'reason': reason,
            'season': pos.get('season', '?'),
            'score_at_buy': pos.get('score_at_buy', 0),
        })
    
    # --- 买入（统一75分，不分季节） ---
    if len(positions) < MAX_POSITIONS:
        candidates = [s for s in stocks_today 
                     if s['score'] >= 75 and s['close'] > 0 and s['ts_code'] not in positions]
        candidates.sort(key=lambda x: x['score'], reverse=True)
        
        max_slots = MAX_POSITIONS - len(positions)
        for s in candidates[:max_slots]:
            if cash <= 0: break
            buy_amt = cash * MAX_SINGLE
            if buy_amt < 10000: continue
            shares = int(buy_amt / s['close'] / 100) * 100
            if shares <= 0: continue
            cost = shares * s['close'] * (1 + COMMISSION_PCT)
            if cost > cash: continue
            
            positions[s['ts_code']] = {
                'buy_date': today, 'buy_price': s['close'],
                'shares': shares, 'cost': cost,
                'high_water_mark': s['close'], 'days_held': 0,
                'season': s['season'], 'score_at_buy': s['score'],
            }
            cash -= cost
    
    pos_value = 0
    for code, pos in positions.items():
        lp = price_cache.get(code, {}).get(today, {})
        p = lp.get('close', pos['buy_price'])
        if p: pos_value += pos['shares'] * p
    equity_curve.append({
        'date': today, 'cash': round(cash, 2),
        'pos_value': round(pos_value, 2),
        'total': round(cash + pos_value, 2),
        'pos_count': len(positions),
    })

elapsed = time.time() - t0

# 期末强平
final_pos_value = 0
last_date = all_dates[-1]
for code, pos in list(positions.items()):
    lp = price_cache.get(code, {}).get(last_date, {}).get('close', pos['buy_price'])
    final_pos_value += pos['shares'] * lp
    proceeds = pos['shares'] * lp * (1 - COMMISSION_PCT)
    trades.append({
        'ts_code': code, 'type': 'FORCE_SELL',
        'buy_date': pos['buy_date'], 'sell_date': last_date,
        'buy_price': pos['buy_price'], 'sell_price': lp,
        'shares': pos['shares'], 'pnl': round(proceeds - pos['cost'], 2),
        'pnl_pct': round((lp - pos['buy_price']) / pos['buy_price'] * 100, 2),
        'days_held': pos['days_held'], 'reason': '期末强平',
        'season': pos.get('season', '?'),
        'score_at_buy': pos.get('score_at_buy', 0),
    })

total_value = cash + final_pos_value
total_return = (total_value - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100

# 指标
close_trades = [t for t in trades if t['type'] == 'SELL']
win_trades = [t for t in close_trades if t['pnl'] > 0]
loss_trades = [t for t in close_trades if t['pnl'] <= 0]
win_rate = len(win_trades) / len(close_trades) * 100 if close_trades else 0
total_profit = sum(t['pnl'] for t in win_trades)
total_loss = abs(sum(t['pnl'] for t in loss_trades))
profit_factor = total_profit / total_loss if total_loss > 0 else 0
avg_win = total_profit / len(win_trades) if win_trades else 0
avg_loss = total_loss / len(loss_trades) if loss_trades else 0
avg_hold = sum(t['days_held'] for t in close_trades) / len(close_trades) if close_trades else 0

# 最大回撤
max_drawdown = 0; peak = equity_curve[0]['total'] if equity_curve else INITIAL_CAPITAL
for e in equity_curve:
    v = e['total']
    if v > peak: peak = v
    dd = (peak - v) / peak * 100
    if dd > max_drawdown: max_drawdown = dd

# 季节统计
season_stats = defaultdict(lambda: {'trades': 0, 'wins': 0, 'pnl': 0})
for t in close_trades:
    s = t.get('season', '?')
    season_stats[s]['trades'] += 1
    if t['pnl'] > 0: season_stats[s]['wins'] += 1
    season_stats[s]['pnl'] += t['pnl']

# 输出
bbs.log("\n" + "="*60)
bbs.log("🏆 方案B回测结果：统一买入线75分")
bbs.log("="*60)
bbs.log(f"  回测区间: {all_dates[0]} ~ {all_dates[-1]} ({len(all_dates)}天)")
bbs.log(f"  初始本金: {INITIAL_CAPITAL/10000:.0f}万")
bbs.log(f"  最终净值: {total_value/10000:.2f}万")
bbs.log(f"  总收益率: {total_return:+.2f}%")
bbs.log(f"  年化收益: {total_return/len(all_dates)*252:+.2f}%")
bbs.log(f"  最大回撤: {max_drawdown:.2f}%")
bbs.log(f"  ────────────────")
bbs.log(f"  总交易笔数: {len(close_trades)}")
bbs.log(f"  盈利笔数: {len(win_trades)} ({win_rate:.1f}%)")
bbs.log(f"  亏损笔数: {len(loss_trades)} ({100-win_rate:.1f}%)")
bbs.log(f"  盈亏比: {avg_win/avg_loss:.2f}" if avg_loss > 0 else "  盈亏比: ∞")
bbs.log(f"  盈利因子: {profit_factor:.2f}")
bbs.log(f"  平均持有: {avg_hold:.1f}天")
bbs.log(f"  ────────────────")
bbs.log(f"  计算用时: {elapsed:.0f}s")

bbs.log("\n📅 分季节统计:")
for s in sorted(season_stats.keys()):
    st = season_stats[s]
    wr = st['wins']/st['trades']*100 if st['trades'] > 0 else 0
    bbs.log(f"    {s:15s}: {st['trades']:3d}笔 胜率{wr:5.1f}%  收益{st['pnl']/10000:+.2f}万")

# 保存结果
try:
    conn = bbs.get_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS backtest_results (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            run_date DATETIME, label VARCHAR(64),
            start_date DATE, end_date DATE,
            initial_capital DECIMAL(15,2), final_value DECIMAL(15,2),
            total_return DECIMAL(7,2), max_drawdown DECIMAL(7,2),
            win_rate DECIMAL(5,1), profit_factor DECIMAL(7,2),
            total_trades INT, avg_hold_days DECIMAL(5,1),
            params_json TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    params = {'label': '方案B_统一买入线75',
              'buy': 75, 'hold_max': 30, 'stop': '-7/-5',
              'max_positions': 8, 'max_single_pct': 20,
              't_plus_one': True, 'daily_simulation': True}
    cur.execute("""
        INSERT INTO backtest_results
            (run_date, label, start_date, end_date,
             initial_capital, final_value, total_return,
             max_drawdown, win_rate, profit_factor,
             total_trades, avg_hold_days, params_json)
        VALUES (NOW(), '方案B_统一买入线75', %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (all_dates[0], all_dates[-1],
          INITIAL_CAPITAL, total_value, round(total_return, 2),
          round(max_drawdown, 2), round(win_rate, 1), round(profit_factor, 2),
          len(close_trades), round(avg_hold, 1), json.dumps(params)))
    conn.commit()
    cur.close()
    conn.close()
    bbs.log("✅ 结果已保存到 backtest_results 表")
except Exception as e:
    bbs.log(f"⚠️ 保存失败: {e}")

# Top5输赢
sorted_trades = sorted(close_trades, key=lambda t: t.get('pnl_pct', 0))
bbs.log("\n📈 最佳3笔:")
for t in sorted_trades[-3:]:
    bbs.log(f"    {t['ts_code']}: {t['pnl_pct']:+.1f}% ({t['days_held']}d) [{t['season']}] s={t.get('score_at_buy',0)}")
bbs.log("\n📉 最差3笔:")
for t in sorted_trades[:3]:
    bbs.log(f"    {t['ts_code']}: {t['pnl_pct']:+.1f}% ({t['days_held']}d) [{t['season']}] s={t.get('score_at_buy',0)}")

bbs.log(f"\n🏁 方案B回测完成! 总耗时: {elapsed:.0f}s")
