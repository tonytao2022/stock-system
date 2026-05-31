#!/usr/bin/env python3
"""
20日持仓胜率回测 — 基于 score_engine v4.0 + 最新K线数据
=========================================
回测56只回测池股票的评分信号，统计不同持有周期的胜率和收益
"""
import os, sys, pymysql, math
from db_config import get_connection
from datetime import datetime, date
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def main():
    conn = get_connection()
    cur = conn.cursor(pymysql.cursors.DictCursor)
    
    # 最新评分结果（从 trend_score 或 strategy_signal 取今日评分）
    cur.execute("""
        SELECT ts_code FROM backtest_pool WHERE status='ACTIVE' AND market!='指数'
    """)
    pool = [r['ts_code'] for r in cur.fetchall()]
    print(f"📋 回测池股票: {len(pool)}只")
    
    # 获取每日K线数据（前复权）
    stock_data = {}
    for code in pool:
        cur.execute(
            "SELECT trade_date, close FROM daily_kline_qfq WHERE ts_code=%s "
            "ORDER BY trade_date ASC LIMIT 500",
            (code,)
        )
        rows = cur.fetchall()
        if len(rows) >= 60:
            stock_data[code] = [(str(r['trade_date']), float(r['close'])) for r in rows]
    
    print(f"   有足够K线数据: {len(stock_data)}只\n")
    
    # 对每只股票，用评分引擎逐日评分 → 统计未来N日收益
    from score_engine import ScoreEngineV4
    engine = ScoreEngineV4()
    
    periods = [5, 10, 20, 30, 60]
    results = {p: [] for p in periods}  # 存储每笔信号的(score, fwd_return)
    
    scored = 0
    for i, code in enumerate(pool):
        if code not in stock_data:
            continue
        
        klines = stock_data[code]
        dates = [k[0] for k in klines]
        closes = [k[1] for k in klines]
        
        # 每条K线作为评分点（每隔10条取一个评分点，避免过度密集）
        step = max(1, len(klines) // 30)  # 每只约取30个评分点
        for j in range(20, len(klines) - max(periods), step):
            # 用至j日的数据做评分
            chunk = klines[:j+1]
            
            # 模拟 K线数据行格式
            mock_rows = []
            for kd in chunk:
                mock_rows.append({
                    'trade_date': kd[0],
                    'close': kd[1],
                    'high': kd[1] * 1.02,
                    'low': kd[1] * 0.98,
                    'vol': 1000000,
                    'change_pct': 0,
                })
            
            # 跑评分
            try:
                mkt = engine.get_market_context()
                result = engine.score_one(code, mkt)
                if 'error' in result:
                    continue
                vscore = result.get('v_score', 50)
            except:
                continue
            
            # 计算未来各周期收益
            for p in periods:
                if j + p < len(closes):
                    fwd_ret = (closes[j + p] - closes[j]) / closes[j]
                    results[p].append((vscore, fwd_ret))
        
        scored += 1
        if (i+1) % 10 == 0:
            print(f"  评分进度: {i+1}/{len(pool)} ({scored}只有效)")
    
    engine.close()
    cur.close(); conn.close()
    
    print(f"\n{'='*80}")
    print(f"📊 20日持仓胜率回测报告 v4.0")
    print(f"   评分引擎: score_engine.py v4.0")
    print(f"   回测时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"   有效评分股票: {scored}只")
    print(f"{'='*80}")
    
    # 统计每个周期的<strong>买入组(V分≥40) vs 卖出组(V分<20)的胜率和收益
    print(f"\n{'─'*80}")
    print(f"{'持有周期':>8s} {'总样本':>8s} {'买入组(V≥40)':>30s} {'卖出组(V<20)':>30s} {'多空利差':>12s}")
    print(f"{'':>8s} {'':>8s} {'数量':>6s} {'均收益':>8s} {'胜率':>7s} {'数量':>6s} {'均收益':>8s} {'胜率':>7s} {'':>12s}")
    print(f"{'─'*80}")
    
    best_period = None
    best_spread = -999
    
    for p in periods:
        data = results.get(p, [])
        if not data:
            continue
        
        buy = [(s, r) for s, r in data if s >= 40]
        sell = [(s, r) for s, r in data if s < 20]
        
        buy_avg = sum(r for _, r in buy) / len(buy) if buy else 0
        buy_win = sum(1 for _, r in buy if r > 0) / len(buy) * 100 if buy else 0
        sell_avg = sum(r for _, r in sell) / len(sell) if sell else 0
        sell_win = sum(1 for _, r in sell if r > 0) / len(sell) * 100 if sell else 0
        spread = (buy_avg - sell_avg) * 100
        
        print(f"  {p:>4d}日  {len(data):>8d}  {len(buy):>6d} {buy_avg*100:>+7.2f}% {buy_win:>5.1f}%  {len(sell):>6d} {sell_avg*100:>+7.2f}% {sell_win:>5.1f}%  {spread:>+10.2f}%")
        
        if spread > best_spread:
            best_spread = spread
            best_period = p
    
    print(f"{'─'*80}")
    print(f"🏆 最佳周期: {best_period}日 (多空利差 {best_spread:.2f}%)" if best_period else "⚠️ 数据不足")
    
    # 增加: 只看买入组的胜率曲线
    print(f"\n{'─'*80}")
    print(f"📈 买入组(V分≥40) 不同周期胜率对比")
    print(f"{'─'*80}")
    print(f"  {'周期':>8s} {'样本':>6s} {'均收益':>10s} {'胜率':>8s} {'中位数收益':>12s}")
    for p in periods:
        data = results.get(p, [])
        buy = [r for s, r in data if s >= 40]
        if buy:
            buy_sorted = sorted(buy)
            median = buy_sorted[len(buy)//2]
            avg = sum(buy) / len(buy)
            win = sum(1 for r in buy if r > 0) / len(buy) * 100
            print(f"  {p:>4d}日  {len(buy):>6d} {avg*100:>+9.2f}% {win:>6.1f}% {median*100:>+11.2f}%")

if __name__ == '__main__':
    t0 = __import__('time').time()
    main()
    print(f"\n⏱ 耗时: {__import__('time').time()-t0:.1f}s")
