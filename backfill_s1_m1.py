#!/usr/bin/env python3
"""
S1+M1回测评分批量回填 v2
=====================
S1: 短期Alpha因子评分（5因子截面rank标准化）
M1: 中期P6评分（composite_score）

独立实现，不依赖v14_engine（避免连接冲突）
"""
import sys, time, numpy as np
sys.path.insert(0, '/opt/stock-analyzer')
from db_config import get_connection
import warnings
warnings.filterwarnings("ignore")

H5_FACTORS = ['alpha005', 'alpha034', 'alpha046', 'alpha062', 'alpha089']


def compute_s1_factors(ts_code, trade_date, cur):
    """计算单只股票5个Alpha因子原始值，复用传入cursor"""
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
    n = len(c); i = n - 1; factors = {}
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


def compute_day_s1(trade_date, cur):
    """计算一个交易日所有股票S1评分（独立实现）"""
    cur.execute("""
        SELECT ts_code FROM backtest_pool
        WHERE ts_code IN (SELECT DISTINCT ts_code FROM daily_score_snapshot WHERE trade_date=%s)
    """, (trade_date,))
    codes = [r['ts_code'] for r in cur.fetchall()]
    if not codes:
        cur.execute("SELECT ts_code FROM backtest_pool")
        codes = [r['ts_code'] for r in cur.fetchall()]
    
    raw = {}
    for code in codes:
        f = compute_s1_factors(code, trade_date, cur)
        if f:
            raw[code] = f
    if len(raw) < 30: return {}
    
    # 截面rank
    factor_ranks = {}
    for fname in H5_FACTORS:
        vals = {c: f[fname] for c, f in raw.items() if fname in f}
        if len(vals) < 30: continue
        sorted_codes = sorted(vals.keys(), key=lambda x: vals[x])
        ranks = {c: i/(len(sorted_codes)-1) for i, c in enumerate(sorted_codes)}
        factor_ranks[fname] = ranks
    
    # 等权合成
    h5_raw = {}
    for code in raw:
        composite = 0.0; count = 0
        for fname in H5_FACTORS:
            rk = factor_ranks.get(fname, {}).get(code)
            if rk is not None: composite += rk; count += 1
        if count >= 3: h5_raw[code] = composite / count
    if len(h5_raw) < 20: return {}
    
    # 标准化0~100
    vals_arr = np.array(list(h5_raw.values()))
    v_min, v_max = vals_arr.min(), vals_arr.max()
    if v_max > v_min:
        normalized = (vals_arr - v_min) / (v_max - v_min) * 100
    else:
        normalized = np.full_like(vals_arr, 50)
    
    return dict(zip(h5_raw.keys(), [round(float(n), 1) for n in normalized]))


def main():
    t0 = time.time()
    conn = get_connection(); cur = conn.cursor()
    
    # 交易日
    cur.execute("SELECT DISTINCT trade_date FROM daily_kline WHERE trade_date>='2025-01-01' ORDER BY trade_date")
    all_dates = [str(r['trade_date']) for r in cur.fetchall()]
    print(f"📅 交易日: {len(all_dates)}天 ({all_dates[0]} ~ {all_dates[-1]})")
    
    # 检查已回填
    cur.execute("SELECT MAX(trade_date) as d FROM bt_s1_score")
    r = cur.fetchone()
    last_s1 = str(r['d']) if r and r['d'] else None
    cur.execute("SELECT MAX(trade_date) as d FROM bt_m1_score")
    r = cur.fetchone()
    last_m1 = str(r['d']) if r and r['d'] else None
    print(f"  bt_s1_score最新: {last_s1 or '无'}")
    print(f"  bt_m1_score最新: {last_m1 or '无'}")
    
    total_s1 = total_m1 = 0
    
    for di, td in enumerate(all_dates):
        pct = (di+1)/len(all_dates)*100
        print(f"\n[{di+1}/{len(all_dates)} {pct:.0f}%] 📅 {td}")
        
        # --- S1 ---
        if last_s1 and td <= last_s1:
            print(f"  S1: 已回填，跳过")
        else:
            t1 = time.time()
            s1_map = compute_day_s1(td, cur)
            if s1_map:
                inserted = 0
                for code, s1 in s1_map.items():
                    cur.execute("""
                        INSERT INTO bt_s1_score (ts_code, trade_date, s1_score)
                        VALUES (%s, %s, %s)
                        ON DUPLICATE KEY UPDATE s1_score=VALUES(s1_score)
                    """, (code, td, s1))
                    inserted += 1
                    if inserted % 300 == 0: conn.commit()
                conn.commit()
                total_s1 += inserted
                print(f"  S1: {inserted}只 ({time.time()-t1:.0f}s)")
            else:
                print(f"  S1: 无数据")
        
        # --- M1 ---
        if last_m1 and td <= last_m1:
            print(f"  M1: 已回填，跳过")
        else:
            t2 = time.time()
            cur.execute("""
                SELECT ts_code, composite_score, trend_score, momentum_score, structure_score
                FROM strategy_signal WHERE trade_date=%s AND composite_score IS NOT NULL
            """, (td,))
            rows = cur.fetchall()
            if rows:
                inserted = 0
                for r in rows:
                    cur.execute("""
                        INSERT INTO bt_m1_score (ts_code, trade_date, m1_score, trend_score, momentum_score, structure_score)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE m1_score=VALUES(m1_score),
                            trend_score=VALUES(trend_score), momentum_score=VALUES(momentum_score),
                            structure_score=VALUES(structure_score)
                    """, (r['ts_code'], td,
                          float(r['composite_score'] or 0),
                          float(r['trend_score'] or 0) if r['trend_score'] else None,
                          float(r['momentum_score'] or 0) if r['momentum_score'] else None,
                          float(r['structure_score'] or 0) if r['structure_score'] else None))
                    inserted += 1
                    if inserted % 300 == 0: conn.commit()
                conn.commit()
                total_m1 += inserted
                print(f"  M1: {inserted}只 ({time.time()-t2:.0f}s)")
            else:
                # 从daily_score_snapshot读
                cur.execute("""
                    SELECT ts_code, composite_score FROM daily_score_snapshot 
                    WHERE trade_date=%s AND composite_score IS NOT NULL
                """, (td,))
                rows2 = cur.fetchall()
                if rows2:
                    inserted = 0
                    for r in rows2:
                        cur.execute("""
                            INSERT INTO bt_m1_score (ts_code, trade_date, m1_score)
                            VALUES (%s, %s, %s)
                            ON DUPLICATE KEY UPDATE m1_score=VALUES(m1_score)
                        """, (r['ts_code'], td, float(r['composite_score'] or 0)))
                        inserted += 1
                        if inserted % 300 == 0: conn.commit()
                    conn.commit()
                    total_m1 += inserted
                    print(f"  M1: {inserted}只(从snapshot) ({time.time()-t2:.0f}s)")
                else:
                    print(f"  M1: 无数据")
    
    cur.close(); conn.close()
    print(f"\n{'='*55}")
    print(f"  ✅ 完成")
    print(f"  bt_s1_score: {total_s1}条")
    print(f"  bt_m1_score: {total_m1}条")
    print(f"  耗时: {time.time()-t0:.0f}s")


if __name__ == '__main__':
    main()
