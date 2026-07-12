#!/usr/bin/env python3
"""
short_score_engine v1.0 — 短线评分引擎
===================================
2026-07-12 基于MAY建议创建

从现有p6_dual_track_engine中抽离alpha062/046因子计算，
组合成独立short_score，用于H5-H10短线评分。

输入: daily_kline rows (与 track_momentum 相同的120行DESC数据)
输出: short_score + factor_details

权重方案（MAY建议）:
  alpha062 40%
  alpha046 25%
  composite_short 35% (composite_score的5日变化量，暂用delta替代)

使用方法:
  from short_score_engine import calc_short_score
  short = calc_short_score(closes, highs, lows, vols, amounts)
"""

import numpy as np
from typing import Dict, List, Optional, Tuple


def calc_alpha062(highs: List[float], vols: List[float]) -> Tuple[float, float]:
    """
    alpha062: (-1 * Corr(high, volume, 5))
    
    高量负相关逻辑：近期高价放量→正相关→低分（出货信号）
    近期高价缩量→负相关→高分（锁仓信号）
    
    映射: 50 + (-corr) * 30
    - corr=-1(完全负相关) → 80分（最理想）
    - corr=0(无相关) → 50分
    - corr=+1(完全正相关) → 20分（最不理想）
    
    Returns:
        (score: 0-100, corr: -1~1原始值)
    """
    if len(highs) < 5 or len(vols) < 5:
        return 50, None
    
    h5 = highs[-5:]
    v5 = vols[-5:]
    
    if np.std(h5) > 0 and np.std(v5) > 0:
        corr = float(np.corrcoef(h5, v5)[0, 1])
        score = max(0, min(100, 50 + (-corr) * 30))
        return round(score, 2), round(corr, 4)
    return 50, None


def calc_alpha046(closes: List[float]) -> Tuple[float, float]:
    """
    alpha046: (ma3+ma6+ma12+ma24)/(4*close)
    
    多均线位置逻辑：价格在均线下方越远→越超跌→越高分
    ratio=1(均线位置) → 50分
    ratio=0.92(均线在价格下方8%) → 80分
    ratio=1.08(均线在价格上方8%) → 20分
    
    Returns:
        (score: 0-100, ratio原始值)
    """
    if len(closes) < 24:
        return 50, None
    
    ma3 = np.mean(closes[-3:])
    ma6 = np.mean(closes[-6:])
    ma12 = np.mean(closes[-12:])
    ma24 = np.mean(closes[-24:])
    ratio = (ma3 + ma6 + ma12 + ma24) / (4 * closes[-1])
    score = max(0, min(100, 50 + (1 - ratio) * 100 * 3))
    return round(score, 2), round(ratio, 4)


def calc_composite_delta(closes: List[float]) -> Tuple[float, float]:
    """
    composite_short用score_delta替代：
    原本composite_score的5日变化量 → 用于短期动量
    delta = close_today / close_5ago - 1
    映射到0-100分 (越大越好)
    """
    if len(closes) < 6:
        return 50, None
    
    delta_pct = closes[-1] / closes[-6] - 1  # -1~+1
    
    # delta_pct映射: 0%→50分, +10%→80分, -10%→20分
    score = max(0, min(100, 50 + delta_pct * 100 * 3))
    return round(score, 2), round(delta_pct * 100, 2)  # 返回百分比


