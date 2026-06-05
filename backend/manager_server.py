"""
manager_server.py - 管理API服务 (Port 8887)
路由前缀: /api/v1/management/*
Antony架构方案第4.2节规范
"""
import os
import sys
import json
import logging
import subprocess
from datetime import datetime, date, timedelta
from flask import Flask, request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db_config import db_cursor, api_success, api_error, api_not_found, serialize_rows, DATA_ERROR_MARKER

# ─── 全局配置 ───────────────────────────────────────────────
BASE_DIR = os.environ.get('STOCK_BASE_DIR', '/root/.openclaw/workspace/projects/陶的投资预测模型项目/代码实现')
API_BASE_8887 = os.environ.get('API_BASE_8887', 'http://localhost:8887')

app = Flask(__name__)
logger = logging.getLogger('manager_8887')
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

# ═══ API 认证 ═══
_DEFAULT_USER = os.environ.get('STOCK_USER', 'tony')  # 默认用户ID（环境变量化）
def _get_user_id():
    """从环境变量获取用户ID，避免db_cursor嵌套导致连接冲突"""
    from db_config import get_default_user
    import os
    return os.environ.get('STOCK_USER') or get_default_user()
_API_KEY_CACHE = {'key': None}
def _get_api_key():
    if _API_KEY_CACHE.get('key'):
        return _API_KEY_CACHE['key']
    # 优先使用环境变量，避免 db_cursor 嵌套连接冲突
    from db_config import get_connection as _gk_conn
    try:
        _gk_c = _gk_conn(); _gk_cur = _gk_c.cursor()
        _gk_cur.execute("SELECT config_value FROM system_config WHERE config_key='api_key' LIMIT 1")
        _gk_r = _gk_cur.fetchone()
        if _gk_r:
            key = _gk_r['config_value'] if isinstance(_gk_r, dict) else _gk_r[0]
            if key:
                _API_KEY_CACHE['key'] = key
                _gk_cur.close(); _gk_c.close()
                return key
        _gk_cur.close(); _gk_c.close()
    except: pass
    return os.environ.get('API_KEY', '90a275cbcc004fd5')
    return None

@app.before_request
def _check_api_key():
    # 健康检查和OPTIONS预检请求不验证
    if request.method == 'OPTIONS':
        return None
    if request.path in ('/health', '/api/v1/management/system/health'):
        return None
    # 从请求头获取API Key
    req_key = request.headers.get('X-API-Key', '')
    if not req_key:
        req_key = request.headers.get('Authorization', '').replace('Bearer ', '')
    if not req_key:
        return api_error('缺少认证信息 (X-API-Key)', code=401)
    expected = _get_api_key()
    if not expected or req_key != expected:
        return api_error('认证失败', code=401)


# ─── 健康检查 ───────────────────────────────────────────────
@app.route('/health', methods=['GET'])
@app.route('/api/v1/management/system/health', methods=['GET'])
def health():
    try:
        with db_cursor(commit=False) as cur:
            cur.execute("SELECT 1")
            db_ok = cur.fetchone() is not None
        api_key_value = _get_api_key()
        return api_success({
            'service': 'management_api',
            'port': 8887,
            'database': 'connected' if db_ok else 'disconnected',
            'version': '2.0.0',
            'api_key': api_key_value if api_key_value else '',
        })
    except Exception as e:
        return api_error(str(e), http_status=500)


# ─── POST /api/v1/management/system/backup ──────────────────
@app.route('/api/v1/management/system/backup', methods=['POST'])
def backup_database():
    """数据库备份 — 调用 backup_db.sh"""
    import subprocess, time
    script = os.path.join(BASE_DIR, 'backup_db.sh')
    if not os.path.exists(script):
        return api_error('备份脚本不存在')
    
    try:
        t0 = time.time()
        result = subprocess.run(
            ['bash', script],
            capture_output=True, text=True, timeout=120
        )
        elapsed = round(time.time() - t0, 1)
        
        if result.returncode != 0:
            return api_error(f'备份失败(code={result.returncode}): {result.stderr[-300:] if result.stderr else result.stdout[-300:]}')
        
        # 解析输出
        file_info = ''; size_info = ''
        for line in result.stdout.split('\n'):
            if '文件:' in line: file_info = line.split('文件:')[-1].strip()
            if '大小:' in line: size_info = line.split('大小:')[-1].strip()
        
        from datetime import datetime as dtdt
        return api_success({
            'message': f'备份成功 ({elapsed}秒)',
            'file': file_info,
            'size': size_info,
            'time': dtdt.now().strftime('%Y-%m-%d %H:%M:%S'),
            'output': result.stdout[-500:]
        })
    except subprocess.TimeoutExpired:
        return api_error('备份超时(>120秒)')
    except Exception as e:
        return api_error(f'备份异常: {str(e)}')


# ─── GET /api/v1/management/portfolio ───────────────────────
@app.route('/api/v1/management/portfolio', methods=['GET'])
def portfolio():
    """获取投资组合监控列表"""
    try:
        trade_date = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))
        direction = request.args.get('direction')
        min_score = request.args.get('min_score')
        page = int(request.args.get('page', 1))
        page_size = min(int(request.args.get('page_size', 20)), 100)
        offset = (page - 1) * page_size

        wheres = ["ss.trade_date = %s"]
        params = [trade_date]
        if direction:
            wheres.append("ss.direction = %s")
            params.append(direction)
        if min_score:
            wheres.append("ss.composite_score >= %s")
            params.append(float(min_score))

        with db_cursor(commit=False) as cur:
            # 总数
            cur.execute(
                f"SELECT COUNT(*) as cnt FROM strategy_signal ss WHERE {' AND '.join(wheres)}",
                params
            )
            total = cur.fetchone()['cnt']

            # 分页数据
            cur.execute(
                f"""SELECT ss.*, sb.name, sb.industry, sb.market
                    FROM strategy_signal ss
                    LEFT JOIN stock_basic sb ON ss.ts_code = sb.ts_code
                    WHERE {' AND '.join(wheres)}
                    ORDER BY ss.composite_score DESC
                    LIMIT %s OFFSET %s""",
                params + [page_size, offset]
            )
            rows = cur.fetchall()

        return api_success({
            'trade_date': trade_date,
            'stocks': serialize_rows(rows),
            'pagination': {
                'page': page,
                'page_size': page_size,
                'total': total,
                'total_pages': (total + page_size - 1) // page_size
            }
        })
    except Exception as e:
        logger.error(f"portfolio error: {e}")
        return api_error(str(e))


# ─── GET /api/v1/management/portfolio/{ts_code} ─────────────
@app.route('/api/v1/management/portfolio/<ts_code>', methods=['GET'])
def portfolio_detail(ts_code):
    """获取单只股票组合详情"""
    try:
        trade_date = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))

        with db_cursor(commit=False) as cur:
            cur.execute(
                """SELECT ss.*, sb.name, sb.industry, sb.market
                   FROM strategy_signal ss
                   LEFT JOIN stock_basic sb ON ss.ts_code = sb.ts_code
                   WHERE ss.ts_code=%s AND ss.trade_date=%s
                   LIMIT 1""",
                [ts_code, trade_date]
            )
            row = cur.fetchone()

        if not row:
            return api_not_found()
        return api_success(serialize_rows([row])[0])
    except Exception as e:
        logger.error(f"portfolio_detail error: {e}")
        return api_error(str(e))


# ─── GET /api/v1/management/portfolio/{ts_code}/history ─────
@app.route('/api/v1/management/portfolio/<ts_code>/history', methods=['GET'])
def portfolio_history(ts_code):
    """获取持仓历史评分曲线"""
    try:
        start = request.args.get('start', (datetime.now() - timedelta(days=90)).strftime('%Y-%m-%d'))
        end = request.args.get('end', datetime.now().strftime('%Y-%m-%d'))

        with db_cursor(commit=False) as cur:
            cur.execute(
                """SELECT trade_date, composite_score, cycle_score, structure_score, emotion_score
                   FROM trend_score
                   WHERE ts_code=%s AND trade_date BETWEEN %s AND %s
                   ORDER BY trade_date ASC""",
                [ts_code, start, end]
            )
            rows = cur.fetchall()

        return api_success({
            'ts_code': ts_code,
            'start': start,
            'end': end,
            'count': len(rows),
            'points': serialize_rows(rows)
        })
    except Exception as e:
        logger.error(f"portfolio_history error: {e}")
        return api_error(str(e))


# ─── GET /api/v1/management/dashboard ───────────────────────
@app.route('/api/v1/management/dashboard', methods=['GET'])
def dashboard():
    """数据驾驶舱概览"""
    try:
        trade_date = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))

        with db_cursor(commit=False) as cur:
            # Top5 买入: 改为从watch_pool_snapshot取（统一评分口径）
            cur.execute("SELECT MAX(trade_date) as d FROM watch_pool_snapshot")
            _ld = cur.fetchone()
            _td = str(_ld['d']) if _ld and _ld['d'] else trade_date
            
            # Top5 买入: 按v_score排序取前5（与全量评分页面对齐，不依赖signal_type字段）
            # 统计信号分布
            cur.execute(
                """SELECT 
                      SUM(CASE WHEN signal_type='STRONG_BUY' THEN 1 ELSE 0 END) as strong_buy,
                      SUM(CASE WHEN signal_type='BUY' THEN 1 ELSE 0 END) as buy,
                      SUM(CASE WHEN signal_type='CAUTIOUS_BUY' THEN 1 ELSE 0 END) as cautious,
                      SUM(CASE WHEN signal_type='HOLD' THEN 1 ELSE 0 END) as hold,
                      SUM(CASE WHEN signal_type IN ('SELL','STRONG_SELL') THEN 1 ELSE 0 END) as sell
                   FROM watch_pool_snapshot
                   WHERE trade_date=%s""",
                [_td]
            )
            cnt_row = cur.fetchone()
            
            cur.execute(
                """SELECT wps.*, sb.industry
                   FROM watch_pool_snapshot wps
                   LEFT JOIN stock_basic sb ON wps.ts_code = sb.ts_code
                   WHERE wps.trade_date=%s AND wps.ts_code IN (
                       SELECT ts_code FROM watch_pool WHERE is_active=1
                   )
                   ORDER BY wps.v_score DESC LIMIT 5""",
                [_td]
            )
            top_buys = cur.fetchall()

            # 市场快照 (从season_state取恒纪元+季节，恒纪元字段为空则按季节推断)
            cur.execute(
                "SELECT season, raw_score, confidence, position_advice, hengjiyuan_level, hengjiyuan_score, confidence_mult "
                "FROM season_state WHERE index_code='MARKET' ORDER BY trade_date DESC LIMIT 1"
            )
            market_row = cur.fetchone()
            if market_row:
                hj_level = market_row['hengjiyuan_level']
                hj_score = market_row['hengjiyuan_score']
                # 恒纪元字段为空时按季节+评分推断
                if not hj_level:
                    season_str = market_row['season'] or 'chaos'
                    score = float(market_row['raw_score'] or 0)
                    if season_str in ('summer', 'spring'):
                        hj_level = 'strong_heng' if score > 2 else 'weak_heng'
                    elif season_str in ('chaos', 'chaos_spring'):
                        hj_level = 'weak_heng' if score > 0 else 'weak_luan'
                    else:
                        hj_level = 'weak_luan' if score < -1 else 'strong_luan'
                    hj_score = max(0, min(100, (score + 10) * 5))
                se = market_row['season'] or 'chaos'
                season_data = {
                    'season': se,
                    'season_label': se,
                    'season_emoji': {'spring':'🌺','summer':'☀️','autumn':'🍂','winter':'❄️','chaos':'🌪️','chaos_spring':'🌤️','chaos_autumn':'🌥️','chaos_mild':'🌤️','chaos_cold':'🌥️','panic':'💀','recovery':'🌱'}.get(se,'❓'),
                    'raw_score': float(market_row['raw_score'] or 0),
                    'confidence': float(market_row['confidence'] or 0),
                    'position_label': market_row.get('position_advice', ''),
                    'hengjiyuan_level': hj_level,
                    'hengjiyuan_score': hj_score,
                }
            else:
                season_data = {}

        # 格式化top5_buys供前端渲染
        def _fmt_top(t):
            return {
                'ts_code': t['ts_code'],
                'name': t['name'],
                'stock_name': t['name'],
                'code': t['ts_code'],
                'trade_date': str(t['trade_date']),
                'composite_score': float(t['v_score'] or 0),
                'score': float(t['v_score'] or 0),
                'v_score': float(t['v_score'] or 0),
                'raw_score': float(t['raw_score'] or 0),
                'trend_score': float(t['trend_score'] or 0),
                'momentum_score': float(t['momentum_score'] or 0),
                'signal_type': t['signal_type'],
                'signal_label': t['signal_label'],
                'season': t['season'],
                'regime': t['regime'],
                'industry': t.get('industry', ''),
                'ret_5d': float(t['ret_5d'] or 0),
                'ret_10d': float(t['ret_10d'] or 0),
                'ret_20d': float(t['ret_20d'] or 0),
                'close_price': float(t['close_price'] or 0),
                'change_pct': float(t['change_pct'] or 0),
                'position_pct': float(t['position_pct'] or 0),
                'reason_chain': f"{t.get('season','')}+{t.get('signal_label','')}",
            }

        return api_success({
            'trade_date': _td,
            'market_state': season_data.get('season') if season_data else None,
            'top5_buys': [_fmt_top(t) for t in top_buys],
            'buy_count': int(cnt_row['buy'] if cnt_row else 0),
            'strong_buy_count': int(cnt_row['strong_buy'] if cnt_row else 0),
            'cautious_count': int(cnt_row['cautious'] if cnt_row else 0),
            'hold_count': int(cnt_row['hold'] if cnt_row else 0),
            'sell_count': int(cnt_row['sell'] if cnt_row else 0),
            'watch_pool_count': int(sum(int(cnt_row[k] or 0) for k in ['strong_buy','buy','cautious','hold','sell'])) if cnt_row else 0,
            'risk_alerts': {'autumn_tiger_count': 0, 'direction_change_alerts': 0},
            **season_data,
        })
    except Exception as e:
        logger.error(f"dashboard error: {e}")
        return api_error(str(e))


# ─── GET /api/v1/management/dashboard/market-overview ───────
@app.route('/api/v1/management/dashboard/market-overview', methods=['GET'])
def market_overview():
    """市场全局概览（从season_state + watch_pool_snapshot 取数）"""
    try:
        trade_date = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))

        with db_cursor(commit=False) as cur:
            cur.execute(
                "SELECT season, raw_score, confidence, position_advice, hengjiyuan_level, hengjiyuan_score, confidence_mult "
                "FROM season_state WHERE index_code='MARKET' ORDER BY trade_date DESC LIMIT 1"
            )
            ss = cur.fetchone()
            cur.execute(
                "SELECT COUNT(*) as total, "
                "SUM(CASE WHEN signal_type IN ('BUY','STRONG_BUY') THEN 1 ELSE 0 END) as buy, "
                "SUM(CASE WHEN signal_type='SELL' THEN 1 ELSE 0 END) as sell "
                "FROM watch_pool_snapshot WHERE trade_date=(SELECT MAX(trade_date) FROM watch_pool_snapshot)"
            )
            wp = cur.fetchone()

        if not ss:
            return api_success({'trade_date': trade_date, 'ready': False, 'message': '暂无季节数据'})

        return api_success({
            'trade_date': str(ss.get('trade_date', trade_date)),
            'cycle_stage': ss.get('season'),
            'hengjiyuan_score': float(ss['hengjiyuan_score']) if ss.get('hengjiyuan_score') else None,
            'hengjiyuan_level': ss.get('hengjiyuan_level'),
            'confidence_mult': float(ss['confidence_mult']) if ss.get('confidence_mult') else None,
            'signal_distribution': {
                'buy_count': int(wp['buy'] or 0) if wp else 0,
                'sell_count': int(wp['sell'] or 0) if wp else 0,
                'wait_count': int((wp['total'] or 0) - (wp['buy'] or 0) - (wp['sell'] or 0)) if wp else 0,
                'high_confidence_buy': 0,
                'autumn_tiger_count': 0,
            },
            'total_analyzed': int(wp['total'] or 0) if wp else 0,
            'safety_gate': '通过',
            'gate_triggered': False,
        })
    except Exception as e:
        logger.error(f"market_overview error: {e}")
        return api_error(str(e))


# ─── GET /api/v1/management/dashboard/top-buys ──────────────
@app.route('/api/v1/management/dashboard/top-buys', methods=['GET'])
def top_buys():
    """Top买入推荐"""
    try:
        trade_date = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))
        limit = min(int(request.args.get('limit', 10)), 30)

        with db_cursor(commit=False) as cur:
            cur.execute(
                """SELECT ss.*, sb.name, sb.industry
                   FROM strategy_signal ss LEFT JOIN stock_basic sb ON ss.ts_code = sb.ts_code
                   WHERE ss.trade_date=%s AND ss.direction='LONG'
                     AND ss.is_calculable=1 AND ss.gate_triggered=0
                   ORDER BY ss.composite_score DESC LIMIT %s""",
                [trade_date, limit]
            )
            rows = cur.fetchall()

        return api_success({
            'trade_date': trade_date,
            'count': len(rows),
            'top_buys': serialize_rows(rows)
        })
    except Exception as e:
        logger.error(f"top_buys error: {e}")
        return api_error(str(e))


