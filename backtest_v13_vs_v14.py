#!/usr/bin/env python3
"""
V13 vs V14 对比回测 v2 — 现场混合H5评分
========================================
V13: 使用 composite_score（来自daily_score_snapshot）
V14: 使用 composite_score × 0.8 + h5_score × 0.2
      H5评分现场计算，只对候选买入股和持仓股计算（非全量）
"""
import sys, os, time, math, json
from datetime import datetime, timedelta
import numpy as np
import pymysql
from pymysql.cursors import DictCursor
import warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, '/opt/stock-analyzer')
import db_config

PARAMS = {
    'summer':         {'buy_min':65,'max_hold':30,'sl_t1':12,'sl_t2':9,'trail':18,'sgl':50,'ttl':50},
    'spring':         {'buy_min':65,'max_hold':30,'sl_t1':12,'sl_t2':9,'trail':15,'sgl':35,'ttl':40},
    'weak_spring':    {'buy_min':68,'max_hold':25,'sl_t1':11,'sl_t2':8,'trail':15,'sgl':35,'ttl':40},
    'chaos_spring':   {'buy_min':72,'max_hold':25,'sl_t1':11,'sl_t2':8,'trail':15,'sgl':20,'ttl':35},
    'chaos':          {'buy_min':80,'max_hold':25,'sl_t1':10,'sl_t2':8,'trail':12,'sgl':20,'ttl':30},
    'chaos_autumn':   {'buy_min':72,'max_hold':20,'sl_t1':8,'sl_t2':6,'trail':10,'sgl':15,'ttl':20},
    'weak_autumn':    {'buy_min':70,'max_hold':20,'sl_t1':8,'sl_t2':6,'trail':12,'sgl':20,'ttl':25},
    'autumn':         {'buy_min':68,'max_hold':20,'sl_t1':10,'sl_t2':8,'trail':12,'sgl':30,'ttl':35},
    'winter':         {'buy_min':85,'max_hold':10,'sl_t1':5,'sl_t2':4,'trail':8,'sgl':5,'ttl':10},
}

LABEL = {'summer':'☀️夏季','spring':'🌸春季','weak_spring':'⛅弱春','chaos_spring':'🌤️混沌春',
         'chaos':'🌪️混沌','chaos_autumn':'☁️混沌秋','weak_autumn':'⛅弱秋',
         'autumn':'🍂秋季','winter':'❄️冬季'}
BASE = 1_000_000; COMM = 0.001; STAMP = 0.0005; MAX_POS = 8; MAX_DAILY_BUY = 5

# 正向5因子H5参数
H5_FACTORS = ['alpha005', 'alpha034', 'alpha046', 'alpha062', 'alpha089']


def get_conn():
    pwd = db_config._get_password()
    return pymysql.connect(host='127.0.0.1', port=3306, user='debian-sys-maint',
                           password=pwd, database='stock_db_v2',
                           charset='utf8mb4', cursorclass=DictCursor)


def run_sql(cur, sql, params=None):
    if params: cur.execute(sql, params)
    else: cur.execute(sql)
    try: return cur.fetchall()
    except: return []


