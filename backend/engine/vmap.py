"""
V型映射 + 信号判定
=================
V型映射公式 + momentum/reversion 双策略信号判定
纯计算，仅依赖 dataclass 结构。
"""

from dataclasses import dataclass
from typing import Dict, Any


@dataclass
class SignalResult:
    signal: str    # STRONG_BUY | BUY | CAUTIOUS_BUY | HOLD | SELL | REV_BUY | WAIT
    label: str     # 🟢强烈买入 等


def vmap_score(raw: float, center: float = 25.0) -> float:
    """
    V型映射：将[-100,+100]的raw映射到[0,100]
    center=25 表示评分中心在25分附近，极端值被放大到两端
    """
    dist = abs(raw - center)
    return round(min(100.0, max(0.0, dist * (100.0 / (100.0 - center)))), 1)


def classify_signal(v_score: float, strategy: str, chanlun: Dict[str, Any]) -> SignalResult:
    """
    双策略信号判定
    - momentum: 趋势越强信号越积极
    - reversion: 评分越高=反弹窗口(REV_BUY), 评分中等=WAIT
    """
    trend = chanlun.get('trend', 50)

    if strategy == 'momentum':
        if v_score >= 42 and trend >= 85:
            return SignalResult('STRONG_BUY', '🟢强烈买入')
        elif v_score >= 38 and trend >= 80:
            return SignalResult('BUY', '🟢买入')
        elif v_score >= 34 and trend >= 75:
            return SignalResult('CAUTIOUS_BUY', '🟡谨慎买入')
        elif v_score >= 20:
            return SignalResult('HOLD', '⏸️持有')
        elif v_score >= 12:
            return SignalResult('SELL', '🔴卖出')
        else:
            return SignalResult('SELL', '⛔清仓')
    else:
        # reversion: 严格仅秋冬/熊市启用
        if v_score >= 50 and trend <= 40:
            return SignalResult('REV_BUY', '🟣反转买入')
        elif v_score >= 35 and trend <= 50:
            return SignalResult('REV_BUY', '🟣关注反转')
        elif v_score >= 20:
            return SignalResult('WAIT', '⏳等待')
        else:
            return SignalResult('SELL', '🔴卖出')