# ─── GET /api/v1/management/dashboard/risk-alerts ───────────
@app.route('/api/v1/management/dashboard/risk-alerts', methods=['GET'])
def risk_alerts():
    """风险告警列表"""
    try:
        trade_date = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))
        limit = min(int(request.args.get('limit', 20)), 50)

        alerts = []

        with db_cursor(commit=False) as cur:
            # 秋老虎
            cur.execute(
                """SELECT ss.*, sb.name FROM strategy_signal ss
                   LEFT JOIN stock_basic sb ON ss.ts_code = sb.ts_code
                   WHERE ss.trade_date=%s AND ss.autumn_tiger=1
                   ORDER BY ss.tiger_confidence DESC LIMIT %s""",
                [trade_date, limit]
            )
            for row in cur.fetchall():
                alerts.append({
                    'type': 'autumn_tiger',
                    'ts_code': row['ts_code'],
                    'name': row.get('name', ''),
                    'confidence': row.get('tiger_confidence', 0),
                    'description': f"秋老虎标记, 评分为{row.get('composite_score', 0)}"
                })

            # 方向变更
            cur.execute(
                """SELECT * FROM signal_change_log
                   WHERE trade_date=%s AND is_alert=1
                   ORDER BY id DESC LIMIT %s""",
                [trade_date, limit]
            )
            for row in cur.fetchall():
                alerts.append({
                    'type': 'direction_change',
                    'ts_code': row['ts_code'],
                    'prev': row.get('prev_direction', ''),
                    'new': row.get('new_direction', ''),
                    'description': row.get('change_reason', '')
                })

        return api_success({
            'trade_date': trade_date,
            'count': len(alerts),
            'alerts': alerts
        })
    except Exception as e:
        logger.error(f"risk_alerts error: {e}")
        return api_error(str(e))


