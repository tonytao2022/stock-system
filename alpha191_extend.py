#!/usr/bin/env python3
"""
Alpha191 第二批因子扩展 - 从18个扩展到60个
使用批量SQL加载DataFrame，向量化计算
"""
import json, math, time, os, gc, warnings
import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from collections import defaultdict
import pymysql
import subprocess

warnings.filterwarnings('ignore')

def db_pass():
    r = subprocess.run(['grep','password','/etc/mysql/debian.cnf'], capture_output=True, text=True)
    for line in r.stdout.strip().split('\n'):
        if 'password' in line:
            return line.split('=')[-1].strip()
    return ''

PWD = db_pass()

# =============== 新因子定义（α006~α060缺失的那些） ===============
ALPHA_META = {
    'alpha006': ('量价秩相关R', "corr(rank(open),rank(vol),10)的变体"),
    'alpha007': ('日内量与振幅', "RET*VOL/MAX VOL"),
    'alpha008': ('量价RS', "RSI-like 量价"),
    'alpha009': ('量价序列', "序列相关性"),
    'alpha010': ('cov量价', "cov(close,vol)"),
    'alpha011': ('量价位置', "(close-open)/high-low"),
    'alpha012': ('开盘量比', "open/vol"),
    'alpha013': ('日内波动量', "high-low*vol"),
    'alpha015': ('开盘跳空', "(open-close.shift(1))/close.shift(1)"),
    'alpha016': ('日内量价秩', "rank(high)-rank(low)"),
    'alpha017': ('量价倒数', "1/close*vol"),
    'alpha019': ('5日涨跌幅条件', "if ret>0 then vol"),
    'alpha021': ('条件量价', "conditional volume"),
    'alpha022': ('量价差', "close-open-mean"),
    'alpha023': ('高低比量', "high/low*vol"),
    'alpha024': ('收盘量价', "close*vol"),
    'alpha025': ('量与振幅', "ret*vol"),
    'alpha026': ('累积量比', "cumsum(vol)/trade_days"),
    'alpha027': ('量比', "vol/mean(vol,5)"),
    'alpha028': ('3日量比', "vol/mean(vol,3)"),
    'alpha029': ('开盘量', "open*vol"),
    'alpha030': ('高低范围', "high-low"),
    'alpha032': ('高中量相关', "corr(high,close,vol)"),
    'alpha033': ('5日量比', "vol/mean(vol,5)*100"),
    'alpha035': ('量价变化', "close/close.shift(5)*vol/vol.shift(5)"),
    'alpha037': ('RSI型因子', "rsi-like volume"),
    'alpha038': ('趋势量', "ma(vol,10)-ma(vol,30)"),
    'alpha039': ('量价比例', "close/ma(close,10)*vol/ma(vol,10)"),
    'alpha041': ('量价异常', "|close-ma(close,20)|/close"),
    'alpha042': ('量价峰度', "峰度指标"),
    'alpha044': ('量变化', "delta(log(vol),5)"),
    'alpha045': ('量斜率', "slope(vol,5)"),
    'alpha047': ('量价衰减', "衰减加权"),
    'alpha048': ('方向变化+量', "sign(close)*log(vol)"),
    'alpha049': ('趋势衰竭', "close趋势减量"),
    'alpha050': ('量价脉冲', "price*vol/ma(vol)"),
    'alpha051': ('高位放量', "close>ma(close,30)&vol>ma(vol,30)*1.5"),
    'alpha052': ('低位缩量', "close<ma(close,30)&vol<ma(vol,30)*0.5"),
    'alpha053': ('量价加速', "acceleration"),
    'alpha054': ('量价减速', "deceleration"),
    'alpha056': ('开盘位置', "(open-low)/(high-low)"),
    'alpha057': ('收盘偏度', "skew(close,5)"),
    'alpha058': ('量价峰度2', "kurt(close*vol)"),
    'alpha059': ('量价左偏', "left_skew"),
    'alpha060': ('量价右偏', "right_skew"),
}

# 保留已有结果
EXISTING_KEYS = ['alpha001','alpha002','alpha003','alpha004','alpha005',
                 'alpha014','alpha018','alpha020','alpha031','alpha034',
                 'alpha036','alpha040','alpha043','alpha046','alpha055',
                 'alpha062','alpha089']


