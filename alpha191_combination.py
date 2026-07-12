#!/usr/bin/env python3
"""
Alpha191 因子归因组合分析
1. 聚类分析（按IC方向+稳定性做因子聚类）
2. 正交性分析（判断哪些因子提供独立信息）
3. 最优因子组合筛选（用现有M1引擎weight结构做替换方案）
"""
import json, math, time, gc, warnings, os, subprocess
import numpy as np
import pandas as pd
import pymysql
from scipy.stats import spearmanr, pearsonr
from collections import defaultdict
warnings.filterwarnings('ignore')

PWD = subprocess.run(['grep','password','/etc/mysql/debian.cnf'], capture_output=True, text=True).stdout
PWD = [l.split('=')[-1].strip() for l in PWD.strip().split('\n') if 'password' in l][0]

# 加载全量因子IC结果
with open('/opt/stock-analyzer/alpha191_ic_20260713_0131.json') as f:
    full_ic = json.load(f)

# 加载季节IC结果
with open('/opt/stock-analyzer/alpha191_season_ic_20260713_0134.json') as f:
    season_ic = json.load(f)

print(f"📥 加载: 全量IC {len(full_ic)}个因子, 季节IC {len(season_ic)}个因子", flush=True)

# ===== 1. 因子归因分类 =====
# 把191个因子按逻辑分成6个引擎维度：
# T-trend(趋势), M-momentum(动量), V-volatility(波动),
# Q-volume(量能), S-structure(结构/超跌), F-fundamental(资金流-暂无)

factor_categories = {
    'T_trend': ['alpha034','alpha036','alpha038','alpha047','alpha057','alpha058','alpha066','alpha067',
                'alpha068','alpha069','alpha070','alpha071','alpha085','alpha094','alpha095','alpha096',
                'alpha097','alpha131','alpha132','alpha133','alpha134','alpha135','alpha159','alpha160',
                'alpha161','alpha162','alpha163','alpha164','alpha177','alpha178','alpha179','alpha180',
                'alpha181','alpha185','alpha186','alpha187','alpha188','alpha189','alpha190','alpha191'],
    'M_momentum': ['alpha005','alpha006','alpha008','alpha009','alpha010','alpha014','alpha015','alpha020',
                   'alpha021','alpha026','alpha031','alpha032','alpha046','alpha053','alpha064','alpha065',
                   'alpha075','alpha076','alpha077','alpha098','alpha099','alpha100','alpha136','alpha137',
                   'alpha138','alpha139','alpha140'],
    'V_volatility': ['alpha002','alpha013','alpha028','alpha048','alpha051','alpha054','alpha084','alpha090',
                     'alpha113','alpha114','alpha115','alpha144','alpha145','alpha146','alpha182','alpha183',
                     'alpha184'],
    'Q_volume': ['alpha001','alpha003','alpha007','alpha023','alpha024','alpha027','alpha029','alpha030',
                 'alpha033','alpha035','alpha037','alpha039','alpha040','alpha050','alpha052','alpha059',
                 'alpha060','alpha061','alpha062','alpha063','alpha072','alpha073','alpha078','alpha079',
                 'alpha080','alpha081','alpha082','alpha083','alpha086','alpha087','alpha088','alpha089',
                 'alpha091','alpha092','alpha093','alpha101','alpha102','alpha103','alpha104','alpha105',
                 'alpha106','alpha107','alpha108','alpha109','alpha110','alpha111','alpha112','alpha116',
                 'alpha117','alpha118','alpha119','alpha120','alpha121','alpha122','alpha123','alpha124',
                 'alpha125','alpha147','alpha148','alpha149','alpha150','alpha151','alpha152','alpha153',
                 'alpha154','alpha155','alpha156','alpha157','alpha158','alpha165','alpha166','alpha167',
                 'alpha168','alpha169','alpha170','alpha171','alpha172','alpha173','alpha174'],
    'S_structure': ['alpha011','alpha012','alpha016','alpha017','alpha018','alpha019','alpha022','alpha025',
                    'alpha041','alpha042','alpha043','alpha044','alpha045','alpha049','alpha055','alpha056',
                    'alpha128','alpha129','alpha130','alpha175','alpha176'],
    'F_fundamental': []  # 暂无，留空
}

# 统计各维度IC表现
print(f"\n{'='*80}")
print(f"  📊 因子归因: 按维度分类IC表现")
print(f"{'='*80}")
print(f"  {'维度':16s} {'数量':>4s} {'|IC|平均':>8s} {'IR平均':>7s} {'有效(≥0.02)':>12s} {'最佳因子':>12s}")
print(f"  {'─'*65}")

