"""
L1 周期阶段评分 —— 优化1
========================
季节×regime 基础分 + 板块周期增强
"""

from dataclasses import dataclass
from typing import List
from .indicators import sma


@dataclass
class CycleResult:
    score: float         # 0-100
    strategy: str        # 'momentum' | 'reversion'
    sector_boost: float  # 板块周期增强值


def score_cycle_enhanced(
    season: str,
    regime: str,
    market_score: float,
    industry: str,
    closes: List[float],
) -> CycleResult:
    """
    v4.0 增强:
    - 基础季节打分(季节×regime)
    - 板块周期特征: 行业指数相对位置(价格在年线位置)
    - 个股周期特征: 价格在120日均线位置
    """
    # 基础季节分
    base = {
        'spring': 75, 'summer': 60, 'autumn': 30, 'winter': 15,
        'panic': 10, 'recovery': 60,
    }.get(season, 40)

    if regime == 'bull':
        base = min(90, base + 10)
    elif regime == 'bear':
        base = max(10, base - 10)

    base += max(-10, min(10, market_score * 2))

    # 板块周期特征: 用价格在年线/半年线位置衡量
    sector_boost = 0.0
    if len(closes) >= 120:
        ma120 = sma(closes, 120)
        close = closes[-1]
        pos120 = (close - ma120) / ma120 if ma120 > 0 else 0
        if pos120 > 0.15:
            sector_boost = 10
        elif pos120 > 0.05:
            sector_boost = 5
        elif pos120 < 0:
            sector_boost = -5
        elif pos120 < -0.1:
            sector_boost = -10

        if len(closes) >= 250:
            ma250 = sma(closes, 250)
            pos250 = (close - ma250) / ma250 if ma250 > 0 else 0
            if pos250 > 0.1:
                sector_boost += 5
            elif pos250 < 0:
                sector_boost -= 5

    base = max(0, min(100, base + sector_boost))

    # 策略判定: 从 season_state 表读取 scoring_strategy
    # fallback: 夏/春/混沌→momentum，秋/冬/恐慌→reversion
    strategy = 'momentum'
    try:
        import pymysql
        _pwd = ''
        with open('/etc/mysql/debian.cnf') as _f:
            for _l in _f:
                if 'password' in _l:
                    _pwd = _l.split('=')[-1].strip().strip('"').strip("'")
                    break
        _conn = pymysql.connect(host='127.0.0.1',port=3306,user='debian-sys-maint',
            password=_pwd,database='stock_db',charset='utf8mb4')
        _cu = _conn.cursor()
        _cu.execute("""
            SELECT scoring_strategy FROM season_state 
            WHERE index_code='MARKET' ORDER BY trade_date DESC LIMIT 1
        """)
        _r = _cu.fetchone()
        if _r and _r[0]:
            strategy = _r[0]
        _cu.close(); _conn.close()
    except:
        # fallback硬编码
        if season in ('autumn', 'winter', 'panic') or regime == 'bear' or market_score < -3:
            strategy = 'reversion'
        else:
            strategy = 'momentum'

    return CycleResult(
        score=round(base, 1),
        strategy=strategy,
        sector_boost=sector_boost,
    )
