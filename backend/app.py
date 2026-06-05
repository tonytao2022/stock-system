"""
app.py — 趋势评分API服务 (Port 8888)
路由前缀: /api/v1/trend/*
Antony架构方案第4.3节规范
"""
import os
import sys
import logging
from datetime import datetime, date, timedelta
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
logger = logging.getLogger('app_8888')
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

# ─── 健康检查 ───────────────────────────────────────────────
@app.route('/health', methods=['GET'])
@app.route('/api/v1/trend/health', methods=['GET'])
def health():
    try:
        with db_cursor(commit=False) as cur:
            cur.execute("SELECT 1")
            db_ok = cur.fetchone() is not None
        return api_success({
            'service': 'trend_score_api',
            'port': 8888,
            'database': 'connected' if db_ok else 'disconnected',
            'version': '2.0.0'
        })
    except Exception as e:
        return api_error(str(e), http_status=500)


# ─── GET /api/v1/trend/market-state ─────────────────────────
@app.route('/api/v1/trend/market-state', methods=['GET'])
def market_state():
    """获取全市场状态(恒纪元+周期)，最核心全局接口"""
    try:
        trade_date = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))
        with db_cursor(commit=False) as cur:
            # 恒纪元
            cur.execute(
                """SELECT * FROM hengjiyuan_evaluation
                   WHERE trade_date=%s AND scope='market'
                   ORDER BY id DESC LIMIT 1""",
                [trade_date]
            )
            heng = cur.fetchone()

            # 周期判定
            cur.execute(
                """SELECT * FROM cycle_judgment
                   WHERE trade_date=%s AND scope='market'
                   ORDER BY id DESC LIMIT 1""",
                [trade_date]
            )
            cycle = cur.fetchone()

            # season_state (统一季节来源)
            cur.execute(
                "SELECT season, raw_score, confidence FROM season_state WHERE index_code='MARKET' ORDER BY trade_date DESC LIMIT 1"
            )
            season_row = cur.fetchone()

        return api_success({
            'trade_date': trade_date,
            'hengjiyuan': serialize_rows([heng])[0] if heng else None,
            'cycle': serialize_rows([cycle])[0] if cycle else None,
            'season_state': {
                'season': season_row['season'],
                'raw_score': float(season_row['raw_score'] or 0),
                'confidence': float(season_row['confidence'] or 0.5),
            } if season_row else None,
        })
    except Exception as e:
        logger.error(f"market-state error: {e}")
        return api_error(str(e))


# ─── GET /api/v1/trend/hengjiyuan ───────────────────────────
@app.route('/api/v1/trend/hengjiyuan', methods=['GET'])
def hengjiyuan_detail():
    """恒纪元详细评估(7因子)"""
    try:
        trade_date = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))
        scope = request.args.get('scope', 'market')
        target = request.args.get('target', None)

        with db_cursor(commit=False) as cur:
            if target:
                cur.execute(
                    """SELECT * FROM hengjiyuan_evaluation
                       WHERE trade_date=%s AND scope=%s AND target_code=%s
                       ORDER BY id DESC LIMIT 1""",
                    [trade_date, scope, target]
                )
            else:
                cur.execute(
                    """SELECT * FROM hengjiyuan_evaluation
                       WHERE trade_date=%s AND scope=%s
                       ORDER BY id DESC LIMIT 1""",
                    [trade_date, scope]
                )
            row = cur.fetchone()

        if not row:
            return api_not_found()
        return api_success(serialize_rows([row])[0])
    except Exception as e:
        logger.error(f"hengjiyuan error: {e}")
        return api_error(str(e))


# ─── GET /api/v1/trend/cycle ────────────────────────────────
@app.route('/api/v1/trend/cycle', methods=['GET'])
def cycle_detail():
    """周期阶段判定"""
    try:
        trade_date = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))
        scope = request.args.get('scope', 'market')
        target = request.args.get('target', None)

        with db_cursor(commit=False) as cur:
            if target:
                cur.execute(
                    """SELECT * FROM cycle_judgment
                       WHERE trade_date=%s AND scope=%s AND target_code=%s
                       ORDER BY id DESC LIMIT 1""",
                    [trade_date, scope, target]
                )
            else:
                cur.execute(
                    """SELECT * FROM cycle_judgment
                       WHERE trade_date=%s AND scope=%s
                       ORDER BY id DESC LIMIT 1""",
                    [trade_date, scope]
                )
            row = cur.fetchone()

        if not row:
            return api_not_found()
        return api_success(serialize_rows([row])[0])
    except Exception as e:
        logger.error(f"cycle error: {e}")
        return api_error(str(e))