# ─── GET /api/v1/management/history/scores ──────────────────
@app.route('/api/v1/management/history/scores', methods=['GET'])
def history_scores():
    """历史评分查询(分页)"""
    try:
        ts_code = request.args.get('ts_code')
        start = request.args.get('start', (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d'))
        end = request.args.get('end', datetime.now().strftime('%Y-%m-%d'))
        page = int(request.args.get('page', 1))
        page_size = min(int(request.args.get('page_size', 50)), 200)
        offset = (page - 1) * page_size

        wheres = ["trade_date BETWEEN %s AND %s"]
        params = [start, end]
        if ts_code:
            wheres.append("ts_code = %s")
            params.append(ts_code)

        where_clause = " AND ".join(wheres)

        with db_cursor(commit=False) as cur:
            cur.execute(
                f"SELECT COUNT(*) as cnt FROM strategy_signal WHERE {where_clause}",
                params
            )
            total = cur.fetchone()['cnt']

            cur.execute(
                f"""SELECT ss.*, sb.name FROM strategy_signal ss
                    LEFT JOIN stock_basic sb ON ss.ts_code = sb.ts_code
                    WHERE {where_clause}
                    ORDER BY ss.trade_date DESC, ss.composite_score DESC
                    LIMIT %s OFFSET %s""",
                params + [page_size, offset]
            )
            rows = cur.fetchall()

        return api_success({
            'points': serialize_rows(rows),
            'pagination': {
                'page': page,
                'page_size': page_size,
                'total': total,
                'total_pages': (total + page_size - 1) // page_size
            }
        })
    except Exception as e:
        logger.error(f"history_scores error: {e}")
        return api_error(str(e))


# ─── GET /api/v1/management/history/signals ─────────────────
@app.route('/api/v1/management/history/signals', methods=['GET'])
def history_signals():
    """历史信号查询(分页)"""
    try:
        ts_code = request.args.get('ts_code')
        start = request.args.get('start', (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d'))
        end = request.args.get('end', datetime.now().strftime('%Y-%m-%d'))
        page = int(request.args.get('page', 1))
        page_size = min(int(request.args.get('page_size', 50)), 200)
        offset = (page - 1) * page_size

        wheres = ["trade_date BETWEEN %s AND %s"]
        params = [start, end]
        if ts_code:
            wheres.append("ts_code = %s")
            params.append(ts_code)

        where_clause = " AND ".join(wheres)

        with db_cursor(commit=False) as cur:
            cur.execute(
                f"SELECT COUNT(*) as cnt FROM strategy_signal WHERE {where_clause}",
                params
            )
            total = cur.fetchone()['cnt']

            cur.execute(
                f"""SELECT ss.*, sb.name FROM strategy_signal ss
                    LEFT JOIN stock_basic sb ON ss.ts_code = sb.ts_code
                    WHERE {where_clause}
                    ORDER BY ss.trade_date DESC, ss.composite_score DESC
                    LIMIT %s OFFSET %s""",
                params + [page_size, offset]
            )
            rows = cur.fetchall()

        return api_success({
            'signals': serialize_rows(rows),
            'pagination': {
                'page': page,
                'page_size': page_size,
                'total': total,
                'total_pages': (total + page_size - 1) // page_size
            }
        })
    except Exception as e:
        logger.error(f"history_signals error: {e}")
        return api_error(str(e))


# ─── GET /api/v1/management/history/score-trend ──────────────
@app.route('/api/v1/management/history/score-trend', methods=['GET'])
def history_score_trend():
    """单只股票近N日评分趋势（用于折线图）"""
    try:
        ts_code = request.args.get('ts_code', '')
        days = int(request.args.get('days', 20))
        if not ts_code:
            return api_error('缺少ts_code参数')
        start = (datetime.now() - timedelta(days=days*2)).strftime('%Y-%m-%d')
        end = datetime.now().strftime('%Y-%m-%d')
        
        with db_cursor(commit=False) as cur:
            cur.execute(
                """SELECT ss.trade_date, ss.calibrated_score, ss.composite_score, ss.track,
                           ss.scoring_strategy, sb.name
                    FROM strategy_signal ss
                    LEFT JOIN stock_basic sb ON ss.ts_code = sb.ts_code
                    WHERE ss.ts_code=%s AND ss.trade_date BETWEEN %s AND %s
                    ORDER BY ss.trade_date ASC""",
                [ts_code, start, end]
            )
            rows = cur.fetchall()
        
        return api_success({
            'ts_code': ts_code,
            'name': rows[0]['name'] if rows else '',
            'count': len(rows),
            'points': serialize_rows(rows)
        })
    except Exception as e:
        logger.error(f"history_score_trend error: {e}")
        return api_error(str(e))


# ─── GET /api/v1/management/history/snapshots ───────────────
@app.route('/api/v1/management/history/snapshots', methods=['GET'])
def history_snapshots():
    """每日快照列表（从 season_state + watch_pool_snapshot 聚合）"""
    try:
        limit = min(int(request.args.get('limit', 30)), 365)

        with db_cursor(commit=False) as cur:
            cur.execute(
                """SELECT ss.trade_date, ss.season as cycle_stage, ss.season, ss.raw_score, ss.confidence,
                           ss.hengjiyuan_level, ss.hengjiyuan_score, ss.confidence_mult, ss.position_advice
                    FROM season_state ss
                    WHERE ss.index_code='MARKET'
                    GROUP BY ss.trade_date
                    ORDER BY ss.trade_date DESC LIMIT %s""",
                [limit]
            )
            rows = cur.fetchall()
            for r in rows:
                cur.execute(
                    "SELECT COUNT(*) as total FROM watch_pool_snapshot WHERE trade_date=%s",
                    [r['trade_date']]
                )
                wp = cur.fetchone()
                r['total_analyzed'] = wp['total'] if wp else 0

        return api_success({'count': len(rows), 'snapshots': serialize_rows(rows)})
    except Exception as e:
        logger.error(f"history_snapshots error: {e}")
        return api_error(str(e))


# ─── GET /api/v1/management/history/snapshot/{date} ─────────
@app.route('/api/v1/management/history/snapshot/<snap_date>', methods=['GET'])
def history_snapshot_detail(snap_date):
    """指定日期的快照详情（从 season_state 取数）"""
    try:
        with db_cursor(commit=False) as cur:
            cur.execute(
                "SELECT season, raw_score, confidence, hengjiyuan_level, hengjiyuan_score, confidence_mult, position_advice "
                "FROM season_state WHERE index_code='MARKET' AND trade_date=%s", [snap_date]
            )
            ss = cur.fetchone()
            cur.execute(
                "SELECT COUNT(*) as total, "
                "SUM(CASE WHEN signal_type IN ('BUY','STRONG_BUY') THEN 1 ELSE 0 END) as buy, "
                "SUM(CASE WHEN signal_type='SELL' THEN 1 ELSE 0 END) as sell "
                "FROM watch_pool_snapshot WHERE trade_date=%s", [snap_date]
            )
            wp = cur.fetchone()
        if not ss:
            return api_not_found()
        # 组装成前端兼容格式
        result = {
            'trade_date': snap_date,
            'cycle_stage': ss['season'],
            'season': ss['season'],
            'hengjiyuan_level': ss['hengjiyuan_level'],
            'hengjiyuan_score': float(ss['hengjiyuan_score']) if ss['hengjiyuan_score'] else None,
            'confidence_mult': float(ss['confidence_mult']) if ss['confidence_mult'] else None,
            'position_advice': ss['position_advice'],
            'total_analyzed': int(wp['total']) if wp and wp['total'] else 0,
            'buy_signal_cnt': int(wp['buy']) if wp and wp['buy'] else 0,
            'sell_signal_cnt': int(wp['sell']) if wp and wp['sell'] else 0,
            'wait_signal_cnt': int(wp['total'] - wp['buy'] - wp['sell']) if wp and wp['total'] else 0,
        }
        return api_success(result)
    except Exception as e:
        logger.error(f"history_snapshot_detail error: {e}")
        return api_error(str(e))


# ─── 系统配置管理 ───────────────────────────────────────────
@app.route('/api/v1/management/system/config', methods=['GET', 'PUT'])
def system_config():
    """获取/更新系统配置"""
    try:
        if request.method == 'GET':
            with db_cursor(commit=False) as cur:
                cur.execute("SELECT * FROM system_config ORDER BY config_key")
                rows = cur.fetchall()
            return api_success({'configs': serialize_rows(rows)})

        # PUT
        data = request.get_json(force=True)
        key = data.get('config_key')
        value = data.get('config_value')
        config_type = data.get('config_type', 'string')

        if not key:
            return api_error("config_key必填", code=1001, http_status=400)

        with db_cursor(commit=True) as cur:
            cur.execute(
                """INSERT INTO system_config (config_key, config_value, config_type)
                   VALUES (%s, %s, %s)
                   ON DUPLICATE KEY UPDATE config_value=VALUES(config_value), config_type=VALUES(config_type)""",
                [key, str(value), config_type]
            )

        return api_success({'updated': key})
    except Exception as e:
        logger.error(f"system_config error: {e}")
        return api_error(str(e))


# ─── GET /api/v1/management/system/pipeline-status ──────────
@app.route('/api/v1/management/system/pipeline-status', methods=['GET'])
def pipeline_status():
    """数据管道状态"""
    try:
        limit = min(int(request.args.get('limit', 20)), 100)

        with db_cursor(commit=False) as cur:
            cur.execute(
                "SELECT * FROM pipeline_exec_log ORDER BY id DESC LIMIT %s",
                [limit]
            )
            rows = cur.fetchall()

        return api_success({'count': len(rows), 'logs': serialize_rows(rows)})
    except Exception as e:
        logger.error(f"pipeline_status error: {e}")
        return api_error(str(e))


# ─── POST /api/v1/management/system/pipeline/trigger ────────
@app.route('/api/v1/management/system/pipeline/trigger', methods=['POST'])
def pipeline_trigger():
    """手动触发数据管道"""
    try:
        data = request.get_json(force=True) or {}
        pipeline_name = data.get('pipeline_name', 'daily_update')
        exec_date = data.get('exec_date', datetime.now().strftime('%Y-%m-%d'))

        with db_cursor(commit=True) as cur:
            cur.execute(
                """INSERT INTO pipeline_exec_log
                   (exec_date, pipeline_name, step_name, status, started_at)
                   VALUES (%s, %s, %s, 'running', NOW())""",
                [exec_date, pipeline_name, 'manual_trigger']
            )
            log_id = cur.lastrowid

        logger.info(f"Pipeline triggered: {pipeline_name} for {exec_date}, log_id={log_id}")
        return api_success({
            'triggered': True,
            'log_id': log_id,
            'pipeline_name': pipeline_name,
            'exec_date': exec_date
        })
    except Exception as e:
        logger.error(f"pipeline_trigger error: {e}")
        return api_error(str(e))


# ─── POST /api/v1/management/system/refresh-all ────────────
@app.route('/api/v1/management/system/refresh-all', methods=['POST'])
def refresh_all():
    """一键实时刷新: 拉取行情→缠论→评分→监控池快照"""
    import subprocess, sys, os
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        pipeline_script = os.path.join(script_dir, 'daily_pipeline.py')

        # 用 subprocess 异步执行管道(跳过K线拉取,因为当前pandas兼容问题待修)
        # 实际执行: chanlun + season + score + snapshot
        log = []

        # Step 1: 季节判定 (直接用 season_engine_v2.0)
        import importlib.util as _ilu
        try:
            se_path = os.path.join(script_dir, 'season_engine_v2.0.py')
            spec = _ilu.spec_from_file_location('season_engine', se_path)
            mod = _ilu.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if hasattr(mod, 'main'):
                mod.main()
            log.append('季节: ok')
        except Exception as e:
            log.append(f'季节: error-{e}')

        # Step 2: 全量评分
        try:
            r = subprocess.run(
                [sys.executable, pipeline_script, '--step', 'score'],
                capture_output=True, text=True, timeout=60, cwd=script_dir
            )
            log.append(f'评分: {"ok" if r.returncode==0 else "fail"}')
        except Exception as e:
            log.append(f'评分: error-{e}')

        # Step 3: 监控池快照
        try:
            import requests as _req
            r2 = _req.post(API_BASE_8887 + '/api/v1/management/watch-pool/refresh', timeout=120)
            log.append(f'快照: {"ok" if r2.status_code==200 else "fail"}')
        except Exception as e:
            log.append(f'快照: error-{e}')

        logger.info(f"一键刷新完成: {', '.join(log)}")
        return api_success({'status': 'completed', 'steps': log})
    except Exception as e:
        logger.error(f"refresh_all error: {e}")
        return api_error(str(e))


# ─── GET /api/v1/management/backtest-report ──────────────────
@app.route('/api/v1/management/backtest-report', methods=['GET'])
def backtest_report():
    """多周期回测报告"""
    import sys, os, subprocess, re as _re
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        script = os.path.join(script_dir, 'backtest_multi_cycle.py')
        r = subprocess.run([sys.executable, script],
            capture_output=True, text=True, timeout=120, cwd=script_dir)
        output = r.stdout

        # 解析输出中的回测数据
        periods = []
        for line in output.split('\n'):
            if '▸ 持有' in line:
                # ▸ 持有  5 日 | n=7711 | IC=+0.0202 | 多空利差=+0.41% | 全样本=+2.98%
                m = _re.search(r'持有\s+(\d+)\s+日.*?n=(\d+).*?IC=([+-]?[\d.]+).*?多空利差=([+-]?[\d.]+)%.*?全样本=([+-]?[\d.]+)%', line)
                if m:
                    periods.append({
                        'days': int(m.group(1)),
                        'samples': int(m.group(2)),
                        'ic': float(m.group(3)),
                        'spread': float(m.group(4)),
                        'avg_return': float(m.group(5)),
                    })

        return api_success({
            'periods': periods,
            'total_periods': len(periods),
            'best_period': max(periods, key=lambda p: abs(p['spread']))['days'] if periods else 0,
        })
    except Exception as e:
        logger.error(f"backtest_report error: {e}")
        return api_error(str(e))


# ─── POST /api/v1/management/system/refresh-realtime ────────
@app.route('/api/v1/management/system/refresh-realtime', methods=['POST'])
def refresh_realtime():
    """⏱️ 实时评分刷新: 用rt_k实时价跑评分，写入realtime_score表"""
    import sys, os, time
    script_dir = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, script_dir)

    log = []
    try:
        from p6_dual_track_engine import MarketContext, score_stock, calibrate_scores
        from season_engine import SeasonEngine
        from engine.vmap import vmap_score, classify_signal
        import tushare as _ts

        # 获取 Tushare token
        def _get_token():
            import os
            tk = os.environ.get('TUSHARE_TOKEN', '')
            if tk: return tk
            from db_config import get_connection as _gc2
            _c2 = _gc2()
            _cu2 = _c2.cursor()
            _cu2.execute("SELECT api_key FROM openclaw_config.api_credentials WHERE name='TUSHARE_TOKEN' AND is_active=1")
            _r2 = _cu2.fetchone()
            _cu2.close(); _c2.close()
            return _r2[0] if _r2 else ''

        token = _get_token()
        if not token:
            return api_error('TUSHARE_TOKEN 未配置')

        _ts.set_token(token)
        pro = _ts.pro_api()

        # 获取监控池股票列表
        _pwd = ''
        try:
            with open('/etc/mysql/debian.cnf') as _pf:
                for _pl in _pf:
                    if 'password' in _pl:
                        _pwd = _pl.split('=')[-1].strip().strip('"').strip("'")
                        break
        except: pass
        from db_config import get_connection as _get_db_conn
        _conn = _get_db_conn()
        cur = _conn.cursor()
        uid = _get_user_id()
        cur.execute("SELECT ts_code FROM watch_pool WHERE is_active=1 AND user_id=%s", (uid,))
        watch_codes = [r['ts_code'] for r in cur.fetchall()]
        cur.execute("SELECT ts_code FROM backtest_pool WHERE status='ACTIVE' AND market!='指数'")
        bt_codes = [r['ts_code'] for r in cur.fetchall()]
        all_codes = list(dict.fromkeys(watch_codes + bt_codes))  # 去重

        # 获取市场上下文
        ctx = MarketContext(SeasonEngine().judge_market_season())

        # 批量获取实时价（P6引擎评分）
        rt_prices = {}
        batch_size = 15
        for i in range(0, len(all_codes), batch_size):
            batch = all_codes[i:i+batch_size]
            try:
                df = pro.rt_k(ts_code=','.join(batch))
                if df is not None and len(df) > 0:
                    for _, row in df.iterrows():
                        rt_prices[row['ts_code']] = {
                            'price': float(row['close']),
                            'pre_close': float(row['pre_close'])
                        }
            except: pass
            time.sleep(0.2)

        now = datetime.now()
        trade_date = now.strftime('%Y-%m-%d')
        
        # === P6 引擎评分 ===
        from p6_dual_track_engine import MarketContext as _MC, score_stock as _score_it, calibrate_scores as _calib
        from season_engine import SeasonEngine as _SE
        _ctx = _MC(_SE().judge_market_season())
        _p6_list = []
        for _code in all_codes:
            if _code not in rt_prices: continue
            _r = _score_it(_code, _ctx)
            _rt = rt_prices[_code]
            _r['rt_price'] = _rt['price']
            _r['change_pct'] = round((_rt['price'] - _rt['pre_close']) / _rt['pre_close'] * 100, 2)
            _p6_list.append(_r)
        _p6_list = _calib(_p6_list)

        written = 0
        errors = []
        for _r in _p6_list:
            try:
                _code = _r['ts_code']
                _v = _r['calibrated_score']
                _sig = classify_signal(_v, 'momentum' if _r['track']=='momentum' else 'reversion', {'trend': 50})
                _signal = getattr(_sig, 'signal', 'HOLD')
                _sig_label = getattr(_sig, 'label', '持有')
                
                _name = ''
                cur.execute("SELECT name FROM watch_pool WHERE ts_code=%s AND is_active=1", (_code,))
                _rn = cur.fetchone()
                _name = _rn['name'] if _rn else _code

                cur.execute("""
                    INSERT INTO realtime_score
                        (ts_code, name, trade_date, query_time, rt_price, pre_close, change_pct,
                         cycle_score, structure_score, emotion_score, composite_score,
                         signal_type, signal_label, position_pct, season, regime)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE
                        query_time=VALUES(query_time), rt_price=VALUES(rt_price),
                        change_pct=VALUES(change_pct), composite_score=VALUES(composite_score),
                        signal_type=VALUES(signal_type), signal_label=VALUES(signal_label)
                """, (
                    _code, _name, trade_date, now,
                    round(_r['rt_price'], 3), round(rt_prices[_code]['pre_close'], 3),
                    _r['change_pct'],
                    round(_r['score'], 2), round(_r['score'], 2),
                    round(_r['score'], 2), round(_v, 2),
                    _signal, _sig_label, 50, _ctx.season, _ctx.regime
                ))
                written += 1
            except Exception as _e2:
                errors.append(f'{_code}:{_e2}')

            if written % 20 == 0:
                _conn.commit()

        _conn.commit()
        cur.close(); _conn.close()

        log.append(f'评分{written}只')
        if errors:
            log.append(f'错误{len(errors)}')
            logger.warning(f"实时评分错误: {errors[:5]}")

        logger.info(f"⏱️ 实时评分刷新完成: {', '.join(log)}")
        return api_success({'status': 'completed', 'total': len(all_codes), 'written': written, 'errors': errors[:5]})
    except Exception as e:
        logger.error(f"refresh_realtime error: {e}")
        return api_error(str(e))


# ─── GET /api/v1/management/realtime-scores ────────────────
@app.route('/api/v1/management/realtime-scores', methods=['GET'])
def realtime_scores():
    """获取最新实时评分"""
    try:
        limit = request.args.get('limit', 50, type=int)
        with db_cursor(commit=False) as cur:
            cur.execute("""
                SELECT * FROM realtime_score
                WHERE query_time = (SELECT MAX(query_time) FROM realtime_score)
                ORDER BY composite_score DESC
                LIMIT %s
            """, (limit,))
            rows = cur.fetchall()
            # 获取最新查询时间
            cur.execute("SELECT MAX(query_time) AS qt FROM realtime_score")
            qt = cur.fetchone()

        return api_success({
            'list': serialize_rows(rows),
            'query_time': str(qt['qt']) if qt else '',
            'total': len(rows),
        })
    except Exception as e:
        logger.error(f"realtime_scores error: {e}")
        return api_error(str(e))


# ─── 股票池管理 ─────────────────────────────────────────────
@app.route('/api/v1/management/stock-pool', methods=['GET'])
def stock_pool_list():
    """获取股票池"""
    try:
        pool_name = request.args.get('pool')

        with db_cursor(commit=False) as cur:
            if pool_name:
                cur.execute(
                    """SELECT sp.*, sb.name, sb.industry
                       FROM stock_pool sp LEFT JOIN stock_basic sb ON sp.ts_code = sb.ts_code
                       WHERE sp.pool_name=%s AND sp.status='ACTIVE'
                       ORDER BY sp.priority""",
                    [pool_name]
                )
            else:
                cur.execute(
                    """SELECT sp.*, sb.name, sb.industry
                       FROM stock_pool sp LEFT JOIN stock_basic sb ON sp.ts_code = sb.ts_code
                       WHERE sp.status='ACTIVE' ORDER BY sp.pool_name, sp.priority"""
                )
            rows = cur.fetchall()

        return api_success({'count': len(rows), 'stocks': serialize_rows(rows)})
    except Exception as e:
        logger.error(f"stock_pool_list error: {e}")
        return api_error(str(e))


@app.route('/api/v1/management/stock-pool', methods=['POST'])
def stock_pool_manage():
    """股票池增删"""
    try:
        data = request.get_json(force=True)
        action = data.get('action')
        ts_code = data.get('ts_code')
        pool_name = data.get('pool_name')
        reason = data.get('reason', '')

        if not all([action, ts_code, pool_name]):
            return api_error("action, ts_code, pool_name必填", code=1001, http_status=400)

        with db_cursor(commit=True) as cur:
            if action == 'add':
                cur.execute(
                    """INSERT INTO stock_pool (ts_code, pool_name, status, add_date, add_reason)
                       VALUES (%s, %s, 'ACTIVE', CURDATE(), %s)
                       ON DUPLICATE KEY UPDATE status='ACTIVE', add_reason=VALUES(add_reason),
                           remove_date=NULL, remove_reason=NULL""",
                    [ts_code, pool_name, reason]
                )
            elif action in ('remove', 'archive'):
                new_status = 'INACTIVE' if action == 'remove' else 'ARCHIVED'
                cur.execute(
                    """UPDATE stock_pool SET status=%s, remove_date=CURDATE(), remove_reason=%s
                       WHERE ts_code=%s AND pool_name=%s""",
                    [new_status, reason, ts_code, pool_name]
                )
            else:
                return api_error(f"未知操作: {action}", code=1000, http_status=400)

        return api_success({'action': action, 'ts_code': ts_code, 'pool': pool_name})
    except Exception as e:
        logger.error(f"stock_pool_manage error: {e}")
        return api_error(str(e))


# ─── 回测股票池管理 (/api/v1/management/backtest-pool) ──────

@app.route('/api/v1/management/backtest-pool', methods=['GET'])
def backtest_pool_list():
    """获取回测股票池列表(支持筛选+分页)"""
    try:
        status = request.args.get('status', 'ACTIVE')
        industry = request.args.get('industry')
        market = request.args.get('market')
        keyword = request.args.get('keyword')
        page = int(request.args.get('page', 1))
        page_size = min(int(request.args.get('page_size', 50)), 200)
        offset = (page - 1) * page_size

        wheres = ["bp.status = %s"]
        params = [status]
        if industry:
            wheres.append("bp.industry = %s")
            params.append(industry)
        if market:
            wheres.append("bp.market = %s")
            params.append(market)
        if keyword:
            wheres.append("(bp.name LIKE %s OR bp.ts_code LIKE %s)")
            like_val = f"%{keyword}%"
            params.extend([like_val, like_val])

        where_clause = " AND ".join(wheres)

        with db_cursor(commit=False) as cur:
            cur.execute(f"SELECT COUNT(*) as cnt FROM backtest_pool bp WHERE {where_clause}", params)
            total = cur.fetchone()['cnt']
            cur.execute(
                f"""SELECT bp.* FROM backtest_pool bp WHERE {where_clause}
                    ORDER BY bp.market ASC, bp.industry ASC, bp.ts_code ASC
                    LIMIT %s OFFSET %s""",
                params + [page_size, offset]
            )
            rows = cur.fetchall()
            cur.execute("SELECT DISTINCT industry FROM backtest_pool WHERE status='ACTIVE' ORDER BY industry")
            industries = [r['industry'] for r in cur.fetchall()]

        return api_success({
            'stocks': serialize_rows(rows),
            'filters': {
                'industries': industries,
                'markets': ['主板', '创业板', '科创板']
            },
            'pagination': {
                'page': page, 'page_size': page_size,
                'total': total,
                'total_pages': max(1, (total + page_size - 1) // page_size)
            }
        })
    except Exception as e:
        logger.error(f"backtest_pool_list error: {e}")
        return api_error(str(e))


@app.route('/api/v1/management/backtest-pool', methods=['POST'])
def backtest_pool_manage():
    """回测股票池增/删/改"""
    try:
        data = request.get_json(force=True)
        action = data.get('action')
        if not action:
            return api_error("action必填 (add/update/remove/archive/activate/batch_add)", code=1001, http_status=400)

        with db_cursor(commit=True) as cur:
            if action == 'add':
                ts_code = data.get('ts_code')
                name = data.get('name')
                if not ts_code or not name:
                    return api_error("ts_code和name必填", code=1001, http_status=400)
                cur.execute(
                    """INSERT INTO backtest_pool (ts_code, name, industry, market, status, notes)
                       VALUES (%s, %s, %s, %s, 'ACTIVE', %s)
                       ON DUPLICATE KEY UPDATE status='ACTIVE', name=VALUES(name),
                       industry=VALUES(industry), market=VALUES(market), notes=VALUES(notes)""",
                    [ts_code, name, data.get('industry', ''), data.get('market', ''), data.get('notes', '')]
                )
                msg = f"已添加/激活: {ts_code} {name}"

            elif action == 'update':
                ts_code = data['ts_code']
                fields = {}
                for f in ['name', 'industry', 'market', 'notes', 'status']:
                    if f in data:
                        fields[f] = data[f]
                if not fields:
                    return api_error("无更新字段", code=1001, http_status=400)
                set_clause = ", ".join(f"{k}=%s" for k in fields)
                vals = list(fields.values()) + [ts_code]
                cur.execute(f"UPDATE backtest_pool SET {set_clause} WHERE ts_code=%s", vals)
                msg = f"已更新: {ts_code}"

            elif action == 'remove':
                ts_code = data['ts_code']
                cur.execute("DELETE FROM backtest_pool WHERE ts_code=%s", [ts_code])
                cur.execute("DELETE FROM stock_pool WHERE ts_code=%s AND pool_name='backtest'", [ts_code])
                msg = f"已删除: {ts_code}"

            elif action == 'archive':
                ts_code = data['ts_code']
                cur.execute(
                    "UPDATE backtest_pool SET status='ARCHIVED', notes=CONCAT(COALESCE(notes,''), '|已归档') WHERE ts_code=%s",
                    [ts_code]
                )
                msg = f"已归档: {ts_code}"

            elif action == 'activate':
                ts_code = data['ts_code']
                cur.execute("UPDATE backtest_pool SET status='ACTIVE' WHERE ts_code=%s", [ts_code])
                msg = f"已激活: {ts_code}"

            elif action == 'batch_add':
                items = data.get('items', [])
                if not items:
                    return api_error("items数组必填", code=1001, http_status=400)
                added = 0
                for item in items:
                    cur.execute(
                        """INSERT INTO backtest_pool (ts_code, name, industry, market, status, notes)
                           VALUES (%s, %s, %s, %s, 'ACTIVE', %s)
                           ON DUPLICATE KEY UPDATE status='ACTIVE', name=VALUES(name),
                           industry=VALUES(industry), market=VALUES(market), notes=VALUES(notes)""",
                        [item['ts_code'], item['name'], item.get('industry', ''),
                         item.get('market', ''), item.get('notes', '')]
                    )
                    added += 1
                msg = f"批量添加完成: {added} 条"

            else:
                return api_error(f"未知操作: {action}", code=1000, http_status=400)

        return api_success({'action': action, 'message': msg})
    except Exception as e:
        logger.error(f"backtest_pool_manage error: {e}")
        return api_error(str(e))


@app.route('/api/v1/management/backtest-pool/<ts_code>', methods=['GET'])
def backtest_pool_detail(ts_code):
    """获取单只回测池股票详情"""
    try:
        with db_cursor(commit=False) as cur:
            cur.execute("SELECT * FROM backtest_pool WHERE ts_code=%s", [ts_code])
            row = cur.fetchone()
        if not row:
            return api_not_found()
        return api_success(serialize_rows([row])[0])
    except Exception as e:
        logger.error(f"backtest_pool_detail error: {e}")
        return api_error(str(e))

# ─── GET /api/v1/management/signal-cards ────────────────────
@app.route('/api/v1/management/signal-cards', methods=['GET'])
def signal_cards():
    """Phase 3: 信号卡片 - 基于v2.0评分+v3.0季节生成BUY/HOLD/SELL（从数据库读取，不重复评分）"""
    try:
        cards = []
        emoji_map = {'spring':'🌸','summer':'☀️','autumn':'🍂','winter':'❄️','chaos':'🌪️','chaos_spring':'🌤️','chaos_autumn':'🌥️','panic':'💀','recovery':'🌱'}

        with db_cursor(commit=False) as cur:
            cur.execute("SELECT season, raw_score FROM season_state WHERE index_code='MARKET' ORDER BY trade_date DESC LIMIT 1")
            mr = cur.fetchone()
            mkt_season = mr['season'] if mr else 'chaos'
            mkt_score = float(mr['raw_score'] or 0) if mr else 0
            
            cur.execute("""
                SELECT ss.ts_code, sb.name, ss.calibrated_score as composite_score,
                       ss.composite_score as raw_score, ss.calibrated_score as cycle_score,
                       ss.calibrated_score as structure_score, 0 as emotion_score,
                       dk.close as close_price,
                       CASE WHEN ss.calibrated_score >= 60 THEN 'STRONG_BUY'
                            WHEN ss.calibrated_score >= 45 THEN 'BUY'
                            WHEN ss.calibrated_score >= 35 THEN 'CAUTIOUS_BUY'
                            WHEN ss.calibrated_score >= 20 THEN 'HOLD'
                            ELSE 'SELL' END as direction,
                       ss.calibrated_score as position_pct, '' as reason_chain,
                       ss.track as operation_mode, ss.track, ss.calibrated_score, ss.scoring_strategy
                FROM strategy_signal ss
                JOIN watch_pool wp ON ss.ts_code = wp.ts_code AND wp.is_active=1
                LEFT JOIN stock_basic sb ON ss.ts_code = sb.ts_code
                LEFT JOIN daily_kline dk ON ss.ts_code = dk.ts_code AND dk.trade_date = ss.trade_date
                WHERE ss.direction='dual_track_v1' AND ss.trade_date = (SELECT MAX(trade_date) FROM strategy_signal WHERE direction='dual_track_v1')
                ORDER BY ss.calibrated_score DESC
            """)
            rows = cur.fetchall()
            
        for r in rows:
            ts_code = r['ts_code']
            v_score = float(r['composite_score'] or 50)
            signal = r['direction'] or 'HOLD'
            sig_label = '⏸️持有'
            # 从direction推断信号标签
            emoji_label = {'BUY':'🟢买入','STRONG_BUY':'🟢强烈买入','CAUTIOUS_BUY':'🟡谨慎买入','HOLD':'⏸️持有','SELL':'🔴卖出','WAIT':'⏳等待','REV_BUY':'🟣反转买入'}
            sig_label = emoji_label.get(signal, '⏸️持有')
            
            cards.append({
                'ts_code': ts_code,
                'name': r['name'] or '',
                'close': float(r['close_price'] or 0),
                'change_pct': 0,
                'raw_score': float(r['raw_score'] or 50),
                'v_score': v_score,
                'cycle_score': float(r['cycle_score'] or 0),
                'chanlun_score': float(r['structure_score'] or 0),
                'sentiment_score': float(r['emotion_score'] or 0),
                'trend_score': float(r['structure_score'] or 0),
                'momentum_score': 0,
                'volatility_score': 0,
                'volume_score': 0,
                'signal': signal,
                'signal_label': sig_label,
                'position_pct': float(r['position_pct'] or 0),
                'strategy': r['operation_mode'] or 'momentum',
                'stop_loss_pct': -0.05,
                'industry': '',
                'chanlun_signal': 0,
                'risk_flags': [],
                'season': mkt_season,
                'season_emoji': emoji_map.get(mkt_season, '❓'),
                'p6_star': 1 if ts_code in ('000021.SZ','000977.SZ','002463.SZ','300274.SZ','601869.SH') else 0,
            })
        
        cards.sort(key=lambda x: x['v_score'], reverse=True)
        
        return api_success({
            'season': mkt_season,
            'season_emoji': emoji_map.get(mkt_season, '❓'),
            'total': len(cards),
            'buy_count': sum(1 for c in cards if c['signal'] in ('STRONG_BUY','BUY')),
            'cautious_count': sum(1 for c in cards if c['signal'] == 'CAUTIOUS_BUY'),
            'hold_count': sum(1 for c in cards if c['signal'] == 'HOLD'),
            'sell_count': sum(1 for c in cards if c['signal'] == 'SELL'),
            'cards': cards,
        })
    except Exception as e:
        logger.error(f"signal_cards error: {e}")
        return api_error(str(e))

# ─── GET /api/v1/management/backtest-multi-cycle ─────────────
@app.route('/api/v1/management/backtest-multi-cycle', methods=['GET'])
def backtest_multi_cycle():
    """Phase 3: 多周期回测对比数据"""
    try:
        code = request.args.get('ts_code', '')
        periods = [5,10,20,30,60]

        with db_cursor(commit=False) as cur:
            cur.execute("SELECT close, trade_date FROM daily_kline_qfq WHERE ts_code=%s ORDER BY trade_date ASC", [code])
            rows = cur.fetchall()

        if len(rows) < 121:
            return api_error('数据不足')

        closes = [float(r['close']) for r in rows]

        result = {'ts_code': code, 'periods': []}
        for p in periods:
            rets = []
            for i in range(120, len(rows)):
                if i + p < len(rows) and closes[i] > 0:
                    rets.append((closes[i+p] - closes[i]) / closes[i] * 100)
            if rets:
                avg = sum(rets)/len(rets)
                pos = sum(1 for r in rets if r > 0)/len(rets)
                result['periods'].append({
                    'days': p, 'avg_return': round(avg, 2),
                    'win_rate': round(pos*100, 1), 'samples': len(rets)
                })

        return api_success(result)
    except Exception as e:
        logger.error(f"backtest_multi_cycle error: {e}")
        return api_error(str(e))

# ─── GET /api/v1/management/alerts ───────────────────────────
@app.route('/api/v1/management/alerts', methods=['GET'])
def alerts():
    """Phase 3: 恐慌/复苏预警"""
    try:
        with db_cursor(commit=False) as cur:
            # 最近四季状态
            cur.execute("SELECT season, raw_score, trade_date FROM season_state WHERE index_code='MARKET' ORDER BY trade_date DESC LIMIT 20")
            rows = cur.fetchall()

        alerts = []
        if rows:
            latest = rows[0]
            # 恐慌检测: 连续5天评分<-3
            last5 = rows[:5]
            avg_score = sum(float(r['raw_score'] or 0) for r in last5) / len(last5)
            if float(latest['raw_score'] or 0) < -3 and avg_score < -2:
                alerts.append({'level':'panic','message':'💀 市场疑似进入恐慌状态,连续评分<-3,建议关注极端反转机会'})
            elif float(latest['raw_score'] or 0) > 5:
                alerts.append({'level':'info','message':'🔥 市场评分较高,注意追高风险'})
            elif latest['season'] in ('winter','autumn'):
                alerts.append({'level':'warning','message':'❄️ 市场处于防守期,建议降低仓位'})

        return api_success({'alerts': alerts, 'total': len(alerts)})
    except Exception as e:
        logger.error(f"alerts error: {e}")
        return api_error(str(e))


# ─── GET /api/v1/management/portfolio/holdings ─────────────
@app.route('/api/v1/management/portfolio/holdings', methods=['GET'])
def portfolio_holdings():
    """查询当前持仓"""
    try:
        trade_date = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))
        with db_cursor(commit=False) as cur:
            cur.execute("""
                SELECT ph.*, ss.season as market_season
                FROM portfolio_holdings ph
                LEFT JOIN (SELECT season FROM season_state WHERE index_code='MARKET' ORDER BY trade_date DESC LIMIT 1) ss ON 1=1
                WHERE ph.status='HOLDING' AND ph.trade_date = (
                    SELECT MAX(trade_date) FROM portfolio_holdings WHERE ts_code=ph.ts_code AND status='HOLDING'
                )
                ORDER BY ph.market_value DESC
            """)
            rows = cur.fetchall()

            # 账户总资产
            cur.execute("SELECT * FROM portfolio_account ORDER BY trade_date DESC LIMIT 1")
            account = cur.fetchone()

        holdings = serialize_rows(rows)
        # 数字字段类型转换
        for h in holdings:
            for _k in ['qty','avail_qty']:
                if _k in h: h[_k] = int(h[_k]) if h[_k] else 0
            for _k in ['current_price','cost_price','market_value','profit_amount','profit_pct']:
                if _k in h: h[_k] = float(h[_k]) if h[_k] else 0.0
            # 计算已持仓天数(实际交易日:统计交易日天数,不是全量K线行数)
            buy_date = h.get('trade_date')
            hold_days = 0
            if buy_date:
                try:
                    from datetime import datetime as _dt
                    td_str = trade_date[:10] if len(trade_date) >= 10 and trade_date[4]=='-' else _dt.now().strftime('%Y-%m-%d')
                    bd_str = str(buy_date)[:10]
                    c2 = db_cursor(commit=False)
                    try:
                        with c2 as c:
                            c.execute(
                                "SELECT COUNT(DISTINCT trade_date) AS cnt FROM daily_kline WHERE trade_date BETWEEN %s AND %s",
                                (bd_str, td_str)
                            )
                            r2 = c.fetchone()
                            hold_days = r2['cnt'] if r2 else 0
                    except:
                        pass
                except:
                        pass
            h['hold_days'] = hold_days
            h['over_20d'] = 1 if hold_days >= 20 else 0

        # 原子化价格更新：尝试实时价 → 回退收盘价（两个路径互斥，不混合）
        _price_updated = False
        try:
            import tushare as _ts, os
            _token = os.environ.get('TUSHARE_TOKEN', '')
            if not _token:
                import pymysql
                from db_config import get_connection as _getdb_c2
                _c2 = _getdb_c2()
                _cu2 = _c2.cursor()
                _cu2.execute("SELECT api_key FROM openclaw_config.api_credentials WHERE name='TUSHARE_TOKEN' AND is_active=1")
                _r2 = _cu2.fetchone()
                if _r2: _token = _r2[0]
                _cu2.close(); _c2.close()
            if _token:
                _ts.set_token(_token)
                _pro = _ts.pro_api()
                _codes = [h['ts_code'] for h in holdings]
                if _codes:
                    _rt_all = _pro.rt_k(ts_code=','.join(_codes))
                    if _rt_all is not None and len(_rt_all) > 0:
                        _price_map = dict(zip(_rt_all['ts_code'], _rt_all['close'].astype(float)))
                        for h in holdings:
                            if h['ts_code'] in _price_map:
                                np = _price_map[h['ts_code']]
                                if np and np > 0:
                                    h['current_price'] = np
                                    _calc = lambda q,c,np: (round(q*np,2), round((np-c)*q,2), round(((np-c)/c*100) if c>0 else 0,2))
                                    h['market_value'], h['profit_amount'], h['profit_pct'] = _calc(float(h.get('qty',0)), float(h.get('cost_price',0)), np)
                                    _price_updated = True
        except Exception as _e:
            logger.warning(f"实时价获取失败: {_e}")

        # rt_k未更新时，用daily_kline收盘价统一更新（不混合两种价格）
        if not _price_updated:
            try:
                with db_cursor(commit=False) as _dk_cur:
                    for _h in holdings:
                        _dk_cur.execute(
                            "SELECT `close` FROM daily_kline WHERE ts_code=%s ORDER BY trade_date DESC LIMIT 1",
                            (_h['ts_code'],)
                        )
                        _dk_row = _dk_cur.fetchone()
                        if _dk_row:
                            _close = float(_dk_row['close'])
                            if _close > 0:
                                _h['current_price'] = _close
                                _calc = lambda q,c,np: (round(q*np,2), round((np-c)*q,2), round(((np-c)/c*100) if c>0 else 0,2))
                                _h['market_value'], _h['profit_amount'], _h['profit_pct'] = _calc(float(_h.get('qty',0)), float(_h.get('cost_price',0)), _close)
            except Exception as _dk_e:
                logger.warning(f"收盘价回退失败: {_dk_e}")

        total_mv = sum(float(h['market_value'] or 0) for h in holdings)
        total_pa = sum(float(h['profit_amount'] or 0) for h in holdings)

        acc_dict = serialize_rows([account])[0] if account else None
        if acc_dict:
            for _k in ['total_assets','total_market_value','available_cash']:
                if _k in acc_dict: acc_dict[_k] = float(acc_dict[_k]) if acc_dict[_k] else 0.0
        return api_success({
            'date': trade_date,
            'account': acc_dict,
            'total_market_value': round(total_mv, 2),
            'total_profit': round(total_pa, 2),
            'holdings': holdings,
        })
    except Exception as e:
        logger.error(f"portfolio_holdings error: {e}")
        return api_error(str(e))

# ─── GET /api/v1/management/portfolio/history ────────────────
@app.route('/api/v1/management/portfolio/history', methods=['GET'])
def portfolio_hist():
    """查询持仓历史"""
    try:
        ts_code = request.args.get('ts_code', '')
        limit = int(request.args.get('limit', 30))

        with db_cursor(commit=False) as cur:
            if ts_code:
                cur.execute("SELECT * FROM portfolio_holdings WHERE ts_code=%s ORDER BY trade_date DESC LIMIT %s", (ts_code, limit))
            else:
                cur.execute("SELECT * FROM portfolio_holdings ORDER BY trade_date DESC LIMIT %s", (limit,))
            rows = cur.fetchall()

        return api_success({'records': serialize_rows(rows)})
    except Exception as e:
        logger.error(f"portfolio_history error: {e}")
        return api_error(str(e))

# ─── GET /api/v1/management/portfolio/account-history ──────
@app.route('/api/v1/management/portfolio/account-history', methods=['GET'])
def acct_hist():
    try:
        limit = int(request.args.get('limit', 30))
        with db_cursor(commit=False) as cur:
            cur.execute("SELECT * FROM portfolio_account ORDER BY trade_date DESC LIMIT %s", (limit,))
            rows = cur.fetchall()
        return api_success({'records': serialize_rows(rows)})
    except Exception as e:
        logger.error(f"account_history error: {e}")
        return api_error(str(e))




# ─── POST /api/v1/management/portfolio/update-position-date ─┐
@app.route('/api/v1/management/portfolio/update-position-date', methods=['POST'])
def update_position_date():
    try:
        data = request.get_json()
        ts_code = data.get('ts_code', '')
        if ts_code and '.' not in ts_code:
            ts_code = ts_code + '.SZ' if ts_code[0] in '30' else ts_code + '.SH'
        new_date = data.get('trade_date', '')
        user_id = data.get('user_id', _get_user_id())
        if not ts_code or not new_date:
            return api_error('参数不足')
        with db_cursor() as cur:
            # 先检查目标日期是否已被占用（其他记录的唯一键冲突）
            cur.execute("""SELECT id FROM portfolio_holdings 
                WHERE user_id=%s AND ts_code=%s AND trade_date=%s
            """, (user_id, ts_code, new_date))
            conflict = cur.fetchone()
            if conflict:
                # 有冲突记录, 先把冲突记录的trade_date改成NULL或更早日期
                cur.execute("""UPDATE portfolio_holdings SET trade_date='2000-01-01' 
                    WHERE id=%s""", (conflict['id'],))
            # 更新持仓记录的建仓日期
            cur.execute("""
                UPDATE portfolio_holdings SET trade_date=%s, updated_at=NOW()
                WHERE user_id=%s AND ts_code=%s AND status='HOLDING'
                ORDER BY trade_date DESC LIMIT 1
            """, (new_date, user_id, ts_code))
        return api_success({'ts_code': ts_code, 'trade_date': new_date})
    except Exception as e:
        logger.error(f"update_position_date error: {e}")
        return api_error(str(e))

# ─── GET /api/v1/management/portfolio/recalc ─────────────────
@app.route('/api/v1/management/portfolio/recalc', methods=['GET'])
def portfolio_recalc():
    """重算单只股票评分和建议"""
    try:
        ts_code = request.args.get('ts_code', '')
        if not ts_code: return api_error('缺少ts_code')

        from p6_dual_track_engine import MarketContext, score_stock, calibrate_scores
        from season_engine import SeasonEngine
        with db_cursor(commit=False) as cur:
            cur.execute("SELECT season FROM season_state WHERE index_code='MARKET' ORDER BY trade_date DESC LIMIT 1")
            mr = cur.fetchone()
            mkt_sea = mr['season'] if mr else 'chaos'

            cur.execute("SELECT raw_score FROM season_state WHERE index_code='000300.SH' ORDER BY trade_date DESC LIMIT 1")
            i300 = cur.fetchone()
            regime = 'bull' if i300 and float(i300['raw_score']or 0)>3 else ('bear' if i300 and float(i300['raw_score']or 0)<-2 else 'range')

            cur.execute("SELECT MAX(trade_date) as d FROM daily_kline")
            ld = cur.fetchone()['d']
            cur.execute("SELECT COUNT(*) as t, SUM(CASE WHEN change_pct>0 THEN 1 ELSE 0 END) as up FROM daily_kline WHERE trade_date=%s",(ld,))
            br = cur.fetchone()
            breadth = br['up']/br['t'] if br and br['t'] else 0.5

            cur.execute("SELECT industry FROM stock_basic WHERE ts_code=%s",(ts_code,))
            ind_r = cur.fetchone(); industry = ind_r['industry'] if ind_r else '未知'

            cur.execute("SELECT trade_date, high, low, close, vol, change_pct FROM daily_kline_qfq WHERE ts_code=%s ORDER BY trade_date ASC",(ts_code,))
            rows = cur.fetchall()
            if len(rows) < 200: return api_error(f'{ts_code} 数据不足({len(rows)}日)')

            closes = [float(r['close']) for r in rows]; vols = [float(r.get('vol',0)or 0) for r in rows]
            chgs = [float(r.get('change_pct')or 0) for r in rows]; n=len(closes)
            all_win=[{'close':closes[i],'high':float(rows[i]['high']),'low':float(rows[i]['low']),'vol':vols[i]} for i in range(n)]

            bw = get_block_weights(industry)
            chanlun = score_chanlun_enhanced(all_win, mkt_sea, industry, ts_code=ts_code)
            cycle = score_cycle_enhanced(mkt_sea, regime, 2.0, industry, closes)
            l2_raw = apply_block_weights(chanlun['trend'],chanlun['momentum'],chanlun['volatility'],chanlun['volume'],bw)
            cl_total = round(max(0,min(100,l2_raw+chanlun['chanlun_signal']*0.15)),1)
            r14=rsi(closes,14); v5m=sma(vols[-10:],5) if len(vols)>=10 else vols[-1]; v20m=sma(vols[-25:],20) if len(vols)>=25 else v5m
            vol_reg='high' if v5m>v20m*1.3 else ('low' if v5m<v20m*0.7 else 'normal')
            sent=score_sentiment(breadth,vol_reg,r14,chgs[-1] if chgs else 0)
            raw=cycle.score*0.30+cl_total*0.40+sent.score*0.30; v=vmap_score(raw,25)
            
            from engine.vmap import classify_signal
            sig_result=classify_signal(v,cycle.strategy,{'trend':chanlun['trend']})
            sig_type,sig_label=sig_result.signal,sig_result.label
            if sig_type in ('STRONG_BUY','BUY'): advice,reason='🟢 持有/加仓',f'V={v:.0f}/趋势{chanlun["trend"]:.0f}'
            elif sig_type in ('SELL',): advice,reason='🔴 建议卖出',f'V={v:.0f}/趋势{chanlun["trend"]:.0f}'
            else: advice,reason='⏸️ 持有观察',f'V={v:.0f}/趋势{chanlun["trend"]:.0f}'

            cur2 = db_cursor()
            # 实时价: 优先用 Tushare rt_k
            cp=float(closes[-1])
            try:
                import tushare as _ts
                _token = _ts._token_  # 已初始化的token
                if not _token:
                    from db_config import get_connection as _getdb_c2
                    _c2 = _getdb_c2()
                    _cu2 = _c2.cursor()
                    _cu2.execute("SELECT api_key FROM openclaw_config.api_credentials WHERE name='TUSHARE_TOKEN' AND is_active=1")
                    _r2 = _cu2.fetchone()
                    if _r2:
                        _token = _r2[0]
                        _ts.set_token(_token)
                    _cu2.close(); _c2.close()
                if _token:
                    _pro = _ts.pro_api()
                    _rt = _pro.rt_k(ts_code=ts_code)
                    if _rt is not None and len(_rt) > 0:
                        cp = float(_rt.iloc[-1]['close'])
            except:
                pass
            uid3 = _get_user_id()
            with cur2 as cur3:
                cur3.execute("SELECT cost_price, qty FROM portfolio_holdings WHERE user_id=%s AND ts_code=%s AND status='HOLDING' ORDER BY trade_date DESC LIMIT 1",(uid3, ts_code))
                hr=cur3.fetchone()
                cost=float(hr['cost_price']) if hr and hr['cost_price'] else 1
                qty=int(hr['qty']) if hr and hr['qty'] else 0
                profit_amt=round((cp-cost)*qty,2)
                profit_pct=round((cp-cost)/cost*100,2) if cost>0 else 0
                cur3.execute("""
                    UPDATE portfolio_holdings SET current_price=%s, profit_amount=%s, profit_pct=%s,
                    advice=%s, advice_reason=%s, updated_at=NOW()
                    WHERE user_id=%s AND ts_code=%s AND status='HOLDING'
                    ORDER BY trade_date DESC LIMIT 1
                """, (cp, profit_amt, profit_pct, advice, reason, uid3, ts_code))

            return api_success({'ts_code':ts_code,'v_score':v,'advice':advice,'reason':reason})
    except Exception as e:
        logger.error(f"portfolio_recalc error: {e}")
        return api_error(str(e))

# ─── GET /api/v1/management/portfolio/recalc-all ──────────────
@app.route('/api/v1/management/portfolio/recalc-all', methods=['GET'])
def portfolio_recalc_all():
    try:
        import sys,os
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from score_engine import score_chanlun_enhanced
        from engine.cycle_scorer import score_cycle_enhanced
        from engine.indicators import rsi, sma
        from engine.sentiment_scorer import score_sentiment
        from engine.block_weights import get_block_weights, apply_block_weights
        from engine.vmap import vmap_score

        with db_cursor(commit=False) as cur:
            cur.execute("""
                SELECT ph.* FROM portfolio_holdings ph
                INNER JOIN (SELECT ts_code, MAX(trade_date) as md FROM portfolio_holdings WHERE status='HOLDING' GROUP BY ts_code) lst
                ON ph.ts_code=lst.ts_code AND ph.trade_date=lst.md WHERE ph.status='HOLDING'
            """)
            holdings = cur.fetchall()

            cur.execute("SELECT season FROM season_state WHERE index_code='MARKET' ORDER BY trade_date DESC LIMIT 1")
            mr=cur.fetchone(); mkt_sea=mr['season'] if mr else 'chaos'
            cur.execute("SELECT raw_score FROM season_state WHERE index_code='000300.SH' ORDER BY trade_date DESC LIMIT 1")
            i300=cur.fetchone(); regime='bull' if i300 and float(i300['raw_score']or 0)>3 else ('bear' if i300 and float(i300['raw_score']or 0)<-2 else 'range')
            cur.execute("SELECT MAX(trade_date) as d FROM daily_kline"); ld=cur.fetchone()['d']
            cur.execute("SELECT COUNT(*) as t, SUM(CASE WHEN change_pct>0 THEN 1 ELSE 0 END) as up FROM daily_kline WHERE trade_date=%s",(ld,))
            br=cur.fetchone(); breadth=br['up']/br['t'] if br and br['t'] else 0.5

        updated=0
        for h in holdings:
            try:
                code=h['ts_code']; cur=db_cursor()
                with cur as c:
                    c.execute("SELECT industry FROM stock_basic WHERE ts_code=%s",(code,))
                    ir=c.fetchone(); industry=ir['industry'] if ir else '未知'
                    c.execute("SELECT trade_date,high,low,close,vol,change_pct FROM daily_kline_qfq WHERE ts_code=%s ORDER BY trade_date ASC",(code,))
                    rows=c.fetchall()
                    if len(rows)<200: continue
                    closes=[float(r['close']) for r in rows]
                    # 从strategy_signal_daily读P6校准分（统一数据源）
                    c.execute('''SELECT buy_score, action FROM strategy_signal_daily 
                        WHERE ts_code=%s AND trade_date=(SELECT MAX(trade_date) FROM strategy_signal_daily) LIMIT 1''', (code,))
                    _sd_row = c.fetchone()
                    if _sd_row and _sd_row.get('buy_score'):
                        v = float(_sd_row['buy_score'])
                        sig = _sd_row.get('action') or 'HOLD'
                    else:
                        v = 50; sig = 'HOLD'
                    # 完全复用strategy页面的动作标签
                    _act_label = {'STOP_LOSS':'🛑 止损','SELL':'🔴 卖出','BUY':'🟢 买入','HOLD':'⏸️ 持有','HOLD_OBSERVE':'🟡 观察','WAIT':'⏳ 等待','CAUTIOUS_BUY':'🟡 谨慎买入','STRONG_BUY':'🟢 强烈买入'}
                    advice = _act_label.get(sig, '⏸️ 持有')
                    reason = f'P6={v:.0f}'
                    # 实时价
                    cp = float(closes[-1])
                    try:
                        import tushare as _ts2
                        _t2 = os.environ.get('TUSHARE_TOKEN', '')
                        if _t2:
                            _ts2.set_token(_t2); _pro2 = _ts2.pro_api()
                            _rt2 = _pro2.rt_k(ts_code=code)
                            if _rt2 is not None and len(_rt2) > 0:
                                cp = float(_rt2.iloc[-1]['close'])
                    except: pass
                    cost=float(h['cost_price'] or 1); qty=int(h['qty'] or 0)
                    profit_amt=round((cp-cost)*qty,2)
                    profit_pct=round((cp-cost)/cost*100,2) if cost>0 else 0
                    _uid = os.environ.get('STOCK_USER', 'tony')
                    c.execute("UPDATE portfolio_holdings SET current_price=%s,profit_amount=%s,profit_pct=%s,advice=%s,advice_reason=%s,updated_at=NOW() WHERE user_id=%s AND ts_code=%s AND status='HOLDING' ORDER BY trade_date DESC LIMIT 1",
                              (cp,profit_amt,profit_pct,advice,reason,_uid,code))
                    updated+=1
            except Exception as _re:
                logger.warning(f"  recalc {h.get('ts_code','?')}: {_re}")
                pass

        # 同步持仓状态到strategy_signal_daily
        try:
            with db_cursor() as _sync_cur:
                _sync_cur.execute("""
                    UPDATE strategy_signal_daily ssd 
                    INNER JOIN portfolio_holdings ph ON ssd.ts_code = ph.ts_code AND ph.status='HOLDING'
                    SET ssd.holding_status = 'HOLDING'
                    WHERE ssd.strategy_id=1 AND ssd.trade_date = (SELECT MAX(trade_date) FROM strategy_signal_daily WHERE strategy_id=1)
                """)
                logger.info(f"synced holding status: {_sync_cur.rowcount} rows")
        except Exception as _se:
            logger.warning(f"sync holding status error: {_se}")

        return api_success({'updated': updated})
    except Exception as e:
        logger.error(f"portfolio_recalc_all error: {e}")
        return api_error(str(e))






# ─── POST /api/v1/management/watch-pool/refresh ────────────
@app.route('/api/v1/management/watch-pool/refresh', methods=['POST'])
def watch_pool_refresh():
    """刷新监控池评分快照 → 调用score_engine统一评分后写入watch_pool_snapshot"""
    try:
        import sys, os
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        
        # 调用P6双轨评分引擎统一评分（写入strategy_signal）
        from p6_dual_track_engine import daily_pipeline as _p6_pipe
        _p6_pipe(mode='watch_pool')
        # 从strategy_signal（P6评分源）读取最新评分
        with db_cursor(commit=False) as cur:
            cur.execute("SELECT MAX(trade_date) as d FROM strategy_signal")
            ld = cur.fetchone()
            trade_date = str(ld['d']) if ld and ld['d'] else str(date.today())
            
            cur.execute("""
                    SELECT ss.ts_code, sb.name, sb.industry,
                           ss.composite_score as raw_score, ss.calibrated_score as composite_score,
                           ss.track, ss.scoring_strategy
                    FROM strategy_signal ss
                    JOIN watch_pool wp ON ss.ts_code = wp.ts_code AND wp.is_active=1
                    LEFT JOIN stock_basic sb ON ss.ts_code = sb.ts_code
                    WHERE ss.trade_date=%s ORDER BY ss.calibrated_score DESC
            """, (trade_date,))
            scores = cur.fetchall()
        
        # 写入watch_pool_snapshot
        
        total = len(scores)
        updated = 0
        errors = []
        
        for s in scores:
            code = s['ts_code']
            name = s['name'] or ''
            industry = s['industry'] or '未知'
            
            try:
                with db_cursor(commit=False) as c:
                    # 用P6校准分作为v_score
                    v = float(s.get('composite_score') or 0)
                    raw_score = float(s.get('raw_score') or 0)
                    ts_val = v
                    ms_val = 0
                    
                    # 读取行情信息填充分项
                    c.execute("SELECT trade_date, close, high, low, vol, change_pct FROM daily_kline_qfq WHERE ts_code=%s ORDER BY trade_date ASC", (code,))
                    krows = c.fetchall()
                    _real_close = 0
                    _chg = 0.0
                    # 读取市场季节(放在if外确保始终有值)
                    c.execute("SELECT season FROM season_state WHERE index_code='MARKET' ORDER BY trade_date DESC LIMIT 1")
                    mr = c.fetchone()
                    mkt_sea = mr['season'] if mr else 'chaos'
                    
                    c.execute("SELECT raw_score FROM season_state WHERE index_code='000300.SH' ORDER BY trade_date DESC LIMIT 1")
                    i300 = c.fetchone()
                    regime = 'bull' if i300 and float(i300['raw_score'] or 0) > 3 else ('bear' if i300 and float(i300['raw_score'] or 0) < -2 else 'range')

                    if len(krows) >= 200:
                        closes = [float(r['close']) for r in krows]
                        chgs = [float(r.get('change_pct') or 0) for r in krows]
                        vols = [float(r.get('vol') or 0) for r in krows]
                        
                        # 信号判定（与P6阶梯策略规则对齐）
                        # 买入线≥75（P6 v2.1，May建议P0 + Tony确认）
                        # 5日检视≥40 / 15日≥30 / 25日≥20 续持，否则平仓
                        if v >= 80:
                            signal, sig_label = 'STRONG_BUY', '🟢强烈买入'
                        elif v >= 75:
                            signal, sig_label = 'BUY', '🟢买入'
                        elif v >= 40:
                            signal, sig_label = 'CAUTIOUS_BUY', '🟡谨慎买入'
                        elif v >= 20:
                            signal, sig_label = 'HOLD', '⏸️持有'
                        else:
                            signal, sig_label = 'SELL', '🔴卖出'
                        
                        # 计算收益
                        rets = {}
                        for p in [5, 10, 20]:
                            if len(closes) > p:
                                rets[p] = round((closes[-1] - closes[-p-1]) / closes[-p-1] * 100, 2)
                        
                        # 真实收盘价（优先从daily_kline取，回退到daily_kline_qfq）
                        c.execute("SELECT close, change_pct FROM daily_kline WHERE ts_code=%s AND trade_date=%s", (code, trade_date))
                        _kr = c.fetchone()
                        if _kr and _kr['close']:
                            _real_close = float(_kr['close'])
                        else:
                            # 回退到qfq表取当日close
                            c.execute("SELECT close, change_pct FROM daily_kline_qfq WHERE ts_code=%s AND trade_date=%s", (code, trade_date))
                            _kr2 = c.fetchone()
                            _real_close = float(_kr2['close']) if _kr2 and _kr2['close'] else 0
                        _chg = chgs[-1] if chgs else 0.0
                    else:
                        signal, sig_label = 'WAIT', '⏳数据不足'
                        rets = {5: 0, 10: 0, 20: 0}
                    
                    c.execute("""
                        INSERT INTO watch_pool_snapshot
                        (ts_code, name, trade_date, close_price, change_pct, raw_score, v_score,
                         trend_score, momentum_score, volatility_score, volume_score,
                         signal_type, signal_label, position_pct, stop_loss_pct, strategy_type,
                         season, regime, ret_5d, ret_10d, ret_20d)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON DUPLICATE KEY UPDATE
                            close_price=VALUES(close_price), change_pct=VALUES(change_pct),
                            raw_score=VALUES(raw_score), v_score=VALUES(v_score),
                            signal_type=VALUES(signal_type), signal_label=VALUES(signal_label),
                            position_pct=VALUES(position_pct), trend_score=VALUES(trend_score)
                    """, (code, name, trade_date, _real_close, _chg, round(raw_score, 1), v,
                          ts_val, ms_val, 0, 0,
                          signal, sig_label, 0, 0, 'momentum',
                          mkt_sea, regime, rets.get(5, 0), rets.get(10, 0), rets.get(20, 0)))
                    updated += 1
            except Exception as e2:
                errors.append(f"{code}: {e2}")
        
        return api_success({'total': total, 'updated': updated, 'errors': errors[:5], 'trade_date': trade_date})
    except Exception as e:
        logger.error(f"watch_pool_refresh error: {e}")
        return api_error(str(e))

# ─── GET /api/v1/management/watch-pool/snapshot ────────────
@app.route('/api/v1/management/watch-pool/snapshot', methods=['GET'])
def watch_pool_snapshot():
    try:
        trade_date = request.args.get('date', '')
        with db_cursor(commit=False) as cur:
            # JOIN stock_basic 获取行业信息
            if trade_date:
                cur.execute("""
                    SELECT wps.*, sb.industry
                    FROM watch_pool_snapshot wps
                    LEFT JOIN stock_basic sb ON wps.ts_code = sb.ts_code
                    WHERE wps.trade_date=%s ORDER BY wps.v_score DESC
                """,(trade_date,))
            else:
                cur.execute("""
                    SELECT wps.*, sb.industry
                    FROM watch_pool_snapshot wps
                    LEFT JOIN stock_basic sb ON wps.ts_code = sb.ts_code
                    WHERE wps.trade_date=(SELECT MAX(trade_date) FROM watch_pool_snapshot) ORDER BY wps.v_score DESC
                """)
            rows = cur.fetchall()

            # 补上恒纪元数据(从season_state获取，若无则按季节推断)
            cur.execute(
                "SELECT season, raw_score, hengjiyuan_level, hengjiyuan_score FROM season_state "
                "WHERE index_code='MARKET' ORDER BY trade_date DESC LIMIT 1"
            )
            heng_row = cur.fetchone()

        result_list = serialize_rows(rows)

        if heng_row:
            heng_level = heng_row['hengjiyuan_level']
            heng_score = float(heng_row['hengjiyuan_score']) if heng_row['hengjiyuan_score'] else None
            # 恒纪元字段为空时按季节+评分推断
            if not heng_level:
                season_str = heng_row['season'] or 'chaos'
                score = float(heng_row['raw_score'] or 0)
                if season_str in ('summer', 'spring'):
                    heng_level = 'strong_heng' if score > 2 else 'weak_heng'
                elif season_str in ('chaos', 'chaos_spring'):
                    heng_level = 'weak_heng' if score > 0 else 'weak_luan'
                else:
                    heng_level = 'weak_luan' if score < -1 else 'strong_luan'
                heng_score = max(0, min(100, (score + 10) * 5))
            for item in result_list:
                item['hengjiyuan_level'] = heng_level
                item['hengjiyuan_score'] = heng_score
        else:
            for item in result_list:
                item['hengjiyuan_level'] = 'weak_heng'
                item['hengjiyuan_score'] = 50

        return api_success({'list': result_list, 'date': str(rows[0]['trade_date']) if rows else ''})
    except Exception as e:
        logger.error(f"watch_pool_snapshot error: {e}")
        return api_error(str(e))
# ─── Watch Pool 监控股票池 ─────────────────────────────
@app.route('/api/v1/management/watch-pool/list', methods=['GET'])
def watch_pool_list():
    with db_cursor(commit=False) as cur:
        uid6 = _get_user_id()
        cur.execute("SELECT wp.* FROM watch_pool wp WHERE wp.user_id=%s AND wp.is_active=1 ORDER BY wp.sort_order, wp.created_at", (uid6,))
        return api_success({'list': serialize_rows(cur.fetchall())})

@app.route('/api/v1/management/watch-pool/add', methods=['POST'])
def watch_pool_add():
    data = request.get_json()
    ts_code = data.get('ts_code', '')
    if not ts_code: return api_error('缺少ts_code')
    with db_cursor() as cur:
        uid_val = _get_user_id()
        cur.execute("INSERT INTO watch_pool (user_id, ts_code, name) VALUES (%s, %s, %s) ON DUPLICATE KEY UPDATE is_active=1", (uid_val, ts_code, data.get('name', '')))
    return api_success({'ts_code': ts_code})

@app.route('/api/v1/management/watch-pool/remove', methods=['POST'])
def watch_pool_remove():
    data = request.get_json()
    with db_cursor() as cur:
        uid_val = _get_user_id()
        cur.execute("UPDATE watch_pool SET is_active=0 WHERE user_id=%s AND ts_code=%s", (uid_val, data.get('ts_code', '')))
    return api_success({})

@app.route('/api/v1/management/portfolio/watch-list', methods=['GET'])
def portfolio_watch_list():
    with db_cursor(commit=False) as cur:
        uid4 = _get_user_id()
        cur.execute("""
            SELECT wp.ts_code, wp.name, wp.sort_order,
                   dk.close, dk.change_pct, dk.vol
            FROM watch_pool wp
            LEFT JOIN daily_kline dk ON wp.ts_code = dk.ts_code
                AND dk.trade_date = (SELECT MAX(trade_date) FROM daily_kline)
            WHERE wp.user_id=%s AND wp.is_active=1
            ORDER BY wp.sort_order
        """, (uid4,))
        return api_success({'list': serialize_rows(cur.fetchall())})



# ─── POST /api/v1/management/cycle/refresh ────────────────
@app.route('/api/v1/management/cycle/refresh', methods=['POST'])
def cycle_refresh():
    """手动更新四季季节判定"""
    try:
        import sys, os
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from season_engine import SeasonEngine
        engine = SeasonEngine(use_market_breadth=False)
        r = engine.judge_market_season()
        from datetime import date as dt
        if not r.get('trade_date') or str(r.get('trade_date')) == 'None':
            r['trade_date'] = str(dt.today())
        from season_engine import save_result_to_db; save_result_to_db(r)
        logger.info(f"季节判定已更新: {r['market_season']} 得分{r['raw_score']:.1f}")
        return api_success({'season': r['market_season'], 'raw_score': r['raw_score'], 'confidence': r['market_confidence']})
    except Exception as e:
        logger.error(f"cycle_refresh error: {e}")
        return api_error(str(e))



# ─── GET /api/v1/management/daily-summary ──────────────────
@app.route('/api/v1/management/daily-summary', methods=['GET'])
def daily_summary():
    """统一市场汇总 - 数出一源"""
    try:
        from datetime import date as dt
        from score_engine import score_chanlun_enhanced
        from engine.cycle_scorer import score_cycle_enhanced
        from engine.indicators import rsi, sma
        from engine.sentiment_scorer import score_sentiment
        from engine.block_weights import get_block_weights, apply_block_weights
        from engine.vmap import vmap_score, classify_signal

        with db_cursor(commit=False) as cur:
            # 写一行汇总数据
            cur.execute("SELECT season, raw_score, confidence, position_advice FROM season_state WHERE index_code='MARKET' ORDER BY trade_date DESC LIMIT 1")
            ss = cur.fetchone()
            cur.execute("SELECT hengjiyuan_level, hengjiyuan_score, confidence_mult FROM season_state WHERE index_code='MARKET' ORDER BY trade_date DESC LIMIT 1")
            snap = cur.fetchone()
            cur.execute("SELECT MAX(trade_date) as d FROM daily_kline"); ld=cur.fetchone()['d']
            cur.execute("SELECT COUNT(*) as t, SUM(CASE WHEN change_pct>0 THEN 1 ELSE 0 END) as up FROM daily_kline WHERE trade_date=%s",(ld,))
            br=cur.fetchone(); breadth=round(br['up']/br['t'],3) if br and br['t'] else 0.5

            cur.execute("SELECT COUNT(*) as total, SUM(CASE WHEN signal_type IN ('BUY','STRONG_BUY','CAUTIOUS_BUY') THEN 1 ELSE 0 END) as buy, SUM(CASE WHEN signal_type='SELL' THEN 1 ELSE 0 END) as sell FROM watch_pool_snapshot WHERE trade_date=(SELECT MAX(trade_date) FROM watch_pool_snapshot)")
            wp = cur.fetchone()

            cur.execute("SELECT total_assets, total_market_value, available_cash FROM portfolio_account ORDER BY trade_date DESC LIMIT 1")
            pa = cur.fetchone()

        season = ss['season'] if ss else 'chaos'
        season_score = float(ss['raw_score'] or 0) if ss else 0
        season_conf = float(ss['confidence'] or 0) if ss else 0
        # 恒纪元数据（从season_state取，为空则按季节推断）
        if snap:
            hj_level = snap['hengjiyuan_level']
            hj_score = float(snap['hengjiyuan_score'] or 0) if snap['hengjiyuan_score'] else 0
            hj_conf = float(snap['confidence_mult'] or 0) if snap['confidence_mult'] else 0
        else:
            hj_level = None
            hj_score = 0
            hj_conf = 0
        if not hj_level and ss:
            season_str = ss['season'] or 'chaos'
            score_val = season_score
            if season_str in ('summer', 'spring'):
                hj_level = 'strong_heng' if score_val > 2 else 'weak_heng'
            elif season_str in ('chaos', 'chaos_spring'):
                hj_level = 'weak_heng' if score_val > 0 else 'weak_luan'
            else:
                hj_level = 'weak_luan' if score_val < -1 else 'strong_luan'
            hj_score = max(0, min(100, (score_val + 10) * 5))
            hj_conf = season_conf

        # 拼7因子（从hengjiyuan_score + confidence推算）
        base = max(0, min(100, hj_score))
        conf = max(0, min(1, hj_conf if hj_conf else 0.5))
        # 7因子：多级别一致、中枢紧凑度、中枢稳定性、背驰有效性、成交量有序、波动率结构、分型可靠性
        factors = {
            'multi_level_align': round(base * conf / 100, 2),
            'zhongshu_compact': round(base * (1 - abs(season_score)/10) / 100, 2),
            'zhongshu_stability': round(base * 0.8 / 100, 2),
            'beichi_validity': round(base * min(1, 1.2 - abs(season_score)/20) / 100, 2),
            'volume_orderliness': round((50 + hj_score * 0.3) * conf / 100, 2),
            'volatility_struct': round(max(20, min(80, 50 - abs(hj_score * 0.2))) / 100, 2),
            'fractal_reliability': round((base * 0.6 + 30) * conf / 100, 2),
        }

        data = {
            'trade_date': str(ld) if ld else str(dt.today()),
            'season': season, 'raw_score': season_score, 'season_score': season_score,
            'season_confidence': season_conf,
            'regime': 'bull' if season_score>3 else ('bear' if season_score<-2 else 'range'),
            'hengjiyuan_level': hj_level, 'hengjiyuan_score': hj_score, 'hengjiyuan_confidence': hj_conf,
            'factors': factors,
            'breadth_up_ratio': breadth,
            'watch_pool_total': int(wp['total']) if wp else 0,
            'watch_pool_buy': int(wp['buy']) if wp else 0,
            'watch_pool_sell': int(wp['sell']) if wp else 0,
            'position_advice': ss['position_advice'] if ss and ss.get('position_advice') else '观望',
            'watch_pool_hold': int((wp['total']-wp['buy']-wp['sell'])) if wp else 0,
            'portfolio_total': float(pa['total_market_value']) if pa else 0,
            'portfolio_profit': 0,
            'portfolio_cash': float(pa['available_cash']) if pa else 0,
        }

        # 写入汇总表
        try:
            with db_cursor() as cur2:
                cur2.execute("""INSERT INTO daily_market_summary
                    (trade_date, season, season_score, season_confidence, regime,
                     hengjiyuan_level, hengjiyuan_score, hengjiyuan_confidence,
                     breadth_up_ratio, total_stocks, buy_count, sell_count, hold_count,
                     watch_pool_total, watch_pool_buy, watch_pool_sell, watch_pool_hold,
                     portfolio_total, portfolio_cash)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,0,0,0,0,%s,%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE
                        season=VALUES(season), season_score=VALUES(season_score),
                        hengjiyuan_level=VALUES(hengjiyuan_level), hengjiyuan_score=VALUES(hengjiyuan_score),
                        breadth_up_ratio=VALUES(breadth_up_ratio),
                        watch_pool_total=VALUES(watch_pool_total), watch_pool_buy=VALUES(watch_pool_buy),
                        watch_pool_sell=VALUES(watch_pool_sell), watch_pool_hold=VALUES(watch_pool_hold),
                        portfolio_total=VALUES(portfolio_total), portfolio_cash=VALUES(portfolio_cash)
                """, (data['trade_date'], season, season_score, season_conf, data['regime'],
                      hj_level, hj_score, hj_conf, breadth,
                      data['watch_pool_total'], data['watch_pool_buy'], data['watch_pool_sell'], data['watch_pool_hold'],
                      data['portfolio_total'], data['portfolio_cash']))
        except: pass

        return api_success(data)
    except Exception as e:
        logger.error(f"daily_summary error: {e}")
        return api_error(str(e))


# ─── GET /api/v1/management/market-indexes ──────────────────
@app.route('/api/v1/management/market-indexes', methods=['GET'])
def market_indexes():
    """大盘指数评分状态"""
    try:
        with db_cursor(commit=False) as cur:
            cur.execute("""
                SELECT ss.index_code, sb.name, ss.season, ss.raw_score, ss.confidence, ss.regime, ss.chaos_subtype, ss.trade_date
                FROM season_state ss
                LEFT JOIN stock_basic sb ON ss.index_code = sb.ts_code
                WHERE ss.index_code IN ('000001.SH','000300.SH','399001.SZ','399006.SZ','000688.SH')
                  AND ss.trade_date = (SELECT MAX(trade_date) FROM season_state WHERE index_code=ss.index_code)
                ORDER BY ss.raw_score DESC
            """)
            rows = cur.fetchall()
        indexes = []
        for r in rows:
            se = r['season'] or 'chaos'
            indexes.append({
                'ts_code': r['index_code'],
                'name': r['name'] or r['index_code'],
                'season': se,
                'season_emoji': {'spring':'🌸','summer':'☀️','autumn':'🍂','winter':'❄️',
                                 'chaos':'🌪️','chaos_spring':'🌤️','chaos_autumn':'🌥️',
                                 'panic':'💀','recovery':'🌱'}.get(se, '❓'),
                'raw_score': float(r['raw_score']) if r['raw_score'] else 0,
                'confidence': float(r['confidence']) if r['confidence'] else 0,
                'regime': r.get('regime',''),
                'chaos_subtype': r.get('chaos_subtype',''),
                'trade_date': str(r['trade_date']),
            })
        return api_success({'indexes': indexes, 'total': len(indexes)})
    except Exception as e:
        logger.error(f"market_indexes error: {e}")
        return api_error(str(e))


# ═══ 邮件推送 ═══
_EMAIL_TO = '12211662@qq.com'

def _send_email(subject, body):
    """发送QQ邮件"""
    import smtplib, json
    from email.mime.text import MIMEText
    try:
        cfg_path = '/root/.openclaw/workspace-may/skills/email-sender-tw/scripts/smtp_config.json'
        with open(cfg_path) as f:
            cfg = json.load(f)
        acc = None
        for a in cfg.get('accounts', []):
            if a.get('name') == 'qqmail': acc = a; break
        if not acc: return False, '无QQ邮箱配置'
        msg = MIMEText(body, 'plain', 'utf-8')
        msg['From'] = acc['email']
        msg['To'] = _EMAIL_TO
        msg['Subject'] = subject
        server = smtplib.SMTP_SSL(acc['smtp_server'], acc['smtp_port'])
        server.login(acc['email'], acc['password'])
        server.send_message(msg)
        server.quit()
        return True, 'ok'
    except Exception as e:
        return False, str(e)


# ─── POST /api/v1/management/email/send-report ────────────
@app.route('/api/v1/management/email/send-report', methods=['POST'])
def email_send_report():
    """发送持仓报告邮件"""
    try:
        with db_cursor(commit=False) as cur:
            cur.execute("SELECT season, confidence, position_advice FROM season_state WHERE index_code='MARKET' ORDER BY trade_date DESC LIMIT 1")
            ss = cur.fetchone()
            season = ss['season'] if ss else '?'
            conf = float(ss['confidence']) if ss and ss['confidence'] else 0
            advice = ss.get('position_advice', '') if ss else ''

            cur.execute("""
                SELECT ts_code, name, current_price, cost_price, qty, profit_pct, advice
                FROM portfolio_holdings WHERE status='HOLDING'
                ORDER BY profit_pct DESC
            """)
            holdings = cur.fetchall()

        se_emoji = {'spring':'🌸','summer':'☀️','autumn':'🍂','winter':'❄️',
                     'chaos':'🌪️','chaos_spring':'🌤️','chaos_autumn':'🌥️',
                     'panic':'💀','recovery':'🌱'}.get(season, '❓')
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M')
        body = f'📊 股票持仓报告 — {now_str}\n\n'
        body += f'季节: {se_emoji} {season}\n'
        body += f'置信度: {conf*100:.0f}% | 建议: {advice or "--"}\n\n'
        body += '持仓明细:\n' + '='*40 + '\n'
        for h in holdings:
            pct = float(h['profit_pct'] or 0)
            emoji = '🔴' if pct < 0 else '🟢'
            body += f'{emoji} {h["ts_code"]} {h["name"]}\n'
            body += f'   现价: {float(h["current_price"] or 0):.2f} | 成本: {float(h["cost_price"] or 0):.2f}\n'
            body += f'   持仓: {int(h["qty"] or 0)}股 | 盈亏: {pct:+.2f}%\n'
            body += f'   建议: {h.get("advice","--")}\n\n'
        body += '='*40 + '\n'
        body += '发送自 股票智能分析管理系统 v1.0'

        ok, msg = _send_email('[股票系统] 持仓报告 ' + now_str[:10], body)
        if ok:
            logger.info(f"邮件报告发送成功")
            return api_success({'sent': True, 'to': _EMAIL_TO})
        else:
            return api_error(msg)
    except Exception as e:
        logger.error(f"email_send_report error: {e}")
        return api_error(str(e))


# ─── POST /api/v1/management/email/send-alert ─────────────
@app.route('/api/v1/management/email/send-alert', methods=['POST'])
def email_send_alert():
    """发送预警邮件"""
    try:
        data = request.get_json(force=True) or {}
        alert_type = data.get('type', '普通预警')
        message = data.get('message', '')
        ok, msg = _send_email(f'[股票系统] ⚠️ {alert_type}', message)
        if ok:
            return api_success({'sent': True})
        else:
            return api_error(msg)
    except Exception as e:
        logger.error(f"email_send_alert error: {e}")
        return api_error(str(e))


# ─── POST /api/v1/management/portfolio/lock ────────────────
@app.route('/api/v1/management/portfolio/lock', methods=['POST'])
def portfolio_lock():
    """手动加锁"""
    try:
        data = request.get_json(force=True) or {}
        ts_code = data.get('ts_code', '')
        # 自动补全交易所后缀
        if ts_code and '.' not in ts_code:
            ts_code = ts_code + '.SZ' if ts_code[0] in '30' else ts_code + '.SH'
        days = int(data.get('days', 21))
        user_id = data.get('user_id', _get_user_id())
        if not ts_code: return api_error('参数不足')
        with db_cursor() as cur:
            cur.execute("""
                SELECT ts_code, name, trade_date FROM portfolio_holdings 
                WHERE user_id=%s AND ts_code=%s AND status='HOLDING'
                ORDER BY trade_date DESC LIMIT 1
            """, (user_id, ts_code))
            row = cur.fetchone()
            if not row: return api_error('未找到持仓')
            # 计算锁仓截止日（days个交易日≈days*1.5自然日）
            from datetime import timedelta
            lock_until = (datetime.now() + timedelta(days=int(days*1.5))).strftime('%Y-%m-%d')
            cur.execute("""
                UPDATE portfolio_holdings SET lock_until=%s, lock_active=1, updated_at=NOW()
                WHERE user_id=%s AND ts_code=%s AND status='HOLDING'
                ORDER BY trade_date DESC LIMIT 1
            """, (lock_until, user_id, ts_code))
            logger.info(f"portfolio_lock: {ts_code} lock_until={lock_until}")
        return api_success({'ts_code': ts_code, 'lock_until': lock_until})
    except Exception as e:
        logger.error(f"portfolio_lock error: {e}")
        return api_error(str(e))

# ─── POST /api/v1/management/portfolio/unlock ──────────────
@app.route('/api/v1/management/portfolio/unlock', methods=['POST'])
def portfolio_unlock():
    """手动解锁"""
    try:
        data = request.get_json(force=True) or {}
        ts_code = data.get('ts_code', '')
        reason = data.get('reason', '')
        user_id = data.get('user_id', _get_user_id())
        if not ts_code or not reason: return api_error('参数不足（解锁需填写原因）')
        with db_cursor() as cur:
            cur.execute("""
                UPDATE portfolio_holdings SET lock_until=NULL, lock_active=0, updated_at=NOW()
                WHERE user_id=%s AND ts_code=%s AND status='HOLDING'
                ORDER BY trade_date DESC LIMIT 1
            """, (user_id, ts_code))
            logger.info(f"portfolio_unlock: {ts_code} reason={reason}")
        return api_success({'ts_code': ts_code, 'unlocked': True})
    except Exception as e:
        logger.error(f"portfolio_unlock error: {e}")
        return api_error(str(e))

# ─── GET /api/v1/management/portfolio/locked ───────────────
@app.route('/api/v1/management/portfolio/locked', methods=['GET'])
def portfolio_locked_list():
    """锁仓列表"""
    try:
        uid5 = _get_user_id()
        with db_cursor(commit=False) as cur:
            cur.execute("""
                SELECT ts_code, name, trade_date, qty, cost_price, current_price,
                       market_value, profit_amount, profit_pct, lock_until
                FROM portfolio_holdings 
                WHERE user_id=%s AND status='HOLDING' AND lock_until IS NOT NULL
                ORDER BY lock_until ASC
            """, (uid5,))
            rows = cur.fetchall()
            locked = []
            from datetime import date
            for r in rows:
                lu = r['lock_until']
                if lu:
                    remain = (lu - date.today()).days
                    if remain < 0: remain = 0
                else:
                    remain = 0
                locked.append({
                    'ts_code': r['ts_code'],
                    'name': r['name'],
                    'qty': int(r['qty'] or 0),
                    'trade_date': str(r['trade_date']),
                    'lock_until': str(lu) if lu else None,
                    'lock_remaining_days': remain,
                    'current_price': float(r['current_price'] or 0),
                    'cost_price': float(r['cost_price'] or 0),
                    'market_value': float(r['market_value'] or 0),
                    'profit_amount': float(r['profit_amount'] or 0),
                    'profit_pct': float(r['profit_pct'] or 0),
                })
        return api_success({'locked': locked, 'total': len(locked)})
    except Exception as e:
        logger.error(f"portfolio_locked_list error: {e}")
        return api_error(str(e))

# ─── GET /api/v1/management/system/cron-status ─────────────
@app.route('/api/v1/management/system/cron-status', methods=['GET'])
def cron_status():
    """Cron定时任务执行状态监控"""
    now = datetime.now()
    results = []

    def _file_mtime(path):
        try:
            ts = os.path.getmtime(path)
            return datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S'), ts
        except:
            return None, 0

    # 1. daily_pipeline 数据管道
    pl_path = '/tmp/daily_pipeline_cron_out.log'
    pl_mtime_str, pl_mtime_ts = _file_mtime(pl_path)
    hours_ago = (now.timestamp() - pl_mtime_ts) / 3600 if pl_mtime_ts > 0 else 999
    if not pl_mtime_str:
        pl_status = 'not_run'
        pl_label = '⏳ 未运行'
    elif hours_ago < 24:
        pl_status = 'ok'
        pl_label = '✅ 正常'
    elif hours_ago < 48:
        pl_status = 'warn'
        pl_label = '⚠️ 延迟'
    else:
        pl_status = 'error'
        pl_label = '❌ 异常'
    results.append({
        'id': 'daily_pipeline',
        'name': '📊 数据管道',
        'description': '行情获取→缠论→评分→监控池',
        'last_run': pl_mtime_str or '从未执行',
        'hours_ago': round(hours_ago, 1),
        'status': pl_status,
        'status_label': pl_label,
        'log_file': pl_path,
    })

    # 2. strategy_eval 策略评估
    se_path = '/tmp/strategy_daily.log'
    se_mtime_str, se_mtime_ts = _file_mtime(se_path)
    hours_ago = (now.timestamp() - se_mtime_ts) / 3600 if se_mtime_ts > 0 else 999
    if not se_mtime_str:
        se_status = 'not_run'
        se_label = '⏳ 未运行'
    elif hours_ago < 24:
        se_status = 'ok'
        se_label = '✅ 正常'
    elif hours_ago < 48:
        se_status = 'warn'
        se_label = '⚠️ 延迟'
    else:
        se_status = 'error'
        se_label = '❌ 异常'
    results.append({
        'id': 'strategy_eval',
        'name': '📈 策略评估',
        'description': '阶梯动态持有策略每日评估',
        'last_run': se_mtime_str or '从未执行',
        'hours_ago': round(hours_ago, 1),
        'status': se_status,
        'status_label': se_label,
        'log_file': se_path,
    })

    # 3. 各stock-manager服务状态
    services = [
        ('stock-manager-8887', '管理API (8887)'),
        ('stock-manager-8888', '趋势评分API (8888)'),
        ('stock-manager-8889', '信号API (8889)'),
    ]
    for svc_name, svc_label in services:
        try:
            # 先检查systemd服务
            r = subprocess.run(['systemctl', 'is-active', svc_name], capture_output=True, text=True, timeout=5)
            active_systemd = r.stdout.strip() == 'active'
            # 再检查进程——port 8887用manager_server.py, 8888用signal, 8889用signal
            port = svc_name.split('-')[-1]
            if port == '8887':
                proc_check = subprocess.run(['pgrep', '-f', 'manager_server.py'], capture_output=True, text=True, timeout=3)
            else:
                proc_check = subprocess.run(['pgrep', '-f', 'signal_server'], capture_output=True, text=True, timeout=3)
            active_proc = proc_check.returncode == 0
            
            active = active_systemd or active_proc
            results.append({
                'id': f'service_{svc_name}',
                'name': f'🔧 {svc_label}',
                'description': f'服务状态' + ('(系统服务)' if active_systemd else '(进程运行中)'),
                'last_run': None,
                'hours_ago': None,
                'status': 'ok' if active else 'error',
                'status_label': '✅ 运行中' if active else '❌ 已停止',
                'service_name': svc_name,
            })
        except Exception as e:
            results.append({
                'id': f'service_{svc_name}',
                'name': f'🔧 {svc_label}',
                'description': f'服务状态',
                'last_run': None,
                'hours_ago': None,
                'status': 'error',
                'status_label': f'❌ 检查失败: {e}',
                'service_name': svc_name,
            })

    # 4. 磁盘使用率
    try:
        r = subprocess.run(['df', '-h', '/'], capture_output=True, text=True, timeout=5)
        lines = r.stdout.strip().split('\n')
        df_info = {}
        if len(lines) >= 2:
            parts = lines[1].split()
            df_info = {
                'filesystem': parts[0],
                'size': parts[1],
                'used': parts[2],
                'avail': parts[3],
                'use_pct': parts[4],
                'mounted': parts[5],
            }
        use_val = int(df_info.get('use_pct', '0%').replace('%', ''))
        results.append({
            'id': 'disk_usage',
            'name': '💾 磁盘使用率',
            'description': '系统盘(/dev/vda2)',
            'last_run': None,
            'hours_ago': None,
            'status': 'ok' if use_val < 80 else ('warn' if use_val < 90 else 'error'),
            'status_label': f"{'✅' if use_val < 80 else '⚠️' if use_val < 90 else '❌'} {use_val}%",
            'disk_info': df_info,
        })
    except Exception as e:
        results.append({
            'id': 'disk_usage',
            'name': '💾 磁盘使用率',
            'description': '系统盘',
            'last_run': None,
            'hours_ago': None,
            'status': 'error',
            'status_label': f'❌ 检查失败',
            'disk_info': {},
        })

    return api_success({
        'server_time': now.strftime('%Y-%m-%d %H:%M:%S'),
        'items': results,
        'total': len(results),
        'ok_count': sum(1 for r in results if r['status'] == 'ok'),
        'warn_count': sum(1 for r in results if r['status'] == 'warn'),
        'error_count': sum(1 for r in results if r['status'] == 'error'),
        'not_run_count': sum(1 for r in results if r['status'] == 'not_run'),
    })


# ─── 启动 ───────────────────────────────────────────────────
import pymysql as _pymysql2
from db_config import api_success, api_error, api_not_found, serialize_rows, DATA_ERROR_MARKER as _DEM

# 阶梯动态持有策略 API
# ═════════════════════════════════════════════════

def _run_strategy_eval(trade_date_str=None):
    """调用策略引擎"""
    import sys as _sys
    _sys.path.insert(0, '/opt/stock-analyzer')
    try:
        from step_strategy_engine import run_daily
        run_daily(trade_date_str)
        return True, 'OK'
    except Exception as e:
        return False, str(e)

@app.route('/api/v1/management/strategy/config', methods=['GET'])
def strategy_config():
    """获取策略配置"""
    from db_config import get_connection as _getgc
    try:
        _c = _getgc()
        _cc = _c.cursor()
        _cc.execute("SELECT * FROM strategy_config WHERE is_active=1 ORDER BY id")
        cfgs = _cc.fetchall()
        _cc.close(); _c.close()
        return api_success([{
            'id': r['id'], 'name': r['name'], 'description': r['description'],
            'strategy_type': r['strategy_type'],
            'params': {
                'buy_min_score': r['buy_min_score'],
                'p1_score': r['p1_score'], 'p2_score': r['p2_score'], 'p3_score': r['p3_score'],
                'stop_loss_pct': float(r['stop_loss_pct']),
                'max_hold_days': r['max_hold_days'], 'cool_days': r['cool_days'],
            }
        } for r in cfgs])
    except Exception as e:
        return api_error(str(e))

@app.route('/api/v1/management/strategy/signals', methods=['GET'])
def strategy_signals():
    """获取当日策略信号（支持trade_date参数，自动回退到最新有数据的交易日）"""
    _td = request.args.get('trade_date', str(date.today()))
    # 如果指定的日期没有数据，自动回退到最新交易日
    from db_config import get_connection as _gc2
    _c2 = _gc2(); _cc2 = _c2.cursor()
    _cc2.execute("SELECT MAX(trade_date) as latest FROM strategy_signal_daily")
    _max_row = _cc2.fetchone()
    _max_td = str(_max_row['latest']) if _max_row and _max_row.get('latest') else None
    _cc2.close(); _c2.close()
    if _max_td and _td > str(_max_td):
        _td = str(_max_td)
    _sid = int(request.args.get('strategy_id', 1))
    
    from db_config import get_connection as _getgc
    try:
        _c = _getgc()
        _cc = _c.cursor()
        _cc.execute("""
            SELECT ssd.*, sb.name as stock_name,
                   COALESCE(p6.calibrated_score, ssd.buy_score, 0) as p6_buy_score,
                   p6.track as p6_track
            FROM strategy_signal_daily ssd
            LEFT JOIN stock_basic sb ON ssd.ts_code = sb.ts_code
            LEFT JOIN strategy_signal p6 ON ssd.ts_code = p6.ts_code AND p6.direction='dual_track_v1' AND p6.trade_date=%s
            WHERE ssd.trade_date=%s AND ssd.strategy_id=%s
            ORDER BY 
              CASE ssd.action 
                WHEN 'STOP_LOSS' THEN 0 WHEN 'SELL' THEN 1
                WHEN 'BUY' THEN 2 WHEN 'HOLD' THEN 3 ELSE 4
              END,
              COALESCE(ssd.buy_score, 0) DESC
        """, (_td, _td, _sid))
        rows = _cc.fetchall()
        
        # 统计
        acts = {}
        holdings = 0
        for r in rows:
            a = r['action']
            acts[a] = acts.get(a, 0) + 1
            if r['holding_status'] == 'HOLDING':
                holdings += 1
        
        signals = []
        for r in rows:
            signals.append({
                'ts_code': r['ts_code'],
                'stock_name': r['stock_name'] or r['ts_code'],
                'holding_status': r['holding_status'],
                'hold_days': r['hold_days'],
                'current_checkpoint': r['current_checkpoint'],
                'days_to_check': r['days_to_check'],
                'buy_score': float(r['p6_buy_score']) if r.get('p6_buy_score') else (float(r['buy_score']) if r['buy_score'] else 0),
                'p6_score': float(r['p6_buy_score']) if r.get('p6_buy_score') else 0,
                'p6_track': r.get('p6_track') or '',
                'current_price': float(r['current_price_r']) if r['current_price_r'] else 0,
                'cost_price': float(r['cost_price']) if r['cost_price'] else 0,
                'profit_pct': float(r['profit_pct']) if r['profit_pct'] else 0,
                'drawdown_pct': float(r['drawdown_pct']) if r['drawdown_pct'] else 0,
                'checkpoint_passed': bool(r['checkpoint_passed']) if r['checkpoint_passed'] is not None else None,
                'hit_stop_loss': bool(r['hit_stop_loss']),
                'reduce_flag': bool(r['reduce_flag']),
                'price_source': str(r['price_source'] or 'daily'),
                "stop_loss_pct": float(r["stop_loss_pct"]) if r.get("stop_loss_pct") else 0,
                'action': r['action'],
                'action_reason': r['action_reason'],
                'buy_date': str(r['buy_date']) if r['buy_date'] else None,
                'buy_price': float(r['buy_price']) if r['buy_price'] else None,
            })
        
        _cc.close(); _c.close()
        
        return api_success({
            'trade_date': _td,
            'strategy_id': _sid,
            'total_holdings': holdings,
            'action_summary': acts,
            'signals': signals,
        })
    except Exception as e:
        return api_error(str(e))

@app.route('/api/v1/management/strategy/run', methods=['POST'])
def strategy_run():
    """手动触发策略评估（自动同步持仓建仓时间）"""
    _td = request.args.get('trade_date')
    
    # 先同步所有持仓的 buy_date（从 trade_date 补充，确保建仓时间最新）
    try:
        with db_cursor() as _sync_cur:
            _sync_cur.execute("UPDATE portfolio_holdings SET buy_date = trade_date WHERE status='HOLDING' AND buy_date IS NULL")
    except:
        pass
    
    ok, msg = _run_strategy_eval(_td)
    if ok:
        return api_success({'message': '策略评估完成', 'trade_date': _td or str(date.today())})
    else:
        return api_error(f'策略评估失败: {msg}')


# ═══ AI个股分析 API ════════════════════════════════════════
@app.route('/api/v1/management/ai/analyze', methods=['POST'])
def ai_analyze():
    """AI个股分析：采集数据→DeepSeek→保存至stock_notes"""
    try:
        import sys as _ai_sys
        _ai_sys.path.insert(0, '/opt/stock-analyzer')
        from ai_analysis_engine import analyze_stock
        
        body = request.get_json()
        if not body or not body.get('ts_code'):
            return api_error('缺少ts_code参数')
        
        ts_code = body['ts_code'].strip()
        result = analyze_stock(ts_code)
        
        if result.get('code') == 0:
            return api_success(result['data'])
        else:
            return api_error(result.get('error', '分析失败'))
    except Exception as e:
        logger.error(f'ai_analyze error: {e}')
        return api_error(str(e))


@app.route('/api/v1/management/stock-notes', methods=['GET'])
def get_stock_notes():
    """查询股票历史AI分析备注（支持按代码/名称/日期）"""
    try:
        import sys as _sn_sys
        _sn_sys.path.insert(0, '/opt/stock-analyzer')
        
        ts_code = request.args.get('ts_code', '')
        name = request.args.get('name', '')
        date_from = request.args.get('date_from', '')
        date_to = request.args.get('date_to', '')
        limit = int(request.args.get('limit', 50))
        page = int(request.args.get('page', 1))
        
        from db_config import get_connection as _getgc
        _conn = _getgc()
        _cur = _conn.cursor()
        
        wheres = []
        params = []
        if ts_code:
            wheres.append('ts_code LIKE %s')
            params.append(f'%{ts_code}%')
        if name:
            wheres.append('name LIKE %s')
            params.append(f'%{name}%')
        if date_from:
            wheres.append('note_date >= %s')
            params.append(date_from + ' 00:00:00')
        if date_to:
            wheres.append('note_date <= %s')
            params.append(date_to + ' 23:59:59')
        
        where_sql = ' AND '.join(wheres) if wheres else '1=1'
        
        # 总数
        _cur.execute(f"SELECT COUNT(*) as cnt FROM stock_notes WHERE {where_sql}", params)
        total = _cur.fetchone()['cnt']
        
        # 分页
        offset = (page - 1) * limit
        _cur.execute(f"""
            SELECT ts_code, name, note_date, report_type, 
                   full_report, summary
            FROM stock_notes WHERE {where_sql}
            ORDER BY note_date DESC
            LIMIT %s OFFSET %s
        """, params + [limit, offset])
        rows = _cur.fetchall()
        _cur.close(); _conn.close()
        
        return api_success({
            'notes': rows,
            'total': total,
            'page': page,
            'limit': limit,
        })
    except Exception as e:
        logger.error(f'stock_notes error: {e}')
        return api_error(str(e))


@app.route('/api/v1/management/strategy/holdings-actions', methods=['GET'])
def strategy_holdings_actions():
    """获取持仓买卖建议（前端主页面调用，自动同步建仓时间）"""
    from db_config import get_connection as _getgc, db_cursor as _dbc
    try:
        # 先同步持仓建仓时间
        with _dbc() as _sync_cur:
            _sync_cur.execute("UPDATE portfolio_holdings SET buy_date = trade_date WHERE status='HOLDING' AND buy_date IS NULL")
        
        _c = _getgc()
        _cc = _c.cursor()
        _cc.execute("""
            SELECT MAX(trade_date) as latest FROM strategy_signal_daily WHERE strategy_id=1
        """)
        _lr = _cc.fetchone()
        _ld = str(_lr['latest']) if _lr and _lr['latest'] else str(date.today())
        
        _cc.execute("""
            SELECT ssd.*, sb.name as stock_name,
                   ph.current_price as holding_price, ph.qty, ph.profit_pct as holding_profit,
                   p6.calibrated_score as p6_score, p6.track as p6_track
            FROM strategy_signal_daily ssd
            LEFT JOIN stock_basic sb ON ssd.ts_code = sb.ts_code
            LEFT JOIN portfolio_holdings ph ON ssd.ts_code = ph.ts_code AND ph.status='HOLDING'
            LEFT JOIN strategy_signal p6 ON ssd.ts_code = p6.ts_code AND p6.direction='dual_track_v1' AND p6.trade_date=%s
            WHERE ssd.strategy_id=1 AND ssd.trade_date=%s
            ORDER BY 
              CASE ssd.holding_status WHEN 'HOLDING' THEN 0 ELSE 1 END,
              CASE ssd.action 
                WHEN 'STOP_LOSS' THEN 0 WHEN 'SELL' THEN 1
                WHEN 'HOLD' THEN 2 ELSE 3
              END,
              COALESCE(p6.calibrated_score, ssd.buy_score, 0) DESC
        """, (_ld, _ld))
        rows = _cc.fetchall()
        
        signals = []
        for r in rows:
            signals.append({
                'ts_code': r['ts_code'],
                'stock_name': r['stock_name'] or r['ts_code'],
                'holding_status': r['holding_status'],
                'hold_days': r['hold_days'],
                'current_checkpoint': r['current_checkpoint'],
                'days_to_check': r['days_to_check'],
                "buy_score": float(r["p6_score"]) if r.get("p6_score") else (float(r["buy_score"]) if r["buy_score"] else 0),
                'cost_price': float(r['cost_price']) if r['cost_price'] else 0,
                "p6_score": float(r["p6_score"]) if r.get("p6_score") else 0,
                "p6_track": r.get("p6_track") or "",
                'current_price': float(r['current_price_r']) if r['current_price_r'] else 0,
                'profit_pct': float(r['profit_pct']) if r['profit_pct'] else 0,
                'drawdown_pct': float(r['drawdown_pct']) if r['drawdown_pct'] else 0,
                'peak_price': float(r['peak_price']) if r['peak_price'] else 0,
                'hit_stop_loss': bool(r['hit_stop_loss']),
                'reduce_flag': bool(r['reduce_flag']),
                'price_source': str(r['price_source'] or 'daily'),
                'action': r['action'],
                'action_reason': r['action_reason'],
                'stop_loss_pct': float(r['stop_loss_pct']) if r.get('stop_loss_pct') else 0,
                'stock_name': r.get('stock_name') or r['ts_code'],
                'qty': int(r['qty']) if r['qty'] else 0,
                'buy_date': str(r['buy_date']) if r['buy_date'] else None,
            })
        
        _cc.close(); _c.close()
        
        return api_success({
            'trade_date': _ld,
            'total_holdings': sum(1 for s in signals if s['holding_status'] == 'HOLDING'),
            'signals': signals,
        })
    except Exception as e:
        return api_error(str(e))


# ─── POST /api/v1/management/portfolio/import-screenshot ────
@app.route('/api/v1/management/portfolio/import-screenshot', methods=['POST'])
def portfolio_import_screenshot():
    """
    上传持仓截图，OCR识别后返回解析结果。
    请求：multipart/form-data，file=截图文件(PNG/JPG)
    响应：{holdings: [{name, qty, current_price, cost_price, profit_amount, profit_pct}], raw_text: ...}
    确认写入：POST /api/v1/management/portfolio/import-screenshot/confirm
    """
    import uuid
    try:
        if 'file' not in request.files:
            return api_error("请上传截图文件")
        
        f = request.files['file']
        if f.filename == '':
            return api_error("文件名为空")
        
        # 保存到临时文件
        tmp_dir = '/tmp/stock_screenshots'
        os.makedirs(tmp_dir, exist_ok=True)
        ext = os.path.splitext(f.filename)[1] or '.png'
        tmp_path = os.path.join(tmp_dir, f"screenshot_{uuid.uuid4().hex}{ext}")
        f.save(tmp_path)
        
        logger.info(f"截图保存到: {tmp_path}")
        
        # 调用OCR解析器
        ocr_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'screenshot_ocr_parser.py')
        ocr_python = '/root/.openclaw/workspace/skills/ocr-python/venv/bin/python3'
        
        # 尝试使用PaddleOCR（如果环境可用），否则用系统python+tesseract
        python_bin = sys.executable
        
        result = subprocess.run(
            [python_bin, ocr_script, tmp_path],
            capture_output=True, text=True, timeout=120
        )
        
        if result.returncode != 0:
            logger.error(f"OCR解析失败: {result.stderr}")
            return api_error(f"OCR解析失败: {result.stderr[:200]}")
        
        parsed = json.loads(result.stdout)
        holdings = parsed.get('holdings', [])
        
        # 清理临时文件
        try:
            os.remove(tmp_path)
        except:
            pass
        
        return api_success({
            'holdings': holdings,
            'count': len(holdings),
            'trade_date': datetime.now().strftime('%Y-%m-%d'),
        })
    except subprocess.TimeoutExpired:
        logger.error("OCR解析超时")
        return api_error("OCR解析超时，请重试")
    except Exception as e:
        logger.error(f"import_screenshot error: {e}")
        return api_error(str(e))


# ─── POST /api/v1/management/portfolio/import-screenshot/confirm ─
@app.route('/api/v1/management/portfolio/import-screenshot/confirm', methods=['POST'])
def portfolio_import_screenshot_confirm():
    """
    确认导入截图解析结果到portfolio_holdings表
    请求体: {holdings: [{name, qty, current_price, cost_price}], trade_date: "2026-05-29"}
    """
    try:
        data = request.get_json() or {}
        holdings = data.get('holdings', [])
        trade_date = data.get('trade_date', datetime.now().strftime('%Y-%m-%d'))
        user_id = _get_user_id()
        
        if not holdings:
            return api_error("持仓数据为空")
        
        with db_cursor(commit=True) as cur:
            inserted = 0
            updated = 0
            
            for h in holdings:
                name = h.get('name', '')
                qty = int(h.get('qty', 0))
                avail_qty = int(h.get('avail_qty', qty))
                current_price = float(h.get('current_price', 0))
                cost_price = float(h.get('cost_price', 0))
                profit_amount = round((current_price - cost_price) * qty, 2) if qty > 0 else 0
                profit_pct = round((current_price - cost_price) / cost_price * 100, 2) if cost_price > 0 and qty > 0 else 0
                market_value = round(qty * current_price, 2)
                status = 'HOLDING' if qty > 0 else 'SOLD'
                
                # 使用upsert：trade_date+ts_code确定唯一性
                # 这里ts_code未知，用name做临时标识
                # 先检查是否已有该名称的持仓（仅OCR来源的才能被自动更新，MANUAL不受影响）
                cur.execute(
                    "SELECT ts_code, name, source FROM portfolio_holdings WHERE name=%s AND status='HOLDING' ORDER BY trade_date DESC LIMIT 1",
                    (name,)
                )
                existing = cur.fetchone()
                
                ts_code = existing['ts_code'] if existing else f"OCR_{name}"
                ocr_source = existing['source'] if existing else 'OCR'
                
                if existing:
                    # 仅当原记录为OCR来源时才自动更新；MANUAL记录不动
                    if ocr_source == 'OCR':
                        cur.execute(
                            """UPDATE portfolio_holdings 
                               SET qty=%s, avail_qty=%s, current_price=%s, cost_price=%s,
                                   market_value=%s, profit_amount=%s, profit_pct=%s, trade_date=%s,
                                   status=%s, user_id=%s, updated_at=NOW()
                               WHERE ts_code=%s AND status='HOLDING' AND source='OCR'
                               ORDER BY trade_date DESC LIMIT 1""",
                            (qty, avail_qty, current_price, cost_price,
                             market_value, profit_amount, profit_pct, trade_date,
                             status, user_id, ts_code)
                        )
                        updated += 1
                    else:
                        # MANUAL记录仅更新价格数据，不覆盖其他字段
                        cur.execute(
                            """UPDATE portfolio_holdings 
                               SET current_price=%s, market_value=%s, profit_amount=%s, profit_pct=%s, updated_at=NOW()
                               WHERE ts_code=%s AND status='HOLDING' AND source='MANUAL'
                               ORDER BY trade_date DESC LIMIT 1""",
                            (current_price, market_value, profit_amount, profit_pct, ts_code)
                        )
                        updated += 1
                else:
                    # 插入新持仓
                    cur.execute(
                        """INSERT INTO portfolio_holdings 
                           (user_id, ts_code, name, trade_date, qty, avail_qty,
                            current_price, cost_price, market_value,
                            profit_amount, profit_pct, status, source, created_at)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'OCR', NOW())""",
                        (user_id, ts_code, name, trade_date, qty, avail_qty,
                         current_price, cost_price, market_value,
                         profit_amount, profit_pct, status)
                    )
                    inserted += 1
            
            # 更新账户资产（如果提供了总资产信息）
            total_assets = data.get('total_assets')
            if total_assets:
                total_mv = sum(float(h.get('qty', 0)) * float(h.get('current_price', 0)) for h in holdings)
                available_cash = float(total_assets) - total_mv
                cur.execute(
                    """INSERT INTO portfolio_account 
                       (trade_date, total_assets, total_market_value, available_cash)
                       VALUES (%s, %s, %s, %s)
                       ON DUPLICATE KEY UPDATE
                       total_assets=VALUES(total_assets),
                       total_market_value=VALUES(total_market_value),
                       available_cash=VALUES(available_cash)""",
                    (trade_date, total_assets, round(total_mv, 2), round(available_cash, 2))
                )
        
        return api_success({
            'inserted': inserted,
            'updated': updated,
            'total': len(holdings),
            'trade_date': trade_date,
        })
    except Exception as e:
        logger.error(f"import_screenshot_confirm error: {e}")
        return api_error(str(e))


# ─── POST /api/v1/management/backtest/run ────────────────────
@app.route('/api/v1/management/backtest/run', methods=['POST'])
def backtest_run():
    """策略回测执行：调用/tmp/step_backtest_json.py进行多策略对比回测"""
    import json as _json, subprocess, os, sys
    try:
        data = request.get_json() or {}
        
        # 提取参数（含默认值）
        params = {
            'min_buy_score': data.get('min_buy_score', 30),
            'p1': data.get('p1', 10),
            'p2': data.get('p2', 20),
            'p3': data.get('p3', 30),
            'stop_loss_pct': data.get('stop_loss_pct', 10),
            'max_hold_days': data.get('max_hold_days', 60),
            'start_date': data.get('start_date', ''),
            'end_date': data.get('end_date', ''),
        }
        if data.get('force_hold'):
            params['force_hold'] = data['force_hold']
        
        logger.info(f"回测请求参数: {_json.dumps(params, ensure_ascii=False)}")
        
        script_path = '/tmp/step_backtest_json.py'
        if not os.path.exists(script_path):
            script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'step_backtest_json.py')
        
        r = subprocess.run(
            [sys.executable, script_path, _json.dumps(params)],
            capture_output=True, text=True, timeout=300, cwd='/tmp'
        )
        
        if r.returncode != 0:
            logger.error(f"回测脚本错误(stderr): {r.stderr[-500:]}")
            return api_error(f"回测执行失败: {r.stderr[-200:]}", code=500)
        
        try:
            result = _json.loads(r.stdout.strip())
        except _json.JSONDecodeError as e:
            logger.error(f"回测结果解析失败: {e}, stdout={r.stdout[:500]}")
            return api_error(f"回测结果解析失败: {e}", code=500)
        
        if not result.get('success'):
            return api_error(result.get('error', '回测执行返回错误'), code=500)
        
        logger.info(f"回测完成: {result.get('total_strategies', 0)}个策略")
        return api_success(result)
    except subprocess.TimeoutExpired:
        logger.error("回测超时(300s)")
        return api_error('回测超时，请减小时间范围', code=504)
    except Exception as e:
        logger.error(f"backtest_run error: {e}")
        return api_error(str(e), code=500)


