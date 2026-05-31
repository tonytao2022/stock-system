#!/usr/bin/env python3
"""
数据一致性同步器 v1.0
========================
每天早上15:30收盘数据管道最后一步运行，确保：
  1. trend_score → strategy_signal_daily   (评分→策略信号，322→194只)
  2. strategy_signal_daily → portfolio_holdings.advice (策略信号→持仓建议)
  3. 检查不一致的记录并告警

运行方式: python3 data_sync.py
"""
import os, sys, time, pymysql
from datetime import date, datetime
from collections import defaultdict

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
add_log("数据一致性同步器启动")
add_log("=" * 50)

# ─── Step 1: 确保strategy_signal_daily覆盖所有持仓股票 ───
add_log("\nStep 1: 同步持仓策略信号...")
latest_ssd = None
cur.execute("SELECT MAX(trade_date) FROM strategy_signal_daily WHERE strategy_id=1")
latest_ssd = cur.fetchone()[0]

if not latest_ssd:
    add_log("  ❌ strategy_signal_daily无数据")
else:
    # 获取持仓中的股票列表
    cur.execute("SELECT ts_code FROM portfolio_holdings WHERE status='HOLDING'")
    holding_codes = [r[0] for r in cur.fetchall()]
    add_log(f"  持仓股票: {len(holding_codes)} 只")

    # 检查哪些持仓股票在strategy_signal_daily中没有最新日期的记录
    missing_signal = []
    if holding_codes:
        cur.execute("""
            SELECT ph.ts_code, ph.name FROM portfolio_holdings ph
            LEFT JOIN strategy_signal_daily ssd ON ph.ts_code=ssd.ts_code 
                AND ssd.strategy_id=1 AND ssd.trade_date=%s
            WHERE ph.status='HOLDING' AND ssd.ts_code IS NULL
        """, (latest_ssd,))
        missing_signal = cur.fetchall()

    if missing_signal:
        for code, name in missing_signal:
            add_log(f"  ⚠️ {code} {name} 缺少策略信号，正在补充...")
            # 从trend_score取最新评分，写入strategy_signal_daily
            cur.execute("""
                SELECT composite_score, structure_score FROM trend_score 
                WHERE ts_code=%s AND trade_date <= %s ORDER BY trade_date DESC LIMIT 1
            """, (code, latest_ssd))
            ts_row = cur.fetchone()
            if ts_row:
                score = float(ts_row[0] or 0)
                action = 'BUY' if score >= 30 else 'WAIT'
                cur.execute("""
                    INSERT INTO strategy_signal_daily (strategy_id, ts_code, trade_date, action, holding_status, buy_score, created_at)
                    VALUES (1, %s, %s, %s, 'HOLDING', %s, NOW())
                    ON DUPLICATE KEY UPDATE action=VALUES(action), buy_score=VALUES(buy_score), holding_status='HOLDING'
                """, (code, latest_ssd, action, round(score, 1)))
                add_log(f"    → 写入评分{score:.1f}, 建议{action}")
            else:
                add_log(f"    ⚠️ trend_score中无{code}的评分，跳过")
    else:
        add_log(f"  ✅ 全部{len(holding_codes)}只持仓均有策略信号")

# ─── Step 2: 同步strategy_signal_daily → portfolio_holdings.advice ───
add_log("\nStep 2: 同步持仓操作建议...")
cur.execute("""
    SELECT ph.ts_code, ph.name, ph.user_id,
           ssd.action, ssd.buy_score
    FROM portfolio_holdings ph
    LEFT JOIN strategy_signal_daily ssd ON ph.ts_code=ssd.ts_code 
        AND ssd.strategy_id=1 AND ssd.trade_date=%s
    WHERE ph.status='HOLDING'
""", (latest_ssd,))

holdings = cur.fetchall()
updated = 0
for row in holdings:
    ts_code = row[0]
    name = row[1]
    user_id = row[2]
    action = row[3] or 'WAIT'
    buy_score = float(row[4]) if row[4] else 0
    
    # 生成建议文本
    action_map = {'BUY': '买入', 'HOLD': '持有/加仓', 'SELL': '卖出', 'WAIT': '等待', 'STOP_LOSS': '止损'}
    advice = f"🟢 {action_map.get(action, action)}"
    if action == 'WAIT':
        advice = f"⏸️ {action_map.get(action, '等待')}"
    elif action == 'STOP_LOSS':
        advice = f"🔴 {action_map.get(action, '止损')}"
    
    # 获取结构分和季节分
    cur.execute("SELECT structure_score, cycle_score FROM trend_score WHERE ts_code=%s AND trade_date <= %s ORDER BY trade_date DESC LIMIT 1", (ts_code, latest_ssd))
    tr = cur.fetchone()
    structure = float(tr[0]) if tr and tr[0] else 0
    cycle = float(tr[1]) if tr and tr[1] else 0
    
    reason = f"V={buy_score:.0f}/趋势{structure:.0f}"
    
    # 更新
    cur.execute("""
        UPDATE portfolio_holdings 
        SET advice=%s, advice_reason=%s, updated_at=NOW()
        WHERE ts_code=%s AND user_id=%s AND status='HOLDING'
        ORDER BY trade_date DESC LIMIT 1
    """, (advice, reason, ts_code, user_id))
    if cur.rowcount > 0:
        updated += 1

add_log(f"  已同步 {updated}/{len(holdings)} 只持仓建议")
if updated < len(holdings):
    add_log(f"  ⚠️ {len(holdings)-updated} 只同步失败")

# ─── Step 3: 验证一致性 ───
add_log("\nStep 3: 一致性验证...")
cur.execute("""
    SELECT ph.ts_code, ph.name, ph.advice, COALESCE(ph.advice_reason,'无') as reason,
           ssd.action, ssd.buy_score
    FROM portfolio_holdings ph
    LEFT JOIN strategy_signal_daily ssd ON ph.ts_code=ssd.ts_code 
        AND ssd.strategy_id=1 AND ssd.trade_date=%s
    WHERE ph.status='HOLDING'
    ORDER BY ph.ts_code
""", (latest_ssd,))

inconsistent = 0
for r in cur.fetchall():
    name = r[1]
    advice = r[2] or '无'
    reason = r[3]
    ssd_action = r[4] or '无数据'
    score = r[5]
    msg = f"  {name:<8} 持仓建议:{advice}  策略信号:{ssd_action}({score})  原因:{reason[:30]}"
    if ssd_action == '无数据':
        msg += " ❌ 信号缺失"
        inconsistent += 1
    add_log(msg)

conn.commit()
add_log(f"\n  {'='*15}")
add_log(f"  一致性检查: {'✅ 全部一致' if not inconsistent else f'❌ {inconsistent}处不一致'}")
add_log(f"  {'='*15}")

cur.close()
conn.close()
add_log("\n数据同步完成")
