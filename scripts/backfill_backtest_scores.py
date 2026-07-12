#!/usr/bin/env python3
"""
P6全量历史评分回填 + 逐日T+1真实回测
========================================
目标：
1. 对监控池全部842只股票，逐日调用P6双轨引擎评分
2. 写入backtest_score_daily表（补齐222只新票的历史评分）
3. 用逐日模拟+T+1规则跑真实回测

字段映射（P6引擎score_stock输出 → backtest_score_daily）：
  score → composite_score
  calibrated_score（由calibrate_scores添加）
  details.chanlun_trend → chanlun_trend
  details.momentum_raw → momentum_score
  details.mf_score → mf_score
  外部查daily_kline → close_price

使用方式:
  python3 backfill_backtest_scores.py           # 1. 补评分数据
  python3 backfill_backtest_scores.py --backtest # 2. 补完自动跑回测
  python3 backfill_backtest_scores.py --all      # 3. 一步到位
"""
import sys, os, time, json, argparse
from datetime import date, datetime, timedelta
from collections import defaultdict

# 代码实现目录
CODE_DIR = '/root/.openclaw/workspace/projects/陶的投资预测模型项目/代码实现'
sys.path.insert(0, CODE_DIR)

from p6_dual_track_engine import batch_score, MarketContext, calibrate_scores
from season_engine import SeasonEngine, save_result_to_db as save_season
from db_config import get_connection
import warnings; warnings.filterwarnings("ignore")

LOG_FILE = '/tmp/backfill_backtest_score_daily.log'

def log(msg):
    ts = datetime.now().strftime('%H:%M:%S')
    line = f'[{ts}] {msg}'
    print(line, flush=True)
    with open(LOG_FILE, 'a') as f:
        f.write(line + '\n')

def get_trade_dates(from_date='2024-09-02', to_date='2026-07-03'):
    """获取所有交易日"""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT trade_date FROM daily_kline
        WHERE trade_date >= %s AND trade_date <= %s
        ORDER BY trade_date
    """, (from_date, to_date))
    dates = [list(r.values())[0] for r in cur.fetchall()]
    cur.close(); conn.close()
    return dates

def get_watch_pool_codes():
    """获取监控池全部股票"""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT ts_code FROM watch_pool WHERE is_active=1")
    codes = [list(r.values())[0] for r in cur.fetchall()]
    cur.close(); conn.close()
    return codes

def preload_close_prices(from_date, to_date):
    """预加载每日收盘价"""
    log("📥 预加载收盘价...")
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT ts_code, trade_date, close FROM daily_kline
        WHERE trade_date >= %s AND trade_date <= %s
        AND ts_code IN (SELECT ts_code FROM watch_pool WHERE is_active=1)
    """, (from_date, to_date))
    prices = defaultdict(dict)
    for r in cur.fetchall():
        prices[r['ts_code']][str(r['trade_date'])] = float(r['close'])
    cur.close(); conn.close()
    log(f"✅ 收盘价预加载完成: {sum(len(v) for v in prices.values())}条")
    return prices

def get_already_scored(trade_date):
    """获取某日已评分股票集"""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT ts_code FROM backtest_score_daily WHERE trade_date=%s", (trade_date,))
    result = set(list(r.values())[0] for r in cur.fetchall())
    cur.close(); conn.close()
    return result

