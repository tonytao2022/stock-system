#!/usr/bin/env python3
"""
V13 vs V14 评分对比 — 全截面分析
=================================
对每个有评分的交易日：
1. 从DB读取P6评分（composite_score）
2. 现场计算H5评分
3. 合成V14评分
4. 对比V13和V14的排名变化
"""
import sys, time
import numpy as np
from collections import defaultdict
sys.path.insert(0, '/opt/stock-analyzer')
from db_config import get_connection
import warnings
warnings.filterwarnings("ignore")

H5_FACTORS = ['alpha005', 'alpha034', 'alpha046', 'alpha062', 'alpha089']

def get_conn():
    return get_connection()


def compute_h5_factors(ts_code, trade_date, cur):
    """计算单只股票在指定交易日的5个Alpha因子"""
    cur.execute("""
        SELECT trade_date, `open`, high, low, `close`, vol
        FROM daily_kline WHERE ts_code=%s AND trade_date <= %s
        ORDER BY trade_date DESC LIMIT 120
    """, (ts_code, trade_date))
    rows = cur.fetchall()
    if not rows or len(rows) < 30: return None
    
    rows.reverse()
    o=[]; h=[]; l=[]; c=[]; v=[]
    for r in rows:
        o.append(float(r['open'])); h.append(float(r['high']))
        l.append(float(r['low'])); c.append(float(r['close'])); v.append(float(r['vol']))
    
    n = len(c); i = n - 1
    factors = {}
    
    try:
        if i>=4:
            c5=c[i-4:i+1]; v5=v[i-4:i+1]
            if len(set(round(x,4) for x in c5))>=2 and len(set(round(x,4) for x in v5))>=2:
                rc=np.argsort(c5)/len(c5); rv=np.argsort(v5)/len(v5)
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


def compare_day(trade_date, cur, h5_weight=0.20):
    """比较一个交易日的V13 vs V14评分"""
    cur.execute("SELECT ts_code, composite_score FROM daily_score_snapshot WHERE trade_date=%s AND composite_score IS NOT NULL", (trade_date,))
    score_rows = cur.fetchall()
    if len(score_rows) < 30: return None
    
    p6_scores = {r['ts_code']: float(r['composite_score']) for r in score_rows}
    
    # 计算所有股票的H5原始因子值
    h5_raw = {}
    for code in p6_scores:
        f = compute_h5_factors(code, trade_date, cur)
        if f and len(f) >= 3:
            h5_raw[code] = f
    
    if len(h5_raw) < 30: return None
    
    # 对每个因子做截面rank标准化
    factor_ranks = {f: {} for f in H5_FACTORS}
    for code, factors in h5_raw.items():
        for fname in H5_FACTORS:
            if fname in factors:
                factor_ranks[fname][code] = factors[fname]
    
    for fname in H5_FACTORS:
        vals = factor_ranks[fname]
        if len(vals) < 30: continue
        sorted_codes = sorted(vals.keys(), key=lambda x: vals[x])
        rank = {c: i/(len(sorted_codes)-1)*100 for i, c in enumerate(sorted_codes)}
        factor_ranks[fname] = rank
    
    # 等权合成H5评分（0~100）
    h5_scores = {}
    for code in h5_raw:
        composite = 0.0; count = 0
        for fname in H5_FACTORS:
            rank = factor_ranks.get(fname, {}).get(code)
            if rank is not None:
                composite += rank; count += 1
        if count >= 3:
            h5_scores[code] = composite / count
    
    # 对P6做截面标准化到0~100
    val_arr = np.array(list(p6_scores.values()))
    p5, p95 = np.percentile(val_arr, [5, 95])
    v_min = max(val_arr.min(), p5)
    v_max = min(val_arr.max(), p95)
    if v_max > v_min:
        p6_norm = {c: (max(min(v, p95), p5)-v_min)/(v_max-v_min)*100 
                  for c, v in p6_scores.items()}
    else:
        p6_norm = {c: 50.0 for c in p6_scores}
    
    # V14混合
    v14_scores = {}
    for code in p6_norm:
        if code in h5_scores:
            v14 = p6_norm[code] * (1 - h5_weight) + h5_scores[code] * h5_weight
            v14_scores[code] = {
                'p6': round(p6_norm[code], 1),
                'h5': round(h5_scores[code], 1),
                'v14': round(v14, 1),
            }
    
    return v14_scores


