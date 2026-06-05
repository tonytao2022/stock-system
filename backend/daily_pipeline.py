#!/usr/bin/env python3
"""
每日数据管道调度器 v1.0
======================
每日17:00自动执行（收盘后Tushare数据出全）:
  原则: 数据只从 Tushare Pro 获取（拒绝腾讯/东方财富等替代源）
  1. 拉取回测池+监控池 K线 (Tushare, 5次重试)
  2. 同步到前复权表
  3. 跑缠论结构分析 (chanlun_structure)
  4. 季节判定 (season_engine v2.1 自动入库)
  5. P6双轨评分 (strategy_signal)
  6. 写 watch_pool_snapshot (监控池快照)
  7. 多周期回测

用法:
  python3 daily_pipeline.py              # 全量运行
  python3 daily_pipeline.py --step kline  # 只拉K线
  python3 daily_pipeline.py --step score  # 只跑评分
"""
import os, sys, time, logging, argparse
from db_config import db_cursor, get_connection, get_user_id
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('daily_pipeline')

def get_mysql_pass():
    """从 debian.cnf 读取 MySQL 密码"""
    try:
        with open('/etc/mysql/debian.cnf') as f:
            for line in f:
                if 'password' in line:
                    return line.strip().split('=')[-1].strip().strip('"').strip("'")
    except:
        pass
    return ''

def run_step(name, func, *args, **kwargs):
    logger.info(f"{'='*50}")
    logger.info(f"🚀 [{name}] 开始...")
    t0 = time.time()
    try:
        result = func(*args, **kwargs)
        elapsed = time.time() - t0
        logger.info(f"✅ [{name}] 完成 ({elapsed:.1f}s): {result}")
        return result
    except Exception as e:
        elapsed = time.time() - t0
        logger.error(f"❌ [{name}] 失败 ({elapsed:.1f}s): {e}")
        return None

def step_kline():
    """Step 1: 拉取最新K线（批量优化版）"""
    import pymysql, tushare as ts
    
    def get_token():
        import os
        tk = os.environ.get('TUSHARE_TOKEN', '')
        if tk: return tk
        c2 = get_connection()
        cu2 = c2.cursor()
        cu2.execute("SELECT api_key FROM api_credentials WHERE name='TUSHARE_TOKEN' AND is_active=1")
        r2 = cu2.fetchone()
        cu2.close(); c2.close()
        return r2[0] if r2 else ''
    
    conn = get_connection()
    cur = conn.cursor(pymysql.cursors.DictCursor)
    
    # 获取股票列表
    cur.execute(f"""
        SELECT DISTINCT ts_code FROM (
            SELECT ts_code FROM backtest_pool WHERE status='ACTIVE' AND market!='指数'
            UNION
            SELECT ts_code FROM watch_pool WHERE is_active=1 AND user_id='{get_user_id()}'
        ) AS pool
    """)
    codes = [r['ts_code'] for r in cur.fetchall()]
    logger.info(f"📋 待更新K线: {len(codes)}只")
    
    # 用 Tushare 批量获取最新行情（昨天+今天）
    token = get_token()
    if not token:
        logger.error("❌ TUSHARE_TOKEN 未配置")
        return {'success': 0, 'fail': len(codes), 'total': len(codes), 'error': 'no token'}
    
    ts.set_token(token)
    pro = ts.pro_api()
    
    today = datetime.now().strftime('%Y%m%d')
    start = '20260101'
    success = 0; fail = 0
    batch_size = 10
    
    cur2 = conn.cursor()

    for i in range(0, len(codes), batch_size):
        batch = codes[i:i+batch_size]
        
        for code in batch:
            # Tushare 拉取（5次重试，间隔指数增长让API喘口气）
            ok = False
            for retry in range(5):
                try:
                    df = pro.daily(ts_code=code, start_date='20260520', end_date=today)
                    if df is not None and len(df) > 0:
                        for _, row in df.iterrows():
                            cur2.execute("""
                                INSERT INTO daily_kline (ts_code, trade_date, open, high, low, close, vol, amount, change_pct)
                                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                                ON DUPLICATE KEY UPDATE close=VALUES(close), vol=VALUES(vol), change_pct=VALUES(change_pct)
                            """, (code, row['trade_date'],
                                  float(row['open']), float(row['high']), float(row['low']),
                                  float(row['close']), float(row['vol']), float(row['amount']),
                                  float(row.get('pct_chg',0)or 0)))
                        conn.commit()
                        success += 1
                        ok = True
                        break
                except Exception as e:
                    wait = 3 + retry * 2  # 3, 5, 7, 9, 11秒
                    if retry < 4:
                        time.sleep(wait)
                    continue
            
            if not ok:
                fail += 1
            time.sleep(0.4)
        
        if (i+batch_size) % 50 == 0:
            logger.info(f"  进度: {min(i+batch_size, len(codes))}/{len(codes)} (Tushare: {success}成功, {fail}失败)")
    
    # 同步到前复权表
    cur2.execute(
        "INSERT IGNORE INTO daily_kline_qfq (ts_code, trade_date, open, high, low, close, change_pct, vol, amount) "
        "SELECT ts_code, trade_date, open, high, low, close, change_pct, vol, amount FROM daily_kline "
        "WHERE trade_date >= '2026-05-20'"
    )
    conn.commit()
    cur2.close(); cur.close(); conn.close()
    
    logger.info(f"📊 K线更新: {success}成功, {fail}失败 (共{len(codes)}只)")
    return {'success': success, 'fail': fail, 'total': len(codes)}

