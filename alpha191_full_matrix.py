#!/usr/bin/env python3
"""
Alpha191 全量因子IC - 矩阵版
批量加载300只K线到pivot矩阵，向量化计算因子
"""
import json, math, time, gc, warnings
import numpy as np
import pandas as pd
import pymysql, subprocess
from scipy.stats import spearmanr
from collections import defaultdict
warnings.filterwarnings('ignore')

PWD = subprocess.run(['grep','password','/etc/mysql/debian.cnf'], capture_output=True, text=True).stdout
PWD = [l.split('=')[-1].strip() for l in PWD.strip().split('\n') if 'password' in l][0]

CONN_CFG = dict(host='localhost', user='debian-sys-maint', password=PWD, database='stock_db_v2', charset='utf8mb4')

ALL_NAMES = {
    'alpha001':'量价背离','alpha002':'日内振幅变化','alpha003':'开盘量价背离',
    'alpha004':'收盘组合','alpha005':'量价时序','alpha014':'5日涨幅',
    'alpha018':'5日收盘比','alpha020':'6日涨幅','alpha031':'12日偏离度',
    'alpha034':'12日均线比','alpha036':'量价秩相关','alpha040':'量比功率',
    'alpha043':'净量因子','alpha046':'多均线位置','alpha055':'随机指标',
    'alpha062':'高量负相关','alpha089':'高低量相关',
    'alpha006':'量价秩相关R','alpha007':'日内量与振幅','alpha008':'量价RS',
    'alpha010':'cov量价','alpha011':'量价位置','alpha012':'开盘量比',
    'alpha015':'开盘跳空','alpha016':'日内量价秩','alpha017':'量价倒数',
    'alpha022':'量价差','alpha023':'高低比量','alpha024':'收盘量价',
    'alpha025':'量与振幅','alpha026':'累积量比','alpha027':'量比',
    'alpha028':'3日量比','alpha029':'开盘量','alpha030':'高低范围',
    'alpha033':'5日量比','alpha035':'量价变化','alpha038':'趋势量',
    'alpha041':'量价异常','alpha044':'量变化','alpha049':'趋势衰竭',
    'alpha056':'开盘位置',
}

def rolling_mean(arr, w):
    res = np.full_like(arr, np.nan)
    if w == 0: return res
    cum = np.cumsum(arr, axis=0)
    res[w-1:] = (cum[w-1:] - np.vstack([np.zeros((1,arr.shape[1])), cum[:-w]])) / w
    return res

