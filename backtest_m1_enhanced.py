#!/usr/bin/env python3
"""
M1增强版回测 — 支持alpha062权重和alpha046门控配置
===================================================
评分源: bt_m1_score.m1_score + strategy_signal.alpha062_score + alpha046_score
"""
import os, sys, time, math, json
import numpy as np
sys.path.insert(0, '/opt/stock-analyzer')
os.environ['MYSQL_PWD'] = 'iXve1rVBXfdA4tL9'

import pymysql
conn = pymysql.connect(host='localhost', user='debian-sys-maint', password='iXve1rVBXfdA4tL9', database='stock_db_v2')
cur = conn.cursor(pymysql.cursors.DictCursor)

# ======= 参数 =======
START = '2025-01-02'
END = '2026-07-10'
INITIAL_CAPITAL = 1000000
MAX_POSITIONS = 5
BUY_THRESHOLD = 74
OVERBOUGHT_LIMIT = 0.10  # 单票10%上限（直接用M1选股不设总仓）

# alpha因子配置
ALPHA062_WEIGHT = 0.0    # 0~0.3
USE_ALPHA046_GATE = False
ALPHA046_GATE_MIN = 30   # alpha046低于此值不买

# 季节参数矩阵
SEASON_PARAMS = {
    'summer':       {'buy':74,'hold':30,'sl1':-12,'sl2':-9,'tp':18,'single':0.50,'total':0.50},
    'spring':       {'buy':74,'hold':30,'sl1':-12,'sl2':-9,'tp':15,'single':0.35,'total':0.40},
    'weak_spring':  {'buy':74,'hold':25,'sl1':-11,'sl2':-8,'tp':15,'single':0.35,'total':0.40},
    'chaos_spring': {'buy':74,'hold':25,'sl1':-11,'sl2':-8,'tp':15,'single':0.20,'total':0.35},
    'chaos':        {'buy':74,'hold':25,'sl1':-10,'sl2':-8,'tp':12,'single':0.20,'total':0.30},
    'chaos_autumn': {'buy':74,'hold':20,'sl1': -8,'sl2':-6,'tp':10,'single':0.15,'total':0.20},
    'weak_autumn':  {'buy':74,'hold':20,'sl1': -8,'sl2':-6,'tp':12,'single':0.20,'total':0.25},
    'autumn':       {'buy':74,'hold':20,'sl1':-10,'sl2':-8,'tp':12,'single':0.30,'total':0.35},
    'winter':       {'buy':74,'hold':10,'sl1': -5,'sl2':-4,'tp': 8,'single':0.05,'total':0.10},
}

# 补仓/止盈
REPLENISH_TRIGGER = -0.08
REPLENISH_STOP = -0.15
TRAILING_ACTIVATE = 0.12  # +12%触发半仓止盈

