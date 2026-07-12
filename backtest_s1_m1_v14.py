#!/usr/bin/env python3
"""
S1 vs M1 vs V14 全对比回测
=========================
使用bt_s1_score + bt_m1_score表做独立回测对比

三套评分系统：
1. S1（短期Alpha）：买评分≥70，持仓20日
2. M1（中期复合）：买评分≥70，持仓20日
3. V14（混合）：S1×0.2 + M1×0.8，买评分≥70，持仓20日
"""
import sys, time, pymysql
import numpy as np
import warnings
warnings.filterwarnings("ignore")

DB = {'host':'127.0.0.1','port':3306,'user':'debian-sys-maint',
      'password':'iXve1rVBXfdA4tL9','database':'stock_db_v2',
      'charset':'utf8mb4','connect_timeout':10,'read_timeout':300,'write_timeout':300,
      'autocommit':True,'cursorclass':pymysql.cursors.DictCursor}

START = '2026-01-05'
END = '2026-07-10'

# ========== 回测参数 ==========
HOLDING_DAYS = 20          # 固定持有期
BUY_THRESHOLD = 70         # 买入线
SLIPPAGE = 0.001           # 滑点0.1%
COMMISSION_RATE = 0.0003   # 佣金万三
STOP_LOSS = -0.08          # -8%止损
INITIAL_CAPITAL = 1000000  # 100万
MAX_POSITIONS = 5          # 最多同时持5只

# V14混合权重
V14_W1 = 0.20  # S1权重
V14_W2 = 0.80  # M1权重


def get_conn():
    return pymysql.connect(**DB)


def load_ohlcv(conn, ts_code, start_date, end_date):
    """加载个股K线"""
    cur = conn.cursor()
    cur.execute("""
        SELECT trade_date, `close`, high, low
        FROM daily_kline
        WHERE ts_code=%s AND trade_date>=%s AND trade_date<=%s
        ORDER BY trade_date
    """, (ts_code, start_date, end_date))
    rows = cur.fetchall()
    cur.close()
    return rows


def load_all_scores(conn, score_type='s1'):
    """加载回测评分数据"""
    cur = conn.cursor()
    if score_type == 's1':
        cur.execute("""
            SELECT ts_code, trade_date, s1_score as score
            FROM bt_s1_score
            WHERE trade_date>=%s AND trade_date<=%s AND s1_score IS NOT NULL
            ORDER BY trade_date, score DESC
        """, (START, END))
    elif score_type == 'm1':
        cur.execute("""
            SELECT ts_code, trade_date, m1_score as score
            FROM bt_m1_score
            WHERE trade_date>=%s AND trade_date<=%s AND m1_score IS NOT NULL
            ORDER BY trade_date, score DESC
        """, (START, END))
    rows = cur.fetchall()
    cur.close()
    return rows


def load_v14_scores(conn):
    """加载V14混合评分"""
    cur = conn.cursor()
    query = """
        SELECT s1.ts_code, s1.trade_date,
               (s1.s1_score * %f + COALESCE(m1.m1_score, 50) * %f) as score
        FROM bt_s1_score s1
        LEFT JOIN bt_m1_score m1 ON s1.ts_code=m1.ts_code AND s1.trade_date=m1.trade_date
        WHERE s1.trade_date>=%%s AND s1.trade_date<=%%s
        ORDER BY s1.trade_date, score DESC
    """ % (V14_W1, V14_W2)
    cur.execute(query, (START, END))
    rows = cur.fetchall()
    cur.close()
    return rows


