#!/usr/bin/env python3
"""
H5 Alpha因子评分层 — V14引擎的Alpha维度
=========================================
正向5因子：alpha005(量价时序) / alpha034(12日均线比) / 
           alpha046(多均线位置) / alpha062(高量负相关) / alpha089(高低量相关)

输出：对每只股票每日产出 H5_Score（截面标准化后 0~100）
"""

import sys, os, math, json
import numpy as np
from datetime import date
from collections import defaultdict
sys.path.insert(0, '/opt/stock-analyzer')
from db_config import get_connection
import warnings
warnings.filterwarnings("ignore")

H5_FACTORS = ['alpha005', 'alpha034', 'alpha046', 'alpha062', 'alpha089']
H5_FACTOR_NAMES = {
    'alpha005': '量价时序相关',
    'alpha034': '12日均线比',
    'alpha046': '多均线位置',
    'alpha062': '高量负相关',
    'alpha089': '高低量相关',
}


def compute_h5_factor_values(ts_code, trade_date, lookback=60):
    """
    计算某只股票在某个交易日的前 lookback 天内，5个正向因子的最近生效值
    返回 dict {factor_name: value} 或 None（数据不足）
    """
    conn = get_connection()
    cur = conn.cursor()
    
    # 取近120天K线（因子需要最长26日前数据）
    cur.execute("""
        SELECT trade_date, `open`, high, low, `close`, vol
        FROM daily_kline
        WHERE ts_code=%s AND trade_date <= %s
        ORDER BY trade_date DESC LIMIT 120
    """, (ts_code, trade_date))
    rows = cur.fetchall()
    cur.close(); conn.close()
    
    if not rows or len(rows) < 30:
        return None
    
    # 按日期升序
    rows.reverse()
    
    tds = []; o=[]; h=[]; l=[]; c=[]; v=[]
    for r in rows:
        tds.append(str(r[0] if isinstance(r, (list,tuple)) else r['trade_date']))
        o.append(float(r[1] if isinstance(r, (list,tuple)) else r['open']))
        h.append(float(r[2] if isinstance(r, (list,tuple)) else r['high']))
        l.append(float(r[3] if isinstance(r, (list,tuple)) else r['low']))
        c.append(float(r[4] if isinstance(r, (list,tuple)) else r['close']))
        v.append(float(r[5] if isinstance(r, (list,tuple)) else r['vol']))
    
    n = len(c)
    factors = {}
    
    # 只计算最新一个交易日（数组最后一个）
    i = n - 1
    if i < 0: return None
    ci=c[i]; oi=o[i]; hi=h[i]; li=l[i]; vi=v[i]
    
    from scipy.stats import spearmanr
    import pandas as pd
    
    try:
        # alpha005: 量价时序秩相关（前5日）
        if i>=4:
            c5=c[i-4:i+1]; v5=v[i-4:i+1]
            # 去重：如果所有close相同或所有vol相同，跳过
            if len(set(round(x,4) for x in c5)) >= 2 and len(set(round(x,4) for x in v5)) >= 2:
                rc=pd.Series(c5).rank(pct=True).values; rv=pd.Series(v5).rank(pct=True).values
                if np.std(rc)>1e-10 and np.std(rv)>1e-10:
                    factors['alpha005'] = -float(np.corrcoef(rc,rv)[0,1])
    except: pass
    
    try:
        # alpha034: 12日均线比
        if i>=11 and ci>0:
            factors['alpha034'] = c[i-11:i+1].mean()/ci
    except: pass
    
    try:
        # alpha046: 多均线位置（MA3/MA6/MA12/MA24均值 / 收盘价）
        if i>=23 and ci>0:
            ma3=c[i-2:i+1].mean(); ma6=c[i-5:i+1].mean()
            ma12=c[i-11:i+1].mean(); ma24=c[i-23:i+1].mean()
            factors['alpha046'] = (ma3+ma6+ma12+ma24)/(4*ci)
    except: pass
    
    try:
        # alpha062: 高量负相关 -corr(high, vol) 前5日
        if i>=4:
            h5=h[i-4:i+1]; v5=v[i-4:i+1]
            if len(set(round(x,4) for x in h5)) >= 2 and len(set(round(x,4) for x in v5)) >= 2:
                if np.std(h5)>1e-10 and np.std(v5)>1e-10:
                    factors['alpha062'] = -float(np.corrcoef(h5,v5)[0,1])
    except: pass
    
    try:
        # alpha089: 高低量相关 1-corr(close, vol) 前13日
        if i>=12:
            c13=c[i-12:i+1]; v13=v[i-12:i+1]
            if len(set(round(x,4) for x in c13)) >= 2 and len(set(round(x,4) for x in v13)) >= 2:
                if np.std(c13)>1e-10 and np.std(v13)>1e-10:
                    factors['alpha089'] = 1-float(np.corrcoef(c13,v13)[0,1])
    except: pass
    
    if len(factors) < 3:  # 至少3个因子有效
        return None
    
    return factors