all_best = []
for cat_name, factors in factor_categories.items():
    valid = [(f, full_ic[f]) for f in factors if f in full_ic and abs(full_ic[f]['ic']) >= 0.020]
    if not valid: continue
    all_ics = [full_ic[f]['ic'] for f in factors if f in full_ic]
    all_irs = [full_ic[f]['ir'] for f in factors if f in full_ic]
    
    avg_abs_ic = np.mean([abs(x) for x in all_ics])
    avg_ir = np.mean(all_irs)
    best_f = max(valid, key=lambda x: abs(x[1]['ic']))
    
    label = cat_name.replace('_', ' ').title()
    print(f"  {label:16s} {len(factors):4d} {avg_abs_ic:8.4f} {avg_ir:7.3f} {len(valid):4d}个      {best_f[0]:>8s}({best_f[1]['ic']:+5.3f})")

# ===== 2. 正交性分析：计算因子之间的相关性 =====
print(f"\n[01:38] 开始加载数据做因子相关性分析...", flush=True)
conn = pymysql.connect(host='localhost', user='debian-sys-maint', 
                       password=PWD, database='stock_db_v2', charset='utf8mb4')
df = pd.read_sql("""
    SELECT b.ts_code, a.trade_date, a.`open`, a.high, a.low, a.`close`, a.vol
    FROM daily_kline a
    JOIN (SELECT ts_code FROM daily_kline WHERE trade_date>='2023-01-01' 
          GROUP BY ts_code HAVING COUNT(*)>=400 ORDER BY COUNT(*) DESC LIMIT 100) b
    ON a.ts_code=b.ts_code
    WHERE a.trade_date>='2024-01-01'
    ORDER BY a.ts_code, a.trade_date
""", conn)
conn.close()

close = df.pivot_table(index='trade_date', columns='ts_code', values='close').values.astype(np.float64)
high = df.pivot_table(index='trade_date', columns='ts_code', values='high').values.astype(np.float64)
low = df.pivot_table(index='trade_date', columns='ts_code', values='low').values.astype(np.float64)
open_ = df.pivot_table(index='trade_date', columns='ts_code', values='open').values.astype(np.float64)
vol = df.pivot_table(index='trade_date', columns='ts_code', values='vol').values.astype(np.float64)
n, m = close.shape
print(f"  矩阵: {n}日 × {m}只", flush=True)

del df; gc.collect()

# 只选取每个维度IC最强的因子
best_per_category = {}
for cat_name, factors in factor_categories.items():
    valid = [(f, full_ic[f]) for f in factors if f in full_ic and abs(full_ic[f]['ic']) >= 0.020 and abs(full_ic[f]['ir']) >= 0.10]
    if valid:
        best = sorted(valid, key=lambda x: abs(x[1]['ic']))[:4]
        best_per_category[cat_name] = [f[0] for f in best]
        print(f"  {cat_name}: {' '.join(f[0] for f in best)}", flush=True)

# 快速计算因子矩阵（只计算上述候选因子）
# 内联因子计算
from alpha191_season import compute_all_factors as calc_factors

factors = calc_factors(close, high, low, open_, vol)

# 筛选候选因子
candidate_factors = {}
for cat, fns in best_per_category.items():
    for fn in fns:
        if fn in factors:
            candidate_factors[fn] = factors[fn]

candidate_names = list(candidate_factors.keys())
print(f"  候选因子: {len(candidate_names)}个", flush=True)

# 计算因子间截面相关性（选取最后100个交易日）
n_latest = min(100, n)
corr_matrix = np.full((len(candidate_names), len(candidate_names)), np.nan)

for i in range(len(candidate_names)):
    for j in range(i, len(candidate_names)):
        # 用最新100天的均值截面rank
        ci = candidate_factors[candidate_names[i]][-n_latest:, :]
        cj = candidate_factors[candidate_names[j]][-n_latest:, :]
        
        # flatten取所有值做corr
        fi = ci.flatten(); fj = cj.flatten()
        mask = ~np.isnan(fi) & ~np.isnan(fj) & ~np.isinf(fi) & ~np.isinf(fj)
        if np.sum(mask) < 100:
            corr = 0
        else:
            corr, _ = spearmanr(fi[mask], fj[mask])
            corr = float(corr) if not math.isnan(corr) else 0
        corr_matrix[i][j] = corr
        corr_matrix[j][i] = corr

print(f"\n{'='*80}")
print(f"  📊 候选因子正交性矩阵（Spearman秩相关）")
print(f"{'='*80}")
print(f"  {'因子':>12s}", end='')
for c in candidate_names:
    print(f" {c:>10s}", end='')
print()
for i, ci in enumerate(candidate_names):
    print(f"  {ci:>12s}", end='')
    for j in range(len(candidate_names)):
        v = corr_matrix[i][j]
        p = '+' if not math.isnan(v) and v > 0 else ''
        print(f" {p}{v:+.3f}  ", end='')
    print()

# 找低相关（正交）且高IC的组合
print(f"\n{'='*80}")
print(f"  📊 最优正交因子组合（低相关+高IC）")
print(f"{'='*80}")
print(f"  {'因子A':12s} {'因子B':12s} {'|IC(A)|':>7s} {'|IC(B)|':>7s} {'|Corr|':>6s} {'正交得分':>8s}")
print(f"  {'─'*55}")

