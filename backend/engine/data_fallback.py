"""
数据源回退链 v1.0 — 腾讯财经 + 东方财富 fallback
==================================================
三级回退: Tushare Pro → 腾讯财经 → 东方财富

功能:
  1. fetch_daily_tencent(ts_code) — 腾讯财经 抓取日K线
  2. fetch_daily_eastmoney(ts_code) — 东方财富 抓取日K线
  3. fetch_with_fallback(ts_code, start, end) — 三级回退, 返回统一格式

输出格式:
  [{'trade_date':'2026-05-26','open':10.0,'high':10.5,'low':9.8,'close':10.2,'vol':100000,'amount':1e8,'change_pct':2.3}, ...]
"""
import re, json, time, logging
from typing import List, Dict, Optional, Tuple
from datetime import datetime, date

logger = logging.getLogger('data_fallback')

# 沪深市场映射
def _market_prefix(ts_code: str) -> str:
    """股票代码 → 市场前缀"""
    code = ts_code.split('.')[0]
    # 6开头沪市, 0/3开头深市, 688/4/8开头科创板
    if code.startswith('6') or code.startswith('9'):
        return 'sh'
    elif code.startswith('0') or code.startswith('3') or code.startswith('4') or code.startswith('8'):
        return 'sz'
    return 'sh'  # fallback


# ═══ 腾讯财经 ═══

def _format_date(d: str) -> str:
    """统一格式为 YYYY-MM-DD"""
    d = d.replace('-', '').replace('/', '').strip()
    if len(d) == 8:
        return f'{d[:4]}-{d[4:6]}-{d[6:8]}'
    return d[:10]


def fetch_daily_tencent(ts_code: str, start_date: str = '20240101',
                         end_date: str = None) -> Tuple[List[Dict], bool]:
    """
    腾讯财经日K线 (前复权) — 通过 fqkline 接口
    腾讯接口: https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sz002916,day,,,320,qfq
    """
    end_date = _format_date(end_date) if end_date else date.today().strftime('%Y-%m-%d')
    start_date = _format_date(start_date)

    import requests
    code_num = ts_code.split('.')[0]
    prefix = _market_prefix(ts_code)

    # 前复权K线接口: 取最近N天(最多足够)
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={prefix}{code_num},day,,,600,qfq"

    try:
        resp = requests.get(url, timeout=15, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        if resp.status_code != 200:
            logger.warning(f"  腾讯财经 {ts_code} HTTP {resp.status_code}")
            return ([], False)

        data = resp.json()
        if not data or data.get('code') != 0:
            return ([], False)

        # 解析K线 (前复权)
        klines = []
        code_data = data.get('data', {}).get(f'{prefix}{code_num}', {})
        qfqday = code_data.get('qfqday', [])
        day_data = qfqday if qfqday else code_data.get('day', [])

        if not day_data:
            return ([], False)

        for row in day_data:
            if not isinstance(row, (list, tuple)) or len(row) < 6:
                continue

            k_date = _format_date(str(row[0]))
            k_open = float(row[1] or 0)
            k_close = float(row[2] or 0)  # 腾讯fqkline: [date, open, close, high, low, vol]
            k_high = float(row[3] or 0)
            k_low = float(row[4] or 0)
            k_vol = float(row[5] or 0)

            # 过滤日期
            if k_date < start_date:
                continue
            if k_date > end_date:
                continue

            # 涨跌幅
            k_change = 0.0
            if k_open > 0:
                k_change = (k_close - k_open) / k_open * 100

            klines.append({
                'trade_date': k_date,
                'open': round(k_open, 3),
                'high': round(k_high, 3),
                'low': round(k_low, 3),
                'close': round(k_close, 3),
                'vol': int(k_vol),  # 腾讯单位是股
                'amount': 0,
                'change_pct': round(k_change, 3),
            })

        if not klines:
            return ([], False)

        klines.sort(key=lambda x: x['trade_date'])
        logger.info(f"  腾讯财经 {ts_code}: {len(klines)}条 (前复权, 日期{klines[0]['trade_date']}~{klines[-1]['trade_date']})")
        return (klines, True)

    except Exception as e:
        logger.warning(f"  腾讯财经 {ts_code} 异常: {e}")
        return ([], False)


# ═══ 东方财富 ═══

def fetch_daily_eastmoney(ts_code: str, start_date: str = '20240101',
                           end_date: str = None) -> Tuple[List[Dict], bool]:
    """
    东方财富日K线 (第三回退)
    接口: https://push2.eastmoney.com/api/qt/stock/kline/get
    参数: secid=市场.代码, klt=101(日线), fqt=1(前复权), lmt=数量, end=结束日
    """
    end_date = _format_date(end_date) if end_date else date.today().strftime('%Y-%m-%d')
    start_date = _format_date(start_date)

    import requests
    code_num = ts_code.split('.')[0]
    secid = '1.' + code_num if code_num.startswith(('6', '9')) else '0.' + code_num
    end_ymd = end_date.replace('-', '')

    url = ("https://push2.eastmoney.com/api/qt/stock/kline/get"
           f"?secid={secid}&fields1=f1,f2,f3,f4,f5,f6"
           f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
           f"&klt=101&fqt=1&end={end_ymd}&lmt=500")

    try:
        resp = requests.get(url, timeout=10, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://quote.eastmoney.com/',
        })
        if resp.status_code != 200:
            logger.warning(f"  东方财富 {ts_code} HTTP {resp.status_code}")
            return ([], False)

        data = resp.json()
        if not data or data.get('code') != 0:
            return ([], False)

        klines_raw = data.get('data', {}).get('klines', [])
        if not klines_raw:
            return ([], False)

        klines = []
        for item in klines_raw:
            parts = item.split(',')
            if len(parts) < 11:
                continue
            k_date = _format_date(parts[0])
            if k_date < start_date:
                continue

            klines.append({
                'trade_date': k_date,
                'open': float(parts[1] or 0),
                'close': float(parts[2] or 0),
                'high': float(parts[3] or 0),
                'low': float(parts[4] or 0),
                'vol': int(float(parts[5] or 0)),
                'amount': float(parts[6] or 0),
                'change_pct': float(parts[8] or 0),
            })

        if not klines:
            return ([], False)

        klines.sort(key=lambda x: x['trade_date'])
        logger.info(f"  东方财富 {ts_code}: {len(klines)}条 (日期{klines[0]['trade_date']}~{klines[-1]['trade_date']})")
        return (klines, True)

    except Exception as e:
        logger.warning(f"  东方财富 {ts_code} 异常: {e}")
        return ([], False)


