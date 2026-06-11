# -*- coding: utf-8 -*-
"""
每日操作建议 API
"""
from fastapi import APIRouter, Depends
from typing import Dict, Any, List, Optional
from datetime import date, timedelta
from collections import defaultdict
import json, math, sys, os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

router = APIRouter()

DAYS_CN = ['周一','周二','周三','周四','周五','周六','周日']
SEASON_MAP = {
    'summer': '☀️ 夏季(持有)', 'chaos_spring': '🌤️ 弱春(偏多)', 'spring': '🌸 春季(进攻)',
    'chaos': '🌪️ 混沌(观望)', 'chaos_autumn': '🌥️ 弱秋(偏空)', 'autumn': '🍂 秋季(防守)', 'winter': '❄️ 冬季(休眠)'
}
SEASON_EMOJI = {'summer': '☀️', 'chaos_spring': '🌤️', 'chaos': '🌪️', 'chaos_autumn': '🌥️', 'autumn': '🍂', 'winter': '❄️', 'spring': '🌸'}


def get_pwd():
    import re
    try:
        with open('/etc/mysql/debian.cnf') as f:
            for l in f:
                if 'password' in l:
                    return l.split('=')[-1].strip().strip('"').strip("'")
    except: pass
    return os.environ.get('MYSQL_PASS', '')


PWD = get_pwd()

def get_conn():
    import pymysql
    return pymysql.connect(host='127.0.0.1', port=3306, user='debian-sys-maint',
                           password=*** charset='utf8mb4',
                           cursorclass=pymysql.cursors.DictCursor)