combos = []
for i in range(len(candidate_names)):
    for j in range(i+1, len(candidate_names)):
        ci = candidate_names[i]; cj = candidate_names[j]
        ic1 = abs(full_ic.get(ci, {}).get('ic', 0))
        ic2 = abs(full_ic.get(cj, {}).get('ic', 0))
        corr_val = abs(corr_matrix[i][j]) if not math.isnan(corr_matrix[i][j]) else 1
        if corr_val > 0.7: continue
        
        # 正交得分 = 组合IC / 相关性惩罚
        ortho_score = (ic1 + ic2) / (corr_val + 0.1)
        combos.append((ci, cj, ic1, ic2, corr_val, ortho_score))

combos.sort(key=lambda x: -x[5])
for ci, cj, ic1, ic2, cv, score in combos[:20]:
    print(f"  {ci:12s} {cj:12s} {ic1:7.4f} {ic2:7.4f} {cv:6.3f} {score:8.2f}")

# ===== 3. 现有引擎因子替换分析 =====
print(f"\n{'='*80}")
print(f"  📊 现有M1引擎因子替换建议")
print(f"{'='*80}")

# M1当前使用因子: trend(38%), struct(10%), moment(19%), mf(19%), α062(14%)
# 对应alpha191因子: 
# trend→α034/α036/α094等
# struct→α130/α017/α056等
# moment→α010/α006/α046等
# mf→无对应（资金流独立）
# α062→已用

m1_factor_map = {
    'T_trend': {'current': 'α034+α036+α094等', 'alpha191_candidates': ['alpha094','alpha036','alpha034','alpha066','alpha085']},
    'S_structure': {'current': '内部结构评分', 'alpha191_candidates': ['alpha130','alpha017','alpha056','alpha128']},
    'M_momentum': {'current': '内部动量评分', 'alpha191_candidates': ['alpha006','alpha010','alpha046']},
    'Q_volume': {'current': '部分融入trend', 'alpha191_candidates': ['alpha062','alpha089','alpha052','alpha027','alpha101']},
}

for dim, info in m1_factor_map.items():
    print(f"\n  ▎{dim}:")
    print(f"     当前: {info['current']}")
    print(f"     Alpha191候选:", end='')
    for fn in info['alpha191_candidates']:
        ic = full_ic.get(fn, {})
        sic = season_ic.get(fn, {})
        if ic:
            label = '✅' if abs(ic.get('ic',0))>=0.03 else '⚡'
            print(f"\n        {fn}({label} IC={ic.get('ic',0):+.4f} IR={ic.get('ir',0):.2f})", end='')
    print()

# 最终推荐
print(f"\n{'='*80}")
print(f"  🏆 最终推荐：最优因子补充组合")
print(f"{'='*80}")

# 基于IC+IR+正交性综合评分
scored = []
for fn, ic_data in full_ic.items():
    # 基础分 = |IC| + IR*0.02
    ic_val = abs(ic_data.get('ic', 0))
    ir_val = ic_data.get('ir', 0)
    pos_pct = ic_data.get('pos_pct', 50)
    
    # 方向一致性加分
    season_data = season_ic.get(fn, {})
    season_signs = []
    for s in ['summer','chaos_spring','chaos','chaos_autumn','autumn']:
        if s in season_data:
            season_signs.append(season_data[s]['ic'])
    all_same = False
    if len(season_signs) >= 3:
        all_same = all(v>0 for v in season_signs) or all(v<0 for v in season_signs)
    
    # 打分
    score = ic_val * 100 + ir_val * 20 + (pos_pct/50 - 1) * 10
    if all_same and ic_val >= 0.02:
        score += 5
    
    scored.append((fn, round(ic_val,4), round(ir_val,3), round(pos_pct,1), all_same, round(score,2)))

scored.sort(key=lambda x: -x[5])

print(f"\n  {'排名':>4s} {'因子':14s} {'|IC|':>7s} {'IR':>6s} {'正IC%':>7s} {'跨季同向':>8s} {'综合分':>7s}")
print(f"  {'─'*55}")
for i, (fn, ic, ir, pp, same, sc) in enumerate(scored[:30]):
    sm = '✅' if same else ' '
    print(f"  {i+1:4d} {fn:14s} {ic:7.4f} {ir:6.2f} {pp:6.1f}% {sm:>8s} {sc:7.2f}")

# 保存推荐结果
rec = {'rankings': [{'factor':f, 'abs_ic':ic, 'ir':ir, 'pos_pct':pp, 'stable_season':sm, 'total_score':sc} 
                     for f,ic,ir,pp,sm,sc in scored[:50]]}
with open('/opt/stock-analyzer/alpha191_combination_recommendation.json', 'w') as f:
    json.dump(rec, f, indent=2, ensure_ascii=False)
print(f"\n💾 推荐结果已保存到 alpha191_combination_recommendation.json")
print(f"⏱ {time.time()-t0:.0f}s")