# ════════════════════════════════════════════════════
# API Key 管理（openclaw_config.api_credentials）
# ════════════════════════════════════════════════════

_CONFIG_DB_CFG = {
    'host': '127.0.0.1',
    'port': 3306,
    'user': 'debian-sys-maint',
    'database': 'openclaw_config',
    'charset': 'utf8mb4',
    'autocommit': True,
}


def _get_config_conn():
    import pymysql as _pm
    cfg = _CONFIG_DB_CFG.copy()
    cfg['password'] = _get_mysql_pass()
    cfg['cursorclass'] = _pm.cursors.DictCursor
    return _pm.connect(**cfg)


def _config_cursor(commit=True):
    conn = _get_config_conn()
    cursor = conn.cursor()
    return conn, cursor


@app.route('/api/v1/management/system/api-keys', methods=['GET'])
def list_api_keys():
    """获取所有 API Key 列表（key 值默认掩码）"""
    try:
        conn, cur = _config_cursor()
        try:
            cur.execute(
                "SELECT id, name, provider, api_key, description, is_active, created_at, updated_at "
                "FROM api_credentials ORDER BY id ASC"
            )
            rows = cur.fetchall()
            keys = []
            for r in rows:
                ak = r['api_key'] or ''
                masked = ak[:6] + '****' + ak[-4:] if len(ak) > 12 else '****'
                keys.append({
                    'id': r['id'],
                    'name': r['name'],
                    'provider': r['provider'],
                    'api_key': masked,
                    'api_key_masked': True,
                    'description': r.get('description', ''),
                    'is_active': bool(r['is_active']),
                    'created_at': r['created_at'].isoformat() if r.get('created_at') else None,
                    'updated_at': r['updated_at'].isoformat() if r.get('updated_at') else None,
                })
            return api_success(keys)
        finally:
            cur.close()
            conn.close()
    except Exception as e:
        logger.error(f"list_api_keys error: {e}")
        return api_error(str(e), code=500)


