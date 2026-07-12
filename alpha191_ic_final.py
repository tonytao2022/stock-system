#!/usr/bin/env python3
"""
Alpha191 因子 IC v6 — 批量加载+高效计算
========================================
一次SQL加载全部K线，分股票计算，28个因子
"""

import os, sys, json, math, time, gc
import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from collections import defaultdict
sys.path.insert(0, '/opt/stock-analyzer')
import db_config
import pymysql

START = '2024-01-02'
END = '2026-07-10'

ALPHA_NAMES = {
    'alpha001': '量价背离',     'alpha002': '日内振幅变化',
    'alpha004': '收盘组合判断', 'alpha005': '量价时序相关',
    'alpha011': '量价位置因子', 'alpha014': '5日涨幅',
    'alpha018': '5日收盘比',    'alpha019': '5日涨跌幅条件',
    'alpha020': '6日涨幅',      'alpha031': '12日偏离度',
    'alpha032': '高中量相关',   'alpha034': '12日均线比',
    'alpha036': '量价秩相关',   'alpha040': '量比功率',
    'alpha043': '净量因子',     'alpha046': '多均线位置',
    'alpha048': '方向变化+量',  'alpha055': '随机指标',
    'alpha056': '开盘位置',     'alpha062': '高量负相关',
    'alpha064': '量价强度',     'alpha084': '累积上涨',
    'alpha087': '日内波动',     'alpha089': '高低量相关',
    'alpha092': '衰减量价',     'alpha094': '相对强度+量',
    'alpha102': '三维量价',     'alpha108': '相对波动',
}


def compute_alphas_numpy(c_arr, o_arr, h_arr, l_arr, v_arr, tds):
    """numpy批量计算所有因子，返回 {td_str: {alpha: value}}"""
    n = len(c_arr)
    if n < 30:
        return {}
    
    c = c_arr.astype(float); o = o_arr.astype(float)
    h = h_arr.astype(float); l = l_arr.astype(float)
    v = v_arr.astype(float)
    
    results = {}
    
    for i in range(29, n):
        vals = {}
        try:
            ci = c[i]; oi = o[i]; hi = h[i]; li = l[i]; vi = v[i]
            
            # 基础类（无循环）
            if i>=5: vals['alpha014'] = ci - c[i-5]
            if i>=5 and c[i-5]>0: vals['alpha018'] = ci/c[i-5]
            if i>=5:
                d=ci-c[i-5]; vals['alpha019']=-1 if abs(d)<0.001 else (1 if d>0 else -1)
            if i>=6 and c[i-6]>0: vals['alpha020']=(ci-c[i-6])/c[i-6]*100
            if i>=11:
                ma12=c[i-11:i+1].mean()
                if ma12>0: vals['alpha031']=(ci-ma12)/ma12*100
            if i>=11 and ci>0: vals['alpha034']=c[i-11:i+1].mean()/ci
            if i>=23 and ci>0:
                ma3=c[i-2:i+1].mean(); ma6=c[i-5:i+1].mean()
                ma12=c[i-11:i+1].mean(); ma24=c[i-23:i+1].mean()
                vals['alpha046']=(ma3+ma6+ma12+ma24)/(4*ci)
            if i>=11:
                hi12=h[i-11:i+1].max(); lo12=l[i-11:i+1].min()
                if hi12>lo12: vals['alpha055']=(ci-lo12)/(hi12-lo12)*100
            if i>=11:
                hi12=h[i-11:i+1].max(); lo12=l[i-11:i+1].min()
                vals['alpha056']=(ci-lo12)/max(hi12-lo12,0.001)
            if ci>0:
                vals['alpha087']=(hi-li)/ci
                if vi>0: vals['alpha102']=vi*(hi-li)/ci
            if oi>0 and vi>0:
                vals['alpha064']=(ci-oi)/oi*vi
                vals['alpha094']=-1*(ci-oi)/oi*vi
            if ci>0 and li>0: vals['alpha108']=-1*((hi-li)/ci)*(hi/li)
            if oi>0: vals['alpha004']=(ci-oi)*(hi-li)/(oi+0.001)
            
            # 含循环的因子
            if i>=26:
                up=np.sum(v[i-25:i+1][c[i-25:i+1]>c[i-26:i]])
                dn=np.sum(v[i-25:i+1][c[i-25:i+1]<=c[i-26:i]])
                vals['alpha040']=up/max(dn,1)*100
            
            if i>=6:
                net=0.0
                for j in range(i-5,i+1): net+=v[j] if c[j]>c[j-1] else -v[j]
                vals['alpha043']=net
            
            if i>=20:
                vals['alpha084']=float(np.sum(np.maximum(c[i-19:i+1]-c[i-20:i],0)))
            
            if i>=5:
                s=0.0
                for j in range(i-5,i+1):
                    hlj=max(h[j]-l[j],0.001)
                    s+=((c[j]-l[j])-(h[j]-c[j]))/hlj*v[j]
                vals['alpha011']=s
            
            # 相关性因子
            if i>=5:
                v7=np.maximum(v[i-5:i+1],1)
                dvol=np.diff(np.log(v7))
                ret7=(c[i-5:i+1]-o[i-5:i+1])/np.maximum(o[i-5:i+1],0.001)
                if len(dvol)>=5:
                    r1=pd.Series(dvol).rank(pct=True).values
                    r2=pd.Series(ret7).rank(pct=True).values
                    if np.std(r1)>0 and np.std(r2)>0:
                        vals['alpha001']=float(-np.corrcoef(r1,r2)[0,1])
            
            if i>=1:
                v_now=((ci-li)-(hi-ci))/max(hi-li,0.001)
                v_prev=((c[i-1]-l[i-1])-(h[i-1]-c[i-1]))/max(h[i-1]-l[i-1],0.001)
                vals['alpha002']=-(v_now-v_prev)
            
            if i>=4:
                c5=c[i-4:i+1]; v5=v[i-4:i+1]
                rc=pd.Series(c5).rank(pct=True).values
                rv=pd.Series(v5).rank(pct=True).values
                if np.std(rc)>0 and np.std(rv)>0:
                    vals['alpha005']=float(-np.corrcoef(rc,rv)[0,1])
            
            if i>=6 and ci>0 and vi>0:
                vals['alpha032']=(c[i-6:i+1].mean()-ci)/ci*vi
            
            if i>=5:
                c6=c[i-5:i+1]; v6=v[i-5:i+1]
                rc=pd.Series(c6).rank(pct=True).values
                rv=pd.Series(v6).rank(pct=True).values
                if np.std(rc)>0 and np.std(rv)>0:
                    vals['alpha036']=float(np.corrcoef(rc,rv)[0,1])
            
            if i>=4:
                c5=c[i-4:i+1]; v5=v[i-4:i+1]
                if np.std(c5)>0 and np.std(v5)>0:
                    corr=np.corrcoef(c5,v5)[0,1]
                    vals['alpha048']=float(corr*(ci-c[i-1]))
            
            if i>=4:
                h5=h[i-4:i+1]; v5=v[i-4:i+1]
                if np.std(h5)>0 and np.std(v5)>0:
                    vals['alpha062']=float(-np.corrcoef(h5,v5)[0,1])
            
            if i>=12:
                c13=c[i-12:i+1]; v13=v[i-12:i+1]
                if np.std(c13)>0 and np.std(v13)>0:
                    vals['alpha089']=float(1-np.corrcoef(c13,v13)[0,1])
            
            if i>=12 and ci>0 and vi>0:
                hi13=c[i-12:i+1].max(); lo13=c[i-12:i+1].min()
                vals['alpha092']=(hi13-lo13)/ci*vi
            
        except Exception:
            pass
        
        if vals:
            results[tds[i]] = vals
    
    return results


