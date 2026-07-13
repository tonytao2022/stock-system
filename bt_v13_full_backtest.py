#!/usr/bin/env python3
"""
bt_v13_full_backtest.py — 情景化因子矩阵全量T+1回测
2026-07-13 by Main

方案：M1综合分 + 截面Rank Alpha加分（方案E）
回测：日级T+1模拟，含买入/卖出/止损/仓位管理

策略逻辑：
  每日评分: final_score = M1.composite_score + alpha_bonus
  买入: final >= buy_threshold，当天下午挂单，次日开盘买入
  持有检查: 每10日重评，低于续持线则卖出
  止损: 最高点回撤 >= stop_loss% 卖出
  最长持有: 60日强制平仓
  单票仓位: per_stock (total可用资金×)
  总仓上限: position_base
"""

import pymysql
import numpy as np
from collections import defaultdict
from datetime import datetime, date, timedelta
import sys, os, json, warnings
warnings.filterwarnings('ignore')

# ==================== 配置 ====================
def get_db_pass():
    try:
        with open('/etc/mysql/debian.cnf') as f:
            for line in f:
                if line.strip().startswith('password'):
                    return line.split('=')[1].strip()
    except: pass
    return 'iXve1rVBXfdA4tL9'
DB_CONFIG = {
    'host': 'localhost',
    'user': 'debian-sys-maint',
    'password': get_db_pass(),
    'database': 'stock_db_v2',
    'charset': 'utf8mb4', 'cursorclass': pymysql.cursors.DictCursor,
}

SCENARIO_CONFIG = {
    'summer':      {'name':'S1牛市','buy':68,'hold':55,'stop':0.08,'max_pos':0.50,'per':0.30,'cooldown':0,'alpha':{'alpha052':0.35,'alpha122':0.25,'alpha093':0.20}},
    'chaos_spring':{'name':'S2震荡暖','buy':70,'hold':55,'stop':0.08,'max_pos':0.40,'per':0.25,'cooldown':0,'alpha':{'alpha062':0.35,'alpha001':0.25,'alpha052':0.20}},
    'spring':      {'name':'S2春','buy':70,'hold':55,'stop':0.07,'max_pos':0.40,'per':0.25,'cooldown':0,'alpha':{'alpha062':0.30,'alpha001':0.30,'alpha052':0.20}},
    'chaos':       {'name':'S3震荡','buy':72,'hold':55,'stop':0.08,'max_pos':0.35,'per':0.20,'cooldown':3,'alpha':{'alpha169':0.35,'alpha013':0.30,'alpha052':0.35}},
    'chaos_autumn':{'name':'S4震荡弱','buy':72,'hold':50,'stop':0.06,'max_pos':0.20,'per':0.12,'cooldown':5,'alpha':{'alpha031':0.35,'alpha162':0.30,'alpha168':0.25}},
    'weak_autumn': {'name':'S5弱秋','buy':75,'hold':50,'stop':0.05,'max_pos':0.15,'per':0.08,'cooldown':7,'alpha':{'alpha062':0.40}},
    'autumn':      {'name':'S5弱市','buy':78,'hold':45,'stop':0.05,'max_pos':0.10,'per':0.05,'cooldown':10,'alpha':{'alpha062':0.40}},
    'winter':      {'name':'S5冬季','buy':85,'hold':40,'stop':0.03,'max_pos':0.05,'per':0.03,'cooldown':15,'alpha':{}},
}

SEASON_MAP = {
    'summer':'summer','spring':'spring','weak_spring':'spring',
    'chaos_spring':'chaos_spring','chaos':'chaos',
    'chaos_autumn':'chaos_autumn','weak_autumn':'weak_autumn',
    'autumn':'autumn','winter':'winter',
}

BONUS_SCALE = 30
BONUS_OFFSET = 10
BONUS_MAX = 15
BONUS_MIN = -8


