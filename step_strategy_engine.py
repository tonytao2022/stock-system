#!/usr/bin/env python3
"""
阶梯动态持有策略引擎 - 每日评估 + API

规则：
  买入条件：综合评分≥30
  10日检查点：评分≥10续持，否则平仓
  20日检查点：评分≥20续持，否则平仓  
  30日检查点：评分≥30续持，否则平仓
  30日后每10日再评估：评分≥30续持，否则平仓
  全程止损：从最高点回撤≥10%时平仓
  最大持有60日

部署为：/opt/stock-analyzer/step_strategy_engine.py
由8887管理服务每日16:00调用的cron触发
"""

import pymysql, math, sys, json, os
from datetime import datetime, date, timedelta
from collections import defaultdict

# ─── DB连接 ───
def get_pwd():
    try:
        with open('/etc/mysql/debian.cnf') as f:
            for l in f:
                if 'password' in l:
                    return l.split('=')[-1].strip().strip('"').strip("'")
    except: pass
    return ''

PWD = get_pwd()
DB = {'host':'127.0.0.1','port':3306,'user':'debian-sys-maint','password':PWD,'database':'stock_db','charset':'utf8mb4'}

def get_conn():
    return pymysql.connect(**DB)


# ─── 实时行情获取（东方财富/腾讯） ───
def fetch_realtime_price(ts_code):
    """获取实时股价（盘中用腾讯行情，收盘用东方财富）"""
    import urllib.request
    
    # 代码转换: 600xxx.SH -> sh600xxx, 000xxx.SZ / 30xxxx.SZ -> sz30xxxx
    if ts_code.endswith('.SH'):
        qcode = 'sh' + ts_code[:6]
    elif ts_code.endswith('.SZ'):
        qcode = 'sz' + ts_code[:6]
    else:
        return None
    
    url = f'https://qt.gtimg.cn/q={qcode}'
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        resp = urllib.request.urlopen(req, timeout=5)
        text = resp.read().decode('gbk')
        # 格式: v_sh600xxx="...~name~code~now~close~open~vol~..."
        parts = text.split('~')
        if len(parts) >= 5:
            now_price = parts[3].strip()
            close_price = parts[4].strip()  # 昨收
            change_pct = parts[5].strip() if len(parts) > 5 else '0'
            try:
                return {
                    'price': float(now_price),
                    'prev_close': float(close_price),
                    'change_pct': round((float(now_price) - float(close_price)) / float(close_price) * 100, 2) if float(close_price) > 0 else 0,
                    'realtime': True,
                }
            except:
                return None
    except Exception as e:
        return None


# ─── 评分计算工具 ───
def sma(data, w):
    r = [None]*len(data)
    s = 0
    for i in range(len(data)):
        s += data[i]
        if i >= w-1:
            if i >= w: s -= data[i-w]
            r[i] = s / w
    return r

def sstd(data, w):
    r = [None]*len(data)
    for i in range(w-1, len(data)):
        avg = sum(data[i-w+1:i+1]) / w
        r[i] = math.sqrt(sum((x-avg)**2 for x in data[i-w+1:i+1])/w)
    return r

