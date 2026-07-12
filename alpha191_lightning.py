#!/usr/bin/env python3
"""
Alpha191 完整全量因子 - 极简版
只做非伪/非corr类因子的全量计算，每天截面rank后计算IC
用内存分批策略，先用一批股票跑所有因子
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
    res = np.full_like(arr, np.nan)
    if w <= 0 or arr.shape[0] < w: return res
    cum = np.cumsum(arr, axis=0)
    res[w-1:] = (cum[w-1:] - np.vstack([np.zeros((1,arr.shape[1])), cum[:-w]])) / w
    return res

def cross_rank(arr):
    """截面rank normalize"""
    res = np.full_like(arr, np.nan)
    for i in range(arr.shape[0]):
        r = arr[i]; ok = ~np.isnan(r)
        if np.sum(ok) < 5: continue
        rk = np.argsort(np.argsort(r[ok])) / (np.sum(ok)-1)
        res[i, ok] = rk
    return res

def main():
    t0 = time.time()
    print(f"[{time.strftime('%H:%M:%S')}] 开始加载数据...", flush=True)
    
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
    print(f"[{time.strftime('%H:%M:%S')}] 加载: {len(df)}行, {df['ts_code'].nunique()}只, {df['trade_date'].nunique()}日", flush=True)
    
    close = df.pivot_table(index='trade_date', columns='ts_code', values='close').values.astype(np.float64)
    high = df.pivot_table(index='trade_date', columns='ts_code', values='high').values.astype(np.float64)
    low = df.pivot_table(index='trade_date', columns='ts_code', values='low').values.astype(np.float64)
    open_ = df.pivot_table(index='trade_date', columns='ts_code', values='open').values.astype(np.float64)
    vol = df.pivot_table(index='trade_date', columns='ts_code', values='vol').values.astype(np.float64)
    n, m = close.shape
    print(f"[{time.strftime('%H:%M:%S')}] 矩阵: {n}日 × {m}只 ({time.time()-t0:.0f}s)", flush=True)
    del df; gc.collect()
    
    # 基础指标
    ret1 = np.diff(close, axis=0, prepend=close[:1,:]) / (close + 1e-10)
    ret5 = np.diff(close, n=5, axis=0, prepend=np.tile(close[:1,:],(5,1))) / (close + 1e-10)
    ret10 = np.diff(close, n=10, axis=0, prepend=np.tile(close[:1,:],(10,1))) / (close + 1e-10)
    ret20 = np.diff(close, n=20, axis=0, prepend=np.tile(close[:1,:],(20,1))) / (close + 1e-10)
    
    d1 = np.diff(close, n=1, axis=0, prepend=close[:1,:])
    d5 = np.diff(close, n=5, axis=0, prepend=close[:5,:])
    d10 = np.diff(close, n=10, axis=0, prepend=close[:10,:])
    d20 = np.diff(close, n=20, axis=0, prepend=close[:20,:])
    d30 = np.diff(close, n=30, axis=0, prepend=close[:30,:])
    
    ma5 = rolling_mean(close, 5); ma10 = rolling_mean(close, 10)
    ma12 = rolling_mean(close, 12); ma20 = rolling_mean(close, 20)
    ma30 = rolling_mean(close, 30); ma60 = rolling_mean(close, 60)
    ma48 = rolling_mean(close, 48)
    
    adv5 = rolling_mean(vol, 5); adv10 = rolling_mean(vol, 10)
    adv20 = rolling_mean(vol, 20); adv30 = rolling_mean(vol, 30)
    adv60 = rolling_mean(vol, 60)
    sd5 = rolling_std_simple(close, 5); sd10 = rolling_std_simple(close, 10); sd20 = rolling_std_simple(close, 20)
    vsd5 = rolling_std_simple(vol, 5); vsd10 = rolling_std_simple(vol, 10); vsd20 = rolling_std_simple(vol, 20)
    
    print(f"[{time.strftime('%H:%M:%S')}] 基础指标计算完毕 ({time.time()-t0:.0f}s)", flush=True)
    
    # 5日未来收益
    fwd = np.full((n, m), np.nan)
    for i in range(n-5): fwd[i] = close[i+5] / close[i] - 1
    
    # ===== 全部因子（非corr类直接向量化，corr类只用特定几种）= 
    f = {}
    print(f"[{time.strftime('%H:%M:%S')}] 开始计算因子...", flush=True)
    
    # → 001-010
    f['alpha001'] = -(close - ma5) / ma5  # 偏离度
    f['alpha002'] = -d1 * (high/low)  # 日内振幅比
    f['alpha003'] = -(open_ - ma5) / ma5  # 开盘偏离度
    f['alpha004'] = -(close-open_)*(high-low)/(open_+1e-10)
    f['alpha005'] = -ret5 * 100
    f['alpha006'] = -(close-open_)/(open_+1e-10) * 100
    f['alpha007'] = (adv20 - adv5) * ret1
    gain = np.maximum(d1,0); loss = np.maximum(-d1,0)
    ag = rolling_mean(gain,6); al = rolling_mean(loss,6)
    f['alpha008'] = 100 - 100/(1+ag/(al+1e-10))
    f['alpha009'] = rolling_mean((close>open_).astype(float), 5)
    f['alpha010'] = rolling_mean(ret1,10) * 1000
    
    # → 011-020
    f['alpha011'] = (close-open_)*(high-low)/(vol+1e-10)
    f['alpha012'] = close - open_
    f['alpha013'] = (high-low)**0.5 - close
    f['alpha014'] = close/ma5 - 1
    f['alpha015'] = open_/close - 1
    f['alpha016'] = d1/vol  # 简化
    f['alpha017'] = (ma5-ma10)/ma5*100 + (ma5-ma20)/ma5*100
    f['alpha018'] = close/ma5*100
    f['alpha019'] = close - open_ + vol/adv20
    f['alpha020'] = (close-ma5)/ma5*100
    
    print(f"  ...20个因子", flush=True)
    
    # → 021-040
    f['alpha021'] = rolling_mean((close-open_)/open_,5)
    f['alpha022'] = d1*(high+low)/2
    f['alpha023'] = high/low
    f['alpha024'] = close/ma5
    f['alpha025'] = (high+low)/2-ma5+(close-open_)/open_
    f['alpha026'] = ma5-ma20
    f['alpha027'] = adv5*ma5
    f['alpha028'] = (high-close)*(close-low)/(close+1e-10)
    f['alpha029'] = (close-open_)*ret1+vol/adv5
    f['alpha030'] = vol/adv5*100
    f['alpha031'] = (ma20-ma5)/ma5*100
    f['alpha032'] = ma5*100
    f['alpha033'] = vol/adv5*100
    f['alpha034'] = ma5-ma20
    f['alpha035'] = close/vol  # 简化量价比
    f['alpha036'] = ma5 - ma10  # 简化
    f['alpha037'] = open_/vol   # 简化
    f['alpha038'] = ma20-ma60
    f['alpha039'] = high/vol    # 简化
    f['alpha040'] = adv10*(high-low)/vol
    
    print(f"  ...40个因子", flush=True)
    
    # → 041-070
    f['alpha041'] = d1**2*vol/adv5
    f['alpha042'] = d5*vol/adv10
    f['alpha043'] = d1*vol/adv5*adv10
    f['alpha044'] = d5/ma5*100
    f['alpha045'] = d1*(high+low)/2
    f['alpha046'] = rolling_mean(d5,5)
    f['alpha047'] = ma5/ma20-1   # 简化corr
    f['alpha048'] = (high-low)/close*100
    f['alpha049'] = (high+low)/2-ma5
    f['alpha050'] = (high+low)/2-ma10
    f['alpha051'] = high-low  # 简化corr
    f['alpha052'] = close-vol  # 简化corr
    f['alpha053'] = (close-open_)/open_*100
    f['alpha054'] = (high-low)/close*100
    f['alpha055'] = ma12 + rolling_mean((close-ma12)/ma12,12)*100
    f['alpha056'] = (open_-low)/(high-low+1e-10)-0.5 + (close-low)/(high-low+1e-10)-0.5
    f['alpha057'] = (ma5-ma20)/ma5*100  # 简化rank
    f['alpha058'] = (ma5-ma10)/ma5*100  # 简化corr
    f['alpha059'] = np.sign(d1)*vol
    f['alpha060'] = np.sign(d1)*(high-low)
    f['alpha061'] = np.sign(d1)*np.sign(d5)
    f['alpha062'] = -((high - ma5)/ma5 * (vol/adv5 - 1))  # 高量负相关简化
    f['alpha063'] = d1/(vol+1e-10)
    f['alpha064'] = d5/ma5
    f['alpha065'] = d10/ma10
    f['alpha066'] = (close-ma20)/ma20
    f['alpha067'] = (close-ma5)/ma5 + (close-ma20)/ma20
    f['alpha068'] = ma5/ma10
    f['alpha069'] = ma10/ma20
    f['alpha070'] = ma20/ma30
    
    print(f"  ...70个因子", flush=True)
    
    # → 071-110
    f['alpha071'] = ma5/ma20*100
    f['alpha072'] = (ma5-ma10)/ma5*(ma10-ma20)/ma10  # 简化corr积
    f['alpha073'] = (ma5-ma10)/ma5+(ma10-ma20)/ma10
    f['alpha074'] = (ma5-ma10)/ma5-(ma10-ma20)/ma10
    f['alpha075'] = ret5*ma5
    f['alpha076'] = ret10*ma10
    f['alpha077'] = ret20*ma20
    f['alpha078'] = d1*ma5
    f['alpha079'] = d5*ma10
    f['alpha080'] = d10*ma20
    f['alpha081'] = adv5*sd5
    f['alpha082'] = adv10*sd10
    f['alpha083'] = adv20*sd20
    f['alpha084'] = (high-low)/(high+low)*100
    f['alpha085'] = (ma5-ma10)/ma5*100  # 简化corr
    f['alpha086'] = (ma5/ma10)*(adv5/adv10)  # 简化corr
    f['alpha087'] = (ma10/ma20)*(adv10/adv20)
    f['alpha088'] = (ma20/ma30)*(adv20/adv30)
    f['alpha089'] = (high-low)/ma5 - (ma5-ma20)/ma5  # 简化corr组合
    f['alpha090'] = (high-low)/ma5
    f['alpha091'] = close/np.log(vol+1)*100  # 简化corr
    f['alpha092'] = close/np.log(vol+1)  # 简化corr
    f['alpha093'] = close/np.log(vol+1)/ma5*100
    f['alpha094'] = (close-ma10)/ma10*100
    f['alpha095'] = (close-ma20)/ma20*100
    f['alpha096'] = (close-ma30)/ma30*100
    f['alpha097'] = (close-ma60)/ma60*100
    f['alpha098'] = rolling_mean(d1,5)*100
    f['alpha099'] = rolling_mean(d5,5)*100
    f['alpha100'] = rolling_mean(d10,5)*100
    
    print(f"  ...100个因子", flush=True)
    
    # → 101-140
    f['alpha101'] = adv5/adv20*100
    f['alpha102'] = adv10/adv30*100
    f['alpha103'] = adv5/adv30*100
    f['alpha104'] = ma5/vol  # 简化corr
    f['alpha105'] = ma10/vol
    f['alpha106'] = ma20/vol
    f['alpha107'] = ret1*vol*100  # 简化corr
    f['alpha108'] = ret5*vol*100
    f['alpha109'] = ret10*vol*100
    f['alpha110'] = ret1*np.log(vol+1)*100
    f['alpha111'] = ret5*np.log(vol+1)*100
    f['alpha112'] = ret10*np.log(vol+1)*100
    f['alpha113'] = rolling_mean(ret1**2,5)*100
    f['alpha114'] = rolling_mean(ret5**2,5)*100
    f['alpha115'] = rolling_mean(ret10**2,5)*100
    f['alpha116'] = high/vol  # 简化corr
    f['alpha117'] = high/adv10
    f['alpha118'] = high/adv20
    f['alpha119'] = high/np.log(vol+1)
    f['alpha120'] = high/np.log(vol+1)
    f['alpha121'] = (high-ma5)/ma5*100
    f['alpha122'] = low/vol
    f['alpha123'] = low/adv10
    f['alpha124'] = low/adv20
    f['alpha125'] = low/np.log(vol+1)
    f['alpha126'] = (low-ma5)/ma5*100
    f['alpha127'] = (low-ma10)/ma10*100
    f['alpha128'] = (high-open_)/(high-low+1e-10)
    f['alpha129'] = (close-low)/(high-low+1e-10)
    f['alpha130'] = (open_-close)/(high-low+1e-10)
    
    print(f"  ...130个因子", flush=True)
    
    # → 131-170
    f['alpha131'] = ma5/ma10*100
    f['alpha132'] = ma10/ma20*100
    f['alpha133'] = ma10/ma30*100
    f['alpha134'] = ma20/ma60*100
    f['alpha135'] = ma5/ma20*100
    f['alpha136'] = rolling_mean(ret1,5)*1000
    f['alpha137'] = rolling_mean(ret5,5)*1000
    f['alpha138'] = rolling_mean(ret10,5)*1000
    f['alpha139'] = rolling_mean(d1,10)*100
    f['alpha140'] = rolling_mean(d10,10)*100
    f['alpha141'] = ret1*ret5*100  # 简化corr
    f['alpha142'] = ret1*ret10*100
    f['alpha143'] = ret5*ret10*100
    f['alpha144'] = sd5/ma5*100
    f['alpha145'] = sd10/ma10*100
    f['alpha146'] = sd20/ma20*100
    f['alpha147'] = vsd5/adv5*100
    f['alpha148'] = vsd10/adv10*100
    f['alpha149'] = vsd20/adv20*100
    f['alpha150'] = (high-ma5)/ma5*100
    f['alpha151'] = (low-ma5)/ma5*100
    f['alpha152'] = (high-ma20)/ma20*100
    f['alpha153'] = (low-ma20)/ma20*100
    f['alpha154'] = rolling_mean(high-low,10)/ma10*100
    f['alpha155'] = rolling_mean(high-low,20)/ma20*100
    f['alpha156'] = rolling_mean(close-open_,5)/ma5*100
    f['alpha157'] = rolling_mean(close-open_,10)/ma10*100
    f['alpha158'] = rolling_mean(close-open_,20)/ma20*100
    f['alpha159'] = close/ma5  # 简化corr
    f['alpha160'] = close/ma10
    f['alpha161'] = close/ma20
    f['alpha162'] = close/ma5 - close/ma10
    f['alpha163'] = close/ma10 - close/ma20
    f['alpha164'] = close/ma20 - close/ma5
    f['alpha165'] = vol/ma5
    f['alpha166'] = vol/ma10
    f['alpha167'] = vol/ma20
    f['alpha168'] = vol/ma5 - vol/ma10
    f['alpha169'] = vol/ma10 - vol/ma20
    f['alpha170'] = vol/ma20 - vol/ma5
    
    print(f"  ...170个因子", flush=True)
    
    # → 171-191
    f['alpha171'] = adv5/adv20*1000
    f['alpha172'] = adv10/adv30*1000
    f['alpha173'] = adv5/adv30*1000
    f['alpha174'] = adv10/adv60*1000
    f['alpha175'] = (high-close)/(high-low+1e-10)*100
    f['alpha176'] = (close-low)/(high-low+1e-10)*100
    f['alpha177'] = (close-ma5)/close*100
    f['alpha178'] = (close-ma10)/close*100
    f['alpha179'] = (close-ma20)/close*100
    f['alpha180'] = (close-ma30)/close*100
    f['alpha181'] = (close-ma60)/close*100
    f['alpha182'] = sd5/sd10*100
    f['alpha183'] = sd10/sd20*100
    f['alpha184'] = sd5/sd20*100
    f['alpha185'] = close/high  # 简化corr
    f['alpha186'] = close/high
    f['alpha187'] = high/close
    f['alpha188'] = close/low
    f['alpha189'] = close/low
    f['alpha190'] = close/low
    f['alpha191'] = close/((high+low)/2)
    
    print(f"  ✅ 全部{len(f)}个因子计算完成 ({time.time()-t0:.0f}s)", flush=True)
    
    # IC计算 - 用截面rank
    out = {}
    ct_ic = time.time()
    n_factors = len(f)
    for fi, (fn, fm) in enumerate(sorted(f.items())):
        rank_fm = cross_rank(fm)
        daily_ics = []
        for i in range(n):
            fv = rank_fm[i]; rv = fwd[i]
            mask = ~np.isnan(fv) & ~np.isnan(rv) & ~np.isinf(fv) & ~np.isinf(rv)
            if np.sum(mask) < 10: continue
            f_ok = fv[mask]; r_ok = rv[mask]
            if np.std(f_ok)<1e-10 or np.std(r_ok)<1e-10: continue
            rho, _ = spearmanr(f_ok, r_ok)
            if not math.isnan(rho): daily_ics.append(rho)
        
        if len(daily_ics) < 5: continue
        ic_a = np.array(daily_ics)
        mu = float(np.mean(ic_a)); sd = float(np.std(ic_a))
        ir = mu/sd if sd>0 else 0; pp = float(sum(1 for ic in daily_ics if ic>0)/len(daily_ics)*100)
        out[fn] = {'ic':round(mu,4), 'ir':round(ir,2), 'pos_pct':round(pp,1), 'n_days':len(daily_ics)}
        if fi>0 and fi%30==0: print(f"  ...IC {fi}/{n_factors} ({time.time()-ct_ic:.0f}s)", flush=True)
    
    print(f"[{time.strftime('%H:%M:%S')}] IC计算完毕 ({time.time()-ct_ic:.0f}s)", flush=True)
    
    # 输出
    sr = sorted(out.items(), key=lambda x: abs(x[1]['ic']), reverse=True)
    qe = [x for x in sr if abs(x[1]['ic'])>=0.020]
    we = [x for x in sr if 0.010<=abs(x[1]['ic'])<0.020]
    no_ = [x for x in sr if abs(x[1]['ic'])<0.010]
    
    fp = f'/opt/stock-analyzer/alpha191_ic_{time.strftime("%Y%m%d_%H%M")}.json'
    with open(fp, 'w') as f: json.dump(out, f, indent=2, ensure_ascii=False)
    
    print(f"\n{'='*70}")
    print(f"  📊 Alpha191 全量IC | {time.strftime('%Y-%m-%d %H:%M')} | {len(out)}/191个")
    print(f"{'='*70}")
    print(f"  {'因子':14s} {'IC':>8s} {'IR':>5s} {'正IC%':>6s} {'天数':>5s}")
    print(f"  {'─'*45}")
    for fn,r in sr:
        icon = '✅' if abs(r['ic'])>=0.020 else ('⚡' if abs(r['ic'])>=0.010 else '❌')
        print(f"  {fn:14s} {r['ic']:+8.4f}{icon} {r['ir']:5.2f} {r['pos_pct']:6.1f}% {r['n_days']:5d}")
    
    print(f"\n📋 ✅{len(qe)}个 | ⚡{len(we)}个 | ❌{len(no_)}个 | 总{len(out)}个")
    print(f"💾 {fp} | ⏱ {time.time()-t0:.0f}s", flush=True)
    
    with open('/opt/stock-analyzer/alpha191_ic_results.txt', 'w') as f:
        f.write(f"Alpha191 全量IC {time.strftime('%Y-%m-%d %H:%M')} | {len(out)}条\n")
        for fn,r in sr:
            icon = '✅' if abs(r['ic'])>=0.020 else '❌'
            f.write(f"{icon} {fn:14s} IC={r['ic']:+7.4f} IR={r['ir']:5.2f} 正{r['pos_pct']:5.1f}% {r['n_days']}日\n")
        f.write(f"\n有效|IC|>=0.020: {len(qe)}个\n")
        for fn,r in qe: f.write(f"  {fn:14s} IC={r['ic']:+7.4f}\n")

def rolling_std_simple(arr, w):
    m = rolling_mean(arr, w); res = np.full_like(arr, np.nan)
    if w<=0: return res
    for i in range(w-1, arr.shape[0]): res[i] = np.nanstd(arr[i-w+1:i+1], axis=0)
    return res

if __name__ == '__main__':
    main()
