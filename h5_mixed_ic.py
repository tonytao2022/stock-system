#!/usr/bin/env python3
"""
H5混合因子 IC 计算 — 14个Alpha因子组合
========================================
三种组合方式分别计算复合RankIC
"""
import os, sys, json, math, time, subprocess
import numpy as np
from scipy.stats import spearmanr
from collections import defaultdict
sys.path.insert(0, '/opt/stock-analyzer')
import db_config

PWD = db_config._get_password()
START = '2024-01-02'; END = '2026-07-10'

# 14个有效因子及其IC权重
FACTOR_ICS = {
    'alpha005': {'ic': 0.0282, 'ir': 0.32, 'positive': True},
    'alpha014': {'ic': -0.0271, 'ir': -0.16, 'positive': False},
    'alpha018': {'ic': -0.0321, 'ir': -0.19, 'positive': False},
    'alpha020': {'ic': -0.0331, 'ir': -0.19, 'positive': False},
    'alpha031': {'ic': -0.0393, 'ir': -0.22, 'positive': False},
    'alpha034': {'ic': 0.0393, 'ir': 0.22, 'positive': True},
    'alpha036': {'ic': -0.0338, 'ir': -0.37, 'positive': False},
    'alpha040': {'ic': -0.0270, 'ir': -0.19, 'positive': False},
    'alpha046': {'ic': 0.0453, 'ir': 0.26, 'positive': True},
    'alpha055': {'ic': -0.0263, 'ir': -0.18, 'positive': False},
    'alpha062': {'ic': 0.0327, 'ir': 0.34, 'positive': True},
    'alpha089': {'ic': 0.0407, 'ir': 0.34, 'positive': True},
    'alpha092': {'ic': -0.0375, 'ir': -0.32, 'positive': False},
    'alpha102': {'ic': -0.0397, 'ir': -0.33, 'positive': False},
}

# 方向符号（使所有因子正向一致）
FACTOR_DIR = {k: 1 if v['positive'] else -1 for k, v in FACTOR_ICS.items()}

# 三套权重方案（包含方向归一化）
WEIGHTS = {}
n = len(FACTOR_ICS)
# 1. 等权（归一化方向后的等权）
WEIGHTS['equal_norm'] = {k: FACTOR_DIR[k]/n for k in FACTOR_ICS}
# 2. IC加权（按|IC|，方向归一化）
ic_sum = sum(abs(v['ic']) for v in FACTOR_ICS.values())
WEIGHTS['ic_norm'] = {k: FACTOR_DIR[k]*abs(v['ic'])/ic_sum for k, v in FACTOR_ICS.items()}
# 3. IR加权（按|IR|，方向归一化）
ir_sum = sum(abs(v['ir']) for v in FACTOR_ICS.values())
WEIGHTS['ir_norm'] = {k: FACTOR_DIR[k]*abs(v['ir'])/ir_sum for k, v in FACTOR_ICS.items()}
# 4. 等权（不归一化，留作对比）
WEIGHTS['equal_raw'] = {k: 1.0/n for k in FACTOR_ICS}
# 5. 仅用正向因子（alpha046/034/062/089/005 共5个）
positive_factors = ['alpha005','alpha034','alpha046','alpha062','alpha089']
for f in positive_factors: WEIGHTS['pos_only'] = {f: 1.0/len(positive_factors) for f in positive_factors}