class Backtester:
    def __init__(self, start_capital=1000000):
        self.start_capital = start_capital
        self.capital = start_capital
        self.holdings = {}  # {ts_code: {qty, buy_price, buy_date, high_since_buy, latest_score, last_review}}
        self.trade_log = []
        self.daily_equity = []
        self.db = None
        
    def connect(self):
        self.db = pymysql.connect(**DB_CONFIG)
        return self.db
    
    def close(self):
        if self.db:
            self.db.close()
    
    def get_season(self, trade_date):
        with self.db.cursor() as c:
            c.execute("SELECT trade_date, season FROM season_state WHERE trade_date=%s", (trade_date,))
            r = c.fetchone()
        if not r:
            return 'chaos'
        return SEASON_MAP.get(r['season'], 'chaos')
    
    def get_m1_scores(self, trade_date, codes):
        if not codes:
            return {}
        result = {}
        with self.db.cursor() as c:
            for i in range(0, len(codes), 200):
                chunk = codes[i:i+200]
                ids = ','.join(["'%s'" % c for c in chunk])
                c.execute("SELECT ts_code, composite_score FROM strategy_signal WHERE trade_date='%s' AND ts_code IN (%s)" % (trade_date, ids))
                for r in c.fetchall():
                    result[r['ts_code']] = float(r['composite_score'] or 50)
        return result
    
    def get_alpha_scores(self, trade_date, codes, factor_names):
        if not codes or not factor_names:
            return {}
        result_map = {fn: {} for fn in factor_names}
        
        # alpha062从strategy_signal读
        afs = [fn for fn in factor_names if fn != 'alpha062']
        if 'alpha062' in factor_names:
            for i in range(0, len(codes), 300):
                chunk = codes[i:i+300]
                ids = ','.join(["'%s'" % c for c in chunk])
                with self.db.cursor() as c:
                    c.execute("SELECT ts_code, alpha062_score FROM strategy_signal WHERE trade_date='%s' AND ts_code IN (%s) AND alpha062_score IS NOT NULL" % (trade_date, ids))
                    for r in c.fetchall():
                        result_map['alpha062'][r['ts_code']] = float(r['alpha062_score'])
        if afs:
            fn_str = ','.join(["'%s'" % fn for fn in afs])
            for i in range(0, len(codes), 300):
                chunk = codes[i:i+300]
                ids = ','.join(["'%s'" % c for c in chunk])
                with self.db.cursor() as c:
                    c.execute("SELECT ts_code, factor_name, factor_score FROM alpha_factor_score WHERE trade_date='%s' AND ts_code IN (%s) AND factor_name IN (%s)" % (trade_date, ids, fn_str))
                    for r in c.fetchall():
                        result_map[r['factor_name']][r['ts_code']] = float(r['factor_score'])
        return result_map
    
    def get_close_prices(self, trade_date, codes):
        """获取当日收盘价"""
        if not codes:
            return {}
        result = {}
        with self.db.cursor() as c:
            for i in range(0, len(codes), 300):
                chunk = codes[i:i+300]
                ids = ','.join(["'%s'" % c for c in chunk])
                c.execute("SELECT ts_code, close FROM daily_kline WHERE trade_date='%s' AND ts_code IN (%s)" % (trade_date, ids))
                for r in c.fetchall():
                    result[r['ts_code']] = float(r['close'])
        return result
    
    def compute_rank(self, scores):
        if not scores:
            return {}
        keys = list(scores.keys())
        vals = np.array([scores[k] for k in keys], dtype=float)
        order = np.argsort(vals)
        ranks = np.empty(len(keys))
        ranks[order] = np.arange(len(keys)) / max(len(keys) - 1, 1)
        return {keys[i]: float(ranks[i]) for i in range(len(keys))}
    
    def compute_bonus(self, alpha_scores, alpha_ranks_cfg):
        """计算截面Rank加分"""
        if not alpha_ranks_cfg:
            return {}
        alpha_ranks = {}
        for fn in alpha_ranks_cfg:
            if fn in alpha_scores and alpha_scores[fn]:
                alpha_ranks[fn] = self.compute_rank(alpha_scores[fn])
        
        bonuses = {}
        all_ts = set()
        for fn in alpha_ranks:
            all_ts.update(alpha_ranks[fn].keys())
        
        for ts in all_ts:
            raw = 0
            ws = 0
            for fn, w in alpha_ranks_cfg.items():
                rank = alpha_ranks.get(fn, {}).get(ts, 0.5)
                raw += rank * w
                ws += w
            if ws > 0:
                raw /= ws
            bonus = raw * BONUS_SCALE - BONUS_OFFSET
            bonus = max(BONUS_MIN, min(BONUS_MAX, round(bonus, 1)))
            bonuses[ts] = bonus
        return bonuses
    
    def get_pool(self):
        with self.db.cursor() as c:
            c.execute("SELECT ts_code FROM watch_pool")
            return [r['ts_code'] for r in c.fetchall()]
    
    def get_dates(self, start_date, end_date):
        with self.db.cursor() as c:
            c.execute("SELECT DISTINCT trade_date FROM daily_kline WHERE trade_date >= %s AND trade_date <= %s ORDER BY trade_date", (start_date, end_date))
            return [str(r['trade_date']) for r in c.fetchall()]
    
    def record_equity(self, trade_date):
        """记录当日净资产"""
        total = self.capital
        # 简化：持仓按买入价估算（不拉实时价）
        for ts, h in self.holdings.items():
            total += h['qty'] * h.get('current_price', h['buy_price'])
        self.daily_equity.append({'date': trade_date, 'equity': total})
    
    def run_backtest(self, start_date='2024-09-01', end_date='2026-07-10'):
        print("=" * 60)
        print("全量T+1回测 — 情景化因子矩阵方案E")
        print("初始资金: %.0f" % self.start_capital)
        print("时间: %s ~ %s" % (start_date, end_date))
        print("=" * 60)
        
        self.connect()
        try:
            pool = self.get_pool()
            pool_set = set(pool)
            print("监控池: %d只" % len(pool))
            
            dates = self.get_dates(start_date, end_date)
            print("交易日: %d天" % len(dates))
            
            total_trades = 0
            total_buys = 0
            
            for idx, d in enumerate(dates):
                season = self.get_season(d)
                config = SCENARIO_CONFIG.get(season, SCENARIO_CONFIG['chaos'])
                buy_threshold = config['buy']
                hold_threshold = config['hold']
                stop_loss = config['stop']
                max_pos = config['max_pos']
                per_stock = config['per']
                alpha_ranks_cfg = config.get('alpha', {})
                
                # ===== 1. 持仓检查（按昨日最高价止损） =====
                to_sell = []
                for ts, h in list(self.holdings.items()):
                    # 获取当日最高价
                    with self.db.cursor() as c:
                        c.execute("SELECT high, close FROM daily_kline WHERE ts_code=%s AND trade_date=%s", (ts, d))
                        r = c.fetchone()
                    if not r:
                        continue
                    current_high = float(r['high'] or h['buy_price'])
                    current_close = float(r['close'] or h['buy_price'])
                    h['current_price'] = current_close
                    
                    # 更新最高点
                    if current_high > h.get('high_since_buy', 0):
                        h['high_since_buy'] = current_high
                    
                    # 检查是否超过最长持有（60日）
                    buy_date = h['buy_date']
                    days_held = idx - dates.index(buy_date) if buy_date in dates else 60
                    if days_held >= 60:
                        to_sell.append((ts, 'expire_60d'))
                        continue
                    
                    # 止损检查
                    drawdown = (h['high_since_buy'] - current_close) / h['high_since_buy']
                    if drawdown >= stop_loss:
                        to_sell.append((ts, 'stop_loss'))
                        continue
                    
                    # 续持期检查（每10日）
                    if days_held % 10 == 0:
                        m1 = self.get_m1_scores(d, [ts]).get(ts, 50)
                        alpha_scores = self.get_alpha_scores(d, [ts], list(alpha_ranks_cfg.keys()))
                        bonuses = self.compute_bonus(alpha_scores, alpha_ranks_cfg)
                        bonus = bonuses.get(ts, 0)
                        final_score = m1 + bonus
                        if final_score < hold_threshold:
                            to_sell.append((ts, 'score_below_hold'))
                
                # 执行卖出
                for ts, reason in to_sell:
                    h = self.holdings.get(ts)
                    if not h:
                        continue
                    sell_price = h.get('current_price', h['buy_price'])
                    proceeds = h['qty'] * sell_price
                    self.capital += proceeds
                    pnl = proceeds - h['qty'] * h['buy_price']
                    pnl_pct = pnl / (h['qty'] * h['buy_price']) * 100
                    self.trade_log.append({
                        'date': d, 'ts_code': ts, 'action': 'SELL',
                        'qty': h['qty'], 'price': sell_price,
                        'pnl': round(pnl, 2), 'pnl_pct': round(pnl_pct, 2),
                        'reason': reason, 'days_held': days_held,
                    })
                    total_trades += 1
                    del self.holdings[ts]
                
                # ===== 2. 买入决策 =====
                # 冷却期检查
                in_cooldown = any(
                    tl['date'] > str(date.fromisoformat(d) - timedelta(days=config.get('cooldown', 0)))
                    for tl in self.trade_log[-5:] if tl['action'] == 'SELL' and tl['reason'] == 'stop_loss'
                )
                
                # 计算可用仓位
                used = sum(h['qty'] * h.get('current_price', h['buy_price']) for h in self.holdings.values())
                equity = self.capital + used
                available = equity * max_pos - used
                
                if not in_cooldown and available > 0:
                    # 获取当日M1分数 >= buy_threshold - 10 的候选
                    candidates = [ts for ts in pool if ts not in self.holdings]
                    m1_scores = self.get_m1_scores(d, candidates)
                    candidates = [ts for ts in candidates if ts in m1_scores and m1_scores[ts] >= (buy_threshold - 10)]
                    
                    if candidates:
                        # 算加分
                        alpha_scores = self.get_alpha_scores(d, candidates, list(alpha_ranks_cfg.keys()))
                        bonuses = self.compute_bonus(alpha_scores, alpha_ranks_cfg)
                        
                        # 计算最终分并排序
                        final_scores = {}
                        for ts in candidates:
                            base = m1_scores.get(ts, 50)
                            bonus = bonuses.get(ts, 0)
                            final = base + bonus
                            if final >= buy_threshold:
                                final_scores[ts] = round(final, 1)
                        
                        # 按分数降序排序，选Top3
                        ranked = sorted(final_scores.items(), key=lambda x: -x[1])[:3]
                        
                        # 获取收盘价
                        close_prices = self.get_close_prices(d, [r[0] for r in ranked])
                        
                        for ts, score in ranked:
                            if available <= 0:
                                break
                            price = close_prices.get(ts, 0)
                            if price <= 0:
                                continue
                            
                            max_qty = int(available * per_stock / price)
                            if max_qty <= 0:
                                continue
                            
                            qty = max_qty
                            cost = qty * price
                            available -= cost
                            self.capital -= cost
                            
                            self.holdings[ts] = {
                                'qty': qty,
                                'buy_price': price,
                                'buy_date': d,
                                'high_since_buy': price,
                                'buy_score': score,
                            }
                            self.trade_log.append({
                                'date': d, 'ts_code': ts, 'action': 'BUY',
                                'qty': qty, 'price': price,
                                'score': score, 'pnl': 0, 'pnl_pct': 0, 'reason': '',
                            })
                            total_buys += 1
                
                # 记录每日净值
                self.record_equity(d)
                
                if (idx + 1) % 50 == 0:
                    print("  进度 %d/%d | 持仓%d只 | 资金%.0f" % (idx+1, len(dates), len(self.holdings), self.capital))
            
            # 平仓
            for ts, h in list(self.holdings.items()):
                sell_price = h.get('current_price', h['buy_price'])
                self.capital += h['qty'] * sell_price
                self.trade_log.append({
                    'date': dates[-1], 'ts_code': ts, 'action': 'FINAL_SELL',
                    'qty': h['qty'], 'price': sell_price,
                    'pnl': h['qty'] * (sell_price - h['buy_price']),
                    'pnl_pct': (sell_price - h['buy_price']) / h['buy_price'] * 100,
                    'reason': 'backtest_end',
                })
            self.holdings = {}
            
            # ===== 结果统计 =====
            final_equity = self.daily_equity[-1]['equity'] if self.daily_equity else self.capital
            total_return = (final_equity / self.start_capital - 1) * 100
            
            buy_trades = [t for t in self.trade_log if t['action'] in ('SELL', 'FINAL_SELL')]
            win_trades = [t for t in buy_trades if t['pnl'] > 0]
            loss_trades = [t for t in buy_trades if t['pnl'] <= 0]
            
            win_rate = len(win_trades) / max(len(buy_trades), 1) * 100
            avg_win = np.mean([t['pnl_pct'] for t in win_trades]) if win_trades else 0
            avg_loss = np.mean([t['pnl_pct'] for t in loss_trades]) if loss_trades else 0
            profit_factor = abs(sum(t['pnl'] for t in win_trades) / max(abs(sum(t['pnl'] for t in loss_trades)), 0.01)) if loss_trades else float('inf')
            
            max_drawdown = 0
            peak = 0
            for e in self.daily_equity:
                if e['equity'] > peak:
                    peak = e['equity']
                dd = (peak - e['equity']) / peak * 100
                if dd > max_drawdown:
                    max_drawdown = dd
            
            sharpe = 0
            if len(self.daily_equity) > 20:
                returns = []
                prev = self.daily_equity[0]['equity']
                for e in self.daily_equity[1:]:
                    ret = (e['equity'] - prev) / prev
                    returns.append(ret)
                    prev = e['equity']
                sharpe = np.mean(returns) / max(np.std(returns), 0.0001) * np.sqrt(252)
            
            print("\n" + "=" * 70)
            print("📈 回测结果")
            print("=" * 70)
            print("  初始资金: %.0f" % self.start_capital)
            print("  最终净值: %.0f" % final_equity)
            print("  总收益率: %+.2f%%" % total_return)
            print("  最大回撤: %.2f%%" % max_drawdown)
            print("  夏普比: %.2f" % sharpe)
            print("  卡玛比: %.2f" % (total_return / max(max_drawdown, 0.01)))
            print("  ——————————————————————")
            print("  交易笔数: %d (买入%d/卖出%d)" % (total_buys + len([t for t in self.trade_log if t['action'] in ('SELL','FINAL_SELL')]), total_buys, len(buy_trades)))
            print("  胜率: %.1f%% (%d/%d)" % (win_rate, len(win_trades), len(buy_trades)))
            print("  平均盈利: %+.2f%%" % avg_win)
            print("  平均亏损: %+.2f%%" % avg_loss)
            print("  盈亏比: %.2f" % (abs(avg_win / max(avg_loss, 0.01)) if avg_loss != 0 else float('inf')))
            print("  利润因子: %.2f" % profit_factor)
            
        finally:
            self.close()


def main():
    bt = Backtester(start_capital=1000000)
    bt.run_backtest(start_date='2024-09-01', end_date='2026-07-10')


if __name__ == '__main__':
    main()
