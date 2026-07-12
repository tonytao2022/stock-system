#!/usr/bin/env python3
"""
M1评分 · 74分买入线 · 金字塔补仓 · 阶梯止盈 全回测
===================================================
评分源: bt_m1_score.m1_score (124天, 100918条)
买入线: 74分 (统一)
补仓: -8%触发 → 1份同仓位 → 均价-15%统一止损
止盈: +12%浮盈 → 出半仓 → 留半仓至全仓止损/止盈
"""
import sys, time, pymysql, numpy as np
sys.path.insert(0, '/opt/stock-analyzer')
import warnings
warnings.filterwarnings("ignore")

DB = {'host':'127.0.0.1','port':3306,'user':'debian-sys-maint',
      'password':'iXve1rVBXfdA4tL9','database':'stock_db_v2',
      'charset':'utf8mb4','connect_timeout':10,'read_timeout':300,'write_timeout':300,
      'autocommit':True,'cursorclass':pymysql.cursors.DictCursor}

START = '2026-01-05'
END = '2026-07-10'
BUY_THRESHOLD = 74          # 74分买入线
MAX_POSITIONS = 5           # 最多持5只
INITIAL_CAPITAL = 1000000   # 100万初资
COMMISSION = 0.0003         # 万三佣金
SLIPPAGE = 0.001            # 0.1%滑点

# 补仓参数
REPLENISH_TRIGGER = -0.08   # 首仓-8%触发补仓
REPLENISH_STOP_LOSS = -0.15 # 补仓后均价-15%全仓止损

# 阶梯止盈
TRAILING_ACTIVATE = 0.12    # 浮盈+12%激活止盈
TAKE_PROFIT_HALF = True     # 出半仓留半仓


def get_conn():
    return pymysql.connect(**DB)


def load_ohlcv(conn, code, start_dt, end_dt):
    """加载个股K线"""
    cur = conn.cursor()
    cur.execute("""
        SELECT trade_date, `open`, high, low, `close`
        FROM daily_kline WHERE ts_code=%s AND trade_date>=%s AND trade_date<=%s
        ORDER BY trade_date
    """, (code, start_dt, end_dt))
    rows = cur.fetchall()
    cur.close()
    return rows


def load_m1_scores(conn):
    """加载M1评分"""
    cur = conn.cursor()
    cur.execute("""
        SELECT ts_code, trade_date, m1_score as score
        FROM bt_m1_score
        WHERE trade_date>=%s AND trade_date<=%s AND m1_score IS NOT NULL
        ORDER BY trade_date, score DESC
    """, (START, END))
    rows = cur.fetchall(); cur.close()
    return rows


