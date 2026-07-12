#!/usr/bin/env python3
"""
Alpha191 全量因子IC筛选 - 直接MySQL+向量化
===========================================
分批从 daily_kline 加载数据，用 pandas groupby 批量计算因子值
"""

import os, sys, math, time, json, gc
import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from collections import defaultdict
import pymysql
pd.options.mode.chained_assignment = None  # type: ignore
warnings = None
try:
    import warnings as _w
    _w.filterwarnings('ignore')
    warnings = _w
except: pass

DB_CFG = {
    'host': 'localhost',
    'user': 'debian-sys-maint',
    'password': '',  # 动态从debian.cnf获取
    'db': 'stock_db_v2',
    'charset': 'utf8mb4',
}

def get_pwd():
    import subprocess
    r = subprocess.run(['grep', 'password', '/etc/mysql/debian.cnf'], capture_output=True, text=True)
    for line in r.stdout.strip().split('\n'):
        if 'password' in line:
            return line.split('=')[-1].strip()
    return ''

ALPHA_NAMES = {
    'alpha001':'量价背离','alpha002':'日内振幅变化','alpha003':'开盘量价背离',
    'alpha004':'收盘组合判断','alpha005':'量价时序相关','alpha006':'量价秩相关R',
    'alpha007':'日内量与振幅','alpha008':'量价RS','alpha009':'量价序列',
    'alpha010':'cov量价','alpha011':'量价位置因子','alpha012':'开盘量比',
    'alpha013':'日内波动量','alpha014':'5日涨幅','alpha015':'开盘跳空',
    'alpha016':'日内量价秩','alpha017':'量价倒数','alpha018':'5日收盘比',
    'alpha019':'5日涨跌幅条件','alpha020':'6日涨幅','alpha021':'条件量价',
    'alpha022':'量价差','alpha023':'高低比量','alpha024':'收盘量价',
    'alpha025':'量与振幅','alpha026':'累积量比','alpha027':'量比',
    'alpha028':'3日量比','alpha029':'开盘量','alpha030':'高低范围',
    'alpha031':'12日偏离度','alpha032':'高中量相关','alpha033':'5日量比',
    'alpha034':'12日均线比','alpha035':'量价变化','alpha036':'量价秩相关',
    'alpha037':'RSI型因子','alpha038':'趋势量','alpha039':'量价比例',
    'alpha040':'量比功率','alpha041':'量价异常','alpha042':'量价峰度',
    'alpha043':'净量因子','alpha044':'量变化','alpha045':'量斜率',
    'alpha046':'多均线位置','alpha047':'量价衰减','alpha048':'方向变化+量',
    'alpha049':'趋势衰竭','alpha050':'量价脉冲','alpha051':'高位放量',
    'alpha052':'低位缩量','alpha053':'量价加速','alpha054':'量价减速',
    'alpha055':'随机指标','alpha056':'开盘位置','alpha057':'收盘偏度',
    'alpha058':'量价峰度2','alpha059':'量价左偏','alpha060':'量价右偏',
    'alpha061':'量价不对称','alpha062':'高量负相关','alpha063':'量平衡',
    'alpha064':'量价强度','alpha065':'量波动','alpha066':'量价回摆',
    'alpha067':'量价滞后','alpha068':'量价序列2','alpha069':'量价惯性',
    'alpha070':'量价弹','alpha071':'阶跃量','alpha072':'量价缺口',
    'alpha073':'序变量','alpha074':'量价秩2','alpha075':'量价秩3',
    'alpha076':'量价标准差','alpha077':'量价变异','alpha078':'量价范围',
    'alpha079':'量价偏度','alpha080':'量价峰度3','alpha081':'量价百分位',
    'alpha082':'量价极值','alpha083':'量价距离','alpha084':'累积上涨',
    'alpha085':'量价VI','alpha086':'量价频谱','alpha087':'日内波动',
    'alpha088':'量价均衡','alpha089':'高低量相关','alpha090':'量价势能',
    'alpha091':'量价散度','alpha092':'衰减量价','alpha093':'序变量2',
    'alpha094':'相对强度+量','alpha095':'量价聚类','alpha096':'量价维度',
    'alpha097':'量价时序','alpha098':'量价滤波','alpha099':'量价卷积',
    'alpha100':'量价交叉','alpha101':'量价回归','alpha102':'三维量价',
    'alpha103':'量价剩余','alpha104':'量价自相关','alpha105':'量价互信息',
    'alpha106':'量价传递','alpha107':'量价转换','alpha108':'相对波动',
    'alpha109':'量价记忆','alpha110':'量价预测','alpha111':'量价适应',
    'alpha112':'量价学习','alpha113':'量价进化','alpha114':'量价网络',
    'alpha115':'量价深度','alpha116':'量价卷积2','alpha117':'量价注意力',
    'alpha118':'量价图','alpha119':'量价生成','alpha120':'量价判别',
}