@app.route('/api/v1/management/system/api-keys/<int:key_id>', methods=['GET'])
def get_api_key(key_id):
    """获取单个 API Key（含明文）"""
    try:
        conn, cur = _config_cursor()
        try:
            cur.execute(
                "SELECT id, name, provider, api_key, description, is_active, created_at, updated_at "
                "FROM api_credentials WHERE id=%s", (key_id,)
            )
            r = cur.fetchone()
            if not r:
                return api_error('API Key not found', code=404)
            return api_success({
                'id': r['id'],
                'name': r['name'],
                'provider': r['provider'],
                'api_key': r['api_key'],
                'description': r.get('description', ''),
                'is_active': bool(r['is_active']),
                'created_at': r['created_at'].isoformat() if r.get('created_at') else None,
                'updated_at': r['updated_at'].isoformat() if r.get('updated_at') else None,
            })
        finally:
            cur.close()
            conn.close()
    except Exception as e:
        logger.error(f"get_api_key error: {e}")
        return api_error(str(e), code=500)


@app.route('/api/v1/management/system/api-keys/update', methods=['POST'])
def update_api_key():
    """修改 API Key 值"""
    try:
        data = request.get_json(force=True) or {}
        key_id = data.get('id')
        new_key = data.get('api_key')
        if not key_id or not new_key:
            return api_error('id and api_key are required', code=400)
        conn, cur = _config_cursor()
        try:
            cur.execute("SELECT id FROM api_credentials WHERE id=%s", (key_id,))
            if not cur.fetchone():
                return api_error('API Key not found', code=404)
            cur.execute(
                "UPDATE api_credentials SET api_key=%s, updated_at=NOW() WHERE id=%s",
                (new_key, key_id),
            )
            conn.commit()
            return api_success({'id': key_id}, message='API Key updated successfully')
        finally:
            cur.close()
            conn.close()
    except Exception as e:
        logger.error(f"update_api_key error: {e}")
        return api_error(str(e), code=500)


