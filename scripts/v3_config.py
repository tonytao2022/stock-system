#!/usr/bin/env python3
"""
V3策略固化配置（等同于DB strategy_config.id=9）
所有规则从DB读取，此处仅做文档记录
"""
V3_CONFIG = {
    'id': 9,
    'name': 'V3新规则(基线)',
    'buy_min_score': 70,          # 买入线70
    'p1_score': 60,               # P1门限降到60
    'p2_score': 30,
    'p3_score': 20,
    'stop_loss_pct': 8.00,         # 默认止损，实际按时间衰减
    'max_hold_days': 30,
    'cool_days': 20,
    'trailing_stop_pct': 15.00,
    'max_positions': 6,            # 最大同时持仓6只
    'max_pos_pct': 30,            # 最高单只仓位
    'p1_grace_days': 2,           # P1延判2天
    'sl_time_decay': [(5,5), (7,10), (8,999)],  # 止损时间衰减
    'cap_dynamic': {              # 评分动态仓位
        80: 0.30,   # ≥80分 → 30%
        65: 0.20,   # 65-79分 → 20%
        0: 0.00,    # <65 → 不买
    },
}
