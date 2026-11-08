"""
启动恢复与异常核对管理器 (RecoveryManager)
========================================
负责程序启动时对历史未完成操作（SUBMITTING / UNKNOWN / VERIFYING）进行扫描与服务端对账恢复。
确保程序崩溃、断电重启后，能够准确恢复真实报名状态，绝不轻率重复提交。
"""
from typing import Dict, List, Tuple
from .audit_logger import AuditLogger
from .request_manager import RequestManager
from .state_store import RegistrationStatus, StateStore


class RecoveryManager:
    """崩溃与异常恢复管理器"""

    def __init__(
        self,
        state_store: StateStore,
        request_manager: RequestManager,
        audit_logger: AuditLogger
    ):
        self.state_store = state_store
        self.req_mgr = request_manager
        self.audit_logger = audit_logger

    def recover_on_startup(self) -> Dict[str, List[Dict]]:
        """
        程序启动时执行状态对账：
        1. 检查是否存在上次运行遗留的未决记录 (SUBMITTING, UNKNOWN, VERIFYING)。
        2. 如果存在，逐个通过服务端 getActivityList 进行真实状态核实。
        3. 发现 isRegister=True 则恢复为 SUCCESS。
        4. 发现 isRegister=False 则标记为 RETRYABLE。
        5. 复核失败或不在列表则维持 UNKNOWN。

        返回恢复汇总字典：{"recovered_success": [...], "marked_retryable": [...], "remained_unknown": [...]}
        """
        unresolved = self.state_store.get_unresolved_records()
        summary = {
            "recovered_success": [],
            "marked_retryable": [],
            "remained_unknown": []
        }

        if not unresolved:
            return summary

        self.audit_logger.log_event("startup_recovery_start", {"count": len(unresolved)})

        for rec in unresolved:
            aid = rec["activity_id"]
            name = rec["activity_name"]
            orig_status = rec["status"]

            self.state_store.record_verifying(aid)
            verified, is_reg, remote_item = self.req_mgr.verify_registration(aid)

            if verified:
                if is_reg:
                    # 服务端已确认存在有效报名
                    self.state_store.record_success(aid, name, http_status=200)
                    rec_copy = dict(rec)
                    if remote_item:
                        rec_copy["remote_item"] = remote_item
                    summary["recovered_success"].append(rec_copy)
                    self.audit_logger.log_event("startup_recovered_success", {
                        "aid": aid,
                        "name": name,
                        "orig_status": orig_status
                    })
                else:
                    # 服务端确认未报名
                    self.state_store.record_retryable(aid, "启动恢复确认未报名")
                    summary["marked_retryable"].append(rec)
                    self.audit_logger.log_event("startup_marked_retryable", {
                        "aid": aid,
                        "name": name,
                        "orig_status": orig_status
                    })
            else:
                # 复核失败，保留 UNKNOWN
                self.state_store.record_unknown(aid, name, "启动复核失败，维持未知状态")
                summary["remained_unknown"].append(rec)
                self.audit_logger.log_event("startup_remained_unknown", {
                    "aid": aid,
                    "name": name,
                    "orig_status": orig_status
                })

        self.audit_logger.log_event("startup_recovery_done", {
            "recovered_success_count": len(summary["recovered_success"]),
            "marked_retryable_count": len(summary["marked_retryable"]),
            "remained_unknown_count": len(summary["remained_unknown"])
        })

        return summary