@router.get("/advice/today")
async def get_today_advice():
    """获取今日操作建议"""
    conn = get_conn()
    cur = conn.cursor()
    
    # 1. 获取最新季节
    cur.execute("""
        SELECT season, hengjiyuan_level, raw_score, confidence, regime, scoring_strategy
        FROM season_state WHERE index_code='MARKET'
        ORDER BY trade_date DESC LIMIT 1
    """)
    r = cur.fetchone()
    
    season_data = {'season': 'chaos', 'hengji': 'weak_heng', 'raw_score': 0, 'confidence': 0.5, 'regime': 'range', 'scoring_strategy': 'momentum'}
    if r:
        season_data = {
            'season': r['season'], 'hengji': r['hengjiyuan_level'] or 'weak_heng',
            'raw_score': float(r['raw_score'] or 0), 'confidence': float(r['confidence'] or 0.5),
            'regime': r['regime'] or 'range', 'scoring_strategy': r['scoring_strategy'] or 'momentum'
        }
    
    season = season_data['season']
    
    # 2. 获取对应策略参数
    cur.execute("""SELECT buy_min_score, max_pos_pct, stop_loss_pct, trailing_stop_pct,
                   cool_days, max_hold_days, p1_score, p2_score, p3_score
                   FROM strategy_config WHERE season_type=%s AND is_active=1 LIMIT 1""", (season,))
    sp = cur.fetchone()
    
    if not sp:
        cur.execute("""SELECT buy_min_score, max_pos_pct, stop_loss_pct, trailing_stop_pct,
                       cool_days, max_hold_days, p1_score, p2_score, p3_score
                       FROM strategy_config WHERE season_type='ALL' AND is_active=1 LIMIT 1""")
        sp = cur.fetchone()
    
    params = {}
    if sp:
        params = {
            'threshold': int(sp['buy_min_score']), 'max_pos_pct': int(sp['max_pos_pct']),
            'stop_loss': float(sp['stop_loss_pct']), 'trailing_stop': float(sp['trailing_stop_pct']),
            'cool_days': int(sp['cool_days']), 'max_hold': int(sp['max_hold_days']),
            'p1': int(sp['p1_score']), 'p2': int(sp['p2_score']), 'p3': int(sp['p3_score']),
        }
    
    # 3. 最新交易日
    cur.execute("SELECT MAX(trade_date) as td FROM strategy_signal WHERE trade_date <= CURDATE()")
    trade_date = str(cur.fetchone()['td'])
    
    # 4. 持仓
    cur.execute("""SELECT ts_code, name, qty, cost_price, current_price, profit_pct, buy_date, status
                   FROM portfolio_holdings WHERE status='HOLDING'""")
    holdings_rows = cur.fetchall()
    
    holdings = []
    for h in holdings_rows:
        code = h['ts_code']; name = h['name'] or ''; qty = int(h['qty'] or 0)
        cost = float(h['cost_price'] or 0); cur_price = float(h['current_price'] or 0)
        profit = float(h['profit_pct'] or 0); buy_date = str(h['buy_date'])
        
        # 评分
        cur.execute("""SELECT calibrated_score, composite_score FROM strategy_signal
                       WHERE ts_code=%s AND trade_date=%s LIMIT 1""", (code, trade_date))
        sr = cur.fetchone()
        score = float(sr['calibrated_score'] or sr['composite_score'] or 0) if sr else 0
        
        # 持有天数
        hold_days = 0
        if buy_date and buy_date != 'None':
            try: hold_days = (date.fromisoformat(trade_date) - date.fromisoformat(buy_date)).days
            except: pass
        
        # 建议
        advice, reason = generate_advice(code, score, profit, hold_days, params)
        
        holdings.append({
            'ts_code': code, 'name': name, 'qty': qty,
            'cost': round(cost, 2), 'current_price': round(cur_price, 2),
            'profit_pct': round(profit, 2), 'buy_date': buy_date,
            'hold_days': hold_days, 'current_score': round(score, 1),
            'advice': advice, 'reason': reason,
            'market_value': round(cur_price * qty, 2),
        })
    
    # 5. 买入候选
    th = params.get('threshold', 50)
    cur.execute("""SELECT ss.ts_code, sb.name, ss.calibrated_score, ss.composite_score,
                   ss.buy_sell_point, ss.direction
                   FROM strategy_signal ss
                   LEFT JOIN stock_basic sb ON ss.ts_code = sb.ts_code
                   WHERE ss.trade_date=%s AND ss.is_calculable=1 AND ss.gate_triggered=0
                   AND ss.calibrated_score >= %s
                   ORDER BY ss.calibrated_score DESC LIMIT 10""", (trade_date, th))
    
    holding_codes = set(h['ts_code'] for h in holdings)
    candidates = []
    for r in cur.fetchall():
        if r['ts_code'] in holding_codes: continue
        calib = float(r['calibrated_score'] or 0); comp = float(r['composite_score'] or 0)
        candidates.append({
            'ts_code': r['ts_code'], 'name': r['name'] or '',
            'score': max(calib, comp),
            'buy_point': r['buy_sell_point'] or '', 'direction': r['direction'] or '',
        })
        if len(candidates) >= 5: break
    
    cur.close()
    conn.close()
    
    return {
        'status': 'ok',
        'date': trade_date,
        'season': season,
        'season_label': SEASON_MAP.get(season, '🌪️ 混沌(观望)'),
        'hengji': season_data['hengji'],
        'raw_score': season_data['raw_score'],
        'confidence': season_data['confidence'],
        'regime': season_data['regime'],
        'scoring_strategy': season_data['scoring_strategy'],
        'params': params,
        'holdings': holdings,
        'candidates': candidates,
    }


def generate_advice(code, score, profit, hold_days, params):
    th = params.get('threshold', 50); p1 = params.get('p1', 40)
    sl = params.get('stop_loss', 12); ts = params.get('trailing_stop', 18)
    max_hold = params.get('max_hold', 30)
    
    if profit <= -sl:
        return '🛑 卖出', f'亏损{profit:.1f}%触发止损线-{sl:.0f}%，无条件平仓'
    if profit >= ts and hold_days >= 10:
        return '💰 考虑止盈', f'盈利{profit:.1f}%超过止盈线{ts:.0f}%，建议分批止盈'
    if hold_days >= max_hold:
        return '⏰ 到期卖出', f'持有{hold_days}日达上限{max_hold}日，建议卖出'
    if 5 <= hold_days <= 6 and score < p1:
        return '🔴 卖出', f'5日检视评分{score}<{p1}，不达标建议卖出'
    if 10 <= hold_days <= 11 and score < params.get('p2', 30):
        return '🔴 卖出', f'10日检视评分{score}<{params["p2"]}，不达标建议卖出'
    if 20 <= hold_days <= 21 and score < params.get('p3', 20):
        return '🔴 卖出', f'20日检视评分{score}<{params["p3"]}，不达标建议卖出'
    if profit > 0:
        return '🟢 持有', f'评分{score}已持有{hold_days}日，盈利{profit:.1f}%，趋势良好继续持有'
    else:
        return '🟡 观察', f'评分{score}已持有{hold_days}日，当前亏损{profit:.1f}%，观察等待反弹'
