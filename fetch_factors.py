#!/usr/bin/env python3
"""
拉取 daily_basic + moneyflow 入库
全市场批量模式（1次拉取全A股，筛选监控池）
"""
import base64, pymysql, tushare as ts, time, sys, os, math, re

def safe_float(v):
    try:
        f = float(v or 0)
        if math.isnan(f) or math.isinf(f):
            return 0.0
        return f
    except:
        return 0.0

def safe_int(v):
    try:
        return int(v or 0)
    except:
        return 0

def get_mysql_pass():
    try:
        b64 = open("/tmp/.dbp_b64").read().strip()
        return base64.b64decode(b64).decode()
    except:
        with open('/etc/mysql/debian.cnf') as f:
            return re.search(r'password\s*=\s*(\S+)', f.read()).group(1)

def get_stock_conn(pwd):
    return pymysql.connect(host='127.0.0.1',port=3306,user='debian-sys-maint',password=pwd,database='stock_db',charset='utf8mb4')

def get_config_conn(pwd):
    return pymysql.connect(host='127.0.0.1',port=3306,user='debian-sys-maint',password=pwd,database='openclaw_config',charset='utf8mb4')

pwd = get_mysql_pass()
config_conn = get_config_conn(pwd)
cur = config_conn.cursor()
cur.execute("SELECT api_key FROM api_credentials WHERE id=1")
token = cur.fetchone()[0]
cur.close(); config_conn.close()

ts.set_token(token)
pro = ts.pro_api()

# 获取监控池股票
conn = get_stock_conn(pwd)
cur = conn.cursor()
cur.execute("SELECT ts_code FROM watch_pool WHERE is_active=1")
codes = [r[0] for r in cur.fetchall()]
cur.close()
print("监控池: %d 只" % len(codes))
pool_codes = set(codes)

# 获取最近交易日
cur = conn.cursor()
cur.execute("SELECT MAX(trade_date) FROM strategy_signal WHERE trade_date < CURDATE()")
latest = cur.fetchone()[0]
cur.close()
trade_date = str(latest)
ts_date = trade_date.replace("-", "")
print("目标交易日: %s" % trade_date)

# ── 1. daily_basic（全市场1次调用）──
print("\n=== daily_basic（全市场批量）===")
try:
    df = pro.daily_basic(trade_date=ts_date)
    if df is not None and len(df) > 0:
        pool_df = df[df['ts_code'].isin(pool_codes)]
        cur = conn.cursor()
        ok = 0
        for _, r in pool_df.iterrows():
            cur.execute("""
                INSERT INTO daily_basic (ts_code, trade_date, turnover_rate, turnover_rate_f, pe, pe_ttm, pb, ps, ps_ttm, dv_ratio, dv_ttm, total_mv, circ_mv)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE turnover_rate=VALUES(turnover_rate), pe=VALUES(pe), pe_ttm=VALUES(pe_ttm), pb=VALUES(pb), total_mv=VALUES(total_mv), circ_mv=VALUES(circ_mv)
            """, (r["ts_code"], r["trade_date"],
                  safe_float(r.get("turnover_rate")), safe_float(r.get("turnover_rate_f")),
                  safe_float(r.get("pe")), safe_float(r.get("pe_ttm")),
                  safe_float(r.get("pb")), safe_float(r.get("ps")),
                  safe_float(r.get("ps_ttm")), safe_float(r.get("dv_ratio")),
                  safe_float(r.get("dv_ttm")), safe_float(r.get("total_mv")),
                  safe_float(r.get("circ_mv"))))
            ok += 1
        conn.commit()
        cur.close()
        print("daily_basic: 全市场%d条, 筛选%d条 OK" % (len(df), ok))
    else:
        print("daily_basic: 无数据")
except Exception as e:
    print("daily_basic 失败: %s" % str(e)[:100])

# ── 2. moneyflow（全市场1次调用）──
print("\n=== moneyflow（全市场批量）===")
try:
    df = pro.moneyflow(trade_date=ts_date)
    if df is not None and len(df) > 0:
        pool_df = df[df['ts_code'].isin(pool_codes)]
        cur = conn.cursor()
        mf_ok = 0
        for _, r in pool_df.iterrows():
            cur.execute("""
                INSERT INTO moneyflow (ts_code, trade_date,
                  buy_sm_vol, buy_sm_amount, sell_sm_vol, sell_sm_amount,
                  buy_md_vol, buy_md_amount, sell_md_vol, sell_md_amount,
                  buy_lg_vol, buy_lg_amount, sell_lg_vol, sell_lg_amount,
                  buy_elg_vol, buy_elg_amount, sell_elg_vol, sell_elg_amount,
                  net_mf_vol, net_mf_amount)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE
                  buy_elg_amount=VALUES(buy_elg_amount), sell_elg_amount=VALUES(sell_elg_amount),
                  buy_lg_amount=VALUES(buy_lg_amount), sell_lg_amount=VALUES(sell_lg_amount),
                  net_mf_amount=VALUES(net_mf_amount)
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
            mf_ok += 1
        conn.commit()
        cur.close()
        print("moneyflow: 全市场%d条, 筛选%d条 OK" % (len(df), mf_ok))
    else:
        print("moneyflow: 无数据")
except Exception as e:
    print("moneyflow 失败: %s" % str(e)[:100])

conn.close()
print("\n全部完成!")