# ─── GET /api/v1/trend/score/{ts_code} ──────────────────────
@app.route('/api/v1/trend/score/<ts_code>', methods=['GET'])
def trend_score(ts_code):
    """获取单只股票趋势评分"""
    try:
        trade_date = request.args.get('date')
        limit = min(int(request.args.get('limit', 30)), 365)

        with db_cursor(commit=False) as cur:
            if trade_date:
                cur.execute(
                    "SELECT * FROM trend_score WHERE ts_code=%s AND trade_date=%s",
                    [ts_code, trade_date]
                )
                row = cur.fetchone()
                if not row:
                    return api_not_found()
                return api_success(serialize_rows([row])[0])

            cur.execute(
                """SELECT * FROM trend_score
                   WHERE ts_code=%s ORDER BY trade_date DESC LIMIT %s""",
                [ts_code, limit]
            )
            rows = cur.fetchall()

        return api_success({
            'ts_code': ts_code,
            'count': len(rows),
            'scores': serialize_rows(rows)
        })
    except Exception as e:
        logger.error(f"trend_score error: {e}")
        return api_error(str(e))


# ─── GET /api/v1/trend/score/top ────────────────────────────
@app.route('/api/v1/trend/score/top', methods=['GET'])
def trend_score_top():
    """Top N趋势评分排名"""
    try:
        trade_date = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))
        limit = min(int(request.args.get('limit', 20)), 100)

        with db_cursor(commit=False) as cur:
            cur.execute("""
                SELECT ts.*, sb.name, sb.industry
                FROM trend_score ts
                LEFT JOIN stock_basic sb ON ts.ts_code = sb.ts_code
                WHERE ts.trade_date = %s AND ts.is_calculable = 1
                ORDER BY ts.composite_score DESC
                LIMIT %s
            """, [trade_date, limit])
            rows = cur.fetchall()

        return api_success({
            'trade_date': trade_date,
            'count': len(rows),
            'top': serialize_rows(rows)
        })
    except Exception as e:
        logger.error(f"trend_score_top error: {e}")
        return api_error(str(e))


# ─── GET/POST /api/v1/trend/score/batch ─────────────────────
@app.route('/api/v1/trend/score/batch', methods=['GET', 'POST'])
def trend_score_batch():
    """批量获取趋势评分"""
    try:
        if request.method == 'GET':
            codes = request.args.get('codes', '').split(',')
            trade_date = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))
        else:
            data = request.get_json(force=True)
            codes = data.get('codes', [])
            trade_date = data.get('date', datetime.now().strftime('%Y-%m-%d'))

        if not codes or codes == ['']:
            return api_error("codes参数必填", code=1001, http_status=400)

        codes = [c.strip() for c in codes if c.strip()][:50]

        with db_cursor(commit=False) as cur:
            placeholders = ','.join(['%s'] * len(codes))
            cur.execute(
                f"""SELECT * FROM trend_score
                    WHERE ts_code IN ({placeholders}) AND trade_date = %s""",
                codes + [trade_date]
            )
            rows = cur.fetchall()

        return api_success({
            'trade_date': trade_date,
            'count': len(rows),
            'scores': serialize_rows(rows)
        })
    except Exception as e:
        logger.error(f"trend_score_batch error: {e}")
        return api_error(str(e))


# ─── GET /api/v1/trend/chanlun/{ts_code} ────────────────────
@app.route('/api/v1/trend/chanlun/<ts_code>', methods=['GET'])
def chanlun(ts_code):
    """获取缠论结构详情"""
    try:
        level = request.args.get('level', 'daily')
        trade_date = request.args.get('date')
        limit = min(int(request.args.get('limit', 20)), 100)

        with db_cursor(commit=False) as cur:
            if trade_date:
                cur.execute(
                    """SELECT * FROM chanlun_structure
                       WHERE ts_code=%s AND analysis_level=%s AND trade_date=%s
                       ORDER BY trade_date DESC LIMIT 1""",
                    [ts_code, level, trade_date]
                )
                row = cur.fetchone()
                if not row:
                    return api_not_found()
                return api_success(serialize_rows([row])[0])

            cur.execute(
                """SELECT * FROM chanlun_structure
                   WHERE ts_code=%s AND analysis_level=%s
                   ORDER BY trade_date DESC LIMIT %s""",
                [ts_code, level, limit]
            )
            rows = cur.fetchall()

        return api_success({
            'ts_code': ts_code,
            'level': level,
            'count': len(rows),
            'data': serialize_rows(rows)
        })
    except Exception as e:
        logger.error(f"chanlun error: {e}")
        return api_error(str(e))