# ═══ 三级回退主入口 ═══

def fetch_with_fallback(ts_code: str, tushare_func=None,
                         start_date: str = '20240101',
                         end_date: str = None) -> List[Dict]:
    """
    三级回退: Tushare → 腾讯财经 → 东方财富

    tushare_func: callback function, 返回 List[Dict] 或 (-1, False)
                  调用方式: tushare_func(ts_code, start, end) → (result, ok)

    返回: 统一格式的K线列表 [{'trade_date','open','high','low','close','vol','change_pct'}, ...]
          全部失败则返回 None
    """
    # 第一级: Tushare
    if tushare_func:
        logger.info(f"📡 [L1] Tushare {ts_code} ...")
        try:
            result, ok = tushare_func(ts_code, start_date, end_date)
            if ok and result not in (None, -1) and (isinstance(result, list) and len(result) > 0):
                logger.info(f"  ✅ L1 Tushare 成功: {len(result)}条")
                return result
            if ok and isinstance(result, int) and result > 0:
                # tushare_func 可能返回 (int条数, bool)
                # 需要从数据库读取最新数据
                logger.info(f"  ✅ L1 Tushare 已写入 {result}条")
                return None  # 表示数据已通过tushare写入，需调用方去数据库读取
        except Exception as e:
            logger.warning(f"  ⚠️ L1 Tushare 失败: {e}")
    else:
        logger.info(f"  ⚠️ L1 Tushare 无回调函数, 跳过")

    # 第二级: 腾讯财经
    logger.info(f"📡 [L2] 腾讯财经 {ts_code} ...")
    klines, ok = fetch_daily_tencent(ts_code, start_date, end_date)
    if ok and klines:
        logger.info(f"  ✅ L2 腾讯财经 成功: {len(klines)}条")
        return klines

    # 第三级: 东方财富
    logger.info(f"📡 [L3] 东方财富 {ts_code} ...")
    klines, ok = fetch_daily_eastmoney(ts_code, start_date, end_date)
    if ok and klines:
        logger.info(f"  ✅ L3 东方财富 成功: {len(klines)}条")
        return klines

    logger.error(f"❌ 三级回退全部失败 {ts_code}")
    return None
