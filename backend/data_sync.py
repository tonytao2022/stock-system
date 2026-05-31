#!/usr/bin/env python3
"""
数据一致性同步器 v2.0 - 含回撤止损决策规则固化
"""
import os, sys, pymysql
from datetime import date, datetime

password = ''
with open('/etc/mysql/debian.cnf') as f:
    for l in f:
        if 'password' in l:
            password = l.split('=')[1].strip()
            break

conn = pymysql.connect(host='127.0.0.1', port=3306, user='debian-sys-maint', password='iXve1rVBXfdA4tL9', database='stock_db')
cur = conn.cursor()

log = []
def add_log(msg):
    ts = datetime.now().strftime('%H:%M:%S')
    print(f"[{ts}] {msg}")
    log.append(f"[{ts}] {msg}")

add_log("=" * 50)
add_log("数据一致性同步器 v2.0")
add_log("=" * 50)

# ─── Step 1: 获取最新交易日 ───
cur.execute("SELECT MAX(trade_date) FROM strategy_signal_daily WHERE strategy_id=1")
latest_day = cur.fetchone()[0]
if not latest_day:
    add_log("❌ strategy_signal_daily无数据，退出")
    exit(1)
add_log(f"最新交易日: {latest_day}")

# ─── Step 2: 确保持仓股票有策略信号 ───
add_log("\nStep 1: 同步持仓策略信号...")
cur.execute("SELECT ts_code, name, cost_price, current_price FROM portfolio_holdings WHERE status='HOLDING'")
holdings = {r[0]: {'name': r[1], 'cost_price': float(r[2] or 0), 'current_price': float(r[3] or 0)} for r in cur.fetchall()}
add_log(f"  持仓: {len(holdings)} 只")

for code, info in holdings.items():
    cur.execute("SELECT COUNT(*) FROM strategy_signal_daily WHERE ts_code=%s AND strategy_id=1 AND trade_date=%s", (code, latest_day))
    if cur.fetchone()[0] == 0:
        cur.execute("SELECT composite_score, structure_score FROM trend_score WHERE ts_code=%s AND trade_date<=%s ORDER BY trade_date DESC LIMIT 1", (code, latest_day))
        tr = cur.fetchone()
        if tr:
            sc = float(tr[0] or 0)
            cur.execute("INSERT INTO strategy_signal_daily (strategy_id, ts_code, trade_date, action, holding_status, buy_score, created_at) VALUES (1,%s,%s,%s,'HOLDING',%s,NOW()) ON DUPLICATE KEY UPDATE action=VALUES(action), buy_score=VALUES(buy_score), holding_status='HOLDING'",
                (code, latest_day, 'BUY' if sc >= 30 else 'WAIT', round(sc, 1)))

# ─── Step 3: 回撤止损决策规则固化 ───
add_log("\nStep 2: 回撤止损决策规则评估...")
cur.execute("""
    SELECT ssd.ts_code, ssd.action, ssd.buy_score, ssd.drawdown_pct, ssd.hold_days,
           ssd.peak_price, ssd.current_price_r, ssd.hit_stop_loss
    FROM strategy_signal_daily ssd
    WHERE ssd.strategy_id=1 AND ssd.trade_date=%s AND ssd.holding_status='HOLDING'
""", (latest_day,))

rules_applied = 0
for r in cur.fetchall():
    code = r[0]
    action = r[1]
    score = float(r[2]) if r[2] else 0
    dd = float(r[3]) if r[3] else 0
    hold_days = int(r[4]) if r[4] else 0
    peak_price = float(r[5]) if r[5] else 0
    cur_price = float(r[6]) if r[6] else 0
    hit_stop = int(r[7] or 0)

    name = holdings.get(code, {}).get('name', code)
    cost_price = holdings.get(code, {}).get('cost_price', 0)
    profit_pct = (cur_price - cost_price) / cost_price * 100 if cost_price > 0 else 0
    is_profit = profit_pct > 0  # 是否浮盈

    # 决策规则
    if hit_stop == 1:
        # 回撤止损触发时的决策
        if is_profit and score >= 30:
            new_action = 'HOLD'
            reason = f"回撤{dd:.1f}%触发止损，但浮盈{profit_pct:.1f}%+评分{score:.0f}，建议持有关注，放宽止损线至15%"
        elif is_profit and score < 30:
            new_action = 'REDUCE'
            reason = f"回撤{dd:.1f}%触发止损，浮盈{profit_pct:.1f}%但评分{score:.0f}<30，建议减半仓锁定利润"
        elif not is_profit and score >= 20:
            new_action = 'HOLD'
            reason = f"回撤{dd:.1f}%触发止损，亏损{profit_pct:.1f}%但评分{score:.0f}≥20，建议持有关注"
        elif not is_profit and score < 20:
            new_action = 'SELL'
            reason = f"回撤{dd:.1f}%触发止损，亏损{profit_pct:.1f}%且评分{score:.0f}<20，建议立即止损"
        else:
            new_action = action
            reason = ''
    else:
        # 未触发止损的正常持有
        if is_profit:
            new_action = 'HOLD'
            if hold_days >= 30 and score >= 30:
                reason = f"持有{hold_days}日+评分{score:.0f}≥30，建议继续持有，每10日再评估"
            elif hold_days >= 20 and score < 20:
                new_action = 'SELL'
                reason = f"20日检查评分{score:.0f}<20，建议卖出"
            else:
                reason = f"评分{score:.0f}持有{hold_days}日，浮盈{profit_pct:.1f}%"
        else:
            # 亏损中
            if score >= 30:
                new_action = 'HOLD'
                reason = f"评分{score:.0f}≥30亏损{profit_pct:.1f}%，建议持有等待反弹"
            elif score >= 20:
                new_action = 'HOLD_OBSERVE'
                reason = f"评分{score:.0f}亏损{profit_pct:.1f}%，建议观察跌破20分再止损"
            else:
                new_action = 'STOP_LOSS'
                reason = f"评分{score:.0f}<20亏损{profit_pct:.1f}%，建议立即止损"

    # 写入action_reason
    cur.execute("UPDATE strategy_signal_daily SET action=%s, action_reason=%s WHERE strategy_id=1 AND ts_code=%s AND trade_date=%s",
        (new_action, reason, code, latest_day))

    # 同步到portfolio_holdings
    action_label = {'HOLD':'🟢 持有','REDUCE':'🟡 减仓','SELL':'🔴 卖出','STOP_LOSS':'🔴 止损','HOLD_OBSERVE':'🟡 观察'}.get(new_action, '⏸️ 等待')
    cur.execute("UPDATE portfolio_holdings SET advice=%s, advice_reason=%s, updated_at=NOW() WHERE ts_code=%s AND status='HOLDING' ORDER BY trade_date DESC LIMIT 1",
        (action_label, reason, code))

    add_log(f"  {name:<8} 操作:{action_label:<8} {reason}")
    rules_applied += 1

add_log(f"  已应用 {rules_applied} 条决策规则")

conn.commit()
cur.close()
conn.close()
add_log("\n✅ 数据同步+决策规则执行完成")
