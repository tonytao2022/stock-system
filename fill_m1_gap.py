#!/usr/bin/env python3
"""
补回M1缺失的10天（2026-06-01 ~ 2026-06-12）
v3: 每次评分新建独立连接，避免p6引擎内部close冲突
"""
import sys, time, pymysql, numpy as np
sys.path.insert(0, '/opt/stock-analyzer')
import warnings
warnings.filterwarnings("ignore")

DB = {'host':'127.0.0.1','port':3306,'user':'debian-sys-maint',
      'password':'iXve1rVBXfdA4tL9','database':'stock_db_v2',
      'charset':'utf8mb4','connect_timeout':10,'read_timeout':300,'write_timeout':300,
      'autocommit':True,'cursorclass':pymysql.cursors.DictCursor}

TOTAL_MISSING = ['2025-02-18','2025-02-19','2025-02-20','2025-02-21','2025-02-24',
               '2025-02-25','2025-02-26','2025-02-27','2025-02-28',
               '2026-06-01','2026-06-02','2026-06-03','2026-06-04','2026-06-05',
               '2026-06-08','2026-06-09','2026-06-10','2026-06-11','2026-06-12']


def build_judge_result(trade_date):
    conn = pymysql.connect(**DB); cur = conn.cursor()
    
    # HS300 5日趋势
    cur.execute("""
        SELECT close FROM daily_kline
        WHERE ts_code='000300.SH' AND trade_date <= %s AND trade_date >= DATE_SUB(%s, INTERVAL 10 DAY)
        ORDER BY trade_date DESC LIMIT 5
    """, (trade_date, trade_date))
    closes = [float(r['close']) for r in cur.fetchall()]
    hs300_trend = (closes[0] - closes[-1]) / closes[-1] if len(closes) >= 5 else 0.0
    
    # HS300 当日涨跌
    cur.execute("""
        SELECT (c.close - pc.close) / pc.close as chg_pct
        FROM daily_kline c
        JOIN daily_kline pc ON pc.ts_code=c.ts_code
            AND pc.trade_date=(SELECT MAX(trade_date) FROM daily_kline WHERE trade_date<%s AND ts_code='000300.SH')
        WHERE c.trade_date=%s AND c.ts_code='000300.SH'
    """, (trade_date, trade_date))
    r = cur.fetchone()
    hs300_chg = float(r['chg_pct']) if r else 0.0
    
    # 涨跌家数
    cur.execute("""
        SELECT SUM(CASE WHEN close > open THEN 1 ELSE 0 END) as up, COUNT(*) as total
        FROM daily_kline WHERE trade_date=%s
    """, (trade_date,))
    r = cur.fetchone()
    rs_signal = r['up'] / r['total'] if r and r['total'] > 0 else 0.5
    
    # 波动率
    cur.execute("""
        SELECT close FROM daily_kline WHERE ts_code='000300.SH' AND trade_date <= %s
        ORDER BY trade_date DESC LIMIT 20
    """, (trade_date,))
    h_closes = [float(c['close']) for c in cur.fetchall()]
    if len(h_closes) >= 10:
        returns = [(h_closes[i] - h_closes[i+1]) / h_closes[i+1] for i in range(len(h_closes)-1)]
        volatility = min(100, max(0, np.std(returns) * 100 * 3))
    else:
        volatility = 50
    
    # 行业轮动
    cur.execute("""
        SELECT sb.industry, STD(c.return_r) as vol
        FROM (SELECT ts_code, (close - open) / open as return_r FROM daily_kline WHERE trade_date=%s) c
        JOIN stock_basic sb ON sb.ts_code=c.ts_code
        WHERE sb.industry IS NOT NULL
        GROUP BY sb.industry
    """, (trade_date,))
    ind_vols = [float(r['vol']) for r in cur.fetchall()]
    industry_rotation = np.std(ind_vols) if len(ind_vols) > 5 else 0.5
    
    # 季节推断
    if hs300_trend > 0.015:
        season = 'spring' if hs300_trend < 0.05 else 'summer'
        regime = 'bull'; conf = min(0.8, max(0.3, abs(hs300_trend)*5))
    elif hs300_trend < -0.015:
        season = 'autumn' if hs300_trend > -0.04 else 'winter'
        regime = 'bear'; conf = min(0.8, max(0.3, abs(hs300_trend)*5))
    else:
        season = 'chaos'; regime = 'range'; conf = 0.4
    
    # 从season_state取参考
    cur.execute("SELECT season, hengjiyuan_level, confidence FROM season_state ORDER BY trade_date DESC LIMIT 1")
    last_season = cur.fetchone()
    if last_season and last_season['season'] in ('spring','summer','chaos','autumn','winter'):
        season = last_season['season']
        if last_season['hengjiyuan_level'] and last_season['hengjiyuan_level'] != '?':
            regime = last_season['hengjiyuan_level']
        conf = min(1.0, conf * 0.5 + float(last_season['confidence'] or 0.5) * 0.5)
    
    scoring_strategy = 'momentum' if regime in ('bull', 'range') and season in ('summer', 'spring', 'chaos') else 'reversion'
    
    cur.close(); conn.close()
    
    return {
        'trade_date': trade_date,
        'market_season': season,
        'market_regime': regime,
        'market_confidence': conf,
        'market_scoring_strategy': scoring_strategy,
        'rs_signal': rs_signal,
        'volatility': volatility,
        'industry_rotation': industry_rotation,
        'hs300_chg': hs300_chg,
        'hs300_trend': hs300_trend
    }


