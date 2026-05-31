#!/usr/bin/env python3
"""
信号跟踪回测 v1.0 — 真实交易模拟
=====================================
每天根据评分信号动态买卖：
- BUY → 建仓（当天收盘买入）
- HOLD → 继续持有
- SELL → 平仓（当天收盘卖出），记录持有天数和收益
- 统计所有平仓记录的持有天数分布与胜率

最终回答：实际交易中到底持多少天胜率最高？
"""
import os, sys, time, pymysql, json
from db_config import get_connection
from datetime import datetime
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))



COST_RATE = 0.003  # 交易成本: 万3佣金+千1印花税

def main():
    conn = get_connection()
    cur = conn.cursor(pymysql.cursors.DictCursor)

    # 读取历史评分数据
    cur.execute("SELECT MIN(trade_date), MAX(trade_date) FROM backtest_score_daily")
    date_range = cur.fetchone()
    print(f"backtest_score_daily 日期范围: {date_range['MIN(trade_date)']} ~ {date_range['MAX(trade_date)']}")

    # 从 backtest_score_daily 读取全量历史评分
    cur.execute("""
        SELECT ts_code, trade_date, total_score
        FROM backtest_score_daily
        WHERE trade_date >= '2025-01-01'
        ORDER BY ts_code, trade_date
    """)
    rows = cur.fetchall()
    print(f"📋 读取历史评分数据: {len(rows)}条")

    # 按股票分组
    stock_data = defaultdict(list)
    for r in rows:
        stock_data[r['ts_code']].append(r)

    print(f"   涉及 {len(stock_data)} 只股票")

    # 获取K线价格
    stock_klines = {}
    for code in stock_data:
        cur.execute(
            "SELECT trade_date, close FROM daily_kline_qfq WHERE ts_code=%s ORDER BY trade_date ASC",
            (code,)
        )
        klines = cur.fetchall()
        if len(klines) > 60:
            stock_klines[code] = [(str(r['trade_date']), float(r['close'])) for r in klines]

    cur.close(); conn.close()
    print(f"   有K线数据: {len(stock_klines)} 只\n")

    # ═══════════════════════════════════════════
    # 信号跟踪回测引擎
    # ═══════════════════════════════════════════
    
    SIGNAL_MAP = {
        'V≥45': ('BUY', 45),
        'V≥40': ('CAUTIOUS_BUY', 40),
        'V≥30': ('HOLD', 30),
        'V<30': ('SELL', 0),
    }

    all_trades = []     # 所有平仓记录
    max_hold_days = 60  # 最长持有天数限制

    for code, scores in stock_data.items():
        if code not in stock_klines:
            continue
        
        scores.sort(key=lambda x: x['trade_date'])
        klines = stock_klines[code]
        
        # 生成每日信号序列
        dates = [str(s['trade_date']) for s in scores]
        scores_by_date = {str(s['trade_date']): s for s in scores}
        
        position = None  # 当前持仓: {'entry_date', 'entry_price', 'entry_idx'}
        trades = []      # 该股票的平仓记录
        
        for idx, (k_date, k_close) in enumerate(klines):
            if k_date not in scores_by_date:
                # 无评分日，如果持仓则继续持有
                if position:
                    # 检查是否超期
                    hold_days = idx - position['entry_idx']
                    if hold_days >= max_hold_days:
                        # 强制平仓
                        profit = (k_close - position['entry_price']) / position['entry_price']
                        trades.append({
                            'ts_code': code,
                            'entry_date': position['entry_date'],
                            'exit_date': k_date,
                            'hold_days': hold_days,
                            'profit_pct': round(profit * 100, 2),
                            'exit_reason': '强制平仓(60日)',
                        })
                        position = None
                continue
            
            s = scores_by_date[k_date]
            v = float(s.get('total_score', s.get('composite_score', 0)) or 0)
            
            # 信号判定
            if v >= 45:
                signal = 'BUY'
            elif v >= 40:
                signal = 'CAUTIOUS_BUY'
            elif v >= 30:
                signal = 'HOLD'
            else:
                signal = 'SELL'
            
            if signal in ('BUY', 'CAUTIOUS_BUY'):
                # 开仓/加仓
                if position is None:
                    position = {
                        'entry_date': k_date,
                        'entry_price': k_close,
                        'entry_idx': idx,
                    }
                # 已有仓位 → 继续持有（不重复开仓）
                
            elif signal == 'SELL':
                # 平仓
                if position:
                    hold_days = idx - position['entry_idx']
                    profit = (k_close - position['entry_price']) / position['entry_price']
                    trades.append({
                        'ts_code': code,
                        'entry_date': position['entry_date'],
                        'exit_date': k_date,
                        'hold_days': hold_days,
                        'profit_pct': round(profit * 100, 2),
                        'exit_reason': '信号卖出',
                    })
                    position = None
            
            elif signal == 'HOLD':
                # 持有中，不做操作
                if position:
                    hold_days = idx - position['entry_idx']
                    if hold_days >= max_hold_days:
                        profit = (k_close - position['entry_price']) / position['entry_price']
                        trades.append({
                            'ts_code': code,
                            'entry_date': position['entry_date'],
                            'exit_date': k_date,
                            'hold_days': hold_days,
                            'profit_pct': round(profit * 100, 2),
                            'exit_reason': '强制平仓(60日)',
                        })
                        position = None
        
        # 平掉最后仍持有的仓位
        if position:
            last_k_date = klines[-1][0]
            last_k_close = klines[-1][1]
            hold_days = len(klines) - 1 - position['entry_idx']
            profit = (last_k_close - position['entry_price']) / position['entry_price']
            trades.append({
                'ts_code': code,
                'entry_date': position['entry_date'],
                'exit_date': last_k_date,
                'hold_days': hold_days,
                'profit_pct': round(profit * 100, 2),
                'exit_reason': '期末平仓',
            })
        
        all_trades.extend(trades)

    # ═══════════════════════════════════════════
    # 报告输出
    # ═══════════════════════════════════════════
    
    print("=" * 90)
    print("📊 信号跟踪回测报告 v1.0 — 真实交易模拟")
    print(f"   生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"   总交易数: {len(all_trades)} 笔")
    print(f"   涉及股票: {len(stock_data)} 只")
    print("=" * 90)
    
    # 按持有天数分组统计胜率
    print(f"\n{'─'*90}")
    print(f"📈 持有天数 vs 胜率分布")
    print(f"{'─'*90}")
    print(f"  {'天数区间':>10s} {'交易数':>6s} {'胜数':>6s} {'败数':>6s} {'胜率':>8s} {'均收益':>10s} {'中位数收益':>12s}")
    print(f"  {'-'*60}")
    
    # 按天数分组
    day_groups = defaultdict(list)
    for t in all_trades:
        d = min(t['hold_days'], 60)
        if d <= 3:
            group = '1-3日'
        elif d <= 7:
            group = '4-7日'
        elif d <= 10:
            group = '8-10日'
        elif d <= 15:
            group = '11-15日'
        elif d <= 20:
            group = '16-20日'
        elif d <= 30:
            group = '21-30日'
        elif d <= 45:
            group = '31-45日'
        else:
            group = '46-60日'
        day_groups[group].append(t['profit_pct'])
    
    for group in ['1-3日','4-7日','8-10日','11-15日','16-20日','21-30日','31-45日','46-60日']:
        profits = day_groups.get(group, [])
        if not profits:
            continue
        wins = sum(1 for p in profits if p > 0)
        total = len(profits)
        avg = sum(profits) / total
        sorted_p = sorted(profits)
        median = sorted_p[total // 2]
        print(f"  {group:>10s} {total:>6d} {wins:>6d} {total-wins:>6d} {wins/total*100:>6.1f}% {avg:>+8.2f}% {median:>+10.2f}%")
    
    # 按精确天数统计
    print(f"\n{'─'*90}")
    print(f"📊 按精确持有天数统计（显示样本≥5的）")
    print(f"{'─'*90}")
    print(f"  {'天数':>6s} {'交易数':>6s} {'胜率':>8s} {'均收益':>10s}")
    print(f"  {'-'*34}")
    
    day_exact = defaultdict(list)
    for t in all_trades:
        day_exact[t['hold_days']].append(t['profit_pct'])
    
    for d in sorted(day_exact.keys()):
        profits = day_exact[d]
        if len(profits) < 5:
            continue
        wins = sum(1 for p in profits if p > 0)
        avg = sum(profits) / len(profits)
        print(f"  {d:>4d}日  {len(profits):>6d} {wins/len(profits)*100:>6.1f}% {avg:>+8.2f}%")
    
    # 总统计
    print(f"\n{'─'*90}")
    print(f"📋 总统计")
    print(f"{'─'*90}")
    total_wins = sum(1 for t in all_trades if t['profit_pct'] > 0)
    total_avg = sum(t['profit_pct'] for t in all_trades) / len(all_trades) if all_trades else 0
    total_median = sorted([t['profit_pct'] for t in all_trades])[len(all_trades)//2] if all_trades else 0
    avg_days = sum(t['hold_days'] for t in all_trades) / len(all_trades) if all_trades else 0
    print(f"  总交易: {len(all_trades)} 笔")
    print(f"  总胜率: {total_wins/len(all_trades)*100:.1f}%" if all_trades else "")
    print(f"  平均收益: {total_avg:+.2f}%" if all_trades else "")
    print(f"  中位数收益: {total_median:+.2f}%" if all_trades else "")
    print(f"  平均持有天数: {avg_days:.1f} 日" if all_trades else "")
    
    # 最佳持有天数（按胜率排序Top5）
    print(f"\n{'─'*90}")
    print(f"🏆 最佳持有天数 Top 5（按胜率排序, 样本≥5）")
    print(f"{'─'*90}")
    best = sorted([(d, profits) for d, profits in day_exact.items() if len(profits) >= 5],
                  key=lambda x: sum(1 for p in x[1] if p > 0) / len(x[1]), reverse=True)[:5]
    for d, profits in best:
        win_rate = sum(1 for p in profits if p > 0) / len(profits) * 100
        avg = sum(profits) / len(profits)
        print(f"  🥇 持有 {d:>2d} 日 — 胜率{win_rate:.1f}% 均收益{avg:+.2f}% 样本{len(profits)}笔")

if __name__ == '__main__':
    t0 = time.time()
    main()
    print(f"\n⏱ 耗时: {time.time()-t0:.1f}s")
