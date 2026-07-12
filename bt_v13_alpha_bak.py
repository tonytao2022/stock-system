#!/usr/bin/env python3
"""
V13.2 季节参数 + 74分基准 + 金字塔补仓 + 阶梯止盈 全回测
=======================================================
评分源: bt_m1_score.m1_score
季节参数: 取自V13.2 strategy_config (买入线统一改为74)
"""
import sys, os, time, pymysql, numpy as np
sys.path.insert(0, '/opt/stock-analyzer')
import warnings
warnings.filterwarnings("ignore")

DB = {'host':'127.0.0.1','port':3306,'user':'debian-sys-maint',
      'password':'iXve1rVBXfdA4tL9','database':'stock_db_v2',
      'charset':'utf8mb4','connect_timeout':10,'read_timeout':300,'write_timeout':300,
      'autocommit':True,'cursorclass':pymysql.cursors.DictCursor}

INITIAL_CAPITAL = 1000000
COMMISSION = 0.0003
SLIPPAGE = 0.001

# 季节参数矩阵（V13.2基础 + 买入线改为74）
SEASON_PARAMS = {
    'summer':        {'buy':65, 'hold':30, 'stop':0.12, 'take':0.18, 'sp':50, 'tp':50},
    'spring':        {'buy':65, 'hold':30, 'stop':0.12, 'take':0.15, 'sp':35, 'tp':40},
    'weak_spring':   {'buy':65, 'hold':25, 'stop':0.11, 'take':0.15, 'sp':35, 'tp':40},
    'chaos_spring':  {'buy':65, 'hold':25, 'stop':0.11, 'take':0.15, 'sp':20, 'tp':35},
    'chaos':         {'buy':65, 'hold':25, 'stop':0.10, 'take':0.12, 'sp':20, 'tp':30},
    'chaos_autumn':  {'buy':65, 'hold':20, 'stop':0.08, 'take':0.10, 'sp':15, 'tp':20},
    'weak_autumn':   {'buy':65, 'hold':20, 'stop':0.08, 'take':0.12, 'sp':20, 'tp':25},
    'autumn':        {'buy':65, 'hold':20, 'stop':0.10, 'take':0.12, 'sp':30, 'tp':35},
    'winter':        {'buy':65, 'hold':10, 'stop':0.05, 'take':0.08, 'sp':5,  'tp':10},
}

# 补仓/止盈参数（统一）
REPLENISH_TRIGGER = -0.08
REPLENISH_STOP_LOSS = -0.15
TRAILING_ACTIVATE = 0.12


def get_conn():
    return pymysql.connect(**DB)


def load_ohlcv(conn, code, start_dt, end_dt):
    cur = conn.cursor()
    cur.execute("""
        SELECT trade_date, `open`, high, low, `close`
        FROM daily_kline WHERE ts_code=%s AND trade_date>=%s AND trade_date<=%s
        ORDER BY trade_date
    """, (code, start_dt, end_dt))
    rows = cur.fetchall(); cur.close()
    return rows


def get_season(date_str):
    """获取季节判定——从season_state表"""
    conn = get_conn(); cur = conn.cursor()
    cur.execute("""
        SELECT season, hengjiyuan_level, regime, confidence, scoring_strategy
        FROM season_state WHERE trade_date=%s
    """, (date_str,))
    r = cur.fetchone(); cur.close(); conn.close()
    if r:
        season = r['season']
        if season not in SEASON_PARAMS:
            season = 'summer'
        return season
    return 'summer'


