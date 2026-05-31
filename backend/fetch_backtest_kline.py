#!/usr/bin/env python3
"""
从Tushare拉取回测池股票最近一年的日K线数据，存入MySQL
用法: python fetch_backtest_kline.py [--days 365]
"""
import os, sys, time, argparse
from db_config import get_connection
import pymysql
import tushare as ts
from datetime import datetime, date, timedelta

# ─── 参数 ───
parser = argparse.ArgumentParser()
parser.add_argument('--days', type=int, default=365, help='回看天数')
parser.add_argument('--batch-size', type=int, default=30, help='每批股票数')
parser.add_argument('--sleep', type=float, default=0.3, help='批次间隔秒')
parser.add_argument('--retry', type=int, default=3, help='重试次数')
args = parser.parse_args()

# ─── 数据库连接 ───
def get_password():
    try:
        with open('/etc/mysql/debian.cnf') as f:
            for line in f:
                if 'password' in line:
                    return line.strip().split('=')[-1].strip().strip('"').strip("'")
    except: pass
    return os.environ.get('MYSQL_PASSWORD', '')



# ─── Tushare初始化 ───
token = os.environ.get('TUSHARE_TOKEN', '')
if not token:
    # fallback: from MySQL
    with get_connection() as c:
        cu = c.cursor(pymysql.cursors.DictCursor)
        cu.execute("SELECT api_key FROM openclaw_config.api_credentials WHERE name='TUSHARE_TOKEN' AND is_active=1")
        row = cu.fetchone()
        if row: token = row['api_key']
    if not token:
        print("❌ TUSHARE_TOKEN not found!")
        sys.exit(1)
ts.set_token(token)
pro = ts.pro_api()

# ─── 获取回测池股票列表 ───
def get_backtest_stocks():
    with get_connection() as c:
        cu = c.cursor(pymysql.cursors.DictCursor)
        cu.execute("SELECT ts_code, name, market FROM backtest_pool WHERE status='ACTIVE' ORDER BY ts_code")
        return cu.fetchall()

# ─── 拉取单只股票的日K线 ───
def fetch_kline(ts_code, start_date, end_date):
    for attempt in range(args.retry):
        try:
            df = pro.daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
            time.sleep(0.15)
            if df is not None and not df.empty:
                df = df.sort_values('trade_date')
                df = df.where(df.notna(), None)
                rows = []
                for _, r in df.iterrows():
                    td = r.get('trade_date', '')
                    if isinstance(td, str) and len(td) == 8:
                        td = f"{td[:4]}-{td[4:6]}-{td[6:8]}"
                    rows.append((
                        ts_code, td,
                        r.get('open'), r.get('high'), r.get('low'), r.get('close'),
                        r.get('pre_close'), r.get('pct_chg'),
                        r.get('vol'), r.get('amount'),
                        r.get('turnover_rate'), r.get('volume_ratio'),
                        r.get('pe'), r.get('pb'), r.get('total_mv'),
                        'tushare', 1
                    ))
                return rows
            return []
        except Exception as e:
            err = str(e)
            if '每分钟最多访问' in err or '200次' in err or 'rate limit' in err.lower():
                wait = 65 + attempt * 10
                print(f"  ⏳ 频率限制，等待{wait}s...")
                time.sleep(wait)
                continue
            if attempt < args.retry - 1:
                time.sleep(2 + attempt)
                continue
            print(f"  ⚠️ {ts_code} 失败: {err[:80]}")
            return []
    return []

# ─── 批量写入MySQL ───
INSERT_SQL = """INSERT IGNORE INTO daily_kline 
    (ts_code, trade_date, open, high, low, close, pre_close, change_pct,
     vol, amount, turnover_rate, volume_ratio, pe, pb, total_mv, data_source, is_valid)
    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"""

def insert_rows(conn, rows):
    cur = conn.cursor()
    cur.executemany(INSERT_SQL, rows)
    conn.commit()
    return cur.rowcount

# ─── 主流程 ───
def main():
    stocks = get_backtest_stocks()
    print(f"📋 回测池股票: {len(stocks)}只")
    print(f"📅 拉取周期: 过去 {args.days} 天")
    
    start_date = (date.today() - timedelta(days=args.days)).strftime('%Y%m%d')
    end_date = date.today().strftime('%Y%m%d')
    print(f"📆 日期范围: {start_date} ~ {end_date}")
    
    total_inserted = 0
    total_rows = 0
    failed = []
    
    conn = get_connection()
    
    for i in range(0, len(stocks), args.batch_size):
        batch = stocks[i:i + args.batch_size]
        batch_no = i // args.batch_size + 1
        total_batches = (len(stocks) + args.batch_size - 1) // args.batch_size
        
        print(f"\n📦 批次 {batch_no}/{total_batches} ({len(batch)}只股票)")
        
        for stock in batch:
            ts_code = stock['ts_code']
            print(f"  📥 {ts_code} {stock['name']}", end='', flush=True)
            rows = fetch_kline(ts_code, start_date, end_date)
            if rows:
                cnt = insert_rows(conn, rows)
                total_inserted += cnt
                total_rows += len(rows)
                print(f" ✅ {len(rows)}条 (新增{cnt})")
            else:
                failed.append(ts_code)
                print(f" ⚠️ 无数据")
        
        if i + args.batch_size < len(stocks):
            time.sleep(args.sleep)
    
    conn.close()
    
    # ─── 汇总 ───
    print(f"\n{'='*60}")
    print(f"✅ 完成!")
    print(f"   股票数: {len(stocks)}")
    print(f"   总K线条数: {total_rows}")
    print(f"   新增入库: {total_inserted}")
    print(f"   无数据/失败: {len(failed)}只")
    if failed:
        print(f"   失败列表: {', '.join(failed[:10])}" + ('...' if len(failed)>10 else ''))
    
    # ─── 检查数据覆盖 ───
    with get_connection() as c:
        cu = c.cursor()
        cu.execute("""
            SELECT COUNT(DISTINCT ts_code) AS stock_cnt, 
                   MIN(trade_date) AS min_date, MAX(trade_date) AS max_date,
                   COUNT(*) AS total_rows
            FROM daily_kline
            WHERE ts_code IN (SELECT ts_code FROM backtest_pool WHERE status='ACTIVE')
        """)
        r = cu.fetchone()
        print(f"\n📊 数据库汇总:")
        print(f"   覆盖股票: {r[0]}/{len(stocks)}")
        print(f"   日期范围: {r[1]} ~ {r[2]}")
        print(f"   K线总行数: {r[3]}")

if __name__ == '__main__':
    main()