def compute_alpha(c, o, h, l, v, tds):
    """只算需要的14个因子"""
    n = len(c)
    if n < 30: return {}
    results = {}
    for i in range(29, n):
        vals = {}
        try:
            ci=c[i];oi=o[i];hi=h[i];li=l[i];vi=v[i]
            
            # alpha005: 量价时序秩相关
            if i>=4:
                c5=c[i-4:i+1]; v5=v[i-4:i+1]
                rc=pd.Series(c5).rank(pct=True).values; rv=pd.Series(v5).rank(pct=True).values
                if np.std(rc)>1e-10 and np.std(rv)>1e-10:
                    vals['alpha005'] = -float(np.corrcoef(rc,rv)[0,1])
            
            # alpha014: 5日涨幅
            if i>=5: vals['alpha014'] = ci - c[i-5]
            
            # alpha018: 5日收盘比
            if i>=5 and c[i-5]>0: vals['alpha018'] = ci/c[i-5]
            
            # alpha020: 6日涨幅%
            if i>=6 and c[i-6]>0: vals['alpha020'] = (ci-c[i-6])/c[i-6]*100
            
            # alpha031: 12日偏离度
            if i>=11:
                ma12 = c[i-11:i+1].mean()
                if ma12>0: vals['alpha031'] = (ci-ma12)/ma12*100
            
            # alpha034: 12日均线比
            if i>=11 and ci>0: vals['alpha034'] = c[i-11:i+1].mean()/ci
            
            # alpha036: 量价秩相关（正相关）
            if i>=5:
                c6=c[i-5:i+1]; v6=v[i-5:i+1]
                rc=pd.Series(c6).rank(pct=True).values; rv=pd.Series(v6).rank(pct=True).values
                if np.std(rc)>1e-10 and np.std(rv)>1e-10:
                    vals['alpha036'] = float(np.corrcoef(rc,rv)[0,1])
            
            # alpha040: 量比功率
            if i>=26:
                up = np.sum(v[i-25:i+1][c[i-25:i+1] > c[i-26:i]])
                dn = np.sum(v[i-25:i+1][c[i-25:i+1] <= c[i-26:i]])
                vals['alpha040'] = up/max(dn,1)*100
            
            # alpha046: 多均线位置
            if i>=23 and ci>0:
                ma3=c[i-2:i+1].mean(); ma6=c[i-5:i+1].mean()
                ma12=c[i-11:i+1].mean(); ma24=c[i-23:i+1].mean()
                vals['alpha046'] = (ma3+ma6+ma12+ma24)/(4*ci)
            
            # alpha055: 随机指标(RSI-like)
            if i>=11:
                hi12=h[i-11:i+1].max(); lo12=l[i-11:i+1].min()
                if hi12>lo12:
                    vals['alpha055'] = (ci-lo12)/(hi12-lo12)*100
            
            # alpha062: 高量负相关
            if i>=4:
                h5=h[i-4:i+1]; v5=v[i-4:i+1]
                if np.std(h5)>1e-10 and np.std(v5)>1e-10:
                    vals['alpha062'] = -float(np.corrcoef(h5,v5)[0,1])
            
            # alpha089: 高低量相关
            if i>=12:
                c13=c[i-12:i+1]; v13=v[i-12:i+1]
                if np.std(c13)>1e-10 and np.std(v13)>1e-10:
                    vals['alpha089'] = 1-float(np.corrcoef(c13,v13)[0,1])
            
            # alpha092: 衰减量价
            if i>=12 and ci>0 and vi>0:
                hi13=c[i-12:i+1].max(); lo13=c[i-12:i+1].min()
                vals['alpha092'] = (hi13-lo13)/ci*vi
            
            # alpha102: 三维量价
            if ci>0 and vi>0:
                vals['alpha102'] = vi*(hi-li)/ci
        except: pass
        if vals: results[tds[i]] = vals
    return results


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
    
    print(f"⏳ 计算14因子+H5混合... ({len(codes)}只, {len(alld)}日)")
    
    # 收集原始因子值 + H5复合值
    raw_factors = defaultdict(lambda: defaultdict(dict))  # raw[td][code][factor]
    
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
        
        result = compute_alpha(np.array(c),np.array(o),np.array(h),np.array(l),np.array(v),tds)
        
        for td, alpha_vals in result.items():
            td_idx = alld.index(td) if td in alld else -1
            if td_idx<0 or td_idx+5>=len(alld): continue
            future = alld[td_idx+5]
            tc=date_close[td].get(code); fc=date_close[future].get(code)
            if not tc or not fc or tc==0: continue
            fwd_ret = (fc/tc-1)*100
            
            for aname, aval in alpha_vals.items():
                if aname not in FACTOR_ICS: continue
                if aval is None or math.isnan(aval) or math.isinf(aval): continue
                raw_factors[td].setdefault(code, {})[aname] = aval
    
    print(f"\n⏳ 计算H5混合因子IC...")
    
    # 对每个交易日，按每种权重方案计算H5值，然后算RankIC
    h5_results = {}  # scheme -> list of daily ICs
    
    for scheme_name, weights in WEIGHTS.items():
        daily_ics = []
        for td in sorted(raw_factors.keys()):
            codes_td = raw_factors[td]
            if len(codes_td) < 30: continue
            
            # 收集所有股票在这个交易日的因子矩阵
            h5_vals = {}
            for code, factor_vals in codes_td.items():
                # 只保留有所有14个因子值的股票
                # 或者可用部分因子：有≥10个因子值就计算
                present = [f for f in FACTOR_ICS if f in factor_vals]
                if len(present) < 10: continue
                
                # 计算H5混合值
                h5 = 0.0
                w_sum = 0.0
                for fname in present:
                    if fname not in weights: continue
                    w = weights[fname]
                    fval = factor_vals[fname]
                    h5 += w * fval
                    w_sum += w
                if w_sum > 0:
                    h5_vals[code] = h5 / w_sum
            
            if len(h5_vals) < 20: continue
            
            # 用date_close取未来5日收益
            td_idx = alld.index(td)
            if td_idx+5 >= len(alld): continue
            future = alld[td_idx+5]
            
            pairs = []
            for code, h5 in h5_vals.items():
                fc = date_close[future].get(code)
                tc = date_close[td].get(code)
                if tc and fc and tc != 0:
                    fwd_ret = (fc/tc-1)*100
                    pairs.append((h5, fwd_ret))
            
            if len(pairs) < 20: continue
            vals, rets = zip(*pairs)
            try:
                rho, _ = spearmanr(vals, rets)
                if not math.isnan(rho): daily_ics.append(rho)
            except: pass
        
        h5_results[scheme_name] = daily_ics
    
    # 输出结果
    print(f"\n\n{'='*70}")
    print(f"  📊 H5混合因子 IC 验证（14个Alpha因子组合）")
    print(f"  {len(codes)}只 | {len(alld)}交易日 | {time.time()-t0:.0f}s")
    print(f"{'='*70}")
    print(f"  {'方案':12s} {'平均IC':>8s} {'IR':>6s} {'正IC%':>7s} {'IC>0.025':>9s} {'有效天':>6s}")
    print(f"  {'─'*55}")
    
    for scheme_name in ['equal_raw', 'equal_norm', 'ic_norm', 'ir_norm', 'pos_only']:
        ics = h5_results.get(scheme_name, [])
        if not ics: continue
        ic_arr = np.array(ics)
        mean_ic = np.mean(ic_arr); std_ic = np.std(ic_arr)
        ir = mean_ic/std_ic if std_ic>0 else 0
        pos_pct = sum(1 for ic in ics if ic>0)/len(ics)*100
        gt0025 = sum(1 for ic in ics if abs(ic)>=0.025)/len(ics)*100
        label = {'equal_raw':'原始等权','equal_norm':'等权归一化','ic_norm':'IC加权归一化','ir_norm':'IR加权归一化','pos_only':'仅正向5因子'}
        icon = '✅' if abs(mean_ic)>=0.025 else '⚡'
        print(f"  {label[scheme_name]:12s} {mean_ic:+8.4f}{icon} {ir:6.2f} {pos_pct:6.0f}% {gt0025:8.0f}% {len(ics):6d}")
    
    # 最佳因子（单因子对比）
    print(f"\n{'─'*55}")
    print(f"  📌 最佳单因子对比")
    print(f"{'─'*55}")
    best = sorted(FACTOR_ICS.items(), key=lambda x: abs(x[1]['ic']), reverse=True)
    for aname, v in best[:5]:
        print(f"  {aname:10s} IC={v['ic']:+7.4f} IR={v['ir']:5.2f}")
    
    # 提升分析
    best_ic = max(abs(v['ic']) for v in FACTOR_ICS.values())
    best_ir = max(abs(v['ir']) for v in FACTOR_ICS.values())
    print(f"\n{'─'*55}")
    print(f"  📈 混合提升效果")
    for scheme_name in ['equal_raw', 'equal_norm', 'ic_norm', 'ir_norm', 'pos_only']:
        ics = h5_results.get(scheme_name, [])
        if not ics: continue
        mean_ic = np.mean(ics); std_ic = np.std(ics)
        ir = mean_ic/std_ic if std_ic>0 else 0
        ic_pct = abs(mean_ic)/best_ic*100
        ir_pct = abs(ir)/best_ir*100
        label = {'equal_raw':'原始等权','equal_norm':'等权归一','ic_norm':'IC加权归一','ir_norm':'IR加权归一','pos_only':'正向5因子'}[scheme_name]
        print(f"  {label:10s}: IC={mean_ic:+7.4f} ({ic_pct:5.0f}% vs 最佳单因子{best_ic:.4f})"
              f" | IR={ir:5.2f} ({ir_pct:5.0f}% vs 最佳单因子{best_ir:.2f})")
    
    print(f"\n{'='*70}")
    print(f"  📋 结论：混合是否提升？")
    for scheme_name in ['equal_raw', 'equal_norm', 'ic_norm', 'ir_norm', 'pos_only']:
        ics = h5_results.get(scheme_name, [])
        if not ics: continue
        mean_ic = np.mean(ics)
        best_single = max(abs(v['ic']) for v in FACTOR_ICS.values())
        improv = (abs(mean_ic) - best_single)/best_single*100
        label = {'equal_raw':'原始等权','equal_norm':'等权归一','ic_norm':'IC加权归一','ir_norm':'IR加权归一','pos_only':'正向5因子'}[scheme_name]
        flag = '✅ 提升' if abs(mean_ic) > best_single else ('⚠️ 持平' if abs(abs(mean_ic)-best_single)<0.001 else '❌ 下降')
        print(f"  {label:10s}: IC={mean_ic:+7.4f} | 最佳单因子={best_single:+7.4f} | {flag} ({improv:+.1f}%)")
    
    print(f"{'='*70}")
    
    fp = f'/tmp/h5_ic_{time.strftime("%Y%m%d_%H%M%S")}.json'
    with open(fp, 'w') as f:
        json.dump({k: {'mean_ic': round(float(np.mean(v)), 4), 
                       'ir': round(float(np.mean(v)/max(np.std(v),1e-10)), 2),
                       'pos_pct': round(float(sum(1 for ic in v if ic>0)/len(v)*100), 1) if v else 0,
                       'n_days': len(v),
                       'daily_ics': [round(x,4) for x in v]}
                   for k,v in h5_results.items() if v}, f, indent=2)
    print(f"📁 {fp}")


if __name__ == '__main__':
    import pandas as pd
    main()