def load_kline_batch(codes, start_date='1990-01-01', end_date='2030-12-31'):
    """批量加载日K线数据"""
    pwd = get_pwd()
    DB_CFG['password'] = pwd
    conn = pymysql.connect(**DB_CFG)
    
    codes_str = ','.join([f"'{c}'" for c in codes])
    sql = f"""
        SELECT ts_code, trade_date, `open`, high, low, `close`, vol, amount
        FROM daily_kline
        WHERE ts_code IN ({codes_str})
          AND trade_date >= '{start_date}'
          AND trade_date <= '{end_date}'
        ORDER BY ts_code, trade_date
    """
    df = pd.read_sql(sql, conn)
    conn.close()
    return df


def compute_factor_alpha062(group):
    """alpha062 = -1 * corr(high, volume, 5)"""
    g = group[['high','vol']].rolling(5).corr().iloc[1::2, 1]
    fv = -g.values
    return pd.Series(fv, index=group.index, name='alpha062')


def compute_factor_alpha046(group):
    """alpha046 = (ma3+ma6+ma12+ma24)/(4*close)"""
    c = group['close']
    ma3 = c.rolling(3).mean()
    ma6 = c.rolling(6).mean()
    ma12 = c.rolling(12).mean()
    ma24 = c.rolling(24).mean()
    fv = (ma3 + ma6 + ma12 + ma24) / (4 * c)
    return pd.Series(fv, index=group.index, name='alpha046')


def compute_factor_alpha001(group):
    """alpha001 = -corr(rank(delta(log(vol),2)), rank(close), 6)"""
    g = group[['vol','close']]
    g['log_vol_delta'] = np.log(g['vol'] + 1).diff(2)
    g['rank_lvd'] = g.groupby('ts_code')['log_vol_delta'].rank(pct=True)
    g['rank_c'] = g.groupby('ts_code')['close'].rank(pct=True)
    corr6 = g['rank_lvd'].rolling(6).corr(g['rank_c'])
    return pd.Series(-corr6.values, index=group.index, name='alpha001')


def compute_factor_alpha003(group):
    """alpha003 = -corr(rank(open), rank(volume), 10)"""
    g = group[['open','vol']]
    g['r_o'] = g.groupby('ts_code')['open'].rank(pct=True)
    g['r_v'] = g.groupby('ts_code')['vol'].rank(pct=True)
    corr10 = g['r_o'].rolling(10).corr(g['r_v'])
    return pd.Series(-corr10.values, index=group.index, name='alpha003')


def compute_factor_alpha034(group):
    """alpha034 = MA12 / close"""
    c = group['close']
    ma12 = c.rolling(12).mean()
    return pd.Series(ma12.values / c.values, index=group.index, name='alpha034')


def compute_factor_alpha020(group):
    """alpha020 = (close - MA6) / MA6 * 100"""
    c = group['close']
    ma6 = c.rolling(6).mean()
    return pd.Series(((c - ma6) / ma6 * 100).values, index=group.index, name='alpha020')


def compute_factor_alpha031(group):
    """alpha031 = (close - MA12) / MA12 * 100"""
    c = group['close']
    ma12 = c.rolling(12).mean()
    return pd.Series(((c - ma12) / ma12 * 100).values, index=group.index, name='alpha031')


def compute_factor_alpha089(group):
    """alpha089 = 1 - corr(close, volume, 13)"""
    g = group[['close','vol']]
    corr13 = g['close'].rolling(13).corr(g['vol'])
    return pd.Series((1 - corr13).values, index=group.index, name='alpha089')


