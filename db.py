"""
SQLite 持久化模块 (db.py)
管理本地 qq_secretary.db，包含多线程安全访问锁与短连接操作。
"""

import os
import sqlite3
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qq_secretary.db")
_db_lock = threading.Lock()


def get_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    """获取 SQLite 连接，配置短连接超时与线程安全性"""
    path = db_path or DEFAULT_DB_PATH
    conn = sqlite3.connect(path, timeout=15.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn



def init_db(db_path: Optional[str] = None) -> None:
    """初始化数据库表结构"""
    with _db_lock:
        conn = get_connection(db_path)
        try:
            with conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS notifications (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        source TEXT NOT NULL,
                        urgency TEXT NOT NULL,
                        summary TEXT NOT NULL,
                        action_item TEXT,
                        raw_text TEXT NOT NULL,
                        is_pushed INTEGER DEFAULT 0,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_notifications_urgency_pushed 
                    ON notifications (urgency, is_pushed)
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_notifications_created_at 
                    ON notifications (created_at)
                    """
                )
        finally:
            conn.close()


def insert_notification(
    source: str,
    urgency: str,
    summary: str,
    action_item: Optional[str],
    raw_text: str,
    is_pushed: int = 0,
    db_path: Optional[str] = None,
) -> int:
    """
    插入单条通知记录
    :return: 插入记录的自增 ID
    """
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _db_lock:
        conn = get_connection(db_path)
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO notifications 
                    (source, urgency, summary, action_item, raw_text, is_pushed, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (source, urgency, summary, action_item, raw_text, is_pushed, created_at),
                )
                return cursor.lastrowid
        finally:
            conn.close()


def get_unpushed_deferred(db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """获取所有尚未推送的 DEFERRED 记录"""
    with _db_lock:
        conn = get_connection(db_path)
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, source, urgency, summary, action_item, raw_text, is_pushed, created_at
                FROM notifications
                WHERE urgency = 'DEFERRED' AND is_pushed = 0
                ORDER BY id ASC
                """
            )
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()


def mark_deferred_pushed(ids: List[int], db_path: Optional[str] = None) -> None:
    """标记指定的通知 ID 列表为已推送 (is_pushed = 1)"""
    if not ids:
        return
    with _db_lock:
        conn = get_connection(db_path)
        try:
            with conn:
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"UPDATE notifications SET is_pushed = 1 WHERE id IN ({placeholders})",
                    ids,
                )
        finally:
            conn.close()


def get_today_notifications(db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """获取当天本地时间 00:00 至今的所有有效记录（按时间升序）"""
    today_start = datetime.now().strftime("%Y-%m-%d 00:00:00")
    with _db_lock:
        conn = get_connection(db_path)
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, source, urgency, summary, action_item, raw_text, is_pushed, created_at
                FROM notifications
                WHERE created_at >= ?
                ORDER BY id ASC
                """,
                (today_start,),
            )
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()


def get_stats(db_path: Optional[str] = None) -> Dict[str, Any]:
    """获取今日消息统计（用于 /health 端点）"""
    today_start = datetime.now().strftime("%Y-%m-%d 00:00:00")
    with _db_lock:
        conn = get_connection(db_path)
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT urgency, COUNT(*) as count 
                FROM notifications 
                WHERE created_at >= ? 
                GROUP BY urgency
                """,
                (today_start,),
            )
            counts = {row["urgency"]: row["count"] for row in cursor.fetchall()}

            cursor.execute(
                """
                SELECT COUNT(*) as count 
                FROM notifications 
                WHERE urgency = 'DEFERRED' AND is_pushed = 0
                """
            )
            unpushed_deferred_count = cursor.fetchone()["count"]

            return {
                "today_counts": counts,
                "unpushed_deferred_count": unpushed_deferred_count,
            }
        finally:
            conn.close()
