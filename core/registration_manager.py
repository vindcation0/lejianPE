"""
报名管理与状态机模块 (RegistrationManager)
========================================
负责协调 StateStore、RequestManager 和 AuditLogger，执行「宁可少发，不可重复；发完必复核」原则。
确保任何 UNKNOWN / 异常结果必须经过服务端真实状态 (isRegister) 验证，
杜绝因网络抖动和网关 5xx 造成的重复报名。
"""
import datetime
from typing import Any, Callable, Dict, Optional, Tuple

from .audit_logger import AuditLogger
from .request_manager import RequestManager, SubmitResult
from .state_store import RegistrationStatus, StateStore, StateStoreError


class RegistrationManager:
    """报名状态机与业务协调器"""

    def __init__(
        self,
        state_store: StateStore,
        request_manager: RequestManager,
        audit_logger: AuditLogger
    ):
        self.state_store = state_store
        self.req_mgr = request_manager
        self.audit_logger = audit_logger

    def register_activity(
        self,
        item: Dict,
        validate_rules_cb: Optional[Callable[[Dict], Tuple[bool, str]]] = None
    ) -> Tuple[bool, str, str, Dict]:
        """
        报名主流程：
        1. 检查本地持久化状态与服务端当前状态。
        2. 若状态未决，先进行服务端复核。
        3. 规则合规且确认未报时，持久化 SUBMITTING。
        4. 提交一次报名请求（绝不盲目重试）。
        5. 明确成功/失败则更新持久化状态。
        6. 若遇超时或 5xx 异常，进入 UNKNOWN 并立即触发服务端真实状态复核。

        返回: (is_success: bool, final_status: str, message: str, final_item: Dict)
        """
        aid = str(item.get("id"))
        name = (item.get("name") or "").replace("\n", " ").strip()

        # ---------------- 1. 本地状态检查与冲突前置拦截 ----------------
        rec = self.state_store.get_record(aid)
        if rec:
            st = rec.get("status")
            if st == RegistrationStatus.SUCCESS:
                return False, RegistrationStatus.SUCCESS, f"本地已存在报名成功记录: {name}", item

            if st in (RegistrationStatus.SUBMITTING, RegistrationStatus.UNKNOWN, RegistrationStatus.VERIFYING):
                # 状态不明确，必须先向服务端求证真实状态
                verified, is_reg, remote_item = self.req_mgr.verify_registration(aid)
                if verified:
                    if is_reg:
                        self.state_store.record_success(aid, name, http_status=200)
                        self.audit_logger.log_event("pre_verify_success", {"aid": aid, "name": name})
                        return True, RegistrationStatus.SUCCESS, f"经复核该活动已在服务端报名成功: {name}", remote_item or item
                    else:
                        # 服务端确认未报名，重新评估
                        if validate_rules_cb:
                            ok, reason = validate_rules_cb(remote_item or item)
                            if not ok:
                                self.state_store.record_failed(aid, name, f"复核未报名但已不合规: {reason}")
                                return False, RegistrationStatus.FAILED, f"复核未报名但已不合规: {reason}", remote_item or item
                        self.state_store.record_retryable(aid, "复核确认未报名，允许重新评估提交")
                else:
                    # 复核接口本身失败，不能判断为未报，严禁冒险提交
                    self.state_store.record_unknown(aid, name, "前置复核失败，无法确认报名状态")
                    return False, RegistrationStatus.UNKNOWN, f"无法确认活动真实报名状态，暂停提交: {name}", item

        # 服务端已带 isRegister 的情形
        if item.get("isRegister"):
            self.state_store.record_success(aid, name, http_status=200)
            return True, RegistrationStatus.SUCCESS, f"活动已显示报名: {name}", item

        # ---------------- 2. 报名资格与规则二次确认 ----------------
        if validate_rules_cb:
            ok, reason = validate_rules_cb(item)
            if not ok:
                return False, RegistrationStatus.FAILED, f"规则校验未通过: {reason}", item

        # ---------------- 3. 提交前持久化 SUBMITTING ----------------
        t_start = datetime.datetime.now()
        attempts = 1
        try:
            cur_rec = self.state_store.record_submitting(aid, name)
            if cur_rec:
                attempts = cur_rec.get("attempts", 1)
        except StateStoreError as e:
            # 状态库不可写时，绝对不可发起无状态保护的提交
            return False, RegistrationStatus.FAILED, f"状态库持久化失败，拒绝提交以避免不可逆状态: {e}", item

        # ---------------- 4. 提交一次请求 ----------------
        res = self.req_mgr.submit_once("education/app/activity/signUp", {"activityId": aid})
        t_end = datetime.datetime.now()

        # ---------------- 5. 结果评估与复核闭环 ----------------
        # 5.1 明确业务成功
        if res.classification == "SUCCESS":
            self.state_store.record_success(aid, name, http_status=res.http_status)
            self.audit_logger.log_request(
                activity_id=aid,
                activity_name=name,
                action="sign_up",
                started_at=t_start,
                finished_at=t_end,
                http_status=res.http_status,
                business_code=res.json_data.get("code") if res.json_data else 0,
                business_success=True,
                registration_verified=True,
                final_state=RegistrationStatus.SUCCESS,
                attempts=attempts
            )
            return True, RegistrationStatus.SUCCESS, "报名成功", item

        # 5.2 明确业务失败（名额已满、已达上限等）
        if res.classification == "FAILED":
            self.state_store.record_failed(aid, name, error=res.reason, http_status=res.http_status)
            self.audit_logger.log_request(
                activity_id=aid,
                activity_name=name,
                action="sign_up",
                started_at=t_start,
                finished_at=t_end,
                http_status=res.http_status,
                business_code=res.json_data.get("code") if res.json_data else None,
                business_success=False,
                registration_verified=None,
                final_state=RegistrationStatus.FAILED,
                attempts=attempts,
                error_message=res.reason
            )
            return False, RegistrationStatus.FAILED, res.reason, item

        # 5.3 结果未知 (HTTP 500, 502, 503, 超时, 未明响应等)
        # 必须先置为 UNKNOWN，再进行服务端状态复核
        self.state_store.record_unknown(aid, name, error=res.reason or res.error or "", http_status=res.http_status)
        self.state_store.record_verifying(aid)

        verified, is_reg, remote_item = self.req_mgr.verify_registration(aid)
        if verified:
            if is_reg:
                # 提交虽然报异常，但服务端实际上已经落库成功！
                self.state_store.record_success(aid, name, http_status=res.http_status)
                self.audit_logger.log_request(
                    activity_id=aid,
                    activity_name=name,
                    action="sign_up_and_verify",
                    started_at=t_start,
                    finished_at=datetime.datetime.now(),
                    http_status=res.http_status,
                    business_code=res.json_data.get("code") if res.json_data else None,
                    business_success=True,
                    registration_verified=True,
                    final_state=RegistrationStatus.SUCCESS,
                    attempts=attempts,
                    error_message=f"提交报 {res.reason}，但复核确认已成功"
                )
                return True, RegistrationStatus.SUCCESS, "提交遇到网关异常，但复核确认报名成功", remote_item or item
            else:
                # 服务端明确确认未报名
                self.state_store.record_retryable(aid, f"提交异常({res.reason})，复核确认未报名")
                self.audit_logger.log_request(
                    activity_id=aid,
                    activity_name=name,
                    action="sign_up_and_verify",
                    started_at=t_start,
                    finished_at=datetime.datetime.now(),
                    http_status=res.http_status,
                    business_code=res.json_data.get("code") if res.json_data else None,
                    business_success=False,
                    registration_verified=True,
                    final_state=RegistrationStatus.RETRYABLE,
                    attempts=attempts,
                    error_message=f"提交报 {res.reason}，复核确认未报名"
                )
                return False, RegistrationStatus.RETRYABLE, f"提交异常({res.reason})，复核确认未报名", remote_item or item
        else:
            # 复核未能确认（列表缺失或复核请求超时）
            self.state_store.record_unknown(aid, name, error=f"提交异常({res.reason})且复核未能确认状态", http_status=res.http_status)
            self.audit_logger.log_request(
                activity_id=aid,
                activity_name=name,
                action="sign_up_and_verify",
                started_at=t_start,
                finished_at=datetime.datetime.now(),
                http_status=res.http_status,
                business_code=res.json_data.get("code") if res.json_data else None,
                business_success=None,
                registration_verified=False,
                final_state=RegistrationStatus.UNKNOWN,
                attempts=attempts,
                error_message=f"提交报 {res.reason}，且复核未能取得确认"
            )
            return False, RegistrationStatus.UNKNOWN, f"提交异常且无法复核状态({res.reason})，保持 UNKNOWN", item