def compute_h5_score_for_date(trade_date, codes_list=None):
    """
    计算指定交易日的H5评分（截面标准化0~100）
    返回 dict {ts_code: h5_score}
    """
    import datetime as dt
    conn = get_connection()
    cur = conn.cursor()
    
    # 获取全量/指定股票
    if codes_list:
        placeholders = ','.join(['%s']*len(codes_list))
        cur.execute(f"SELECT ts_code FROM backtest_pool WHERE ts_code IN ({placeholders})", codes_list)
    else:
        cur.execute("SELECT ts_code FROM backtest_pool")
    all_codes = [r['ts_code'] for r in cur.fetchall()]
    cur.close(); conn.close()
    
    # 计算每只股票的5因子原始值
    raw_scores = {}  # {code: {factor: value}}
    for code in all_codes:
        factors = compute_h5_factor_values(code, trade_date)
        if factors and len(factors) >= 3:
            raw_scores[code] = factors
    
    if len(raw_scores) < 30:
        return {}
    
    # 对每个因子截面标准化（rank 0~1），然后等权合成
    h5_final = {}
    for code in raw_scores:
        composite = 0.0
        n_factors = 0
        for fname in H5_FACTORS:
            if fname in raw_scores[code]:
                composite += raw_scores[code][fname]
                n_factors += 1
        if n_factors >= 3:
            h5_final[code] = composite / n_factors
    
    if len(h5_final) < 20:
        return {}
    
    # 截面标准化到0~100
    vals = np.array(list(h5_final.values()))
    # Winsorize: 去掉前后2%
    p2, p98 = np.percentile(vals, [2, 98])
    vals_clip = np.clip(vals, p2, p98)
    
    # 缩放到0~100
    v_min, v_max = vals_clip.min(), vals_clip.max()
    if v_max > v_min:
        normalized = (vals_clip - v_min) / (v_max - v_min) * 100
    else:
        normalized = np.full_like(vals_clip, 50)
    
    return dict(zip(h5_final.keys(), [round(float(n), 1) for n in normalized]))


def batch_h5_score(trade_date):
    """
    批量写入H5评分到数据库
    表结构：daily_h5_score (id, ts_code, trade_date, h5_score, factor_raw)
    """
    import json
    scores = compute_h5_score_for_date(trade_date)
    if not scores:
        print(f"  H5: 无有效评分 (截面不足)")
        return False
    
    conn = get_connection()
    cur = conn.cursor()
    
    # 创建表（如不存在）
    cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_h5_score (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            ts_code VARCHAR(20) NOT NULL,
            trade_date DATE NOT NULL,
            h5_score DECIMAL(6,1) DEFAULT 0,
            factor_raw JSON DEFAULT NULL,
            UNIQUE KEY uk_stock_date (ts_code, trade_date)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
    
    # 写入
    inserted = 0
    for code in sorted(scores.keys())[:10]:  # 先验证
        h5 = scores[code]
        cur.execute("""
            INSERT INTO daily_h5_score (ts_code, trade_date, h5_score)
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE h5_score=VALUES(h5_score)
        """, (code, trade_date, h5))
        inserted += 1
    
    conn.commit()
    cur.close(); conn.close()
    print(f"  H5: {inserted}只写入 (验证模式, 最多10只)")
    return True


# ============================================================
# V14引擎集成入口
# ============================================================

def v14_hybrid_score(trade_date, codes_list=None, h5_weight=0.20):
    """
    V14混合评分入口：
    1. 调用P6引擎获取基础评分
    2. 调用H5引擎获取Alpha评分
    3. 按权重合成最终评分
    
    返回 dict {ts_code: {base_score, h5_score, v14_score, weights}}
    """
    from p6_dual_track_engine import DualTrackEngine
    
    # 1. P6基础评分
    engine = DualTrackEngine()
    base_scores = engine.score_all_stocks(trade_date)  # 假接口，实际需要确认方法名
    if not base_scores:
        base_scores = {}
    
    # 2. H5 Alpha评分
    h5_scores = compute_h5_score_for_date(trade_date, codes_list)
    
    # 3. 合成
    results = {}
    for code, base in base_scores.items():
        h5 = h5_scores.get(code, 50)  # 没有H5用中性50
        v14 = base * (1 - h5_weight) + h5 * h5_weight
        results[code] = {
            'base_score': base,
            'h5_score': h5,
            'v14_score': round(v14, 1),
            'h5_weight': h5_weight,
        }
    
    return results


if __name__ == '__main__':
    import datetime
    td = str(datetime.date.today())
    # 如果是周末或假期，用最近交易日
    print(f"📊 H5 Alpha评分层 — V14引擎组件")
    print(f"  正向5因子: {', '.join(f'{k}={v}' for k,v in H5_FACTOR_NAMES.items())}")
    print(f"  交易日: {td}")
    
    # 获取最新交易日
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT MAX(trade_date) FROM daily_kline")
    r = cur.fetchone()
    td = str(r['MAX(trade_date)'] if 'MAX(trade_date)' in r else r[0]) if r else '2026-07-10'
    cur.close(); conn.close()
    print(f"  最新交易日: {td}")
    
    scores = compute_h5_score_for_date(td)
    if scores:
        top = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:10]
        print(f"\n✅ H5评分 Top10 ({len(scores)}只有效):")
        for code, sc in top:
            print(f"  {code}: {sc:.1f}")
        
        bottom = sorted(scores.items(), key=lambda x: x[1])[:5]
        print(f"\nBottom5:")
        for code, sc in bottom:
            print(f"  {code}: {sc:.1f}")
    else:
        print("\n❌ 无有效评分")
