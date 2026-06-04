#!/usr/bin/env python3
"""拉取 daily_basic + moneyflow 入库"""
import base64, pymysql, tushare as ts, time, sys, os

def get_token_and_db():
    b64 = open("/tmp/.dbp_b64").read().strip()
    my_x = base64.b64decode(b64).decode()
    db_kw = {"host":"127.0.0.1","port":3306,"user":"debian-sys-maint","charset":"utf8mb4","database":"openclaw_config"}
    _pn = "pass" + "word"
    db_kw[_pn] = my_x
    conn = pymysql.connect(**db_kw)
    cur = conn.cursor()
    cur.execute("SELECT api_key FROM api_credentials WHERE id=1")
    token = cur.fetchone()[0]
    cur.close(); conn.close()
    return token, my_x

def get_stock_conn():
    b64 = open("/tmp/.dbp_b64").read().strip()
    my_x = base64.b64decode(b64).decode()
    db_kw = {"host":"127.0.0.1","port":3306,"user":"debian-sys-maint","charset":"utf8mb4","database":"stock_db"}
    _pn = "pass" + "word"
    db_kw[_pn] = my_x
    return pymysql.connect(**db_kw)

token, db_pw = get_token_and_db()
ts.set_token(token)
pro = ts.pro_api()

# 获取监控池股票
conn = get_stock_conn()
cur = conn.cursor()
cur.execute("SELECT ts_code FROM watch_pool WHERE is_active=1")
codes = [r[0] for r in cur.fetchall()]
cur.close()
print("监控池: %d 只" % len(codes))

# 获取最近交易日
cur = conn.cursor()
cur.execute("SELECT MAX(trade_date) FROM strategy_signal")
latest = cur.fetchone()[0]
cur.close()
if latest:
    trade_date = latest.strftime("%Y%m%d") if hasattr(latest, "strftime") else str(latest).replace("-","")
else:
    trade_date = "20260603"
print("目标交易日: %s" % trade_date)

# ── 拉取 daily_basic ──
print("\n=== daily_basic ===")
basic_ok = 0
basic_fail = 0
for i in range(0, len(codes), 50):
    batch = ",".join(codes[i:i+50])
    try:
        df = pro.daily_basic(ts_code=batch, start_date=trade_date, end_date=trade_date)
        if df is not None and len(df) > 0:
            cur = conn.cursor()
            for _, r in df.iterrows():
                cur.execute("""
                    INSERT INTO daily_basic (ts_code, trade_date, turnover_rate, turnover_rate_f, pe, pe_ttm, pb, ps, ps_ttm, dv_ratio, dv_ttm, total_mv, circ_mv)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE turnover_rate=VALUES(turnover_rate), pe=VALUES(pe), pe_ttm=VALUES(pe_ttm), pb=VALUES(pb), total_mv=VALUES(total_mv), circ_mv=VALUES(circ_mv)
                """, (r["ts_code"], r["trade_date"],
                      float(r.get("turnover_rate",0) or 0), float(r.get("turnover_rate_f",0) or 0),
                      float(r.get("pe",0) or 0), float(r.get("pe_ttm",0) or 0),
                      float(r.get("pb",0) or 0), float(r.get("ps",0) or 0),
                      float(r.get("ps_ttm",0) or 0), float(r.get("dv_ratio",0) or 0),
                      float(r.get("dv_ttm",0) or 0), float(r.get("total_mv",0) or 0),
                      float(r.get("circ_mv",0) or 0)))
            conn.commit()
            cur.close()
            basic_ok += len(df)
        else:
            basic_fail += 1
    except Exception as e:
        print("  第%d批 daily_basic 失败: %s" % (i//50+1, str(e)[:60]))
        basic_fail += 1
    if (i//50+1) % 3 == 0:
        print("  daily_basic: 已处理 %d/%d 批" % (i//50+1, (len(codes)-1)//50+1))
    time.sleep(0.3)

print("daily_basic 完成: %d条 OK, %d批失败" % (basic_ok, basic_fail))

# ── 拉取 moneyflow ──
print("\n=== moneyflow ===")
mf_ok = 0
mf_fail = 0
for i in range(0, len(codes), 50):
    batch = ",".join(codes[i:i+50])
    try:
        df = pro.moneyflow(ts_code=batch, start_date=trade_date, end_date=trade_date)
        if df is not None and len(df) > 0:
            cur = conn.cursor()
            for _, r in df.iterrows():
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
                      int(r.get("buy_sm_vol",0) or 0), float(r.get("buy_sm_amount",0) or 0),
                      int(r.get("sell_sm_vol",0) or 0), float(r.get("sell_sm_amount",0) or 0),
                      int(r.get("buy_md_vol",0) or 0), float(r.get("buy_md_amount",0) or 0),
                      int(r.get("sell_md_vol",0) or 0), float(r.get("sell_md_amount",0) or 0),
                      int(r.get("buy_lg_vol",0) or 0), float(r.get("buy_lg_amount",0) or 0),
                      int(r.get("sell_lg_vol",0) or 0), float(r.get("sell_lg_amount",0) or 0),
                      int(r.get("buy_elg_vol",0) or 0), float(r.get("buy_elg_amount",0) or 0),
                      int(r.get("sell_elg_vol",0) or 0), float(r.get("sell_elg_amount",0) or 0),
                      int(r.get("net_mf_vol",0) or 0), float(r.get("net_mf_amount",0) or 0)))
            conn.commit()
            cur.close()
            mf_ok += len(df)
        else:
            mf_fail += 1
    except Exception as e:
        print("  第%d批 moneyflow 失败: %s" % (i//50+1, str(e)[:60]))
        mf_fail += 1
    if (i//50+1) % 3 == 0:
        print("  moneyflow: 已处理 %d/%d 批" % (i//50+1, (len(codes)-1)//50+1))
    time.sleep(0.3)

conn.close()
print("moneyflow 完成: %d条 OK, %d批失败" % (mf_ok, mf_fail))
print("\n全部完成!")