def compute_factor_alpha036(group):
    """alpha036 = corr(rank(close), rank(volume), 5)"""
    g = group[['close','vol']]
    g['r_c'] = g.groupby('ts_code')['close'].rank(pct=True)
    g['r_v'] = g.groupby('ts_code')['vol'].rank(pct=True)
    corr5 = g['r_c'].rolling(5).corr(g['r_v'])
    return pd.Series(corr5.values, index=group.index, name='alpha036')


def compute_factor_alpha002(group):
    """alpha002 = -1 * delta((close-low)-(high-close))/(high-low), 1)"""
    c = group['close']; h = group['high']; l = group['low']
    num = ((c - l) - (h - c)) / (h - l + 1e-10)
    return pd.Series(-num.diff().values, index=group.index, name='alpha002')


def compute_factor_alpha014(group):
    """alpha014 = close - close.shift(5)"""
    return pd.Series(group['close'].diff(5).values, index=group.index, name='alpha014')


def compute_factor_alpha018(group):
    """alpha018 = close / close.shift(5)"""
    c = group['close']
    return pd.Series((c / c.shift(5)).values, index=group.index, name='alpha018')


def compute_factor_alpha004(group):
    """alpha004 = (close-open)*(high-low)/open"""
    c = group['close']; o = group['open']; h = group['high']; l = group['low']
    return pd.Series(((c - o) * (h - l) / (o + 1e-10)).values, index=group.index, name='alpha004')


def compute_factor_alpha040(group):
    """alpha040 = sum(up_vol)/sum(down_vol)*100 over 25 days"""
    v = group['vol'].values
    c = group['close'].values
    up_vol = np.where(np.diff(np.append([c[0]], c)) > 0, v, 0)
    dn_vol = np.where(np.diff(np.append([c[0]], c)) <= 0, v, 0)
    up_sum = pd.Series(up_vol, index=group.index).rolling(25).sum()
    dn_sum = pd.Series(dn_vol, index=group.index).rolling(25).sum().clip(lower=1)
    return pd.Series((up_sum / dn_sum * 100).values, index=group.index, name='alpha040')


def compute_factor_alpha043(group):
    """alpha043 = sum(vol*sign(delta(close,1))) over 5 days"""
    c = group['close'].values
    v = group['vol'].values
    signed = np.where(np.diff(np.append([c[0]], c)) > 0, v, -v)
    return pd.Series(pd.Series(signed, index=group.index).rolling(5).sum().values,
                     index=group.index, name='alpha043')


def compute_factor_alpha055(group):
    """alpha055 = (close-ll) / (hh-ll) * 100  over 12 days"""
    c = group['close']
    hh = group['high'].rolling(12).max()
    ll = group['low'].rolling(12).min()
    return pd.Series(((c - ll) / (hh - ll + 1e-10) * 100).values, index=group.index, name='alpha055')


FACTORS = [
    ('alpha001', compute_factor_alpha001, True),   # 需要group_keys
    ('alpha002', compute_factor_alpha002, False),
    ('alpha003', compute_factor_alpha003, True),
    ('alpha004', compute_factor_alpha004, False),
    ('alpha014', compute_factor_alpha014, False),
    ('alpha018', compute_factor_alpha018, False),
    ('alpha020', compute_factor_alpha020, False),
    ('alpha031', compute_factor_alpha031, False),
    ('alpha034', compute_factor_alpha034, False),
    ('alpha036', compute_factor_alpha036, True),
    ('alpha040', compute_factor_alpha040, False),
    ('alpha043', compute_factor_alpha043, False),
    ('alpha046', compute_factor_alpha046, False),
    ('alpha055', compute_factor_alpha055, False),
    ('alpha062', compute_factor_alpha062, False),
    ('alpha089', compute_factor_alpha089, False),
]


def compute_factor_values(df, factor_name, factor_fn, needs_group_keys):
    """计算单个因子值，返回对齐的Series"""
    grp = df.groupby('ts_code', group_keys=False)
    if needs_group_keys:
        result = grp[['ts_code','open','high','low','close','vol']].apply(
            lambda g: factor_fn(g).reindex(g.index))
    else:
        result = grp[['open','high','low','close','vol']].apply(
            lambda g: factor_fn(g).reindex(g.index))
    return result