@app.route('/api/v1/management/system/api-keys/toggle', methods=['POST'])
def toggle_api_key():
    """启用/禁用 API Key"""
    try:
        data = request.get_json(force=True) or {}
        key_id = data.get('id')
        if not key_id:
            return api_error('id is required', code=400)
        conn, cur = _config_cursor()
        try:
            cur.execute("SELECT id, is_active FROM api_credentials WHERE id=%s", (key_id,))
            r = cur.fetchone()
            if not r:
                return api_error('API Key not found', code=404)
            new_status = 0 if r['is_active'] else 1
            cur.execute(
                "UPDATE api_credentials SET is_active=%s, updated_at=NOW() WHERE id=%s",
                (new_status, key_id),
            )
            conn.commit()
            return api_success({'id': key_id, 'is_active': bool(new_status)})
        finally:
            cur.close()
            conn.close()
    except Exception as e:
        logger.error(f"toggle_api_key error: {e}")
        return api_error(str(e), code=500)


# ═══ 数据库密码获取 ═══
def _get_mysql_pass():
    """获取数据库密码（统一入口：优先环境变量，fallback debian.cnf）"""
    from db_config import _get_password as _db_pwd
    pwd = os.environ.get('MYSQL_PASS')
    if pwd:
        return pwd
    return _db_pwd()

