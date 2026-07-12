#!/usr/bin/env python3
"""
V14评分初始化：建表 + 计算最新交易日H5 + 写入daily_v14_score + 替换emotion_score
"""
import sys, os, time
import numpy as np
sys.path.insert(0, '/opt/stock-analyzer')
from db_config import get_connection
from v14_engine import compute_h5_scores
import warnings
warnings.filterwarnings("ignore")

t0 = time.time()
print(f"🔄 V14引擎初始化")

# 建表
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
conn.commit()
cur.close(); conn.close()
print("✅ daily_v14_score 表已创建")

# 最新交易日
conn = get_connection(); cur = conn.cursor()
cur.execute("SELECT MAX(trade_date) FROM daily_score_snapshot")
td = str(list(cur.fetchone().values())[0])
cur.close(); conn.close()
print(f"📅 交易日: {td}")

# 计算H5
print(f"⏳ 计算H5评分...", end=' ', flush=True)
t1 = time.time()
h5 = compute_h5_scores(td)
print(f"{len(h5)}只 ({time.time()-t1:.0f}s)")

# 读取P6
conn = get_connection(); cur = conn.cursor()
cur.execute("SELECT ts_code, composite_score FROM daily_score_snapshot WHERE trade_date=%s AND composite_score IS NOT NULL", (td,))
p6 = {r['ts_code']: float(r['composite_score']) for r in cur.fetchall()}
cur.close(); conn.close()
print(f"P6评分: {len(p6)}只")

if not h5 or not p6:
    print("❌ 数据不足")
    sys.exit(1)

# P6标准化
p6_vals = np.array(list(p6.values()))
p5, p95 = np.percentile(p6_vals, [5, 95])
p6_min, p6_max = p5, p95
h5_weight = 0.20

p6_norm = {}
for c, v in p6.items():
    if v >= p95: p6_norm[c] = 100.0
    elif v <= p5: p6_norm[c] = 0.0
    elif p6_max > p6_min:
        p6_norm[c] = (v - p5) / (p6_max - p6_min) * 100
    else: p6_norm[c] = 50.0

# 写入daily_v14_score
conn = get_connection(); cur = conn.cursor()
inserted = 0
for code in p6_norm:
    h = h5.get(code, 50)
    p = p6_norm[code]
    v14 = p * (1-h5_weight) + h * h5_weight
    cur.execute("""
        INSERT INTO daily_v14_score (ts_code, trade_date, v14_score, p6_score, h5_score, h5_weight)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE v14_score=VALUES(v14_score), p6_score=VALUES(p6_score), h5_score=VALUES(h5_score)
    """, (code, td, round(v14,1), round(p,1), h, h5_weight))
    inserted += 1
conn.commit()
print(f"📝 已写入 daily_v14_score: {inserted}条")

# 替换emotion_score
print(f"⏳ 替换emotion_score...")
cur.execute("SELECT ts_code FROM strategy_signal WHERE trade_date=%s", (td,))
signal_codes = [r['ts_code'] for r in cur.fetchall()]
print(f"  strategy_signal中该日股票: {len(signal_codes)}只")

updated = 0
for code in signal_codes:
    h5_val = h5.get(code)
    if h5_val is not None:
        cur.execute("UPDATE strategy_signal SET emotion_score=%s WHERE ts_code=%s AND trade_date=%s",
                     (h5_val, code, td))
        updated += 1

conn.commit()
print(f"  ✅ 更新emotion_score→H5: {updated}只")

# 验证
cur.execute("""
    SELECT COUNT(*) as cnt, ROUND(AVG(emotion_score),2) as avg_h5,
           ROUND(MIN(emotion_score),2) as min_h5, ROUND(MAX(emotion_score),2) as max_h5
    FROM strategy_signal WHERE trade_date=%s AND emotion_score > 0
""", (td,))
r = cur.fetchone()
print(f"  验证: {r['cnt']}条 | 均值={r['avg_h5']} 最小={r['min_h5']} 最大={r['max_h5']}")

# 抽查
cur.execute("SELECT ts_code, emotion_score FROM strategy_signal WHERE trade_date=%s AND emotion_score > 70 LIMIT 5", (td,))
print(f"  高分样本:")
for s in cur.fetchall():
    print(f"    {s['ts_code']}: {s['emotion_score']}")

cur.close(); conn.close()
print(f"\n{'='*50}")
print(f"  ✅ V14集成完成 | 总耗时: {time.time()-t0:.0f}s")
print(f"  ℹ️ emotion_score 现在存储 H5评分（替代L3情绪因子）")
