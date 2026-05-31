#!/usr/bin/env python3
"""
每日数据管道调度器 v1.0
======================
每日15:30自动执行:
  1. 拉取回测池+监控池 最新K线 (三级回退)
  2. 同步到前复权表
  3. 跑缠论结构分析 (chanlun_structure)
  4. 跑季节判定 (season_engine_v2.0)
  5. 跑全量评分入库 (score_engine → trend_score + strategy_signal)
  6. 写 watch_pool_snapshot (监控池快照)
  7. 生成 daily_market_summary

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
        c2 = pymysql.connect(host='127.0.0.1',port=3306,user='debian-sys-maint',
            password=get_mysql_pass(),database='openclaw_config',charset='utf8mb4')
        cu2 = c2.cursor()
        cu2.execute("SELECT api_key FROM api_credentials WHERE name='TUSHARE_TOKEN' AND is_active=1")
        r2 = cu2.fetchone()
        cu2.close(); c2.close()
        return r2[0] if r2 else ''
    
    conn = pymysql.connect(host='127.0.0.1',port=3306,user='debian-sys-maint',
        password=get_mysql_pass(),database='stock_db',charset='utf8mb4')
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
    batch_size = 15
    
    cur2 = conn.cursor()
    
    for i in range(0, len(codes), batch_size):
        batch = codes[i:i+batch_size]
        codes_str = ','.join(batch)
        
        try:
            # 用 Tushare daily 批量拉取（指定单只做批量效果不好，改每只独立但共用token）
            from data_fetcher import DataFetcherV2
            for code in batch:
                # 只拉最近7天的数据，已有历史K线，仅补最新
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
                else:
                    fail += 1
                time.sleep(0.15)
        except Exception as e:
            fail += len(batch)
            logger.warning(f"  批次 {i}-{i+batch_size} 失败: {e}")
        
        if (i+batch_size) % 60 == 0:
            logger.info(f"  进度: {min(i+batch_size, len(codes))}/{len(codes)} ({success}成功, {fail}失败)")
    
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
    """Step 3: 季节判定"""
    import importlib.util
    spec = importlib.util.spec_from_file_location('season_engine',
        os.path.join(os.path.dirname(os.path.abspath(__file__)), 'season_engine_v2.0.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if hasattr(mod, 'main'):
        mod.main()
    return 'ok'

def step_score():
    """Step 4: 全量评分入库"""
    from score_engine import ScoreEngineV4, main as score_main
    import argparse
    # 模拟 --top 100 参数 (默认自动入库)
    sys.argv = ['score_engine.py', '--top', '100']
    score_main()
    return 'ok'

def step_snapshot():
    """Step 5: 监控池快照 (通过API触发)"""
    import requests
    import pymysql
    try:
        # 读数据库中的api_key
        conn = pymysql.connect(host='127.0.0.1',port=3306,user='debian-sys-maint',
            password=get_mysql_pass(),database='stock_db')
        cur = conn.cursor()
        cur.execute("SELECT config_value FROM system_config WHERE config_key='api_key' LIMIT 1")
        row = cur.fetchone()
        api_key = row[0] if row else ''
        cur.close(); conn.close()
        
        headers = {'X-API-Key': api_key}
        r = requests.post('http://localhost:8887/api/v1/management/watch-pool/refresh',
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


def step_regime_switcher():
    """市场状态识别与动态调参"""
    import subprocess
    try:
        r = subprocess.run(["python3", os.path.join(os.path.dirname(__file__), "regime_switcher.py")],
            capture_output=True, text=True, timeout=30)
        logger.info(f"市场状态识别: {r.stdout.strip()}")
        if r.returncode != 0 and r.stderr:
            logger.warning(f"regime_switcher stderr: {r.stderr.strip()}")
        return r.stdout.strip()
    except Exception as e:
        logger.error(f"regime_switcher error: {e}")
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
    if args.step == 'all':
        run_step('7/7 市场状态识别', step_regime_switcher)

    elapsed = time.time() - total_t0
    logger.info(f"{'='*50}")
    logger.info(f"🏁 每日数据管道运行完成 (总耗时{elapsed:.0f}s)")
    logger.info(f"{'='*50}")

if __name__ == '__main__':
    main()