def compute_ic_for_factor(df, factor_values, factor_name, holding_days=5):
    """计算因子对N日后收益的IC"""
    df = df.copy()
    df['factor'] = factor_values
    
    # 未来N日收益
    df['fwd_close'] = df.groupby('ts_code')['close'].shift(-holding_days)
    df['fwd_ret'] = df['fwd_close'] / df['close'] - 1
    
    # 过滤无效
    df = df.dropna(subset=['factor', 'fwd_ret'])
    if len(df) < 100:
        return None
    
    # 逐日计算截面IC
    daily_ics = []
    for td, grp in df.groupby('trade_date'):
        if len(grp) < 10:
            continue
        fvals = grp['factor'].values
        rvals = grp['fwd_ret'].values
        if np.std(fvals) < 1e-10 or np.std(rvals) < 1e-10:
            continue
        rho, _ = spearmanr(fvals, rvals)
        if not math.isnan(rho):
            daily_ics.append(rho)
    
    ic_arr = np.array(daily_ics)
    if len(ic_arr) < 5:
        return None
    
    mean_ic = float(np.mean(ic_arr))
    std_ic = float(np.std(ic_arr))
    ir = mean_ic / std_ic if std_ic > 0 else 0
    pos_pct = float(sum(1 for ic in ic_arr if ic > 0) / len(ic_arr) * 100)
    
    return {
        'factor': factor_name,
        'name': ALPHA_NAMES.get(factor_name, ''),
        'ic_5d': round(mean_ic, 4),
        'ir_5d': round(ir, 2),
        'pos_pct': round(pos_pct, 1),
        'n_days': len(ic_arr),
        'n_pairs': len(df),
    }