def main():
    t0 = time.time()
    conn = get_conn()

    print("⏳ 加载M1评分...")
    scores = load_m1_scores(conn)
    print(f"  M1: {len(scores)}条 ({len(set(r['trade_date'] for r in scores))}天)")

    # 按交易日分组
    day_scores = {}
    for r in scores:
        td = str(r['trade_date'])
        day_scores.setdefault(td, []).append({
            'ts_code': r['ts_code'],
            'score': float(r['score'])
        })

    all_dates = sorted(day_scores.keys())
    print(f"  交易日: {len(all_dates)}天 ({all_dates[0]} ~ {all_dates[-1]})")
    print(f"\n📐 参数:")
    print(f"  买入线: {BUY_THRESHOLD}分 | 最多持: {MAX_POSITIONS}只")
    print(f"  补仓: -{abs(REPLENISH_TRIGGER)*100:.0f}%触发 → 均价-{abs(REPLENISH_STOP_LOSS)*100:.0f}%止损")
    print(f"  止盈: +{TRAILING_ACTIVATE*100:.0f}%激活 → 出半仓")
    print(f"  滑点: {SLIPPAGE*100:.1f}% | 佣金: {COMMISSION*100:.2f}%")
    print()

    # 持仓管理
    # {code: {buy_date, avg_price, total_cost, shares, bought_scores, 
    #         replenished(是否已补), peak_price, half_exited(是否已出半仓)}}
    positions = {}
    trades = []  # (buy_date, sell_date, code, buy_price/avg, sell_price, pnl_pct, score, reason)
    capital = INITIAL_CAPITAL
    peak_capital = INITIAL_CAPITAL
    max_drawdown = 0.0
    daily_equity = []

    # K线缓存: {code: {td: {open,high,low,close}}}
    kline_cache = {}
    
    def get_close(code, td):
        """从缓存或加载获取收盘价"""
        if code not in kline_cache:
            rows = load_ohlcv(conn, code, START, END)
            kline_cache[code] = {str(r['trade_date']): {
                'open': float(r['open']), 'high': float(r['high']),
                'low': float(r['low']), 'close': float(r['close'])
            } for r in rows}
        return kline_cache[code].get(td)

    for di, td in enumerate(all_dates):
        if di % 20 == 0:
            pct = int((di+1)/len(all_dates)*100)
            print(f"  [{di+1}/{len(all_dates)} {pct}%] 📅 {td} 持{len(positions)}只 资{capital:>8.0f}", end='\r', flush=True)

        candidates = day_scores[td]
        
        # -- 现有持仓处理 --
        to_close = []
        for code, pos in list(positions.items()):
            kl = get_close(code, td)
            if not kl: continue
            price = kl['close']
            
            # 更新峰值
            if price > pos['peak_price']:
                pos['peak_price'] = price
            
            # 已出半仓的，检查全仓止盈或全仓止损
            if pos.get('half_exited'):
                # 已出半仓，从半仓价的峰值追踪
                # 检查是否跌破行权价
                current_pnl = (price - pos['avg_price']) / pos['avg_price']
                # 计算从半仓卖出后的峰值回撤
                if pos.get('half_peak', 0) > 0:
                    dd_from_peak = (pos['half_peak'] - price) / pos['half_peak']
                    if dd_from_peak >= 0.10:  # 从半仓后峰值回撤10%出剩余
                        to_close.append((code, price, '半仓后止盈'))
                        continue
                
                # 止盈止损检查
                if current_pnl <= REPLENISH_STOP_LOSS:
                    to_close.append((code, price, '全仓止损(半仓后)'))
                continue
            
            # 未出半仓：检查补仓 → 止损 → 止盈
            
            # 1) 检查补仓
            if not pos.get('replenished'):
                buy_price = pos['first_buy_price']
                pnl_from_buy = (price - buy_price) / buy_price
                
                if pnl_from_buy <= REPLENISH_TRIGGER:
                    # 检查当日评分是否仍 ≥ 买入线
                    day_candidate = [c for c in candidates if c['ts_code'] == code]
                    if day_candidate and day_candidate[0]['score'] >= BUY_THRESHOLD:
                        # 补1份（等量）
                        alloc = capital * 0.5  # 最多用50%剩余资金
                        add_shares = int(alloc / price / 100) * 100
                        if add_shares >= 100:
                            cost = add_shares * price * COMMISSION
                            capital -= add_shares * price + cost
                            
                            new_shares = pos['shares'] + add_shares
                            pos['avg_price'] = (pos['avg_price'] * pos['shares'] + price * add_shares) / new_shares
                            pos['total_cost'] += add_shares * price + cost
                            pos['shares'] = new_shares
                            pos['replenished'] = True
                            trades.append((td, td, code, buy_price, price, 
                                         0, day_candidate[0]['score'], '补仓'))
            
            # 2) 补仓后统一止损检查（从均价）
            pnl_from_avg = (price - pos['avg_price']) / pos['avg_price']
            if pnl_from_avg <= REPLENISH_STOP_LOSS:
                to_close.append((code, price, '均价止损'))
                continue
            
            # 3) 阶梯止盈检查
            if pos.get('replenished'):
                # 补仓后的止盈：从均价+12%激活 → 出半仓
                pnl_from_avg = (price - pos['avg_price']) / pos['avg_price']
                if pnl_from_avg >= TRAILING_ACTIVATE:
                    # 出半仓
                    half_shares = pos['shares'] // 2
                    if half_shares >= 100:
                        sell_value = half_shares * price
                        commission_cost = sell_value * COMMISSION
                        capital += sell_value - commission_cost
                        pos['shares'] -= half_shares
                        pos['total_cost'] = pos['avg_price'] * pos['shares']
                        pos['half_exited'] = True
                        pos['half_peak'] = price  # 记录半仓时价格
                        pos['half_date'] = td
                        trades.append((td, td, code, pos['avg_price'], price,
                                     0, 0, '半仓止盈'))
                        continue
            else:
                # 未补仓的止盈：+12%出半仓
                pnl_from_buy = (price - pos['first_buy_price']) / pos['first_buy_price']
                if pnl_from_buy >= TRAILING_ACTIVATE:
                    half_shares = pos['shares'] // 2
                    if half_shares >= 100:
                        sell_value = half_shares * price
                        commission_cost = sell_value * COMMISSION
                        capital += sell_value - commission_cost
                        pos['shares'] -= half_shares
                        pos['total_cost'] = pos['avg_price'] * pos['shares']
                        pos['half_exited'] = True
                        pos['half_peak'] = price
                        pos['half_date'] = td
                        trades.append((td, td, code, pos['first_buy_price'], price,
                                     0, 0, '半仓止盈'))
                        continue
            
            # 4) 常规到期检查（最长60日）
            if pos.get('hold_days', 0) >= 60:
                to_close.append((code, price, '到期60日'))
                continue
            
            pos['hold_days'] = pos.get('hold_days', 0) + 1

        # 执行卖出
        for code, sell_price, reason in to_close:
            pos = positions.pop(code)
            sell_value = pos['shares'] * sell_price
            commission_cost = sell_value * COMMISSION
            realized_pnl = sell_value - pos['total_cost'] - commission_cost
            pnl_pct = (sell_value - pos['total_cost']) / pos['total_cost'] * 100
            capital += sell_value - commission_cost
            trades.append((
                pos['buy_date'], td, code,
                pos['first_buy_price'], sell_price,
                round(pnl_pct, 2),
                pos.get('buy_score', 0), reason
            ))

        # -- 买入 --
        if len(positions) < MAX_POSITIONS:
            slots = MAX_POSITIONS - len(positions)
            held_codes = set(positions.keys())
            
            buy_list = [c for c in candidates 
                        if c['score'] >= BUY_THRESHOLD 
                        and c['ts_code'] not in held_codes]
            buy_list.sort(key=lambda x: x['score'], reverse=True)
            
            for cand in buy_list[:slots]:
                code = cand['ts_code']
                kl = get_close(code, td)
                if not kl: continue
                price = kl['close']
                if price <= 0: continue
                
                # 等权分配
                alloc = capital / (slots - buy_list.index(cand) if slots > 1 else 1)
                shares = int(alloc / price / 100) * 100
                if shares < 100: continue
                
                cost = shares * price * COMMISSION
                capital -= shares * price + cost
                
                positions[code] = {
                    'buy_date': td,
                    'first_buy_price': price,
                    'avg_price': price,
                    'total_cost': shares * price + cost,
                    'shares': shares,
                    'buy_score': round(cand['score'], 1),
                    'peak_price': price,
                    'replenished': False,
                    'half_exited': False,
                    'hold_days': 0
                }

        # 每日净值
        total_value = capital
        for code, pos in positions.items():
            kl = get_close(code, td)
            if kl:
                total_value += pos['shares'] * kl['close']
        daily_equity.append((td, total_value))
        
        dd = (peak_capital - total_value) / peak_capital * 100 if peak_capital > 0 else 0
        if dd > max_drawdown:
            max_drawdown = dd
        if total_value > peak_capital:
            peak_capital = total_value

    # 收盘前强平
    last_td = all_dates[-1]
    for code, pos in list(positions.items()):
        kl = get_close(code, last_td)
        if kl:
            sell_price = kl['close']
            sell_value = pos['shares'] * sell_price
            commission_cost = sell_value * COMMISSION
            pnl_pct = (sell_value - pos['total_cost']) / pos['total_cost'] * 100
            capital += sell_value - commission_cost
            half = '是' if pos.get('half_exited') else '否'
            trades.append((
                pos['buy_date'], last_td, code,
                pos['first_buy_price'], sell_price,
                round(pnl_pct, 2), pos.get('buy_score', 0),
                f'强平(补{pos.get("replenished")}半{half})'
            ))

    # 统计
    total_return = (capital - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100
    
    # 按原始交易（非补仓/止盈）统计胜率
    real_trades = [t for t in trades if t[1] != t[0]]  # 排除同一天的补仓/止盈
    win_trades = [t for t in real_trades if t[5] > 0]
    loss_trades = [t for t in real_trades if t[5] <= 0]
    
    win_rate = len(win_trades) / len(real_trades) * 100 if real_trades else 0
    avg_win = np.mean([t[5] for t in win_trades]) if win_trades else 0
    avg_loss = np.mean([t[5] for t in loss_trades]) if loss_trades else 0
    profit_factor = abs(sum(t[5] for t in win_trades) / sum(t[5] for t in loss_trades)) if loss_trades and sum(t[5] for t in loss_trades) != 0 else float('inf')
    
    # 补仓统计
    replenish_trades = [t for t in trades if '补仓' in t[7]]
    half_exit_trades = [t for t in trades if '半仓' in t[7]]
    
    print(f"\n\n{'='*55}")
    print(f"  📊 M1 · 74分线 · 金字塔补仓 · 阶梯止盈")
    print(f"  {START} ~ {END} ({len(all_dates)}个交易日)")
    print(f"{'='*55}")
    print(f"  总收益率:      {total_return:>+8.2f}%")
    print(f"  最大回撤:      {max_drawdown:>7.2f}%")
    print(f"  最终资金:      {capital:>10.0f}")
    print(f"  {'─'*42}")
    print(f"  交易次数:      {len(real_trades):>5}笔")
    print(f"  胜率:          {win_rate:>5.1f}%")
    print(f"  平均盈利:      {avg_win:>+7.2f}%")
    print(f"  平均亏损:      {avg_loss:>7.2f}%")
    print(f"  盈亏比:        {abs(avg_win/avg_loss):>5.2f}" if avg_loss != 0 else "  盈亏比:    ∞")
    print(f"  盈利因子:      {profit_factor:>5.2f}")
    print(f"  {'─'*42}")
    print(f"  补仓执行:      {len(replenish_trades):>5}次")
    print(f"  半仓止盈:      {len(half_exit_trades):>5}次")
    
    print(f"\n  前15笔主交易:")
    print(f"  {'买入':>10} {'卖出':>10} {'代码':>10} {'收益':>7} {'原因':>14}")
    print(f"  {'-'*55}")
    for t in real_trades[:15]:
        print(f"  {t[0]:>10} {t[1]:>10} {t[2]:>10} {t[5]:>+6.1f}% {t[7]:>12}")
    
    elapsed = time.time() - t0
    print(f"\n  ⏱ 耗时: {elapsed:.0f}s")
    
    conn.close()


if __name__ == '__main__':
    main()
