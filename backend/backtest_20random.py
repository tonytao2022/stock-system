#!/usr/bin/env python3
"""随机20只股票 v4.0 多周期回测"""
import pymysql, sys, os, json, math, random
from db_config import get_connection
from collections import defaultdict, namedtuple

sys.path.insert(0, '.')
from score_engine import ScoreEngineV4

# 读取随机 20 只
with open('/tmp/sample_20.json') as f:
    sample = json.load(f)
codes = sample['codes']
names = sample['names']

e = ScoreEngineV4()
mkt = e.get_market_context()

print("=" * 110)
print(f"🧪 随机20只股票 v4.0 多周期回测")
print(f"   市场: {mkt['season']} {mkt['regime']} breadth={mkt['breadth_ratio']*100:.0f}%")
print(f"   样本: {len(codes)}只, 覆盖 {len(set(e._industry_cache.get(c, '?') for c in codes))} 行业")
print("=" * 110)

# ─── Part 1: 逐日评分轨迹 (5/10-5/25) ───
print(f"\n{'─'*110}")
print("📊 Part 1: 今日评分 + 信号")
print(f"  {'代码':>12s} {'名称':>8s} {'行业':>8s} {'收盘':>7s} {'L1':>4s} {'L2':>4s} {'L3':>4s} {'V分':>5s} {'信号':>12s} {'仓位':>5s} {'5日%':>7s} {'10日%':>7s} {'20日%':>7s}")
print(f"  {'─'*105}")

results = []
for code in codes:
    r = e.score_one(code, mkt)
    if 'error' in r: continue
    results.append(r)
    print(f"  {r['ts_code']:>12s} {r.get('name',names.get(code,''))[:6]:>8s} {r.get('industry','?')[:6]:>8s} "
          f"{r['close']:>7.2f} {r['cycle_score']:>4.0f} {r['chanlun_score']:>4.0f} {r['sentiment_score']:>4.0f} "
          f"{r['v_score']:>5.1f} {r['signal_label']:>12s} {r['position_pct']:>4.0f}% "
          f"{r.get('ret_5d',0):>+6.1f}% {r.get('ret_10d',0):>+6.1f}% {r.get('ret_20d',0):>+6.1f}%")

# ─── Part 2: 多周期回测 (5/10/20日) ───
print(f"\n{'─'*110}")
print("📊 Part 2: 多周期回测 — 原始分层 vs V型映射")
print(f"{'─'*110}")

conn = get_connection()
cur = conn.cursor(pymysql.cursors.DictCursor)

# 为每只股票跑历史回测
all_raw_5 = []; all_raw_10 = []; all_raw_20 = []
all_v_5 = []; all_v_10 = []; all_v_20 = []

for code in codes:
    cur.execute("SELECT trade_date, high, low, close, vol, change_pct FROM daily_kline_qfq WHERE ts_code=%s ORDER BY trade_date ASC", (code,))
    rows = cur.fetchall()
    if len(rows) < 200: continue
    
    closes = [float(r['close']) for r in rows]
    highs = [float(r['high']) for r in rows]
    lows = [float(r['low']) for r in rows]
    vols = [float(r.get('vol',0) or 0) for r in rows]
    
    # 逐日评分
    for i in range(120, len(rows)):
        # 用当时的市场上下文简化
        r = e.score_one(code, mkt)
        if 'error' in r: continue
        raw = r['raw_score']; v = r['v_score']
        
        for fwd in [5,10,20]:
            if i + fwd < len(rows) and closes[i] > 0:
                fwd_ret = (closes[i+fwd] - closes[i]) / closes[i]
                if fwd == 5: 
                    all_raw_5.append({'raw':raw, 'v':v, 'ret':fwd_ret})
                    all_v_5.append({'raw':raw, 'v':v, 'ret':fwd_ret})
                elif fwd == 10:
                    all_raw_10.append({'raw':raw, 'v':v, 'ret':fwd_ret})
                    all_v_10.append({'raw':raw, 'v':v, 'ret':fwd_ret})
                else:
                    all_raw_20.append({'raw':raw, 'v':v, 'ret':fwd_ret})
                    all_v_20.append({'raw':raw, 'v':v, 'ret':fwd_ret})

def calc_spread(data, key='raw', n_groups=5):
    """多空利差"""
    data.sort(key=lambda x: x[key])
    n = len(data)
    if n < n_groups * 10: return 0, 0, 0
    top = data[int(n*0.8):]; bot = data[:int(n*0.2)]
    return (sum(r['ret'] for r in top)/len(top) - sum(r['ret'] for r in bot)/len(bot)) * 100, \
           len(data), \
           sum(r['ret'] for r in data)/n*100

for label, data_sets, key in [
    ('原始评分 5日', [(all_raw_5,'raw')], 'raw'),
    ('原始评分 10日', [(all_raw_10,'raw')], 'raw'), 
    ('原始评分 20日', [(all_raw_20,'raw')], 'raw'),
    ('V型映射 5日', [(all_v_5,'v')], 'v'),
    ('V型映射 10日', [(all_v_10,'v')], 'v'),
    ('V型映射 20日', [(all_v_20,'v')], 'v'),
]:
    for data, k in data_sets:
        spread, n, avg = calc_spread(data, k)
        if n > 0:
            print(f"  {label:<20s} n={n:>5d} 利差={spread:>+6.2f}% 均值={avg:>+5.2f}%")

# ─── Part 3: 信号分布 ───
print(f"\n{'─'*110}")
print("📊 Part 3: 信号分布")
buy = sum(1 for r in results if r['signal'] in ('STRONG_BUY','BUY'))
cautious = sum(1 for r in results if r['signal'] == 'CAUTIOUS_BUY')
hold = sum(1 for r in results if r['signal'] == 'HOLD')
sell = sum(1 for r in results if r['signal'] == 'SELL')
print(f"  🟢买入:{buy} | 🟡谨慎:{cautious} | ⏸️持有:{hold} | 🔴卖出:{sell}")

# 信号分组收益
buy_ret = sum(r.get('ret_10d',0) for r in results if r['signal'] in ('STRONG_BUY','BUY'))
sell_ret = sum(r.get('ret_10d',0) for r in results if r['signal'] == 'SELL')
if buy > 0: print(f"  买入组 10日均收益: {buy_ret/buy:+.2f}%")
if sell > 0: print(f"  卖出组 10日均收益: {sell_ret/sell:+.2f}%")

cur.close(); conn.close()
e.close()
print(f"\n{'='*110}")
print("✅ 回测完成")
