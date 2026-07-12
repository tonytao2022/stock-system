#!/usr/bin/env python3
"""
Alpha191 缺失因子补算 — 只算那9个没出来的因子
==================================================
"""
import os, sys, json, math, time, subprocess
import numpy as np
from scipy.stats import spearmanr
from collections import defaultdict
sys.path.insert(0, '/opt/stock-analyzer')
import db_config

PWD = db_config._get_password()
START = '2024-01-02'
END = '2026-07-10'

MISSING_FACTORS = {
    'alpha001': '量价背离(rank)',
    'alpha002': '日内振幅变化',
    'alpha005': '量价时序相关',
    'alpha032': '高中量相关',
    'alpha036': '量价秩相关',
    'alpha048': '方向变化+量',
    'alpha062': '高量负相关',
    'alpha089': '高低量相关',
    'alpha092': '衰减量价',
}


def main():
    t0 = time.time()
    
    # 日期索引+股票列表
    r = subprocess.run(['mysql','-u','debian-sys-maint',f'-p{PWD}','stock_db_v2','-N','-B'],
        input=f"SELECT trade_date, ts_code, close FROM daily_kline WHERE trade_date>='{START}' AND trade_date<='{END}' ORDER BY trade_date",
        capture_output=True, text=True, timeout=120)
    date_close = defaultdict(dict)
    for line in r.stdout.strip().split('\n'):
        parts = line.split('\t')
        if len(parts)<3: continue
        date_close[parts[0]][parts[1].strip()] = float(parts[2])
    alld = sorted(date_close.keys())
    
    r2 = subprocess.run(['mysql','-u','debian-sys-maint',f'-p{PWD}','stock_db_v2','-N','-B'],
        input="SELECT ts_code FROM backtest_pool",
        capture_output=True, text=True, timeout=30)
    codes = [l.strip() for l in r2.stdout.strip().split('\n') if l.strip()]
    
    print(f"⏳ 补算9个缺失因子 ({len(codes)}只, {len(alld)}日)...")
    alpha_records = defaultdict(lambda: defaultdict(list))
    
    t2 = time.time()
    for idx, code in enumerate(codes):
        if idx % 100 == 0:
            print(f"  [{idx}/{len(codes)}] {time.time()-t2:.0f}s")
        
        sql = f"SELECT trade_date, `open`, high, low, `close`, vol FROM daily_kline WHERE ts_code='{code}' AND trade_date>='{START}' AND trade_date<='{END}' ORDER BY trade_date"
        r3 = subprocess.run(['mysql','-u','debian-sys-maint',f'-p{PWD}','stock_db_v2','-N','-B'],
            input=sql, capture_output=True, text=True, timeout=30)
        lines = r3.stdout.strip().split('\n')
        
        tds=[];c=[];o=[];h=[];l=[];v=[]
        for line in lines:
            p=line.split('\t')
            if len(p)<6:continue
            tds.append(p[0])
            try: c.append(float(p[4]));o.append(float(p[1]));h.append(float(p[2]));l.append(float(p[3]));v.append(float(p[5]))
            except: continue
        if len(c)<30: continue
        n = len(c)
        
        for i in range(29, n):
            ci=c[i];oi=o[i];hi=h[i];li=l[i];vi=v[i]
            td = tds[i]
            td_idx = alld.index(td) if td in alld else -1
            if td_idx<0 or td_idx+5>=len(alld): continue
            future = alld[td_idx+5]
            tc=date_close[td].get(code); fc=date_close[future].get(code)
            if not tc or not fc or tc==0: continue
            fwd_ret = (fc/tc-1)*100
            
            # alpha032: 6日均线比收盘 * vol
            try:
                if i>=6 and ci>0 and vi>0:
                    a032 = (np.mean(c[i-6:i+1])-ci)/ci*vi
                    if not (a032 is None or math.isnan(a032) or math.isinf(a032)):
                        # clip极端值
                        a032_clip = max(min(a032, 1e6), -1e6)
                        alpha_records['alpha032'][td].append((a032_clip, fwd_ret))
            except: pass
            
            # alpha092: 12日高低幅/收盘*量
            try:
                if i>=12 and ci>0 and vi>0:
                    hi13=max(c[i-12:i+1]); lo13=min(c[i-12:i+1])
                    a092 = (hi13-lo13)/ci*vi
                    if not (a092 is None or math.isnan(a092) or math.isinf(a092)):
                        a092_clip = max(min(a092, 1e6), -1e6)
                        alpha_records['alpha092'][td].append((a092_clip, fwd_ret))
            except: pass
            
            # alpha001: 量价时序秩相关（5日）
            try:
                if i>=5:
                    v7=np.maximum(v[i-5:i+1],1)
                    dvol = np.diff(np.log(v7))
                    ret7 = (c[i-5:i+1]-o[i-5:i+1])/np.maximum(o[i-5:i+1], 0.001)
                    if len(dvol)>=5 and np.std(dvol)>1e-10 and np.std(ret7)>1e-10:
                        r1 = pd.Series(dvol).rank(pct=True).values
                        r2 = pd.Series(ret7).rank(pct=True).values
                        a001 = -float(np.corrcoef(r1,r2)[0,1])
                        if not math.isnan(a001) and not math.isinf(a001):
                            alpha_records['alpha001'][td].append((a001, fwd_ret))
            except: pass
            
            # alpha002: 日内振幅变化
            try:
                if i>=1:
                    max_hl=max(hi-li, 0.001); max_hl_prev=max(h[i-1]-l[i-1], 0.001)
                    v_now = ((ci-li)-(hi-ci))/max_hl
                    v_prev = ((c[i-1]-l[i-1])-(h[i-1]-c[i-1]))/max_hl_prev
                    a002 = -(v_now - v_prev)
                    if not math.isnan(a002) and not math.isinf(a002):
                        alpha_records['alpha002'][td].append((a002, fwd_ret))
            except: pass
            
            # alpha005: 量价时序秩相关（5日）
            try:
                if i>=4:
                    c5=c[i-4:i+1]; v5=v[i-4:i+1]
                    rc=pd.Series(c5).rank(pct=True).values; rv=pd.Series(v5).rank(pct=True).values
                    if np.std(rc)>1e-10 and np.std(rv)>1e-10:
                        a005 = -float(np.corrcoef(rc,rv)[0,1])
                        if not math.isnan(a005) and not math.isinf(a005):
                            alpha_records['alpha005'][td].append((a005, fwd_ret))
            except: pass
            
            # alpha036: 量价秩相关
            try:
                if i>=5:
                    c6=c[i-5:i+1]; v6=v[i-5:i+1]
                    rc=pd.Series(c6).rank(pct=True).values; rv=pd.Series(v6).rank(pct=True).values
                    if np.std(rc)>1e-10 and np.std(rv)>1e-10:
                        a036 = float(np.corrcoef(rc,rv)[0,1])
                        if not math.isnan(a036) and not math.isinf(a036):
                            alpha_records['alpha036'][td].append((a036, fwd_ret))
            except: pass
            
            # alpha048: 方向变化+量 (corr(close, vol) * 当日变化)
            try:
                if i>=4:
                    c5=c[i-4:i+1]; v5=v[i-4:i+1]
                    if np.std(c5)>1e-10 and np.std(v5)>1e-10:
                        a048 = float(np.corrcoef(c5,v5)[0,1]*(ci-c[i-1]))
                        if not math.isnan(a048) and not math.isinf(a048):
                            alpha_records['alpha048'][td].append((a048, fwd_ret))
            except: pass
            
            # alpha062: 高量负相关 -corr(high, vol)
            try:
                if i>=4:
                    h5=h[i-4:i+1]; v5=v[i-4:i+1]
                    if np.std(h5)>1e-10 and np.std(v5)>1e-10:
                        a062 = -float(np.corrcoef(h5,v5)[0,1])
                        if not math.isnan(a062) and not math.isinf(a062):
                            alpha_records['alpha062'][td].append((a062, fwd_ret))
            except: pass
            
            # alpha089: 高低量相关 1-corr(close, vol)
            try:
                if i>=12:
                    c13=c[i-12:i+1]; v13=v[i-12:i+1]
                    if np.std(c13)>1e-10 and np.std(v13)>1e-10:
                        a089 = 1-float(np.corrcoef(c13,v13)[0,1])
                        if not math.isnan(a089) and not math.isinf(a089):
                            alpha_records['alpha089'][td].append((a089, fwd_ret))
            except: pass
    
    print(f"\n⏳ IC计算...")
    ic_results = {}
    for aname, cname in MISSING_FACTORS.items():
        records = alpha_records.get(aname, {})
        if not records:
            print(f"  {aname}: ❌ 无记录")
            continue
        daily_ics = []
        for td, pairs in records.items():
            if len(pairs) < 10: continue
            vals, rets = zip(*pairs)
            try:
                rho, _ = spearmanr(vals, rets)
                if not math.isnan(rho): daily_ics.append(rho)
            except: pass
        if len(daily_ics) < 5:
            print(f"  {aname}: ❌ IC天数不足{len(daily_ics)}/{len(records)}日")
            continue
        ic_arr=np.array(daily_ics)
        mean_ic=np.mean(ic_arr); std_ic=np.std(ic_arr)
        ir=mean_ic/std_ic if std_ic>0 else 0
        pos_pct=sum(1 for ic in daily_ics if ic>0)/len(daily_ics)*100
        icon = '✅' if abs(mean_ic)>=0.025 else ('⚡' if abs(mean_ic)>=0.01 else '❌')
        print(f"  {aname:10s} {cname:10s} {mean_ic:+7.4f}{icon} IR{ir:5.2f} 正{pos_pct:5.0f}% {len(daily_ics):3d}日")
        ic_results[aname] = {'name':cname,'ic':round(mean_ic,4),'ir':round(ir,2),
                             'pos_pct':round(pos_pct,1),'n_days':len(daily_ics)}
    
    print(f"\n⏳ 总耗时: {time.time()-t0:.0f}s")
    fp = f'/tmp/alpha191_missing_factors_{time.strftime("%Y%m%d_%H%M%S")}.json'
    with open(fp,'w') as f:
        json.dump(ic_results, f, indent=2, ensure_ascii=False)
    print(f"📁 {fp}")


if __name__ == '__main__':
    import pandas as pd
    main()