def main():
    t0 = time.time()
    conn = get_conn()
    
    print("⏳ 加载M1评分...")
    cur = conn.cursor()
    cur.execute("""
        SELECT ts_code, trade_date, m1_score as score
        FROM bt_m1_score WHERE m1_score IS NOT NULL ORDER BY trade_date, score DESC
    """)
    scores = cur.fetchall(); cur.close()
    print(f"  M1: {len(scores)}条 ({len(set(r['trade_date'] for r in scores))}天)")
    
    # ====== Alpha因子加载与混合 ======
    # 配置
    A062_W = float(os.environ.get('A062_W', '0.0'))
    A046_GATE = os.environ.get('A046_GATE', '0') == '1'
    A046_MIN = int(os.environ.get('A046_MIN', '30'))
    
    ALPHA_DATA = {}
    cur2 = conn.cursor()
    cur2.execute("""SELECT ts_code, trade_date, alpha062_score, alpha046_score
        FROM strategy_signal WHERE alpha062_score IS NOT NULL""")
    for r2 in cur2.fetchall():
        key = (r2['ts_code'], str(r2['trade_date']))
        ALPHA_DATA[key] = {'a062': float(r2['alpha062_score'] or 50), 'a046': float(r2['alpha046_score'] or 50)}
    cur2.close()
    print(f"  Alpha因子: {len(ALPHA_DATA)}条")
    
    day_scores = {}
    a062_hits = 0; a046_blocked = 0
    for r in scores:
        td = str(r['trade_date'])
        code = r['ts_code']
        m1 = float(r['score'])
        
        # 获取alpha
        key = (code, td)
        if key in ALPHA_DATA:
            a062 = ALPHA_DATA[key]['a062']
            a046 = ALPHA_DATA[key]['a046']
            a062_hits += 1
        else:
            a062, a046 = 50.0, 50.0
        
        # alpha046门控
        if A046_GATE and a046 < A046_MIN:
            a046_blocked += 1
            continue
        
        # 混合评分（修正：M1原始权重总和=0.90，不是1.0）
        # α062×15% 时: blended = (M1×0.75 + α062×0.15) / 0.90
        if A062_W > 0:
            m1 = (m1 * (0.90 - A062_W) + a062 * A062_W) / 0.90
        
        day_scores.setdefault(td, []).append({
            'ts_code': code, 'score': round(m1, 1)
        })
    
    all_dates = sorted(day_scores.keys())
    print(f"  交易日: {len(all_dates)}天 ({all_dates[0]} ~ {all_dates[-1]})")
    if a062_hits: print(f"  alpha062命中: {a062_hits}次")
    if a046_blocked: print(f"  alpha046拦截: {a046_blocked}次")
    
    print("\n📐 季节参数矩阵（买入线65基准）:")
    for sn, p in SEASON_PARAMS.items():
        print(f"  {sn:14s} | 买{p['buy']:2d} 持{p['hold']:2d}d 止{p['stop']*100:4.0f}% 止盈{p['take']*100:4.0f}% 单{p['sp']:2d}% 总{p['tp']:2d}%")
    print(f"\n  补仓: -{abs(REPLENISH_TRIGGER)*100:.0f}% → 均价-{abs(REPLENISH_STOP_LOSS)*100:.0f}% 止盈: +{TRAILING_ACTIVATE*100:.0f}%半仓")
    print()
    
    return run_backtest(conn, day_scores, all_dates)


