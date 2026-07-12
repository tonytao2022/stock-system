#!/usr/bin/env python3
"""
M1 + Alpha062/046 完整回测套件
==============================
从bt_m1_score读取M1评分，在Python层加载alpha因子数据做混合评分
"""
import os, sys, pickle, time, json, gc, math, warnings
import numpy as np
import pymysql
warnings.filterwarnings('ignore')

MYSQL_PWD = 'iXve1rVBXfdA4tL9'

# 加载alpha因子数据
print("⏳ 加载alpha因子数据...")
t0 = time.time()
with open('/tmp/alpha_factors.pkl', 'rb') as f:
    ALPHA_DATA = pickle.load(f)
print(f"  Alpha因子: {len(ALPHA_DATA)}条 ({time.time()-t0:.0f}s)")

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

INITIAL_CAPITAL = 1000000

def get_season(date_str, conn):
    cur = conn.cursor()
    cur.execute("SELECT season FROM season_state WHERE trade_date=%s LIMIT 1", (date_str,))
    r = cur.fetchone()
    cur.close()
    if r and r[0] in SEASON_PARAMS:
        return r[0]
    return 'summer'

def run_one(a062_w, a046_gate, a046_min, label):
    """跑一组回测，a062_w=alpha062权重(0~1), a046_gate=是否启用alpha046门控"""
    conn = pymysql.connect(host='localhost', user='debian-sys-maint', password='iXve1rVBXfdA4tL9', database='stock_db_v2')
    
    t0 = time.time()
    print(f"\n{'='*65}")
    print(f"  🔄 {label}")
    print(f"{'='*65}")
    
    # 加载M1评分
    cur = conn.cursor()
    cur.execute("""
        SELECT ts_code, trade_date, m1_score as score
        FROM bt_m1_score WHERE m1_score IS NOT NULL ORDER BY trade_date, score DESC
    """)
    scores = cur.fetchall()
    cur.close()
    print(f"  M1: {len(scores)}条")
    
    # 构建day_scores（Python层混合评分）
    a062_hits = 0
    a046_hits = 0
    a046_blocked = 0
    
    day_scores = {}
    for r in scores:
        code = r[0]
        td = str(r[1])
        m1 = float(r[2])
        
        # 获取alpha因子
        key = (code, td)
        if key in ALPHA_DATA:
            a062, a046 = ALPHA_DATA[key]
            a062_hits += 1
        else:
            a062, a046 = 50.0, 50.0
        
        # alpha046门控
        if a046_gate and a046 < a046_min:
            a046_blocked += 1
            continue
        
        # 混合评分
        blended = m1 * (1 - a062_w) + a062 * a062_w
        blended = round(blended, 1)
        
        day_scores.setdefault(td, []).append({
            'ts_code': code, 'score': blended
        })
    
    all_dates = sorted(day_scores.keys())
    print(f"  交易日: {len(all_dates)}天 ({all_dates[0]}~{all_dates[-1]})")
    if a062_hits: print(f"  alpha062命中: {a062_hits}次")
    if a046_blocked: print(f"  alpha046门控拦截: {a046_blocked}次")
    
    # ====== 回测主循环 ======
    capital = INITIAL_CAPITAL
    peak_capital = INITIAL_CAPITAL
    max_drawdown = 0.0
    
    positions = {}  # code -> {shares, avg_price, buy_di, half_closed, replenished}
    trades = []     # [buy_date, sell_date, code, pnl_pct, reason]
    real_trades = []
    
    replenish_cnt = 0
    half_cnt = 0
    
    # 缓存ohlcv
    ohlcv = {}
    def get_price(code, td, field='close'):
        key = (code, td)
        if key not in ohlcv:
            cur2 = conn.cursor()
            cur2.execute("SELECT `close` FROM daily_kline WHERE ts_code=%s AND trade_date=%s", (code, td))
            r2 = cur2.fetchone()
            cur2.close()
            ohlcv[key] = float(r2[0]) if r2 else None
        return ohlcv[key]
    
    for di, td in enumerate(all_dates):
        season = get_season(td, conn)
        p = SEASON_PARAMS.get(season, SEASON_PARAMS['chaos'])
        buy_th = p['buy']; hold_period = p['hold']
        sl1 = p['sl1']/100; sl2 = p['sl2']/100; tp_rate = p['tp']/100
        single_limit = p['single']
        
        peak_capital = max(peak_capital, capital)
        dd = (peak_capital - capital) / peak_capital * 100
        max_drawdown = max(max_drawdown, dd)
        
        # 检查持仓
        close_list = []
        for code, pos in list(positions.items()):
            price = get_price(code, td)
            if price is None: continue
            days_held = di - pos['buy_di']
            cost = pos['avg_price']
            pnl = (price - cost) / cost
            
            if days_held <= 5 and pnl <= sl1 * 1.5:
                close_list.append((code, price, '止损T1'))
            elif pnl <= sl2:
                close_list.append((code, price, '止损T2'))
            elif days_held >= hold_period:
                close_list.append((code, price, '到期'))
            
            # 半仓止盈
            if pnl >= 0.12 and not pos.get('half_closed', False):
                half_shares = pos['shares'] // 2
                if half_shares > 0:
                    capital += half_shares * price
                    pos['shares'] -= half_shares
                    pos['half_closed'] = True
                    half_cnt += 1
            
            # 补仓
            if pnl <= -0.08 and not pos.get('replenished', False):
                # 下一天检查评分
                if di+1 < len(all_dates):
                    next_td = all_dates[di+1]
                    day_cands = day_scores.get(next_td, [])
                    if any(c['ts_code'] == code and c['score'] >= buy_th for c in day_cands):
                        replenish_qty = pos['shares']
                        cost2 = replenish_qty * price
                        if cost2 <= capital:
                            capital -= price * replenish_qty
                            pos['avg_price'] = (cost * pos['shares'] + price * replenish_qty) / (pos['shares'] + replenish_qty)
                            pos['shares'] += replenish_qty
                            pos['replenished'] = True
                            replenish_cnt += 1
            
            if pos.get('replenished', False):
                avg_pnl = (price - pos['avg_price']) / pos['avg_price']
                if avg_pnl <= -0.15:
                    close_list.append((code, price, '均价止损'))
        
        for code, price, reason in close_list:
            pos = positions.pop(code)
            if pos:
                pnl_pct = (price - pos['avg_price']) / pos['avg_price'] * 100
                capital += pos['shares'] * price
                trades.append([pos['buy_date'], td, code, round(pnl_pct, 1), reason])
                real_trades.append([pos['buy_date'], td, code, price, pos['avg_price'], round(pnl_pct, 1), reason, pnl_pct])
        
        # 买入
        available = 5 - len(positions)
        if available > 0 and capital > 10000:
            daily = day_scores.get(td, [])
            remaining = [s for s in daily if s['ts_code'] not in positions and s['score'] >= buy_th]
            
            for cand in remaining[:available]:
                code = cand['ts_code']
                price = get_price(code, td)
                if price is None or price <= 0: continue
                
                pos_val = min(capital * single_limit, price * 1000000)
                qty = max(int(pos_val / price / 100) * 100, 100)
                cost = qty * price
                
                if cost <= 0 or cost > capital * 0.5: continue
                
                capital -= cost
                positions[code] = {
                    'shares': qty, 'avg_price': price, 'buy_date': td, 'buy_di': di,
                    'half_closed': False, 'replenished': False
                }
    
    # 平仓剩余
    if positions:
        last_td = all_dates[-1]
        for code, pos in list(positions.items()):
            price = get_price(code, last_td)
            if price is None: price = pos['avg_price']
            pnl_pct = (price - pos['avg_price']) / pos['avg_price'] * 100
            capital += pos['shares'] * price
            trades.append([pos['buy_date'], last_td, code, round(pnl_pct, 1), '期末'])
            real_trades.append([pos['buy_date'], last_td, code, price, pos['avg_price'], round(pnl_pct, 1), '期末', pnl_pct])
        positions.clear()
    
    conn.close()
    
    # ====== 统计 ======
    total_ret = (capital - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100
    
    win_trades = [t for t in real_trades if t[7] > 0]
    lose_trades = [t for t in real_trades if t[7] <= 0]
    wr = len(win_trades) / max(len(real_trades), 1) * 100
    avg_w = np.mean([t[7] for t in win_trades]) if win_trades else 0
    avg_l = np.mean([t[7] for t in lose_trades]) if lose_trades else 0
    pl_ratio = abs(avg_w / avg_l) if avg_l != 0 else float('inf')
    tp = sum(t[7] for t in win_trades) if win_trades else 0
    tl = abs(sum(t[7] for t in lose_trades)) if lose_trades else 0
    pf = tp / max(tl, 0.01)
    
    elapsed = time.time() - t0
    
    print()
    print(f"  {'─'*42}")
    print(f"  📊 结果: {label}")
    print(f"  {'─'*42}")
    print(f"  总收益率:      {total_ret:>+8.2f}%")
    print(f"  最大回撤:      {max_drawdown:>7.2f}%")
    print(f"  交易次数:      {len(real_trades):>5}笔")
    print(f"  胜率:          {wr:>5.1f}%")
    print(f"  平均盈利:      {avg_w:>+7.2f}%")
    print(f"  平均亏损:      {avg_l:>7.2f}%")
    print(f"  盈亏比:        {pl_ratio:>5.2f}")
    print(f"  盈利因子:      {pf:>5.2f}")
    print(f"  补仓执行:      {replenish_cnt:>5}次")
    print(f"  半仓止盈:      {half_cnt:>5}次")
    print(f"  用时:          {elapsed:.0f}s")
    
    return {
        'label': label, 'ret': round(total_ret,2), 'dd': round(max_drawdown,2),
        'trades': len(real_trades), 'wr': round(wr,1),
        'avg_w': round(float(avg_w),2), 'avg_l': round(float(avg_l),2),
        'pl': round(pl_ratio,2), 'pf': round(pf,2),
        'repl': replenish_cnt, 'hp': half_cnt
    }


if __name__ == '__main__':
    configs = [
        # (a062_w, gate, gmin, label)
        (0.0,  False, 0,  'M1季节补仓(基准)'),
        (0.05, False, 0,  'M1+α062×5%'),
        (0.10, False, 0,  'M1+α062×10%'),
        (0.15, False, 0,  'M1+α062×15%'),
        (0.20, False, 0,  'M1+α062×20%'),
        (0.30, False, 0,  'M1+α062×30%'),
        (0.50, False, 0,  'M1+α062×50%'),
        (0.10, True,  30, 'M1+α062×10%+α046门控'),
        (0.05, False, 0,  'M1+α062×5%+α046×5%(模拟当前)'),
    ]
    
    all_results = []
    for w, gate, gmin, label in configs:
        r = run_one(w, gate, gmin, label)
        all_results.append(r)
        gc.collect()
    
    # 汇总
    print(f"\n\n{'='*80}")
    print(f"  📋 对比汇总")
    print(f"{'='*80}")
    print(f"{'方案':35s} {'收益%':>8s} {'回撤%':>7s} {'胜率':>6s} {'盈亏比':>7s} {'因子':>7s} {'交易':>5s} {'补仓':>4s}")
    print('─'*85)
    for r in sorted(all_results, key=lambda x: -(x['ret'] or 0)):
        pl_s = f"{r['pl']:>7.2f}" if r['pl'] != float('inf') else '    ∞'
        print(f"{r['label']:35s} {r['ret']:>+8.2f} -{abs(r['dd']):>5.2f}% {r['wr']:>6.1f}% {pl_s} {r['pf']:>7.2f} {r['trades']:>5d} {r['repl']:>4d}")