def backfill_scores(from_date='2024-09-02', to_date='2026-07-03'):
    """
    用P6双轨引擎逐日评分，补齐backtest_score_daily
    """
    all_dates = get_trade_dates(from_date, to_date)
    all_codes = get_watch_pool_codes()
    total_codes = len(all_codes)
    
    log(f"📋 待评分: {total_codes}只股票 × {len(all_dates)}个交易日")
    log(f"  日期范围: {all_dates[0]} ~ {all_dates[-1]}")
    
    # 预加载收盘价
    price_cache = preload_close_prices(from_date, to_date)
    
    # 确保表存在
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS backtest_score_daily (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            ts_code VARCHAR(16) NOT NULL,
            trade_date DATE NOT NULL,
            track VARCHAR(16),
            composite_score DECIMAL(5,1),
            calibrated_score DECIMAL(5,1),
            chanlun_trend DECIMAL(5,1),
            structure_score DECIMAL(5,1),
            momentum_score DECIMAL(5,1),
            pos_score DECIMAL(5,1),
            mf_score DECIMAL(5,1),
            margin_score DECIMAL(5,1),
            season VARCHAR(16),
            close_price DECIMAL(12,3),
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uk_code_date (ts_code, trade_date),
            KEY idx_date (trade_date),
            KEY idx_code (ts_code),
            KEY idx_season (season)
        )
    """)
    conn.commit()
    cur.close()
    conn.close()
    log("✅ backtest_score_daily 表已确认")
    
    total_processed = 0
    total_skipped = 0
    t0 = time.time()
    
    for idx, td in enumerate(all_dates):
        td_str = str(td)
        
        # 检查该日已有评分数（跳过已完成的）
        already = get_already_scored(td_str)
        need_codes = [c for c in all_codes if c not in already]
        
        if not need_codes:
            total_skipped += total_codes
            if (idx + 1) % 50 == 0:
                elapsed = time.time() - t0
                rate = total_processed / elapsed if elapsed > 0 else 0
                log(f"  [{idx+1}/{len(all_dates)}] ✅ 已全部评分 {td_str} (+0, 累计{total_processed}条, {elapsed:.0f}s, {rate:.1f}条/s)")
            continue
        
        # 季节判定
        try:
            engine = SeasonEngine()
            judge_result = engine.judge_market_season()
            ctx = MarketContext(judge_result)
            try:
                save_season(judge_result)
            except:
                pass
        except Exception as e:
            log(f"  ⚠️ 跳过 {td_str} (季节判定失败: {e})")
            total_skipped += len(need_codes)
            continue
        
        # 强制覆盖评分日期
        ctx.trade_date = td_str
        
        # 用batch_score批量评分（内部已调calibrate_scores）
        try:
            results = batch_score(need_codes, ctx)
        except Exception as e:
            log(f"  ❌ batch_score失败 {td_str}: {e}")
            total_skipped += len(need_codes)
            continue
        
        if not results:
            total_skipped += len(need_codes)
            continue
        
        # 批量写入
        inserted = 0
        try:
            conn2 = get_connection()
            cur2 = conn2.cursor()
            for r in results:
                dt = r.get('details', {})
                code = r['ts_code']
                close_price = price_cache.get(code, {}).get(td_str, 0)
                
                cur2.execute("""
                    INSERT INTO backtest_score_daily
                        (ts_code, trade_date, track, composite_score, calibrated_score,
                         chanlun_trend, structure_score, momentum_score,
                         pos_score, mf_score, margin_score, season, close_price)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        composite_score=VALUES(composite_score),
                        calibrated_score=VALUES(calibrated_score)
                """, (
                    code,
                    td_str,
                    dt.get('track', r.get('track', ctx.scoring_strategy)),
                    round(float(r.get('score', 50)), 1),
                    round(float(r.get('calibrated_score', r.get('score', 50))), 1),
                    round(float(dt.get('chanlun_trend', 50)), 1),
                    0,
                    round(float(dt.get('momentum_raw', 50)), 1),
                    round(float(dt.get('pos_score', 0)), 1),
                    round(float(dt.get('mf_score', 50)), 1),
                    0,
                    ctx.season if hasattr(ctx, 'season') else 'unknown',
                    round(float(close_price), 3)
                ))
                inserted += 1
            conn2.commit()
            cur2.close()
            conn2.close()
            total_processed += inserted
        except Exception as e:
            log(f"  ❌ 入库失败 {td_str}: {e}")
            total_skipped += len(need_codes)
            continue
        
        if (idx + 1) % 10 == 0:
            elapsed = time.time() - t0
            rate = total_processed / elapsed if elapsed > 0 else 0
            log(f"  [{idx+1}/{len(all_dates)}] {td_str}: +{inserted}条 (累计{total_processed}条, {elapsed:.0f}s, {rate:.1f}条/s)")
        
        time.sleep(0.25)  # 防Tushare限频
    
    elapsed = time.time() - t0
    log(f"\n{'='*50}")
    log(f"🏁 评分回填完成!")
    log(f"   处理交易日: {len(all_dates)}天")
    log(f"   写入评分: {total_processed}条")
    log(f"   跳过(已有): {total_skipped}条")
    log(f"   总耗时: {elapsed:.0f}s ({elapsed/60:.1f}min)")
    log(f"{'='*50}")
    return total_processed


def run_backtest():
    """
    逐日真实回测（T+1规则 + 真实日K线逐日模拟）
    
    规则（V13方案C）：
    - 买入：评分>=买入线（分季节），收盘价买入
    - T+1：买入次日才能卖出
    - 止损：最高点回撤10%硬止损 + 固定阈值止损（T2更严）
    - 持有期：分季节（春夏60d/20d/25d/15d/10d）
    - 单票上限20%，总仓8只
    """
    THRESHOLDS = {
        'summer':       {'buy':65, 'sell_t1':-8, 'sell_t2':-6, 'hold_max':60, 'pullback_stop':-10},
        'spring':       {'buy':65, 'sell_t1':-8, 'sell_t2':-6, 'hold_max':20, 'pullback_stop':-10},
        'chaos_spring': {'buy':72, 'sell_t1':-8, 'sell_t2':-6, 'hold_max':20, 'pullback_stop':-10},
        'chaos':        {'buy':72, 'sell_t1':-10,'sell_t2':-8, 'hold_max':25, 'pullback_stop':-10},
        'chaos_autumn': {'buy':80, 'sell_t1':-5, 'sell_t2':-4, 'hold_max':15, 'pullback_stop':-8},
        'autumn':       {'buy':80, 'sell_t1':-5, 'sell_t2':-4, 'hold_max':15, 'pullback_stop':-8},
        'winter':       {'buy':80, 'sell_t1':-5, 'sell_t2':-4, 'hold_max':10, 'pullback_stop':-8},
        'panic':        {'buy':999,'sell_t1':-5, 'sell_t2':-4, 'hold_max':0,  'pullback_stop':-8},
        'recovery':     {'buy':65, 'sell_t1':-8, 'sell_t2':-6, 'hold_max':20, 'pullback_stop':-10},
    }
    
    INITIAL_CAPITAL = 1_000_000
    MAX_POSITIONS = 8
    MAX_SINGLE = 0.20
    COMMISSION_PCT = 0.0008  # 万8（买卖双向）
    
    log("\n" + "="*60)
    log("📊 启动逐日T+1真实回测 (V13方案C)")
    log("="*60)
    
    # 读取全量评分 + K线
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT d.ts_code, d.composite_score, d.season, d.trade_date,
               k.close, k.high, k.low, k.vol
        FROM backtest_score_daily d
        JOIN daily_kline k ON d.ts_code = k.ts_code AND d.trade_date = k.trade_date
        WHERE d.trade_date >= '2024-09-02' AND d.trade_date <= '2026-07-03'
        ORDER BY d.trade_date
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    log(f"📋 读取评分+K线数据: {len(rows)}条")
    
    # 按日期组织
    daily_data = defaultdict(list)
    for r in rows:
        td = str(r['trade_date'])
        daily_data[td].append({
            'ts_code': r['ts_code'],
            'score': float(r['composite_score']) if r['composite_score'] else 0,
            'season': r['season'] or 'chaos',
            'close': float(r['close']) if r['close'] else 0,
            'high': float(r['high']) if r['high'] else 0,
            'low': float(r['low']) if r['low'] else 0,
            'vol': float(r['vol']) if r['vol'] else 0,
        })
    
    all_dates = sorted(daily_data.keys())
    log(f"📅 交易日: {all_dates[0]} ~ {all_dates[-1]} ({len(all_dates)}天)")
    
    # 建立K线价格缓存
    price_cache = defaultdict(dict)
    for td, stocks in daily_data.items():
        for s in stocks:
            price_cache[s['ts_code']][td] = {
                'close': s['close'],
                'high': s['high'],
                'low': s['low']
            }
    
    # === 回测主循环 ===
    cash = INITIAL_CAPITAL
    positions = {}  # code -> {buy_date, buy_price, shares, cost, high_water_mark, days_held, season, score}
    trades = []
    equity_curve = []
    
    t0 = time.time()
    
    for idx, today in enumerate(all_dates):
        if (idx + 1) % 100 == 0:
            log(f"  ⏳ 回测进度: {idx+1}/{len(all_dates)} ({today})")
        
        stocks_today = daily_data[today]
        
        # === 1. 检查持仓（止损/到期） ===
        to_close = []
        for code, pos in list(positions.items()):
            pos['days_held'] += 1
            
            price_info = price_cache.get(code, {}).get(today, {})
            current_price = price_info.get('close', 0)
            if current_price == 0:
                continue
            
            # 更新高水位
            if current_price > pos.get('high_water_mark', pos['buy_price']):
                pos['high_water_mark'] = current_price
            
            # 季节配置
            sea = pos.get('season', 'chaos')
            th = THRESHOLDS.get(sea, THRESHOLDS['chaos'])
            
            # T+1：买入次日才能卖
            if pos['days_held'] <= 1:
                continue
            
            # A. 最高点回撤止损
            hwm = pos.get('high_water_mark', pos['buy_price'])
            drawdown = (current_price - hwm) / hwm * 100
            if drawdown <= th['pullback_stop']:
                to_close.append((code, current_price, f'回撤{drawdown:.1f}%'))
                continue
            
            # B. 固定止损线
            pnl_pct = (current_price - pos['buy_price']) / pos['buy_price'] * 100
            stop_line = th['sell_t2'] if pos['days_held'] >= 2 else th['sell_t1']
            if pnl_pct <= stop_line:
                to_close.append((code, current_price, f'止损{stop_line}%({pnl_pct:.1f}%)'))
                continue
            
            # C. 持有期到期
            if pos['days_held'] >= th['hold_max']:
                to_close.append((code, current_price, f'到期{pos["days_held"]}d'))
                continue
        
        # 执行卖出
        for code, price, reason in to_close:
            pos = positions.pop(code)
            proceeds = pos['shares'] * price * (1 - COMMISSION_PCT)
            pnl = proceeds - pos['cost']
            pnl_pct = pos['pnl_pct'] if 'pnl_pct' in pos else (price - pos['buy_price']) / pos['buy_price'] * 100
            cash += proceeds
            
            trades.append({
                'ts_code': code, 'type': 'SELL',
                'buy_date': pos['buy_date'], 'sell_date': today,
                'buy_price': pos['buy_price'], 'sell_price': price,
                'shares': pos['shares'], 'pnl': round(pnl, 2),
                'pnl_pct': round(pnl_pct, 2),
                'days_held': pos['days_held'], 'reason': reason,
                'season': pos.get('season', '?'),
                'score_at_buy': pos.get('score_at_buy', 0),
            })
        
        # === 2. 买入信号 ===
        if len(positions) < MAX_POSITIONS:
            season_today = stocks_today[0]['season'] if stocks_today else 'chaos'
            th = THRESHOLDS.get(season_today, THRESHOLDS['chaos'])
            
            if th['buy'] < 999:
                candidates = [s for s in stocks_today
                             if s['score'] >= th['buy']
                             and s['close'] > 0
                             and s['ts_code'] not in positions]
                candidates.sort(key=lambda x: x['score'], reverse=True)
                
                max_slots = MAX_POSITIONS - len(positions)
                per_slot = cash * MAX_SINGLE
                
                for s in candidates[:max_slots]:
                    if cash <= 0:
                        break
                    buy_amt = min(cash * MAX_SINGLE, per_slot)
                    if buy_amt < 10000:
                        continue
                    shares = int(buy_amt / s['close'] / 100) * 100
                    if shares <= 0:
                        continue
                    cost = shares * s['close'] * (1 + COMMISSION_PCT)
                    if cost > cash:
                        continue
                    
                    positions[s['ts_code']] = {
                        'buy_date': today, 'buy_price': s['close'],
                        'shares': shares, 'cost': cost,
                        'high_water_mark': s['close'], 'days_held': 0,
                        'season': s['season'], 'score_at_buy': s['score'],
                        'pnl_pct': 0,
                    }
                    cash -= cost
        
        # 净值记录
        pos_value = 0
        for code, pos in positions.items():
            lp = price_cache.get(code, {}).get(today, {})
            p = lp.get('close', pos['buy_price'])
            if p:
                pos_value += pos['shares'] * p
        equity_curve.append({
            'date': today, 'cash': round(cash, 2),
            'pos_value': round(pos_value, 2),
            'total': round(cash + pos_value, 2),
            'pos_count': len(positions),
        })
    
    elapsed = time.time() - t0
    
    # === 3. 期末强制平仓 ===
    final_pos_value = 0
    last_date = all_dates[-1]
    for code, pos in list(positions.items()):
        lp = price_cache.get(code, {}).get(last_date, {}).get('close', pos['buy_price'])
        final_pos_value += pos['shares'] * lp
        proceeds = pos['shares'] * lp * (1 - COMMISSION_PCT)
        trades.append({
            'ts_code': code, 'type': 'FORCE_SELL',
            'buy_date': pos['buy_date'], 'sell_date': last_date,
            'buy_price': pos['buy_price'], 'sell_price': lp,
            'shares': pos['shares'],
            'pnl': round(proceeds - pos['cost'], 2),
            'pnl_pct': round((lp - pos['buy_price']) / pos['buy_price'] * 100, 2),
            'days_held': pos['days_held'], 'reason': '期末强平',
            'season': pos.get('season', '?'),
            'score_at_buy': pos.get('score_at_buy', 0),
        })
    
    total_value = cash + final_pos_value
    total_return = (total_value - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100
    
    # 指标计算
    close_trades = [t for t in trades if t['type'] == 'SELL']
    win_trades = [t for t in close_trades if t['pnl'] > 0]
    loss_trades = [t for t in close_trades if t['pnl'] <= 0]
    
    win_rate = len(win_trades) / len(close_trades) * 100 if close_trades else 0
    total_profit = sum(t['pnl'] for t in win_trades)
    total_loss = abs(sum(t['pnl'] for t in loss_trades))
    profit_factor = total_profit / total_loss if total_loss > 0 else 0
    
    avg_win = total_profit / len(win_trades) if win_trades else 0
    avg_loss = total_loss / len(loss_trades) if loss_trades else 0
    avg_hold = sum(t['days_held'] for t in close_trades) / len(close_trades) if close_trades else 0
    
    # 最大回撤
    max_drawdown = 0
    peak = equity_curve[0]['total'] if equity_curve else INITIAL_CAPITAL
    for e in equity_curve:
        v = e['total']
        if v > peak:
            peak = v
        dd = (peak - v) / peak * 100
        if dd > max_drawdown:
            max_drawdown = dd
    
    # 分季节统计
    season_stats = defaultdict(lambda: {'trades': 0, 'wins': 0, 'pnl': 0, 'pnl_pct': 0})
    for t in close_trades:
        s = t.get('season', '?')
        season_stats[s]['trades'] += 1
        if t['pnl'] > 0:
            season_stats[s]['wins'] += 1
        season_stats[s]['pnl'] += t['pnl']
    
    # 输出
    log("\n" + "="*60)
    log("🏆 逐日T+1真实回测结果 (V13方案C)")
    log("="*60)
    log(f"  回测区间: {all_dates[0]} ~ {all_dates[-1]} ({len(all_dates)}天)")
    log(f"  初始本金: {INITIAL_CAPITAL/10000:.0f}万")
    log(f"  最终净值: {total_value/10000:.2f}万")
    log(f"  总收益率: {total_return:+.2f}%")
    log(f"  年化收益: {total_return/len(all_dates)*252:+.2f}%")
    log(f"  最大回撤: {max_drawdown:.2f}%")
    log(f"  ────────────────")
    log(f"  总交易笔数: {len(close_trades)}")
    log(f"  盈利笔数: {len(win_trades)} ({win_rate:.1f}%)")
    log(f"  亏损笔数: {len(loss_trades)} ({100-win_rate:.1f}%)")
    log(f"  盈亏比: {avg_win/avg_loss:.2f}" if avg_loss > 0 else "  盈亏比: ∞")
    log(f"  盈利因子: {profit_factor:.2f}")
    log(f"  平均持有: {avg_hold:.1f}天")
    log(f"  最大持仓: {MAX_POSITIONS}只 | 单票上限{MAX_SINGLE*100:.0f}%")
    log(f"  ────────────────")
    log(f"  计算用时: {elapsed:.0f}s")
    
    # 季节收益
    log("\n📅 分季节统计:")
    for s in sorted(season_stats.keys()):
        st = season_stats[s]
        wr = st['wins']/st['trades']*100 if st['trades'] > 0 else 0
        log(f"    {s:15s}: {st['trades']:3d}笔 胜率{wr:5.1f}%  收益{st['pnl']/10000:+.2f}万")
    
    # 保存回测结果
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS backtest_results (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                run_date DATETIME, label VARCHAR(64),
                start_date DATE, end_date DATE,
                initial_capital DECIMAL(15,2), final_value DECIMAL(15,2),
                total_return DECIMAL(7,2), max_drawdown DECIMAL(7,2),
                win_rate DECIMAL(5,1), profit_factor DECIMAL(7,2),
                total_trades INT, avg_hold_days DECIMAL(5,1),
                params_json TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        params = {
            'buy_thresholds': {k: v['buy'] for k,v in THRESHOLDS.items()},
            'hold_max': {k: v['hold_max'] for k,v in THRESHOLDS.items()},
            'sell_stops': {k: f"{v['sell_t1']}/{v['sell_t2']}" for k,v in THRESHOLDS.items()},
            'max_positions': MAX_POSITIONS, 'max_single_pct': MAX_SINGLE,
            't_plus_one': True, 'daily_simulation': True,
        }
        cur.execute("""
            INSERT INTO backtest_results
                (run_date, label, start_date, end_date,
                 initial_capital, final_value, total_return,
                 max_drawdown, win_rate, profit_factor,
                 total_trades, avg_hold_days, params_json)
            VALUES (NOW(), 'P6_逐日T+1_V13方案C', %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (all_dates[0], all_dates[-1],
              INITIAL_CAPITAL, total_value, round(total_return, 2),
              round(max_drawdown, 2), round(win_rate, 1), round(profit_factor, 2),
              len(close_trades), round(avg_hold, 1), json.dumps(params)))
        conn.commit()
        cur.close()
        conn.close()
        log("✅ 回测结果已保存到 backtest_results 表")
    except Exception as e:
        log(f"⚠️ 保存回测结果失败: {e}")
    
    # 打印3笔最佳/最差交易
    sorted_trades = sorted(close_trades, key=lambda t: t.get('pnl_pct', 0))
    log("\n📈 最佳3笔:")
    for t in sorted_trades[-3:]:
        log(f"    {t['ts_code']}: {t['pnl_pct']:+.1f}% ({t['days_held']}d) [{t['season']}] s={t.get('score_at_buy',0)}")
    log("\n📉 最差3笔:")
    for t in sorted_trades[:3]:
        log(f"    {t['ts_code']}: {t['pnl_pct']:+.1f}% ({t['days_held']}d) [{t['season']}] s={t.get('score_at_buy',0)}")
    
    return {
        'total_return': round(total_return, 2),
        'max_drawdown': round(max_drawdown, 2),
        'win_rate': round(win_rate, 1),
        'profit_factor': round(profit_factor, 2),
        'total_trades': len(close_trades),
        'avg_hold': round(avg_hold, 1),
    }


def main():
    ap = argparse.ArgumentParser(description='P6全量历史评分回填 + 逐日T+1回测')
    ap.add_argument('--backtest', action='store_true', help='评分完后自动跑回测')
    ap.add_argument('--all', action='store_true', help='评分+回测一步到位')
    ap.add_argument('--from-date', default='2024-09-02')
    ap.add_argument('--to-date', default='2026-07-03')
    ap.add_argument('--only-backtest', action='store_true', help='只跑回测不补评分')
    args = ap.parse_args()
    
    overall_t0 = time.time()
    
    if not args.only_backtest:
        log("="*60)
        log("🚀 Phase 1: P6全量历史评分回填（842只×442日）")
        log("="*60)
        backfill_scores(args.from_date, args.to_date)
    
    if args.backtest or args.all or args.only_backtest:
        log("\n" + "="*60)
        log("🚀 Phase 2: 逐日T+1真实回测")
        log("="*60)
        run_backtest()
    
    total_elapsed = time.time() - overall_t0
    log(f"\n🏁 全部完成! 总耗时: {total_elapsed:.0f}s ({total_elapsed/60:.1f}min)")


if __name__ == '__main__':
    main()