def run_backtest(label):
    t0 = time.time()
    
    # 加载M1评分
    cur.execute("""
        SELECT ts_code, trade_date, m1_score 
        FROM bt_m1_score 
        WHERE trade_date>=%s AND trade_date<=%s AND m1_score IS NOT NULL
        ORDER BY trade_date, m1_score DESC
    """, (START, END))
    m1_rows = cur.fetchall()
    
    # 如果有alpha062权重>0或alpha046门控，加载因子数据
    alpha_scores = {}
    if ALPHA062_WEIGHT > 0 or USE_ALPHA046_GATE:
        cur.execute("""
            SELECT ts_code, trade_date, alpha062_score, alpha046_score
            FROM strategy_signal
            WHERE trade_date>=%s AND trade_date<=%s
              AND alpha062_score IS NOT NULL
        """, (START, END))
        for r in cur.fetchall():
            key = (r['ts_code'], str(r['trade_date']))
            alpha_scores[key] = {'a062': float(r['alpha062_score'] or 50), 'a046': float(r['alpha046_score'] or 50)}
    
    # 加载季节判定
    cur.execute("""
        SELECT trade_date, season FROM season_daily_signal 
        WHERE trade_date>=%s AND trade_date<=%s
        ORDER BY trade_date
    """, (START, END))
    season_map = {}
    for r in cur.fetchall():
        season_map[str(r['trade_date'])] = r['season'] or 'chaos'
    
    # 按天分组M1评分
    day_scores = {}
    for r in m1_rows:
        td = str(r['trade_date'])
        code = r['ts_code']
        m1 = float(r['m1_score'])
        
        # 应用alpha062权重修正
        if ALPHA062_WEIGHT > 0:
            sk = (code, td)
            if sk in alpha_scores:
                a062 = alpha_scores[sk]['a062']
                m1 = m1 * (1 - ALPHA062_WEIGHT) + a062 * ALPHA062_WEIGHT
        
        # alpha046门控
        if USE_ALPHA046_GATE:
            sk = (code, td)
            if sk in alpha_scores:
                a046 = alpha_scores[sk]['a046']
                if a046 < ALPHA046_GATE_MIN:
                    continue  # 门控拦截，不纳入候选
        
        day_scores.setdefault(td, []).append({'ts_code': code, 'score': m1})
    
    # 所有交易日
    trade_dates = sorted(day_scores.keys())
    print(f"  📅 交易日: {len(trade_dates)}天 ({trade_dates[0]} ~ {trade_dates[-1]})")
    
    # ====== 回测主循环 ======
    capital = INITIAL_CAPITAL
    peak_capital = INITIAL_CAPITAL
    max_drawdown = 0
    
    # 持仓: {ts_code: {entry_price, avg_price, shares, buy_date, score, bought_qty}}
    positions = {}
    # 交易记录
    trades = []
    # 补仓次数统计
    replenish_count = 0
    step_profit_count = 0
    
    # 加载OHLCV
    ohlcv_cache = {}
    
    def get_price(code, td, field='close'):
        key = (code, td)
        if key not in ohlcv_cache:
            cur.execute("SELECT `close`, high, low FROM daily_kline WHERE ts_code=%s AND trade_date=%s", (code, td))
            r = cur.fetchone()
            ohlcv_cache[key] = r if r else None
        r2 = ohlcv_cache[key]
        if not r2: return None
        return float(r2.get(field if field in r2 else 'close', r2['close']))
    
    for di, td in enumerate(trade_dates):
        season = season_map.get(td, 'chaos')
        params = SEASON_PARAMS.get(season, SEASON_PARAMS['chaos'])
        buy_th = params['buy']
        hold_period = params['hold']
        sl1 = params['sl1'] / 100
        sl2 = params['sl2'] / 100
        tp = params['tp'] / 100
        single_limit = params['single']
        total_limit = params['total']
        
        peak_capital = max(peak_capital, capital)
        dd = (peak_capital - capital) / peak_capital * 100
        max_drawdown = max(max_drawdown, dd)
        
        # 检查现有持仓
        close_positions = []
        for code, pos in list(positions.items()):
            cur_price = get_price(code, td)
            if cur_price is None: continue
            days_held = di - pos['buy_di']
            cost = pos['avg_price']
            pnl = (cur_price - cost) / cost
            
            # 阶梯止损
            if days_held <= 5 and pnl <= sl1 * 1.5:
                close_positions.append((code, cur_price, '止损T1'))
            elif pnl <= sl2:
                close_positions.append((code, cur_price, '止损T2'))
            elif days_held >= hold_period:
                close_positions.append((code, cur_price, '到期'))
            
            # 半仓止盈
            if pnl >= TRAILING_ACTIVATE and code in positions and not pos.get('half_closed', False):
                half_shares = pos['shares'] // 2
                if half_shares > 0:
                    profit = half_shares * cur_price - half_shares * cost
                    capital += half_shares * cur_price
                    pos['shares'] -= half_shares
                    pos['half_closed'] = True
                    step_profit_count += 1
            
            # 补仓检查
            if pnl <= REPLENISH_TRIGGER and not pos.get('replenished', False):
                if di < len(trade_dates):
                    check_td = trade_dates[min(di+1, len(trade_dates)-1)]
                    day_cands = day_scores.get(check_td, [])
                    if any(c['ts_code'] == code and c['score'] >= buy_th for c in day_cands):
                        replenish_qty = pos['shares']
                        cost2 = replenish_qty * cur_price
                        if cost2 <= capital:
                            capital -= cur_price * replenish_qty
                            pos['avg_price'] = (pos['avg_price'] * pos['shares'] + cur_price * replenish_qty) / (pos['shares'] + replenish_qty)
                            pos['shares'] += replenish_qty
                            pos['replenished'] = True
                            replenish_count += 1
            
            # 平均价止损
            if pos.get('replenished', False):
                pnl_from_avg = (cur_price - pos['avg_price']) / pos['avg_price']
                if pnl_from_avg <= REPLENISH_STOP:
                    close_positions.append((code, cur_price, '均价止损'))
        
        for code, price, reason in close_positions:
            pos = positions.pop(code, None)
            if pos:
                profit = pos['shares'] * (price - pos['avg_price'])
                pnl_pct = (price - pos['avg_price']) / pos['avg_price'] * 100
                capital += pos['shares'] * price
                trades.append({
                    'code': code, 'buy_date': pos['buy_date'], 'sell_date': td,
                    'days_held': di - pos['buy_di'],
                    'buy_price': pos['avg_price'], 'sell_price': price,
                    'pnl_pct': round(pnl_pct, 2), 'reason': reason
                })
        
        # ====== 买入信号 ======
        buy_cash = capital
        available_positions = MAX_POSITIONS - len(positions)
        if available_positions > 0 and buy_cash > 10000:
            # 按评分从高到选
            daily_scores = day_scores.get(td, [])
            remaining_codes = [s for s in daily_scores if s['ts_code'] not in positions and s['score'] >= buy_th]
            
            for cand in remaining_codes[:available_positions]:
                code = cand['ts_code']
                entry_price = get_price(code, td)
                if entry_price is None or entry_price <= 0:
                    continue
                
                position_value = min(buy_cash * single_limit, entry_price * 1000000)  # 每只100万
                qty = int(position_value / entry_price / 100) * 100
                if qty < 100: qty = 100
                cost = qty * entry_price
                
                if cost <= 0 or cost > buy_cash * 0.5:
                    continue
                
                capital -= cost
                positions[code] = {
                    'shares': qty, 'avg_price': entry_price, 'entry_price': entry_price,
                    'buy_date': td, 'buy_di': di, 'score': cand['score'],
                    'half_closed': False, 'replenished': False
                }
    
    # 结尾平仓
    if positions:
        last_td = trade_dates[-1] if len(trade_dates) >= 2 else trade_dates[0]
        for code, pos in list(positions.items()):
            price = get_price(code, last_td)
            if price is None: price = pos['avg_price']
            profit = pos['shares'] * (price - pos['avg_price'])
            pnl_pct = (price - pos['avg_price']) / pos['avg_price'] * 100
            capital += pos['shares'] * price
            trades.append({
                'code': code, 'buy_date': pos['buy_date'], 'sell_date': last_td,
                'days_held': len(trade_dates) - pos['buy_di'],
                'buy_price': pos['avg_price'], 'sell_price': price,
                'pnl_pct': round(pnl_pct, 2), 'reason': '期末'
            })
        positions.clear()
    
    # ====== 统计 ======
    total_return = (capital - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100
    max_dd = max(max_drawdown, 0)
    
    win_trades = [t for t in trades if t['pnl_pct'] > 0]
    lose_trades = [t for t in trades if t['pnl_pct'] <= 0]
    win_rate = len(win_trades) / max(len(trades), 1) * 100
    
    avg_win = np.mean([t['pnl_pct'] for t in win_trades]) if win_trades else 0
    avg_loss = np.mean([t['pnl_pct'] for t in lose_trades]) if lose_trades else 0
    profit_loss_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else float('inf')
    
    total_profit = sum(t['pnl_pct'] for t in win_trades) if win_trades else 0
    total_loss = abs(sum(t['pnl_pct'] for t in lose_trades)) if lose_trades else 0
    profit_factor = total_profit / max(total_loss, 0.01)
    
    print(f"\n  =======================================================")
    print(f"  📊 {label}")
    print(f"  =======================================================")
    print(f"  总收益率:      {total_return:+.2f}%")
    print(f"  最大回撤:      -{max_dd:.2f}%")
    print(f"  交易次数:      {len(trades)}")
    print(f"  胜率:          {win_rate:.1f}%")
    print(f"  平均盈利:      {avg_win:+.2f}%")
    print(f"  平均亏损:      {avg_loss:+.2f}%")
    print(f"  盈亏比:        {profit_loss_ratio:.2f}")
    print(f"  盈利因子:      {profit_factor:.2f}")
    print(f"  补仓次数:      {replenish_count}")
    print(f"  半仓止盈:      {step_profit_count}")
    print(f"  最终资金:      {capital:,.0f}")
    print(f"  用时:          {time.time()-t0:.1f}s")
    
    return {
        'label': label, 'return': round(total_return, 2), 'dd': round(max_dd, 2),
        'trades': len(trades), 'win_rate': round(win_rate, 1),
        'avg_win': round(float(avg_win), 2), 'avg_loss': round(float(avg_loss), 2),
        'pl_ratio': round(profit_loss_ratio, 2), 'profit_factor': round(profit_factor, 2),
        'replenish': replenish_count, 'step_profit': step_profit_count,
        'final': round(capital)
    }


if __name__ == '__main__':
    configs = [
        # (label, alpha062_weight, alpha046_gate)
        ('M1季节补仓(基准)', 0.0, False),
        ('M1+α062×5%', 0.05, False),
        ('M1+α062×10%', 0.10, False),
        ('M1+α062×15%', 0.15, False),
        # alpha046门控+最优α062权重
    ]
    
    # 先跑权重扫描
    all_results = []
    for label, w, gate in configs:
        ALPHA062_WEIGHT = w
        USE_ALPHA046_GATE = gate
        r = run_backtest(label)
        all_results.append(r)
    
    # 再跑α046门控（用前面最优权重）
    print(f"\n{'='*70}")
    print(f"  用最优α062权重跑α046门控")
    print(f"{'='*70}")
    # 先找出最优
    best = max([r for r in all_results if 'α062' in r['label'] and r['return'] > 0], key=lambda x: x['return'], default=None)
    if best:
        best_w = [0.05, 0.10, 0.15][[0.05,0.10,0.15].index(float(best['label'].split('×')[1].replace('%',''))/100)]
        
        ALPHA062_WEIGHT = best_w
        USE_ALPHA046_GATE = True
        r = run_backtest(f'M1+α062×{int(best_w*100)}%+α046门控')
        all_results.append(r)
    
    # 对比汇总
    print(f"\n{'='*70}")
    print(f"  📋 对比汇总")
    print(f"{'='*70}")
    print(f"{'方案':35s} {'收益%':>7s} {'回撤%':>7s} {'胜率':>5s} {'盈亏比':>7s} {'因子':>7s} {'交易':>5s}")
    print('─'*75)
    for r in sorted(all_results, key=lambda x: x['return'], reverse=True):
        print(f"{r['label']:35s} {r['return']:>+7.2f} -{abs(r['dd']):>5.2f}% {r['win_rate']:>5.1f}% {r['pl_ratio']:>7.2f} {r['profit_factor']:>7.2f} {r['trades']:>5d}")
