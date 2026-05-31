#!/usr/bin/env python3
"""
阶梯动态持有策略·全量回测 v1.0
================================
基于 daily_kline_qfq + season_state + watch_pool 历史数据的策略模拟回测：
  1. 逐日模拟评分（用历史K线计算简化评分）
  2. 按阶梯策略规则生成买卖信号
  3. 统计收益/胜率/盈亏比/最大回撤

V1实盘参数:
  买入 = 评分≥30
  5日止损 = 评分<20 或 最高点回撤>10%
  20日检查 = 评分≥20 继续持有
  30日检查 = 评分≥30 继续持有
  30日后 = 每10日再评估
  减半仓 = 亏损>5% 且 评分<25
  最长 = 60日
"""

import os, sys, json, math, time
from datetime import datetime, date, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_config import get_connection

import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('ladder_backtest')

# ─── 策略参数（V1实盘版本） ───
BUY_THRESHOLD = 30        # 买入门槛
STOP_SCORE = 20           # 5日止损评分线
STOP_DRAWDOWN = 10        # 回撤止损百分比
CHECK_10_SCORE = 20       # 10日检查
CHECK_20_SCORE = 20       # 20日检查
CHECK_30_SCORE = 30       # 30日检查
HALF_LOSS = 5             # 减半仓亏损线
HALF_SCORE = 25           # 减半仓评分线
MAX_HOLD = 60             # 最长持有

# ─── 简化评分函数（基于K线数据计算） ───
def calc_simple_score(row, season, industry=''):
    """基于单日K线和季节状态的简化评分（模拟ScoreEngineV4）"""
    import statistics
    
    close = float(row['close'])
    open_p = float(row['open'])
    high = float(row['high'])
    low = float(row['low'])
    vol = float(row.get('vol', 0) or 0)
    chg_pct = float(row.get('change_pct', 0) or 0)
    
    # 趋势分 (0-40): 基于短期动量
    trend = 20
    if chg_pct > 3: trend = 35
    elif chg_pct > 1: trend = 28
    elif chg_pct > 0: trend = 22
    elif chg_pct > -2: trend = 15
    else: trend = 8
    
    # 量能分 (0-20): 基于涨幅与量的配合
    volume = 10
    if chg_pct > 0 and vol > 1.5: volume = 18
    elif chg_pct > 0 and vol > 1.0: volume = 14
    elif chg_pct < 0 and vol > 1.5: volume = 6
    elif chg_pct < 0 and vol < 0.8: volume = 12
    
    # 季节调整 (0-20)
    season_bonus = 10
    if season in ('chaos', 'panic'): season_bonus = 6
    elif season in ('chaos_spring', 'chaos_autumn'): season_bonus = 10
    elif season in ('spring', 'summer'): season_bonus = 14
    elif season in ('autumn', 'winter'): season_bonus = 12
    elif season == 'recovery': season_bonus = 16
    
    # 波动分 (0-20): (high-low)/close
    volatility = ((high - low) / close * 100) if close > 0 else 0
    vol_score = 10
    if volatility < 2: vol_score = 14  # 低波动稳定
    elif volatility < 4: vol_score = 10
    elif volatility < 6: vol_score = 6
    else: vol_score = 3
    
    raw = trend + volume + season_bonus + vol_score
    return max(0, min(100, raw))