def get_all_codes(limit=300):
    conn = pymysql.connect(host='localhost', user='debian-sys-maint', 
                           password=PWD, database='stock_db_v2', charset='utf8mb4')
    df = pd.read_sql("""
        SELECT ts_code, COUNT(*) as cnt FROM daily_kline 
        WHERE trade_date >= '2023-01-01'
        GROUP BY ts_code HAVING cnt >= 300
        ORDER BY cnt DESC LIMIT %d
    """ % limit, conn)
    conn.close()
    return df['ts_code'].tolist()


def load_kline_batch(codes, min_date='2022-01-01'):
    """批量加载K线"""
    conn = pymysql.connect(host='localhost', user='debian-sys-maint',
                           password=PWD, database='stock_db_v2', charset='utf8mb4')
    codes_str = ','.join(["'%s'" % c for c in codes])
    sql = """
        SELECT ts_code, trade_date, `open`, high, low, `close`, vol
        FROM daily_kline WHERE ts_code IN (%s) AND trade_date >= '%s'
        ORDER BY ts_code, trade_date
    """ % (codes_str, min_date)
    df = pd.read_sql(sql, conn)
    conn.close()
    return df


def compute_batch_factors(df):
    """对DataFrame批量计算因子"""
    factors = {}
    
    # 准备基础列
    codes = df['ts_code'].unique()
    n = len(df)
    
    # 按股票分组
    grouped = df.groupby('ts_code')
    
    # ===== 流畅变化的因子（直接分组计算）=====
    
    # α006: corr(rank(open),rank(vol),10)
    def alpha006(g):
        r_o = g['open'].rank() / len(g)
        r_v = g['vol'].rank() / len(g)
        return r_o.rolling(10).corr(r_v).rename('alpha006')
    factors['alpha006'] = grouped[['open','vol']].apply(lambda g: alpha006(g).reindex(g.index))
    
    # α007: 日内量与振幅
    factors['alpha007'] = grouped.apply(lambda g: (g['close'] - g['open']).abs() * g['vol'] / g['vol'].max()).values
    
    # α011: (close-open)/(high-low)
    factors['alpha011'] = grouped.apply(lambda g: (g['close']-g['open'])/(g['high']-g['low']+1e-10)).values
    
    # α015: 开盘跳空
    factors['alpha015'] = grouped['open'].pct_change().values
    
    # α016: rank(high)-rank(low) → 截面排名，但先简化
    def alpha016(g):
        r_h = g['high'].rank()
        r_l = g['low'].rank()
        return (r_h - r_l).rename('alpha016')
    factors['alpha016'] = grouped[['high','low']].apply(lambda g: alpha016(g).reindex(g.index))
    
    # α022: close-open (简化)
    factors['alpha022'] = (df['close'] - df['open']).values
    
    # α023: high/low
    factors['alpha023'] = (df['high'] / (df['low']+1e-10)).values
    
    # α024: close*vol
    factors['alpha024'] = (df['close'] * df['vol']).values
    
    # α030: high-low
    factors['alpha030'] = (df['high'] - df['low']).values
    
    # α012: open/vol
    factors['alpha012'] = (df['open'] / (df['vol']+1e-10)).values
    
    # α017: 1/close*vol
    factors['alpha017'] = (1 / (df['close']+1e-10) * df['vol']).values
    
    # α025: ret*vol
    factors['alpha025'] = (df.groupby('ts_code')['close'].pct_change() * df['vol']).values
    
    # α027: vol/MA(vol,5)
    factors['alpha027'] = (df['vol'] / (df.groupby('ts_code')['vol'].transform(lambda x: x.rolling(5).mean())+1e-10)).values
    
    # α028: vol/MA(vol,3)
    factors['alpha028'] = (df['vol'] / (df.groupby('ts_code')['vol'].transform(lambda x: x.rolling(3).mean())+1e-10)).values
    
    # α029: open*vol
    factors['alpha029'] = (df['open'] * df['vol']).values
    
    # α033: vol/MA(vol,5)*100
    factors['alpha033'] = factors['alpha027'] * 100
    
    # α035: close/close.shift(5)*vol/vol.shift(5)
    factors['alpha035'] = (df.groupby('ts_code')['close'].pct_change(5).add(1) * 
                          (df.groupby('ts_code')['vol'].pct_change(5).add(1))).values
    
    # α041: |close-MA(close,20)|/close
    ma20 = df.groupby('ts_code')['close'].transform(lambda x: x.rolling(20).mean())
    factors['alpha041'] = ((df['close'] - ma20).abs() / (df['close']+1e-10)).values
    
    # α044: delta(log(vol),5)
    factors['alpha044'] = df.groupby('ts_code')['vol'].transform(lambda x: np.log(x+1).diff(5)).values
    
    # α056: (open-low)/(high-low)
    factors['alpha056'] = ((df['open']-df['low'])/(df['high']-df['low']+1e-10)).values
    
    # α008: RSI-like (close变化)
    def alpha008(g):
        delta = g['close'].diff()
        gain = delta.clip(lower=0).rolling(6).mean()
        loss = (-delta).clip(lower=0).rolling(6).mean()
        rs = gain / (loss+1e-10)
        return (100 - 100 / (1+rs)).rename('alpha008')
    factors['alpha008'] = grouped[['close']].apply(lambda g: alpha008(g).reindex(g.index))
    
    # α026: cumsum(vol)/trade_days → 简化用累计均值
    def alpha026(g):
        return (g['vol'].expanding().mean()).rename('alpha026')
    factors['alpha026'] = grouped[['vol']].apply(lambda g: alpha026(g).reindex(g.index))
    
    # α038: MA(vol,10)-MA(vol,30)
    ma10 = df.groupby('ts_code')['vol'].transform(lambda x: x.rolling(10).mean())
    ma30 = df.groupby('ts_code')['vol'].transform(lambda x: x.rolling(30).mean())
    factors['alpha038'] = (ma10 - ma30).values
    
    # α049: 趋势衰竭 (close趋势放缓)
    def alpha049(g):
        ma5 = g['close'].rolling(5).mean()
        ma20 = g['close'].rolling(20).mean()
        return (ma5 < ma20).astype(float).rename('alpha049')
    factors['alpha049'] = grouped[['close']].apply(lambda g: alpha049(g).reindex(g.index))
    
    return factors


