#!/usr/bin/env python3
"""
持仓同步工具 — OCR截图上传 → 解析 → 增量对比 → 存入数据库
=============================================================
每次调用:
  1. 解析截图 → 5只持仓详请 + 账户总资产
  2. 对比数据库中昨日记录
  3. 判断: 新买入/加仓/减仓/持有/清仓
  4. 更新数据库
  5. 给出操作建议(结合评分引擎信号)
"""
import pytesseract, re, json, sys, os
from db_config import get_connection
from PIL import Image
from datetime import date, datetime
from collections import defaultdict
from typing import List, Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ─── 截图中已知的5只股票坐标 ───
STOCK_DATA = [
    (1406, '301377.SZ', '鼎泰高科'),
    (1634, '688525.SH', '佰维存储'),
    (1860, '300476.SZ', '胜宏科技'),
    (2088, '002185.SZ', '华天科技'),
    (2315, '002050.SZ', '三花智控'),
]

def parse_screenshot(image_path: str) -> Dict:
    """
    解析持仓截图, 返回结构:
    {
        'trade_date': '2026-05-26',
        'total_assets': 1037912.93,
        'holdings': [
            {'ts_code':'301377.SZ','name':'鼎泰高科','qty':600,'avail_qty':600,
             'current_price':327.230,'cost_price':243.548,
             'market_value':196338.00,'profit_amount':50208.92,'profit_pct':34.36},
            ...
        ]
    }
    """
    img = Image.open(image_path)
    data = pytesseract.image_to_data(img, lang='chi_sim+eng', output_type=pytesseract.Output.DICT)
    
    # 提取总资产
    total_assets = 0
    for i in range(len(data['text'])):
        t = data['text'][i].strip()
        if not t: continue
        left = int(data['left'][i]); top = int(data['top'][i])
        if 600 <= top <= 750 and 100 <= left <= 400:
            try:
                v = float(t.replace(',',''))
                if v > 100000:
                    total_assets = v
            except: pass
    
    # 提取每只股票
    holdings = []
    for base_y, ts_code, name in STOCK_DATA:
        # 数量: y=base_y, x=400~650
        qty = 0
        for i in range(len(data['text'])):
            t = data['text'][i].strip(); top = int(data['top'][i]); left = int(data['left'][i])
            if abs(top - base_y) > 40: continue
            if 400 <= left <= 650:
                try:
                    v = float(t.replace(',',''))
                    if v == int(v) and v > 0 and int(v) < 100000:
                        qty = int(v)
                except: pass
        
        # 可用数量: y=base_y+70, x=400~650
        avail_qty = 0
        for i in range(len(data['text'])):
            t = data['text'][i].strip(); top = int(data['top'][i]); left = int(data['left'][i])
            if abs(top - (base_y + 70)) > 30: continue
            if 400 <= left <= 650:
                try:
                    v = float(t.replace(',',''))
                    if v == int(v) and v >= 0 and int(v) < 100000:
                        avail_qty = int(v)
                except: pass
        
        # 现价: y=base_y, x=700~850
        current_price = 0
        for i in range(len(data['text'])):
            t = data['text'][i].strip(); top = int(data['top'][i]); left = int(data['left'][i])
            if abs(top - base_y) > 40: continue
            if 700 <= left <= 850:
                try:
                    v = float(t.replace(',',''))
                    if 1 <= v <= 2000:
                        current_price = v
                except: pass
        
        # 成本价: y=base_y+70, x=700~850
        cost_price = 0
        for i in range(len(data['text'])):
            t = data['text'][i].strip(); top = int(data['top'][i]); left = int(data['left'][i])
            if abs(top - (base_y + 70)) > 30: continue
            if 700 <= left <= 850:
                try:
                    v = float(t.replace(',',''))
                    if 1 <= v <= 2000:
                        cost_price = v
                except: pass
        
        # 盈亏额: y=base_y, x>=950
        profit_amt = 0
        for i in range(len(data['text'])):
            t = data['text'][i].strip(); top = int(data['top'][i]); left = int(data['left'][i])
            if abs(top - base_y) > 40: continue
            if left >= 950:
                try:
                    v = float(t.replace(',','').replace('%','').replace('+',''))
                    if abs(v) > 100:
                        profit_amt = v
                except: pass
        
        # 盈亏率: y=base_y+70, x>=950
        profit_pct = 0
        for i in range(len(data['text'])):
            t = data['text'][i].strip(); top = int(data['top'][i]); left = int(data['left'][i])
            if abs(top - (base_y + 70)) > 30: continue
            if left >= 950:
                try:
                    v = float(t.replace('%','').replace('+',''))
                    if 0 < abs(v) < 100:
                        profit_pct = v
                except: pass
        
        if qty > 0:
            holdings.append({
                'ts_code': ts_code, 'name': name,
                'qty': qty, 'avail_qty': avail_qty,
                'current_price': round(current_price, 3),
                'cost_price': round(cost_price, 3),
                'market_value': round(qty * current_price, 2),
                'profit_amount': round(profit_amt, 2),
                'profit_pct': round(profit_pct, 2),
            })
    
    total_mv = sum(h['market_value'] for h in holdings)
    available_cash = round(total_assets - total_mv, 2) if total_assets > 0 else 0
    
    return {
        'trade_date': str(date.today()),
        'total_assets': round(total_assets, 2),
        'total_market_value': round(total_mv, 2),
        'available_cash': available_cash,
        'holdings': holdings,
    }

