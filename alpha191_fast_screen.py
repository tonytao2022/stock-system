#!/usr/bin/env python3
"""
Alpha191 快速筛选 - 逐股循环，SQL分批，向量化计算
内存友好版（300只×1200天，约300万行）
"""
import os, sys, math, time, json, gc
import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from collections import defaultdict
import subprocess

_pw = None
def db_pass():
    global _pw
    if _pw: return _pw
    r = subprocess.run(['grep','password','/etc/mysql/debian.cnf'], capture_output=True, text=True)
    for line in r.stdout.strip().split('\n'):
        if 'password' in line:
            _pw = line.split('=')[-1].strip()
            return _pw
    return ''

import pymysql

ALPHA_NAMES = {
    'alpha001':'量价背离','alpha002':'日内振幅变化','alpha003':'开盘量价背离',
    'alpha004':'收盘组合','alpha014':'5日涨幅','alpha018':'5日收盘比',
    'alpha020':'6日涨幅','alpha031':'12日偏离度','alpha034':'12日均线比',
    'alpha036':'量价秩相关','alpha040':'量比功率','alpha043':'净量因子',
    'alpha046':'多均线位置','alpha055':'随机指标','alpha060':'量价右偏',
    'alpha062':'高量负相关','alpha089':'高低量相关','alpha005':'量价时序',
    'alpha007':'日内量与振幅','alpha008':'量价RS',
}

def load_stock_kline(code):
    conn = pymysql.connect(host='localhost', user='debian-sys-maint', password=db_pass(), 
                           db='stock_db_v2', charset='utf8mb4')
    df = pd.read_sql(f"""
        SELECT trade_date, `open`, high, low, `close`, vol FROM daily_kline 
        WHERE ts_code='{code}' AND trade_date >= '2020-01-01'
        ORDER BY trade_date
    """, conn)
    conn.close()
    if len(df) < 100: return None
    df['log_vol'] = np.log(df['vol'].clip(lower=1))
    return df