def compute_ic_all_stocks(codes, factor_names, existing={}):
    """逐只加载股票数据，计算因子+IC"""
    # 收集因子-IC数据
    pool = defaultdict(lambda: defaultdict(list))
    
    t0 = time.time()
    for i, code in enumerate(codes):
        if (i+1) % 50 == 0:
            print(f"  [{i+1}/{len(codes)}] {time.time()-t0:.0f}s")
        
        conn = pymysql.connect(host='localhost', user='debian-sys-maint',
                               password=PWD, database='stock_db_v2', charset='utf8mb4')
        df = pd.read_sql("""
            SELECT trade_date, `open`, high, low, `close`, vol
            FROM daily_kline WHERE ts_code='%s' AND trade_date >= '2020-01-01'
            ORDER BY trade_date
        """ % code, conn)
        conn.close()
        
        if len(df) < 200: continue
        
        # 用逐股票版计算
        c = df['close'].values.astype(float)
        o = df['open'].values.astype(float)
        h = df['high'].values.astype(float)
        l = df['low'].values.astype(float)
        v = df['vol'].values.astype(float)
        n = len(df)
        
        # 未来5日收益
        fwd = np.full(n, np.nan)
        for j in range(n-5):
            fwd[j] = c[j+5] / c[j] - 1
        
        fac_dict = {}
        for _fn in factor_names:
            fac_dict[_fn] = np.full(n, np.nan)
        
        # α006: corr(rank(open),rank(vol),10) [简化版]
        for j in range(9, n):
            v_o = pd.Series(o[j-9:j+1]).rank(pct=True).values
            v_v = pd.Series(v[j-9:j+1]).rank(pct=True).values
            if np.std(v_o)>0 and np.std(v_v)>0:
                fac_dict['alpha006'][j] = np.corrcoef(v_o, v_v)[0,1]
        
        # α007: 日内振幅*vol
        for j in range(n):
            fac_dict['alpha007'][j] = abs(c[j]-o[j]) * v[j]
        
        # α008: RSI
        for j in range(6, n):
            gain = sum(c[j-k]-c[j-k-1] for k in range(6) if c[j-k] > c[j-k-1])
            loss = sum(c[j-k-1]-c[j-k] for k in range(6) if c[j-k] < c[j-k-1])
            if loss > 0:
                fac_dict['alpha008'][j] = 100 - 100/(1+gain/(loss+1e-10))
        
        # α010: cov(close,vol,5)
        for j in range(4, n):
            fac_dict['alpha010'][j] = np.cov(c[j-4:j+1], v[j-4:j+1])[0,1]
        
        # α011: (close-open)/(high-low)
        for j in range(n):
            fac_dict['alpha011'][j] = (c[j]-o[j])/(h[j]-l[j]+1e-10)
        
        # α012: open/vol
        for j in range(n):
            fac_dict['alpha012'][j] = o[j]/(v[j]+1e-10)
        
        # α015: 开盘跳空
        for j in range(1, n):
            fac_dict['alpha015'][j] = (o[j]-c[j-1])/(c[j-1]+1e-10)
        
        # α016: rank(high)-rank(low)
        for j in range(30, n):
            rh = pd.Series(h[j-29:j+1]).rank(pct=True).iloc[-1]
            rl = pd.Series(l[j-29:j+1]).rank(pct=True).iloc[-1]
            fac_dict['alpha016'][j] = rh - rl
        
        # α017: vol/close
        for j in range(n):
            fac_dict['alpha017'][j] = v[j]/(c[j]+1e-10)
        
        # α022: close-open
        for j in range(n):
            fac_dict['alpha022'][j] = c[j] - o[j]
        
        # α023: high/low
        for j in range(n):
            fac_dict['alpha023'][j] = h[j]/(l[j]+1e-10)
        
        # α024: close*vol
        for j in range(n):
            fac_dict['alpha024'][j] = c[j] * v[j]
        
        # α025: 收益率*vol
        for j in range(1, n):
            fac_dict['alpha025'][j] = (c[j]/c[j-1]-1) * v[j]
        
        # α027: vol/MA(vol,5)
        for j in range(4, n):
            fac_dict['alpha027'][j] = v[j]/(np.mean(v[j-4:j+1])+1e-10)
        
        # α028: vol/MA(vol,3)
        for j in range(2, n):
            fac_dict['alpha028'][j] = v[j]/(np.mean(v[j-2:j+1])+1e-10)
        
        # α029: open*vol
        for j in range(n):
            fac_dict['alpha029'][j] = o[j] * v[j]
        
        # α030: high-low
        for j in range(n):
            fac_dict['alpha030'][j] = h[j] - l[j]
        
        # α033: vol/MA(vol,5)*100
        for j in range(4, n):
            fac_dict['alpha033'][j] = v[j]/(np.mean(v[j-4:j+1])+1e-10)*100
        
        # α035: close/close.shift(5)*vol/vol.shift(5)
        for j in range(5, n):
            fac_dict['alpha035'][j] = (c[j]/c[j-5])*(v[j]/v[j-5])
        
        # α041: |close-MA20|/close
        for j in range(19, n):
            fac_dict['alpha041'][j] = abs(c[j]-np.mean(c[j-19:j+1]))/(c[j]+1e-10)
        
        # α044: delta(log(vol),5)
        for j in range(5, n):
            fac_dict['alpha044'][j] = math.log(v[j]+1) - math.log(v[j-5]+1)
        
        # α056: (open-low)/(high-low)
        for j in range(n):
            fac_dict['alpha056'][j] = (o[j]-l[j])/(h[j]-l[j]+1e-10)
        
        # α049: 趋势衰竭 (MA5<MA20)
        for j in range(19, n):
            ma5 = np.mean(c[j-4:j+1])
            ma20 = np.mean(c[j-19:j+1])
            fac_dict['alpha049'][j] = 1.0 if ma5 < ma20 else 0.0
        
        # α038: MA10-MA30
        for j in range(29, n):
            ma10 = np.mean(c[j-9:j+1])
            ma30 = np.mean(c[j-29:j+1])
            fac_dict['alpha038'][j] = ma10 - ma30
        
        # α026: 累积均值量
        cum = 0
        for j in range(n):
            cum += v[j]
            fac_dict['alpha026'][j] = cum/(j+1)
        
        # 收集IC数据
        for fname in factor_names:
            if fname not in fac_dict: continue
            fv = fac_dict[fname]
            for j in range(n):
                if math.isnan(fv[j]) or math.isnan(fwd[j]): continue
                pool[fname][str(df['trade_date'].iloc[j])].append((fv[j], fwd[j]))
    
    return pool