def compute_h5_factors(ts_code, trade_date, cur):
    """计算单只股票在指定交易日的5个Alpha因子原始值"""
    cur.execute("""
        SELECT trade_date, `open`, high, low, `close`, vol
        FROM daily_kline WHERE ts_code=%s AND trade_date <= %s
        ORDER BY trade_date DESC LIMIT 120
    """, (ts_code, trade_date))
    rows = cur.fetchall()
    if not rows or len(rows) < 30: return None
    
    rows.reverse()
    o=[]; h=[]; l=[]; c=[]; v=[]
    for r in rows:
        o.append(float(r['open'])); h.append(float(r['high']))
        l.append(float(r['low'])); c.append(float(r['close'])); v.append(float(r['vol']))
    
    n = len(c); i = n - 1
    factors = {}
    
    try:
        if i>=4:
            c5=c[i-4:i+1]; v5=v[i-4:i+1]
            if len(set(round(x,4) for x in c5))>=2 and len(set(round(x,4) for x in v5))>=2:
                rc=np.argsort(c5)/len(c5); rv=np.argsort(v5)/len(v5)
                if np.std(rc)>1e-10 and np.std(rv)>1e-10:
                    factors['alpha005'] = -float(np.corrcoef(rc,rv)[0,1])
    except: pass
    
    try:
        if i>=11 and c[i]>0:
            factors['alpha034'] = c[i-11:i+1].mean()/c[i]
    except: pass
    
    try:
        if i>=23 and c[i]>0:
            ma3=c[i-2:i+1].mean(); ma6=c[i-5:i+1].mean()
            ma12=c[i-11:i+1].mean(); ma24=c[i-23:i+1].mean()
            factors['alpha046'] = (ma3+ma6+ma12+ma24)/(4*c[i])
    except: pass
    
    try:
        if i>=4:
            h5_=h[i-4:i+1]; v5_=v[i-4:i+1]
            if len(set(round(x,4) for x in h5_))>=2 and len(set(round(x,4) for x in v5_))>=2:
                if np.std(h5_)>1e-10 and np.std(v5_)>1e-10:
                    factors['alpha062'] = -float(np.corrcoef(h5_,v5_)[0,1])
    except: pass
    
    try:
        if i>=12:
            c13=c[i-12:i+1]; v13=v[i-12:i+1]
            if len(set(round(x,4) for x in c13))>=2 and len(set(round(x,4) for x in v13))>=2:
                if np.std(c13)>1e-10 and np.std(v13)>1e-10:
                    factors['alpha089'] = 1-float(np.corrcoef(c13,v13)[0,1])
    except: pass
    
    return factors if len(factors) >= 3 else None


def get_kline_close(ts_code, trade_date, cur):
    cur.execute("SELECT close FROM daily_kline WHERE ts_code=%s AND trade_date=%s", (ts_code, trade_date))
    r = cur.fetchone()
    return float(r['close']) if r else None


