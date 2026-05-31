#!/usr/bin/env python3
"""回测1: 修复前后评分对比"""
import sys, os, pymysql

sys.path.insert(0, '/root/.openclaw/workspace/projects/陶的投资预测模型项目/代码实现')
pwd = os.environ.get('MYSQL_PWD', '')

from score_engine import ScoreEngineV4
from db_config import get_connection
e = ScoreEngineV4()
mkt = e.get_market_context()
print(f'季节: {mkt["season"]} 置信度: {mkt["confidence"]:.0%}')
e.score_pool(save_db=True)

conn = get_connection()
cur = conn.cursor()

# 评分分布
cur.execute("SELECT ROUND(composite_score/10,0)*10 as bucket, COUNT(*) as cnt FROM trend_score WHERE trade_date='2026-05-28' GROUP BY bucket ORDER BY bucket")
print('\n评分分布(2026-05-28):')
for r in cur.fetchall():
    bar = '█' * (r[1]//5)
    print(f'  {int(r[0]):3d}-{int(r[0]+9):3d}: {r[1]:3d} {bar}')

# 信号分布
cur.execute("SELECT direction, COUNT(*) as cnt FROM strategy_signal WHERE trade_date='2026-05-28' GROUP BY direction")
print('\n信号分布(2026-05-28):')
for r in cur.fetchall():
    emoji = {'LONG':'🟢','SHORT':'🔴','NEUTRAL':'⏸️'}.get(r[0],'❓')
    print(f'  {emoji} {r[0]}: {r[1]}只')

# 信号标签分布
cur.execute("SELECT signal_label, COUNT(*) as cnt FROM strategy_signal WHERE trade_date='2026-05-28' AND direction='LONG' GROUP BY signal_label ORDER BY cnt DESC")
print('\n买入信号标签分布:')
for r in cur.fetchall():
    print(f'  {r[0]}: {r[1]}只')

# Top10
cur.execute("SELECT ts_code, composite_score FROM trend_score WHERE trade_date='2026-05-28' ORDER BY composite_score DESC LIMIT 10")
print('\nTop10(2026-05-28):')
for r in cur.fetchall():
    print(f'  {r[0]} 评分={r[1]}')

conn.close()
print('\n✅ 回测1完成')
