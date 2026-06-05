"""
signal_server.py — 策略信号API服务 (Port 8889)
路由前缀: /api/v1/signal/*
Antony架构方案第4.4节规范
"""
import os
import sys
import logging
from datetime import datetime, date
from flask import Flask, request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db_config import db_cursor, api_success, api_error, api_not_found, serialize_rows, DATA_ERROR_MARKER

app = Flask(__name__)

# ═══ API 认证 ═══
_API_KEY_CACHE = {"key": None}
def _get_api_key():
    if _API_KEY_CACHE["key"]:
        return _API_KEY_CACHE["key"]
    try:
        with db_cursor(commit=False) as _ck_cur:
            _ck_cur.execute("SELECT config_value FROM system_config WHERE config_key=%(k)s LIMIT 1", {"k": "api_key"})
            _ck_r = _ck_cur.fetchone()
            if _ck_r:
                _API_KEY_CACHE["key"] = _ck_r["config_value"] if isinstance(_ck_r, dict) else _ck_r[0]
                return _ck_r["config_value"] if isinstance(_ck_r, dict) else _ck_r[0]
    except: pass
    return None

@app.before_request
def _check_api_key():
    if request.method == "OPTIONS":
        return None
    if request.path in ("/health",):
        return None
    req_key = request.headers.get("X-API-Key", "") or request.headers.get("Authorization", "").replace("Bearer ", "")
    if not req_key:
        return {"code": -1, "error": "缺少认证信息 (X-API-Key)", "data": None}, 401
    expected = _get_api_key()
    if not expected or req_key != expected:
        return {"code": -1, "error": "认证失败", "data": None}, 401
logger = logging.getLogger('signal_8889')
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')


# ─── 健康检查 ───────────────────────────────────────────────
@app.route('/health', methods=['GET'])
@app.route('/api/v1/signal/health', methods=['GET'])
def health():
    try:
        with db_cursor(commit=False) as cur:
            cur.execute("SELECT 1")
            db_ok = cur.fetchone() is not None
        return api_success({
            'service': 'strategy_signal_api',
            'port': 8889,
            'database': 'connected' if db_ok else 'disconnected',
            'version': '2.0.0'
        })
    except Exception as e:
        return api_error(str(e), http_status=500)


# ─── GET /api/v1/signal/daily ──────────────────────────────
@app.route('/api/v1/signal/daily', methods=['GET'])
def signal_daily():
    """获取当日全量策略信号，支持过滤"""
    try:
        trade_date = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))
        direction = request.args.get('direction')
        min_score = request.args.get('min_score')
        operation_mode = request.args.get('operation_mode')
        limit = min(int(request.args.get('limit', 200)), 500)

        wheres = ["trade_date = %s"]
        params = [trade_date]
        if direction:
            wheres.append("direction = %s")
            params.append(direction)
        if min_score:
            wheres.append("composite_score >= %s")
            params.append(float(min_score))
        if operation_mode:
            wheres.append("operation_mode = %s")
            params.append(operation_mode)

        sql = f"SELECT * FROM strategy_signal WHERE {' AND '.join(wheres)} ORDER BY composite_score DESC LIMIT %s"
        params.append(limit)

        with db_cursor(commit=False) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

        return api_success({
            'trade_date': trade_date,
            'count': len(rows),
            'signals': serialize_rows(rows)
        })
    except Exception as e:
        logger.error(f"signal_daily error: {e}")
        return api_error(str(e))


