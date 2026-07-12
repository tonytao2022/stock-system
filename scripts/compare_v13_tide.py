#!/usr/bin/env python3
"""
V13.1 vs TIDE 同区间逐日T+1回测对比
数据集: 2026-04-01 ~ 2026-07-03 (68交易日)
"""
import sys, os, time, json
sys.path.insert(0, '/opt/stock-analyzer/scripts')
import backfill_backtest_scores as bbs
from collections import defaultdict
from db_config import get_connection

INITIAL_CAPITAL = 1_000_000
MAX_POSITIONS = 8
MAX_SINGLE = 0.20
COMMISSION_PCT = 0.0008
DATE_FROM = '2026-04-01'
DATE_TO = '2026-07-03'

def fetch_data(table, score_col, label):
    """从指定表取评分+季节+K线数据（避免collation冲突，分步取）"""
    conn = get_connection()
    cur = conn.cursor()
    
    # 取评分
    season_col = 'season' if table == 'backtest_score_daily' else "'chaos' as season"
    cur.execute(f"""
        SELECT ts_code, {score_col} as score, {season_col}, trade_date
        FROM {table}
        WHERE trade_date >= '{DATE_FROM}' AND trade_date <= '{DATE_TO}'
          AND {score_col} IS NOT NULL
        ORDER BY trade_date
    """)
    score_rows = cur.fetchall()
    
    # 取K线
    ts_codes = sorted(set(str(r['ts_code']) for r in score_rows))
    kline_map = {}
    for code in ts_codes:
        cur.execute(f"""
            SELECT trade_date, close, high, low, vol
            FROM daily_kline
            WHERE ts_code = '{code}' AND trade_date >= '{DATE_FROM}' AND trade_date <= '{DATE_TO}'
        """)
        for row in cur.fetchall():
            kline_map[(code, str(row['trade_date']))] = {
                'close': float(row['close']) if row['close'] else 0,
                'high': float(row['high']) if row['high'] else 0,
                'low': float(row['low']) if row['low'] else 0,
                'vol': float(row['vol']) if row['vol'] else 0,
            }
    
    rows = []
    for r in score_rows:
        key = (str(r['ts_code']), str(r['trade_date']))
        kl = kline_map.get(key, {})
        rows.append({
            'ts_code': str(r['ts_code']),
            'score': float(r['score']) if r['score'] else 0,
            'season': str(r['season'] or 'chaos'),
            'trade_date': str(r['trade_date']),
            'close': kl.get('close', 0),
            'high': kl.get('high', 0),
            'low': kl.get('low', 0),
            'vol': kl.get('vol', 0),
        })
    cur.close()
    conn.close()
    bbs.log(f"📋 {label}: {len(rows)}条")
    return rows