def compute_factors(df):
    """计算一只股票的15个主要Alpha因子值"""
    n = len(df)
    c = df['close'].values.astype(float)
    o = df['open'].values.astype(float)
    h = df['high'].values.astype(float)
    l = df['low'].values.astype(float)
    v = df['vol'].values.astype(float)
    lv = df['log_vol'].values.astype(float)
    
    fac = {}
    
    # alpha062: -corr(high, vol, 5)
    corr_hv = np.full(n, np.nan)
    for i in range(4, n):
        if np.std(h[i-4:i+1])>0 and np.std(v[i-4:i+1])>0:
            corr_hv[i] = -np.corrcoef(h[i-4:i+1], v[i-4:i+1])[0,1]
    fac['alpha062'] = corr_hv
    
    # alpha046: (ma3+ma6+ma12+ma24)/(4*close)
    v46 = np.full(n, np.nan)
    for i in range(23, n):
        ma3 = np.mean(c[i-2:i+1])
        ma6 = np.mean(c[i-5:i+1])
        ma12 = np.mean(c[i-11:i+1])
        ma24 = np.mean(c[i-23:i+1])
        v46[i] = (ma3+ma6+ma12+ma24)/(4*c[i]+1e-10)
    fac['alpha046'] = v46
    
    # alpha034: MA12/close
    fac['alpha034'] = np.full(n, np.nan)
    for i in range(11, n):
        fac['alpha034'][i] = np.mean(c[i-11:i+1]) / (c[i]+1e-10)
    
    # alpha020: (close-MA6)/MA6*100
    fac['alpha020'] = np.full(n, np.nan)
    for i in range(5, n):
        ma6 = np.mean(c[i-5:i+1])
        fac['alpha020'][i] = (c[i]-ma6)/ma6*100 if ma6>0 else np.nan
    
    # alpha031: (close-MA12)/MA12*100
    fac['alpha031'] = np.full(n, np.nan)
    for i in range(11, n):
        ma12 = np.mean(c[i-11:i+1])
        fac['alpha031'][i] = (c[i]-ma12)/ma12*100 if ma12>0 else np.nan
    
    # alpha001: -corr(rank(delta(log(vol),2)), rank(close), 6)
    fac['alpha001'] = np.full(n, np.nan)
    if n > 7:
        dlv = np.diff(lv, prepend=lv[0])
        r_dlv = pd.Series(dlv).rank(pct=True).values
        r_c = pd.Series(c).rank(pct=True).values
        for i in range(5, n):
            if np.std(r_dlv[i-5:i+1])>0 and np.std(r_c[i-5:i+1])>0:
                fac['alpha001'][i] = -np.corrcoef(r_dlv[i-5:i+1], r_c[i-5:i+1])[0,1]
    
    # alpha003: -corr(rank(open), rank(vol), 10)
    fac['alpha003'] = np.full(n, np.nan)
    if n > 10:
        r_o = pd.Series(o).rank(pct=True).values
        r_v = pd.Series(v).rank(pct=True).values
        for i in range(9, n):
            if np.std(r_o[i-9:i+1])>0 and np.std(r_v[i-9:i+1])>0:
                fac['alpha003'][i] = -np.corrcoef(r_o[i-9:i+1], r_v[i-9:i+1])[0,1]
    
    # alpha036: corr(rank(close), rank(vol), 5)
    fac['alpha036'] = np.full(n, np.nan)
    if n > 5:
        r_c = pd.Series(c).rank(pct=True).values
        r_v = pd.Series(v).rank(pct=True).values
        for i in range(4, n):
            if np.std(r_c[i-4:i+1])>0 and np.std(r_v[i-4:i+1])>0:
                fac['alpha036'][i] = np.corrcoef(r_c[i-4:i+1], r_v[i-4:i+1])[0,1]
    
    # alpha089: 1-corr(close, vol, 13)
    fac['alpha089'] = np.full(n, np.nan)
    for i in range(12, n):
        if np.std(c[i-12:i+1])>0 and np.std(v[i-12:i+1])>0:
            fac['alpha089'][i] = 1 - np.corrcoef(c[i-12:i+1], v[i-12:i+1])[0,1]
    
    # alpha005: -corr(rank(close), rank(vol), 5) [变体]
    fac['alpha005'] = np.full(n, np.nan)
    if n > 5:
        r_c5 = pd.Series(c).rank(pct=True).values
        r_v5 = pd.Series(v).rank(pct=True).values
        for i in range(4, n):
            if np.std(r_c5[i-4:i+1])>0 and np.std(r_v5[i-4:i+1])>0:
                fac['alpha005'][i] = -np.corrcoef(r_c5[i-4:i+1], r_v5[i-4:i+1])[0,1]
    
    # alpha002: -delta((close-low)-(high-close))/(high-low)
    fac['alpha002'] = np.full(n, np.nan)
    for i in range(1, n):
        hl = h[i]-l[i]
        if hl > 1e-10:
            vi = ((c[i]-l[i])-(h[i]-c[i]))/hl
            vj = ((c[i-1]-l[i-1])-(h[i-1]-c[i-1]))/(h[i-1]-l[i-1]+1e-10)
            fac['alpha002'][i] = -(vi - vj)
    
    # alpha014: close - close.shift(5)
    fac['alpha014'] = np.full(n, np.nan)
    for i in range(5, n): fac['alpha014'][i] = c[i] - c[i-5]
    
    # alpha018: close/close.shift(5)
    fac['alpha018'] = np.full(n, np.nan)
    for i in range(5, n): fac['alpha018'][i] = c[i]/(c[i-5]+1e-10)
    
    # alpha004: (close-open)*(high-low)/open
    fac['alpha004'] = np.full(n, np.nan)
    for i in range(n):
        if o[i] > 0: fac['alpha004'][i] = (c[i]-o[i])*(h[i]-l[i])/o[i]
    
    # alpha040: sum(up_vol)/sum(down_vol)*100 over 25d
    fac['alpha040'] = np.full(n, np.nan)
    for i in range(24, n):
        up = sum(v[i-24+j+1] for j in range(24) if c[i-24+j+1] > c[i-24+j])
        dn = sum(v[i-24+j+1] for j in range(24) if c[i-24+j+1] <= c[i-24+j])
        fac['alpha040'][i] = up/(dn+1)*100
    
    # alpha043: net signed volume over 5d
    fac['alpha043'] = np.full(n, np.nan)
    for i in range(5, n):
        net = sum(v[i-5+j+1] if c[i-5+j+1] > c[i-5+j] else -v[i-5+j+1] for j in range(5))
        fac['alpha043'][i] = net
    
    # alpha055: (close-LL)/(HH-LL)*100 over 12d
    fac['alpha055'] = np.full(n, np.nan)
    for i in range(11, n):
        hh = np.max(h[i-11:i+1]); ll = np.min(l[i-11:i+1])
        if hh > ll: fac['alpha055'][i] = (c[i]-ll)/(hh-ll)*100
    
    return fac

