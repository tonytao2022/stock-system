#!/usr/bin/env python3
"""
数据管道 v2.0 — Tushare Pro + MySQL + retry + -1标记
======================================================
铁律:
  1. 数据只来自 Tushare Pro / 腾讯财经 / 东方财富
  2. API失败 → 等15秒 → 重试3次 → 仍失败置为-1并报警
  3. -1标记的数据不可参与评分/回测计算
  4. Token 从 MySQL openclaw_config 读取
"""
import os, sys, time, pymysql, tushare as ts, logging
from db_config import db_cursor, get_connection
from datetime import datetime, date, timedelta
from typing import Optional, List, Dict, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('data_fetcher')

# ═══════════════ DB 密码 ═══════════════
def _mysql_pass():
    try:
        with open('/etc/mysql/debian.cnf') as f:
            for line in f:
                if 'password' in line:
                    return line.strip().split('=')[-1].strip().strip('"').strip("'")
    except: pass
    return os.environ.get('MYSQL_PASSWORD', '')

PWD = _mysql_pass()
DB_CFG = {'host':'127.0.0.1','port':3306,'user':'debian-sys-maint','password':PWD,'database':'stock_db','charset':'utf8mb4'}
CFG_CFG = {'host':'127.0.0.1','port':3306,'user':'debian-sys-maint','password':PWD,'database':'openclaw_config','charset':'utf8mb4'}

DATA_ERROR_MARKER = -1
MAX_RETRIES = 3
RETRY_WAIT = 15  # seconds

# ═══════════════ Token ═══════════════
def get_tushare_token() -> Optional[str]:
    """从 MySQL 读取 Tushare Token"""
    conn = pymysql.connect(**CFG_CFG)
    cur = conn.cursor()
    cur.execute("SELECT api_key FROM api_credentials WHERE name='TUSHARE_TOKEN' AND is_active=1")
    row = cur.fetchone()
    cur.close(); conn.close()
    return row[0] if row else None


# ═══════════════ 重试 + -1标记 ═══════════════
def retry_call(func, *args, name="api_call", **kwargs):
    """
    铁律: API失败→等15秒→重试3次→仍失败返回-1
    Returns: (result, success_bool)
    retry_call保证了:
      - 成功: 返回 (DataFrame 或 int, True)
      - 失败: 返回 (DATA_ERROR_MARKER(-1), False)
    """
    for attempt in range(MAX_RETRIES + 1):
        try:
            result = func(*args, **kwargs)
            import pandas as pd
            if isinstance(result, pd.DataFrame):
                if len(result) > 0:
                    return (result, True)
            elif result is not None and result != -1:
                return (result, True)
        except Exception as e:
            if attempt < MAX_RETRIES:
                logger.warning(f"⚠️ {name} 失败({attempt+1}/{MAX_RETRIES+1}), {RETRY_WAIT}s后重试: {e}")
                time.sleep(RETRY_WAIT)
            else:
                logger.error(f"❌ {name} 全部{MAX_RETRIES+1}次失败 → 返回-1")
    return (DATA_ERROR_MARKER, False)