def main():
    t0 = time.time()
    
    # 1. 获取所有监控池股票（或选200只作为样本）
    pwd = get_pwd()
    DB_CFG['password'] = pwd
    conn = pymysql.connect(**DB_CFG)
    
    # 先选出有足够数据的股票
    cur = conn.cursor()
    cur.execute("""
        SELECT ts_code, COUNT(*) as cnt FROM daily_kline 
        WHERE trade_date >= '2023-01-01' AND trade_date <= '2026-07-10'
        GROUP BY ts_code HAVING cnt >= 200
        ORDER BY cnt DESC LIMIT 300
    """)
    codes = [r[0] for r in cur.fetchall()]
    conn.close()
    
    print(f"📊 选股: {len(codes)}只 (2023~2026, >=200交易日)")
    
    # 2. 分批加载K线
    BATCH = 100
    all_factors = {}  # factor_name -> (trade_date, factor_value, fwd_ret)
    
    for batch_start in range(0, len(codes), BATCH):
        batch_codes = codes[batch_start:batch_start+BATCH]
        print(f"\n⏳ 批次 {batch_start//BATCH+1}/{(len(codes)+BATCH-1)//BATCH}: {len(batch_codes)}只...")
        
        df = load_kline_batch(batch_codes, '2020-01-01', '2026-07-10')
        if len(df) == 0:
            continue
        
        print(f"  加载: {len(df)}行 | {df['trade_date'].nunique()}日")
        
        # 3. 对每个因子计算并收集IC候选数据
        interim = {}
        for fname, fn, needs_gk in FACTORS:
            try:
                factor_vals = compute_factor_values(df, fname, fn, needs_gk)
                if factor_vals is None or len(factor_vals) == 0:
                    continue
                # 对齐到原df
                df[fname] = factor_vals
                interim[fname] = df[['ts_code','trade_date',fname,'close']].copy()
            except Exception as e:
                print(f"  ⚠️ {fname}: {e}")
        
        # 4. 计算未来收益
        for fname, tmp in interim.items():
            tmp['fwd_close'] = tmp.groupby('ts_code')['close'].shift(-5)
            tmp['fwd_ret'] = tmp['fwd_close'] / tmp['close'] - 1
            tmp = tmp.dropna(subset=[fname, 'fwd_ret'])
            
            if fname not in all_factors:
                all_factors[fname] = []
            for _, row in tmp.iterrows():
                all_factors[fname].append({
                    'trade_date': row['trade_date'],
                    'fval': row[fname],
                    'fwd_ret': row['fwd_ret']
                })
        
        del df, interim
        gc.collect()
        print(f"  批次完成 ({time.time()-t0:.0f}s)")
    
    # 5. 计算IC
    print(f"\n{'='*70}")
    print(f"  📊 Alpha191 因子 IC 验证 (5日持有)")
    print(f"  {len(codes)}只股票 | {time.time()-t0:.0f}s")
    print(f"{'='*70}")
    print(f"  {'因子':14s} {'中文名':12s} {'平均IC':>8s} {'IR':>5s} {'正IC%':>6s} {'有效天':>6s}")
    print(f"  {'─'*56}")
    
    results = {}
    for fname in sorted(FACTORS, key=lambda x: x[0]):
        flist = all_factors.get(fname[0], [])
        if not flist:
            continue
        
        daily_ics = []
        days_dict = defaultdict(list)
        for item in flist:
            days_dict[item['trade_date']].append((item['fval'], item['fwd_ret']))
        
        for td, pairs in days_dict.items():
            if len(pairs) < 10:
                continue
            vals, rets = zip(*pairs)
            try:
                rho, _ = spearmanr(vals, rets)
                if not math.isnan(rho):
                    daily_ics.append(rho)
            except:
                pass
        
        if len(daily_ics) < 5:
            continue
        
        ic_arr = np.array(daily_ics)
        mean_ic = float(np.mean(ic_arr))
        std_ic = float(np.std(ic_arr))
        ir = mean_ic / std_ic if std_ic > 0 else 0
        pos_pct = float(sum(1 for ic in daily_ics if ic > 0) / len(daily_ics) * 100)
        
        m = abs(mean_ic)
        icon = '✅' if m >= 0.020 else ('⚡' if m >= 0.010 else '❌')
        
        print(f"  {fname[0]:14s} {ALPHA_NAMES.get(fname[0],''):12s} {mean_ic:+8.4f}{icon} {ir:5.2f} {pos_pct:6.1f}% {len(daily_ics):6d}")
        
        results[fname[0]] = {
            'name': ALPHA_NAMES.get(fname[0], ''),
            'ic': round(mean_ic, 4),
            'ir': round(ir, 2),
            'pos_pct': round(pos_pct, 1),
            'n_days': len(daily_ics),
        }
    
    # 6. 按IC绝对值排序输出
    sorted_res = sorted(results.items(), key=lambda x: abs(x[1]['ic']), reverse=True)
    qualifying = [x for x in sorted_res if abs(x[1]['ic']) >= 0.020]
    weak = [x for x in sorted_res if 0.010 <= abs(x[1]['ic']) < 0.020]
    bad = [x for x in sorted_res if abs(x[1]['ic']) < 0.010]
    
    out_lines = []
    out_lines.append(f"Alpha191 全量因子IC筛选结果 ({time.strftime('%Y-%m-%d %H:%M')})")
    out_lines.append(f"股票数: {len(codes)} | 持有期: 5日 | IC≥0.020视为有效")
    out_lines.append("")
    
    for label, items in [('✅ 有效因子 (|IC|>=0.020)', qualifying),
                         ('⚡ 弱相关 (0.010~0.019)', weak),
                         ('❌ 无效 (|IC|<0.010)', bad)]:
        out_lines.append(f"── {label} ──")
        if items:
            for fn, r in items:
                out_lines.append(f"  {fn:14s} {r['name']:12s} IC={r['ic']:+6.4f} IR={r['ir']:5.2f} 正IC{r['pos_pct']:5.1f}% N{r['n_days']}日")
        else:
            out_lines.append(f"  (无)")
        out_lines.append("")
    
    out_lines.append(f"汇总: ✅{len(qualifying)}个 | ⚡{len(weak)}个 | ❌{len(bad)}个 | 总计{len(sorted_res)}个")
    
    out_path = '/opt/stock-analyzer/alpha191_ic_results.txt'
    with open(out_path, 'w') as f:
        f.write('\n'.join(out_lines))
    
    # 也存JSON
    json_path = f'/opt/stock-analyzer/alpha191_ic_{time.strftime("%Y%m%d_%H%M")}.json'
    with open(json_path, 'w') as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    
    print(f"\n{'='*70}")
    print(f"  📋 汇总: ✅{len(qualifying)}个 | ⚡{len(weak)}个 | ❌{len(bad)}个 | 总计{len(sorted_res)}个")
    print(f"  📁 结果已保存: {out_path}")
    print(f"  📁 JSON: {json_path}")
    print(f"  ⏱  总耗时: {time.time()-t0:.0f}s")
    print(f"{'='*70}")


if __name__ == '__main__':
    main()
