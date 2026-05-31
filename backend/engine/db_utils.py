"""
数据层 — MySQL连接管理、K线加载、行业查询、市场上下文查询
=========================================================
所有 I/O 操作的唯一入口。
"""

import os
import pymysql
from typing import Dict, List, Optional


def _mysql_pass() -> str:
    """从 debian.cnf 或环境变量读取密码"""
    try:
        with open('/etc/mysql/debian.cnf') as f:
            for line in f:
                if 'password' in line:
                    return line.strip().split('=')[-1].strip().strip('"').strip("'")
    except Exception:
        pass
    return os.environ.get('MYSQL_PASSWORD', '')


DB_CONFIG: Dict[str, object] = {
    'host': '127.0.0.1',
    'port': 3306,
    'user': 'debian-sys-maint',
    'password': _mysql_pass(),
    'database': 'stock_db',
    'charset': 'utf8mb4',
}


def load_kline(conn, ts_code: str, lookback: int = 400) -> List[dict]:
    """加载单只股票K线"""
    cur = conn.cursor(pymysql.cursors.DictCursor)
    cur.execute(
        "SELECT trade_date,high,low,close,vol,change_pct "
        "FROM daily_kline_qfq WHERE ts_code=%s ORDER BY trade_date ASC",
        (ts_code,),
    )
    rows = cur.fetchall()
    cur.close()
    return rows[-lookback:] if len(rows) > lookback else rows


def get_industry(conn, ts_code: str) -> Optional[str]:
    """查询股票所属行业"""
    cur = conn.cursor(pymysql.cursors.DictCursor)
    cur.execute("SELECT industry FROM backtest_pool WHERE ts_code=%s", (ts_code,))
    r = cur.fetchone()
    ind = r['industry'] if r else None
    if not ind:
        cur.execute("SELECT industry FROM stock_basic WHERE ts_code=%s", (ts_code,))
        r2 = cur.fetchone()
        ind = r2['industry'] if r2 else None
    cur.close()
    return ind


def get_market_context(conn) -> Dict[str, object]:
    """查询市场季节/regime/宽度"""
    cur = conn.cursor(pymysql.cursors.DictCursor)

    # 季节状态
    cur.execute(
        "SELECT season, raw_score, confidence FROM season_state "
        "WHERE index_code='MARKET' ORDER BY trade_date DESC LIMIT 1"
    )
    mr = cur.fetchone()
    season = mr['season'] if mr else 'chaos'
    mkt_score = float(mr['raw_score'] or 0) if mr else 0.0
    conf = float(mr.get('confidence') or 0) if mr and mr.get('confidence') else 0.6
    if conf < 0.1:
        conf = 0.6

    # Regime: 300 指数
    cur.execute(
        "SELECT season, raw_score FROM season_state "
        "WHERE index_code='000300.SH' ORDER BY trade_date DESC LIMIT 1"
    )
    idx300 = cur.fetchone()
    if idx300 and float(idx300['raw_score'] or 0) > 3:
        regime = 'bull'
    elif idx300 and float(idx300['raw_score'] or 0) < -2:
        regime = 'bear'
    else:
        regime = 'range'

    # 市场宽度
    cur.execute("SELECT MAX(trade_date) AS d FROM daily_kline")
    ld = cur.fetchone()['d']
    cur.execute(
        "SELECT COUNT(*) AS total, SUM(CASE WHEN change_pct>0 THEN 1 ELSE 0 END) AS up "
        "FROM daily_kline WHERE trade_date=%s",
        (ld,),
    )
    br = cur.fetchone()
    breadth = br['up'] / br['total'] if br and br['total'] else 0.5

    cur.close()
    return {
        'season': season,
        'regime': regime,
        'market_score': mkt_score,
        'confidence': conf,
        'breadth_ratio': breadth,
    }


def get_pool_stocks(conn) -> List[str]:
    """查询 backtest_pool 活跃股票列表"""
    cur = conn.cursor(pymysql.cursors.DictCursor)
    cur.execute("SELECT ts_code, name FROM backtest_pool WHERE status='ACTIVE' AND market!='指数' ORDER BY ts_code")
    rows = cur.fetchall()
    cur.close()
    return [r['ts_code'] for r in rows]
