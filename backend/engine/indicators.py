"""
指标计算层 —— 纯函数，无外部依赖，可独立测试
==============================================
sma / rsi / roc / stddev / atr
"""

import math
from typing import List


def sma(data: List[float], period: int) -> float:
    """简单移动平均"""
    if len(data) < period:
        return sum(data) / len(data) if data else 0.0
    return sum(data[-period:]) / period


def rsi(closes: List[float], period: int = 14) -> float:
    """RSI 相对强弱"""
    if len(closes) < period + 1:
        return 50.0
    gains = sum(max(0, closes[i] - closes[i - 1]) for i in range(-period, 0))
    losses = sum(max(0, closes[i - 1] - closes[i]) for i in range(-period, 0)) + 0.0001
    return 100.0 - 100.0 / (1.0 + gains / losses)


def roc(closes: List[float], period: int) -> float:
    """价格变动率"""
    if len(closes) <= period:
        return 0.0
    return (closes[-1] - closes[-period - 1]) / closes[-period - 1]


def stddev(data: List[float], period: int) -> float:
    """标准差"""
    if len(data) < period:
        return 0.0
    avg = sum(data[-period:]) / period
    return (sum((x - avg) ** 2 for x in data[-period:]) / period) ** 0.5


def atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> float:
    """平均真实波幅"""
    if len(closes) < period + 1:
        return 0.0
    tr_list = [
        max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        for i in range(-period, 0)
    ]
    return sum(tr_list) / period