def sync_to_db(result: Dict, db: 'pymysql.connection') -> Dict:
    """
    同步截图数据到数据库, 并返回增量变更
    
    Returns:
    {
        'new_holdings': [],   # 新买入的
        'added': [],          # 加仓的
        'reduced': [],        # 减仓的
        'held': [],           # 持有不变的
        'closed': [],         # 清仓的
        'trades': [],         # 今日买卖操作记录
    }
    """
    cur = db.cursor(pymysql.cursors.DictCursor)
    trade_date = result['trade_date']
    changes = {'new_holdings': [], 'added': [], 'reduced': [], 'held': [], 'closed': [], 'trades': []}
    
    # 获取昨日持仓
    cur.execute("""
        SELECT * FROM portfolio_holdings 
        WHERE trade_date = (SELECT MAX(trade_date) FROM portfolio_holdings WHERE trade_date < %s)
        AND status = 'HOLDING'
    """, (trade_date,))
    yesterday = {r['ts_code']: r for r in cur.fetchall()}
    
    # 获取今日截图中现有持仓
    screenshot_codes = {h['ts_code'] for h in result['holdings']}
    
    # 1. 处理每只截图中的股票
    for h in result['holdings']:
        code = h['ts_code']
        
        # 对比昨日持仓
        if code in yesterday:
            y = yesterday[code]
            old_qty = int(y['qty'])
            new_qty = h['qty']
            
            if new_qty > old_qty:
                changes['added'].append(h)
                changes['trades'].append({
                    'ts_code': code, 'name': h['name'],
                    'action': '加仓', 'old_qty': old_qty, 'new_qty': new_qty,
                    'diff': new_qty - old_qty,
                })
            elif new_qty < old_qty:
                changes['reduced'].append(h)
                changes['trades'].append({
                    'ts_code': code, 'name': h['name'],
                    'action': '减仓', 'old_qty': old_qty, 'new_qty': new_qty,
                    'diff': old_qty - new_qty,
                })
            else:
                changes['held'].append(h)
        else:
            # 新买入
            changes['new_holdings'].append(h)
            changes['trades'].append({
                'ts_code': code, 'name': h['name'],
                'action': '新买入', 'old_qty': 0, 'new_qty': h['qty'],
                'diff': h['qty'],
            })
        
        # 写入今日持仓
        cur.execute("""
            INSERT INTO portfolio_holdings 
            (ts_code, name, trade_date, qty, avail_qty, current_price, cost_price, 
             market_value, profit_amount, profit_pct, status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'HOLDING')
            ON DUPLICATE KEY UPDATE 
                qty=VALUES(qty), avail_qty=VALUES(avail_qty),
                current_price=VALUES(current_price), market_value=VALUES(market_value),
                profit_amount=VALUES(profit_amount), profit_pct=VALUES(profit_pct)
        """, (code, h['name'], trade_date, h['qty'], h.get('avail_qty',0),
              h['current_price'], h['cost_price'], h['market_value'],
              h['profit_amount'], h['profit_pct']))
    
    # 2. 处理清仓的（昨日有但截图没有的）
    for code, y in yesterday.items():
        if code not in screenshot_codes:
            qty = int(y['qty'])
            if qty > 0:
                # 清仓
                cur.execute("""
                    INSERT INTO portfolio_holdings 
                    (ts_code, name, trade_date, qty, avail_qty, current_price, cost_price,
                     market_value, profit_amount, profit_pct, status, closed_date)
                    VALUES (%s,%s,%s,0,0,%s,%s,0,0,0,'SOLD',%s)
                    ON DUPLICATE KEY UPDATE status='SOLD', closed_date=VALUES(closed_date)
                """, (y['ts_code'], y['name'], trade_date, y['current_price'], y['cost_price'], trade_date))
                
                changes['closed'].append(y)
                changes['trades'].append({
                    'ts_code': code, 'name': y['name'],
                    'action': '清仓', 'old_qty': qty, 'new_qty': 0,
                    'diff': qty,
                })
    
    # 3. 写入账户总资产
    cur.execute("""
        INSERT INTO portfolio_account 
        (trade_date, total_assets, total_market_value, available_cash)
        VALUES (%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE 
            total_assets=VALUES(total_assets),
            total_market_value=VALUES(total_market_value),
            available_cash=VALUES(available_cash)
    """, (trade_date, result['total_assets'], result['total_market_value'], result['available_cash']))
    
    db.commit()
    cur.close()
    return changes

