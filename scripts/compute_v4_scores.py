#!/usr/bin/env python3
"""
V4全量评分重算 - 逐日调用batch_score
从backtest_score_daily取出所有交易日，对监控池股票用V4引擎重算
结果写入 backtest_score_v4 表
"""
import sys, os, time, json
from datetime import date, datetime
from collections import defaultdict

sys.path.insert(0, '/root/.openclaw/workspace/projects/陶的投资预测模型项目/代码实现')
from p6_dual_track_engine import batch_score, MarketContext, calibrate_scores
from season_engine import SeasonEngine
from db_config import get_connection
import warnings; warnings.filterwarnings("ignore")

LOG_FILE = '/tmp/v4_backtest_score.log'

def log(msg):
    ts = datetime.now().strftime('%H:%M:%S')
    line = f'[{ts}] {msg}'
    print(line, flush=True)
    with open(LOG_FILE, 'a') as f:
        f.write(line + '\n')

def get_trade_dates():
    """获取回测用交易日（从daily_kline取监控池+backtest_pool的交易日）"""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT trade_date FROM daily_kline_qfq
        WHERE ts_code IN (SELECT ts_code FROM watch_pool WHERE is_active=1)
        AND trade_date >= '2024-01-01' AND trade_date <= '2026-06-09'
        ORDER BY trade_date
    """)
    dates = [str(r['trade_date']) for r in cur.fetchall()]
    cur.close(); conn.close()
    return dates

def get_pool_codes():
    """获取监控池+回测池的股票代码"""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT ts_code FROM watch_pool WHERE is_active=1")
    codes = [r['ts_code'] for r in cur.fetchall()]
    cur.close(); conn.close()
    return codes

def create_v4_table():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS backtest_score_v4 (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            ts_code VARCHAR(16) NOT NULL,
            trade_date DATE NOT NULL,
            total_score DECIMAL(5,1),
            track VARCHAR(20),
            trend_score DECIMAL(5,1),
            momentum_score DECIMAL(5,1),
            mf_score DECIMAL(5,1),
            hs300_trend DECIMAL(5,4),
            vol_ratio DECIMAL(6,2),
            filtered TINYINT DEFAULT 0,
            filter_reason VARCHAR(100),
            close_price DECIMAL(12,3),
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uk_code_date (ts_code, trade_date),
            KEY idx_date (trade_date),
            KEY idx_code (ts_code)
        )
    """)
    conn.commit(); cur.close(); conn.close()
    log('✅ backtest_score_v4 表已创建/确认存在')

def run_fullV4():
    create_v4_table()
    
    # 交易日 + 监控池
    all_dates = get_trade_dates()
    pool_codes = get_pool_codes()
    
    log(f'交易日: {all_dates[0]} ~ {all_dates[-1]}, 共{len(all_dates)}天')
    log(f'监控池: {len(pool_codes)}只')
    
    t0 = time.time()
    total_saved = 0
    skipped_days = 0
    engine = SeasonEngine()
    
    # 分段处理，每30天报一次进度
    for i, td in enumerate(all_dates):
        try:
            # Season判定
            judge = engine.judge_market_season(target_date=date.fromisoformat(td))
            ctx = MarketContext(judge)
            
            # V4批量评分
            results = batch_score(pool_codes, ctx)
            
            # 沪深300趋势
            hs300_t = ctx.get_hs300_trend()
            
            # 入库
            conn = get_connection()
            cur = conn.cursor()
            
            for r in results:
                code = r['ts_code']
                score = r['score']
                track = r.get('track', '')
                details = r.get('details', {})
                
                td_score = details.get('chanlun_trend', details.get('structure_factor', 50))
                mo_score = details.get('momentum_raw', details.get('oversold_factor', 50))
                mf = details.get('mf_score', 50)
                
                calib = r.get('calibrated_score', score)
                
                # 过滤标记
                filtered = 1 if r.get('_filtered') else 0
                filter_reason = r.get('_filter_reasons', '')
                
                cur.execute("""
                    INSERT INTO backtest_score_v4 
                        (ts_code, trade_date, total_score, track, 
                         trend_score, momentum_score, mf_score,
                         hs300_trend, vol_ratio, filtered, filter_reason, close_price)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        total_score=VALUES(total_score), track=VALUES(track),
                        trend_score=VALUES(trend_score), momentum_score=VALUES(momentum_score),
                        mf_score=VALUES(mf_score), hs300_trend=VALUES(hs300_trend),
                        vol_ratio=VALUES(vol_ratio), filtered=VALUES(filtered),
                        filter_reason=VALUES(filter_reason)
                """, (code, td, round(calib, 1), track,
                      round(td_score, 1), round(mo_score, 1), round(mf, 1),
                      round(hs300_t, 4), 0.0, filtered, filter_reason, 0))
            
            conn.commit()
            total_saved += len(results)
            cur.close(); conn.close()
            
        except Exception as e:
            skipped_days += 1
            if skipped_days <= 5:
                log(f'  ⚠️ {td} 失败: {str(e)[:80]}')
        
        if (i+1) % 30 == 0:
            el = time.time()-t0
            pct = (i+1)/len(all_dates)*100
            rate = total_saved/(el+0.1)
            log(f'  [{i+1}/{len(all_dates)}] {pct:.0f}% | 已入库{total_saved}条 | 跳过{skipped_days}天 | {rate:.0f}条/秒 | 耗时{el:.0f}s')
    
    el = time.time()-t0
    log(f'\n{"="*50}')
    log(f' 完成!')
    log(f' 总交易日: {len(all_dates)}天')
    log(f' 已入库: {total_saved}条')
    log(f' 跳过: {skipped_days}天')
    log(f' 耗时: {el:.0f}s ({el/60:.1f}分钟)')
    
    # 验证
    conn = get_connection()
    cur = conn.cursor()
    cur.execute('SELECT COUNT(DISTINCT trade_date), COUNT(DISTINCT ts_code), COUNT(*) FROM backtest_score_v4')
    r = cur.fetchone()
    log(f' 最终: {r[0]}天, {r[1]}只, {r[2]}条')
    cur.close(); conn.close()

if __name__ == '__main__':
    log('='*50)
    log(' V4 全量评分重算启动')
    log('='*50)
    run_fullV4()
