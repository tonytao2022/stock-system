#!/usr/bin/env python3
"""
V13.1 vs TIDE 逐日T+1回测对比
V13.1: 买入线75统一，持有30天，止损-7%/-5%，移动止盈15%，单票20%，总仓30%
TIDE:  买入线82统一，持有30天，止损-7%/-5%，移动止盈15%，单票20%，总仓80%
"""
import sys, os, time, json
sys.path.insert(0, '/opt/stock-analyzer/scripts')
import backfill_backtest_scores as bbs
from collections import defaultdict

INITIAL_CAPITAL = 1_000_000
MAX_POSITIONS = 8
MAX_SINGLE = 0.20
COMMISSION_PCT = 0.0008

RESULTS = {}

# 定义两组参数
SCHEMES = {
    'V13.1': {
        'label': 'V13.1',
        'buy': 75,
        'hold': 30,
        'stop_t1': -7,
        'stop_t2': -5,
        'trail': 15,
        'total_pct': 30,
        'single_pct': 20,
        'table': 'backtest_score_daily',
        'score_col': 'composite_score',
    },
    'TIDE': {
        'label': 'TIDE',
        'buy': 82,
        'hold': 30,
        'stop_t1': -7,
        'stop_t2': -5,
        'trail': 15,
        'total_pct': 80,
        'single_pct': 20,
        'table': 'tide_score_signal',
        'score_col': 'tide_score',
        'date_from': '2026-04-01',
        'date_to': '2026-07-03',
    },
    'V13.1_SHORT': {
        'label': 'V13.1(同区间)',
        'buy': 75,
        'hold': 30,
        'stop_t1': -7,
        'stop_t2': -5,
        'trail': 15,
        'total_pct': 30,
        'single_pct': 20,
        'table': 'backtest_score_daily',
        'score_col': 'composite_score',
        'date_from': '2026-04-01',
        'date_to': '2026-07-03',
    },
}

