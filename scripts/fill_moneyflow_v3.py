#!/usr/bin/env python3
"""
补全资金流向 - 按股票逐只拉取全部历史
效率：291只 × 1.5秒/只 ≈ 7分钟
"""
import sys, os, time, math
sys.path.insert(0, '/opt/stock-analyzer')
from step_strategy_engine import _get_ts_token, PWD, DB
import warnings; warnings.filterwarnings("ignore")
import tushare as ts

token = _get_ts_token()
ts.set_token(token)
pro = ts.pro_api()

DB['database'] = 'stock_db'
import pymysql

def safe(v):
    try: f = float(v or 0); return f if not (math.isnan(f) or math.isinf(f)) else 0.0
    except: return 0.0

# 获取监控池+回测池
conn = pymysql.connect(**DB); cur = conn.cursor()
cur.execute("SELECT DISTINCT ts_code FROM watch_pool WHERE is_active=1")
codes = [r[0] for r in cur.fetchall()]

# 查已有数据（按股票）
cur.execute("SELECT ts_code, COUNT(*) as cnt FROM money_flow GROUP BY ts_code")
existing = {r[0]: r[1] for r in cur.fetchall()}
cur.close(); conn.close()

# 看哪些需要补
need_codes = [c for c in codes if existing.get(c, 0) < 500]
print(f'监控池共{len(codes)}只, 已有数据的{len(existing)}只, 需补全的{len(need_codes)}只')

t0 = time.time()
total_rows = 0

for i, code in enumerate(need_codes):
    try:
        df = pro.moneyflow(ts_code=code, start_date='20230101', end_date='20260609')
        if df is None or len(df) == 0:
            if (i+1) % 30 == 0:
                print(f'  [{i+1}/{len(need_codes)}] {code} 无数据', flush=True)
            time.sleep(1.1)
            continue
        
        # 写入DB
        rows = []
        for _, r in df.iterrows():
            rows.append((
                code, str(r['trade_date']),
                safe(r.get('buy_sm_vol')), safe(r.get('buy_sm_amount')),
                safe(r.get('sell_sm_vol')), safe(r.get('sell_sm_amount')),
                safe(r.get('buy_md_vol')), safe(r.get('buy_md_amount')),
                safe(r.get('sell_md_vol')), safe(r.get('sell_md_amount')),
                safe(r.get('buy_lg_vol')), safe(r.get('buy_lg_amount')),
                safe(r.get('sell_lg_vol')), safe(r.get('sell_lg_amount')),
                safe(r.get('buy_elg_vol')), safe(r.get('buy_elg_amount')),
                safe(r.get('sell_elg_vol')), safe(r.get('sell_elg_amount')),
                safe(r.get('net_mf_vol')), safe(r.get('net_mf_amount'))))
        
        conn = pymysql.connect(**DB); cur = conn.cursor()
        sql = """INSERT IGNORE INTO money_flow 
            (ts_code,trade_date,buy_sm_vol,buy_sm_amount,sell_sm_vol,sell_sm_amount,
             buy_md_vol,buy_md_amount,sell_md_vol,sell_md_amount,
             buy_lg_vol,buy_lg_amount,sell_lg_vol,sell_lg_amount,
             buy_elg_vol,buy_elg_amount,sell_elg_vol,sell_elg_amount,
             net_mf_vol,net_mf_amount)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"""
        try:
            cur.executemany(sql, rows)
            conn.commit()
            n = len(rows)
        except:
            conn.rollback(); n = 0
        cur.close(); conn.close()
        total_rows += n
        
    except Exception as e:
        pass
    
    if (i+1) % 30 == 0:
        el = time.time()-t0
        print(f'  [{i+1}/{len(need_codes)}] 已写入{total_rows}行, 耗时{el:.0f}s', flush=True)
    
    time.sleep(1.1)

el = time.time()-t0
print(f'\n完成! 共写入{total_rows}行, 耗时{el:.0f}s ({el/60:.1f}分钟)', flush=True)

# 验证
conn = pymysql.connect(**DB); cur = conn.cursor()
cur.execute("SELECT COUNT(DISTINCT trade_date) as days, COUNT(*) as rows FROM money_flow")
r = cur.fetchone()
print(f'最终状态: {r[0]}天, {r[1]}行', flush=True)
cur.close(); conn.close()
