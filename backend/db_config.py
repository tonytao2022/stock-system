"""
db_config.py — 数据库配置 + Tushare Token + 响应工具
统一响应格式: {code, message, data, error, timestamp, request_id}
"""
import os
import uuid
import pymysql
from datetime import datetime, date
from contextlib import contextmanager
from flask import jsonify

# ─── 密码获取 ────────────────────────────────────────────────
_db_password = None

def _get_password():
    global _db_password
    if _db_password is None:
        try:
            with open('/etc/mysql/debian.cnf') as f:
                for line in f:
                    if 'password' in line:
                        _db_password = line.strip().split('=')[-1].strip().strip('"').strip("'")
                        break
        except Exception:
            _db_password = os.environ.get('MYSQL_PASSWORD', '')
    return _db_password


DB_CONFIG = {
    'host': '127.0.0.1',
    'port': 3306,
    'user': 'debian-sys-maint',
    'password': '',
    'database': 'stock_db',
    'charset': 'utf8mb4',
    'cursorclass': pymysql.cursors.DictCursor,
    'autocommit': True,
}


def get_connection():
    config = DB_CONFIG.copy()
    config['password'] = _get_password()
    return pymysql.connect(**config)


@contextmanager
def db_cursor(commit=True):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        yield cursor
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


def get_tushare_token():
    return os.environ.get('TUSHARE_TOKEN', '')


# ─── 统一响应工具 ────────────────────────────────────────────
def api_success(data=None, message="success", code=0):
    """统一成功响应"""
    return jsonify({
        "code": code,
        "message": message,
        "data": data if data is not None else {},
        "error": None,
        "timestamp": datetime.now().isoformat(),
        "request_id": str(uuid.uuid4())[:8]
    })

def api_error(error="unknown error", code=-1, message=None, http_status=500):
    """统一错误响应: code=-1表示数据异常"""
    return jsonify({
        "code": code,
        "message": message or error,
        "data": None,
        "error": error,
        "timestamp": datetime.now().isoformat(),
        "request_id": str(uuid.uuid4())[:8]
    }), http_status

def api_not_found():
    return api_error("数据不存在", code=2001, http_status=404)


# ─── 序列化 ──────────────────────────────────────────────────
def serialize_rows(rows):
    """处理 date/datetime 序列化为ISO格式字符串"""
    result = []
    for row in rows:
        item = {}
        for k, v in row.items():
            if isinstance(v, (date, datetime)):
                item[k] = v.isoformat()
            elif isinstance(v, bytes):
                item[k] = v.decode('utf-8')
            else:
                item[k] = v
        result.append(item)
    return result


# 兼容旧名
_serialize_rows = serialize_rows

# ─── 用户ID管理 ────────────────────────────────────────────
def get_user_id():
    """从system_config获取默认用户ID，硬编码统一入口"""
    try:
        cur = _get_cursor()
        cur.execute("SELECT config_value FROM system_config WHERE config_key='default_user_id' LIMIT 1")
        r = cur.fetchone()
        cur.close()
        if r:
            v = r['config_value'] if isinstance(r, dict) else r[0]
            if v: return v
    except:
        pass
    return 'tony'

def _get_cursor():
    """内部获取游标，不依赖flask上下文"""
    conn = pymysql.connect(host='127.0.0.1', port=3306, user='debian-sys-maint',
        password=_get_password(), database='stock_db', charset='utf8mb4')
    return conn.cursor(pymysql.cursors.DictCursor)

# ─── 铁律: 数据标记 + 重试 ────────────────────────────────────
DATA_ERROR_MARKER = -1
# -1标记的数据不可参与评分/回测计算
# API失败→等15秒→重试3次→仍失败置为-1并报警

