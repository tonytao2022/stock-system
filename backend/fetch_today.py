from db_config import get_connection
#!/usr/bin/env python3
"""拉取今日收盘数据"""
import sys, os, time, pymysql, tushare as _ts

tk = os.environ.get('TUSHARE_TOKEN', '')
if not tk:
    c = get_connection()
    cu = c.cursor()
    cu.execute("SELECT api_key FROM openclaw_config.api_credentials WHERE name='TUSHARE_TOKEN' AND is_active=1")
    r = cu.fetchone()
    if r: tk = r[0]
    c.close()

_ts.set_token(tk)
pro = _ts.pro_api()

# 检查是否是交易日
cal = pro.trade_cal(exchange='SSE', start_date='20260528', end_date='20260528')
if cal is not None and len(cal) > 0 and cal.iloc[0]['is_open'] == 1:
    print('今天(5月28日)是交易日，拉取收盘数据...')
else:
    print('今天不是交易日或数据未就绪')
    sys.exit(0)

conn = get_connection()
cur = conn.cursor()
cur.execute("SELECT ts_code FROM backtest_pool WHERE status='ACTIVE' AND market!='指数'")
codes = [r[0] for r in cur.fetchall()]
print(f'股票数: {len(codes)}只')

# 按市场分组批量拉取
ok = 0
for i, code in enumerate(codes):
    try:
        df = pro.daily(ts_code=code, start_date='20260528', end_date='20260528')
        if df is not None and len(df) > 0:
            r = df.iloc[0]
            cur.execute("""
                INSERT INTO daily_kline (ts_code, trade_date, `open`, high, low, `close`, pre_close, change_pct, vol, amount)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE `close`=VALUES(`close`), change_pct=VALUES(change_pct), vol=VALUES(vol), amount=VALUES(amount)
            """, (code, r['trade_date'], float(r['open']), float(r['high']), float(r['low']),
                  float(r['close']), float(r['pre_close']), float(r['pct_chg']), float(r['vol']), float(r['amount'])))
            conn.commit()
            ok += 1
    except:
        pass
    if (i+1) % 50 == 0:
        print(f'  进度: {i+1}/{len(codes)} (成功{ok})')
    time.sleep(0.3)

print(f'\n✅ 拉取完成: {ok}/{len(codes)}只')
cur.execute("SELECT MAX(trade_date) as d FROM daily_kline")
r = cur.fetchone()
print(f'daily_kline最新: {r[0]}')
cur.execute("SELECT trade_date, COUNT(DISTINCT ts_code) as c FROM daily_kline GROUP BY trade_date ORDER BY trade_date DESC LIMIT 3")
for r2 in cur.fetchall():
    print(f'  {r2[0]}: {r2[1]}只')
conn.close()