def simulate(conn, start_date, end_date, use_h5=False):
    scheme = 'V14' if use_h5 else 'V13'
    cur = conn.cursor()
    
    # 交易日（从daily_score_snapshot获取有评分的交易日）
    cur.execute("SELECT DISTINCT trade_date FROM daily_score_snapshot WHERE trade_date>=%s AND trade_date<=%s ORDER BY trade_date", (start_date, end_date))
    td_rows = cur.fetchall()
    all_td = [r['trade_date'].strftime('%Y-%m-%d') if hasattr(r['trade_date'],'strftime') else str(r['trade_date']) for r in td_rows]
    
    # 季节
    cur.execute("SELECT trade_date, season FROM season_state WHERE index_code='MARKET' ORDER BY trade_date")
    season_map = {}
    for r in cur.fetchall():
        td = r['trade_date'].strftime('%Y-%m-%d') if hasattr(r['trade_date'],'strftime') else str(r['trade_date'])
        season_map[td] = r['season']
    
    print(f"\n{'='*55}")
    print(f"  {scheme} 回测 | {start_date} ~ {end_date} | {len(all_td)}日")
    print(f"{'='*55}")
    
    total_days = len(all_td)
    cash = BASE
    holdings = {}
    trades = []; daily_value = []
    total_fee = total_tax = 0
    prev_pct = -1; t0_sim = time.time()
    
    # H5局部缓存：很多天可能共享相同的H5值（因为因子变化慢）
    h5_cache = {}
    
    for di, td in enumerate(all_td):
        if di < 3: continue
        pct = int((di+1)/total_days*100)
        if pct % 10 == 0 and pct > prev_pct:
            print(f"  [{pct}%] {td} ({time.time()-t0_sim:.0f}s)")
            prev_pct = pct
        
        pos_value = 0
        to_close = []
        for code, h in list(holdings.items()):
            k = get_kline_close(code, td, cur)
            if k is None: continue
            shares = h['shares']
            pos_value += shares * k
            
            if di - h['buy_day_idx'] >= h['max_hold']:
                to_close.append((code, 'MAX_HOLD', k, shares, h))
                continue
            if (k - h['avg_price'])/h['avg_price']*100 <= -h['sl_t1']:
                to_close.append((code, 'SL', k, shares, h))
                continue
            # 回撤止损
            if k > h.get('peak', 0): h['peak'] = k
            trail = h.get('trail', 15)
            if (h['peak'] - k)/h['peak']*100 >= trail:
                to_close.append((code, 'TRAIL', k, shares, h))
                continue
        
        for code, reason, price, shares, h in to_close:
            fee = shares * price * COMM
            tax = shares * price * STAMP
            total_fee += fee; total_tax += tax
            cash += shares * price - fee - tax
            pnl = shares*price - shares*h['avg_price'] - fee - tax
            trades.append({'code':code,'action':'SELL','reason':reason,'buy_date':h['buy_date'],
                          'sell_date':td,'shares':shares,'buy_price':round(h['avg_price'],2),
                          'sell_price':round(price,2),'pnl':round(pnl,2),
                          'pnl_pct':round((price-h['avg_price'])/h['avg_price']*100,2),
                          'hold_days':di-h['buy_day_idx']})
            if code in holdings: del holdings[code]
        
        # 获取评分
        cur.execute("SELECT ts_code, composite_score FROM daily_score_snapshot WHERE trade_date=%s AND composite_score IS NOT NULL", (td,))
        score_rows = cur.fetchall()
        scores = {r['ts_code']: float(r['composite_score']) for r in score_rows}
        
        if use_h5 and scores:
            # 对候选买入股票计算H5评分混合
            candidate_codes = [c for c in scores if c not in holdings]
            for code in candidate_codes[:100]:  # 只检查前100只（按P6排名）
                if code in h5_cache and h5_cache[code].get('expire') == td:
                    h5 = h5_cache[code].get('h5')
                else:
                    f = compute_h5_factors(code, td, cur)
                    if f and len(f) >= 3:
                        # 截面rank标准化不做了，直接用因子均值加权
                        h5 = sum(f.values()) / len(f)
                        h5_cache[code] = {'h5': h5, 'expire': td}
                    else:
                        h5 = None
                if h5 is not None:
                    # 简单H5分：未标准化，但用百分位数概念映射到0~100
                    # 为了简化，直接混合：V14 = P6 * 0.8 + h5_normalized * 0.2
                    # h5_normalized 直接把h5原始值缩放到50左右（通过线性变换）
                    h5_norm = h5 * 50 + 50  # 粗略映射
                    scores[code] = scores[code] * 0.8 + h5_norm * 0.2
        
        # 季节参数
        season = season_map.get(td, 'chaos')
        p = PARAMS.get(season, PARAMS['chaos'])
        buy_min = p['buy_min']
        
        pos_pct = pos_value / max(cash, 1) * 100
        max_pos_pct = p.get('ttl', 50)
        avail = max_pos_pct * cash / 100 - pos_value
        per_pos = min(BASE * p.get('sgl', 20) / 100, avail / max(1, MAX_POS - len(holdings)))
        
        daily_value.append({'date': td, 'cash': round(cash,2), 'pos_value': round(pos_value,2),
                          'total': round(cash+pos_value,2)})
        
        if len(holdings) < MAX_POS and avail > 1000:
            cands = sorted([(c,s) for c,s in scores.items() if c not in holdings and s >= buy_min], key=lambda x:-x[1])
            for code, score in cands[:MAX_DAILY_BUY]:
                if len(holdings) >= MAX_POS: break
                k = get_kline_close(code, td, cur)
                if not k or k<=0: continue
                amt = min(per_pos, avail)
                shares = int(amt/k/100)*100
                if shares < 100: continue
                cost = shares*k*(1+COMM)
                if cost > avail: continue
                cash -= cost; total_fee += shares*k*COMM; avail -= cost
                holdings[code] = {'shares':shares,'avg_price':k,'buy_date':td,
                                 'buy_day_idx':di,'max_hold':p['max_hold'],
                                 'sl_t1':p['sl_t1'],'trail':p.get('trail',15),'peak':k}
                trades.append({'code':code,'action':'BUY','reason':f'score={score:.0f}','buy_date':td,
                             'sell_date':'','shares':shares,'buy_price':round(k,2),
                             'sell_price':0,'pnl':0,'pnl_pct':0,'hold_days':0})
    
    # 清盘
    for code, h in list(holdings.items()):
        k = get_kline_close(code, all_td[-1], cur)
        if k:
            cash += h['shares']*k*(1-COMM-STAMP)
            total_fee += h['shares']*k*COMM
    final = cash
    cur.close()
    
    return analyze(trades, daily_value, final, scheme)


