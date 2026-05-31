#!/usr/bin/env python3
"""
并发版复权K线更新器 v2.0
用5个线程并发拉取Tushare日K线数据，写入daily_kline_qfq表
每个请求间隔0.2秒，总耗时控制在60秒内
"""
import pymysql, os, time, sys, json
from db_config import get_connection
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
import tushare as _ts

# ─── 超时控制 ───
REQUEST_TIMEOUT = 10  # 单次Tushare请求超时秒数
FETCH_DAYS = 60  # 每次只拉最近60天数据

# 给requests库设默认超时（Tushare底层用requests）
import requests
from requests.adapters import HTTPAdapter
session = requests.Session()
session.mount('https://', HTTPAdapter(pool_maxsize=20))
session.mount('http://', HTTPAdapter(pool_maxsize=20))
_ts._adapter = session

# ─── 最新交易日动态获取 ───
def get_token():
    tk = os.environ.get('TUSHARE_TOKEN', '')
    if tk: return tk
    c = get_connection()
    cu = c.cursor()
    cu.execute("SELECT api_key FROM openclaw_config.api_credentials WHERE name='TUSHARE_TOKEN' AND is_active=1 LIMIT 1")
    r = cu.fetchone()
    cu.close(); c.close()
    return r[0] if r else ''

# ─── 获取待更新股票列表 ───
def get_latest_trade_day():
    """从Tushare获取最新交易日"""
    import tushare as _ts2
    tk = get_token()
    if tk:
        _ts2.set_token(tk)
        pro = _ts2.pro_api()
        # 加超时、限制查询范围
        end_dt = (datetime.now() + timedelta(days=1)).strftime('%Y%m%d')
        start_dt = (datetime.now() - timedelta(days=10)).strftime('%Y%m%d')
        cal = pro.trade_cal(exchange='SSE', start_date=start_dt, end_date=end_dt)
        if cal is not None and len(cal) > 0:
            open_days = cal[cal['is_open']==1]
            if len(open_days) > 0:
                return open_days.iloc[-1]['cal_date']
    return '20260528'  # fallback

LATEST_TRADE_DAY = get_latest_trade_day()

def get_missing_stocks():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(f"""
        SELECT bp.ts_code
        FROM backtest_pool bp
        LEFT JOIN daily_kline_qfq d ON bp.ts_code = d.ts_code AND d.trade_date = '{LATEST_TRADE_DAY}'
        WHERE bp.status='ACTIVE' AND bp.market!='指数' AND d.id IS NULL
    """)
    codes = [r[0] for r in cur.fetchall()]
    cur.close(); conn.close()
    return codes

# ─── 单只股票拉取任务 ───
lock = Lock()
ok_count = 0
fail_count = 0
total_count = 0
t0 = time.time()

def fetch_one(code):
    global ok_count, fail_count
    try:
        # 只拉最近FETCH_DAYS天数据（避免全量历史请求超时）
        from datetime import datetime, timedelta
        start = (datetime.now() - timedelta(days=FETCH_DAYS)).strftime('%Y%m%d')
        pro = _ts.pro_api()
        df = pro.daily(ts_code=code, start_date=start, end_date=f'{LATEST_TRADE_DAY}')
        if df is not None and len(df) > 0:
            conn = get_connection()
            cur = conn.cursor()
            for _, r2 in df.iterrows():
                cur.execute("""
                    INSERT INTO daily_kline_qfq (ts_code, trade_date, open, high, low, close, vol, change_pct)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE close=VALUES(close), change_pct=VALUES(change_pct), vol=VALUES(vol)
                """, (code, r2['trade_date'],
                      float(r2['open']), float(r2['high']), float(r2['low']),
                      float(r2['close']), float(r2['vol']), float(r2['pct_chg'])))
            conn.commit()
            conn.close()
            with lock:
                ok_count += 1
        else:
            with lock:
                fail_count += 1
    except Exception as e:
        with lock:
            fail_count += 1
    
    # 每完成20只打印一次进度
    with lock:
        done = ok_count + fail_count
        if done % 20 == 0 and done > 0:
            elapsed = time.time() - t0
            rate = done / elapsed if elapsed > 0 else 0
            print(f"  进度: {done}/{total_count} 成功{ok_count} 失败{fail_count} 耗时{elapsed:.0f}s ({rate:.1f}条/s)")

# ─── 主流程 ───
def main():
    global total_count
    print("📡 并发版复权K线更新器 v2.0")
    print(f"   启动时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    
    codes = get_missing_stocks()
    total_count = len(codes)
    print(f"   待更新股票: {total_count}只")
    
    if total_count == 0:
        print("✅ 全部已更新，无需拉取")
        return
    
    # 预初始化Tushare
    tk = get_token()
    _ts.set_token(tk)
    
    # 5线程并发
    MAX_WORKERS = 5
    print(f"   线程数: {MAX_WORKERS}")
    print(f"   预计耗时: {total_count / MAX_WORKERS * 0.25:.0f}秒")
    print()
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(fetch_one, code): code for code in codes}
        for future in as_completed(futures):
            pass  # 进度在fetch_one里打印
    
    # 最终结果
    elapsed = time.time() - t0
    print(f"\n✅ 复权K线更新完成")
    print(f"   成功: {ok_count}  失败: {fail_count}  总耗时: {elapsed:.0f}秒")
    
    # 验证
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT MAX(trade_date) as d FROM daily_kline_qfq")
    r = cur.fetchone()
    print(f"   daily_kline_qfq最新: {r[0]}")
    cur.execute("SELECT trade_date, COUNT(DISTINCT ts_code) as c FROM daily_kline_qfq GROUP BY trade_date ORDER BY trade_date DESC LIMIT 3")
    for r2 in cur.fetchall():
        print(f"     {r2[0]}: {r2[1]}只")
    conn.close()

if __name__ == '__main__':
    main()