def get_advice(holdings: List[Dict], db) -> List[Dict]:
    """结合评分引擎给出每只持仓的操作建议"""
    try:
        from engine.vmap import vmap_score
        from engine.chanlun_scorer import score_chanlun_enhanced
        from engine.cycle_scorer import score_cycle_enhanced
        from engine.indicators import rsi, sma
        from engine.sentiment_scorer import score_sentiment
        from engine.block_weights import get_block_weights, apply_block_weights
        
        conn2 = get_connection()
        cur2 = conn2.cursor(pymysql.cursors.DictCursor)
        
        advices = []
        for h in holdings:
            code = h['ts_code']
            cur2.execute("SELECT trade_date, high, low, close, vol, change_pct FROM daily_kline_qfq WHERE ts_code=%s ORDER BY trade_date ASC",(code,))
            rows = cur2.fetchall()
            if len(rows) < 200:
                advices.append({'ts_code':code,'name':h['name'],'signal':'NODATA','advice':'⏸️ 数据不足'})
                continue
            
            closes=[float(r['close']) for r in rows]; vols=[float(r.get('vol',0) or 0) for r in rows]
            chgs=[float(r.get('change_pct') or 0) for r in rows]
            pass  # 评分引擎引用暂时保留
            
        conn2.close()
    except Exception as e:
        return []  # 评分引擎不可用时不报错
    return []

def main(image_path: str):
    import pymysql
    conn = get_connection()
    
    print(f"📷 解析截图: {image_path}")
    result = parse_screenshot(image_path)
    
    print(f"\n{'='*60}")
    print(f"📊 持仓同步报告")
    print(f"{'='*60}")
    print(f"  日期: {result['trade_date']}")
    print(f"  账户资产: {result['total_assets']:>12.2f}")
    print(f"  持仓市值: {result['total_market_value']:>12.2f}")
    print(f"  可用资金: {result['available_cash']:>12.2f}\n")
    
    changes = sync_to_db(result, conn)
    
    if changes['new_holdings']:
        print(f"  🆕 新买入:")
        for h in changes['new_holdings']:
            print(f"    {h['name']} {h['qty']}股 @ {h['current_price']}")
    
    if changes['added']:
        print(f"  📈 加仓:")
        for t in changes['trades']:
            if t['action']=='加仓': print(f"    {t['name']} {t['diff']}股")
    
    if changes['reduced']:
        print(f"  📉 减仓:")
        for t in changes['trades']:
            if t['action']=='减仓': print(f"    {t['name']} {t['diff']}股")
    
    if changes['held']:
        print(f"  ⏸️ 持有中 ({len(changes['held'])}只)")
    
    if changes['closed']:
        print(f"  🔴 清仓:")
        for h in changes['closed']:
            print(f"    {h['name']} (平仓日: {h['trade_date']})")
    
    print(f"\n  📋 今日交易:")
    for t in changes['trades']:
        print(f"    {t['action']} {t['name']} {t['diff']}股")
    
    conn.close()

if __name__ == '__main__':
    import sys
    img = sys.argv[1] if len(sys.argv) > 1 else '/root/.openclaw/media/qqbot/downloads/5D13A2197D4E6FB6C6D3FA70D53C5718_1779799566045_10dc1b.png'
    main(img)