def run_backtest(conn, day_scores, all_dates):
    positions = {}
    trades = []
    capital = INITIAL_CAPITAL
    peak_capital = INITIAL_CAPITAL
    max_drawdown = 0.0
    
    # K线缓存
    kline_cache = {}
    def get_close(code, td):
        if code not in kline_cache:
            rows = load_ohlcv(conn, code, all_dates[0], all_dates[-1])
            kline_cache[code] = {str(r['trade_date']): {
                'open':float(r['open']),'high':float(r['high']),
                'low':float(r['low']),'close':float(r['close'])
            } for r in rows} if rows else {}
        return kline_cache[code].get(td)
    
    for di, td in enumerate(all_dates):
        pct = int((di+1)/len(all_dates)*100)
        if di % 30 == 0:
            print(f"  [{di+1}/{len(all_dates)} {pct}%] 📅 {td} 持{len(positions)}只 资{capital:>8.0f}", end='\r', flush=True)
        
        candidates = day_scores[td]
        
        # 获取当日季节参数
        season = get_season(td)
        params = SEASON_PARAMS.get(season, SEASON_PARAMS['summer'])
        buy_threshold = params['buy']
        max_hold = 60  # 最久持有60日（和实盘一致）
        stop_loss = params['stop']
        max_single_pct = params['sp'] / 100
        max_total_pct = params['tp'] / 100
        
        # -- 持仓管理 --
        to_close = []
        for code, pos in list(positions.items()):
            kl = get_close(code, td)
            if not kl: continue
            price = kl['close']
            
            if price > pos['peak_price']:
                pos['peak_price'] = price
            
            # 已出半仓
            if pos.get('half_exited'):
                current_pnl = (price - pos['avg_price']) / pos['avg_price']
                if pos.get('half_peak', 0) > 0:
                    dd_from_peak = (pos['half_peak'] - price) / pos['half_peak']
                    if dd_from_peak >= 0.10:
                        to_close.append((code, price, '半仓后止盈'))
                        continue
                if current_pnl <= REPLENISH_STOP_LOSS:
                    to_close.append((code, price, '全仓止损(半仓后)'))
                continue
            
            # 补仓检查
            if not pos.get('replenished'):
                pnl_from_buy = (price - pos['first_buy_price']) / pos['first_buy_price']
                if pnl_from_buy <= REPLENISH_TRIGGER:
                    day_cand = [c for c in candidates if c['ts_code'] == code]
                    if day_cand and day_cand[0]['score'] >= buy_threshold:
                        alloc = capital * 0.5
                        add_shares = int(alloc / price / 100) * 100
                        if add_shares >= 100:
                            cost = add_shares * price * COMMISSION
                            capital -= add_shares * price + cost
                            new_shares = pos['shares'] + add_shares
                            pos['avg_price'] = (pos['avg_price'] * pos['shares'] + price * add_shares) / new_shares
                            pos['total_cost'] += add_shares * price + cost
                            pos['shares'] = new_shares
                            pos['replenished'] = True
                            trades.append((td, td, code, pos['first_buy_price'], price,
                                         0, day_cand[0]['score'], '补仓'))
            
            # 补仓后止损
            pnl_from_avg = (price - pos['avg_price']) / pos['avg_price']
            if pnl_from_avg <= REPLENISH_STOP_LOSS:
                to_close.append((code, price, '均价止损'))
                continue
            
            # 阶梯止盈
            if pos.get('replenished'):
                pnl_from_avg = (price - pos['avg_price']) / pos['avg_price']
                take_profit_line = params['take']
                if pnl_from_avg >= take_profit_line:
                    half = pos['shares'] // 2
                    if half >= 100:
                        sv = half * price
                        cc = sv * COMMISSION
                        capital += sv - cc
                        pos['shares'] -= half
                        pos['total_cost'] = pos['avg_price'] * pos['shares']
                        pos['half_exited'] = True
                        pos['half_peak'] = price
                        pos['half_date'] = td
                        trades.append((td, td, code, pos['avg_price'], price, 0, 0, '半仓止盈'))
                        continue
            else:
                pnl_from_buy = (price - pos['first_buy_price']) / pos['first_buy_price']
                take_profit_line = params['take']
                if pnl_from_buy >= take_profit_line:
                    half = pos['shares'] // 2
                    if half >= 100:
                        sv = half * price
                        cc = sv * COMMISSION
                        capital += sv - cc
                        pos['shares'] -= half
                        pos['total_cost'] = pos['avg_price'] * pos['shares']
                        pos['half_exited'] = True
                        pos['half_peak'] = price
                        pos['half_date'] = td
                        trades.append((td, td, code, pos['first_buy_price'], price, 0, 0, '半仓止盈'))
                        continue
            
            # 最久持有期（实盘V13.2：60日）
            pos['hold_days'] = pos.get('hold_days', 0) + 1
            if pos['hold_days'] >= max_hold:
                to_close.append((code, price, f'到期{max_hold}d'))
                continue
            
            # 固定止损
            pnl_from_first = (price - pos['first_buy_price']) / pos['first_buy_price']
            if pnl_from_first <= -stop_loss:
                to_close.append((code, price, '止损'))
                continue
        
        # 执行卖出
        for code, sell_price, reason in to_close:
            pos = positions.pop(code)
            sv = pos['shares'] * sell_price
            cc = sv * COMMISSION
            pnl_pct = (sv - pos['total_cost'] - cc) / pos['total_cost'] * 100
            capital += sv - cc
            trades.append((
                pos['buy_date'], td, code,
                pos['first_buy_price'], sell_price,
                round(pnl_pct, 2), pos.get('buy_score', 0), reason
            ))
        
        # -- 买入（季节仓位约束）--
        if len(positions) < 5:  # 最多5只，季节仓位控制单票比例
            held_codes = set(positions.keys())
            buy_list = [c for c in candidates 
                       if c['score'] >= buy_threshold 
                       and c['ts_code'] not in held_codes]
            buy_list.sort(key=lambda x: x['score'], reverse=True)
            slots = min(5 - len(positions), 5)  # 最多一天买5只
            
            for cand in buy_list[:slots]:
                code = cand['ts_code']
                kl = get_close(code, td)
                if not kl: continue
                price = kl['close']
                if price <= 0: continue
                
                # 按季节单票仓位分配
                alloc = capital * max_single_pct
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
            if kl: total_value += pos['shares'] * kl['close']
        
        dd = (peak_capital - total_value) / peak_capital * 100
        max_drawdown = max(max_drawdown, dd)
        if total_value > peak_capital: peak_capital = total_value
    
    # 强平
    last_td = all_dates[-1]
    for code, pos in list(positions.items()):
        kl = get_close(code, last_td)
        if kl:
            sv = pos['shares'] * kl['close']
            cc = sv * COMMISSION
            pnl = (sv - pos['total_cost'] - cc) / pos['total_cost'] * 100
            capital += sv - cc
            half = '是' if pos.get('half_exited') else '否'
            trades.append((pos['buy_date'], last_td, code, pos['first_buy_price'],
                         kl['close'], round(pnl,2), pos.get('buy_score',0),
                         f'强平(补{pos.get("replenished")}半{half})'))
    
    # 统计
    total_return = (capital - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100
    real_trades = [t for t in trades if t[1] != t[0]]
    win = [t for t in real_trades if t[5] > 0]
    loss = [t for t in real_trades if t[5] <= 0]
    win_rate = len(win)/len(real_trades)*100 if real_trades else 0
    avg_win = np.mean([t[5] for t in win]) if win else 0
    avg_loss = np.mean([t[5] for t in loss]) if loss else 0
    pf = abs(sum(t[5] for t in win) / sum(t[5] for t in loss)) if loss and sum(t[5] for t in loss) != 0 else float('inf')
    
    replenish_cnt = len([t for t in trades if '补仓' in t[7]])
    half_cnt = len([t for t in trades if '半仓' in t[7]])
    
    print(f"\n\n{'='*55}")
    print(f"  📊 V13.2 季节参数 · 65买入线 · +补仓+止盈")
    print(f"  {all_dates[0]} ~ {all_dates[-1]} ({len(all_dates)}个交易日)")
    print(f"{'='*55}")
    print(f"  总收益率:      {total_return:>+8.2f}%")
    print(f"  最大回撤:      {max_drawdown:>7.2f}%")
    print(f"  最终资金:      {capital:>10.0f}")
    print(f"  {'─'*42}")
    print(f"  交易次数:      {len(real_trades):>5}笔")
    print(f"  胜率:          {win_rate:>5.1f}%")
    print(f"  平均盈利:      {avg_win:>+7.2f}%")
    print(f"  平均亏损:      {avg_loss:>7.2f}%")
    print(f"  盈亏比:        {abs(avg_win/avg_loss):>5.2f}" if avg_loss else "  盈亏比:     ∞")
    print(f"  盈利因子:      {pf:>5.2f}")
    print(f"  {'─'*42}")
    print(f"  补仓执行:      {replenish_cnt:>5}次")
    print(f"  半仓止盈:      {half_cnt:>5}次")
    
    print(f"\n  前20笔主交易:")
    print(f"  {'买入':>10} {'卖出':>10} {'代码':>10} {'收益':>7} {'原因':>16}")
    print(f"  {'─'*58}")
    for t in real_trades[:20]:
        print(f"  {t[0]:>10} {t[1]:>10} {t[2]:>10} {t[5]:>+6.1f}% {t[7]:>16}")
    
    elapsed = time.time() - t0
    print(f"\n  ⏱ 耗时: {elapsed:.0f}s")
    conn.close()


if __name__ == '__main__':
    main()