def run_backtest(conn, scores, label):
    """
    通用回测引擎
    scores: list of dict{ts_code, trade_date, score}
    """
    t0 = time.time()
    
    # 按交易日分组评分
    day_scores = {}
    for r in scores:
        td = str(r['trade_date'])
        if td not in day_scores:
            day_scores[td] = []
        day_scores[td].append({
            'ts_code': r['ts_code'],
            'score': float(r['score'])
        })
    
    # 排序交易日
    all_dates = sorted(day_scores.keys())
    if not all_dates:
        return None
    
    trade_dates_set = set(all_dates)
    
    # 持仓管理
    # {ts_code: {buy_date, buy_price, shares, buy_score, holding_days}}
    positions = {}
    
    # 交易记录
    trades = []  # [(buy_date, sell_date, ts_code, buy_price, sell_price, pnl_pct, score)]
    capital = INITIAL_CAPITAL
    peak_capital = INITIAL_CAPITAL
    max_drawdown = 0
    daily_capital = []
    
    for di, td in enumerate(all_dates):
        day_candidates = day_scores[td]
        
        # --- 检查已有持仓（用当日收盘价评估）---
        to_close = []
        for code, pos in list(positions.items()):
            pos['holding_days'] += 1
            
            # 获取当日收盘价
            ohlcv = load_ohlcv(conn, code, td, td)
            if not ohlcv:
                continue
            close_price = float(ohlcv[0]['close'])
            
            # 检查是否到期
            if pos['holding_days'] >= HOLDING_DAYS:
                to_close.append((code, close_price, '到期'))
                continue
            
            # 止损检查
            pnl_pct = (close_price - pos['buy_price']) / pos['buy_price']
            if pnl_pct <= STOP_LOSS:
                to_close.append((code, close_price, '止损'))
                continue
        
        # 执行卖出
        for code, sell_price, reason in to_close:
            pos = positions.pop(code)
            pnl_pct = (sell_price - pos['buy_price']) / pos['buy_price'] - COMMISSION_RATE
            pnl = pos['shares'] * (sell_price - pos['buy_price']) - pos['shares'] * sell_price * COMMISSION_RATE - pos['cost']
            capital += pos['shares'] * sell_price
            capital -= pos['shares'] * sell_price * COMMISSION_RATE
            
            trades.append((
                pos['buy_date'], td, code,
                pos['buy_price'], sell_price,
                round(pnl_pct * 100, 2),
                pos['buy_score'], reason
            ))
        
        # --- 买入逻辑 ---
        if len(positions) < MAX_POSITIONS:
            # 取当日评分≥阈值且不在持仓中的
            candidates = [c for c in day_candidates 
                         if c['score'] >= BUY_THRESHOLD
                         and c['ts_code'] not in positions]
            
            # 按评分取前N名
            candidates.sort(key=lambda x: x['score'], reverse=True)
            slots = MAX_POSITIONS - len(positions)
            
            for cand in candidates[:slots]:
                code = cand['ts_code']
                # 获取当日开盘价
                ohlcv = load_ohlcv(conn, code, td, td)
                if not ohlcv:
                    continue
                buy_price = float(ohlcv[0]['close'])  # 用收盘作为买入价（实际回测保守处理）
                if buy_price <= 0:
                    continue
                
                # 等权分配资金
                alloc = capital / (slots - candidates.index(cand) if slots > 1 else 1)
                shares = int(alloc / buy_price / 100) * 100
                if shares < 100:
                    continue
                
                cost = shares * buy_price * COMMISSION_RATE
                capital -= shares * buy_price + cost
                
                positions[code] = {
                    'buy_date': td,
                    'buy_price': buy_price,
                    'shares': shares,
                    'cost': cost,
                    'buy_score': round(cand['score'], 1),
                    'holding_days': 0
                }
        
        # 每日净值
        total_value = capital
        for code, pos in positions.items():
            ohlcv = load_ohlcv(conn, code, td, td)
            if ohlcv:
                total_value += pos['shares'] * float(ohlcv[0]['close'])
        daily_capital.append((td, total_value))
        
        dd = (peak_capital - total_value) / peak_capital * 100 if peak_capital > 0 else 0
        max_drawdown = max(max_drawdown, dd)
        if total_value > peak_capital:
            peak_capital = total_value
    
    # 收盘前强平剩余持仓
    for code, pos in list(positions.items()):
        ohlcv = load_ohlcv(conn, code, all_dates[-1], all_dates[-1])
        if ohlcv:
            sell_price = float(ohlcv[0]['close'])
            pnl_pct = (sell_price - pos['buy_price']) / pos['buy_price'] - COMMISSION_RATE
            capital += pos['shares'] * sell_price
            capital -= pos['shares'] * sell_price * COMMISSION_RATE
            trades.append((
                pos['buy_date'], all_dates[-1], code,
                pos['buy_price'], sell_price,
                round(pnl_pct * 100, 2),
                pos['buy_score'], '到期强平'
            ))
    
    # 统计
    total_return = (capital - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100
    win_trades = [t for t in trades if t[5] > 0]
    loss_trades = [t for t in trades if t[5] <= 0]
    
    win_rate = len(win_trades) / len(trades) * 100 if trades else 0
    avg_win = np.mean([t[5] for t in win_trades]) if win_trades else 0
    avg_loss = np.mean([t[5] for t in loss_trades]) if loss_trades else 0
    profit_factor = abs(sum(t[5] for t in win_trades) / sum(t[5] for t in loss_trades)) if loss_trades and sum(t[5] for t in loss_trades) != 0 else float('inf')
    
    elapsed = time.time() - t0
    
    return {
        'label': label,
        '总收益率': round(total_return, 2),
        '最大回撤': round(max_drawdown, 2),
        '交易次数': len(trades),
        '胜率': round(win_rate, 1),
        '平均盈利': round(avg_win, 2) if win_trades else 0,
        '平均亏损': round(avg_loss, 2) if loss_trades else 0,
        '盈亏比': round(abs(avg_win / avg_loss), 2) if avg_loss != 0 else float('inf'),
        '盈利因子': round(profit_factor, 2),
        '最终资金': round(capital, 2),
        '用时': round(elapsed, 1),
        'trades': trades[:50]  # 最多取前50笔
    }


def print_report(result):
    if not result:
        print("  ❌ 无结果")
        return
    print(f"\n{'='*55}")
    print(f"  📊 {result['label']}")
    print(f"{'='*55}")
    print(f"  总收益率:      {result['总收益率']:>+.2f}%")
    print(f"  最大回撤:      -{result['最大回撤']:.2f}%")
    print(f"  交易次数:      {result['交易次数']}")
    print(f"  胜率:          {result['胜率']:.1f}%")
    print(f"  平均盈利:      {result['平均盈利']:>+.2f}%")
    print(f"  平均亏损:      {result['平均亏损']:.2f}%")
    print(f"  盈亏比:        {result['盈亏比']:.2f}")
    print(f"  盈利因子:      {result['盈利因子']:.2f}")
    print(f"  最终资金:      {result['最终资金']:>10.0f}")
    print(f"  用时:          {result['用时']:.1f}s")
    
    if result.get('trades'):
        print(f"\n  前10笔交易:")
        print(f"  {'买入':>10} {'卖出':>10} {'代码':>10} {'收益':>6}")
        print(f"  {'-'*42}")
        for t in result['trades'][:10]:
            print(f"  {t[0]:>10} {t[1]:>10} {t[2]:>10} {t[5]:>+6.1f}% {t[7]}")


def main():
    t0 = time.time()
    conn = get_conn()
    
    print(f"📅 回测区间: {START} ~ {END}")
    print(f"📐 买入线={BUY_THRESHOLD} 持有期={HOLDING_DAYS}日 止损={STOP_LOSS*100:.0f}% 最大持仓={MAX_POSITIONS}只")
    print(f"💰 初始资金: {INITIAL_CAPITAL:,.0f}")
    print(f"⚡ V14权重: S1×{V14_W1} + M1×{V14_W2}")
    print()
    
    # 加载评分数据
    print("⏳ 加载S1评分...")
    s1_scores = load_all_scores(conn, 's1')
    print(f"  S1: {len(s1_scores)}条评分记录 ({len(set(r['trade_date'] for r in s1_scores))}天)")
    
    print("⏳ 加载M1评分...")
    m1_scores = load_all_scores(conn, 'm1')
    print(f"  M1: {len(m1_scores)}条评分记录 ({len(set(r['trade_date'] for r in m1_scores))}天)")
    
    print("⏳ 计算V14混合评分...")
    v14_scores = load_v14_scores(conn)
    print(f"  V14: {len(v14_scores)}条评分记录 ({len(set(r['trade_date'] for r in v14_scores))}天)")
    
    # 回测S1
    print("\n🔬 S1回测...")
    r_s1 = run_backtest(conn, s1_scores, 'S1 (短期Alpha)')
    print_report(r_s1)
    
    # 回测M1
    print("\n🔬 M1回测...")
    r_m1 = run_backtest(conn, m1_scores, 'M1 (中期复合)')
    print_report(r_m1)
    
    # 回测V14
    print("\n🔬 V14混合回测...")
    r_v14 = run_backtest(conn, v14_scores, 'V14 (S1×0.2+M1×0.8)')
    print_report(r_v14)
    
    # 总结对比
    print(f"\n{'='*55}")
    print(f"  📋 三方案对比总结")
    print(f"{'='*55}")
    print(f"  {'指标':<16} {'S1':>12} {'M1':>12} {'V14':>12}")
    print(f"  {'-'*52}")
    rows_data = [
        ('总收益率%', r_s1['总收益率'], r_m1['总收益率'], r_v14['总收益率']),
        ('最大回撤%', f"-{r_s1['最大回撤']}", f"-{r_m1['最大回撤']}", f"-{r_v14['最大回撤']}"),
        ('交易次数', r_s1['交易次数'], r_m1['交易次数'], r_v14['交易次数']),
        ('胜率%', r_s1['胜率'], r_m1['胜率'], r_v14['胜率']),
        ('盈亏比', r_s1['盈亏比'], r_m1['盈亏比'], r_v14['盈亏比']),
        ('盈利因子', r_s1['盈利因子'], r_m1['盈利因子'], r_v14['盈利因子']),
    ]
    for name, *vals in rows_data:
        print(f"  {name:<16} {str(vals[0]):>12} {str(vals[1]):>12} {str(vals[2]):>12}")
    
    print(f"\n  ⏱ 总耗时: {time.time()-t0:.0f}s")
    conn.close()


if __name__ == '__main__':
    main()