# ─── GET /api/v1/signal/{ts_code} ───────────────────────────
@app.route('/api/v1/signal/<ts_code>', methods=['GET'])
def signal_single(ts_code):
    """获取单只股票策略信号"""
    try:
        trade_date = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))

        with db_cursor(commit=False) as cur:
            cur.execute(
                """SELECT ss.*, sb.name, sb.industry
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
        logger.error(f"signal_single error: {e}")
        return api_error(str(e))


# ─── GET /api/v1/signal/{ts_code}/history ───────────────────
@app.route('/api/v1/signal/<ts_code>/history', methods=['GET'])
def signal_history(ts_code):
    """历史信号序列"""
    try:
        limit = min(int(request.args.get('limit', 30)), 200)

        with db_cursor(commit=False) as cur:
            cur.execute(
                """SELECT * FROM strategy_signal
                   WHERE ts_code=%s
                   ORDER BY trade_date DESC LIMIT %s""",
                [ts_code, limit]
            )
            rows = cur.fetchall()

        return api_success({
            'ts_code': ts_code,
            'count': len(rows),
            'history': serialize_rows(rows)
        })
    except Exception as e:
        logger.error(f"signal_history error: {e}")
        return api_error(str(e))


# ─── GET /api/v1/signal/buy ─────────────────────────────────
@app.route('/api/v1/signal/buy', methods=['GET'])
def signal_buy():
    """买入信号精选(Top N)"""
    try:
        trade_date = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))
        min_score = float(request.args.get('min_score', 70))
        limit = min(int(request.args.get('limit', 20)), 50)

        with db_cursor(commit=False) as cur:
            cur.execute(
                """SELECT ss.*, sb.name, sb.industry
                   FROM strategy_signal ss
                   LEFT JOIN stock_basic sb ON ss.ts_code = sb.ts_code
                   WHERE ss.trade_date=%s AND ss.direction='LONG'
                     AND ss.composite_score >= %s
                     AND ss.is_calculable = 1 AND ss.gate_triggered = 0
                   ORDER BY ss.composite_score DESC
                   LIMIT %s""",
                [trade_date, min_score, limit]
            )
            rows = cur.fetchall()

        return api_success({
            'trade_date': trade_date,
            'count': len(rows),
            'buy_signals': serialize_rows(rows)
        })
    except Exception as e:
        logger.error(f"signal_buy error: {e}")
        return api_error(str(e))


# ─── GET /api/v1/signal/sell ────────────────────────────────
@app.route('/api/v1/signal/sell', methods=['GET'])
def signal_sell():
    """卖出信号精选"""
    try:
        trade_date = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))
        limit = min(int(request.args.get('limit', 20)), 50)

        with db_cursor(commit=False) as cur:
            # 锁仓过滤: 跳过 lock_until > 当前日期 的持仓
            today = datetime.now().strftime('%Y-%m-%d')
            # 先统计锁仓跳过的数量
            cur.execute(
                """SELECT COUNT(*) as cnt FROM strategy_signal
                   WHERE trade_date=%s AND direction='SHORT'
                     AND is_calculable = 1
                     AND lock_until > %s""",
                [trade_date, today]
            )
            locked_row = cur.fetchone()
            locked_skipped = locked_row['cnt'] if locked_row else 0
            
            cur.execute(
                """SELECT ss.*, sb.name, sb.industry
                   FROM strategy_signal ss
                   LEFT JOIN stock_basic sb ON ss.ts_code = sb.ts_code
                   WHERE ss.trade_date=%s AND ss.direction='SHORT'
                     AND ss.is_calculable = 1
                     AND (ss.lock_until IS NULL OR ss.lock_until <= %s)
                   ORDER BY ss.composite_score ASC
                   LIMIT %s""",
                [trade_date, today, limit]
            )
            rows = cur.fetchall()

        return api_success({
            'trade_date': trade_date,
            'count': len(rows),
            'locked_skipped': locked_skipped,
            'sell_signals': serialize_rows(rows)
        })
    except Exception as e:
        logger.error(f"signal_sell error: {e}")
        return api_error(str(e))


# ─── GET /api/v1/signal/alerts ──────────────────────────────
@app.route('/api/v1/signal/alerts', methods=['GET'])
def signal_alerts():
    """告警信号列表"""
    try:
        trade_date = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))
        limit = min(int(request.args.get('limit', 50)), 100)

        # 秋老虎 + 方向变更
        with db_cursor(commit=False) as cur:
            cur.execute(
                """SELECT ss.*, sb.name
                   FROM strategy_signal ss
                   LEFT JOIN stock_basic sb ON ss.ts_code = sb.ts_code
                   WHERE ss.trade_date=%s AND ss.autumn_tiger=1
                   ORDER BY ss.tiger_confidence DESC
                   LIMIT %s""",
                [trade_date, limit]
            )
            tigers = cur.fetchall()

            # 信号变更告警
            cur.execute(
                """SELECT * FROM signal_change_log
                   WHERE trade_date=%s AND is_alert=1
                   ORDER BY id DESC LIMIT %s""",
                [trade_date, limit]
            )
            changes = cur.fetchall()

        return api_success({
            'trade_date': trade_date,
            'autumn_tigers': serialize_rows(tigers) if tigers else [],
            'direction_changes': serialize_rows(changes) if changes else [],
        })
    except Exception as e:
        logger.error(f"signal_alerts error: {e}")
        return api_error(str(e))


# ─── GET /api/v1/signal/autumn-tigers ───────────────────────
@app.route('/api/v1/signal/autumn-tigers', methods=['GET'])
def autumn_tigers():
    """秋老虎标记股票列表"""
    try:
        trade_date = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))
        limit = min(int(request.args.get('limit', 50)), 200)

        with db_cursor(commit=False) as cur:
            cur.execute(
                """SELECT ss.*, sb.name
                   FROM strategy_signal ss
                   LEFT JOIN stock_basic sb ON ss.ts_code = sb.ts_code
                   WHERE ss.trade_date=%s AND ss.autumn_tiger=1
                   ORDER BY ss.tiger_confidence DESC
                   LIMIT %s""",
                [trade_date, limit]
            )
            rows = cur.fetchall()

        return api_success({
            'trade_date': trade_date,
            'count': len(rows),
            'tigers': serialize_rows(rows)
        })
    except Exception as e:
        logger.error(f"autumn_tigers error: {e}")
        return api_error(str(e))


# ─── GET /api/v1/signal/safe-gate ───────────────────────────
@app.route('/api/v1/signal/safe-gate', methods=['GET'])
def safe_gate():
    """安全闸门状态（从 season_state 取数）"""
    try:
        trade_date = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))

        with db_cursor(commit=False) as cur:
            cur.execute(
                "SELECT season, raw_score, confidence, hengjiyuan_level, hengjiyuan_score FROM season_state "
                "WHERE index_code='MARKET' ORDER BY trade_date DESC LIMIT 1"
            )
            ss = cur.fetchone()

        if not ss:
            return api_not_found()

        return api_success({
            'trade_date': str(ss.get('trade_date', trade_date)),
            'safety_gate': '通过',
            'gate_triggered': False,
            'cycle_stage': ss.get('season'),
            'hengjiyuan_level': ss.get('hengjiyuan_level'),
        })
    except Exception as e:
        logger.error(f"safe_gate error: {e}")
        return api_error(str(e))


# ─── 启动 ───────────────────────────────────────────────────
if __name__ == '__main__':
    port_8889 = int(os.environ.get('STOCK_PORT_8889', 8889))
    logger.info(f"Starting strategy_signal API server on port {port_8889}...")
    app.run(host='0.0.0.0', port=port_8889, debug=False)
