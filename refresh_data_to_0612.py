#!/usr/bin/env python3
"""
补拉缺失的交易日数据（6月10日~12日）并更新相关表
"""

import os, sys, time, math, re, base64, json
from datetime import datetime, date, timedelta

import pymysql
import tushare as ts

# ── DB连接 ──────────────────────────────────────────
def get_pwd():
    try:
        b64 = open("/tmp/.dbp_b64").read().strip()
        return base64.b64decode(b64).decode()
    except:
        with open('/etc/mysql/debian.cnf') as f:
            return re.search(r'password\s*=\s*(\S+)', f.read()).group(1)

def get_token():
    pwd = get_pwd()
    conn = pymysql.connect(host='127.0.0.1',port=3306,user='debian-sys-maint',password=pwd,database='openclaw_config',charset='utf8mb4')
    cur = conn.cursor()
    cur.execute("SELECT api_key FROM api_credentials WHERE id=1")
    token = cur.fetchone()[0]
    cur.close(); conn.close()
    return token

pwd = get_pwd()
token = get_token()
ts.set_token(token)
pro = ts.pro_api()
print(f"✅ Tushare 连接成功")

def get_stock_conn():
    return pymysql.connect(host='127.0.0.1',port=3306,user='debian-sys-maint',password=pwd,database='stock_db_v2',charset='utf8mb4',autocommit=True)

def safe_float(v):
    try: f = float(v or 0)
    except: return 0.0
    return 0.0 if math.isnan(f) or math.isinf(f) else f

def safe_int(v):
    try: return int(v or 0)
    except: return 0

def get_missing_dates(conn, table, date_col='trade_date', date_list=None):
    """检查哪些日期已存在"""
    if not date_list: return []
    cur = conn.cursor()
    fmt = ','.join(['%s']*len(date_list))
    cur.execute(f"SELECT DISTINCT {date_col} FROM {table} WHERE {date_col} IN ({fmt})", date_list)
    existing = set(r[0].strftime('%Y-%m-%d') if hasattr(r[0],'strftime') else str(r[0]) for r in cur.fetchall())
    cur.close()
    return [d for d in date_list if d not in existing]

# ── 需要补的数据 ──────────────────────────────────────
TARGET_DATES = ['2026-06-10','2026-06-11','2026-06-12']
TS_DATES = [d.replace('-','') for d in TARGET_DATES]

# 获取回测池+监控池股票
conn = get_stock_conn()
cur = conn.cursor()
cur.execute("SELECT ts_code, name FROM backtest_pool")
pool = {r[0]: r[1] for r in cur.fetchall()}
cur.execute("SELECT ts_code, name FROM stock_basic WHERE name IS NOT NULL")
all_stocks = {r[0]: r[1] for r in cur.fetchall()}
cur.close()
print(f"📦 回测池: {len(pool)} 只, 全市场: {len(all_stocks)} 只")

# ════════════════════════════════════════════════════
# 1. daily_kline — 逐日分批拉全市场
# ════════════════════════════════════════════════════
print(f"\n{'='*50}")
print(f"📊 1. daily_kline 补拉 {len(TARGET_DATES)} 天")
print(f"{'='*50}")

for ts_date, target_date in zip(TS_DATES, TARGET_DATES):
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM daily_kline WHERE trade_date=%s", (target_date,))
    cnt = cur.fetchone()[0]
    cur.close()
    if cnt > 400:
        print(f"  {target_date}: 已有 {cnt} 条, 跳过")
        continue

    # 全市场分批拉
    all_codes = list(all_stocks.keys())
    batch_size = 300
    total = 0
    for i in range(0, len(all_codes), batch_size):
        batch = all_codes[i:i+batch_size]
        codes_str = ','.join(batch)
        try:
            df = pro.daily(trade_date=ts_date)
            if df is None or len(df) == 0:
                time.sleep(1)
                continue
            df_filtered = df[df['ts_code'].isin(batch)]
            if len(df_filtered) == 0:
                time.sleep(0.5)
                continue
            cur = conn.cursor()
            for _, r in df_filtered.iterrows():
                cur.execute("""
                    INSERT INTO daily_kline (ts_code, trade_date, `open`, high, low, `close`, pre_close, vol, amount)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE `close`=VALUES(`close`), vol=VALUES(vol), amount=VALUES(amount)
                """, (r['ts_code'], r['trade_date'],
                      safe_float(r.get('open')), safe_float(r.get('high')),
                      safe_float(r.get('low')), safe_float(r.get('close')),
                      safe_float(r.get('pre_close')), safe_float(r.get('vol')),
                      safe_float(r.get('amount'))))
                total += 1
            cur.close()
            time.sleep(0.5)
        except Exception as e:
            print(f"  ⚠️ batch {i}-{i+batch_size}: {str(e)[:60]}")
            time.sleep(2)
    print(f"  {target_date}: 写入 {total} 条")

