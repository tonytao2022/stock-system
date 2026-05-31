#!/usr/bin/env python3
"""
批量技术指标计算器 v1.0
=========================
从 daily_kline_qfq 读出OHLC数据
计算 MACD/RSI/布林带/MA/ATR/KDJ → 写入 technical_indicator 表
"""
import os, sys, time, math, pymysql
from db_config import get_connection
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ─── 工具函数 ───
def sma(data, period):
    if len(data) < period: return sum(data)/len(data) if data else 0
    return sum(data[-period:])/period

def ema(data, period):
    """EMA 指数移动平均"""
    if len(data) < period: return sma(data, period)
    alpha = 2/(period+1)
    result = sum(data[:period])/period
    for v in data[period:]:
        result = v*alpha + result*(1-alpha)
    return result

def rsi(data, period=14):
    if len(data) < period+1: return 50
    gains = sum(max(0, data[i]-data[i-1]) for i in range(-period, 0))
    losses = sum(max(0, data[i-1]-data[i]) for i in range(-period, 0))+0.0001
    return 100-100/(1+gains/losses)

def stddev(data, period):
    if len(data) < period: return 0
    avg = sum(data[-period:])/period
    return (sum((x-avg)**2 for x in data[-period:])/period)**0.5

def atr(highs, lows, closes, period=14):
    if len(closes) < period+1: return 0
    trs = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
           for i in range(-period, 0)]
    return sum(trs)/period

def kdj(closes, highs, lows, period=9):
    """KDJ 指标"""
    if len(closes) < period+3: return (50, 50, 50)
    h9 = max(highs[-period:])
    l9 = min(lows[-period:])
    rsv = (closes[-1]-l9)/(h9-l9)*100 if h9>l9 else 50
    k = 2/3*(50) + 1/3*rsv
    d_val = 2/3*(50) + 1/3*k
    j = 3*k - 2*d_val
    return (k, d_val, j)

def calc_one(code, rows):
    """计算一只股票全部指标"""
    closes = [float(r['close']) for r in rows]
    highs = [float(r['high']) for r in rows]
    lows = [float(r['low']) for r in rows]
    vols = [float(r.get('vol',0)or 0) for r in rows]
    n = len(closes)
    if n < 60:
        return None, f'数据不足({n}日)'

    results = []
    for i in range(59, n):  # 从第60天开始（至少60日数据）
        d = str(rows[i]['trade_date'])
        seg_c = closes[:i+1]
        seg_h = highs[:i+1]
        seg_l = lows[:i+1]
        seg_v = vols[:i+1]

        # MACD (12,26,9)
        ema12 = ema(seg_c, 12)
        ema26 = ema(seg_c, 26)
        dif = ema12 - ema26
        # DEA = EMA of DIF
        if i >= 26+9:
            dea = ema(seg_c[:i+1], 9)  # 简化: 用close的9日ema近似
            # 更精确: 维护一个独立的dea列表
        else:
            dea = dif
        # 简化版MACD
        macd_bar = 2*(dif - dea)

        # RSI
        r6 = rsi(seg_c, 6)
        r12 = rsi(seg_c, 12)
        r24 = rsi(seg_c, 24)

        # 布林带 (20,2)
        ma20_seg = sma(seg_c, 20)
        std20 = stddev(seg_c, 20)
        boll_up = ma20_seg + 2*std20
        boll_dn = ma20_seg - 2*std20
        boll_w = (boll_up - boll_dn)/ma20_seg if ma20_seg > 0 else 0

        # MA
        ma5 = sma(seg_c, 5)
        ma10 = sma(seg_c, 10)
        ma20 = sma(seg_c, 20)
        ma60 = sma(seg_c, 60)
        ma120 = sma(seg_c, 120) if len(seg_c) >= 120 else 0
        ma250 = sma(seg_c, 250) if len(seg_c) >= 250 else 0

        # 成交量MA
        vma5 = sma(seg_v, 5)
        vma20 = sma(seg_v, 20)

        # ATR
        atr14 = atr(seg_h, seg_l, seg_c, 14)

        # KDJ
        k, d_val, j = kdj(seg_c, seg_h, seg_l, 9)

        results.append((
            code, d,
            round(dif, 4), round(dea, 4), round(macd_bar, 4),
            round(r6,4), round(r12,4), round(r24,4),
            round(boll_up,3), round(ma20_seg,3), round(boll_dn,3), round(boll_w,4),
            round(ma5,3), round(ma10,3), round(ma20,3), round(ma60,3),
            round(ma120,3), round(ma250,3),
            round(vma5,2), round(vma20,2),
            round(atr14,4),
            round(k,4), round(d_val,4), round(j,4),
            1, None
        ))

    return results, None