def main():
    t0 = time.time()
    from p6_dual_track_engine import score_stock, MarketContext
    
    # 获取股票列表
    conn = pymysql.connect(**DB); cur = conn.cursor()
    cur.execute("SELECT DISTINCT ts_code FROM strategy_signal WHERE trade_date='2026-07-10'")
    all_codes = [r['ts_code'] for r in cur.fetchall()]
    cur.close(); conn.close()
    
    print(f"📊 待补股票: {len(all_codes)}只")
    MISSING_DATES = TOTAL_MISSING
    print(f"📅 缺失天数: {len(MISSING_DATES)}天 ({MISSING_DATES[0]} ~ {MISSING_DATES[-1]})")
    print()
    
    total_inserted = 0
    fail_dates = []
    
    for di, td in enumerate(MISSING_DATES):
        print(f"[{di+1}/{len(MISSING_DATES)}] 📅 {td}...", end=' ', flush=True)
        t1 = time.time()
        
        try:
            judge_result = build_judge_result(td)
            ctx = MarketContext(judge_result)
        except Exception as e:
            print(f"❌ ctx: {e}")
            fail_dates.append(td)
            continue
        
        # 每只评分都新建独立连接（score_stock内部会close连接）
        inserted = 0
        skip_all = False
        
        for ci, code in enumerate(all_codes):
            try:
                r = score_stock(code, ctx)
                score = r.get('score', r.get('composite_score', r.get('raw_score', 0)))
                if score is None or score == 0:
                    continue
                
                # 独立连接写DB
                wc = pymysql.connect(**DB); wcur = wc.cursor()
                wcur.execute("""
                    INSERT INTO bt_m1_score (ts_code, trade_date, m1_score)
                    VALUES (%s, %s, %s)
                    ON DUPLICATE KEY UPDATE m1_score=VALUES(m1_score)
                """, (code, td, float(score)))
                wcur.close(); wc.close()
                inserted += 1
                    
            except Exception as e:
                if ci == 0:
                    print(f"⚠️ 首只{str(e)[:40]}", end=' ')
                    skip_all = True
                    break
                continue
            
            if skip_all:
                break
        
        dt = time.time() - t1
        total_inserted += inserted
        if not skip_all:
            print(f"{inserted}只 ({dt:.0f}s)")
        else:
            fail_dates.append(td)
    
    print(f"\n{'='*55}")
    print(f"  ✅ 完成")
    print(f"  插入: {total_inserted}条")
    if fail_dates:
        print(f"  ❌ 失败: {fail_dates}")
    print(f"  耗时: {time.time()-t0:.0f}s")


if __name__ == '__main__':
    main()
