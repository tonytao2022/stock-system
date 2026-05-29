# -*- coding: utf-8 -*-
"""
阶梯动态持有策略 API endpoint
注册路径: /api/v1/strategy
"""

import sys, os
sys.path.insert(0, '/opt/stock-analyzer')

from fastapi import APIRouter, Query
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from datetime import date
from step_strategy_engine import get_strategy_results, run_daily

router = APIRouter()


@router.get("/config/{strategy_id}")
def get_strategy_config(strategy_id: int = 1):
    """获取策略配置"""
    import pymysql
    from collections import defaultdict
    
    def get_pwd():
        with open('/etc/mysql/debian.cnf') as f:
            for l in f:
                if 'password' in l:
                    return l.split('=')[-1].strip().strip('"').strip("'")
        return ''
    
    conn = pymysql.connect(host='127.0.0.1', port=3306, user='debian-sys-maint',
                           password=get_pwd(), database='stock_db', charset='utf8mb4')
    cur = conn.cursor(pymysql.cursors.DictCursor)
    cur.execute("SELECT * FROM strategy_config WHERE id=%s", (strategy_id,))
    s = cur.fetchone()
    cur.close(); conn.close()
    
    if not s:
        return {'error': 'Strategy not found'}
    
    return {
        'id': s['id'],
        'name': s['name'],
        'description': s['description'],
        'strategy_type': s['strategy_type'],
        'params': {
            'buy_min_score': s['buy_min_score'],
            'p1_score': s['p1_score'],
            'p2_score': s['p2_score'],
            'p3_score': s['p3_score'],
            'stop_loss_pct': float(s['stop_loss_pct']),
            'max_hold_days': s['max_hold_days'],
            'cool_days': s['cool_days'],
        },
        'is_active': bool(s['is_active']),
        'created_at': str(s['created_at']),
    }


@router.get("/signals/{trade_date}")
def get_strategy_signals(
    trade_date: str,
    strategy_id: int = Query(1, description="策略ID"),
):
    """获取某日策略信号"""
    result = get_strategy_results(trade_date, strategy_id)
    return result


@router.post("/run")
def run_strategy_evaluation(
    trade_date: Optional[str] = Query(None, description="评估日期，默认今日"),
):
    """手动触发策略评估"""
    from step_strategy_engine import run_daily
    run_daily(trade_date)
    return {'message': 'OK', 'trade_date': trade_date or str(date.today())}


@router.get("/holdings/actions")
def get_current_holdings_actions():
    """获取当前持仓的下一个交易日买卖建议"""
    import pymysql
    from collections import defaultdict
    
    def get_pwd():
        with open('/etc/mysql/debian.cnf') as f:
            for l in f:
                if 'password' in l:
                    return l.split('=')[-1].strip().strip('"').strip("'")
        return ''
    
    td = str(date.today())
    
    conn = pymysql.connect(host='127.0.0.1', port=3306, user='debian-sys-maint',
                           password=get_pwd(), database='stock_db', charset='utf8mb4')
    cur = conn.cursor(pymysql.cursors.DictCursor)
    
    # 取最新日期的评估结果（策略ID=1）
    cur.execute("""
        SELECT MAX(ssd.trade_date) as latest
        FROM strategy_signal_daily ssd
        WHERE ssd.strategy_id=1
    """)
    latest_row = cur.fetchone()
    latest_date = str(latest_row['latest']) if latest_row and latest_row['latest'] else td
    
    cur.execute("""
        SELECT ssd.*, sb.name as stock_name
        FROM strategy_signal_daily ssd
        LEFT JOIN stock_basic sb ON ssd.ts_code = sb.ts_code
        WHERE ssd.strategy_id=1 AND ssd.trade_date=%s
          AND ssd.holding_status='HOLDING'
        ORDER BY 
          CASE ssd.action 
            WHEN 'STOP_LOSS' THEN 0
            WHEN 'SELL' THEN 1
            WHEN 'HOLD' THEN 2
            WHEN 'BUY' THEN 3
            ELSE 4
          END,
          COALESCE(ssd.buy_score, 0) DESC
    """, (latest_date,))
    
    signals = []
    for s in cur.fetchall():
        signals.append({
            'ts_code': s['ts_code'],
            'stock_name': s['stock_name'] or s['ts_code'],
            'buy_score': float(s['buy_score']) if s['buy_score'] else 0,
            'hold_days': s['hold_days'],
            'current_checkpoint': s['current_checkpoint'],
            'days_to_check': s['days_to_check'],
            'cost_price': float(s['cost_price']) if s['cost_price'] else 0,
            'current_price': float(s['current_price_r']) if s['current_price_r'] else 0,
            'profit_pct': float(s['profit_pct']) if s['profit_pct'] else 0,
            'drawdown_pct': float(s['drawdown_pct']) if s['drawdown_pct'] else 0,
            'checkpoint_passed': bool(s['checkpoint_passed']) if s['checkpoint_passed'] is not None else None,
            'hit_stop_loss': bool(s['hit_stop_loss']),
            'action': s['action'],
            'action_reason': s['action_reason'],
        })
    
    # 统计
    actions = defaultdict(int)
    for s in signals:
        actions[s['action']] += 1
    
    cur.close(); conn.close()
    
    return {
        'trade_date': latest_date,
        'total_holdings': len(signals),
        'action_summary': dict(actions),
        'signals': signals,
    }