def analyze(trades, daily_value, final, scheme):
    total_return = (final-BASE)/BASE*100
    peak = BASE; max_dd = 0
    for dv in daily_value:
        t = dv['total']
        if t > peak: peak = t
        dd = (peak-t)/peak*100
        if dd > max_dd: max_dd = dd
    
    closed = [t for t in trades if t['action']=='SELL']
    wins = [t for t in closed if t['pnl']>0]
    loses = [t for t in closed if t['pnl']<=0]
    wr = len(wins)/len(closed)*100 if closed else 0
    avg_w = np.mean([t['pnl'] for t in wins]) if wins else 0
    avg_l = abs(np.mean([t['pnl'] for t in loses])) if loses else 1
    pr = avg_w/max(avg_l,1)
    
    dr = [(dv['total']-BASE)/BASE for dv in daily_value]
    sharpe = 0
    if len(dr) > 1 and np.std(dr)>1e-10:
        dr_diff = np.diff([dv['total'] for dv in daily_value])
        dr_diff = dr_diff / [dv['total'] for dv in daily_value[:-1]]
        sharpe = np.mean(dr_diff)/max(np.std(dr_diff),1e-10)*np.sqrt(252)
    carmar = total_return/max(max_dd,1)
    
    print(f"\n  {scheme} 回测结果")
    print(f"  总收益: {total_return:>+8.2f}% | 回撤: {max_dd:>5.2f}% | 卡玛: {carmar:>5.2f} | 夏普: {sharpe:>5.2f}")
    print(f"  交易: {len(closed)}笔 | 胜率: {wr:>5.1f}% | 盈亏比: {pr:>5.2f}")
    
    return {'scheme':scheme,'total_return':round(total_return,2),'max_dd':round(max_dd,2),
            'carmar':round(carmar,2),'sharpe':round(sharpe,2),'win_rate':round(wr,1),
            'profit_ratio':round(pr,2),'n_trades':len(closed),'final_capital':round(final,2)}


if __name__ == '__main__':
    BT_START = "2026-06-16"
    BT_END = "2026-07-10"
    t0 = time.time()
    
    conn = get_conn()
    r13 = simulate(conn, BT_START, BT_END, use_h5=False)
    conn.close()
    
    conn2 = get_conn()
    r14 = simulate(conn2, BT_START, BT_END, use_h5=True)
    conn2.close()
    
    print(f"\n{'='*55}")
    print(f"  📋 V13 vs V14 终极对比")
    print(f"{'='*55}")
    print(f"  {'指标':12s} {'V13':>12s} {'V14':>12s} {'变化':>12s}")
    print(f"  {'─'*48}")
    for k, l in zip(['total_return','max_dd','carmar','sharpe','win_rate','profit_ratio','n_trades','final_capital'],
                    ['总收益率%','最大回撤%','卡玛比率','夏普比率','胜率%','盈亏比','交易次数','期末资金']):
        v13 = r13.get(k,0); v14 = r14.get(k,0)
        if isinstance(v13,(int,float)):
            diff = v14-v13
            pc = diff/v13*100 if v13!=0 else 0
            diff_str = f'{diff:+d}' if k=='n_trades' else f'{diff:+7.2f} ({pc:+5.1f}%)'
            print(f"  {l:12s} {v13:>12.2f} {v14:>12.2f} {diff_str:>12s}")
    print(f"\n  总耗时: {time.time()-t0:.0f}s")