def main():
    t0 = time.time()
    
    conn = pymysql.connect(**CONN_CFG)
    df = pd.read_sql("""
        SELECT b.ts_code, a.trade_date, a.`open`, a.high, a.low, a.`close`, a.vol
        FROM daily_kline a
        JOIN (SELECT ts_code FROM daily_kline WHERE trade_date>='2023-01-01' 
              GROUP BY ts_code HAVING COUNT(*)>=300 ORDER BY COUNT(*) DESC LIMIT 200) b
        ON a.ts_code=b.ts_code
        WHERE a.trade_date>='2019-01-01'
        ORDER BY a.ts_code, a.trade_date
    """, conn)
    conn.close()
    print(f"📊 数据: {len(df)}行, {df['ts_code'].nunique()}只, {df['trade_date'].nunique()}日 ({time.time()-t0:.0f}s)")
    
    # Pivot
    close = df.pivot_table(index='trade_date', columns='ts_code', values='close').values.astype(float)
    vol = df.pivot_table(index='trade_date', columns='ts_code', values='vol').values.astype(float)
    open_ = df.pivot_table(index='trade_date', columns='ts_code', values='open').values.astype(float)
    high = df.pivot_table(index='trade_date', columns='ts_code', values='high').values.astype(float)
    low = df.pivot_table(index='trade_date', columns='ts_code', values='low').values.astype(float)
    n, m = close.shape
    print(f"📐 矩阵: {n}日 × {m}只")
    
    # ===== 批量计算因子 =====
    f = {}
    
    print(f"📝 计算因子...")
    ct = time.time()
    
    # 基础列
    f['alpha022'] = close - open_
    f['alpha023'] = high / (low + 1e-10)
    f['alpha024'] = close * vol
    f['alpha030'] = high - low
    f['alpha011'] = (close - open_) / (high - low + 1e-10)
    f['alpha012'] = open_ / (vol + 1e-10)
    f['alpha017'] = vol / (close + 1e-10)
    f['alpha029'] = open_ * vol
    
    # 时间序列类
    ret_srs = np.diff(close, axis=0, prepend=close[:1,:])
    f['alpha025'] = ret_srs * vol
    
    # 开盘跳空
    f['alpha015'] = np.full_like(close, np.nan)
    f['alpha015'][1:] = (open_[1:] - close[:-1]) / (close[:-1] + 1e-10)
    
    # (open-low)/(high-low)
    f['alpha056'] = (open_ - low) / (high - low + 1e-10)
    
    # vol/MA(vol,5)
    ma5v = rolling_mean(vol, 5)
    f['alpha027'] = vol / (ma5v + 1e-10)
    f['alpha028'] = vol / (rolling_mean(vol, 3) + 1e-10)
    f['alpha033'] = f['alpha027'] * 100
    
    # close/close.shift(5)*vol/vol.shift(5)
    f['alpha035'] = np.full_like(close, np.nan)
    f['alpha035'][5:] = (close[5:]/close[:-5]) * (vol[5:]/vol[:-5])
    
    # |close-MA20|/close
    ma20c = rolling_mean(close, 20)
    f['alpha041'] = np.abs(close - ma20c) / (close + 1e-10)
    
    # delta(log(vol),5)
    f['alpha044'] = np.full_like(close, np.nan)
    f['alpha044'][5:] = np.log(vol[5:]+1) - np.log(vol[:-5]+1)
    
    # expanding mean vol
    cumsum_v = np.cumsum(vol, axis=0)
    cnt = np.arange(1, n+1)[:, np.newaxis]
    f['alpha026'] = cumsum_v / cnt
    
    # abs(close-open)*vol
    f['alpha007'] = np.abs(close - open_) * vol
    
    # MA10-MA30 (close)
    ma10c = rolling_mean(close, 10)
    ma30c = rolling_mean(close, 30)
    f['alpha038'] = ma10c - ma30c
    
    # MA5<MA20 (趋势衰竭)
    ma5c = rolling_mean(close, 5)
    f['alpha049'] = (ma5c < ma20c).astype(float)
    f['alpha049'][:19,:] = np.nan
    
    # RSI
    delta = np.diff(close, axis=0, prepend=close[:1,:])
    gain = np.maximum(delta, 0)
    loss = np.maximum(-delta, 0)
    avg_gain = rolling_mean(gain, 6)
    avg_loss = rolling_mean(loss, 6)
    rs = avg_gain / (avg_loss + 1e-10)
    f['alpha008'] = 100 - 100/(1+rs)
    
    # rolling corr/cov 用for循环(效率低但内存安全)
    def rolling_cov(x, y, w):
        res = np.full_like(x, np.nan)
        for i in range(w, n):
            xw = x[i-w:i+1]; yw = y[i-w:i+1]
            xm = np.nanmean(xw, axis=0); ym = np.nanmean(yw, axis=0)
            res[i] = np.nanmean((xw-xm)*(yw-ym), axis=0)
        return res
    
    def rolling_corr(x, y, w):
        res = np.full_like(x, np.nan)
        for i in range(w, n):
            xw = x[i-w:i+1]; yw = y[i-w:i+1]
            x_std = np.nanstd(xw, axis=0); y_std = np.nanstd(yw, axis=0)
            mask = (x_std>1e-10) & (y_std>1e-10)
            res[i,mask] = np.nanmean((xw[:,mask]-np.nanmean(xw[:,mask],axis=0))*(yw[:,mask]-np.nanmean(yw[:,mask],axis=0)), axis=0) / (x_std[mask]*y_std[mask])
        return res
    
    f['alpha010'] = rolling_cov(close, vol, 5)
    f['alpha006'] = rolling_corr(close, vol, 10)  # 简化
    
    # 秩差
    def rank_diff(h_arr, l_arr, w=30):
        res = np.full_like(h_arr, np.nan)
        for i in range(w, n):
            for j in range(m):
                hi = np.sort(h_arr[i-w:i+1,j])
                li = np.sort(l_arr[i-w:i+1,j])
                rk_h = np.searchsorted(hi, hi[-1]) / w
                rk_l = np.searchsorted(li, li[-1]) / w
                res[i,j] = rk_h - rk_l
        return res
    f['alpha016'] = rank_diff(high, low)
    
    # 合并第一批17个因子（直接复用之前的IC计算结果）
    print(f"✅ 因子计算: {time.time()-ct:.0f}s")
    # 5日未来收益
    fwd = np.full((n, m), np.nan)
    fwd = np.full((n, m), np.nan)
    for i in range(n-5):
        fwd[i] = close[i+5] / close[i] - 1
    
    # 计算IC
    results = {}
    for fname in sorted(f.keys()):
        fmat = f[fname]
        daily_ics = []
        for i in range(n):
            fv = fmat[i]; rv = fwd[i]
            mask = ~np.isnan(fv) & ~np.isnan(rv)
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
        results[fname] = {'name': ALL_NAMES.get(fname,''), 'ic':round(mu,4), 'ir':round(ir,2), 'pos_pct':round(pp,1), 'n_days':len(daily_ics)}
    
    # 合并第一批
    with open('/opt/stock-analyzer/alpha191_ic_20260713_0003.json') as f:
        old = json.load(f)
    merged = {**old, **results}
    
    out = f'/opt/stock-analyzer/alpha191_ic_{time.strftime("%Y%m%d_%H%M")}.json'
    with open(out, 'w') as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)
    
    # 输出
    sr = sorted(merged.items(), key=lambda x: abs(x[1]['ic']), reverse=True)
    qe = [x for x in sr if abs(x[1]['ic'])>=0.020]
    we = [x for x in sr if 0.010<=abs(x[1]['ic'])<0.020]
    no_ = [x for x in sr if abs(x[1]['ic'])<0.010]
    
    print(f"\n{'='*70}")
    print(f"  📊 Alpha191 全量IC | {time.strftime('%Y-%m-%d %H:%M')} | {len(merged)}个因子")
    print(f"{'='*70}")
    print(f"  {'因子':14s} {'中文名':12s} {'平均IC':>8s} {'IR':>5s} {'正IC%':>6s} {'有效天':>6s}")
    print(f"  {'─'*56}")
    for fn,r in sr:
        icon = '✅' if abs(r['ic'])>=0.020 else ('⚡' if abs(r['ic'])>=0.010 else '❌')
        print(f"  {fn:14s} {r.get('name',''):12s} {r['ic']:+8.4f}{icon} {r['ir']:5.2f} {r['pos_pct']:6.1f}% {r['n_days']:6d}")
    
    print(f"\n📋 ✅{len(qe)}个 | ⚡{len(we)}个 | ❌{len(no_)}个 | 总{len(merged)}个")
    print(f"💾 {out} | ⏱ {time.time()-t0:.0f}s")
    
    # 追加到结果文件
    with open('/opt/stock-analyzer/alpha191_ic_results.txt', 'w') as f:
        f.write(f"Alpha191 全量IC {time.strftime('%Y-%m-%d %H:%M')} | {len(merged)}个因子\n")
        for fn,r in sr:
            icon = '✅' if abs(r['ic'])>=0.020 else '❌'
            f.write(f"{icon} {fn:14s} IC={r['ic']:+7.4f} IR={r['ir']:5.2f} 正{r['pos_pct']:5.1f}% {r['n_days']}日\n")
        f.write(f"\n有效(|IC|>=0.020): {len(qe)}个\n")
        for fn,r in qe:
            f.write(f"  {fn:14s} {r.get('name',''):12s} IC={r['ic']:+7.4f}\n")

if __name__ == '__main__':
    main()