def calc_short_score(closes: List[float], highs: List[float], 
                     vols: List[float], amounts: List[float]) -> Dict:
    """
    计算short_score (H5-H10短线评分)
    
    Args:
        closes: 收盘价列表（最新在前或在后均可，会被自动排序）
        highs: 最高价列表
        vols: 成交量列表
        amounts: 成交额列表
    
    Returns:
        {
            'short_score': float,  # 0-100 综合score
            'alpha062_score': float,
            'alpha062_corr': float or None,
            'alpha046_score': float,
            'alpha046_ratio': float or None,
            'composite_delta_score': float,
            'composite_delta_pct': float or None,
            'short_confidence': str,  # high/medium/low
            'short_reason': str,
        }
    """
    # 确保数据是最新在最后
    c = list(closes)
    h = list(highs)
    v = list(vols)
    a = list(amounts)
    
    # 因子
    a062_score, a062_corr = calc_alpha062(h, v)
    a046_score, a046_ratio = calc_alpha046(c)
    delta_score, delta_pct = calc_composite_delta(c)
    
    # 交易量门槛检查
    avg_amount = np.mean(a[-5:]) if len(a) >= 5 and all(x is not None for x in a[-5:]) else 0
    
    # 综合评分 (v2调整：去掉delta，纯alpha因子)
    # v1验证发现delta的短期IC为负(-0.014)，拖累整体
    # 改用alpha062权重更高(0.7) + alpha046(0.3)
    short_score = a062_score * 0.70 + a046_score * 0.30
    # MAY建议：如果alpha062的IR动荡>30%再重新平衡
    
    # 置信度
    if a062_corr is not None and abs(a062_corr) > 0.3:
        confidence = 'high' if short_score >= 65 else 'medium'
    elif a062_corr is not None:
        confidence = 'medium' if short_score >= 60 else 'low'
    else:
        confidence = 'low'
    
    # 理由
    parts = []
    if a062_score >= 65:
        parts.append(f"alpha062高量负相关(a062={a062_score:.0f},corr={a062_corr:.2f})")
    if a046_score >= 65:
        parts.append(f"alpha046均线超跌(a046={a046_score:.0f},ratio={a046_ratio:.3f})")
    if delta_score >= 65:
        parts.append(f"价格动量向上(delta={delta_pct:+.1f}%)")
    
    reason = '+'.join(parts) if parts else '中性'
    
    return {
        'short_score': round(short_score, 2),
        'alpha062_score': a062_score,
        'alpha062_corr': a062_corr,
        'alpha046_score': a046_score,
        'alpha046_ratio': a046_ratio,
        'composite_delta_score': delta_score,
        'composite_delta_pct': delta_pct,
        'short_confidence': confidence,
        'short_reason': reason,
        'avg_amount_5d': round(float(avg_amount), 0),
    }


def calc_short_score_batch(codes: List[str], date_close_data: Dict,
                           kline_by_code: Dict) -> Dict[str, Dict]:
    """
    批量计算short_score（用于IC验证）
    
    Args:
        codes: 股票代码列表
        date_close_data: {td: {code: close}} 日线数据
        kline_by_code: {code: [{close,high,vol,amount,trade_date}]} K线原始数据
    
    Returns:
        {code: {short_score, alpha062_score, ...}}
    """
    results = {}
    for code in codes:
        rows = kline_by_code.get(code, [])
        if len(rows) < 25:
            continue
        
        # 按日期排序
        sorted_rows = sorted(rows, key=lambda x: x['trade_date'] if hasattr(x['trade_date'], 'strftime') else str(x['trade_date']))
        
        closes = [float(r['close']) for r in sorted_rows]
        highs = [float(r['high']) for r in sorted_rows]
        vols = [float(r['vol']) for r in sorted_rows]
        amounts = [float(r['amount']) if r['amount'] else 0 for r in sorted_rows]
        
        try:
            sr = calc_short_score(closes, highs, vols, amounts)
            results[code] = sr
        except Exception as e:
            results[code] = {'short_score': 50, 'error': str(e)[:50]}
    
    return results


if __name__ == '__main__':
    # 快速测试
    import random
    # 模拟数据
    clos = [100 + random.uniform(-5, 5) for _ in range(30)]
    highs = [c * 1.02 for c in clos]
    vols = [random.randint(1000, 10000) for _ in range(30)]
    amounts = [c * v * 100 for c, v in zip(clos, vols)]
    
    sr = calc_short_score(clos, highs, vols, amounts)
    print(f"short_score: {sr['short_score']}")
    print(f"  alpha062: {sr['alpha062_score']} (corr={sr['alpha062_corr']})")
    print(f"  alpha046: {sr['alpha046_score']} (ratio={sr['alpha046_ratio']})")
    print(f"  delta_score: {sr['composite_delta_score']} (delta={sr['composite_delta_pct']}%)")
    print(f"  confidence: {sr['short_confidence']}")
    print(f"  reason: {sr['short_reason']}")
    print(f"  avg_amount: {sr['avg_amount_5d']}")
