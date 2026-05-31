#!/usr/bin/env python3
"""
缠论结构批量计算器
=================
从 stock_db 读取回测池 + 监控池股票的 daily_kline_qfq 数据
跑完整缠论分析(分型→笔→中枢→背驰→买卖点)并写入 chanlun_structure 表
"""
import os, sys, time, json, math
from db_config import get_connection
import pymysql
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engine.chanlun_analyzer import analyze_chanlun

def load_kline(cur, ts_code, lookback=400):
    """加载一只股票的前复权K线"""
    cur.execute(
        "SELECT trade_date, open, high, low, close, vol FROM daily_kline_qfq "
        "WHERE ts_code=%s ORDER BY trade_date ASC LIMIT %s",
        (ts_code, lookback)
    )
    rows = cur.fetchall()
    if len(rows) < 60:
        return None, f'数据不足({len(rows)}日)'
    trade_date = str(rows[-1]['trade_date'])
    ohlc = []
    for r in rows:
        ohlc.append({
            'high': float(r['high']),
            'low': float(r['low']),
            'open': float(r['open']),
            'close': float(r['close']),
            'vol': float(r.get('vol', 0) or 0),
        })
    return {'trade_date': trade_date, 'ohlc': ohlc}, None

def main():
    conn = db_conn()
    cur = conn.cursor(pymysql.cursors.DictCursor)

    # 获取股票池: 回测池 ACTIVE + 监控池 ACTIVE
    cur.execute(f"""
        SELECT DISTINCT ts_code, name FROM (
            SELECT ts_code, name FROM backtest_pool WHERE status='ACTIVE' AND market!='指数'
            UNION
            SELECT ts_code, name FROM watch_pool WHERE is_active=1 AND user_id='{get_user_id()}'
        ) AS pool ORDER BY ts_code
    """)
    stocks = cur.fetchall()
    total = len(stocks)
    print(f"📋 待分析股票数: {total}")
    print(f"={'='*50}")

    success = 0
    errors = []
    written = 0

    for i, s in enumerate(stocks):
        code = s['ts_code']
        name = s['name'] or code
        print(f"\r  [{i+1}/{total}] {code} {name[:8]:>8s}", end='', flush=True)

        try:
            kline_data, err = load_kline(cur, code)
            if err:
                errors.append(f"{code}:{err}")
                continue

            result = analyze_chanlun(
                code, kline_data['trade_date'], kline_data['ohlc']
            )

            if 'error' in result:
                errors.append(f"{code}:{result['error']}")
                continue

            # 写入 chanlun_structure
            ins_cur = conn.cursor()
            ins_cur.execute("""
                INSERT INTO chanlun_structure
                    (ts_code, trade_date, analysis_level,
                     top_fractal_cnt, bottom_fractal_cnt,
                     bi_direction, bi_strength,
                     zhongshu_count, zhongshu_zd, zhongshu_zg,
                     zhongshu_width, zhongshu_stability,
                     zoushi_type, zoushi_stage,
                     beichi_type, beichi_strength, beichi_validity,
                     macd_area_ratio, dif_dea_diverge,
                     buy_sell_point, buy3_confirmed, buy3_failed,
                     autumn_tiger, tiger_confidence, tiger_reasons,
                     structure_score, is_calculable, calc_error)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE
                    top_fractal_cnt=VALUES(top_fractal_cnt),
                    bottom_fractal_cnt=VALUES(bottom_fractal_cnt),
                    bi_direction=VALUES(bi_direction),
                    bi_strength=VALUES(bi_strength),
                    zhongshu_count=VALUES(zhongshu_count),
                    zhongshu_zd=VALUES(zhongshu_zd),
                    zhongshu_zg=VALUES(zhongshu_zg),
                    zhongshu_width=VALUES(zhongshu_width),
                    zhongshu_stability=VALUES(zhongshu_stability),
                    zoushi_type=VALUES(zoushi_type),
                    zoushi_stage=VALUES(zoushi_stage),
                    beichi_type=VALUES(beichi_type),
                    beichi_strength=VALUES(beichi_strength),
                    beichi_validity=VALUES(beichi_validity),
                    macd_area_ratio=VALUES(macd_area_ratio),
                    dif_dea_diverge=VALUES(dif_dea_diverge),
                    buy_sell_point=VALUES(buy_sell_point),
                    buy3_confirmed=VALUES(buy3_confirmed),
                    autumn_tiger=VALUES(autumn_tiger),
                    tiger_confidence=VALUES(tiger_confidence),
                    tiger_reasons=VALUES(tiger_reasons),
                    structure_score=VALUES(structure_score),
                    is_calculable=VALUES(is_calculable),
                    calc_error=VALUES(calc_error)
            """, (
                result['ts_code'], result['trade_date'], result['analysis_level'],
                result['top_fractal_cnt'], result['bottom_fractal_cnt'],
                result['bi_direction'], result['bi_strength'],
                result['zhongshu_count'], result['zhongshu_zd'], result['zhongshu_zg'],
                result['zhongshu_width'], result['zhongshu_stability'],
                result['zoushi_type'], result['zoushi_stage'],
                result['beichi_type'], result['beichi_strength'], result['beichi_validity'],
                result['macd_area_ratio'], result['dif_dea_diverge'],
                result['buy_sell_point'], result['buy3_confirmed'], result['buy3_failed'],
                result['autumn_tiger'], result['tiger_confidence'],
                result.get('tiger_reasons'),
                result['structure_score'], result['is_calculable'],
                result.get('calc_error'),
            ))
            ins_cur.close()
            conn.commit()
            written += 1
            success += 1

        except Exception as e:
            errors.append(f"{code}:{e}")

        # 节奏控制: 每批20只commit一次
        if (i+1) % 20 == 0:
            print(f" ✅ {written}/{i+1}", end='', flush=True)

    cur.close()
    conn.close()

    print(f"\n{'='*50}")
    print(f"✅ 完成!")
    print(f"  成功: {success}/{total}")
    print(f"  写入 chanlun_structure: {written} 条")
    if errors:
        print(f"  ⚠️ 错误 ({len(errors)}):")
        for e in errors[:10]:
            print(f"    {e}")
        if len(errors) > 10:
            print(f"    ... 还有 {len(errors)-10} 个")

if __name__ == '__main__':
    t0 = time.time()
    main()
    print(f"  耗时: {time.time()-t0:.1f}s")