def run_scheme(name, buy, hold, stop_t1, stop_t2, trail, total_pct, table, score_col):
    bbs.log("")
    bbs.log("="*60)
    bbs.log(f"📊 {name}：买入线{buy} | 持有{hold}d | 止损{stop_t1}/{stop_t2}% | 总仓{total_pct}%")
    bbs.log("="*60)
    
    raw_rows = fetch_data(table, score_col, name)
    
    # 按日期组织
    daily_data = defaultdict(list)
    for s in raw_rows:
        daily_data[s['trade_date']].append(s)
    
    all_dates = sorted(daily_data.keys())
    if not all_dates:
        bbs.log("❌ 无数据")
        return None
    bbs.log(f"📅 交易日: {all_dates[0]} ~ {all_dates[-1]} ({len(all_dates)}天)")
    
    # 价格缓存
    price_cache = defaultdict(dict)
    for td, stocks in daily_data.items():
        for s in stocks:
            price_cache[s['ts_code']][td] = {'close': s['close'], 'high': s['high'], 'low': s['low']}
    
    # 回测主循环
    cash = INITIAL_CAPITAL
    positions = {}
    trades = []
    t0 = time.time()
    
    for today in all_dates:
        stocks_today = daily_data[today]
        
        # 持仓检查
        to_close = []
        for code, pos in list(positions.items()):
            pos['days_held'] += 1
            cur_price = price_cache.get(code, {}).get(today, {}).get('close', 0)
            if cur_price == 0: continue
            if cur_price > pos.get('high_water_mark', pos['buy_price']):
                pos['high_water_mark'] = cur_price
            if pos['days_held'] <= 1: continue
            
            hwm = pos.get('high_water_mark', pos['buy_price'])
            drawdown = (cur_price - hwm) / hwm * 100
            if drawdown <= -abs(trail):
                to_close.append((code, cur_price, f'回撤{drawdown:.1f}%'))
                continue
            
            pnl_pct = (cur_price - pos['buy_price']) / pos['buy_price'] * 100
            stop_line = stop_t2 if pos['days_held'] >= 2 else stop_t1
            if pnl_pct <= stop_line:
                to_close.append((code, cur_price, f'止损{stop_line}%({pnl_pct:.1f}%)'))
                continue
            if pos['days_held'] >= hold:
                to_close.append((code, cur_price, f'到期{pos["days_held"]}d'))
                continue
        
        for code, price, reason in to_close:
            pos = positions.pop(code)
            proceeds = pos['shares'] * price * (1 - COMMISSION_PCT)
            pnl = proceeds - pos['cost']
            cash += proceeds
            trades.append({
                'code': code, 'buy_date': pos['buy_date'], 'sell_date': today,
                'buy_price': pos['buy_price'], 'sell_price': price,
                'pnl': round(pnl, 2),
                'pnl_pct': round((price - pos['buy_price']) / pos['buy_price'] * 100, 2),
                'days_held': pos['days_held'], 'reason': reason,
                'season': pos.get('season', '?'), 'score': pos.get('score_at_buy', 0),
            })
        
        # 买入
        if len(positions) < MAX_POSITIONS:
            cands = [s for s in stocks_today if s['score'] >= buy and s['close'] > 0 and s['ts_code'] not in positions]
            cands.sort(key=lambda x: x['score'], reverse=True)
            
            # 总仓检查
            cur_pos_val = sum(
                price_cache.get(pcode, {}).get(today, {}).get('close', pp['buy_price']) * pp.get('shares',0)
                for pcode, pp in positions.items()
            )
            total_asset = cash + cur_pos_val
            pos_ratio = cur_pos_val / total_asset * 100 if total_asset > 0 else 0
            
            for s in cands[:MAX_POSITIONS - len(positions)]:
                if cash <= 0: break
                if pos_ratio >= total_pct: break
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
                cur_pos_val += cost
                total_asset = cash + cur_pos_val
                pos_ratio = cur_pos_val / total_asset * 100 if total_asset > 0 else 0
    
    # 期末强平
    last_date = all_dates[-1]
    final_pos_value = 0
    for code, pos in list(positions.items()):
        lp = price_cache.get(code, {}).get(last_date, {}).get('close', pos['buy_price'])
        final_pos_value += pos['shares'] * lp
    
    total_value = cash + final_pos_value
    total_return = (total_value - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100
    
    # 指标
    close_trades = [t for t in trades if t['reason'] != '期末强平'] if any(t['reason'] == '期末强平' for t in trades) else trades
    wins = [t for t in close_trades if t['pnl'] > 0]
    losses = [t for t in close_trades if t['pnl'] <= 0]
    win_rate = len(wins)/len(close_trades)*100 if close_trades else 0
    avg_win = sum(t['pnl'] for t in wins)/len(wins) if wins else 0
    avg_loss = abs(sum(t['pnl'] for t in losses))/len(losses) if losses else 0
    profit_factor = sum(t['pnl'] for t in wins) / abs(sum(t['pnl'] for t in losses)) if losses and sum(t['pnl'] for t in losses) != 0 else float('inf')
    avg_hold = sum(t['days_held'] for t in close_trades)/len(close_trades) if close_trades else 0
    
    elapsed = time.time() - t0
    
    bbs.log(f"\n{'='*60}")
    bbs.log(f"🏆 {name} 回测结果")
    bbs.log('='*60)
    bbs.log(f"  最终净值: {total_value/10000:.2f}万")
    bbs.log(f"  总收益率: {total_return:+.2f}%")
    bbs.log(f"  年化收益: {total_return/len(all_dates)*252:+.2f}%")
    bbs.log(f"  总交易笔数: {len(close_trades)}")
    bbs.log(f"  盈利笔数: {len(wins)} ({win_rate:.1f}%)")
    bbs.log(f"  亏损笔数: {len(losses)} ({100-win_rate:.1f}%)")
    bbs.log(f"  盈亏比: {avg_win/avg_loss:.2f}" if avg_loss>0 else "")
    bbs.log(f"  盈利因子: {profit_factor:.2f}")
    bbs.log(f"  平均持有: {avg_hold:.1f}天")
    bbs.log(f"  {elapsed:.0f}s")
    
    # 季节统计
    ss = defaultdict(lambda: {'t':0,'w':0,'p':0})
    for t in close_trades:
        s = t.get('season','?')
        ss[s]['t'] += 1
        if t['pnl']>0: ss[s]['w'] += 1
        ss[s]['p'] += t['pnl']
    bbs.log("\n季节统计:")
    for s in sorted(ss.keys()):
        st = ss[s]
        wr = st['w']/st['t']*100 if st['t']>0 else 0
        bbs.log(f"  {s:15s}: {st['t']:3d}笔 胜率{wr:5.1f}%  收益{st['p']/10000:+.2f}万")
    
    return {
        'name': name, 'days': len(all_dates),
        'return': total_return, 'trades': len(close_trades),
        'win_rate': win_rate, 'profit_factor': profit_factor,
        'avg_hold': avg_hold, 'wins': len(wins), 'losses': len(losses),
        'avg_win': avg_win, 'avg_loss': avg_loss,
    }

# ===== 主流程 =====
v13 = run_scheme('V13.1', buy=75, hold=30, stop_t1=-7, stop_t2=-5, trail=15, total_pct=30,
                  table='backtest_score_daily', score_col='composite_score')
tide = run_scheme('TIDE', buy=82, hold=30, stop_t1=-7, stop_t2=-5, trail=15, total_pct=80,
                   table='tide_score_signal', score_col='tide_score')

print()
print("="*60)
print("V13.1 vs TIDE 同区间对比")
print(f"区间: {DATE_FROM} ~ {DATE_TO} ({v13['days']}个交易日)")
print("="*60)
print(f"  {'指标':<18s} {'V13.1':>10s} {'TIDE':>10s} {'差距':>10s}")
print(f"  {'-'*48}")
for m in ['return','trades','win_rate','profit_factor','avg_hold']:
    v = v13[m] if v13 else 0
    t = tide[m] if tide else 0
    if isinstance(v, float):
        unit = '%' if m in ('return','win_rate') else ''
        print(f"  {m:<18s} {v:>9.1f}{unit} {t:>9.1f}{unit} {t-v:>+9.1f}{unit}")
    else:
        print(f"  {m:<18s} {v:>10} {t:>10} {t-v:>+10}")

print(f"\nV13.1: 买入≥75 | 持30d | 止损-7%/-5% | 移动止盈15% | 单票≤20% | 总仓≤30%")
print(f"TIDE:  买入≥82 | 持30d | 止损-7%/-5% | 移动止盈15% | 单票≤20% | 总仓≤80%")
print(f"\n⚠️ 仅68个交易日数据，样本量有限，结论需谨慎")