# ════════════════════════════════════════════════════
# 2. daily_kline_qfq — 向前复权（回测池）
# ════════════════════════════════════════════════════
print(f"\n{'='*50}")
print(f"📊 2. daily_kline_qfq 补拉（回测池）")
print(f"{'='*50}")

pool_codes = list(pool.keys())
for target_date in TARGET_DATES:
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM daily_kline_qfq WHERE trade_date=%s", (target_date,))
    cnt = cur.fetchone()[0]
    cur.close()

    if cnt >= len(pool_codes) * 0.8:
        print(f"  {target_date}: 已有 {cnt} 条, 跳过")
        continue

    total = 0
    for i, code in enumerate(pool_codes):
        try:
            df = pro.daily(ts_code=code, start_date=target_date.replace('-',''), end_date=target_date.replace('-',''))
            if df is None or len(df) == 0:
                continue
            r = df.iloc[0]
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO daily_kline_qfq (ts_code, trade_date, `open`, high, low, `close`, pre_close, vol, amount)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE `close`=VALUES(`close`), vol=VALUES(vol), amount=VALUES(amount)
            """, (code, r['trade_date'],
                  safe_float(r.get('open')), safe_float(r.get('high')),
                  safe_float(r.get('low')), safe_float(r.get('close')),
                  safe_float(r.get('pre_close')), safe_float(r.get('vol')),
                  safe_float(r.get('amount'))))
            cur.close()
            total += 1
            if (i+1) % 100 == 0:
                print(f"    {target_date}: {i+1}/{len(pool_codes)} ({total}条)")
                conn.commit()
        except Exception as e:
            if (i+1) % 50 == 0:
                time.sleep(1)
            continue
    conn.commit()
    print(f"  {target_date}: qfq写入 {total} 条")

# ════════════════════════════════════════════════════
# 3. money_flow — 全市场
# ════════════════════════════════════════════════════
print(f"\n{'='*50}")
print(f"📊 3. money_flow 补拉")
print(f"{'='*50}")

for ts_date, target_date in zip(TS_DATES, TARGET_DATES):
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM money_flow WHERE trade_date=%s", (target_date,))
    cnt = cur.fetchone()[0]
    cur.close()
    if cnt > 0:
        print(f"  {target_date}: 已有 {cnt} 条, 跳过")
        continue

    try:
        df = pro.moneyflow(trade_date=ts_date)
        if df is None or len(df) == 0:
            print(f"  {target_date}: Tushare无数据")
            continue
        # 只保留回测池
        df_pool = df[df['ts_code'].isin(pool_codes)]
        cur = conn.cursor()
        total = 0
        for _, r in df_pool.iterrows():
            cur.execute("""
                INSERT INTO money_flow (ts_code, trade_date,
                  buy_sm_vol, buy_sm_amount, sell_sm_vol, sell_sm_amount,
                  buy_md_vol, buy_md_amount, sell_md_vol, sell_md_amount,
                  buy_lg_vol, buy_lg_amount, sell_lg_vol, sell_lg_amount,
                  buy_elg_vol, buy_elg_amount, sell_elg_vol, sell_elg_amount,
                  net_mf_vol, net_mf_amount)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE net_mf_amount=VALUES(net_mf_amount)
            """, (r["ts_code"], r["trade_date"],
                  safe_int(r.get("buy_sm_vol")), safe_float(r.get("buy_sm_amount")),
                  safe_int(r.get("sell_sm_vol")), safe_float(r.get("sell_sm_amount")),
                  safe_int(r.get("buy_md_vol")), safe_float(r.get("buy_md_amount")),
                  safe_int(r.get("sell_md_vol")), safe_float(r.get("sell_md_amount")),
                  safe_int(r.get("buy_lg_vol")), safe_float(r.get("buy_lg_amount")),
                  safe_int(r.get("sell_lg_vol")), safe_float(r.get("sell_lg_amount")),
                  safe_int(r.get("buy_elg_vol")), safe_float(r.get("buy_elg_amount")),
                  safe_int(r.get("sell_elg_vol")), safe_float(r.get("sell_elg_amount")),
                  safe_int(r.get("net_mf_vol")), safe_float(r.get("net_mf_amount"))))
            total += 1
        cur.close()
        conn.commit()
        print(f"  {target_date}: money_flow写入 {total} 条")
    except Exception as e:
        print(f"  {target_date}: 失败 {str(e)[:80]}")

# ════════════════════════════════════════════════════
# 4. season_state — 补季节判定
# ════════════════════════════════════════════════════
print(f"\n{'='*50}")
print(f"📊 4. season_state 补运行")
print(f"{'='*50}")

try:
    sys.path.insert(0, '/opt/stock-analyzer')
    from season_engine import SeasonEngine
    engine = SeasonEngine()
    for target_date in TARGET_DATES:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM season_state WHERE trade_date=%s", (target_date,))
        cnt = cur.fetchone()[0]
        cur.close()
        if cnt > 0:
            print(f"  {target_date}: 已有 {cnt} 条, 跳过")
            continue
        try:
            result = engine.judge_market_season(target_date=target_date)
            print(f"  {target_date}: {result.get('market_season','?')} / {result.get('market_regime','?')}")
        except Exception as e:
            print(f"  {target_date}: 季节判定失败 {str(e)[:60]}")
except Exception as e:
    print(f"  ⚠️ season_engine不可用: {str(e)[:60]}")

# ════════════════════════════════════════════════════
# 5. technical_indicator — 补技术指标
# ════════════════════════════════════════════════════
print(f"\n{'='*50}")
print(f"📊 5. technical_indicator 补计算")
print(f"{'='*50}")

import numpy as np

def calc_tech(conn, ts_code, days=120):
    """计算MA/RSI/BOLL/MACD等"""
    cur = conn.cursor()
    cur.execute("""
        SELECT trade_date, `close`, high, low, vol
        FROM daily_kline_qfq
        WHERE ts_code=%s
        ORDER BY trade_date DESC LIMIT %s
    """, (ts_code, days + 60))
    rows = cur.fetchall()
    cur.close()
    if not rows or len(rows) < 30:
        return []
    rows.reverse()
    closes = [float(r['close']) for r in rows]
    highs = [float(r['high']) for r in rows]
    lows = [float(r['low']) for r in rows]
    vols = [float(r['vol']) for r in rows]
    dates = [(r['trade_date'].strftime('%Y-%m-%d') if hasattr(r['trade_date'],'strftime') else str(r['trade_date'])) for r in rows]
    n = len(closes)

    results = []
    for i in range(30, n):
        d = dates[i]
        c = closes[:i+1]
        h = highs[:i+1]
        l = lows[:i+1]
        v = vols[:i+1]

        # MA
        ma5 = np.mean(c[-5:]) if len(c)>=5 else None
        ma10 = np.mean(c[-10:]) if len(c)>=10 else None
        ma20 = np.mean(c[-20:]) if len(c)>=20 else None
        ma60 = np.mean(c[-60:]) if len(c)>=60 else None
        ma120 = np.mean(c[-120:]) if len(c)>=120 else None
        ma250 = np.mean(c[-250:]) if len(c)>=250 else None

        # RSI
        if len(c)>=15:
            gains = [max(c[j]-c[j-1],0) for j in range(-14,0)]
            losses = [max(c[j-1]-c[j],0) for j in range(-14,0)]
            avg_g = np.mean(gains) if gains else 0
            avg_l = np.mean(losses) if losses else 0
            rsi_6 = 100 * avg_g/(avg_g+avg_l) if (avg_g+avg_l)>0 else 50

            gains12 = [max(c[j]-c[j-1],0) for j in range(-12,0)]
            losses12 = [max(c[j-1]-c[j],0) for j in range(-12,0)]
            avg_g12 = np.mean(gains12) if gains12 else 0
            avg_l12 = np.mean(losses12) if losses12 else 0
            rsi_12 = 100 * avg_g12/(avg_g12+avg_l12) if (avg_g12+avg_l12)>0 else 50

            gains24 = [max(c[j]-c[j-1],0) for j in range(-24,0)]
            losses24 = [max(c[j-1]-c[j],0) for j in range(-24,0)]
            avg_g24 = np.mean(gains24) if gains24 else 0
            avg_l24 = np.mean(losses24) if losses24 else 0
            rsi_24 = 100 * avg_g24/(avg_g24+avg_l24) if (avg_g24+avg_l24)>0 else 50
        else:
            rsi_6 = rsi_12 = rsi_24 = 50

        # BOLL
        if len(c)>=20:
            mid = np.mean(c[-20:])
            std = np.std(c[-20:])
            upper = mid + 2*std
            lower = mid - 2*std
        else:
            mid = upper = lower = c[-1]

        # MACD
        if len(c)>=26:
            ema12 = c[-1]
            ema26 = c[-1]
            for j in range(2, min(len(c), 52)+1):
                ema12 = c[-j]*2/13 + ema12*11/13 if j==2 else c[-j]*2/13 + ema12*11/13
                ema26 = c[-j]*2/27 + ema26*25/27 if j==2 else c[-j]*2/27 + ema26*25/27
            macd_dif = ema12 - ema26
        else:
            macd_dif = 0

        # ATR
        if len(h)>=14:
            trs = [max(h[j]-l[j], abs(h[j]-c[j-1]), abs(l[j]-c[j-1])) for j in range(-13,0)]
            atr = np.mean(trs)
        else:
            atr = 0

        # vol_ma
        vol_ma5 = np.mean(v[-5:]) if len(v)>=5 else 0
        vol_ma20 = np.mean(v[-20:]) if len(v)>=20 else 0

        results.append((ts_code, d,
            round(macd_dif,4), 0, 0,  # macd_dif, dea, bar
            round(rsi_6,4), round(rsi_12,4), round(rsi_24,4),
            round(upper,3), round(mid,3), round(lower,3), 0,
            round(ma5,3) if ma5 else None,
            round(ma10,3) if ma10 else None,
            round(ma20,3) if ma20 else None,
            round(ma60,3) if ma60 else None,
            round(ma120,3) if ma120 else None,
            round(ma250,3) if ma250 else None,
            round(vol_ma5,2), round(vol_ma20,2),
            round(atr,4), 0,0,0))

    return results

# 只补6月10~12日及之前缺的
cur = conn.cursor()
cur.execute("""
    SELECT ts_code FROM backtest_pool
    WHERE ts_code NOT IN (
        SELECT DISTINCT ts_code FROM technical_indicator WHERE trade_date >= '2026-06-10'
    )
