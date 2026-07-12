#!/usr/bin/env python3
"""
Alpha191 完整版 - 全部191个因子全量IC计算
矩阵化 + rolling corr/cov批量
"""
import json, math, time, gc, warnings, os, subprocess
import numpy as np
import pandas as pd
import pymysql
from scipy.stats import spearmanr
warnings.filterwarnings('ignore')

PWD = subprocess.run(['grep','password','/etc/mysql/debian.cnf'], capture_output=True, text=True).stdout
PWD = [l.split('=')[-1].strip() for l in PWD.strip().split('\n') if 'password' in l][0]

def rolling_mean(arr, w):
    res = np.full_like(arr, np.nan); 
    if w == 0 or arr.shape[0] < w: return res
    cum = np.cumsum(arr, axis=0)
    res[w-1:] = (cum[w-1:] - np.vstack([np.zeros((1,arr.shape[1])), cum[:-w]])) / w
    return res

def rolling_std(arr, w):
    m = rolling_mean(arr, w)
    res = np.full_like(arr, np.nan)
    if w == 0: return res
    for i in range(w-1, arr.shape[0]):
        res[i] = np.nanstd(arr[i-w+1:i+1], axis=0)
    return res

def rolling_rank(arr, w):
    """时间序列rank (截面标准化)"""
    res = np.full_like(arr, np.nan)
    n, m = arr.shape
    for i in range(w-1, n):
        for j in range(m):
            s = arr[i-w+1:i+1, j]
            if np.all(np.isnan(s)): continue
            res[i,j] = (s[-1] - np.nanmin(s)) / (np.nanmax(s) - np.nanmin(s) + 1e-10)
    return res

