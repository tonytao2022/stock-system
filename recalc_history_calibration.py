#!/usr/bin/env python3
"""
重算 strategy_signal 历史 calibrated_score
只改校准分，不碰 composite_score 和子因子

P1-1置信度动态校准：
  ≥0.7 → scale=1.0
  0.5-0.7 → scale=0.875
  0.3-0.5 → scale=0.625
  <0.3 → scale=0.50

用法: python3 recalc_history_calibration.py [start_date] [end_date]
默认: 2024-09-02 ~ 最新
"""
import sys, os, time, pymysql
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_config import _get_db_config

_cfg = _get_db_config()
conn = pymysql.connect(host=_cfg['host'], port=_cfg['port'], user=_cfg['user'],
                       password=_cfg['password'],
                       database=_cfg['database'], charset='utf8mb4',
                       cursorclass=pymysql.cursors.DictCursor)
cur = conn.cursor()

def confidence_scale(conf: float) -> float:
    if conf >= 0.70: return 1.0
    if conf >= 0.50: return 0.875
    if conf >= 0.30: return 0.625
    return 0.50

def calibrate_score(composite: float, all_composites: list, scale: float) -> float:
    if not all_composites:
        return max(0, min(100, composite))
    ss = sorted(all_composites)
    n = len(ss)
    targets = {
        5: int(10*scale), 10: int(15*scale), 15: int(18*scale), 20: int(20*scale),
        25: int(22*scale), 30: int(24*scale), 35: int(26*scale), 40: int(28*scale),
        45: int(29*scale), 50: int(30*scale), 55: int(32*scale), 60: int(34*scale),
        65: int(36*scale), 70: int(38*scale), 75: int(40*scale), 80: int(44*scale),
        85: int(48*scale), 90: int(50*scale), 93: int(55*scale), 95: int(60*scale),
        97: int(68*scale), 99: int(75*scale), 100: int(80*scale)
    }
    cm = {}
    for pct, t in targets.items():
        cm[ss[min(int(n * pct / 100), n - 1)]] = t
    cm[ss[0]] = max(0, targets[5] - 5)
    cm[ss[-1]] = targets[100]

    sr = sorted(cm.keys())
    if composite <= sr[0]: return float(cm[sr[0]])
    if composite >= sr[-1]: return float(cm[sr[-1]])
    for i in range(len(sr) - 1):
        lo, hi = sr[i], sr[i + 1]
        if lo <= composite <= hi:
            if hi == lo: return float(cm[lo])
            return round(cm[lo] + (composite - lo) / (hi - lo) * (cm[hi] - cm[lo]), 1)
    return round(composite, 1)


def main(start_date='2024-09-02', end_date=None):
    t0 = time.time()
    
    # 1. 获取end_date（默认为最新交易日）
    if not end_date:
        cur.execute("SELECT MAX(trade_date) as max_d FROM strategy_signal")
        r = cur.fetchone()
        end_date = str(r['max_d'])
    
    print(f"🔄 重算校准分: {start_date} ~ {end_date}")
    
    # 2. 加载季节+置信度
    cur.execute(
        "SELECT trade_date, season, confidence FROM season_state "
        "WHERE index_code='MARKET' AND trade_date>=%s AND trade_date<=%s ORDER BY trade_date",
        (start_date, end_date)
    )
    seasons = {}
    for r in cur.fetchall():
        seasons[str(r['trade_date'])] = {
            'season': r['season'],
            'confidence': float(r['confidence'] or 0.5)
        }
    print(f"  ✓ 季节: {len(seasons)}天")
    
    # 3. 逐日处理
    cur.execute(
        "SELECT DISTINCT trade_date FROM strategy_signal "
        "WHERE trade_date>=%s AND trade_date<=%s AND composite_score IS NOT NULL ORDER BY trade_date",
        (start_date, end_date)
    )
    trading_days = [str(r['trade_date']) for r in cur.fetchall()]
    print(f"  ✓ 交易日: {len(trading_days)}天")
    
    total_rows = 0
    updated_rows = 0
    skipped_days = 0
    
    for idx, td in enumerate(trading_days):
        if td not in seasons:
            skipped_days += 1
            continue
        
        sd = seasons[td]
        confidence = sd['confidence']
        scale = confidence_scale(confidence)
        
        # 读取当日所有composite_score
        cur.execute(
            "SELECT id, composite_score FROM strategy_signal "
            "WHERE trade_date=%s AND composite_score IS NOT NULL",
            (td,)
        )
        rows = cur.fetchall()
        if not rows:
            continue
        
        composites = [float(r['composite_score']) for r in rows]
        
        # 计算新的校准分
        updates = []
        for r in rows:
            new_cal = calibrate_score(float(r['composite_score']), composites, scale)
            updates.append((new_cal, r['id']))
        
        # 批量更新
        if updates:
            batch_size = 200
            for i in range(0, len(updates), batch_size):
                batch = updates[i:i+batch_size]
                case_stmt = " ".join([f"WHEN {uid} THEN {cal:.1f}" for cal, uid in batch])
                ids = [uid for _, uid in batch]
                sql = f"UPDATE strategy_signal SET calibrated_score = CASE id {case_stmt} END WHERE id IN ({','.join(map(str, ids))})"
                cur.execute(sql)
            conn.commit()
            updated_rows += len(updates)
            total_rows += len(updates)
        
        if (idx + 1) % 50 == 0:
            elapsed = int(time.time() - t0)
            print(f"  📅 {td} ({idx+1}/{len(trading_days)}) | 更新{updated_rows}行 | {elapsed}s")
            updated_rows = 0
    
    elapsed = int(time.time() - t0)
    print(f"\n{'='*40}")
    print(f"✅ 完成!")
    print(f"  周期: {start_date} ~ {end_date}")
    print(f"  总更新: {total_rows}行")
    print(f"  跳过: {skipped_days}天（无季节数据）")
    print(f"  耗时: {elapsed}s")
    
    # 验证采样
    print(f"\n📊 验证采样:")
    for td in ['2025-01-15', '2026-01-15', '2026-06-30']:
        if td in seasons:
            sd = seasons[td]
            scale = confidence_scale(sd['confidence'])
            cur.execute(
                "SELECT ROUND(AVG(composite_score),2) avg_comp, ROUND(MAX(composite_score),2) max_comp, "
                "ROUND(AVG(calibrated_score),2) avg_cal, ROUND(MAX(calibrated_score),2) max_cal, COUNT(*) cnt "
                "FROM strategy_signal WHERE trade_date=%s",
                (td,)
            )
            r = cur.fetchone()
            print(f"  {td} conf={sd['confidence']:.2f} scale={scale:.3f}: "
                  f"comp({r['avg_comp']}~{r['max_comp']}) → "
                  f"cal({r['avg_cal']}~{r['max_cal']}) [{r['cnt']}只]")
    
    conn.close()

if __name__ == '__main__':
    args = sys.argv[1:]
    if len(args) >= 2:
        main(args[0], args[1])
    elif len(args) >= 1:
        main(args[0])
    else:
        main()