def calc_score_all(closes, highs, lows, vols):
    """批量计算全部日期的评分"""
    n = len(closes)
    scores = [50.0]*n
    if n < 120:
        return scores
    
    ma5 = sma(closes,5); ma10 = sma(closes,10); ma20 = sma(closes,20)
    ma60 = sma(closes,60); ma120 = sma(closes,120)
    std20 = sstd(closes,20)
    
    # RSI14
    rsi14 = [None]*n
    for i in range(14, n):
        g = l = 0
        for j in range(i-13, i+1):
            c = closes[j] - closes[j-1]
            if c > 0: g += c
            else: l += abs(c)
        rsi14[i] = 100*g/(g+l) if (g+l)>0 else 50
    
    for i in range(120, n):
        if closes[i] <= 0: continue
        
        # 趋势40%
        tr = 20
        ma5_i, ma10_i, ma20_i = ma5[i], ma10[i], ma20[i]
        ma60_i, ma120_i = ma60[i], ma120[i]
        if all(x for x in [ma5_i, ma10_i, ma20_i, ma60_i, ma120_i]):
            al = 0
            if ma5_i > ma10_i: al += 8
            if ma5_i > ma20_i: al += 8
            if ma10_i > ma20_i: al += 8
            if ma20_i > ma60_i: al += 8
            if ma20_i > ma120_i: al += 3
            po = 0
            if closes[i] > ma5_i: po += 5
            if closes[i] > ma10_i: po += 5
            if closes[i] > ma20_i: po += 5
            old = ma20[i-20] if ma20[i-20] else ma20_i
            slope = (ma20_i - old)/old if old > 0 else 0
            tr = min(100, max(0, al+po+max(0,min(10,(slope+0.05)*80))))
        
        # 动量30%
        r5 = (closes[i]-closes[i-5])/closes[i-5] if closes[i-5]>0 else 0
        r10 = (closes[i]-closes[i-10])/closes[i-10] if closes[i-10]>0 else 0
        r20 = (closes[i]-closes[i-20])/closes[i-20] if closes[i-20]>0 else 0
        r14v = rsi14[i] if rsi14[i] else 50
        mo = max(0, min(30, 10 + r5*40 + r10*20 + r20*10 + (r14v-50)*0.15))
        
        # 波动20%
        wv = 50
        s20v = std20[i]
        if s20v and closes[i] > 0:
            dv = s20v/closes[i]
            if dv < 0.005: wv = 15
            elif dv < 0.015: wv = 40 + (dv-0.005)/0.01*10
            elif dv < 0.03: wv = 25 + (0.03-dv)/0.015*25
            else: wv = max(10, 25-(dv-0.03)*500)
        
        # 量能10%
        vl = 50
        if vols and vols[i] > 0:
            vm20 = sum(vols[max(0,i-19):i+1])/min(20,i+1)
            if vm20 > 0:
                vr = vols[i]/vm20
                if vr < 0.5: vl = 30
                elif vr < 0.8: vl = 40
                elif vr < 1.2: vl = 50
                elif vr < 1.5: vl = 60
                else: vl = 70
        
        scores[i] = round(tr*0.4 + mo*0.3 + wv*0.2 + vl*0.1, 1)
    
    return scores


# ════════════════════════════════════════════
# 主逻辑：每日策略评估
# ════════════════════════════════════════════

def load_strategy_configs():
    """加载所有活跃策略"""
    conn = get_conn()
    cur = conn.cursor(pymysql.cursors.DictCursor)
    cur.execute("SELECT * FROM strategy_config WHERE is_active=1")
    rows = cur.fetchall()
    cur.close(); conn.close()
    return rows