def main():
    t0 = time.time()
    conn = pymysql.connect(host='localhost', user='debian-sys-maint', password=db_pass(),
                           db='stock_db_v2', charset='utf8mb4')
    cur = conn.cursor()
    cur.execute("""
        SELECT ts_code FROM daily_kline WHERE trade_date >= '2023-01-01'
        GROUP BY ts_code HAVING COUNT(*) >= 200
        ORDER BY COUNT(*) DESC LIMIT 300
    """)
    codes = [r[0] for r in cur.fetchall()]
    conn.close()
    print(f"📊 {len(codes)}只股票 (2023~2026)")
    
    # 用字典逐日收集factor值和fwd_ret
    # daily_pool[factor_name][trade_date] = [(fval, fwd_ret), ...]
    daily_pool = defaultdict(lambda: defaultdict(list))
    
    n_done = 0
    for code in codes:
        df = load_stock_kline(code)
        if df is None or len(df) < 120: continue
        
        factors = compute_factors(df)
        n_rows = len(df)
        
        # 计算5日收益
        fwd_ret = np.full(n_rows, np.nan)
        for i in range(n_rows - 5):
            fc = df['close'].iloc[i+5]
            tc = df['close'].iloc[i]
            fwd_ret[i] = fc/tc - 1 if tc > 0 else np.nan
        
        # 收集到daily_pool
        for fname, fvals in factors.items():
            for i in range(n_rows):
                if math.isnan(fvals[i]) or math.isnan(fwd_ret[i]): continue
                td = str(df['trade_date'].iloc[i])
                daily_pool[fname][td].append((fvals[i], fwd_ret[i]))
        
        n_done += 1
        if n_done % 50 == 0:
            eta = (time.time()-t0)/n_done*(len(codes)-n_done)
            print(f"  [{n_done}/{len(codes)}] {time.time()-t0:.0f}s ETA{eta:.0f}s")
    
    print(f"\n✅ 全部计算完成 ({time.time()-t0:.0f}s)")
    
    # 计算IC
    print(f"\n{'='*70}")
    print(f"  📊 Alpha191 因子 IC 验证 (5日持有)")
    print(f"  {n_done}只股票 | {time.time()-t0:.0f}s")
    print(f"{'='*70}")
    print(f"  {'因子':14s} {'中文名':12s} {'平均IC':>8s} {'IR':>5s} {'正IC%':>6s} {'有效天':>6s}")
    print(f"  {'─'*56}")
    
    results = {}
    for fname in sorted(ALPHA_NAMES.keys()):
        pool = daily_pool.get(fname, {})
        if not pool: continue
        
        daily_ics = []
        for td, pairs in pool.items():
            if len(pairs) < 10: continue
            fvals = np.array([p[0] for p in pairs])
            rvals = np.array([p[1] for p in pairs])
            if np.std(fvals) < 1e-10 or np.std(rvals) < 1e-10: continue
            rho, _ = spearmanr(fvals, rvals)
            if not math.isnan(rho): daily_ics.append(rho)
        
        if len(daily_ics) < 5: continue
        ic_arr = np.array(daily_ics)
        mean_ic = float(np.mean(ic_arr))
        std_ic = float(np.std(ic_arr))
        ir = mean_ic / std_ic if std_ic > 0 else 0
        pos_pct = float(sum(1 for ic in daily_ics if ic > 0) / len(daily_ics) * 100)
        
        m = abs(mean_ic)
        icon = '✅' if m >= 0.020 else ('⚡' if m >= 0.010 else '❌')
        print(f"  {fname:14s} {ALPHA_NAMES[fname]:12s} {mean_ic:+8.4f}{icon} {ir:5.2f} {pos_pct:6.1f}% {len(daily_ics):6d}")
        
        results[fname] = {
            'name': ALPHA_NAMES[fname],
            'ic': round(mean_ic, 4),
            'ir': round(ir, 2),
            'pos_pct': round(pos_pct, 1),
            'n_days': len(daily_ics),
        }
    
    # 输出
    out_lines = []; out_lines.append(f"📊 Alpha191 因子IC筛选 ({time.strftime('%Y-%m-%d %H:%M')})")
    out_lines.append(f"  {n_done}只股票 | 5日持有期")
    
    sorted_res = sorted(results.items(), key=lambda x: abs(x[1]['ic']), reverse=True)
    qualifying = [x for x in sorted_res if abs(x[1]['ic']) >= 0.020]
    weak = [x for x in sorted_res if 0.010 <= abs(x[1]['ic']) < 0.020]
    bad = [x for x in sorted_res if abs(x[1]['ic']) < 0.010]
    
    for label, items in [('✅ 有效 (|IC|>=0.020)', qualifying),
                         ('⚡ 弱相关 (0.010~0.019)', weak),
                         ('❌ 无效 (<0.010)', bad)]:
        out_lines.append(f"\n── {label} ──")
        if items:
            for fn, r in items:
                out_lines.append(f"  {fn:14s} {r['name']:12s} IC={r['ic']:+7.4f} IR={r['ir']:5.2f} 正IC{r['pos_pct']:5.1f}% N{r['n_days']}日")
    
    out_lines.append(f"\n📋 汇总: ✅{len(qualifying)}个 | ⚡{len(weak)}个 | ❌{len(bad)}个 | 总计{len(sorted_res)}个")
    
    out_path = '/opt/stock-analyzer/alpha191_ic_results.txt'
    with open(out_path, 'w') as f:
        f.write('\n'.join(out_lines))
    
    json_path = f'/opt/stock-analyzer/alpha191_ic_{time.strftime("%Y%m%d_%H%M")}.json'
    with open(json_path, 'w') as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    
    print(f"\n📁 {out_path}")
    print(f"\n⏱  总耗时: {time.time()-t0:.0f}s")

if __name__ == '__main__':
    main()
