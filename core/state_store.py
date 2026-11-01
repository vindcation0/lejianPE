"""
SQLite 状态持久化模块 (StateStore)
==================================
管理报名状态机的本地持久化记录，保证在崩溃、断电、异常退出时状态不丢失。
使用事务确保原子更新。
"""
import datetime
import os
import sqlite3
from typing import Dict, List, Optional


class RegistrationStatus:
    PENDING = "PENDING"        # 发现符合条件的活动，尚未提交
    SUBMITTING = "SUBMITTING"  # 正在提交报名请求
    SUCCESS = "SUCCESS"        # 已确认报名成功
    FAILED = "FAILED"          # 已确认业务失败（如名额已满）
    UNKNOWN = "UNKNOWN"        # 请求结果未知（超时、网关 502/503、未明响应）
    VERIFYING = "VERIFYING"    # 正在通过服务端核实真实报名状态
    RETRYABLE = "RETRYABLE"    # 服务端确认未报名且活动依然有效，允许重新评估后提交


class StateStoreError(Exception):
    """状态库操作异常"""
    pass


def now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="milliseconds")


class StateStore:
    """管理 registration_records 表的 SQLite 存储器"""

    def __init__(self, db_path: str):
        self.is_memory = (db_path == ":memory:")
        if self.is_memory:
            self.db_path = ":memory:"
        else:
            self.db_path = os.path.abspath(db_path)
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._conn = None
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, timeout=10.0, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            if not self.is_memory:
                # 启用 WAL 模式提高并发性能与抗崩溃能力
                self._conn.execute("PRAGMA journal_mode=WAL;")
                self._conn.execute("PRAGMA synchronous=NORMAL;")
        return self._conn

    def _init_db(self):
        try:
            conn = self._get_conn()
            with conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS registration_records (
                        activity_id TEXT PRIMARY KEY,
                        activity_name TEXT,
                        status TEXT NOT NULL,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        last_error TEXT,
                        updated_at TEXT NOT NULL,
                        verified_at TEXT,
                        last_http_status INTEGER
                    );
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_status ON registration_records(status);
                """)
        except Exception as e:
            raise StateStoreError(f"初始化 SQLite 状态库失败: {e}") from e

    def get_record(self, activity_id: str) -> Optional[Dict]:
        """获取单个活动的最新持久化状态"""
        try:
            conn = self._get_conn()
            cur = conn.execute(
                "SELECT * FROM registration_records WHERE activity_id = ?",
                (str(activity_id),)
            )
            row = cur.fetchone()
            return dict(row) if row else None
        except Exception as e:
            raise StateStoreError(f"查询活动记录失败 {activity_id}: {e}") from e

    def get_all_records(self) -> List[Dict]:
        """获取所有记录"""
        try:
            conn = self._get_conn()
            cur = conn.execute("SELECT * FROM registration_records ORDER BY updated_at DESC")
            return [dict(row) for row in cur.fetchall()]
        except Exception as e:
            raise StateStoreError(f"查询所有记录失败: {e}") from e

    def get_unresolved_records(self) -> List[Dict]:
        """
        获取未决状态的记录：SUBMITTING, UNKNOWN, VERIFYING
        用于崩溃恢复与断点复核。
        """
        try:
            conn = self._get_conn()
            cur = conn.execute(
                "SELECT * FROM registration_records WHERE status IN (?, ?, ?) ORDER BY updated_at ASC",
                (RegistrationStatus.SUBMITTING, RegistrationStatus.UNKNOWN, RegistrationStatus.VERIFYING)
            )
            return [dict(row) for row in cur.fetchall()]
        except Exception as e:
            raise StateStoreError(f"查询未决记录失败: {e}") from e

    def has_unresolved(self, exclude_activity_id: Optional[str] = None) -> bool:
        """检查是否存在未决状态的记录（可排除指定活动自身）"""
        try:
            conn = self._get_conn()
            if exclude_activity_id is not None:
                cur = conn.execute(
                    "SELECT 1 FROM registration_records WHERE status IN (?, ?, ?) AND activity_id != ? LIMIT 1",
                    (RegistrationStatus.SUBMITTING, RegistrationStatus.UNKNOWN, RegistrationStatus.VERIFYING, str(exclude_activity_id))
                )
            else:
                cur = conn.execute(
                    "SELECT 1 FROM registration_records WHERE status IN (?, ?, ?) LIMIT 1",
                    (RegistrationStatus.SUBMITTING, RegistrationStatus.UNKNOWN, RegistrationStatus.VERIFYING)
                )
            return cur.fetchone() is not None
        except Exception as e:
            raise StateStoreError(f"检查未决记录失败: {e}") from e

    def get_unresolved_activity_ids(self) -> List[str]:
        """获取所有处于未决状态的 activity_id 列表"""
        return [r["activity_id"] for r in self.get_unresolved_records()]

    def record_submitting(self, activity_id: str, activity_name: str) -> Dict:
        """
        进入提交阶段（事务内写入）。
        如果已有记录则累加 attempts，否则新建记录。
        """
        t = now_iso()
        try:
            conn = self._get_conn()
            with conn:
                conn.execute("""
                    INSERT INTO registration_records (
                        activity_id, activity_name, status, attempts, last_error, updated_at
                    ) VALUES (?, ?, ?, 1, NULL, ?)
                    ON CONFLICT(activity_id) DO UPDATE SET
                        status = ?,
                        attempts = attempts + 1,
                        updated_at = ?;
                """, (
                    str(activity_id),
                    activity_name,
                    RegistrationStatus.SUBMITTING,
                    t,
                    RegistrationStatus.SUBMITTING,
                    t
                ))
            return self.get_record(activity_id)
        except Exception as e:
            raise StateStoreError(f"更新 SUBMITTING 状态失败: {e}") from e

    def record_success(self, activity_id: str, activity_name: str = "", http_status: int = 200) -> Dict:
        """记录报名明确成功"""
        t = now_iso()
        try:
            conn = self._get_conn()
            with conn:
                conn.execute("""
                    INSERT INTO registration_records (
                        activity_id, activity_name, status, attempts, updated_at, verified_at, last_http_status
                    ) VALUES (?, ?, ?, 1, ?, ?, ?)
                    ON CONFLICT(activity_id) DO UPDATE SET
                        status = ?,
                        updated_at = ?,
                        verified_at = ?,
                        last_http_status = ?;
                """, (
                    str(activity_id),
                    activity_name,
                    RegistrationStatus.SUCCESS,
                    t,
                    t,
                    http_status,
                    RegistrationStatus.SUCCESS,
                    t,
                    t,
                    http_status
                ))
            return self.get_record(activity_id)
        except Exception as e:
            raise StateStoreError(f"更新 SUCCESS 状态失败: {e}") from e

    def record_failed(self, activity_id: str, activity_name: str = "", error: str = "", http_status: Optional[int] = None) -> Dict:
        """记录业务明确失败"""
        t = now_iso()
        try:
            conn = self._get_conn()
            with conn:
                conn.execute("""
                    INSERT INTO registration_records (
                        activity_id, activity_name, status, attempts, last_error, updated_at, last_http_status
                    ) VALUES (?, ?, ?, 1, ?, ?, ?)
                    ON CONFLICT(activity_id) DO UPDATE SET
                        status = ?,
                        last_error = ?,
                        updated_at = ?,
                        last_http_status = ?;
                """, (
                    str(activity_id),
                    activity_name,
                    RegistrationStatus.FAILED,
                    error,
                    t,
                    http_status,
                    RegistrationStatus.FAILED,
                    error,
                    t,
                    http_status
                ))
            return self.get_record(activity_id)
        except Exception as e:
            raise StateStoreError(f"更新 FAILED 状态失败: {e}") from e

    def record_unknown(self, activity_id: str, activity_name: str = "", error: str = "", http_status: Optional[int] = None) -> Dict:
        """记录请求结果未知（如 502/503/超时）"""
        t = now_iso()
        try:
            conn = self._get_conn()
            with conn:
                conn.execute("""
                    INSERT INTO registration_records (
                        activity_id, activity_name, status, attempts, last_error, updated_at, last_http_status
                    ) VALUES (?, ?, ?, 1, ?, ?, ?)
                    ON CONFLICT(activity_id) DO UPDATE SET
                        status = ?,
                        last_error = ?,
                        updated_at = ?,
                        last_http_status = ?;
                """, (
                    str(activity_id),
                    activity_name,
                    RegistrationStatus.UNKNOWN,
                    error,
                    t,
                    http_status,
                    RegistrationStatus.UNKNOWN,
                    error,
                    t,
                    http_status
                ))
            return self.get_record(activity_id)
        except Exception as e:
            raise StateStoreError(f"更新 UNKNOWN 状态失败: {e}") from e

    def record_verifying(self, activity_id: str) -> Dict:
        """记录正在复核"""
        t = now_iso()
        try:
            conn = self._get_conn()
            with conn:
                conn.execute("""
                    UPDATE registration_records
                    SET status = ?, updated_at = ?
                    WHERE activity_id = ?;
                """, (RegistrationStatus.VERIFYING, t, str(activity_id)))
            return self.get_record(activity_id)
        except Exception as e:
            raise StateStoreError(f"更新 VERIFYING 状态失败: {e}") from e

    def record_retryable(self, activity_id: str, reason: str = "") -> Dict:
        """记录服务端已明确确认未报名，重新评估后可重试"""
        t = now_iso()
        try:
            conn = self._get_conn()
            with conn:
                conn.execute("""
                    UPDATE registration_records
                    SET status = ?, last_error = ?, updated_at = ?, verified_at = ?
                    WHERE activity_id = ?;
                """, (RegistrationStatus.RETRYABLE, reason, t, t, str(activity_id)))
            return self.get_record(activity_id)
        except Exception as e:
            raise StateStoreError(f"更新 RETRYABLE 状态失败: {e}") from e

    def close(self):
        """关闭数据库连接"""
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
