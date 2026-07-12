#!/usr/bin/env python3
"""
V14 H5权重扫描 + 季节相关性分析
=================================
H5_weight 从 0% ~ 40% 步长5%，对比每个权重的排名变化
并分析H5评分与季节的相关性
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


def get_season(trade_date, cur):
    cur.execute("SELECT season FROM season_state WHERE index_code='MARKET' AND trade_date=%s", (trade_date,))
    r = cur.fetchone()
    return r['season'] if r else 'chaos'


def run_weight_scan(cur, trade_date):
    """对一个交易日运行权重扫描"""
    # 读取P6评分
    cur.execute("SELECT ts_code, composite_score FROM daily_score_snapshot WHERE trade_date=%s AND composite_score IS NOT NULL", (trade_date,))
    rows = cur.fetchall()
    p6 = {r['ts_code']: float(r['composite_score']) for r in rows}
    if len(p6) < 50: return None, None
    
    # P6 rank标准化
    arr = np.array(list(p6.values()))
    p5, p95 = np.percentile(arr, [5, 95])
    p6_norm = {}
    for c, v in p6.items():
        if v >= p95: p6_norm[c] = 100.0
        elif v <= p5: p6_norm[c] = 0.0
        elif p95 > p5:
            p6_norm[c] = (v - p5) / (p95 - p5) * 100
        else: p6_norm[c] = 50.0
    
    # 计算H5因子
    h5_raw = {}
    for code in p6:
        f = compute_h5_factors(code, trade_date, cur)
        if f and len(f) >= 3:
            h5_raw[code] = f
    if len(h5_raw) < 50: return None, None
    
    # 截面rank标准化
    factor_ranks = {}
    for fname in H5_FACTORS:
        vals = {c: f[fname] for c, f in h5_raw.items() if fname in f}
        if len(vals) < 30: continue
        sorted_codes = sorted(vals.keys(), key=lambda x: vals[x])
        ranks = {c: i/(len(sorted_codes)-1)*100 for i, c in enumerate(sorted_codes)}
        factor_ranks[fname] = ranks
    
    # 合成H5
    h5_scores = {}
    for code in h5_raw:
        composite = 0.0; count = 0
        for fname in H5_FACTORS:
            rk = factor_ranks.get(fname, {}).get(code)
            if rk is not None: composite += rk; count += 1
        if count >= 3: h5_scores[code] = composite / count
    if len(h5_scores) < 50: return None, None
    
    # 排序
    p6_ranked = sorted(p6_norm.keys(), key=lambda x: -p6_norm[x])
    h5_ranked = sorted(h5_scores.keys(), key=lambda x: -h5_scores[x])
    p6_ranks = {c: i for i, c in enumerate(p6_ranked)}
    h5_ranks = {c: i for i, c in enumerate(h5_ranked)}
    
    # 对每个权重，计算指标
    weights = [0.0, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40]
    results = {}
    for w in weights:
        codes = set(p6_norm.keys()) & set(h5_scores.keys())
        v14 = {c: p6_norm[c]*(1-w) + h5_scores[c]*w for c in codes}
        v14_ranked = sorted(v14.keys(), key=lambda x: -v14[x])
        v14_ranks = {c: i for i, c in enumerate(v14_ranked)}
        
        # Top50重合率
        p6_top50 = set(p6_ranked[:50]) & codes
        v14_top50 = set(v14_ranked[:50])
        overlap = len(p6_top50 & v14_top50)
        
        # rank变化大于10的比例
        deltas = [p6_ranks[c] - v14_ranks[c] for c in codes]
        pct_up = sum(1 for d in deltas if d > 10) / len(deltas) * 100
        pct_down = sum(1 for d in deltas if d < -10) / len(deltas) * 100
        
        buy_v13 = sum(1 for c in codes if p6_norm[c] >= 65)
        buy_v14 = sum(1 for c in codes if v14[c] >= 65)
        
        # 斯皮尔曼相关
        from scipy.stats import spearmanr
        rho, _ = spearmanr([p6_norm[c] for c in codes], [v14[c] for c in codes])
        
        results[w] = {
            'top50_overlap': round(overlap/50*100, 1),
            'rank_up_pct': round(pct_up, 1),
            'rank_down_pct': round(pct_down, 1),
            'buy_v13': buy_v13, 'buy_v14': buy_v14,
            'h5_v14_corr': round(rho, 3),
        }
    
    # 季节分析
    season = get_season(trade_date, cur)
    
    # H5与P6的对比：Top100中的H5分离度
    p6_top100 = p6_ranked[:100]
    h5_in_p6_top100 = [c for c in p6_top100 if c in h5_scores]
    h5_in_p6_top100_mean = np.mean([h5_scores[c] for c in h5_in_p6_top100])
    h5_all_mean = np.mean(list(h5_scores.values()))
    
    return results, {
        'date': trade_date, 'season': season, 'n_stocks': len(p6),
        'h5_p6_top100_diff': round(h5_in_p6_top100_mean - h5_all_mean, 1),
        'h5_p6_rank_corr': round(np.corrcoef(
            [p6_ranks.get(c,0) for c in h5_scores],
            [h5_ranks.get(c,0) for c in h5_scores]
        )[0,1], 3) if len(h5_scores) > 10 else 0,
    }


def main():
    t0 = time.time()
    conn = get_conn(); cur = conn.cursor()
    
    cur.execute("SELECT DISTINCT trade_date FROM daily_score_snapshot WHERE trade_date>='2026-06-16' ORDER BY trade_date")
    all_dates = [r['trade_date'].strftime('%Y-%m-%d') if hasattr(r['trade_date'],'strftime') else str(r['trade_date']) for r in cur.fetchall()]
    
    print(f"📊 V14 H5权重扫描 + 季节分析 ({len(all_dates)}个交易日)\n")
    
    # 累计全部结果
    all_season_stats = defaultdict(list)
    all_weight_stats = {w: [] for w in [0.0, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40]}
    
    for idx, td in enumerate(all_dates):
        results, meta = run_weight_scan(cur, td)
        if not results:
            continue
        
        season = meta['season']
        all_season_stats[season].append(meta)
        for w, r in results.items():
            all_weight_stats[w].append(r)
        
        if idx < 5 or (idx % 5 == 0):
            print(f"  [{idx+1}/{len(all_dates)}] {td} ({meta['n_stocks']}只, {meta['season']})")
    
    cur.close(); conn.close()
    
    # === 权重扫描汇总 ===
    print(f"\n{'='*60}")
    print(f"  📋 权重扫描汇总 ({len(all_weight_stats[0.0])}个交易日)")
    print(f"{'='*60}")
    header = f"  {'权重':>6s} {'Top50重合':>10s} {'排名↑>10':>10s} {'排名↓>10':>10s} {'买入V13':>8s} {'买入V14':>8s} {'H5~V14相关':>10s}"
    print(header)
    print(f"  {'─'*58}")
    for w in [0.0, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40]:
        data = all_weight_stats[w]
        if not data: continue
        overlap = np.mean([d['top50_overlap'] for d in data])
        up = np.mean([d['rank_up_pct'] for d in data])
        down = np.mean([d['rank_down_pct'] for d in data])
        b13 = int(np.mean([d['buy_v13'] for d in data]))
        b14 = int(np.mean([d['buy_v14'] for d in data]))
        corr = np.mean([d['h5_v14_corr'] for d in data])
        print(f"  {w*100:>5.0f}%  {overlap:>9.1f}%  {up:>8.1f}%  {down:>9.1f}%  {b13:>7}  {b14:>7}  {corr:>9.3f}")
    
    # === 推荐权重 ===
    print(f"\n  推荐权重分析:")
    best_balance = None
    for w in [0.05, 0.10, 0.15, 0.20, 0.25, 0.30]:
        data = all_weight_stats[w]
        if not data: continue
        overlap = np.mean([d['top50_overlap'] for d in data])
        up = np.mean([d['rank_up_pct'] for d in data])
        # 目标：Top50重合>80%，同时排名变动>15%
        if overlap >= 80 and up >= 15:
            score = overlap + up
            if not best_balance or score > best_balance[2]:
                best_balance = (w, overlap, up)
    if best_balance:
        w, overlap, up = best_balance
        print(f"  平衡方案: weight={w*100:.0f}% (Top50重合{overlap:.1f}%, 排名变动{up:.1f}%)")
    
    # === 季节分析 ===
    print(f"\n{'='*60}")
    print(f"  📋 季节维度H5分析")
    print(f"{'='*60}")
    print(f"  {'季节':12s} {'天数':>5s} {'股票均':>6s} {'H5~P6相关':>10s} {'H5_P6_Top100差':>14s}")
    print(f"  {'─'*50}")
    for season in sorted(all_season_stats.keys()):
        data = all_season_stats[season]
        days = len(data)
        avg_n = int(np.mean([d['n_stocks'] for d in data]))
        avg_corr = np.mean([d['h5_p6_rank_corr'] for d in data])
        avg_diff = np.mean([d['h5_p6_top100_diff'] for d in data])
        print(f"  {season:12s} {days:>5} {avg_n:>6} {avg_corr:>10.3f} {avg_diff:>13.1f}")
    
    # === 每日细节（前3天） ===
    print(f"\n{'='*60}")
    print(f"  📋 每日权重扫描明细（前3天）")
    print(f"{'='*60}")
    conn2 = get_conn(); cur2 = conn2.cursor()
    for td in all_dates[:3]:
        results, meta = run_weight_scan(cur2, td)
        if not results: continue
        print(f"\n  📅 {td} ({meta['season']})")
        header = f"  {'权重':>6s} {'Top50重合':>10s} {'排名↑>10':>10s} {'排名↓>10':>10s} {'买入V13':>8s} {'买入V14':>8s}"
        print(header)
        for w in [0.0, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30]:
            r = results[w]
            print(f"  {w*100:>5.0f}%  {r['top50_overlap']:>9.1f}%  {r['rank_up_pct']:>8.1f}%  {r['rank_down_pct']:>9.1f}%  {r['buy_v13']:>7}  {r['buy_v14']:>7}")
    cur2.close(); conn2.close()
    
    print(f"\n  总耗时: {time.time()-t0:.0f}s")


if __name__ == '__main__':
    main()