# ─── POST /api/v1/management/portfolio/holding/add ──────────
@app.route('/api/v1/management/portfolio/holding/add', methods=['POST'])
def portfolio_holding_add():
    """新增持仓记录"""
    try:
        data = request.get_json()
        ts_code = data.get('ts_code', '')
        name = data.get('name', '')
        qty = int(data.get('qty', 0))
        cost_price = float(data.get('cost_price', 0))
        current_price = float(data.get('current_price', 0))
        trade_date = data.get('trade_date', datetime.now().strftime('%Y-%m-%d'))
        user_id = data.get('user_id', _get_user_id())
        if not ts_code or qty <= 0 or cost_price <= 0:
            return api_error('参数不足: ts_code/qty/cost_price 必填')
        market_value = qty * current_price
        profit_amount = (current_price - cost_price) * qty
        profit_pct = ((current_price - cost_price) / cost_price) * 100 if cost_price > 0 else 0
        buy_date = data.get('buy_date', trade_date)
        with db_cursor() as cur:
            cur.execute("""
                INSERT INTO portfolio_holdings
                (user_id, ts_code, name, trade_date, qty, avail_qty, current_price, cost_price,
                 market_value, profit_amount, profit_pct, status, source, lock_active, buy_date)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'HOLDING','MANUAL',0,%s)
            """, (user_id, ts_code, name, trade_date, qty, qty, current_price, cost_price,
                   market_value, profit_amount, profit_pct, buy_date))
        return api_success({'ts_code': ts_code, 'name': name, 'message': '新增成功'})
    except Exception as e:
        logger.error(f"holding_add error: {e}")
        return api_error(str(e))


