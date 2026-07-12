#!/usr/bin/env python3
"""
V14 引擎集成 — 用H5 Alpha分替换L3情绪因子
===========================================
执行顺序：在每日收盘管道（daily_pipeline）后运行
1. 计算全量股票的H5评分（或从daily_v14_score读缓存）
2. 写入 strategy_signal 表的 emotion_score 字段（替换原L3情绪因子）
3. 同时更新 h5_score 字段（如果存在）

这样不会修改P6引擎，L3情绪因子被H5取代后，前端和策略读取 emotion_score 时拿到的是H5评分
"""
import sys, os, time
import numpy as np
sys.path.insert(0, '/opt/stock-analyzer')
from db_config import get_connection
import warnings
warnings.filterwarnings("ignore")


def replace_emotion_with_h5(trade_date, dry_run=False):
    """
    用H5评分替换strategy_signal表中的emotion_score
    H5评分优先从daily_v14_score读取缓存，没有则现场计算
    """
    t0 = time.time()
    conn = get_connection(); cur = conn.cursor()
    
    # 1. 尝试从 daily_v14_score 读取缓存
    cur.execute("""
        SELECT ts_code, h5_score FROM daily_v14_score 
        WHERE trade_date=%s AND h5_score IS NOT NULL
    """, (trade_date,))
    rows = cur.fetchall()
    
    if rows:
        h5_map = {r['ts_code']: float(r['h5_score']) for r in rows}
        print(f"  📥 从 daily_v14_score 读取H5缓存: {len(h5_map)}条")
    else:
        # 2. 现场计算
        print(f"  ⚙️ 现场计算H5评分...", end=' ', flush=True)
        from v14_engine import compute_h5_scores
        h5_map = compute_h5_scores(trade_date)
        print(f"{len(h5_map)}只")
    
    if not h5_map:
        print(f"  ❌ 无H5评分可用")
        cur.close(); conn.close()
        return 0
    
    # 3. 获取strategy_signal中该交易日有评分的股票
    cur.execute("""
        SELECT ts_code, emotion_score FROM strategy_signal 
        WHERE trade_date=%s
    """, (trade_date,))
    signal_rows = {r['ts_code']: float(r['emotion_score'] or 0) for r in cur.fetchall()}
    print(f"  📋 strategy_signal中该日股票: {len(signal_rows)}只")
    
    # 4. 替换：将emotion_score更新为H5评分
    updated = 0
    batch_vals = []
    for code in signal_rows:
        h5 = h5_map.get(code)
        if h5 is not None:
            batch_vals.append((h5, code, trade_date))
            updated += 1
    
    if not dry_run and batch_vals:
        # 批量更新
        cur.executemany("""
            UPDATE strategy_signal 
            SET emotion_score = %s 
            WHERE ts_code=%s AND trade_date=%s
        """, batch_vals)
        conn.commit()
    
    print(f"  {'📝' if not dry_run else '🔍'} 更新emotion_score→H5: {updated}只")
    
    # 5. 统计替换效果
    if not dry_run:
        cur.execute("""
            SELECT 
                COUNT(*) as total,
                ROUND(AVG(emotion_score), 2) as avg_h5,
                ROUND(MIN(emotion_score), 2) as min_h5,
                ROUND(MAX(emotion_score), 2) as max_h5,
                ROUND(STD(emotion_score), 2) as std_h5
            FROM strategy_signal WHERE trade_date=%s
        """, (trade_date,))
        r = cur.fetchone()
        if r:
            print(f"  📊 分布: 均值={r['avg_h5']} 最小={r['min_h5']} 最大={r['max_h5']} 标准差={r['std_h5']}")
        
        # 校验：读取几个样本
        cur.execute("""
            SELECT ts_code, emotion_score FROM strategy_signal 
            WHERE trade_date=%s AND emotion_score > 0
            LIMIT 10
        """, (trade_date,))
        samples = cur.fetchall()
        if samples:
            print(f"  ✅ 样本验证:")
            for s in samples:
                print(f"    {s['ts_code']}: emotion_score={s['emotion_score']}")
    
    cur.close(); conn.close()
    print(f"  ⏱ 耗时: {time.time()-t0:.1f}s")
    return updated


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--trade-date', help='交易日，默认最新')
    parser.add_argument('--dry-run', action='store_true', help='仅预览不写入')
    args = parser.parse_args()
    
    conn = get_connection(); cur = conn.cursor()
    if args.trade_date:
        td = args.trade_date
    else:
        cur.execute("SELECT MAX(trade_date) FROM strategy_signal")
        r = cur.fetchone()
        td = str(r['MAX(trade_date)'])
        if not td: 
            cur.execute("SELECT MAX(trade_date) FROM daily_kline")
            r = cur.fetchone()
            td = str(r['MAX(trade_date)'])
    cur.close(); conn.close()
    
    print(f"🔄 V14引擎集成: 用H5替换emotion_score")
    print(f"  📅 交易日: {td}")
    if args.dry_run:
        print(f"  🔍 dry_run=True (预览)")
    print()
    
    n = replace_emotion_with_h5(td, dry_run=args.dry_run)
    print(f"\n{'='*50}")
    print(f"  ✅ 完成: 更新{n}只")