def main():
    t0 = time.time()
    conn = get_conn(); cur = conn.cursor()
    
    # 所有有评分的交易日
    cur.execute("SELECT DISTINCT trade_date FROM daily_score_snapshot WHERE trade_date>='2026-06-16' ORDER BY trade_date")
    all_dates = [r['trade_date'].strftime('%Y-%m-%d') if hasattr(r['trade_date'],'strftime') else str(r['trade_date']) for r in cur.fetchall()]
    
    print(f"📊 V13 vs V14 全截面分析 ({len(all_dates)}个交易日)")
    
    all_stats = []
    for idx, td in enumerate(all_dates):
        print(f"\n{'─'*55}")
        print(f"  [{idx+1}/{len(all_dates)}] 📅 {td}")
        print(f"{'─'*55}")
        
        result = compare_day(td, cur)
        if not result:
            print("  无有效评分")
            continue
        
        codes = list(result.keys())
        v14_vals = [r['v14'] for r in result.values()]
        p6_vals = [r['p6'] for r in result.values()]
        h5_vals = [r['h5'] for r in result.values()]
        
        # V14 Top 10
        sorted_v14 = sorted(codes, key=lambda x: result[x]['v14'], reverse=True)
        print(f"\n  V14 Top 10 (评分>=65可买入):")
        print(f"  {'代码':12s} {'V14':>6s} {'P6':>6s} {'H5':>6s} {'排名变化':>8s}")
        for code in sorted_v14[:15]:
            r = result[code]
            # P6排名
            sorted_p6 = sorted(codes, key=lambda x: result[x]['p6'], reverse=True)
            p6_rank = sorted_p6.index(code) if code in sorted_p6 else 999
            v14_rank = sorted_v14.index(code)
            delta = p6_rank - v14_rank  # 正数=V14排名更高
            print(f"  {code:12s} {r['v14']:6.1f} {r['p6']:6.1f} {r['h5']:6.1f} {delta:+5d}")
        
        # 可使用买入的（评分>=65）
        buyable_v13 = sum(1 for r in result.values() if r['p6'] >= 65)
        buyable_v14 = sum(1 for r in result.values() if r['v14'] >= 65)
        
        # H5 vs P6相关性
        from scipy.stats import spearmanr
        rho, _ = spearmanr(p6_vals, h5_vals)
        
        # 排名变化分布
        sorted_p6_full = sorted(codes, key=lambda x: result[x]['p6'], reverse=True)
        p6_ranks = {c: i for i, c in enumerate(sorted_p6_full)}
        sorted_v14_full = sorted(codes, key=lambda x: result[x]['v14'], reverse=True)
        v14_ranks = {c: i for i, c in enumerate(sorted_v14_full)}
        
        rank_deltas = [p6_ranks[c] - v14_ranks[c] for c in codes]
        avg_delta = np.mean(rank_deltas)
        pct_up = sum(1 for d in rank_deltas if d > 10)/len(rank_deltas)*100  # 排名上升>10位
        pct_down = sum(1 for d in rank_deltas if d < -10)/len(rank_deltas)*100
        
        print(f"\n  统计:")
        print(f"  V14均值={np.mean(v14_vals):.1f} | P6均值={np.mean(p6_vals):.1f} | H5均值={np.mean(h5_vals):.1f}")
        print(f"  H5~P6秩相关: {rho:.3f}")
        print(f"  可买入(V13>=65): {buyable_v13}只 | 可买入(V14>=65): {buyable_v14}只")
        print(f"  排名↑>10: {pct_up:.0f}% | 排名↓>10: {pct_down:.0f}%")
        
        # 入选变化
        v13_top50 = set(sorted_p6_full[:50])
        v14_top50 = set(sorted_v14_full[:50])
        overlap = len(v13_top50 & v14_top50)
        print(f"  Top50重合率: {overlap/50*100:.0f}%")
        
        all_stats.append({
            'date': td, 'n_stocks': len(codes),
            'v14_mean': round(np.mean(v14_vals), 1),
            'p6_mean': round(np.mean(p6_vals), 1),
            'h5_mean': round(np.mean(h5_vals), 1),
            'h5_p6_corr': round(rho, 3),
            'buyable_v13': buyable_v13, 'buyable_v14': buyable_v14,
            'top50_overlap': round(overlap/50*100, 0),
            'rank_up_pct': round(pct_up, 1),
            'avg_rank_delta': round(float(np.mean(rank_deltas)), 1),
        })
        
        if idx >= 6:
            print("\n  ⏸️ 已看7天，截断")
            break
    
    cur.close(); conn.close()
    
    # 汇总
    print(f"\n\n{'='*55}")
    print(f"  📋 V13 vs V14 全截面汇总")
    print(f"{'='*55}")
    print(f"  {'日期':12s} {'股票数':>6s} {'V14均':>6s} {'P6均':>6s} {'H5均':>6s} {'H5~P6相关':>8s} {'买入V13':>6s} {'V14':>5s} {'Top50重叠':>8s}")
    print(f"  {'─'*60}")
    for s in all_stats:
        print(f"  {s['date']:12s} {s['n_stocks']:6d} {s['v14_mean']:6.1f} {s['p6_mean']:6.1f} {s['h5_mean']:6.1f} {s['h5_p6_corr']:8.3f} {s['buyable_v13']:5d} {s['buyable_v14']:>5d} {s['top50_overlap']:>7.0f}%")
    
    # 平均
    if all_stats:
        avg = {k: np.mean([s[k] for s in all_stats]) for k in ['h5_p6_corr','top50_overlap','rank_up_pct']}
        print(f"\n  平均 H5~P6秩相关: {avg['h5_p6_corr']:.3f}")
        print(f"  平均 Top50重合率: {avg['top50_overlap']:.0f}%")
        print(f"  平均 排名↑>10: {avg['rank_up_pct']:.1f}%")
    
    print(f"\n  总耗时: {time.time()-t0:.0f}s")


if __name__ == '__main__':
    main()