def step_chanlun():
    """Step 2: 缠论结构分析"""
    from batch_chanlun_analyzer import main as chanlun_main
    chanlun_main()
    return 'ok'

def step_season():
    """Step 3: 季节判定（使用 v2.1，时序更稳定）"""
    import sys
    # 统一用 season_engine.py (v2.1)，移除了并列的 v2.0
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from season_engine import SeasonEngine
    from season_engine import save_result_to_db as save_season_result_to_db
    engine = SeasonEngine()
    result = engine.judge_market_season()
    # 打印关键结果
    logger.info(f"📅 季节判定: {result.get('market_season','?')}/{result.get('regime','?')} "
                f"置信度={result.get('market_confidence',0):.2f} "
                f"评分={result.get('raw_score',0):.2f}")
    # 保存到数据库
    try:
        save_season_result_to_db(result)
        logger.info("💾 季节判定结果已入库")
    except Exception as e:
        logger.warning(f"⚠️ 季节判定入库失败: {e}")
    return 'ok'

def step_score():
    """Step 4: P6 全量评分入库（默认切换为P6双轨引擎）"""
    from p6_dual_track_engine import daily_pipeline as p6_pipeline
    from season_engine import SeasonEngine
    from p6_dual_track_engine import MarketContext
    from db_config import get_connection
    
    # === P6 双轨评分（主引擎）===
    logger.info("🚀 P6双轨评分引擎启动...")
    results = p6_pipeline(mode='watch_pool')
    logger.info(f"✅ P6双轨评分完成: {len(results)}只")
    
    # === 同步 P6 校准分到 trend_score（兼容旧前端）===
    try:
        engine = SeasonEngine()
        ctx = MarketContext(engine.judge_market_season())
        conn = get_connection()
        cur = conn.cursor()
        synced = 0
        for r in results:
            cur.execute("""
                UPDATE trend_score 
                SET composite_score = %s
                WHERE ts_code = %s 
                  AND trade_date = (SELECT MAX(trade_date) FROM trend_score WHERE ts_code = %s)
            """, (r['score'], r['ts_code'], r['ts_code']))
            if cur.rowcount > 0:
                synced += 1
        conn.commit()
        cur.close(); conn.close()
        logger.info(f"🔄 P6校准分同步到trend_score: {synced}只")
    except Exception as e:
        logger.warning(f"⚠️ P6→trend_score同步失败(非致命): {e}")
    
    return 'ok'
def step_snapshot():
    """Step 5: 监控池快照 (通过API触发)"""
    import requests
    import pymysql
    try:
        # 读数据库中的api_key
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT config_value FROM system_config WHERE config_key='api_key' LIMIT 1")
        row = cur.fetchone()
        api_key = row[0] if row else ''
        cur.close(); conn.close()
        
        if not api_key:
            # 回退到环境变量或硬编码默认值
            api_key = os.environ.get('API_KEY', '90a275cbcc004fd5')
        headers = {'X-API-Key': api_key}
        _api_base_8887 = os.environ.get('API_BASE_8887', 'http://localhost:8887')
        r = requests.post(f'{_api_base_8887}/api/v1/management/watch-pool/refresh',
                          headers=headers, timeout=120)
        if r.status_code == 200:
            data = r.json()
            if data.get('code') == 0:
                logger.info(f"📊 监控池快照: {data.get('data',{}).get('updated',0)}只更新")
                return data.get('data',{})
            else:
                logger.warning(f"⚠️ 监控池快照API返回错误: {data.get('error','')}")
                return None
        else:
            logger.warning(f"⚠️ 监控池快照HTTP {r.status_code}: {r.text[:200]}")
            return None
    except Exception as e:
        logger.error(f"❌ 监控池快照失败: {e}")
        return None

def step_backtest():
    """Step 6: 多周期回测（5/10/20/30/60日持有期对比）"""
    import subprocess, os
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'backtest_multi_cycle.py')
    try:
        r = subprocess.run(
            [sys.executable, script],
            capture_output=True, text=True, timeout=120, cwd=os.path.dirname(script)
        )
        # 提取关键结果
        lines = [l for l in r.stdout.split('\n') if '🏆' in l or '利差' in l or '最佳周期' in l]
        for l in lines:
            logger.info(f"  📊 {l.strip()}")
        logger.info(f"✅ 回测完成 (exit={r.returncode})")
        return 'ok'
    except Exception as e:
        logger.error(f"❌ 回测失败: {e}")
        return None


def main():
    ap = argparse.ArgumentParser(description='每日数据管道调度器')
    ap.add_argument('--step', type=str, choices=['kline','chanlun','season','score','snapshot','backtest','all'],
                    default='all', help='运行步骤')
    args = ap.parse_args()

    total_t0 = time.time()

    if args.step in ('all', 'kline'):
        run_step('1/5 K线拉取', step_kline)
    if args.step in ('all', 'chanlun'):
        run_step('2/5 缠论分析', step_chanlun)
    if args.step in ('all', 'season'):
        run_step('3/5 季节判定', step_season)
    if args.step in ('all', 'score'):
        run_step('4/5 全量评分', step_score)
    if args.step in ('all', 'snapshot'):
        run_step('5/5 监控池快照', step_snapshot)
    if args.step in ('all', 'backtest'):
        run_step('6/6 多周期回测', step_backtest)

    elapsed = time.time() - total_t0
    logger.info(f"{'='*50}")
    logger.info(f"🏁 每日数据管道运行完成 (总耗时{elapsed:.0f}s)")
    logger.info(f"{'='*50}")

if __name__ == '__main__':
    main()
