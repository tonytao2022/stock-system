#!/usr/bin/env python3
"""
补全资金流向历史数据（Tushare moneyflow API）
策略：按日期批量拉全市场（单次API拉5000只），效率最高
需要补的区间：2023-01-01 ~ 2026-05-17（已有数据的起始日前一天）
"""
import sys, os, time, json, math
from datetime import date, datetime, timedelta

sys.path.insert(0, '/opt/stock-analyzer')
from step_strategy_engine import _get_ts_token, get_conn
import warnings
warnings.filterwarnings("ignore")

LOG_FILE = '/tmp/fill_moneyflow.log'

def log(msg):
    ts = datetime.now().strftime('%H:%M:%S')
    line = f'[{ts}] {msg}'
    print(line, flush=True)
    with open(LOG_FILE, 'a') as f:
        f.write(line + '\n')

def get_existing_dates():
    """获取已有资金的交易日"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT trade_date FROM money_flow ORDER BY trade_date")
    existing = set(str(r[0]) for r in cur.fetchall())
    cur.close(); conn.close()
    return existing

def get_trade_dates():
    """获取监控池股票的交易日（从daily_kline取）"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT d.trade_date 
        FROM daily_kline d 
        INNER JOIN watch_pool w ON d.ts_code = w.ts_code
        WHERE w.is_active = 1
        AND d.trade_date >= '2023-01-01' AND d.trade_date <= '2026-06-09'
        ORDER BY d.trade_date
    """)
    dates = [str(r[0]) for r in cur.fetchall()]
    cur.close(); conn.close()
    return dates

def init_tushare():
    token = _get_ts_token()
    import tushare as ts
    ts.set_token(token)
    return ts.pro_api()

def fill_batch(pro, trade_date_str, tick=0.3):
    """单日全市场资金流向"""
    try:
        df = pro.moneyflow(trade_date=trade_date_str)
        if df is None or len(df) == 0:
            return 0
        return df
    except Exception as e:
        log(f'  ⚠️ {trade_date_str} 拉取失败: {str(e)[:60]}')
        return 0

def save_to_db(df, trade_date_str):
    """写入DB"""
    conn = get_conn()
    cur = conn.cursor()
    
    # 只用监控池+backtest_pool的股票，减少无效写入
    cur.execute("SELECT ts_code FROM watch_pool WHERE is_active=1")
    pool_codes = set(r[0] for r in cur.fetchall())
    cur.execute("SELECT ts_code FROM backtest_pool")
    for r in cur.fetchall():
        pool_codes.add(r[0])
    
    # 批量INSERT
    rows_to_insert = []
    for _, row in df.iterrows():
        code = row.get('ts_code', '')
        if code not in pool_codes:
            continue
        
        rows_to_insert.append((
            code, trade_date_str,
            safe(row.get('buy_sm_vol')),
            safe(row.get('buy_sm_amount')),
            safe(row.get('sell_sm_vol')),
            safe(row.get('sell_sm_amount')),
            safe(row.get('buy_md_vol')),
            safe(row.get('buy_md_amount')),
            safe(row.get('sell_md_vol')),
            safe(row.get('sell_md_amount')),
            safe(row.get('buy_lg_vol')),
            safe(row.get('buy_lg_amount')),
            safe(row.get('sell_lg_vol')),
            safe(row.get('sell_lg_amount')),
            safe(row.get('buy_elg_vol')),
            safe(row.get('buy_elg_amount')),
            safe(row.get('sell_elg_vol')),
            safe(row.get('sell_elg_amount')),
            safe(row.get('net_mf_vol')),
            safe(row.get('net_mf_amount')),
        ))
    
    if not rows_to_insert:
        return 0
    
    sql = """INSERT IGNORE INTO money_flow 
        (ts_code, trade_date, buy_sm_vol, buy_sm_amount, sell_sm_vol, sell_sm_amount,
         buy_md_vol, buy_md_amount, sell_md_vol, sell_md_amount,
         buy_lg_vol, buy_lg_amount, sell_lg_vol, sell_lg_amount,
         buy_elg_vol, buy_elg_amount, sell_elg_vol, sell_elg_amount,
         net_mf_vol, net_mf_amount)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"""
    
    # 分批写入，每批500行
    batch_size = 500
    total = 0
    for i in range(0, len(rows_to_insert), batch_size):
        batch = rows_to_insert[i:i+batch_size]
        try:
            cur.executemany(sql, batch)
            conn.commit()
            total += len(batch)
        except Exception as e:
            conn.rollback()
            log(f'  ⚠️ 写入失败({len(batch)}行): {str(e)[:60]}')
    
    cur.close()
    conn.close()
    return total

def safe(v):
    try:
        f = float(v or 0)
        return f if not (math.isnan(f) or math.isinf(f)) else 0.0
    except:
        return 0.0

def main():
    print('='*60)
    print(' 补全资金流向历史数据')
    print('='*60)
    
    # 初始化
    log('初始化 Tushare...')
    pro = init_tushare()
    
    # 获取已有日期和交易日
    existing = get_existing_dates()
    all_dates = get_trade_dates()
    
    # 需要补的日期
    need_dates = [d for d in all_dates if d not in existing]
    log(f'已有资金数据: {len(existing)}个交易日')
    log(f'需要补全: {len(need_dates)}个交易日')
    log(f'日期范围: {need_dates[0]} ~ {need_dates[-1]}')
    
    if not need_dates:
        log('✅ 数据已完整，无需补全')
        return
    
    t0 = time.time()
    total_rows = 0
    success_days = 0
    fail_days = 0
    
    for i, td in enumerate(need_dates):
        df = fill_batch(pro, td)
        if isinstance(df, int) and df == 0:
            fail_days += 1
            continue
        
        rows = save_to_db(df, td)
        total_rows += rows
        success_days += 1
        
        # Tushare 60次/分钟限制，每秒最多1次
        time.sleep(1.1)
        
        # 每10天报一次进度
        if (i + 1) % 10 == 0 or i == len(need_dates) - 1:
            elapsed = time.time() - t0
            pct = (i + 1) / len(need_dates) * 100
            rate = total_rows / elapsed if elapsed > 0 else 0
            log(f'  [{i+1}/{len(need_dates)}] {pct:.0f}% | 成功{success_days}天 失败{fail_days}天 | 写入{total_rows}行 | {rate:.0f}行/秒')
    
    total_elapsed = time.time() - t0
    log(f'')
    log(f'{"="*50}')
    log(f' 完成!')
    log(f' 补全: {success_days}个交易日')
    log(f' 写入: {total_rows}行资金数据')
    log(f' 失败: {fail_days}天')
    log(f' 耗时: {total_elapsed:.0f}s ({total_elapsed/60:.1f}分钟)')
    log(f'{"="*50}')

if __name__ == '__main__':
    main()