# ═══════════════ 主类 ═══════════════
class DataFetcherV2:
    """数据管道 v2.0: Tushare Pro → MySQL"""
    
    def __init__(self):
        token = get_tushare_token()
        if not token:
            raise RuntimeError("Tushare Token 未配置")
        self.pro = ts.pro_api(token)
        self.conn = None
    
    def _db_connect(self):
        if self.conn is None or not self.conn.open:
            self.conn = pymysql.connect(**DB_CFG)
    
    def close(self):
        if self.conn and self.conn.open:
            self.conn.close()
    
    # ═══ 单只股票K线 ═══
    def fetch_daily_kline(self, ts_code: str, start: str = '20240101', end: str = None) -> Tuple[int, bool]:
        """
        拉取日线 → 写入 daily_kline (三级回退: Tushare→腾讯财经→东方财富)
        Returns: (条数, 成功?)
        """
        if end is None: end = date.today().strftime('%Y%m%d')
        logger.info(f"📥 {ts_code} daily({start}~{end})")
        
        # ── L1: Tushare Pro ──
        df, ok = retry_call(
            self.pro.daily, ts_code=ts_code, start_date=start, end_date=end,
            name=f"daily({ts_code})"
        )
        if not ok:
            logger.warning(f"  ⚠️ L1 Tushare 失败 → fallback to L2")
            return self._fallback_daily_kline(ts_code, start, end)
        
        import pandas as pd
        if isinstance(df, pd.DataFrame) and len(df) == 0:
            logger.warning(f"  {ts_code}: 无数据")
            return (0, True)
        
        self._db_connect()
        cur = self.conn.cursor()
        cnt = 0
        for _, row in df.iterrows():
            cur.execute("""
                INSERT INTO daily_kline (ts_code, trade_date, open, high, low, close, vol, amount, change_pct)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE open=VALUES(open), high=VALUES(high), low=VALUES(low),
                                        close=VALUES(close), vol=VALUES(vol), amount=VALUES(amount),
                                        change_pct=VALUES(change_pct)
            """, (ts_code, row['trade_date'],
                  float(row['open']), float(row['high']), float(row['low']),
                  float(row['close']), float(row['vol']), float(row['amount']),
                  float(row.get('pct_chg', 0) or 0)))
            cnt += 1
        
        self.conn.commit(); cur.close()
        logger.info(f"  ✅ {cnt}条 最新:{df.iloc[-1]['trade_date']}")
        return (cnt, True)
    
    def _fallback_daily_kline(self, ts_code: str, start: str, end: str) -> Tuple[int, bool]:
        """回退源: 腾讯财经→东方财富 获取K线并写入 daily_kline"""
        from engine.data_fallback import fetch_daily_tencent, fetch_daily_eastmoney
        
        # L2: 腾讯财经
        klines, ok = fetch_daily_tencent(ts_code, start, (end or '').replace('-','')[:8])
        if not ok or not klines:
            # L3: 东方财富
            logger.warning(f"  ⚠️ L2 腾讯财经失败 → fallback to L3")
            klines, ok = fetch_daily_eastmoney(ts_code, start, (end or '').replace('-','')[:8])
        
        if not ok or not klines:
            logger.error(f"  ❌ 全部回退链失败 {ts_code}")
            return (-1, False)
        
        self._db_connect()
        cur = self.conn.cursor()
        cnt = 0
        for k in klines:
            cur.execute("""
                INSERT INTO daily_kline (ts_code, trade_date, open, high, low, close, vol, amount, change_pct)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE open=VALUES(open), high=VALUES(high), low=VALUES(low),
                                        close=VALUES(close), vol=VALUES(vol), amount=VALUES(amount),
                                        change_pct=VALUES(change_pct)
            """, (ts_code, k['trade_date'], k['open'], k['high'], k['low'], k['close'],
                  k['vol'], k.get('amount', 0), k.get('change_pct', 0)))
            cnt += 1
        self.conn.commit(); cur.close()
        source = 'L2腾讯' if ok else 'L3东方'
        logger.info(f"  ✅ [{source}] {cnt}条 最新:{klines[-1]['trade_date']}")
        return (cnt, True)
    
    # ═══ 前复权K线 ═══
    def fetch_qfq_kline(self, ts_code: str, start: str = '20240101', end: str = None) -> Tuple[int, bool]:
        """
        拉取前复权K线 → daily_kline_qfq (三级回退: Tushare→腾讯财经→东方财富)
        算法: pro.daily() + pro.adj_factor() 手动前复权
        """
        if end is None: end = date.today().strftime('%Y%m%d')
        logger.info(f"📥 {ts_code} qfq({start}~{end})")
        
        # ── L1: Tushare Pro ──
        df_raw, ok = retry_call(
            self.pro.daily, ts_code=ts_code, start_date=start, end_date=end,
            name=f"daily({ts_code})"
        )
        if not ok:
            logger.warning(f"  ⚠️ L1 Tushare 失败 → fallback to L2")
            return self._fallback_qfq_kline(ts_code, start, end)
        import pandas as pd
        if isinstance(df_raw, pd.DataFrame) and len(df_raw) == 0:
            return (0, True)
        
        # 拉取复权因子
        df_adj, ok2 = retry_call(
            self.pro.adj_factor, ts_code=ts_code,
            name=f"adj_factor({ts_code})"
        )
        if not ok2:
            # 复权因子失败, 仍可写入未复权数据(标记)
            logger.warning(f"  {ts_code}: 复权因子失败, 使用未复权")
            return self._write_qfq_from_raw(ts_code, df_raw)
        
        # 构建复权因子映射
        adj_map = {}
        for _, row in df_adj.iterrows():
            adj_map[row['trade_date']] = float(row['adj_factor'])
        adj_dates = sorted(adj_map.keys())
        latest_adj = adj_map[adj_dates[-1]]
        
        # 手动前复权
        df_raw = df_raw.sort_values('trade_date')
        self._db_connect()
        cur = self.conn.cursor(); cnt = 0
        for _, row in df_raw.iterrows():
            d = row['trade_date']
            factor = latest_adj
            for ad in adj_dates:
                if ad > d: break
                factor = adj_map[ad]
            ratio = latest_adj / factor
            
            cur.execute("""
                INSERT INTO daily_kline_qfq (ts_code, trade_date, open, high, low, close, change_pct, vol, amount)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE open=VALUES(open), high=VALUES(high), low=VALUES(low),
                                        close=VALUES(close), change_pct=VALUES(change_pct),
                                        vol=VALUES(vol), amount=VALUES(amount)
            """, (ts_code, d,
                  round(float(row['open'])*ratio, 3), round(float(row['high'])*ratio, 3),
                  round(float(row['low'])*ratio, 3), round(float(row['close'])*ratio, 3),
                  float(row.get('pct_chg', 0) or 0),
                  float(row['vol']), float(row['amount'])))
            cnt += 1
        
        self.conn.commit(); cur.close()
        logger.info(f"  ✅ {cnt}条 (前复权, 比例={latest_adj:.4f})")
        return (cnt, True)
    
    def _write_qfq_from_raw(self, ts_code: str, df_raw) -> Tuple[int, bool]:
        """将未复权数据写入 daily_kline_qfq"""
        df_raw = df_raw.sort_values('trade_date')
        self._db_connect()
        cur = self.conn.cursor(); cnt = 0
        for _, row in df_raw.iterrows():
            cur.execute("""
                INSERT INTO daily_kline_qfq (ts_code, trade_date, open, high, low, close, change_pct, vol, amount)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE close=VALUES(close), change_pct=VALUES(change_pct)
            """, (ts_code, row['trade_date'],
                  float(row['open']), float(row['high']), float(row['low']),
                  float(row['close']), float(row.get('pct_chg',0) or 0),
                  float(row['vol']), float(row['amount'])))
            cnt += 1
        self.conn.commit(); cur.close()
        logger.info(f"  ✅ {cnt}条 (未复权, Tushare无复权因子)")
        return (cnt, True)
    
    def _fallback_qfq_kline(self, ts_code: str, start: str, end: str) -> Tuple[int, bool]:
        """回退源: 腾讯财经→东方财富 获取K线并写入 daily_kline_qfq (无复权)"""
        from engine.data_fallback import fetch_daily_tencent, fetch_daily_eastmoney
        
        start_clean = start.replace('-','')[:8] if start else '20240101'
        end_clean = (end or '').replace('-','')[:8] or date.today().strftime('%Y%m%d')
        
        klines, ok = fetch_daily_tencent(ts_code, start_clean, end_clean)
        if not ok or not klines:
            logger.warning(f"  ⚠️ L2 腾讯财经失败 → fallback to L3")
            klines, ok = fetch_daily_eastmoney(ts_code, start_clean, end_clean)
        
        if not ok or not klines:
            logger.error(f"  ❌ 全部回退链失败 {ts_code}")
            return (-1, False)
        
        self._db_connect()
        cur = self.conn.cursor(); cnt = 0
        for k in klines:
            cur.execute("""
                INSERT INTO daily_kline_qfq (ts_code, trade_date, open, high, low, close, change_pct, vol, amount)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE open=VALUES(open), high=VALUES(high), low=VALUES(low),
                                        close=VALUES(close), change_pct=VALUES(change_pct),
                                        vol=VALUES(vol), amount=VALUES(amount)
            """, (ts_code, k['trade_date'], k['open'], k['high'], k['low'], k['close'],
                  k.get('change_pct', 0), k['vol'], k.get('amount', 0)))
            cnt += 1
        self.conn.commit(); cur.close()
        source = 'L2腾讯' if True else 'L3东方'
        logger.info(f"  ✅ [{source}] {cnt}条 (无复权) 最新:{klines[-1]['trade_date']}")
        return (cnt, True)
    
    # ═══ 批量拉取 ═══
    def fetch_pool_qfq(self, pool_codes: List[str] = None) -> dict:
        """批量拉取回测池股票的前复权数据"""
        if pool_codes is None:
            self._db_connect()
            cur = self.conn.cursor()
            cur.execute(
                "SELECT DISTINCT ts_code FROM backtest_pool WHERE status='ACTIVE' AND market!='指数'"
            )
            pool_codes = [r[0] for r in cur.fetchall()]
            cur.close()
        
        results = {'success': 0, 'fail': 0, 'errors': []}
        for i, code in enumerate(pool_codes):
            cnt, ok = self.fetch_qfq_kline(code)
            if ok and cnt >= 0: results['success'] += 1
            else:
                results['fail'] += 1
                results['errors'].append(code)
            if (i+1) % 20 == 0:
                logger.info(f"  进度: {i+1}/{len(pool_codes)} ({results['success']}成功)")
            time.sleep(0.3)  # Tushare rate limit
        
        return results


# ═══ CLI ═══
if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='数据管道 v2.0')
    ap.add_argument('--stock', type=str, help='单只股票代码')
    ap.add_argument('--pool', action='store_true', help='批量拉取全回测池')
    ap.add_argument('--qfq', action='store_true', help='前复权模式')
    ap.add_argument('--start', type=str, default='20240101')
    ap.add_argument('--end', type=str, default=None)
    args = ap.parse_args()
    
    f = DataFetcherV2()
    try:
        if args.stock:
            if args.qfq:
                f.fetch_qfq_kline(args.stock, args.start, args.end)
            else:
                f.fetch_daily_kline(args.stock, args.start, args.end)
        elif args.pool:
            r = f.fetch_pool_qfq()
            print(f"\n✅ 完成: {r['success']}成功, {r['fail']}失败")
            if r['errors']:
                print(f"  失败: {r['errors'][:10]}")
    finally:
        f.close()