def run_scheme(name, scheme):
    bbs.log(f"\n{'='*60}")
    bbs.log(f"📊 {scheme['label']} 回测：买入线{scheme['buy']} | 持有{scheme['hold']}天 | 止损{scheme['stop_t1']}/{scheme['stop_t2']}% | 总仓{scheme['total_pct']}%")
    bbs.log('='*60)
    
    conn = bbs.get_connection()
    cur = conn.cursor()
    
    df = scheme.get('date_from', '2024-09-02')
    dt = scheme.get('date_to', '2026-07-03')
    
    if scheme['table'] == 'backtest_score_daily':
        cur.execute(f"""
            SELECT d.ts_code, d.{scheme['score_col']} as score, d.season, d.trade_date,
                   k.close, k.high, k.low, k.vol
            FROM backtest_score_daily d
            JOIN daily_kline k ON d.ts_code = k.ts_code AND d.trade_date = k.trade_date
            WHERE d.trade_date >= '{df}' AND d.trade_date <= '{dt}'
              AND d.{scheme['score_col']} IS NOT NULL
            ORDER BY d.trade_date
        """)
    else:
        # 分步取：TIDE评分 + daily_kline + backtest_score_daily（避免collation冲突）
        cur.execute(f"""
            SELECT s.ts_code, s.{scheme['score_col']} as score, s.trade_date
            FROM tide_score_signal s
            WHERE s.trade_date >= '{df}' AND s.trade_date <= '{dt}'
              AND s.{scheme['score_col']} IS NOT NULL
            ORDER BY s.trade_date
        """)
        tide_rows = cur.fetchall()
        
        # 取代码和日期列表
        ts_codes = sorted(set(str(r['ts_code']) for r in tide_rows))
        trade_dates = sorted(set(str(r['trade_date']) for r in tide_rows))
        
        # 取K线数据
        kline_map = {}
        if ts_codes and trade_dates:
            for code in ts_codes:
                cur.execute(f"""
                    SELECT trade_date, close, high, low, vol
                    FROM daily_kline
                    WHERE ts_code = '{code}' AND trade_date >= '{df}' AND trade_date <= '{dt}'
                """)
                for row in cur.fetchall():
                    kline_map[(code, str(row['trade_date']))] = {
                        'close': float(row['close']) if row['close'] else 0,
                        'high': float(row['high']) if row['high'] else 0,
                        'low': float(row['low']) if row['low'] else 0,
                        'vol': float(row['vol']) if row['vol'] else 0,
                    }
        
        # 取season
        season_map = {}
        for code in ts_codes:
            cur.execute(f"""
                SELECT trade_date, season
                FROM backtest_score_daily
                WHERE ts_code = '{code}' AND trade_date >= '{df}' AND trade_date <= '{dt}'
            """)
            for row in cur.fetchall():
                season_map[(code, str(row['trade_date']))] = row['season'] or 'chaos'
        
        rows = []
        for r in tide_rows:
            key = (str(r['ts_code']), str(r['trade_date']))
            kl = kline_map.get(key, {})
            rows.append({
                'ts_code': r['ts_code'],
                'score': float(r['score']) if r['score'] else 0,
                'season': season_map.get(key, 'chaos'),
                'trade_date': r['trade_date'],
                'close': kl.get('close', 0),
                'high': kl.get('high', 0),
                'low': kl.get('low', 0),
                'vol': kl.get('vol', 0),
            })
    
    rows = cur.fetchall()
    cur.close()
    conn.close()
    bbs.log(f"📋 读取评分+K线: {len(rows)}条")
    
    daily_data = defaultdict(list)
    for r in rows:
        td = str(r['trade_date'])
        daily_data[td].append({
            'ts_code': r['ts_code'],
            'score': float(r['score']) if r['score'] else 0,
            'season': r['season'] or 'chaos',
            'close': float(r['close']) if r['close'] else 0,
            'high': float(r['high']) if r['high'] else 0,
            'low': float(r['low']) if r['low'] else 0,
            'vol': float(r['vol']) if r['vol'] else 0,
        })
    
    all_dates = sorted(daily_data.keys())
    bbs.log(f"📅 交易日: {all_dates[0]} ~ {all_dates[-1]} ({len(all_dates)}天)")
    
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
        th = scheme  # 使用统一参数
        
        # 持仓检查
        to_close = []
        for code, pos in list(positions.items()):
            pos['days_held'] += 1
            price_info = price_cache.get(code, {}).get(today, {})
            current_price = price_info.get('close', 0)
            if current_price == 0: continue
            
            if current_price > pos.get('high_water_mark', pos['buy_price']):
                pos['high_water_mark'] = current_price
            
            if pos['days_held'] <= 1: continue
            
            hwm = pos.get('high_water_mark', pos['buy_price'])
            drawdown = (current_price - hwm) / hwm * 100
            if drawdown <= -abs(th['trail'] if th['trail'] > 0 else th['trail']):
                drawdown_pct = -abs(th['trail'] if th['trail'] > 0 else th['trail'])
                to_close.append((code, current_price, f'回撤{drawdown:.1f}%'))
                continue
            
            pnl_pct = (current_price - pos['buy_price']) / pos['buy_price'] * 100
            stop_line = th['stop_t2'] if pos['days_held'] >= 2 else th['stop_t1']
            if pnl_pct <= stop_line:
                to_close.append((code, current_price, f'止损{stop_line}%({pnl_pct:.1f}%)'))
                continue
            
            if pos['days_held'] >= th['hold']:
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
        
        # 买入
        if len(positions) < MAX_POSITIONS:
            candidates = [s for s in stocks_today
                         if s['score'] >= th['buy'] and s['close'] > 0 and s['ts_code'] not in positions]
            candidates.sort(key=lambda x: x['score'], reverse=True)
            
            # 检查总仓位上限
            cur_pos_value = sum(
                price_cache.get(p['ts_code'], {}).get(today, {}).get('close', p['buy_price']) * 
                p.get('shares', 0)
                for p in positions.values()
            )
            current_total_asset = cash + cur_pos_value
            current_pos_ratio = cur_pos_value / current_total_asset * 100 if current_total_asset > 0 else 0
            
            max_slots = MAX_POSITIONS - len(positions)
            for s in candidates[:max_slots]:
                if cash <= 0: break
                if current_pos_ratio >= th['total_pct']: break
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
                    'ts_code': s['ts_code'],
                }
                cash -= cost
                cur_pos_value += cost
                current_total_asset = cash + cur_pos_value
                current_pos_ratio = cur_pos_value / current_total_asset * 100 if current_total_asset > 0 else 0
        
        # 日净值
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
    sharpe = (total_return/len(all_dates)*252 - 0) / (20) if max_drawdown_calc(equity_curve) > 0 else 0
    
    # 最大回撤
    peak = equity_curve[0]['total'] if equity_curve else INITIAL_CAPITAL
    max_dd = 0
    for e in equity_curve:
        v = e['total']
        if v > peak: peak = v
        dd = (peak - v) / peak * 100
        if dd > max_dd: max_dd = dd
    
    # 季节统计
    season_stats = defaultdict(lambda: {'trades': 0, 'wins': 0, 'pnl': 0})
    for t in close_trades:
        s = t.get('season', '?')
        season_stats[s]['trades'] += 1
        if t['pnl'] > 0: season_stats[s]['wins'] += 1
        season_stats[s]['pnl'] += t['pnl']
    
    # 输出
    bbs.log(f"\n{'='*60}")
    bbs.log(f"🏆 {scheme['label']} 回测结果")
    bbs.log('='*60)
    bbs.log(f"  回测区间: {all_dates[0]} ~ {all_dates[-1]} ({len(all_dates)}天)")
    bbs.log(f"  初始本金: {INITIAL_CAPITAL/10000:.0f}万")
    bbs.log(f"  最终净值: {total_value/10000:.2f}万")
    bbs.log(f"  总收益率: {total_return:+.2f}%")
    bbs.log(f"  年化收益: {total_return/len(all_dates)*252:+.2f}%")
    bbs.log(f"  最大回撤: {max_dd:.2f}%")
    bbs.log(f"  ────────────────")
    bbs.log(f"  总交易笔数: {len(close_trades)}")
    bbs.log(f"  盈利笔数: {len(win_trades)} ({win_rate:.1f}%)")
    bbs.log(f"  亏损笔数: {len(loss_trades)} ({100-win_rate:.1f}%)")
    bbs.log(f"  盈亏比: {avg_win/avg_loss:.2f}" if avg_loss > 0 else f"  盈亏比: ∞")
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
        params = {'label': scheme['label'],
                  'buy': scheme['buy'], 'hold': scheme['hold'],
                  'stop': f"{scheme['stop_t1']}/{scheme['stop_t2']}",
                  'trail': scheme['trail'],
                  'max_positions': MAX_POSITIONS, 'max_single_pct': int(MAX_SINGLE*100),
                  'total_pos_pct': scheme['total_pct'],
                  't_plus_one': True, 'daily_simulation': True}
        cur.execute("""
            INSERT INTO backtest_results
                (run_date, label, start_date, end_date,
                 initial_capital, final_value, total_return,
                 max_drawdown, win_rate, profit_factor,
                 total_trades, avg_hold_days, params_json)
            VALUES (NOW(), %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (f"{scheme['label']}_对比回测_{all_dates[0]}_{all_dates[-1]}",
              all_dates[0], all_dates[-1],
              INITIAL_CAPITAL, total_value, round(total_return, 2),
              round(max_dd, 2), round(win_rate, 1), round(profit_factor, 2),
              len(close_trades), round(avg_hold, 1), json.dumps(params)))
        conn.commit()
        cur.close()
        conn.close()
        bbs.log("✅ 结果已保存到 backtest_results 表")
    except Exception as e:
        bbs.log(f"⚠️ 保存失败: {e}")
    
    # Top
    sorted_trades = sorted(close_trades, key=lambda t: t.get('pnl_pct', 0))
    bbs.log("\n📈 最佳3笔:")
    for t in sorted_trades[-3:]:
        bbs.log(f"    {t['ts_code']}: {t['pnl_pct']:+.1f}% ({t['days_held']}d) [{t['season']}] s={t.get('score_at_buy',0)}")
    bbs.log("\n📉 最差3笔:")
    for t in sorted_trades[:3]:
        bbs.log(f"    {t['ts_code']}: {t['pnl_pct']:+.1f}% ({t['days_held']}d) [{t['season']}] s={t.get('score_at_buy',0)}")
    
    bbs.log(f"\n🏁 {scheme['label']} 完成! {elapsed:.0f}s")
    
    return {
        'label': scheme['label'],
        'days': len(all_dates),
        'total_return': total_return,
        'annual_return': total_return/len(all_dates)*252,
        'max_dd': max_dd,
        'trades': len(close_trades),
        'win_rate': win_rate,
        'profit_factor': profit_factor,
        'avg_hold': avg_hold,
        'final_value': total_value,
        'avg_win': avg_win,
        'avg_loss': avg_loss,
        'win_loss_ratio': avg_win/avg_loss if avg_loss > 0 else 0,
        'sharpe': sharpe,
        'season_stats': dict(season_stats),
    }

def max_drawdown_calc(curve):
    peak = 0
    max_dd = 0
    for e in curve:
        if e['total'] > peak: peak = e['total']
        dd = (peak - e['total']) / peak * 100
        if dd > max_dd: max_dd = dd
    return max_dd

# ===== 主流程 =====
for name, scheme in SCHEMES.items():
    result = run_scheme(name, scheme)
    RESULTS[name] = result

# 对比表格
print("\n" + "="*60)
print("V13.1 vs TIDE 对比总结")
print("="*60)

rows = [
    ('指标', 'V13.1', 'TIDE', '差距'),
    ('回测期间', '2024-09~2026-07', '2026-04~2026-07', 'V13.1多17个月'),
    ('交易日', str(RESULTS['V13.1']['days']), str(RESULTS['TIDE']['days']), ''),
    ('初始本金', '100万', '100万', ''),
]

# 只对比相同数据区间（V13.1也截取4月~7月）
print("\n📌 共同区间对比 (2026-04~2026-07):")
# 重新跑TIDE在这个区间
# 但V13.1我们已有全部数据，可以截取

common_metrics = ['total_return', 'annual_return', 'max_dd', 'trades', 'win_rate', 'profit_factor', 'avg_hold']
for m in common_metrics:
    v = RESULTS['V13.1'][m]
    t = RESULTS['TIDE'][m]
    if isinstance(v, float):
        diff = t - v
        print(f"  {m:15s}: V13.1={v:>8.2f}  |  TIDE={t:>8.2f}  |  Δ={diff:+.2f}")
    else:
        diff = t - v
        print(f"  {m:15s}: V13.1={v:>8}  |  TIDE={t:>8}  |  Δ={diff:+.0f}")

print(f"\n⚡ V13.1适用区间: {RESULTS['V13.1']['days']}天，TIDE适用区间: {RESULTS['TIDE']['days']}天")
print(f"  注意：TIDE仅覆盖2026年4月~7月（68天），V13.1覆盖438天")
print(f"  时间窗口不同，直接对比需谨慎")