# ─── 回测主逻辑 ───
def run_backtest():
    start_time = time.time()
    
    # 1. 加载数据
    logger.info("加载数据...")
    from db_config import get_connection; conn = get_connection()
    cur = conn.cursor(pymysql.cursors.DictCursor)

    # 获取监控池股票
    cur.execute("SELECT dk.ts_code, COALESCE(sb.name, dk.ts_code) as name, COALESCE(sb.industry, '') as industry FROM (SELECT ts_code FROM daily_kline_qfq GROUP BY ts_code HAVING COUNT(*) >= 500) dk LEFT JOIN stock_basic sb ON dk.ts_code = sb.ts_code ORDER BY dk.ts_code")
    pool = {r["ts_code"]: {'name': r['name'], 'industry': r['industry'] or ''} for r in cur.fetchall()}
    codes = list(pool.keys())
    logger.info(f"  监控池: {len(codes)} 只股票")

    # 获取季节状态
    cur.execute("SELECT trade_date, season, raw_score FROM season_state WHERE index_code='MARKET' ORDER BY trade_date")
    seasons_arr = cur.fetchall()
    season_map = {}
    for r in seasons_arr:
        dt = str(r['trade_date'])
        season_map[dt] = {'season': r['season'], 'score': float(r['raw_score'] or 0)}
    logger.info(f"  季节数据: {len(season_map)} 天")
    
    # 获取所有K线数据
    cur.execute("""
        SELECT ts_code, trade_date, `open`, high, low, close, 
               change_pct, vol, amount 
        FROM daily_kline_qfq 
        WHERE ts_code IN ({})
        ORDER BY ts_code, trade_date
    """.format(','.join(['%s'] * len(codes))), codes)
    
    kline_raw = cur.fetchall()
    kline = defaultdict(list)
    for r in kline_raw:
        code = r['ts_code']
        kline[code].append({
            'trade_date': str(r['trade_date']),
            'open': float(r['open'] or 0),
            'high': float(r['high'] or 0),
            'low': float(r['low'] or 0),
            'close': float(r['close'] or 0),
            
            'change_pct': float(r['change_pct'] or 0),
            'vol': float(r['vol'] or 0),
            'amount': float(r['amount'] or 0),
        })
    logger.info(f"  K线数据: {len(kline_raw)} 条, {len(kline)} 只股票")
    
    cur.close()
    conn.close()

    # 只保留K线数据充足的股票（≥200天）
    filtered = {c: k for c, k in kline.items() if len(k) >= 500}
    skipped = len(kline) - len(filtered)
    logger.info(f"  K线充足(≥200天): {len(filtered)} 只, 跳过: {skipped} 只")
    
    # 2. 逐日模拟交易
    logger.info("\n开始回测(模拟阶梯策略交易)...")
    
    all_trades = []  # 所有交易记录
    code_stats = defaultdict(lambda: {'trades': 0, 'wins': 0, 'total_return': 0.0, 'total_days': 0})
    
    # 回测区间
    start_date = "2023-01-02"
    end_date = '2026-05-29'
    
    for code in filtered:
        k = filtered[code]
        industry = pool.get(code, {}).get('industry', '')
        
        # 找到回测区间内的数据索引
        trade_dates = [r['trade_date'] for r in k]
        trade_data = {r['trade_date']: r for r in k}
        
        # 交易日列表（按顺序）
        all_dates = sorted([d for d in trade_dates if start_date <= d <= end_date])
        if len(all_dates) < 20:
            continue
        
        # 模拟持仓
        position = {
            'holding': False,
            'buy_date': '',
            'buy_price': 0,
            'buy_score': 0,
            'highest': 0,
            'days_held': 0,
            'last_check_day': 0,
        }
        
        for i in range(len(all_dates)):
            today = all_dates[i]
            row = trade_data[today]
            season_info = season_map.get(today, {'season': 'chaos', 'score': 0})
            season = season_info['season']
            
            # 计算评分
            score = calc_simple_score(row, season, industry)
            
            close = row['close']
            
            if not position['holding']:
                # 未持仓 → 检查买入
                if score >= BUY_THRESHOLD:
                    position['holding'] = True
                    position['buy_date'] = today
                    position['buy_price'] = close
                    position['buy_score'] = score
                    position['highest'] = close
                    position['days_held'] = 0
                    position['last_check_day'] = i
            
            else:
                # 已持仓 → 检查卖出
                position['days_held'] += 1
                position['highest'] = max(position['highest'], close)
                
                # 当前盈亏
                profit_pct = (close - position['buy_price']) / position['buy_price'] * 100
                drawdown = (position['highest'] - close) / position['highest'] * 100 if position['highest'] > 0 else 0
                
                sell = False
                sell_reason = ''
                
                # 规则1: 回撤止损 > 10%
                if drawdown > STOP_DRAWDOWN:
                    sell = True
                    sell_reason = 'stop_drawdown'
                
                # 规则2: 5日止损 评分<20
                elif position['days_held'] >= 5 and score < STOP_SCORE:
                    sell = True
                    sell_reason = 'stop_score'
                
                # 规则3: 减半仓信号（亏损>5%且评分<25）
                if profit_pct < -HALF_LOSS and score < HALF_SCORE:
                    # 减半仓记录（不算完全卖出）
                    pass  # 简化处理：减半仓信号不在本回测中模拟
                
                # 规则4: 20日检查
                if not sell and position['days_held'] >= 20 and score < CHECK_20_SCORE:
                    sell = True
                    sell_reason = 'check_20'
                
                # 规则5: 30日检查
                if not sell and position['days_held'] >= 30 and score < CHECK_30_SCORE:
                    sell = True
                    sell_reason = 'check_30'
                
                # 规则6: 60日最长持有
                if not sell and position['days_held'] >= MAX_HOLD:
                    sell = True
                    sell_reason = 'max_hold_60'
                
                if sell:
                    trade_profit = profit_pct
                    trade_days = position['days_held']
                    
                    all_trades.append({
                        'ts_code': code,
                        'name': pool[code]['name'],
                        'industry': industry,
                        'buy_date': position['buy_date'],
                        'sell_date': today,
                        'buy_price': round(position['buy_price'], 2),
                        'sell_price': round(close, 2),
                        'profit_pct': round(trade_profit, 2),
                        'days_held': trade_days,
                        'sell_reason': sell_reason,
                        'buy_score': position['buy_score'],
                        'sell_score': score,
                    })
                    
                    code_stats[code]['trades'] += 1
                    code_stats[code]['wins'] += 1 if trade_profit > 0 else 0
                    code_stats[code]['total_return'] += trade_profit
                    code_stats[code]['total_days'] += trade_days
                    
                    position['holding'] = False
    
    # 3. 统计结果
    logger.info(f"\n{'='*60}")
    logger.info(f"回测完成！总交易: {len(all_trades)} 笔")
    logger.info(f"{'='*60}")
    
    if not all_trades:
        logger.info("无交易记录，检查参数")
        return
    
    profits = [t['profit_pct'] for t in all_trades]
    days = [t['days_held'] for t in all_trades]
    
    wins = [p for p in profits if p > 0]
    losses = [p for p in profits if p <= 0]
    
    win_rate = len(wins) / len(profits) * 100 if profits else 0
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = sum(losses) / len(losses) if losses else 0
    avg_profit = sum(profits) / len(profits) if profits else 0
    avg_days = sum(days) / len(days) if days else 0
    
    profit_factor = abs(avg_win / avg_loss) if avg_loss != 0 else float('inf')
    
    # 盈亏比（平均盈利/平均亏损的绝对值的比率）
    win_loss_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else float('inf')
    
    # 最大回撤（连续亏损）
    max_drawdown = 0
    current_dd = 0
    for p in profits:
        if p < 0:
            current_dd += abs(p)
            max_drawdown = max(max_drawdown, current_dd)
        else:
            current_dd = 0
    
    # 按持有周期统计
    period_stats = {
        '≤10日': {'trades': 0, 'wins': 0, 'profit': 0},
        '11-20日': {'trades': 0, 'wins': 0, 'profit': 0},
        '21-30日': {'trades': 0, 'wins': 0, 'profit': 0},
        '31-60日': {'trades': 0, 'wins': 0, 'profit': 0},
    }
    for t in all_trades:
        d = t['days_held']
        if d <= 10: key = '≤10日'
        elif d <= 20: key = '11-20日'
        elif d <= 30: key = '21-30日'
        else: key = '31-60日'
        period_stats[key]['trades'] += 1
        period_stats[key]['wins'] += 1 if t['profit_pct'] > 0 else 0
        period_stats[key]['profit'] += t['profit_pct']
    
    # 按卖出原因统计
    reason_stats = defaultdict(lambda: {'trades': 0, 'wins': 0, 'profit': 0})
    for t in all_trades:
        r = t['sell_reason']
        reason_stats[r]['trades'] += 1
        reason_stats[r]['wins'] += 1 if t['profit_pct'] > 0 else 0
        reason_stats[r]['profit'] += t['profit_pct']
    
    # ─── 输出结果 ───
    logger.info(f"\n📊 ==== 阶梯策略回测结果 ====")
    logger.info(f"{'='*60}")
    logger.info(f"  回测区间: {start_date} ~ {end_date}")
    logger.info(f"  股票池: {len(filtered)} 只")
    logger.info(f"  总交易: {len(all_trades)} 笔")
    logger.info(f"  {'='*50}")
    logger.info(f"  盈利交易: {len(wins)} 笔")
    logger.info(f"  亏损交易: {len(losses)} 笔")
    logger.info(f"  胜率: {win_rate:.2f}%")
    logger.info(f"  平均盈利: +{avg_win:.2f}%")
    logger.info(f"  平均亏损: {avg_loss:.2f}%")
    logger.info(f"  平均收益: {avg_profit:+.2f}%")
    logger.info(f"  盈亏比: {win_loss_ratio:.2f}")
    logger.info(f"  平均持仓: {avg_days:.0f} 天")
    logger.info(f"  最大连续回撤: {max_drawdown:.2f}%")
    
    logger.info(f"\n📈 ==== 按持有周期统计 ====")
    logger.info(f"{'─'*60}")
    logger.info(f"  {'周期':<10} {'交易':<8} {'胜率':<10} {'均收益':<10}")
    logger.info(f"{'─'*60}")
    for period, st in sorted(period_stats.items()):
        wr = st['wins'] / st['trades'] * 100 if st['trades'] else 0
        avg = st['profit'] / st['trades'] if st['trades'] else 0
        logger.info(f"  {period:<10} {st['trades']:<8} {wr:<10.1f}% {avg:<+10.2f}%")
    
    logger.info(f"\n🔍 ==== 按卖出原因统计 ====")
    logger.info(f"{'─'*60}")
    for reason, st in sorted(reason_stats.items()):
        wr = st['wins'] / st['trades'] * 100 if st['trades'] else 0
        avg = st['profit'] / st['trades'] if st['trades'] else 0
        logger.info(f"  {reason:<20} {st['trades']:<8} {wr:<10.1f}% {avg:<+10.2f}%")
    
    logger.info(f"\n⏱️  耗时: {time.time() - start_time:.1f} 秒")
    
    # ─── 保存结果到JSON ───
    result = {
        'backtest_date': str(date.today()),
        'period': f'{start_date}~{end_date}',
        'stocks_count': len(filtered),
        'total_trades': len(all_trades),
        'win_trades': len(wins),
        'lose_trades': len(losses),
        'win_rate': round(win_rate, 2),
        'avg_win': round(avg_win, 2),
        'avg_loss': round(avg_loss, 2),
        'avg_profit': round(avg_profit, 2),
        'win_loss_ratio': round(win_loss_ratio, 2),
        'avg_hold_days': round(avg_days, 1),
        'max_drawdown': round(max_drawdown, 2),
        'period_stats': {k: {'trades': v['trades'], 'win_rate': round(v['wins']/v['trades']*100, 1) if v['trades'] else 0, 'avg_return': round(v['profit']/v['trades'], 2) if v['trades'] else 0} for k, v in period_stats.items()},
        'reason_stats': {k: {'trades': v['trades'], 'win_rate': round(v['wins']/v['trades']*100, 1) if v['trades'] else 0, 'avg_return': round(v['profit']/v['trades'], 2) if v['trades'] else 0} for k, v in reason_stats.items()},
    }
    
    with open('/tmp/ladder_backtest_result.json', 'w') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    logger.info(f"\n✅ 结果已保存到 /tmp/ladder_backtest_result.json")
    
    # 输出前20笔交易
    logger.info(f"\n📋 前20笔交易:")
    logger.info(f"{'─'*100}")
    for t in all_trades[:20]:
        logger.info(f"  {t['ts_code']:<12} {t['name']:<10} 买:{t['buy_date']} 卖:{t['sell_date']}  "
                    f"持有:{t['days_held']:>2}d  收益:{t['profit_pct']:>+7.2f}%  原因:{t['sell_reason']}")

if __name__ == '__main__':
    run_backtest()