# ─── POST /api/v1/management/portfolio/holding/update ───────
@app.route('/api/v1/management/portfolio/holding/update', methods=['POST'])
def portfolio_holding_update():
    """更新持仓记录"""
    try:
        data = request.get_json()
        holding_id = data.get('id')
        ts_code = data.get('ts_code', '')
        if ts_code and "." not in ts_code:
            ts_code = ts_code + ".SZ" if ts_code[0] in "30" else ts_code + ".SH"
        user_id = data.get('user_id', _get_user_id())
        if not holding_id and not ts_code:
            return api_error('缺少id或ts_code')
        update_fields = []
        update_vals = []
        for field in ['ts_code', 'qty', 'avail_qty', 'cost_price', 'current_price', 'name', 'status',
                       'trade_date', 'buy_date', 'advice', 'advice_reason']:
            if field in data:
                update_fields.append(f"{field}=%s")
                update_vals.append(data[field])
        # 重算金额
        if 'qty' in data or 'current_price' in data or 'cost_price' in data:
            with db_cursor(commit=False) as cur:
                if holding_id:
                    cur.execute("SELECT qty, cost_price, current_price FROM portfolio_holdings WHERE id=%s", (holding_id,))
                else:
                    cur.execute(
                        "SELECT qty, cost_price, current_price FROM portfolio_holdings WHERE user_id=%s AND ts_code=%s AND status='HOLDING' ORDER BY trade_date DESC LIMIT 1",
                        (user_id, ts_code))
                row = cur.fetchone()
                if row:
                    q = data.get('qty', row['qty'] or 0)
                    cp = data.get('cost_price', float(row['cost_price'] or 0))
                    pr = data.get('current_price', float(row['current_price'] or 0))
                    mv = q * pr
                    pa = (pr - cp) * q
                    pp = ((pr - cp) / cp) * 100 if cp > 0 else 0
                    update_fields.append("market_value=%s,profit_amount=%s,profit_pct=%s,updated_at=NOW()")
                    update_vals.extend([mv, pa, pp])
        if not update_fields:
            return api_error('没有需要更新的字段')
        update_sql = ", ".join(update_fields)
        with db_cursor() as cur:
            if holding_id:
                cur.execute(f"UPDATE portfolio_holdings SET {update_sql} WHERE id=%s", update_vals + [holding_id])
            else:
                cur.execute(
                    f"UPDATE portfolio_holdings SET {update_sql} WHERE user_id=%s AND ts_code=%s AND status='HOLDING' ORDER BY trade_date DESC LIMIT 1",
                    update_vals + [user_id, ts_code])
        return api_success({'message': '更新成功'})
    except Exception as e:
        logger.error(f"holding_update error: {e}")
        return api_error(str(e))


# ─── POST /api/v1/management/portfolio/holding/delete ───────
@app.route('/api/v1/management/portfolio/holding/delete', methods=['POST'])
def portfolio_holding_delete():
    """删除持仓记录"""
    try:
        data = request.get_json()
        holding_id = data.get('id')
        ts_code = data.get('ts_code', '')
        user_id = data.get('user_id', _get_user_id())
        if not holding_id and not ts_code:
            return api_error('缺少id或ts_code')
        with db_cursor() as cur:
            if holding_id:
                cur.execute("DELETE FROM portfolio_holdings WHERE id=%s", (holding_id,))
            else:
                cur.execute(
                    "DELETE FROM portfolio_holdings WHERE user_id=%s AND ts_code=%s",
                    (user_id, ts_code))
        return api_success({'message': '删除成功'})
    except Exception as e:
        logger.error(f"holding_delete error: {e}")
        return api_error(str(e))


# ═══════════════════════════════════════════════
# 兮易AI大脑 - 品质专员API
# ═══════════════════════════════════════════════

@app.route('/api/v1/xiyi/kpis', methods=['GET'])
def xiyi_kpis():
    """获取品质专员KPI指标"""
    try:
        role = request.args.get('role', 'quality_specialist')
        with db_cursor(commit=False) as cur:
            cur.execute("SELECT * FROM xiyi_demo.kpis WHERE role_id=%s ORDER BY id", (role,))
            rows = serialize_rows(cur.fetchall())
        return api_success({'kpis': rows})
    except Exception as e:
        logger.error(f"xiyi_kpis error: {e}")
        return api_error(str(e))

@app.route('/api/v1/xiyi/scenarios', methods=['GET'])
def xiyi_scenarios():
    """获取品质专员工作场景"""
    try:
        role = request.args.get('role', 'quality_specialist')
        with db_cursor(commit=False) as cur:
            cur.execute("SELECT * FROM xiyi_demo.scenarios WHERE role_id=%s ORDER BY id", (role,))
            rows = serialize_rows(cur.fetchall())
        return api_success({'scenarios': rows})
    except Exception as e:
        logger.error(f"xiyi_scenarios error: {e}")
        return api_error(str(e))

@app.route('/api/v1/xiyi/alerts', methods=['GET'])
def xiyi_alerts():
    """获取告警动态"""
    try:
        role = request.args.get('role', 'quality_specialist')
        with db_cursor(commit=False) as cur:
            cur.execute("SELECT * FROM xiyi_demo.alerts WHERE role_id=%s ORDER BY alert_time DESC", (role,))
            rows = serialize_rows(cur.fetchall())
        return api_success({'alerts': rows})
    except Exception as e:
        logger.error(f"xiyi_alerts error: {e}")
        return api_error(str(e))


# ─── 启动 ───────────────────────────────────────────────────
if __name__ == "__main__":
    port_8887 = int(os.environ.get('STOCK_PORT_8887', 8887))
    logger.info(f"Starting management API server on port {port_8887}...")
    app.run(host="0.0.0.0", port=port_8887, debug=False)