def evaluate_strategy(trade_date, strategy):
    """对监控池执行一次策略评估"""
    sid = strategy['id']
    buy_min = strategy['buy_min_score']
    p1 = strategy['p1_score']
    p2 = strategy['p2_score']
    p3 = strategy['p3_score']
    sl_pct = float(strategy['stop_loss_pct'])
    max_hold = strategy['max_hold_days']
    cool_days = strategy['cool_days']
    
    sl_ratio = sl_pct / 100.0
    
    conn = get_conn()
    cur = conn.cursor(pymysql.cursors.DictCursor)
    
    # 1. 获取监控池股票
    cur.execute("SELECT ts_code, name FROM watch_pool WHERE is_active=1")
    stocks = cur.fetchall()
    
    # 2. 获取持仓
    cur.execute("""
        SELECT ts_code, name, buy_date, cost_price, current_price, profit_pct, 
               qty, lock_until, lock_active
        FROM portfolio_holdings 
        WHERE status='HOLDING'
    """)
    holdings = {r['ts_code']: r for r in cur.fetchall()}
    
    # 3. 获取K线和预计算评分
    results = []
    
    for stk in stocks:
        ts_code = stk['ts_code']
        name = stk['name']
        
        cur.execute("""
            SELECT trade_date, close, high, low, vol 
            FROM daily_kline_qfq 
            WHERE ts_code=%s 
            ORDER BY trade_date ASC
        """, (ts_code,))
        klines = cur.fetchall()
        
        if len(klines) < 200:
            continue
        
        dates = [str(r['trade_date']) for r in klines]
        closes = [float(r['close']) for r in klines]
        highs = [float(r['high']) for r in klines]
        lows = [float(r['low']) for r in klines]
        vols = [float(r['vol'] or 0) for r in klines]
        
        # 评分
        scores = calc_score_all(closes, highs, lows, vols)
        
        # 找到当前日期在K线中的下标
        trade_date_str = trade_date.strftime('%Y-%m-%d') if isinstance(trade_date, date) else trade_date
        
        try:
            idx = dates.index(trade_date_str)
        except ValueError:
            # 当天可能没有K线（非交易日），取最近的一个
            idx = -1
            for i in range(len(dates)-1, -1, -1):
                if dates[i] <= trade_date_str:
                    idx = i
                    break
            if idx < 120:
                continue
        
        current_score = scores[idx]
        current_price = closes[idx]
        
        # 尝试获取实时行情（盘中替换收盘价）
        rt = fetch_realtime_price(ts_code)
        if rt and rt['realtime']:
            current_price = rt['price']
            # 实时价格不参与评分计算（评分仍用K线数据），仅用于盈亏和价格显示
        
        # 检查是否在持仓中
        holding = holdings.get(ts_code)
        
        if holding:
            # ─── 持仓评估 ───
            buy_date = str(holding['buy_date']) if holding['buy_date'] else dates[0]
            cost = float(holding['cost_price'])
            
            # 计算持仓天数
            buy_idx = -1
            for i in range(len(dates)):
                if dates[i] == buy_date:
                    buy_idx = i
                    break
            if buy_idx < 0:
                for i in range(len(dates)-1, -1, -1):
                    if dates[i] <= buy_date:
                        buy_idx = i
                        break
            
            hold_days = idx - buy_idx if buy_idx >= 0 else 0
            
            # 计算最高价和回撤
            window = closes[buy_idx:idx+1]
            peak = max(window) if window else current_price
            buy_p = cost
            dd = (current_price - peak) / peak if peak > 0 else 0
            
            profit = (current_price - buy_p) / buy_p * 100
            
            # 确定当前检查点
            if hold_days <= 10:
                cp = 10
                days_to_cp = 10 - hold_days
                threshold = p1
                cp_score = None
                passed = None if hold_days < 10 else (scores[idx] >= p1)
            elif hold_days <= 20:
                cp = 20
                days_to_cp = 20 - hold_days
                threshold = p2
                cp_score = scores[idx]
                passed = scores[idx] >= p2
            elif hold_days <= 30:
                cp = 30
                days_to_cp = 30 - hold_days
                threshold = p3
                cp_score = scores[idx]
                passed = scores[idx] >= p3
            else:
                # 30日后每10日检查
                next_check = ((hold_days // 10) + 1) * 10
                cp = 31
                days_to_cp = next_check - hold_days
                threshold = p3
                cp_score = scores[idx]
                # 只有在检查日才判定
                if hold_days % 10 == 0:
                    passed = scores[idx] >= p3
                else:
                    passed = 1  # 非检查日默认通过
            
            # 止损
            hit_sl = 1 if dd <= -sl_ratio else 0
            
            # 方案C: 5日评分检查
            ck5 = strategy.get('ck5_score', 0) or 0
            
            # 减仓检查: 从买入价亏损超过阈值则触发减半仓（仅在未减仓时触发一次）
            reduce_pct = float(strategy.get('reduce_pct', 0) or 0)
            reduce_flag = 0
            if reduce_pct > 0 and profit <= -reduce_pct:
                reduce_flag = 1  # 触发减仓信号
            
            # 最终行动
            if hit_sl:
                action = 'STOP_LOSS'
                reason = f'从最高点回撤{dd*100:.1f}%超过止损{sl_pct}%'
            elif ck5 > 0 and hold_days == 5 and scores[idx] < ck5:
                action = 'SELL'
                reason = f'5日检查评分{scores[idx]}<{ck5}，不达标平仓'
            elif cp == 10 and hold_days >= 10 and not passed:
                action = 'SELL'
                reason = f'10日检查评分{scores[idx]}<{p1}，不达标平仓'
            elif cp == 20 and hold_days >= 20 and not passed:
                action = 'SELL'
                reason = f'20日检查评分{scores[idx]}<{p2}，不达标平仓'
            elif cp == 30 and hold_days >= 30 and not passed:
                action = 'SELL'
                reason = f'30日检查评分{scores[idx]}<{p3}，不达标平仓'
            elif cp == 31 and hold_days % 10 == 0 and not passed:
                action = 'SELL'
                reason = f'30日+再评估评分{scores[idx]}<{p3}，不达标平仓'
            elif hold_days >= max_hold:
                action = 'SELL'
                reason = f'最大持有期{max_hold}日到期'
            else:
                if reduce_flag:
                    action = 'HOLD'
                    reason = f'亏损{profit:.1f}%超{reduce_pct:.0f}%减仓线，建议减半仓，评分{scores[idx]}'
                else:
                    action = 'HOLD'
                    reason = f'已持有{hold_days}日，评分{scores[idx]}，继续持有'
            
            results.append({
                'ts_code': ts_code, 'name': name,
                'trade_date': trade_date_str,
                'strategy_id': sid,
                'buy_score': round(current_score, 1),
                'holding_status': 'HOLDING',
                'hold_days': hold_days,
                'days_to_check': days_to_cp if cp <= 30 else next_check - hold_days,
                'current_checkpoint': cp,
                'buy_date': buy_date,
                'buy_price': round(buy_p, 3),
                'cost_price': round(cost, 3),
                'current_price_r': round(current_price, 3),
                'profit_pct': round(profit, 3),
                'checkpoint_score_check': round(cp_score, 1) if cp_score else None,
                'checkpoint_threshold': threshold,
                'checkpoint_passed': passed if (hold_days == cp) else (1 if hold_days < cp else None),
                'peak_price': round(peak, 3),
                'drawdown_pct': round(dd*100, 3),
                'stop_loss_pct': sl_pct,
                'hit_stop_loss': hit_sl,
                'reduce_flag': reduce_flag,
                'price_source': 'realtime' if (rt and rt.get('realtime')) else 'daily',
                'action': action,
                'action_reason': reason,
            })
            
        else:
            # ─── 未持仓——检查买入信号 ───
            # 检查是否达到买入条件
            last_buy_idx = None
            for i in range(max(120, idx-200), idx+1):
                if scores[i] >= buy_min:
                    last_buy_idx = i
            
            if last_buy_idx is not None and idx - last_buy_idx < cool_days:
                # 冷却期内，不产生买入信号
                cur_action = 'WAIT'
                cur_reason = f'冷却期(距上次信号{idx-last_buy_idx}日)，评分{current_score}'
            elif current_score >= buy_min:
                cur_action = 'BUY'
                cur_reason = f'评分{current_score}≥{buy_min}，触发买入条件'
            else:
                cur_action = 'WAIT'
                cur_reason = f'评分{current_score}<{buy_min}，等待买入'
            
            results.append({
                'ts_code': ts_code, 'name': name,
                'trade_date': trade_date_str,
                'strategy_id': sid,
                'buy_score': round(current_score, 1),
                'holding_status': 'NONE',
                'hold_days': 0,
                'days_to_check': None,
                'current_checkpoint': 0,
                'buy_date': None,
                'buy_price': None,
                'cost_price': None,
                'current_price_r': round(current_price, 3),
                'profit_pct': None,
                'checkpoint_score_check': None,
                'checkpoint_threshold': None,
                'checkpoint_passed': None,
                'peak_price': None,
                'drawdown_pct': None,
                'stop_loss_pct': sl_pct,
                'hit_stop_loss': 0,
                'reduce_flag': 0,
                'price_source': 'realtime' if (rt and rt.get('realtime')) else 'daily',
                'action': cur_action,
                'action_reason': cur_reason,
            })
    
    cur.close(); conn.close()
    return results


def save_results(conn, results):
    """批量写入评估结果到strategy_signal_daily"""
    cur = conn.cursor()
    
    sql = """INSERT INTO strategy_signal_daily 
    (ts_code, trade_date, strategy_id, buy_score, holding_status, hold_days, 
     days_to_check, current_checkpoint, buy_date, buy_price, cost_price, 
     current_price_r, profit_pct, checkpoint_score_check, checkpoint_threshold, 
     checkpoint_passed, peak_price, drawdown_pct, stop_loss_pct, 
     hit_stop_loss, reduce_flag, price_source, action, action_reason)
    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    ON DUPLICATE KEY UPDATE
      buy_score=VALUES(buy_score), holding_status=VALUES(holding_status),
      hold_days=VALUES(hold_days), days_to_check=VALUES(days_to_check),
      current_checkpoint=VALUES(current_checkpoint),
      buy_date=VALUES(buy_date), buy_price=VALUES(buy_price),
      cost_price=VALUES(cost_price), current_price_r=VALUES(current_price_r),
      profit_pct=VALUES(profit_pct),
      checkpoint_score_check=VALUES(checkpoint_score_check),
      checkpoint_threshold=VALUES(checkpoint_threshold),
      checkpoint_passed=VALUES(checkpoint_passed),
      peak_price=VALUES(peak_price), drawdown_pct=VALUES(drawdown_pct),
      hit_stop_loss=VALUES(hit_stop_loss),
      reduce_flag=VALUES(reduce_flag),
      price_source=VALUES(price_source),
      action=VALUES(action), action_reason=VALUES(action_reason)"""
    
    n = 0
    for r in results:
        try:
            cur.execute(sql, (
                r['ts_code'], r['trade_date'], r['strategy_id'], r['buy_score'],
                r['holding_status'], r['hold_days'], r['days_to_check'],
                r['current_checkpoint'],
                r['buy_date'] if r.get('buy_date') and r['buy_date'] != 'None' else None,
                None if r.get('buy_price') is None else float(r['buy_price']),
                None if r.get('cost_price') is None else float(r['cost_price']),
                r['current_price_r'], r['profit_pct'],
                r['checkpoint_score_check'], r['checkpoint_threshold'],
                r['checkpoint_passed'], r['peak_price'], r['drawdown_pct'],
                r['stop_loss_pct'], r['hit_stop_loss'], r.get('reduce_flag', 0),
                r.get('price_source', 'daily'), r['action'], r['action_reason']
            ))
            n += 1
        except Exception as e:
            print(f"  写入失败 {r['ts_code']}: {e}")
    
    conn.commit()
    return n


# ════════════════════════════════════════════
# API响应函数（供FastAPI调用）
# ════════════════════════════════════════════

def get_strategy_results(trade_date=None, strategy_id=1):
    """获取某日的策略评估结果"""
    if trade_date is None:
        trade_date = date.today()
    
    conn = get_conn()
    cur = conn.cursor(pymysql.cursors.DictCursor)
    
    # 获取策略
    cur.execute("SELECT * FROM strategy_config WHERE id=%s AND is_active=1", (strategy_id,))
    strategy = cur.fetchone()
    if not strategy:
        cur.close(); conn.close()
        return {'error': 'Strategy not found'}
    
    cur.execute("""
        SELECT ssd.*, sb.name as stock_name, wp.ts_code as in_watch
        FROM strategy_signal_daily ssd
        JOIN watch_pool wp ON ssd.ts_code = wp.ts_code AND wp.is_active=1
        LEFT JOIN stock_basic sb ON ssd.ts_code = sb.ts_code
        WHERE ssd.trade_date=%s AND ssd.strategy_id=%s
        ORDER BY 
          CASE ssd.action 
            WHEN 'STOP_LOSS' THEN 0
            WHEN 'SELL' THEN 1
            WHEN 'BUY' THEN 2
            ELSE 3
          END,
          ssd.buy_score DESC
    """, (trade_date, strategy_id))
    
    signals = cur.fetchall()
    
    # 汇总统计
    action_counts = defaultdict(int)
    holding_count = 0
    total_profit = 0
    for s in signals:
        action_counts[s['action']] = action_counts.get(s['action'], 0) + 1
        if s['holding_status'] == 'HOLDING':
            holding_count += 1
            if s['profit_pct'] is not None:
                total_profit += float(s['profit_pct'])
    
    cur.close(); conn.close()
    
    return {
        'strategy': {
            'id': strategy['id'],
            'name': strategy['name'],
            'description': strategy['description'],
            'params': {
                'buy_min_score': strategy['buy_min_score'],
                'p1_score': strategy['p1_score'],
                'p2_score': strategy['p2_score'],
                'p3_score': strategy['p3_score'],
                'stop_loss_pct': float(strategy['stop_loss_pct']),
                'max_hold_days': strategy['max_hold_days'],
                'cool_days': strategy['cool_days'],
            }
        },
        'trade_date': str(trade_date),
        'summary': {
            'total_stocks': len(signals),
            'holdings': holding_count,
            'avg_holding_profit': round(total_profit / holding_count, 2) if holding_count > 0 else 0,
            'action_distribution': dict(action_counts),
        },
        'signals': [{
            'ts_code': s['ts_code'],
            'stock_name': s['stock_name'] or s['ts_code'],
            'buy_score': float(s['buy_score']) if s['buy_score'] else 0,
            'holding_status': s['holding_status'],
            'hold_days': s['hold_days'],
            'current_checkpoint': s['current_checkpoint'],
            'days_to_check': s['days_to_check'],
            'buy_price': float(s['buy_price']) if s['buy_price'] else None,
            'cost_price': float(s['cost_price']) if s['cost_price'] else None,
            'current_price': float(s['current_price_r']) if s['current_price_r'] else None,
            'profit_pct': float(s['profit_pct']) if s['profit_pct'] else None,
            'drawdown_pct': float(s['drawdown_pct']) if s['drawdown_pct'] else None,
            'peak_price': float(s['peak_price']) if s['peak_price'] else None,
            'checkpoint_score': float(s['checkpoint_score_check']) if s['checkpoint_score_check'] else None,
            'checkpoint_threshold': s['checkpoint_threshold'],
            'checkpoint_passed': bool(s['checkpoint_passed']) if s['checkpoint_passed'] is not None else None,
            'hit_stop_loss': bool(s['hit_stop_loss']),
            'stop_loss_pct': float(s['stop_loss_pct']),
            'action': s['action'],
            'action_reason': s['action_reason'],
        } for s in signals],
    }


# ════════════════════════════════════════════
# 每日运行入口
# ════════════════════════════════════════════

def run_daily(trade_date_str=None):
    """每日16:00 cron调用"""
    if trade_date_str:
        td = datetime.strptime(trade_date_str, '%Y-%m-%d').date()
    else:
        td = date.today()
    
    print(f"\n{'='*60}")
    print(f"📊 阶梯策略每日评估 - {td}")
    print(f"{'='*60}")
    
    strategies = load_strategy_configs()
    print(f"活跃策略: {len(strategies)}个")
    
    conn = get_conn()
    
    for s in strategies:
        print(f"\n▶ {s['name']} (ID={s['id']})")
        results = evaluate_strategy(td, s)
        print(f"   评估: {len(results)}只股票")
        
        n = save_results(conn, results)
        print(f"   已写入: {n}条")
        
        # 统计
        actions = defaultdict(int)
        holdings = 0
        sell_signals = []
        buy_signals = []
        for r in results:
            actions[r['action']] += 1
            if r['holding_status'] == 'HOLDING':
                holdings += 1
                if r['action'] in ('SELL', 'STOP_LOSS'):
                    sell_signals.append(f"{r['name']}({r['action']}:{r['action_reason']})")
            if r['action'] == 'BUY':
                buy_signals.append(f"{r['name']}(评分{r['buy_score']})")
        
        print(f"   信号分布: {dict(actions)}")
        print(f"   持仓中: {holdings}只")
        if buy_signals:
            print(f"   买入信号: {', '.join(buy_signals[:10])}")
            if len(buy_signals) > 10: print(f"     ...还有{len(buy_signals)-10}只")
        if sell_signals:
            print(f"   卖出信号: {', '.join(sell_signals[:10])}")
            if len(sell_signals) > 10: print(f"     ...还有{len(sell_signals)-10}只")
    
    conn.close()
    print(f"\n✅ 完成")
    return True


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--api':
        # 作为API使用（返回JSON）
        import json
        td = sys.argv[2] if len(sys.argv) > 2 else str(date.today())
        result = get_strategy_results(td)
        print(json.dumps(result, ensure_ascii=False, default=str))
    else:
        # 每日运行
        td = sys.argv[1] if len(sys.argv) > 1 else None
        run_daily(td)
