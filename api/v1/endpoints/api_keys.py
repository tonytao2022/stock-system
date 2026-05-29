# -*- coding: utf-8 -*-
"""
===================================
API Key 管理端点
===================================

职责：
1. 管理 openclaw_config.api_credentials 表中的 API Key
2. 提供查询、修改值、启用/禁用功能
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional
import pymysql

from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)

router = APIRouter()

# 数据库配置 - 复用现有连接方式
MYSQL_USER = 'debian-sys-maint'


def _get_mysql_pass() -> str:
    """从debian.cnf获取MySQL密码"""
    import configparser
    c = configparser.ConfigParser()
    c.read('/etc/mysql/debian.cnf')
    return c['client']['password']


def _get_conn():
    cfg = {
        'host': '127.0.0.1',
        'port': 3306,
        'user': MYSQL_USER,
        'password': _get_mysql_pass(),
        'database': 'openclaw_config',
        'charset': 'utf8mb4',
        'cursorclass': pymysql.cursors.DictCursor,
    }
    return pymysql.connect(**cfg)


@router.get(
    "/system/api-keys",
    summary="获取所有API Key",
    description="从 openclaw_config.api_credentials 查询所有 API Key，默认隐藏 key 值",
)
def list_api_keys():
    """查询所有 API Key 记录"""
    conn = None
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT id, name, provider, api_key, description, is_active, created_at, updated_at "
            "FROM api_credentials ORDER BY id ASC"
        )
        rows = cur.fetchall()
        cur.close()

        keys = []
        for r in rows:
            keys.append({
                "id": r["id"],
                "name": r["name"],
                "provider": r["provider"],
                # 默认隐藏真实 key，返回掩码
                "api_key": r["api_key"][:6] + "****" + r["api_key"][-4:] if len(r["api_key"]) > 12 else "****",
                "api_key_masked": True,
                "description": r.get("description", ""),
                "is_active": bool(r["is_active"]),
                "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
                "updated_at": r["updated_at"].isoformat() if r.get("updated_at") else None,
            })

        return {"code": 0, "data": keys, "message": "success"}

    except Exception as exc:
        logger.error("Failed to list API keys: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": str(exc)})
    finally:
        if conn:
            conn.close()


@router.get(
    "/system/api-keys/{key_id}",
    summary="获取单个API Key（含明文）",
    description="获取指定 ID 的 API Key，返回明文值",
)
def get_api_key(key_id: int):
    """查询单个 API Key 明文（用于编辑时回填）"""
    conn = None
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT id, name, provider, api_key, description, is_active, created_at, updated_at "
            "FROM api_credentials WHERE id=%s",
            (key_id,),
        )
        r = cur.fetchone()
        cur.close()

        if not r:
            raise HTTPException(status_code=404, detail={"error": "not_found", "message": "API Key not found"})

        return {
            "code": 0,
            "data": {
                "id": r["id"],
                "name": r["name"],
                "provider": r["provider"],
                "api_key": r["api_key"],  # 明文返回
                "description": r.get("description", ""),
                "is_active": bool(r["is_active"]),
                "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
                "updated_at": r["updated_at"].isoformat() if r.get("updated_at") else None,
            },
            "message": "success",
        }

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to get API key %s: %s", key_id, exc, exc_info=True)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": str(exc)})
    finally:
        if conn:
            conn.close()


@router.post(
    "/system/api-keys/update",
    summary="修改API Key值",
    description="更新某个 API Key 的 api_key 字段值",
)
def update_api_key(body: dict):
    """更新指定 API Key 的值"""
    key_id = body.get("id")
    new_key = body.get("api_key")

    if not key_id or not new_key:
        raise HTTPException(status_code=400, detail={"error": "bad_request", "message": "id and api_key are required"})

    conn = None
    try:
        conn = _get_conn()
        cur = conn.cursor()

        # 先验证记录存在
        cur.execute("SELECT id FROM api_credentials WHERE id=%s", (key_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail={"error": "not_found", "message": "API Key not found"})

        cur.execute(
            "UPDATE api_credentials SET api_key=%s, updated_at=NOW() WHERE id=%s",
            (new_key, key_id),
        )
        conn.commit()
        cur.close()

        return {"code": 0, "message": "API Key updated successfully"}

    except HTTPException:
        raise
    except Exception as exc:
        if conn:
            conn.rollback()
        logger.error("Failed to update API key %s: %s", key_id, exc, exc_info=True)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": str(exc)})
    finally:
        if conn:
            conn.close()


@router.post(
    "/system/api-keys/toggle",
    summary="启用/禁用API Key",
    description="切换某个 API Key 的 is_active 状态",
)
def toggle_api_key(body: dict):
    """切换 API Key 的启用/禁用状态"""
    key_id = body.get("id")

    if not key_id:
        raise HTTPException(status_code=400, detail={"error": "bad_request", "message": "id is required"})

    conn = None
    try:
        conn = _get_conn()
        cur = conn.cursor()

        cur.execute("SELECT id, is_active FROM api_credentials WHERE id=%s", (key_id,))
        r = cur.fetchone()
        if not r:
            raise HTTPException(status_code=404, detail={"error": "not_found", "message": "API Key not found"})

        new_status = 0 if r["is_active"] else 1
        cur.execute(
            "UPDATE api_credentials SET is_active=%s, updated_at=NOW() WHERE id=%s",
            (new_status, key_id),
        )
        conn.commit()
        cur.close()

        return {
            "code": 0,
            "message": "API Key status toggled",
            "data": {"id": key_id, "is_active": bool(new_status)},
        }

    except HTTPException:
        raise
    except Exception as exc:
        if conn:
            conn.rollback()
        logger.error("Failed to toggle API key %s: %s", key_id, exc, exc_info=True)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": str(exc)})
    finally:
        if conn:
            conn.close()