# ─── GET /api/v1/trend/indicator/{ts_code} ──────────────────
@app.route('/api/v1/trend/indicator/<ts_code>', methods=['GET'])
def indicator(ts_code):
    """获取技术指标"""
    try:
        trade_date = request.args.get('date')
        limit = min(int(request.args.get('limit', 5)), 50)

        with db_cursor(commit=False) as cur:
            if trade_date:
                cur.execute(
                    """SELECT * FROM technical_indicator
                       WHERE ts_code=%s AND trade_date=%s""",
                    [ts_code, trade_date]
                )
                row = cur.fetchone()
                if not row:
                    return api_not_found()
                return api_success(serialize_rows([row])[0])

            cur.execute(
                """SELECT * FROM technical_indicator
                   WHERE ts_code=%s ORDER BY trade_date DESC LIMIT %s""",
                [ts_code, limit]
            )
            rows = cur.fetchall()

        return api_success({
            'ts_code': ts_code,
            'count': len(rows),
            'data': serialize_rows(rows)
        })
    except Exception as e:
        logger.error(f"indicator error: {e}")
        return api_error(str(e))


# ─── GET /api/v1/trend/indicator/{ts_code}/history ──────────
@app.route('/api/v1/trend/indicator/<ts_code>/history', methods=['GET'])
def indicator_history(ts_code):
    """技术指标历史"""
    try:
        start = request.args.get('start', (datetime.now() - timedelta(days=90)).strftime('%Y-%m-%d'))
        end = request.args.get('end', datetime.now().strftime('%Y-%m-%d'))
        limit = min(int(request.args.get('limit', 200)), 500)

        with db_cursor(commit=False) as cur:
            cur.execute(
                """SELECT * FROM technical_indicator
                   WHERE ts_code=%s AND trade_date BETWEEN %s AND %s
                   ORDER BY trade_date DESC LIMIT %s""",
                [ts_code, start, end, limit]
            )
            rows = cur.fetchall()

        return api_success({
            'ts_code': ts_code,
            'start': start,
            'end': end,
            'count': len(rows),
            'data': serialize_rows(rows)
        })
    except Exception as e:
        logger.error(f"indicator_history error: {e}")
        return api_error(str(e))


# ─── GET /api/v1/trend/kline/{ts_code} ──────────────────────
@app.route('/api/v1/trend/kline/<ts_code>', methods=['GET'])
def kline(ts_code):
    """获取日K线数据"""
    try:
        start = request.args.get('start', (datetime.now() - timedelta(days=365)).strftime('%Y-%m-%d'))
        end = request.args.get('end', datetime.now().strftime('%Y-%m-%d'))
        limit = min(int(request.args.get('limit', 250)), 500)

        with db_cursor(commit=False) as cur:
            cur.execute(
                """SELECT * FROM daily_kline
                   WHERE ts_code=%s AND trade_date BETWEEN %s AND %s
                   ORDER BY trade_date DESC LIMIT %s""",
                [ts_code, start, end, limit]
            )
            rows = cur.fetchall()

        return api_success({
            'ts_code': ts_code,
            'count': len(rows),
            'data': serialize_rows(rows)
        })
    except Exception as e:
        logger.error(f"kline error: {e}")
        return api_error(str(e))


# ─── GET /api/v1/trend/stock/list ───────────────────────────
@app.route('/api/v1/trend/stock/list', methods=['GET'])
def stock_list():
    """获取股票基础信息列表"""
    try:
        industry = request.args.get('industry')
        market = request.args.get('market')
        limit = min(int(request.args.get('limit', 100)), 500)

        wheres = []
        params = []
        if industry:
            wheres.append("industry = %s")
            params.append(industry)
        if market:
            wheres.append("market = %s")
            params.append(market)

        sql = "SELECT * FROM stock_basic"
        if wheres:
            sql += " WHERE " + " AND ".join(wheres)
        sql += " LIMIT %s"
        params.append(limit)

        with db_cursor(commit=False) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

        return api_success({'count': len(rows), 'data': serialize_rows(rows)})
    except Exception as e:
        logger.error(f"stock_list error: {e}")
        return api_error(str(e))


# ─── GET /api/v1/trend/stock/{ts_code} ──────────────────────
@app.route('/api/v1/trend/stock/<ts_code>', methods=['GET'])
def stock_detail(ts_code):
    """获取单只股票基础信息"""
    try:
        with db_cursor(commit=False) as cur:
            cur.execute("SELECT * FROM stock_basic WHERE ts_code=%s", [ts_code])
            row = cur.fetchone()
        if not row:
            return api_not_found()
        return api_success(serialize_rows([row])[0])
    except Exception as e:
        logger.error(f"stock_detail error: {e}")
        return api_error(str(e))


# ─── 启动 ───────────────────────────────────────────────────
if __name__ == '__main__':
    logger.info("Starting trend_score API server on port 8888...")
    app.run(host='0.0.0.0', port=8888, debug=False)