def main():
    t0 = time.time()
    
    # 1. 一次性SQL加载
    pwd = db_config._get_password()
    conn = pymysql.connect(host='127.0.0.1', port=3306, user='debian-sys-maint',
                           password=pwd, database='stock_db_v2', charset='utf8mb4',
                           cursorclass=pymysql.cursors.DictCursor)
    cur = conn.cursor()
    
    print(f"⏳ 获取股票列表...", end='', flush=True)
    cur.execute("SELECT ts_code FROM backtest_pool")
    codes = [r['ts_code'] for r in cur.fetchall()]
    print(f" {len(codes)}只")
    
    print(f"⏳ 加载K线 {START}~{END}...", end='', flush=True)
    cur.execute(f"""
        SELECT dk.ts_code, dk.trade_date, dk.`open`, dk.high, dk.low, dk.`close`, dk.vol
        FROM daily_kline dk
        INNER JOIN backtest_pool bp ON dk.ts_code = bp.ts_code
        WHERE dk.trade_date>=%s AND dk.trade_date<=%s
        ORDER BY dk.ts_code, dk.trade_date
    """, (START, END))
    all_rows = cur.fetchall()
    conn.close()
    print(f" {len(all_rows)}条 ({(time.time()-t0):.0f}s)")
    
    # 2. 按股票分组
    t1 = time.time()
    stock_groups = defaultdict(list)
    for r in all_rows:
        stock_groups[r['ts_code']].append(r)
    del all_rows
    gc.collect()
    print(f"  分组完成 ({time.time()-t1:.0f}s), {len(stock_groups)}只")
    
    # 3. 构建交易日索引（所有股票都有数据的交易日）
    date_close = defaultdict(dict)
    for code, rows in stock_groups.items():
        for r in rows:
            td = r['trade_date'].strftime('%Y-%m-%d')
            date_close[td][code] = float(r['close'])
    alld = sorted(date_close.keys())
    print(f"  交易日: {len(alld)}天")
    
    # 4. 逐股票计算
    print(f"\n⏳ 计算Alpha因子 (28个)...")
    alpha_records = defaultdict(lambda: defaultdict(list))
    
    t2 = time.time()
    total = len(stock_groups)
    for idx, (code, rows) in enumerate(stock_groups.items()):
        if idx % 100 == 0:
            print(f"  [{idx}/{total}] {time.time()-t2:.0f}s")
        
        if len(rows) < 30:
            continue
        
        # numpy数组
        c_arr = np.array([float(r['close']) for r in rows])
        o_arr = np.array([float(r['open']) for r in rows])
        h_arr = np.array([float(r['high']) for r in rows])
        l_arr = np.array([float(r['low']) for r in rows])
        v_arr = np.array([float(r['vol']) for r in rows])
        tds = [r['trade_date'].strftime('%Y-%m-%d') for r in rows]
        
        result = compute_alphas_numpy(c_arr, o_arr, h_arr, l_arr, v_arr, tds)
        
        for td, factors in result.items():
            td_idx = alld.index(td) if td in alld else -1
            if td_idx < 0 or td_idx + 5 >= len(alld):
                continue
            future_date = alld[td_idx + 5]
            tc = date_close[td].get(code)
            fc = date_close[future_date].get(code)
            if tc is None or fc is None or tc == 0:
                continue
            fwd_ret = (fc / tc - 1) * 100
            
            for aname, aval in factors.items():
                if aval is not None and not math.isnan(aval) and not math.isinf(aval):
                    alpha_records[aname][td].append((aval, fwd_ret))
    
    elapsed = time.time() - t2
    
    # 5. 计算IC
    print(f"\n\n{'='*70}")
    print(f"  📊 Alpha191 因子 IC 验证（5日持有期）")
    print(f"  {total}只 | {len(alld)}交易日 | {START}~{END} | 耗时{elapsed:.0f}s")
    print(f"  ✅|IC|>=0.025 | ⚡0.01~0.024 | ❌<0.01")
    print(f"{'='*70}")
    print(f"  {'因子':12s} {'中文名':10s} {'平均IC':>8s} {'IR':>5s} {'正IC%':>6s} {'有效天':>6s}")
    print(f"  {'─'*52}")
    
    ic_results = {}
    for aname in sorted(ALPHA_NAMES.keys()):
        records = alpha_records.get(aname, {})
        if not records:
            continue
        
        daily_ics = []
        total_pairs = 0
        for td, pairs in records.items():
            if len(pairs) < 10:
                continue
            vals, rets = zip(*pairs)
            try:
                rho, _ = spearmanr(vals, rets)
                if not math.isnan(rho):
                    daily_ics.append(rho)
                    total_pairs += len(pairs)
            except:
                pass
        
        if len(daily_ics) < 5:
            continue
        
        ic_arr = np.array(daily_ics)
        mean_ic = np.mean(ic_arr)
        std_ic = np.std(ic_arr)
        ir = mean_ic / std_ic if std_ic > 0 else 0
        pos_pct = sum(1 for ic in daily_ics if ic > 0) / len(daily_ics) * 100
        
        cname = ALPHA_NAMES.get(aname, '')
        m = abs(mean_ic)
        icon = '✅' if m >= 0.025 else ('⚡' if m >= 0.01 else '❌')
        print(f"  {aname:12s} {cname:10s} {mean_ic:+7.4f}{icon} {ir:5.2f} {pos_pct:5.0f}% {len(daily_ics):6d}")
        
        ic_results[aname] = {
            'name': cname, 'ic': round(mean_ic, 4), 'ir': round(ir, 2),
            'pos_pct': round(pos_pct, 1), 'n_days': len(daily_ics), 'n_pairs': total_pairs
        }
    
    sorted_ics = sorted(ic_results.items(), key=lambda x: abs(x[1]['ic']), reverse=True)
    qualifying = [x for x in sorted_ics if abs(x[1]['ic']) >= 0.025]
    weak = [x for x in sorted_ics if 0.01 <= abs(x[1]['ic']) < 0.025]
    bad = [x for x in sorted_ics if abs(x[1]['ic']) < 0.01]
    
    for label, items in [('✅ 达到门槛 IC>=0.025', qualifying),
                         ('⚡ 弱相关 0.01~0.024', weak),
                         ('❌ 无效 <0.01', bad)]:
        if not items:
            continue
        print(f"\n{'─'*70}")
        print(f"  {label}")
        print(f"{'─'*70}")
        print(f"  {'因子':12s} {'中文名':10s} {'IC':>8s} {'IR':>5s} {'正IC%':>6s} {'样本':>8s}")
        print(f"  {'─'*52}")
        for aname, r in items:
            print(f"  {aname:12s} {r['name']:10s} {r['ic']:+7.4f} {r['ir']:5.2f} {r['pos_pct']:5.0f}% {r['n_pairs']:>8,}")
    
    print(f"\n{'='*70}")
    print(f"  📋 汇总: ✅{len(qualifying)}个 | ⚡{len(weak)}个 | ❌{len(bad)}个 | 总计{len(sorted_ics)}个")
    print(f"{'='*70}")
    
    fp = f'/tmp/alpha191_ic_v6_{time.strftime("%Y%m%d_%H%M%S")}.json'
    with open(fp, 'w') as f:
        json.dump({k: v for k,v in ic_results.items()}, f, indent=2, ensure_ascii=False)
    print(f"\n📁 {fp}")


if __name__ == '__main__':
    main()
