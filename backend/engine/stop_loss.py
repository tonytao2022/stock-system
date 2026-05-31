"""
ATR动态止损计算 —— 优化4
========================
基于ATR+策略+仓位的三级止损计算
"""

from typing import List
from .indicators import atr


def calc_stop_loss(
    closes: List[float],
    highs: List[float],
    lows: List[float],
    position_pct: float,
    strategy: str,
) -> float:
    """
    基于ATR的动态止损线

    Args:
        closes: 收盘价序列
        highs: 最高价序列
        lows: 最低价序列
        position_pct: 仓位百分比(0-100)
        strategy: 'momentum' | 'reversion'

    Returns:
        止损比例（如 -0.03 = -3%）
    """
    n = len(closes)
    if n < 20:
        return -0.05

    atr14 = atr(highs, lows, closes, 14)
    close = closes[-1]
    if close <= 0:
        return -0.05

    atr_pct = atr14 / close

    # 基准: -1.5×ATR
    stop = -atr_pct * 1.5

    # momentum策略: 宽止损（追涨容忍回调）
    if strategy == 'momentum':
        stop = -atr_pct * 2.0
    # reversion策略: 紧止损（抄底空间有限，及时离场）
    elif strategy == 'reversion':
        stop = -atr_pct * 1.2

    # 仓位越高止损越紧
    if position_pct > 60:
        stop = max(stop, -0.03)
    elif position_pct > 40:
        stop = max(stop, -0.05)
    else:
        stop = max(stop, -0.10)

    return round(stop, 4)