def main():
    t0 = time.time()
    
    # 加载已有结果
    existing = {}
    try:
        with open('/opt/stock-analyzer/alpha191_ic_20260713_0003.json') as f:
            existing = json.load(f)
        print(f"✅ 已有{len(existing)}个因子结果")
    except:
        pass
    
    # 新因子：除去已有17个
    all_new = [k for k in ALPHA_META if k not in EXISTING_KEYS]
    print(f"📊 待测新因子: {len(all_new)}个 ({', '.join(all_new[:5])}...)")
    
    # 选300只股票
    codes = get_all_codes(300)
    print(f"📈 股票: {len(codes)}只")
    
    # 跑IC
    pool = compute_ic_all_stocks(codes, all_new, existing)
    
    # 计算IC
    results = dict(existing)
    print(f"\n{'='*70}")
    print(f"  📊 Alpha191 IC 扩展({len(all_new)}个因子)")
    print(f"{'='*70}")
    print(f"  {'因子':14s} {'中文名':12s} {'平均IC':>8s} {'IR':>5s} {'正IC%':>6s} {'有效天':>6s}")
    print(f"  {'─'*56}")
    
    for fname in all_new:
        p = pool.get(fname, {})
        if not p: continue
        daily_ics = []
        for td, pairs in p.items():
            if len(pairs) < 10: continue
            fv = np.array([x[0] for x in pairs])
            rv = np.array([x[1] for x in pairs])
            if np.std(fv)<1e-10 or np.std(rv)<1e-10: continue
            rho, _ = spearmanr(fv, rv)
            if not math.isnan(rho): daily_ics.append(rho)
        
        if len(daily_ics) < 5: continue
        ic_arr = np.array(daily_ics)
        mean_ic = float(np.mean(ic_arr))
        std_ic = float(np.std(ic_arr))
        ir = mean_ic/std_ic if std_ic>0 else 0
        pos_pct = float(sum(1 for ic in daily_ics if ic>0)/len(daily_ics)*100)
        
        m = abs(mean_ic)
        icon = '✅' if m>=0.020 else ('⚡' if m>=0.010 else '❌')
        print(f"  {fname:14s} {ALPHA_META.get(fname,('',))[0]:12s} {mean_ic:+8.4f}{icon} {ir:5.2f} {pos_pct:6.1f}% {len(daily_ics):6d}")
        
        results[fname] = {
            'name': ALPHA_META.get(fname, ('',))[0],
            'ic': round(mean_ic, 4),
            'ir': round(ir, 2),
            'pos_pct': round(pos_pct, 1),
            'n_days': len(daily_ics),
        }
    
    # 输出
    sorted_res = sorted(results.items(), key=lambda x: abs(x[1]['ic']), reverse=True)
    qualifying = [x for x in sorted_res if abs(x[1]['ic']) >= 0.020]
    
    out_path = f'/opt/stock-analyzer/alpha191_ic_{time.strftime("%Y%m%d_%H%M")}.json'
    with open(out_path, 'w') as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    
    print(f"\n{'='*70}")
    print(f"  📋 汇总: 总计{len(results)}个 | IC>=0.020共{len(qualifying)}个")
    print(f"  ⏱  {time.time()-t0:.0f}s | 💾 {out_path}")
    print(f"{'='*70}")
    
    # 输出结果文件
    lines = [f"Alpha191 扩展因子IC | {time.strftime('%Y-%m-%d %H:%M')}"]
    for label, items in [('✅ IC>=0.020', qualifying)]:
        lines.append(f"\n── {label} ──")
        for fn, r in items:
            if fn in EXISTING_KEYS: continue
            lines.append(f"  {fn:14s} IC={r['ic']:+7.4f} IR={r['ir']:5.2f} 正IC{r['pos_pct']:6.1f}% N{r['n_days']}日")
    
    with open('/opt/stock-analyzer/alpha191_ic_results.txt', 'a') as f:
        f.write('\n\n' + '\n'.join(lines))
    
    print('\n✅ 结果已追加到alpha191_ic_results.txt')


if __name__ == '__main__':
    main()