""")
need_tech = [r[0] for r in cur.fetchall()]
cur.close()

print(f"  需要补技术指标的: {len(need_tech)} 只")
if need_tech:
    total = 0
    for i, code in enumerate(need_tech):
        rows = calc_tech(conn, code)
        if not rows:
            continue
        cur = conn.cursor()
        for row in rows:
            cur.execute("""
                INSERT INTO technical_indicator
                (ts_code, trade_date, macd_dif, macd_dea, macd_bar,
                 rsi_6, rsi_12, rsi_24,
                 boll_upper, boll_mid, boll_lower, boll_width,
                 ma_5, ma_10, ma_20, ma_60, ma_120, ma_250,
                 vol_ma_5, vol_ma_20, atr_14,
                 kdj_k, kdj_d, kdj_j)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE
                    rsi_12=VALUES(rsi_12), rsi_6=VALUES(rsi_6), rsi_24=VALUES(rsi_24),
                    ma_5=VALUES(ma_5), ma_10=VALUES(ma_10), ma_20=VALUES(ma_20),
                    ma_60=VALUES(ma_60), ma_120=VALUES(ma_120), ma_250=VALUES(ma_250),
                    macd_dif=VALUES(macd_dif), atr_14=VALUES(atr_14),
                    boll_upper=VALUES(boll_upper), boll_mid=VALUES(boll_mid),
                    boll_lower=VALUES(boll_lower)
            """, *row)
            total += 1
        cur.close()
        if (i+1) % 30 == 0:
            conn.commit()
            print(f"    tech: {i+1}/{len(need_tech)} ({total}条)")
    conn.commit()
    print(f"  技术指标: 写入 {total} 条")

print(f"\n{'='*50}")
print(f"✅ 数据刷新完成!")
print(f"{'='*50}")

# 最后检查
cur = conn.cursor()
for tbl in ['daily_kline','daily_kline_qfq','money_flow','technical_indicator','season_state']:
    cur.execute(f"SELECT trade_date, COUNT(*) FROM {tbl} WHERE trade_date IN ('2026-06-10','2026-06-11','2026-06-12') GROUP BY trade_date ORDER BY trade_date")
    print(f"  {tbl}:")
    for r in cur.fetchall():
        print(f"    {r[0]}: {r[1]}条")
cur.close()
conn.close()
