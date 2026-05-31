#!/usr/bin/env python3
"""
资金流向批量拉取器 v1.0
========================
从 Tushare moneyflow 拉取最新资金流向数据 → money_flow 表
"""
import os, sys, time, pymysql, tushare as ts
from db_config import get_connection
from datetime import datetime, date, timedelta

def get_token():
    import os
    tk = os.environ.get('TUSHARE_TOKEN', '')
    if tk: return tk
    conn = pymysql.connect(**{**get_connection(), 'database':'openclaw_config'})
    cur = conn.cursor()
    cur.execute("SELECT api_key FROM api_credentials WHERE name='TUSHARE_TOKEN' AND is_active=1")
    r = cur.fetchone()
    cur.close(); conn.close()
    return r[0] if r else ''

def main():
    token = get_token()
    if not token:
        print("❌ TUSHARE_TOKEN 未配置")
        return
    ts.set_token(token)
    pro = ts.pro_api()

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
    print(f"📋 待拉取资金流向: {total}只")

    # 计算需要拉取的日期范围（拉最近3天，覆盖最新交易日）
    end = date.today()
    start = end - timedelta(days=10)
    start_str = start.strftime('%Y%m%d')
    end_str = end.strftime('%Y%m%d')

    success = 0; written = 0; errors = []
    cur2 = conn.cursor()

    for i, code in enumerate(codes):
        if (i+1) % 10 == 0 or i == 0:
            print(f"\r  [{i+1}/{total}] {code}", end='', flush=True)

        try:
            df = pro.moneyflow(ts_code=code, start_date=start_str, end_date=end_str)
            if df is None or len(df) == 0:
                continue

            for _, row in df.iterrows():
                cur2.execute("""
                    INSERT INTO money_flow
                        (ts_code, trade_date,
                         buy_sm_vol, buy_sm_amount, sell_sm_vol, sell_sm_amount,
                         buy_md_vol, buy_md_amount, sell_md_vol, sell_md_amount,
                         buy_lg_vol, buy_lg_amount, sell_lg_vol, sell_lg_amount,
                         buy_elg_vol, buy_elg_amount, sell_elg_vol, sell_elg_amount,
                         net_mf_vol, net_mf_amount)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE
                        net_mf_amount=VALUES(net_mf_amount),
                        buy_lg_amount=VALUES(buy_lg_amount),
                        sell_lg_amount=VALUES(sell_lg_amount)
                """, (
                    row['ts_code'], row['trade_date'],
                    float(row.get('buy_sm_vol',0)or 0), float(row.get('buy_sm_amount',0)or 0),
                    float(row.get('sell_sm_vol',0)or 0), float(row.get('sell_sm_amount',0)or 0),
                    float(row.get('buy_md_vol',0)or 0), float(row.get('buy_md_amount',0)or 0),
                    float(row.get('sell_md_vol',0)or 0), float(row.get('sell_md_amount',0)or 0),
                    float(row.get('buy_lg_vol',0)or 0), float(row.get('buy_lg_amount',0)or 0),
                    float(row.get('sell_lg_vol',0)or 0), float(row.get('sell_lg_amount',0)or 0),
                    float(row.get('buy_elg_vol',0)or 0), float(row.get('buy_elg_amount',0)or 0),
                    float(row.get('sell_elg_vol',0)or 0), float(row.get('sell_elg_amount',0)or 0),
                    float(row.get('net_mf_vol',0)or 0), float(row.get('net_mf_amount',0)or 0),
                ))
                written += 1
            success += 1
            time.sleep(0.35)  # Tushare 频率限制
        except Exception as e:
            errors.append(f'{code}:{e}')
            time.sleep(0.5)

        if (i+1) % 20 == 0:
            conn.commit()

    conn.commit()
    cur2.close()
    cur.close(); conn.close()

    print(f"\n{'='*50}")
    print(f"✅ 完成! {success}/{total}")
    print(f"  写入 money_flow: {written}条")
    if errors:
        print(f"  ⚠️ 错误 ({len(errors)}): {errors[:5]}...")

if __name__ == '__main__':
    t0 = time.time()
    main()
    print(f"  耗时: {time.time()-t0:.1f}s")