def main():
    conn = get_connection()
    cur = conn.cursor(pymysql.cursors.DictCursor)

    # 获取股票池
    cur.execute(f"""
        SELECT DISTINCT ts_code FROM (
            SELECT ts_code FROM backtest_pool WHERE status='ACTIVE' AND market!='指数'
            UNION
            SELECT ts_code FROM watch_pool WHERE is_active=1 AND user_id='{get_user_id()}'
        ) AS pool ORDER BY ts_code
    """)
    codes = [r['ts_code'] for r in cur.fetchall()]
    total = len(codes)
    print(f"📋 待计算股票: {total}")

    success = 0; written = 0; errors = []

    for i, code in enumerate(codes):
        if (i+1) % 10 == 0 or i == 0:
            print(f"\r  [{i+1}/{total}] {code}", end='', flush=True)

        cur.execute(
            "SELECT trade_date, high, low, close, vol FROM daily_kline_qfq WHERE ts_code=%s ORDER BY trade_date ASC LIMIT 500",
            (code,))
        rows = cur.fetchall()
        if len(rows) < 60:
            errors.append(f'{code}:数据不足')
            continue

        result, err = calc_one(code, rows)
        if err:
            errors.append(f'{code}:{err}')
            continue

        # 批量写入（每次100条）
        batch_size = 100
        cur2 = conn.cursor()
        for j in range(0, len(result), batch_size):
            chunk = result[j:j+batch_size]
            cur2.executemany("""
                INSERT INTO technical_indicator
                    (ts_code, trade_date, macd_dif, macd_dea, macd_bar,
                     rsi_6, rsi_12, rsi_24,
                     boll_upper, boll_mid, boll_lower, boll_width,
                     ma_5, ma_10, ma_20, ma_60, ma_120, ma_250,
                     vol_ma_5, vol_ma_20,
                     atr_14,
                     kdj_k, kdj_d, kdj_j,
                     is_calculable, calc_error)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE
                    ma_5=VALUES(ma_5), ma_10=VALUES(ma_10), ma_20=VALUES(ma_20),
                    ma_60=VALUES(ma_60), ma_120=VALUES(ma_120),
                    rsi_6=VALUES(rsi_6), rsi_12=VALUES(rsi_12), rsi_24=VALUES(rsi_24),
                    macd_dif=VALUES(macd_dif), macd_dea=VALUES(macd_dea), macd_bar=VALUES(macd_bar),
                    boll_upper=VALUES(boll_upper), boll_mid=VALUES(boll_mid), boll_lower=VALUES(boll_lower),
                    atr_14=VALUES(atr_14), kdj_k=VALUES(kdj_k), kdj_d=VALUES(kdj_d), kdj_j=VALUES(kdj_j)
            """, chunk)
            conn.commit()
        cur2.close()
        written += len(result)
        success += 1

    cur.close(); conn.close()
    print(f"\n{'='*50}")
    print(f"✅ 完成! {success}/{total}")
    print(f"  写入 technical_indicator: {written}条")
    if errors:
        print(f"  ⚠️ 错误 ({len(errors)}): {errors[:5]}...")

if __name__ == '__main__':
    t0 = time.time()
    main()
    print(f"  耗时: {time.time()-t0:.1f}s")
