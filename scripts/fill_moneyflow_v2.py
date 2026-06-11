#!/usr/bin/env python3
"""补全资金流向 - 轻量版，直接逐日补"""
import sys, os, time, math
sys.path.insert(0, '/opt/stock-analyzer')
from step_strategy_engine import _get_ts_token, get_conn
import warnings; warnings.filterwarnings("ignore")
import tushare as ts

token = _get_ts_token()
ts.set_token(token)
pro = ts.pro_api()

# 获取已有日期
conn = get_conn(); cur = conn.cursor()
cur.execute("SELECT DISTINCT trade_date FROM money_flow ORDER BY trade_date")
existing = set(str(r[0]) for r in cur.fetchall())

# 获取交易日
cur.execute("""
    SELECT DISTINCT d.trade_date 
    FROM daily_kline d
    INNER JOIN watch_pool w ON d.ts_code = w.ts_code
    WHERE w.is_active = 1 AND d.trade_date >= '2023-01-01'
    ORDER BY d.trade_date
""")
all_dates = [str(r[0]) for r in cur.fetchall()]
cur.close(); conn.close()

need_dates = [d for d in all_dates if d.replace('-','') not in [e.replace('-','') for e in existing]]
print(f'已有: {len(existing)}天, 需补: {len(need_dates)}天 ({need_dates[0]}~{need_dates[-1]})')

# 获取监控+回测池代码
conn = get_conn(); cur = conn.cursor()
cur.execute("SELECT ts_code FROM watch_pool WHERE is_active=1")
pool = set(r[0] for r in cur.fetchall())
cur.execute("SELECT ts_code FROM backtest_pool")
for r in cur.fetchall(): pool.add(r[0])
cur.close(); conn.close()

def safe(v):
    try: f = float(v or 0); return f if not (math.isnan(f) or math.isinf(f)) else 0.0
    except: return 0.0

def save_batch(rows):
    if not rows: return 0
    conn = get_conn(); cur = conn.cursor()
    sql = """INSERT IGNORE INTO money_flow 
        (ts_code,trade_date,buy_sm_vol,buy_sm_amount,sell_sm_vol,sell_sm_amount,
         buy_md_vol,buy_md_amount,sell_md_vol,sell_md_amount,
         buy_lg_vol,buy_lg_amount,sell_lg_vol,sell_lg_amount,
         buy_elg_vol,buy_elg_amount,sell_elg_vol,sell_elg_amount,
         net_mf_vol,net_mf_amount)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"""
    try:
        cur.executemany(sql, rows)
        conn.commit(); n = len(rows)
    except:
        conn.rollback(); n = 0
    cur.close(); conn.close()
    return n

t0 = time.time()
total = 0; ok = 0; fail = 0

for i, td in enumerate(need_dates):
    try:
        td_fmt = td.replace('-', '')
        df = pro.moneyflow(trade_date=td_fmt)
        if df is None or len(df) == 0:
            fail += 1
            time.sleep(1)
            continue
        rows = []
        for _, r in df.iterrows():
            c = r.get('ts_code','')
            if c not in pool: continue
            rows.append((c, td,
                safe(r.get('buy_sm_vol')), safe(r.get('buy_sm_amount')),
                safe(r.get('sell_sm_vol')), safe(r.get('sell_sm_amount')),
                safe(r.get('buy_md_vol')), safe(r.get('buy_md_amount')),
                safe(r.get('sell_md_vol')), safe(r.get('sell_md_amount')),
                safe(r.get('buy_lg_vol')), safe(r.get('buy_lg_amount')),
                safe(r.get('sell_lg_vol')), safe(r.get('sell_lg_amount')),
                safe(r.get('buy_elg_vol')), safe(r.get('buy_elg_amount')),
                safe(r.get('sell_elg_vol')), safe(r.get('sell_elg_amount')),
                safe(r.get('net_mf_vol')), safe(r.get('net_mf_amount'))))
        n = save_batch(rows)
        total += n; ok += 1
    except Exception as e:
        fail += 1
    
    if (i+1) % 20 == 0:
        el = time.time() - t0
        print(f'  [{i+1}/{len(need_dates)}] 成功{ok}天 失败{fail}天 写入{total}行 {el:.0f}s', flush=True)
    
    time.sleep(1.05)

el = time.time() - t0
print(f'\n完成! 补了{ok}天, 写入{total}行, 失败{fail}天, 耗时{el:.0f}s ({el/60:.1f}分钟)', flush=True)