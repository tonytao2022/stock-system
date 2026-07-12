#!/usr/bin/env python3
"""
V14 混合引擎 — P6双轨评分 + H5 Alpha因子
===========================================
集成方式：V14评分 = P6评分 × (1 - h5_weight) + H5评分 × h5_weight
h5_weight 默认为 0.20（回测确定）

每日收盘管道入口：v14_score_full_pipeline(trade_date)
"""

import sys, os, math, json, time
import numpy as np
from datetime import date
from collections import defaultdict
sys.path.insert(0, '/opt/stock-analyzer')
from db_config import get_connection
import warnings
warnings.filterwarnings("ignore")

# ============================================================
# H5因子计算层
# ============================================================

H5_FACTORS = ['alpha005', 'alpha034', 'alpha046', 'alpha062', 'alpha089']

def compute_h5_factor_values(ts_code, trade_date):
    """计算单只股票在指定交易日的5个Alpha因子原始值"""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT trade_date, `open`, high, low, `close`, vol
        FROM daily_kline WHERE ts_code=%s AND trade_date <= %s
        ORDER BY trade_date DESC LIMIT 120
    """, (ts_code, trade_date))
    rows = cur.fetchall()
    cur.close(); conn.close()
    if not rows or len(rows) < 30: return None
    
    rows.reverse()
    tds = []; o=[]; h=[]; l=[]; c=[]; v=[]
    for r in rows:
        tds.append(str(r['trade_date'])); o.append(float(r['open']))
        h.append(float(r['high'])); l.append(float(r['low']))
        c.append(float(r['close'])); v.append(float(r['vol']))
    
    n = len(c); i = n - 1
    factors = {}
    from scipy.stats import spearmanr
    import pandas as pd
    
    try:
        if i>=4:
            c5=c[i-4:i+1]; v5=v[i-4:i+1]
            if len(set(round(x,4) for x in c5))>=2 and len(set(round(x,4) for x in v5))>=2:
                rc=pd.Series(c5).rank(pct=True).values; rv=pd.Series(v5).rank(pct=True).values
                if np.std(rc)>1e-10 and np.std(rv)>1e-10:
                    factors['alpha005'] = -float(np.corrcoef(rc,rv)[0,1])
    except: pass
    
    try:
        if i>=11 and c[i]>0:
            factors['alpha034'] = c[i-11:i+1].mean()/c[i]
    except: pass
    
    try:
        if i>=23 and c[i]>0:
            ma3=c[i-2:i+1].mean(); ma6=c[i-5:i+1].mean()
            ma12=c[i-11:i+1].mean(); ma24=c[i-23:i+1].mean()
            factors['alpha046'] = (ma3+ma6+ma12+ma24)/(4*c[i])
    except: pass
    
    try:
        if i>=4:
            h5_=h[i-4:i+1]; v5_=v[i-4:i+1]
            if len(set(round(x,4) for x in h5_))>=2 and len(set(round(x,4) for x in v5_))>=2:
                if np.std(h5_)>1e-10 and np.std(v5_)>1e-10:
                    factors['alpha062'] = -float(np.corrcoef(h5_,v5_)[0,1])
    except: pass
    
    try:
        if i>=12:
            c13=c[i-12:i+1]; v13=v[i-12:i+1]
            if len(set(round(x,4) for x in c13))>=2 and len(set(round(x,4) for x in v13))>=2:
                if np.std(c13)>1e-10 and np.std(v13)>1e-10:
                    factors['alpha089'] = 1-float(np.corrcoef(c13,v13)[0,1])
    except: pass
    
    return factors if len(factors) >= 3 else None


def compute_h5_scores(trade_date, codes_list=None):
    """
    截面H5评分（0~100）
    先用每只股票的因子值做截面rank（0~1），再等权合成后标准化
    这样比直接对原始值标准化更平滑
    """
    conn = get_connection()
    cur = conn.cursor()
    if codes_list:
        placeholders = ','.join(['%s']*len(codes_list))
        cur.execute(f"SELECT ts_code FROM backtest_pool WHERE ts_code IN ({placeholders})", codes_list)
    else:
        cur.execute("SELECT ts_code FROM backtest_pool")
    all_codes = [r['ts_code'] for r in cur.fetchall()]
    cur.close(); conn.close()
    
    raw_scores = {}
    for code in all_codes:
        factors = compute_h5_factor_values(code, trade_date)
        if factors:
            raw_scores[code] = factors
    
    if len(raw_scores) < 30: return {}
    
    # 对每个因子做截面rank（用百分比排名，0~1）
    factor_ranks = {f: {} for f in H5_FACTORS}
    for code, factors in raw_scores.items():
        for fname in H5_FACTORS:
            if fname in factors:
                factor_ranks[fname][code] = factors[fname]
    
    # 计算rank
    for fname in H5_FACTORS:
        vals = factor_ranks[fname]
        if len(vals) < 30:
            factor_ranks[fname] = {}
            continue
        sorted_codes = sorted(vals.keys(), key=lambda x: vals[x])
        rank = {c: i/(len(sorted_codes)-1) for i, c in enumerate(sorted_codes)}
        factor_ranks[fname] = rank
    
    # 等权合成
    h5_raw = {}
    for code in raw_scores:
        composite = 0.0; count = 0
        for fname in H5_FACTORS:
            rank = factor_ranks.get(fname, {}).get(code, None)
            if rank is not None:
                composite += rank; count += 1
        if count >= 3:
            h5_raw[code] = composite / count
    
    if len(h5_raw) < 20: return {}
    
    # 缩放到0~100
    vals = np.array(list(h5_raw.values()))
    v_min, v_max = vals.min(), vals.max()
    if v_max > v_min:
        normalized = (vals - v_min) / (v_max - v_min) * 100
    else:
        normalized = np.full_like(vals, 50)
    
    return dict(zip(h5_raw.keys(), [round(float(n), 1) for n in normalized]))


# ============================================================
# V14混合评分入口
# ============================================================

def v14_score_stock(trade_date, h5_weight=0.20):
    """
    V14混合评分：P6基础分 + H5 Alpha分
    返回 dict {code: {p6: float, h5: float, v14: float}}
    """
    from p6_dual_track_engine import score_stock, MarketContext, batch_score
    from season_engine import SeasonEngine
    
    print(f"⏳ [1/3] P6基础评分...", end=' ', flush=True)
    t1 = time.time()
    
    p6_scores = {}
    try:
        # 获取季节上下文
        se = SeasonEngine()
        judge = se.judge_season(trade_date)
        ctx = MarketContext(judge)
        
        # 获取完整股票池
        conn = get_connection(); cur = conn.cursor()
        cur.execute("SELECT ts_code FROM backtest_pool")
        codes = [r['ts_code'] for r in cur.fetchall()]
        cur.close(); conn.close()
        
        # 批量评分
        results_list = batch_score(codes, ctx)
        p6_scores = {r.get('ts_code', r.get('code', '')): r.get('final_score', r.get('score', 50)) for r in results_list if r}
        print(f"ok {len(p6_scores)}只 ({time.time()-t1:.0f}s)")
    except Exception as e:
        print(f"P6引擎异常: {e}")
        # fallback: 从数据库读取最近一天的评分
        print(f"⏳ 直接读取数据库评分...", end=' ', flush=True)
        try:
            conn = get_connection(); cur = conn.cursor()
            cur.execute("""
                SELECT ts_code, composite_score FROM daily_score_snapshot
                WHERE trade_date=(SELECT MAX(trade_date) FROM daily_score_snapshot)
            """)
            rows = cur.fetchall()
            cur.close(); conn.close()
            p6_scores = {r['ts_code']: float(r['composite_score']) for r in rows if r['composite_score']}
        except Exception as e2:
            print(f"  DB fallback也失败: {e2}")
            p6_scores = {}
        print(f"{len(p6_scores)}只 ({time.time()-t1:.0f}s)")
    
    print(f"⏳ [2/3] H5 Alpha评分...", end=' ', flush=True)
    t2 = time.time()
    h5_scores = compute_h5_scores(trade_date, list(p6_scores.keys()) if p6_scores else None)
    print(f"{len(h5_scores)}只 ({time.time()-t2:.0f}s)")
    
    print(f"⏳ [3/3] V14混合合成...", end=' ', flush=True)
    t3 = time.time()
    
    # 对P6评分也做截面标准化到0~100
    if p6_scores:
        p6_vals = np.array(list(p6_scores.values()))
        # 去掉极端值
        p5, p95 = np.percentile(p6_vals, [5, 95])
        p6_clip = np.clip(p6_vals, p5, p95)
        p6_min, p6_max = p6_clip.min(), p6_clip.max()
        if p6_max > p6_min:
            p6_normalized = {c: (max(min(v, p95), p5)-p6_min)/(p6_max-p6_min)*100 
                            for c, v in p6_scores.items()}
        else:
            p6_normalized = {c: 50.0 for c in p6_scores}
    else:
        p6_normalized = {}
    
    results = {}
    for code in p6_normalized:
        h5 = h5_scores.get(code, 50)
        p6 = p6_normalized[code]
        v14 = p6 * (1 - h5_weight) + h5 * h5_weight
        results[code] = {
            'p6': round(p6, 1),
            'h5': h5,
            'v14': round(v14, 1),
        }
    
    print(f"ok {len(results)}只 ({time.time()-t3:.0f}s)")
    return results


def v14_full_pipeline(trade_date, h5_weight=0.20, dry_run=True):
    """
    完整V14管道：评分 → 买入推荐 → 写入数据库
    如果dry_run=True，只输出不写入
    """
    t0 = time.time()
    print(f"\n{'='*60}")
    print(f"  V14 引擎全量运行 | {trade_date} | h5_weight={h5_weight:.0%}")
    print(f"{'='*60}")
    
    results = v14_score_stock(trade_date, h5_weight)
    if not results:
        print("❌ 无评分结果")
        return
    
    # Top/Bottom
    sorted_codes = sorted(results.items(), key=lambda x: x[1]['v14'], reverse=True)
    
    print(f"\n📊 V14评分 Top 20:")
    print(f"  {'代码':12s} {'V14':>6s} {'P6':>6s} {'H5':>6s}")
    for code, r in sorted_codes[:20]:
        print(f"  {code:12s} {r['v14']:6.1f} {r['p6']:6.1f} {r['h5']:6.1f}")
    
    print(f"\n  Bottom 10:")
    for code, r in sorted_codes[-10:]:
        print(f"  {code:12s} {r['v14']:6.1f} {r['p6']:6.1f} {r['h5']:6.1f}")
    
    # 统计分布
    v14_vals = [r['v14'] for r in results.values()]
    p6_vals = [r['p6'] for r in results.values()]
    h5_vals = [r['h5'] for r in results.values()]
    
    print(f"\n📈 分布统计:")
    print(f"  {'':6s} {'均值':>8s} {'中位':>8s} {'std':>8s} {'p25':>8s} {'p75':>8s}")
    print(f"  V14: {np.mean(v14_vals):8.1f} {np.median(v14_vals):8.1f} {np.std(v14_vals):8.1f} {np.percentile(v14_vals,25):8.1f} {np.percentile(v14_vals,75):8.1f}")
    print(f"  P6:  {np.mean(p6_vals):8.1f} {np.median(p6_vals):8.1f} {np.std(p6_vals):8.1f} {np.percentile(p6_vals,25):8.1f} {np.percentile(p6_vals,75):8.1f}")
    print(f"  H5:  {np.mean(h5_vals):8.1f} {np.median(h5_vals):8.1f} {np.std(h5_vals):8.1f} {np.percentile(h5_vals,25):8.1f} {np.percentile(h5_vals,75):8.1f}")
    
    # 差异分析：V14 vs P6
    diff = np.array(v14_vals) - np.array(p6_vals)
    up_pct = sum(1 for d in diff if d > 2)/len(diff)*100  # V14>P6超过2分的比例
    dn_pct = sum(1 for d in diff if d < -2)/len(diff)*100
    print(f"\n  V14 > P6 (+2分以上): {up_pct:.0f}%")
    print(f"  V14 < P6 (-2分以上): {dn_pct:.0f}%")
    
    if not dry_run:
        # 写入数据库
        conn = get_connection(); cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS daily_v14_score (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                ts_code VARCHAR(20) NOT NULL,
                trade_date DATE NOT NULL,
                v14_score DECIMAL(6,1),
                p6_score DECIMAL(6,1),
                h5_score DECIMAL(6,1),
                h5_weight DECIMAL(4,2),
                UNIQUE KEY uk_stock_date (ts_code, trade_date)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        
        inserted = 0
        for code, r in results.items():
            cur.execute("""
                INSERT INTO daily_v14_score (ts_code, trade_date, v14_score, p6_score, h5_score, h5_weight)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE v14_score=VALUES(v14_score),
                    p6_score=VALUES(p6_score), h5_score=VALUES(h5_score)
            """, (code, trade_date, r['v14'], r['p6'], r['h5'], h5_weight))
            inserted += 1
        conn.commit(); cur.close(); conn.close()
        print(f"\n✅ 已写入 daily_v14_score: {inserted}条")
    
    print(f"\n{'─'*60}")
    print(f"  总耗时: {time.time()-t0:.0f}s")
    return results


if __name__ == '__main__':
    # 获取最新交易日
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT MAX(trade_date) FROM daily_kline")
    r = cur.fetchone()
    td = str(r[0] if isinstance(r, (list,tuple)) else (r['MAX(trade_date)'] if 'MAX' in str(r.keys()) else list(r.values())[0]))
    cur.close(); conn.close()
    
    v14_full_pipeline(td, h5_weight=0.20, dry_run=True)
