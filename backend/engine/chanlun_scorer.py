"""
L2 缠论结构代理评分 —— 优化2
============================
趋势(40%) + 动量(35%) + 波动(15%) + 量能(10%) + 多周期背离检测
"""

from dataclasses import dataclass
from typing import List, Dict
from .indicators import sma, rsi, roc, stddev


@dataclass
class ChanlunResult:
    total: float           # 综合评分 0-100
    trend: float           # 趋势分
    momentum: float        # 动量分
    volatility: float      # 波动分
    volume: float          # 量能分
    chanlun_signal: float  # 缠论背离信号 -100~+100


def score_chanlun_enhanced(
    rows: List[dict],
    season: str,
    industry: str,
) -> ChanlunResult:
    """
    v4.0 缠论增强:
    - 优先从数据库读取chanlun_structure(buy_sell_point/structure_score/beichi)
    - 数据为空时用多周期背离代理: MACD背离 + RSI背离 + MA乖离
    """
    closes = [float(r['close']) for r in rows]
    highs = [float(r['high']) for r in rows]
    lows = [float(r['low']) for r in rows]
    vols = [float(r.get('vol', 0) or 0) for r in rows]
    n = len(closes)

    if n < 120:
        return ChanlunResult(
            total=50.0, trend=50.0, momentum=50.0,
            volatility=50.0, volume=50.0, chanlun_signal=0.0,
        )

    close = closes[-1]

    # ── 简化fallback: 不重复计算四因子，只做缠论微调 ──
    # 四因子由score_cycle_enhanced和主引擎计算，此处不重复
    trend_score = 50.0
    momentum_score = 50.0
    volatility_score = 50.0
    volume_score = 50.0

    # ── 缠论代理: 多周期背离检测 ──
    chanlun_signal = 0.0  # -100~+100: 负=超跌反弹窗口, 正=趋势延续

    # MACD金叉/死叉 (12/26/9)
    ema12 = sma(closes, 12)
    ema26 = sma(closes, 26)
    if n >= 35:
        old_ema12 = sma(closes[-9:-1], 12) if n >= 38 else ema12
        old_ema26 = sma(closes[-9:-1], 26) if n >= 38 else ema26
        if ema12 > ema26 and old_ema12 <= old_ema26:
            chanlun_signal += 15  # 金叉

    # RSI背离: 价格创新高但RSI未创新高=顶背离
    if n >= 40:
        h20_p = max(closes[-30:-10])
        r20_p = rsi(closes[-30:-10], 14)
        h20_n = max(closes[-10:])
        r20_n = rsi(closes[-10:], 14)
        if h20_n > h20_p and r20_n < r20_p - 5:
            chanlun_signal -= 20  # 顶背离
        l20_p = min(closes[-30:-10])
        r20_p2 = rsi(closes[-30:-10], 14)
        l20_n = min(closes[-10:])
        r20_n2 = rsi(closes[-10:], 14)
        if l20_n < l20_p and r20_n2 > r20_p2 + 5:
            chanlun_signal += 20  # 底背离

    # MA乖离: 价格远离MA20=超跌/超涨
    if close > 0 and n >= 20:
        ma20_dev = (close - ma20) / ma20
        if ma20_dev < -0.1:
            chanlun_signal += 15  # 深度超跌
        elif ma20_dev < -0.05:
            chanlun_signal += 8
        elif ma20_dev > 0.1:
            chanlun_signal -= 10  # 追高危险

    # 连续K线方向
    if n >= 5:
        cons_up = sum(1 for i in range(-4, 0) if closes[i] > closes[i - 1])
        cons_dn = sum(1 for i in range(-4, 0) if closes[i] < closes[i - 1])
        if cons_up >= 4:
            chanlun_signal += 10
        elif cons_dn >= 4:
            chanlun_signal -= 5

    chanlun_signal = max(-100, min(100, chanlun_signal))

    # 合成
    total = trend_score * 0.40 + momentum_score * 0.35 + volatility_score * 0.15 + volume_score * 0.10
    # 缠论信号修正: ±15分
    total += chanlun_signal * 0.15

    return ChanlunResult(
        total=round(max(0, min(100, total)), 1),
        trend=trend_score,
        momentum=momentum_score,
        volatility=volatility_score,
        volume=volume_score,
        chanlun_signal=chanlun_signal,
    )