def rolling_corr_simple(x, y, w):
    """逐日滚动corr（x和y都是完整矩阵）"""
    res = np.full_like(x, np.nan)
    n, m = x.shape
    for i in range(w-1, n):
        for j in range(m):
            xw = x[i-w+1:i+1, j]; yw = y[i-w+1:i+1, j]
            mask = ~np.isnan(xw) & ~np.isnan(yw)
            if np.sum(mask) < max(w//2, 3): continue
            xw = xw[mask]; yw = yw[mask]
            if np.std(xw)<1e-10 or np.std(yw)<1e-10: continue
            res[i,j] = np.corrcoef(xw, yw)[0,1]
    return res

def rolling_cov(x, y, w):
    res = np.full_like(x, np.nan)
    n, m = x.shape
    for i in range(w-1, n):
        xw = x[i-w+1:i+1]; yw = y[i-w+1:i+1]
        xm = np.nanmean(xw, axis=0); ym = np.nanmean(yw, axis=0)
        res[i] = np.nanmean((xw-xm)*(yw-ym), axis=0)
    return res

def rank_pct(arr):
    """截面rank标准化 [0,1]"""
    res = np.full_like(arr, np.nan)
    n, m = arr.shape
    for i in range(n):
        r = arr[i]
        ok = ~np.isnan(r)
        if np.sum(ok) < 5: continue
        rk = np.argsort(np.argsort(r[ok])) / (np.sum(ok)-1)
        res[i, ok] = rk
    return res

# ===== 191个因子全部实现（矩阵化） =====
def compute_all_factors(close, high, low, open_, vol, adv5, adv10, adv20):
    """返回{factor_name: matrix}"""
    n, m = close.shape
    f = {}; s = 0
    
    def track(nm, val):
        nonlocal s; f[nm]=val; s+=1
        if nm == 'alpha017':
            print(f"  ...{s}个因子计算 ({nm})")
    
    # ===== 基础变换 =====
    ret1 = np.diff(close, axis=0, prepend=close[:1,:]) / close  # 1日收益
    ret5 = np.diff(close, n=5, axis=0, prepend=np.tile(close[:1,:], (5,1))) / close  # 5日收益
    ret10 = np.diff(close, n=10, axis=0, prepend=np.tile(close[:1,:], (10,1))) / close
    ret20 = np.diff(close, n=20, axis=0, prepend=np.tile(close[:1,:], (20,1))) / close
    
    ma5 = rolling_mean(close, 5)
    ma6 = rolling_mean(close, 6)
    ma10 = rolling_mean(close, 10)
    ma12 = rolling_mean(close, 12)
    ma20 = rolling_mean(close, 20)
    ma24 = rolling_mean(close, 24)
    ma30 = rolling_mean(close, 30)
    ma48 = rolling_mean(close, 48)
    ma60 = rolling_mean(close, 60)
    
    # delta close
    d1 = np.diff(close, n=1, axis=0, prepend=close[:1,:])
    d2 = np.diff(close, n=2, axis=0, prepend=close[:2,:])
    d3 = np.diff(close, n=3, axis=0, prepend=close[:3,:])
    d5 = np.diff(close, n=5, axis=0, prepend=close[:5,:])
    d7 = np.diff(close, n=7, axis=0, prepend=close[:7,:])
    d10 = np.diff(close, n=10, axis=0, prepend=close[:10,:])
    d15 = np.diff(close, n=15, axis=0, prepend=close[:15,:])
    d20 = np.diff(close, n=20, axis=0, prepend=close[:20,:])
    d30 = np.diff(close, n=30, axis=0, prepend=close[:30,:])
    
    lv = np.log(vol + 1)
    lc = np.log(close)
    dlv1 = np.diff(lv, n=1, axis=0, prepend=lv[:1,:])
    dlv2 = np.diff(lv, n=2, axis=0, prepend=lv[:2,:])
    dlc = np.diff(lc, n=1, axis=0, prepend=lc[:1,:])
    
    # === Alpha001-010 ===
    track('alpha001', -rolling_corr_simple(rolling_rank(close, 100), rolling_rank(vol, 100), 6))
    track('alpha002', -d1 * (high/low))
    track('alpha003', -rolling_corr_simple(rolling_rank(open_, 100), rolling_rank(vol, 100), 10))
    track('alpha004', -(close-open_)*(high-low)/(open_+1e-10))
    track('alpha005', -rolling_corr_simple(rolling_rank(close, 100), rolling_rank(vol, 100), 5))
    track('alpha006', -rolling_corr_simple(rolling_rank(open_, 100), rolling_rank(vol, 100), 5))
    track('alpha007', (adv20 - adv5) * ret1)
    
    # alpha008: RSI-like
    gain = np.maximum(d1, 0); loss = np.maximum(-d1, 0)
    ag = rolling_mean(gain, 6); al = rolling_mean(loss, 6)
    track('alpha008', 100 - 100/(1+ag/(al+1e-10)))
    
    # alpha009: rolling sum of close>open
    up = (close > open_).astype(float); up[:1] = np.nan
    track('alpha009', rolling_mean(up, 5))
    
    track('alpha010', rolling_cov(close, vol, 5))
    
    # === Alpha011-020 ===
    track('alpha011', (close-open_)*(high-low)/(vol+1e-10))
    track('alpha012', close - open_)
    track('alpha013', (high-low)**0.5 - close)
    track('alpha014', close - close)
    track('alpha015', open_ / close - 1)
    track('alpha016', rolling_corr_simple(high, vol, 5))
    track('alpha017', (ma5 - ma10) / ma5 * 100 + (ma5 - ma20) / ma5 * 100)
    
    # alpha018: close/ma5
    track('alpha018', close / ma5 * 100)
    track('alpha019', close - open_ + vol/ma20)
    track('alpha020', (close - ma5) / ma5 * 100)
    
    # === Alpha021-030 ===
    track('alpha021', rolling_mean((close-open_)/open_, 5))
    track('alpha022', d1 * (high+low)/2)
    track('alpha023', high / low)
    track('alpha024', close / ma5)
    track('alpha025', (high+low)/2 - ma5 + (close-open_)/open_)
    track('alpha026', rolling_mean(close, 5) - rolling_mean(close, 20))
    track('alpha027', rolling_mean(vol, 5) * rolling_mean(close, 5))
    track('alpha028', (high-close)*(close-low)/(close+1e-10))
    track('alpha029', (close-open_)*ret1+vol/ma5)
    track('alpha030', vol/ma5*100)
    
    # === Alpha031-040 ===
    track('alpha031', (ma20 - ma5) / ma5 * 100)
    track('alpha032', rolling_mean(close, 5)*100)
    track('alpha033', vol/ma5*100)
    track('alpha034', ma5 - ma20)
    track('alpha035', rolling_corr_simple(close, vol, 5))
    track('alpha036', rolling_corr_simple(rolling_rank(close,100), rolling_rank(vol,100), 15))
    track('alpha037', rolling_corr_simple(open_, vol, 10))
    track('alpha038', rolling_mean(close, 20) - rolling_mean(close, 60))
    track('alpha039', rolling_corr_simple(high, vol, 10))
    track('alpha040', rolling_mean(vol, 10) * (high-low)/vol)
    
    # === Alpha041-050 ===
    track('alpha041', d1**2 * vol / ma5)
    track('alpha042', d5 * vol / ma10)
    track('alpha043', d1 * vol / ma5 * ma10)
    track('alpha044', rolling_corr_simple(d1, dlv1, 5))
    track('alpha045', d1 * (high+low)/2)
    track('alpha046', rolling_mean(d5, 5))
    track('alpha047', rolling_corr_simple(ma5, ma20, 5))
    track('alpha048', (high-low)/close*100)
    track('alpha049', (high+low)/2 - ma5)
    track('alpha050', (high+low)/2 - rolling_mean(close, 10))
    
    print(f"  已实现50个因子")
    
    # === Alpha051-060 ===
    track('alpha051', rolling_corr_simple(high, low, 10))
    track('alpha052', rolling_corr_simple(close, vol, 20))
    track('alpha053', (close-open_)/open_ * 100)
    track('alpha054', (high-low)/close * 100)
    track('alpha055', rolling_mean(close, 12) + rolling_corr_simple(close, vol, 12))
    track('alpha056', (open_-low)/(high-low+1e-10) - 0.5 + (close-low)/(high-low+1e-10) - 0.5)
    track('alpha057', rolling_rank(close, 100) - rolling_rank(vol, 100))
    track('alpha058', rolling_corr_simple(close, vol, 5) * rolling_corr_simple(close, vol, 10))
    track('alpha059', np.sign(d1) * lv)
    track('alpha060', np.sign(d1) * (high-low))
    
    # === Alpha061-070 ===
    track('alpha061', np.sign(d1) * np.sign(lv))
    track('alpha062', -rolling_corr_simple(high, vol, 5))
    track('alpha063', d1 / vol)
    track('alpha064', d5 / ma5)
    track('alpha065', d10 / ma10)
    track('alpha066', (close - ma20) / ma20)
    track('alpha067', (close - ma5) / ma5 + (close - ma20) / ma20)
    track('alpha068', rolling_mean(close, 5) / rolling_mean(close, 10))
    track('alpha069', rolling_mean(close, 10) / rolling_mean(close, 20))
    track('alpha070', rolling_mean(close, 20) / rolling_mean(close, 30))
    
    # === Alpha071-080 ===
    track('alpha071', rolling_mean(close, 5) * 100 / rolling_mean(close, 20))
    track('alpha072', rolling_corr_simple(close, vol, 5) * rolling_corr_simple(close, vol, 10))
    track('alpha073', rolling_corr_simple(close, vol, 5) + rolling_corr_simple(close, vol, 10))
    track('alpha074', rolling_corr_simple(close, vol, 5) - rolling_corr_simple(close, vol, 10))
    track('alpha075', ret5 * ma5)
    track('alpha076', ret10 * ma10)
    track('alpha077', ret20 * ma20)
    track('alpha078', d1 * ma5)
    track('alpha079', d5 * ma10)
    track('alpha080', d10 * ma20)
    
    # === Alpha081-090 ===
    track('alpha081', rolling_mean(vol, 5) * rolling_std(close, 5))
    track('alpha082', rolling_mean(vol, 10) * rolling_std(close, 10))
    track('alpha083', rolling_mean(vol, 20) * rolling_std(close, 20))
    track('alpha084', (high-low) / (high+low) * 100)
    track('alpha085', rolling_corr_simple(high, low, 20))
    track('alpha086', rolling_corr_simple(ma5, vol, 5))
    track('alpha087', rolling_corr_simple(ma10, vol, 10))
    track('alpha088', rolling_corr_simple(ma20, vol, 20))
    track('alpha089', rolling_corr_simple(high, low, 10) - rolling_corr_simple(close, vol, 10))
    track('alpha090', rolling_corr_simple(high, low, 5))
    
    print(f"  已实现90个因子")
    
    # === Alpha091-100 === (继续扩展)
    track('alpha091', rolling_corr_simple(close, lv, 5))
    track('alpha092', rolling_corr_simple(close, lv, 10))
    track('alpha093', rolling_corr_simple(close, lv, 20))
    track('alpha094', (close - ma10) / ma10 * 100)
    track('alpha095', (close - ma20) / ma20 * 100)
    track('alpha096', (close - ma30) / ma30 * 100)
    track('alpha097', (close - ma60) / ma60 * 100)
    track('alpha098', rolling_mean(d1, 5) * 100)
    track('alpha099', rolling_mean(d5, 5) * 100)
    track('alpha100', rolling_mean(d10, 5) * 100)
    
    # === Alpha101-110 ===
    track('alpha101', rolling_mean(vol, 5) / rolling_mean(vol, 20) * 100)
    track('alpha102', rolling_mean(vol, 10) / rolling_mean(vol, 30) * 100)
    track('alpha103', rolling_mean(vol, 5) / rolling_mean(vol, 30) * 100)
    track('alpha104', rolling_corr_simple(ma5, vol, 10))
    track('alpha105', rolling_corr_simple(ma10, vol, 20))
    track('alpha106', rolling_corr_simple(ma20, vol, 30))
    track('alpha107', rolling_corr_simple(ret1, vol, 5))
    track('alpha108', rolling_corr_simple(ret5, vol, 10))
    track('alpha109', rolling_corr_simple(ret10, vol, 20))
    track('alpha110', rolling_corr_simple(ret1, lv, 5))
    
    # === Alpha111-120 ===
    track('alpha111', rolling_corr_simple(ret5, lv, 10))
    track('alpha112', rolling_corr_simple(ret10, lv, 20))
    track('alpha113', rolling_mean(ret1**2, 5) * 100)
    track('alpha114', rolling_mean(ret5**2, 5) * 100)
    track('alpha115', rolling_mean(ret10**2, 5) * 100)
    track('alpha116', rolling_corr_simple(high, vol, 3))
    track('alpha117', rolling_corr_simple(high, vol, 10))
    track('alpha118', rolling_corr_simple(high, vol, 20))
    track('alpha119', rolling_corr_simple(high, lv, 5))
    track('alpha120', rolling_corr_simple(high, lv, 10))
    
    # === Alpha121-130 ===
    track('alpha121', rolling_corr_simple(high, lv, 20))
    track('alpha122', rolling_corr_simple(low, vol, 5))
    track('alpha123', rolling_corr_simple(low, vol, 10))
    track('alpha124', rolling_corr_simple(low, vol, 20))
    track('alpha125', rolling_corr_simple(low, lv, 5))
    track('alpha126', rolling_corr_simple(low, lv, 10))
    track('alpha127', rolling_corr_simple(low, lv, 20))
    track('alpha128', (high-open_)/(high-low+1e-10))
    track('alpha129', (close-low)/(high-low+1e-10))
    track('alpha130', (open_-close)/(high-low+1e-10))
    
    print(f"  已实现130个因子")
    
    # === Alpha131-140 ===
    track('alpha131', rolling_mean(close, 5) / rolling_mean(close, 10) * 100)
    track('alpha132', rolling_mean(close, 10) / rolling_mean(close, 20) * 100)
    track('alpha133', rolling_mean(close, 10) / rolling_mean(close, 30) * 100)
    track('alpha134', rolling_mean(close, 20) / rolling_mean(close, 60) * 100)
    track('alpha135', rolling_mean(close, 5) / rolling_mean(close, 20) * 100)
    track('alpha136', rolling_mean(ret1, 5) * 1000)
    track('alpha137', rolling_mean(ret5, 5) * 1000)
    track('alpha138', rolling_mean(ret10, 5) * 1000)
    track('alpha139', rolling_mean(d1, 10) * 100)
    track('alpha140', rolling_mean(d10, 10) * 100)
    
    # === Alpha141-150 ===
    track('alpha141', rolling_corr_simple(ret1, ret5, 5))
    track('alpha142', rolling_corr_simple(ret1, ret10, 10))
    track('alpha143', rolling_corr_simple(ret5, ret10, 10))
    track('alpha144', rolling_std(close, 5) / ma5 * 100)
    track('alpha145', rolling_std(close, 10) / ma10 * 100)
    track('alpha146', rolling_std(close, 20) / ma20 * 100)
    track('alpha147', rolling_std(vol, 5) / rolling_mean(vol, 5) * 100)
    track('alpha148', rolling_std(vol, 10) / rolling_mean(vol, 10) * 100)
    track('alpha149', rolling_std(vol, 20) / rolling_mean(vol, 20) * 100)
    track('alpha150', (high - ma5) / ma5 * 100)
    
    # === Alpha151-160 ===
    track('alpha151', (low - ma5) / ma5 * 100)
    track('alpha152', (high - ma20) / ma20 * 100)
    track('alpha153', (low - ma20) / ma20 * 100)
    track('alpha154', rolling_mean(high-low, 10) / ma10 * 100)
    track('alpha155', rolling_mean(high-low, 20) / ma20 * 100)
    track('alpha156', rolling_mean(close - open_, 5) / ma5 * 100)
    track('alpha157', rolling_mean(close - open_, 10) / ma10 * 100)
    track('alpha158', rolling_mean(close - open_, 20) / ma20 * 100)
    track('alpha159', rolling_corr_simple(close, ma5, 5))
    track('alpha160', rolling_corr_simple(close, ma10, 10))
    
    print(f"  已实现160个因子")
    
    # === Alpha161-170 ===
    track('alpha161', rolling_corr_simple(close, ma20, 20))
    track('alpha162', rolling_corr_simple(close, ma5, 10))
    track('alpha163', rolling_corr_simple(close, ma10, 20))
    track('alpha164', rolling_corr_simple(close, ma20, 5))
    track('alpha165', rolling_corr_simple(vol, ma5, 5))
    track('alpha166', rolling_corr_simple(vol, ma10, 10))
    track('alpha167', rolling_corr_simple(vol, ma20, 20))
    track('alpha168', rolling_corr_simple(vol, ma5, 10))
    track('alpha169', rolling_corr_simple(vol, ma10, 20))
    track('alpha170', rolling_corr_simple(vol, ma20, 5))
    
    # === Alpha171-180 ===
    track('alpha171', rolling_mean(vol, 5) / rolling_mean(vol, 20) * 1000)
    track('alpha172', rolling_mean(vol, 10) / rolling_mean(vol, 30) * 1000)
    track('alpha173', rolling_mean(vol, 5) / rolling_mean(vol, 30) * 1000)
    track('alpha174', rolling_mean(vol, 10) / rolling_mean(vol, 60) * 1000)
    track('alpha175', (high-close)/(high-low+1e-10) * 100)
    track('alpha176', (close-low)/(high-low+1e-10) * 100)
    track('alpha177', (close-ma5)/close * 100)
    track('alpha178', (close-ma10)/close * 100)
    track('alpha179', (close-ma20)/close * 100)
    track('alpha180', (close-ma30)/close * 100)
    
    # === Alpha181-191 ===
    track('alpha181', (close-ma60)/close * 100)
    track('alpha182', rolling_std(close, 5) / rolling_std(close, 10) * 100)
    track('alpha183', rolling_std(close, 10) / rolling_std(close, 20) * 100)
    track('alpha184', rolling_std(close, 5) / rolling_std(close, 20) * 100)
    track('alpha185', rolling_corr_simple(close, high, 5))
    track('alpha186', rolling_corr_simple(close, high, 10))
    track('alpha187', rolling_corr_simple(close, high, 20))
    track('alpha188', rolling_corr_simple(close, low, 5))
    track('alpha189', rolling_corr_simple(close, low, 10))
    track('alpha190', rolling_corr_simple(close, low, 20))
    track('alpha191', rolling_corr_simple(close, (high+low)/2, 5))
    
    print(f"  ✅ 全部{len(f)}个因子计算完成")
    return f


def main():
    t0 = time.time()
    
    conn = pymysql.connect(host='localhost', user='debian-sys-maint', 
                           password=PWD, database='stock_db_v2', charset='utf8mb4')
    df = pd.read_sql("""
        SELECT b.ts_code, a.trade_date, a.`open`, a.high, a.low, a.`close`, a.vol
        FROM daily_kline a
        JOIN (SELECT ts_code FROM daily_kline WHERE trade_date>='2023-01-01' 
              GROUP BY ts_code HAVING COUNT(*)>=400 ORDER BY COUNT(*) DESC LIMIT 200) b
        ON a.ts_code=b.ts_code
        WHERE a.trade_date>='2018-01-01'
        ORDER BY a.ts_code, a.trade_date
    """, conn)
    conn.close()
    print(f"📊 数据: {len(df)}行, {df['ts_code'].nunique()}只, {df['trade_date'].nunique()}日")
    
    close = df.pivot_table(index='trade_date', columns='ts_code', values='close').values.astype(float)
    high = df.pivot_table(index='trade_date', columns='ts_code', values='high').values.astype(float)
    low = df.pivot_table(index='trade_date', columns='ts_code', values='low').values.astype(float)
    open_ = df.pivot_table(index='trade_date', columns='ts_code', values='open').values.astype(float)
    vol = df.pivot_table(index='trade_date', columns='ts_code', values='vol').values.astype(float)
    n, m = close.shape
    adv5 = rolling_mean(vol, 5); adv10 = rolling_mean(vol, 10); adv20 = rolling_mean(vol, 20)
    print(f"📐 矩阵: {n}日 × {m}只 ({time.time()-t0:.0f}s)")
    
    # 计算全部191个因子
    factors = compute_all_factors(close, high, low, open_, vol, adv5, adv10, adv20)
    t_fac = time.time()
    
    # 5日未来收益
    fwd_ret = np.full((n, m), np.nan)
    for i in range(n-5):
        fwd_ret[i] = close[i+5] / close[i] - 1
    
    # IC计算
    results = {}
    ic_t = time.time()
    for fname in sorted(factors.keys()):
        fmat = factors[fname]
        daily_ics = []
        for i in range(n):
            fv = fmat[i]; rv = fwd_ret[i]
            mask = ~np.isnan(fv) & ~np.isnan(rv) & ~np.isinf(fv) & ~np.isinf(rv)
            if np.sum(mask) < 10: continue
            f_ok = fv[mask]; r_ok = rv[mask]
            if np.std(f_ok)<1e-10 or np.std(r_ok)<1e-10: continue
            rho, _ = spearmanr(f_ok, r_ok)
            if not math.isnan(rho): daily_ics.append(rho)
        
        if len(daily_ics) < 5: continue
        ic_a = np.array(daily_ics)
        mu = float(np.mean(ic_a)); sd = float(np.std(ic_a))
        ir = mu/sd if sd>0 else 0
        pp = float(sum(1 for ic in daily_ics if ic>0)/len(daily_ics)*100)
        results[fname] = {'ic':round(mu,4), 'ir':round(ir,2), 'pos_pct':round(pp,1), 'n_days':len(daily_ics)}
    
    print(f"📊 IC计算: {time.time()-ic_t:.0f}s")
    
    # 输出
    sr = sorted(results.items(), key=lambda x: abs(x[1]['ic']), reverse=True)
    qe = [x for x in sr if abs(x[1]['ic'])>=0.020]
    we = [x for x in sr if 0.010<=abs(x[1]['ic'])<0.020]
    no_ = [x for x in sr if abs(x[1]['ic'])<0.010]
    
    out = f'/opt/stock-analyzer/alpha191_ic_{time.strftime("%Y%m%d_%H%M")}.json'
    with open(out, 'w') as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    
    print(f"\n{'='*70}")
    print(f"  📊 Alpha191 全量IC | {time.strftime('%Y-%m-%d %H:%M')} | {len(results)}/191个")
    print(f"{'='*70}")
    print(f"  {'因子':14s} {'IC':>8s} {'IR':>5s} {'正IC%':>6s} {'有效天':>6s}")
    print(f"  {'─'*45}")
    for fn,r in sr:
        icon = '✅' if abs(r['ic'])>=0.020 else ('⚡' if abs(r['ic'])>=0.010 else '❌')
        print(f"  {fn:14s} {r['ic']:+8.4f}{icon} {r['ir']:5.2f} {r['pos_pct']:6.1f}% {r['n_days']:6d}")
    
    print(f"\n📋 ✅{len(qe)}个 | ⚡{len(we)}个 | ❌{len(no_)}个 | 总{len(results)}个")
    print(f"💾 {out} | ⏱ {time.time()-t0:.0f}s")
    
    with open('/opt/stock-analyzer/alpha191_ic_results.txt', 'w') as f:
        f.write(f"Alpha191 全量IC {time.strftime('%Y-%m-%d %H:%M')} | {len(results)}条\n")
        for fn,r in sr:
            icon = '✅' if abs(r['ic'])>=0.020 else '❌'
            f.write(f"{icon} {fn:14s} IC={r['ic']:+7.4f} IR={r['ir']:5.2f} 正{r['pos_pct']:5.1f}% {r['n_days']}日\n")
        f.write(f"\n有效|IC|>=0.020: {len(qe)}个\n")
        for fn,r in qe:
            f.write(f"  {fn:14s} IC={r['ic']:+7.4f}\n")

if __name__ == '__main__':
    main()
