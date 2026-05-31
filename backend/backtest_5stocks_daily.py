#!/usr/bin/env python3
"""5只股票逐日评分回测 — 2026-05-10 至 2026-05-25"""
import pymysql, sys
from db_config import get_connection
sys.path.insert(0, '.')
from score_engine import score_trend, score_momentum, score_volatility, score_volume, vmap

conn = get_connection()
cur = conn.cursor(pymysql.cursors.DictCursor)

stocks = {
    '002050.SZ': '三花智控',
    '002185.SZ': '华天科技',
    '300476.SZ': '胜宏科技',
    '688036.SH': '传音控股',
    '301226.SZ': '鼎泰高科',
}

# 5月10日开始的交易日
target_dates = ['2026-05-11','2026-05-12','2026-05-13','2026-05-14','2026-05-15',
                '2026-05-18','2026-05-19','2026-05-20','2026-05-21','2026-05-22',
                '2026-05-25']

print("=" * 125)
print("🧪 5只股票 逐日评分回测 — 2026-05-10 至 2026-05-25")
print("=" * 125)

for code, name in stocks.items():
    cur.execute("SELECT trade_date, high, low, close, vol, change_pct FROM stock_db.daily_kline WHERE ts_code=%s ORDER BY trade_date ASC", (code,))
    rows = cur.fetchall()
    
    if len(rows) < 200:
        print(f"\n  ❌ {code} {name}: 数据不足 ({len(rows)}日)")
        continue
    
    closes = [float(r['close']) for r in rows]
    highs = [float(r['high']) for r in rows]
    lows = [float(r['low']) for r in rows]
    vols = [float(r.get('vol',0) or 0) for r in rows]
    dates = [str(r['trade_date']) for r in rows]
    
    # 找到 >= 5月10日的起始位置
    start_idx = None
    for i, d in enumerate(dates):
        if d >= '2026-05-10':
            start_idx = i; break
    if start_idx is None: continue
    
    # 打印期内价格走势摘要
    close_0510 = None
    for i, d in enumerate(dates):
        if d == '2026-05-09':
            close_0510 = closes[i] if i < len(closes) else None
            break
    if close_0510 is None:
        for i, d in enumerate(dates):
            if d >= '2026-05-09' and d <= '2026-05-11':
                close_0510 = closes[i]; break
    
    latest_close = closes[-1]
    period_ret = (latest_close - close_0510) / close_0510 * 100 if close_0510 else 0
    
    print(f"\n{'─'*125}")
    print(f"  📊 {code} {name} | 基准(5/9): {close_0510:.2f} → 最新(5/25): {latest_close:.2f} | 区间: {period_ret:+.2f}%")
    print(f"     {'日期':>12s} {'收盘':>8s} {'日涨跌':>7s} {'原始分':>7s} {'V分':>6s} {'趋势':>6s} {'动量':>6s} {'波动':>6s} {'量能':>6s} | 评价")
    print(f"     {'─'*100}")
    
    prev_raw = None
    for idx in range(start_idx, len(rows)):
        d = dates[idx]
        close_val = closes[idx]
        chg = float(rows[idx].get('change_pct') or 0)
        
        if d not in target_dates:
            continue
        
        c = closes[:idx+1]; v = vols[:idx+1]
        if len(c) < 120: continue
        
        tr = score_trend(c); mo = score_momentum(c, v)
        vl = score_volatility(c); vo = score_volume(v, c)
        raw = tr*0.30 + mo*0.30 + vl*0.20 + vo*0.20
        vs = vmap(raw, 25)
        
        # 评价
        parts = []
        if tr >= 90: parts.append('趋势极强')
        elif tr >= 70: parts.append('趋势强')
        elif tr < 35: parts.append('趋势弱')
        else: parts.append('趋势中性')
        
        if vl <= 25: parts.append('低波稳健')
        elif vl >= 45: parts.append('⚠️高波动')
        
        if vs >= 45: parts.append('★极端信号')
        elif vs <= 15: parts.append('·中间地带')
        
        # 分数变化方向
        if prev_raw is not None:
            if raw > prev_raw + 3: parts.append('📈加速')
            elif raw < prev_raw - 3: parts.append('📉减速')
        prev_raw = raw
        
        comment = ' | '.join(parts)
        print(f"     {d:>12s} {close_val:>8.2f} {chg:>6.2f}% {raw:>7.1f} {vs:>6.1f} {tr:>6.1f} {mo:>6.1f} {vl:>6.1f} {vo:>6.1f} | {comment}")

cur.close()
conn.close()
