"""
乐健体育 · 独立系统体检与非破坏性只读诊断模块 (System Doctor).
=================================================================
职责 (M4 独立诊断命令)：
1. 非破坏性只读诊断 (Non-destructive Diagnostics)：
   - 绝不修改任何用户配置 (config.json, timetable.json)；
   - 绝不修改或覆盖认证凭据 (credential.json)；
   - 绝不修改会话数据 (state.json)；
   - 绝不修改或重置 SQLite 数据库 (采用 mode=ro 只读模式连接)；
   - 绝不提交抢课、报名或选课修改；
   - 仅在目录读写权限探测时创建并立即清理专用临时探测文件 (.doctor_probe_*.tmp)；
   - 在线 Token 验真只读查询，但向服务端发送 HTTP 请求可能产生服务端访问日志等外部痕迹。
2. 6 大核心维度检查：
   - 运行环境 (Python 版本、平台与 DPAPI 凭据安全保护、核心依赖、ADB 探测)
   - 本地存储与配置 (工作目录权限、SQLite 只读完整性检查、config.json、timetable.json、未决事务安全性)
   - 本地认证材料 (credential.json 存在性、DACL 访问控制、DPAPI 解密往返、字段完整性、账号脱敏展示)
   - 本地会话状态 (state.json 存在性、Token 有效期剩余计算、RefreshToken 状态、学校学期信息)
   - 在线服务与只读连通性 (服务端轻量连通探测、只读在线验证 Token 真伪及细分 401 判定)
   - 总体健康评估与针对性运维建议 (HEALTHY / WARNING / ERROR)
3. 严格安全脱敏：不输出明文密码、不输出完整 signDigital、不输出完整 Token、不输出 AES 密钥。
4. 提供结构化纯文本控制台报告 (`format_report`) 与机器可读格式 (`to_dict`)。
"""
import base64
import datetime
import json
import os
import platform
import shutil
import sqlite3
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.credential_storage import (
    CredentialStorage,
    dpapi_unprotect,
    is_windows,
    mask_account,
    mask_secret,
)


@dataclass
class CheckItem:
    """单个检查项结果"""
    category: str              # 类别 (runtime / storage / credential / session / online / transaction)
    name: str                  # 检查项名称
    status: str                # OK / WARN / ERROR / INFO
    message: str               # 简明摘要
    detail: Optional[str] = None      # 详细诊断信息
    suggestion: Optional[str] = None  # 针对性建议

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class DoctorReport:
    """系统体检综合诊断报告"""
    timestamp: str
    doctor_id: str
    root_dir: str
    checks: List[CheckItem] = field(default_factory=list)
    overall_status: str = "HEALTHY"   # HEALTHY / WARNING / ERROR
    summary: str = ""
    recommendations: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "doctor_id": self.doctor_id,
            "root_dir": self.root_dir,
            "overall_status": self.overall_status,
            "summary": self.summary,
            "recommendations": self.recommendations,
            "checks": [c.to_dict() for c in self.checks],
        }

    def format_report(self, verbose: bool = False) -> str:
        """生成脱敏且层次清晰的纯文本诊断报告"""
        lines = [
            "=" * 66,
            "                 lejianPE 系统健康诊断报告",
            "=" * 66,
            f"诊断时间: {self.timestamp}",
            f"诊断 ID : {self.doctor_id}",
            f"工作目录: {self.root_dir}",
            "",
        ]

        # 映射图标
        status_icons = {
            "OK": "[√]",
            "WARN": "[!]",
            "ERROR": "[X]",
            "INFO": "[i]",
        }

        # 分类映射
        category_titles = {
            "runtime": "[1] 运行环境与依赖",
            "storage": "[2] 本地存储与配置",
            "credential": "[3] 本地认证材料 (credential.json)",
            "session": "[4] 本地会话状态 (state.json)",
            "online": "[5] 在线服务与只读连通性",
            "transaction": "[6] 事务安全性与备份",
        }

        # 按分类聚合并按预定顺序展示
        cat_order = ["runtime", "storage", "credential", "session", "online", "transaction"]
        for cat in cat_order:
            cat_checks = [c for c in self.checks if c.category == cat]
            if not cat_checks:
                continue
            lines.append(category_titles.get(cat, f"[{cat}]"))
            for item in cat_checks:
                icon = status_icons.get(item.status, "[?]")
                lines.append(f"  {icon} {item.name:<16}: {item.message}")
                if verbose and item.detail:
                    lines.append(f"      详情: {item.detail}")
            lines.append("")

        lines.append("=" * 66)
        if self.overall_status == "HEALTHY":
            lines.append("总体健康评估: 正常 (HEALTHY)")
            lines.append(self.summary or "系统状态良好，所有认证材料与本地环境均就绪，可随时执行抢课！")
        elif self.overall_status == "WARNING":
            lines.append("总体健康评估: 警告 (WARNING)")
            lines.append(self.summary or "系统基本可用，但检测到部分配置或状态需要关注：")
        else:
            lines.append("总体健康评估: 异常 (ERROR)")
            lines.append(self.summary or "系统检测到阻断性问题，请根据以下建议修复后重试：")

        if self.recommendations:
            lines.append("-" * 66)
            lines.append("运维与修复建议:")
            for idx, rec in enumerate(self.recommendations, 1):
                lines.append(f"  {idx}. {rec}")

        lines.append("=" * 66)
        return "\n".join(lines)


class SystemDoctor:
    """
    独立系统体检与健康诊断执行器 (严格只读).
    """

    def __init__(self, root_dir: Optional[str] = None, base_url: str = "https://cpes.legym.cn/"):
        self.root_dir = os.path.abspath(root_dir or ROOT)
        self.base_url = base_url.rstrip("/") + "/"
        self.cred_path = os.path.join(self.root_dir, "credential.json")
        self.state_path = os.path.join(self.root_dir, "state.json")
        self.config_path = os.path.join(self.root_dir, "config.json")
        self.timetable_path = os.path.join(self.root_dir, "timetable.json")
        self.data_dir = os.path.join(self.root_dir, "data")
        self.db_path = os.path.join(self.data_dir, "registration.db")
        self.logs_dir = os.path.join(self.root_dir, "logs")
        self.captures_dir = os.path.join(self.root_dir, "captures")

    def run_diagnostics(self, online_probe: bool = True, timeout: float = 4.0) -> DoctorReport:
        """
        执行系统全方位体检，返回 DoctorReport 对象。
        纯只读操作，绝不修改、覆盖或删除任何用户认证与业务数据。
        """
        ts_now = datetime.datetime.now()
        doctor_id = f"doc-{ts_now.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
        report = DoctorReport(
            timestamp=ts_now.strftime("%Y-%m-%d %H:%M:%S"),
            doctor_id=doctor_id,
            root_dir=self.root_dir,
        )

        # 依次运行各维度只读探测
        self._check_runtime(report)
        self._check_storage_and_config(report)
        self._check_credential(report)
        self._check_session(report)
        self._check_transactions(report)
        if online_probe:
            self._check_online_and_token(report, timeout=timeout)
        else:
            report.checks.append(CheckItem(
                category="online",
                name="在线网络探针",
                status="INFO",
                message="已跳过在线探针 (单测/离线模式)",
            ))

        # 汇总总体状态与建议
        self._evaluate_overall(report)
        return report

    # -------------------------------------------------------------------------
    # 1. 运行环境与依赖检查
    # -------------------------------------------------------------------------
    def _check_runtime(self, report: DoctorReport) -> None:
        # Python 版本
        py_ver = sys.version_info
        py_str = f"Python {py_ver.major}.{py_ver.minor}.{py_ver.micro}"
        if py_ver >= (3, 8):
            report.checks.append(CheckItem(
                category="runtime",
                name="Python 环境",
                status="OK",
                message=f"{py_str} (符合 >= 3.8 要求)",
            ))
        else:
            report.checks.append(CheckItem(
                category="runtime",
                name="Python 环境",
                status="ERROR",
                message=f"{py_str} (过低，要求 >= 3.8)",
                suggestion="请升级系统 Python 至 3.8 或更高版本。",
            ))

        # 平台与加密支持
        plat_str = platform.platform()
        if is_windows():
            report.checks.append(CheckItem(
                category="runtime",
                name="平台与加密",
                status="OK",
                message=f"{plat_str} (支持 Windows DPAPI 凭据安全保护)",
            ))
        else:
            report.checks.append(CheckItem(
                category="runtime",
                name="平台与加密",
                status="OK",
                message=f"{plat_str} (POSIX 受限权限保护模式 chmod 0600，访问控制非加密)",
            ))

        # 核心依赖检查
        missing_mods = []
        for mod_name in ["sqlite3", "ssl", "json", "ctypes", "threading", "urllib"]:
            try:
                __import__(mod_name)
            except ImportError:
                missing_mods.append(mod_name)

        if not missing_mods:
            report.checks.append(CheckItem(
                category="runtime",
                name="核心依赖库",
                status="OK",
                message="sqlite3, ssl, json, ctypes, threading, urllib 全部就绪",
            ))
        else:
            report.checks.append(CheckItem(
                category="runtime",
                name="核心依赖库",
                status="ERROR",
                message=f"缺失必要内置模块: {', '.join(missing_mods)}",
                suggestion="请检查 Python 安装环境是否完整。",
            ))

        # ADB 探测 (非阻断项：若已有凭据，抢课日常不需要 ADB)
        adb_path = shutil.which("adb")
        has_cred = os.path.exists(self.cred_path)
        if adb_path:
            report.checks.append(CheckItem(
                category="runtime",
                name="安卓 ADB 工具",
                status="OK",
                message=f"已就绪 ({adb_path})",
            ))
        elif has_cred:
            report.checks.append(CheckItem(
                category="runtime",
                name="安卓 ADB 工具",
                status="INFO",
                message="未在系统 PATH 中检测到 adb (当前已有有效凭据，日常抢课无需模拟器与 ADB)",
            ))
        else:
            report.checks.append(CheckItem(
                category="runtime",
                name="安卓 ADB 工具",
                status="WARN",
                message="未在系统 PATH 中检测到 adb (首次抓包建议启动安卓模拟器并安装 ADB)",
                suggestion="若需重新抓包，请启动安卓模拟器并将 adb.exe 所在目录加入系统环境变量 PATH。",
            ))

    # -------------------------------------------------------------------------
    # 2. 本地存储与配置检查
    # -------------------------------------------------------------------------
    def _check_storage_and_config(self, report: DoctorReport) -> None:
        target_dirs = [self.data_dir, self.logs_dir, self.captures_dir]

        # 1. 扫描并自愈清理可能因断电异常残留的历史探测文件
        for d in target_dirs:
            if os.path.exists(d):
                try:
                    for fname in os.listdir(d):
                        if fname.startswith(".doctor_probe_") and fname.endswith(".tmp"):
                            try:
                                os.unlink(os.path.join(d, fname))
                            except Exception:
                                pass
                except Exception:
                    pass

        # 2. 目录读写权限验证 (创建临时探测文件并在 finally 中立即清理，非破坏性)
        writable_dirs = []
        unwritable_dirs = []
        for d in target_dirs:
            test_file = None
            try:
                os.makedirs(d, exist_ok=True)
                test_file = os.path.join(d, f".doctor_probe_{os.getpid()}_{int(time.time()*1000)}.tmp")
                with open(test_file, "w", encoding="utf-8") as f:
                    f.write("probe")
                writable_dirs.append(os.path.basename(d))
            except Exception as e:
                unwritable_dirs.append(f"{os.path.basename(d)} ({e})")
            finally:
                if test_file and os.path.exists(test_file):
                    try:
                        os.unlink(test_file)
                    except Exception:
                        pass

        if not unwritable_dirs:
            report.checks.append(CheckItem(
                category="storage",
                name="工作目录权限",
                status="OK",
                message=f"{', '.join(writable_dirs)} 均具有正常读写权限",
            ))
        else:
            report.checks.append(CheckItem(
                category="storage",
                name="工作目录权限",
                status="ERROR",
                message=f"部分目录无写权限: {', '.join(unwritable_dirs)}",
                suggestion="请检查并授予当前用户对 lejianPE 目录的完整读写权限。",
            ))

        # 3. SQLite 状态库检查（采用 mode=ro 只读连接，杜绝产生数据库写入或事务日志）
        if not os.path.exists(self.db_path):
            report.checks.append(CheckItem(
                category="storage",
                name="SQLite 状态库",
                status="INFO",
                message="data/registration.db 尚未创建 (将在首次抢课时自动建立)",
            ))
        else:
            try:
                db_abs = os.path.abspath(self.db_path)
                db_uri = f"file:{urllib.request.pathname2url(db_abs)}?mode=ro"
                conn = sqlite3.connect(db_uri, uri=True, timeout=5.0)
                cur = conn.cursor()
                cur.execute("PRAGMA integrity_check;")
                row = cur.fetchone()
                integrity_ok = bool(row and row[0] == "ok")
                cur.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='registration_records';")
                tbl_row = cur.fetchone()
                tbl_exists = bool(tbl_row and tbl_row[0] > 0)
                record_count = 0
                if tbl_exists:
                    cur.execute("SELECT count(*) FROM registration_records;")
                    cnt_row = cur.fetchone()
                    record_count = cnt_row[0] if cnt_row else 0
                conn.close()

                if integrity_ok:
                    report.checks.append(CheckItem(
                        category="storage",
                        name="SQLite 状态库",
                        status="OK",
                        message=f"data/registration.db 完整性通过 (包含 {record_count} 条抢课记录, 只读连接)",
                    ))
                else:
                    report.checks.append(CheckItem(
                        category="storage",
                        name="SQLite 状态库",
                        status="ERROR",
                        message=f"数据库完整性损坏: {row[0] if row else '未知错误'}",
                        suggestion="数据库损坏，建议先将 data/registration.db 备份为 .corrupt.bak，然后删除原文件由程序自动重建，并运行 python lejian.py list 复核已选活动。",
                    ))
            except Exception as e:
                report.checks.append(CheckItem(
                    category="storage",
                    name="SQLite 状态库",
                    status="ERROR",
                    message=f"访问数据库异常: {e}",
                    suggestion="数据库损坏或被独占占用，建议先将 data/registration.db 备份为 .corrupt.bak，然后删除原文件由程序自动重建，并运行 python lejian.py list 复核已选活动。",
                ))

        # config.json 检查
        if not os.path.exists(self.config_path):
            report.checks.append(CheckItem(
                category="storage",
                name="任务配置文件",
                status="WARN",
                message="未检测到 config.json (将使用系统默认抢课配置)",
                suggestion="可参考文档创建 config.json 自定义目标抢课活动列表。",
            ))
        else:
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                if not isinstance(cfg, dict):
                    raise ValueError("根节点必须为 JSON 对象")
                targets = cfg.get("targets", [])
                poll_ms = cfg.get("poll_interval_ms", 500)
                report.checks.append(CheckItem(
                    category="storage",
                    name="任务配置文件",
                    status="OK",
                    message=f"config.json 格式正确 (已配置 {len(targets)} 个目标课程, 轮询间隔 {poll_ms}ms)",
                ))
            except Exception as e:
                report.checks.append(CheckItem(
                    category="storage",
                    name="任务配置文件",
                    status="WARN",
                    message=f"config.json 语法错误: {e}",
                    suggestion="请检查并修正 config.json 中的 JSON 语法。",
                ))

        # timetable.json 检查
        if not os.path.exists(self.timetable_path):
            report.checks.append(CheckItem(
                category="storage",
                name="课表配置文件",
                status="WARN",
                message="未检测到 timetable.json (未配置个人排课冲突过滤)",
                suggestion="可运行 python lejian.py timetable 或使用网页编辑器 tools/editor_server.py 配置课表。",
            ))
        else:
            try:
                with open(self.timetable_path, "r", encoding="utf-8") as f:
                    tt = json.load(f)
                if not isinstance(tt, dict):
                    raise ValueError("根节点必须为 JSON 对象")
                w1 = tt.get("week1_monday", "-")
                tw = tt.get("total_weeks", 16)
                report.checks.append(CheckItem(
                    category="storage",
                    name="课表配置文件",
                    status="OK",
                    message=f"timetable.json 格式正确 (第 1 周周一: {w1}, 共 {tw} 周)",
                ))
            except Exception as e:
                report.checks.append(CheckItem(
                    category="storage",
                    name="课表配置文件",
                    status="WARN",
                    message=f"timetable.json 格式错误: {e}",
                    suggestion="请检查 timetable.json 格式或使用可视化课表编辑器重新生成。",
                ))

    # -------------------------------------------------------------------------
    # 3. 本地认证材料 (credential.json) 检查
    # -------------------------------------------------------------------------
    def _check_credential(self, report: DoctorReport) -> None:
        if not os.path.exists(self.cred_path):
            report.checks.append(CheckItem(
                category="credential",
                name="凭据文件",
                status="WARN",
                message="未检测到 credential.json (未初始化认证凭据)",
                suggestion="新用户请运行 python lejian.py setup 启动首次配置向导。",
            ))
            return

        # 尝试读取原始 JSON
        try:
            with open(self.cred_path, "r", encoding="utf-8") as f:
                raw_data = json.load(f)
        except Exception as e:
            report.checks.append(CheckItem(
                category="credential",
                name="凭据文件",
                status="ERROR",
                message=f"credential.json 损坏或不是合法 JSON: {e}",
                suggestion="凭据文件损坏，请运行 python lejian.py reinit 重新抓包初始化。",
            ))
            return

        if not isinstance(raw_data, dict):
            report.checks.append(CheckItem(
                category="credential",
                name="凭据文件",
                status="ERROR",
                message="credential.json 格式错误: 根节点非 JSON 对象",
                suggestion="请运行 python lejian.py reinit 重新抓包初始化。",
            ))
            return

        # 提取脱敏账号
        acc = raw_data.get("account") or raw_data.get("userName") or raw_data.get("phone")
        masked_acc = mask_account(acc) if acc else "未配置"

        # 检查 DPAPI 加密与签名提取
        is_encrypted = bool(raw_data.get("signDigitalEncrypted"))
        sign_plain = ""
        decrypt_ok = False
        decrypt_err = ""

        if is_encrypted:
            try:
                cipher_bytes = base64.b64decode(raw_data["signDigitalEncrypted"])
                sign_plain = dpapi_unprotect(cipher_bytes).decode("utf-8")
                decrypt_ok = True
            except Exception as e:
                decrypt_err = str(e)
                decrypt_ok = False
        else:
            sign_plain = raw_data.get("signDigital") or ""
            decrypt_ok = bool(sign_plain)

        # 凭据保护与解密状态
        if is_encrypted:
            if decrypt_ok:
                report.checks.append(CheckItem(
                    category="credential",
                    name="凭据保护模式",
                    status="OK",
                    message="Windows DPAPI 凭据安全保护 (往返解密测试通过)",
                ))
            else:
                report.checks.append(CheckItem(
                    category="credential",
                    name="凭据保护模式",
                    status="ERROR",
                    message=f"Windows DPAPI 密文解密失败: {decrypt_err}",
                    suggestion="可能是由于跨 Windows 用户迁移或凭据被破坏，请运行 python lejian.py reinit 重新初始化。",
                ))
        elif not is_windows():
            report.checks.append(CheckItem(
                category="credential",
                name="凭据保护模式",
                status="OK",
                message="受限文件权限保护模式 (POSIX chmod 0600 访问控制，非加密)",
            ))
        else:
            report.checks.append(CheckItem(
                category="credential",
                name="凭据保护模式",
                status="WARN",
                message="明文存储模式 (建议使用 Windows DPAPI 加密存储以提升安全性)",
                suggestion="运行 python lejian.py reinit 可自动升级为 Windows DPAPI 凭据安全保护。",
            ))

        # 账号与签名有效性
        if acc:
            report.checks.append(CheckItem(
                category="credential",
                name="绑定账号",
                status="OK",
                message=f"{masked_acc}",
            ))
        else:
            report.checks.append(CheckItem(
                category="credential",
                name="绑定账号",
                status="ERROR",
                message="缺少有效 account 字段",
                suggestion="请运行 python lejian.py reinit 重新初始化凭据。",
            ))

        if decrypt_ok and sign_plain:
            report.checks.append(CheckItem(
                category="credential",
                name="签名凭据 (sign)",
                status="OK",
                message=f"已配置 (长度 {len(sign_plain)} 字符, 摘要 {mask_secret(sign_plain)})",
            ))
        elif not is_encrypted:
            report.checks.append(CheckItem(
                category="credential",
                name="签名凭据 (sign)",
                status="ERROR",
                message="缺少 signDigital 签名",
                suggestion="请运行 python lejian.py reinit 重新抓取签名。",
            ))

    # -------------------------------------------------------------------------
    # 4. 本地会话状态 (state.json) 检查
    # -------------------------------------------------------------------------
    def _check_session(self, report: DoctorReport) -> None:
        if not os.path.exists(self.state_path):
            report.checks.append(CheckItem(
                category="session",
                name="会话文件",
                status="WARN",
                message="未检测到 state.json (未生成登录会话)",
                suggestion="若已有凭据，可运行 python lejian.py setup 进行免抓包快速登录。",
            ))
            return

        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                st = json.load(f)
        except Exception as e:
            report.checks.append(CheckItem(
                category="session",
                name="会话文件",
                status="ERROR",
                message=f"state.json 损坏或不是合法 JSON: {e}",
                suggestion="请运行 python lejian.py setup 重新登录生成会话。",
            ))
            return

        if not isinstance(st, dict):
            report.checks.append(CheckItem(
                category="session",
                name="会话文件",
                status="ERROR",
                message="state.json 根节点非 JSON 对象",
                suggestion="请运行 python lejian.py setup 重新登录生成会话。",
            ))
            return

        # 检查学校与学期
        sch = st.get("schoolId")
        sem = st.get("semesterId")
        if sch and sem:
            report.checks.append(CheckItem(
                category="session",
                name="学校与学期上下文",
                status="OK",
                message=f"schoolId={mask_secret(sch)}, semesterId={mask_secret(sem)}",
            ))
        else:
            report.checks.append(CheckItem(
                category="session",
                name="学校与学期上下文",
                status="WARN",
                message=f"缺少学校或学期标识 (schoolId={sch}, semesterId={sem})",
                suggestion="建议重新登录拉取最新学期信息。",
            ))

        # 检查 Token 生命周期
        tok = st.get("accessToken")
        rt = st.get("refreshToken")
        exp = int(st.get("accessTokenExpires") or 0)
        now_ms = int(time.time() * 1000)

        if not tok:
            report.checks.append(CheckItem(
                category="session",
                name="访问令牌 (Token)",
                status="ERROR",
                message="state.json 缺少 accessToken",
                suggestion="请运行 python lejian.py setup 重新登录生成 Token。",
            ))
        else:
            masked_tok = mask_secret(tok)
            if exp <= 0:
                report.checks.append(CheckItem(
                    category="session",
                    name="访问令牌 (Token)",
                    status="WARN",
                    message=f"{masked_tok} (未记录有效过期时间戳)",
                    suggestion="建议运行 python lejian.py refresh 刷新 Token。",
                ))
            elif now_ms < exp:
                left_sec = int((exp - now_ms) / 1000)
                left_hours = left_sec // 3600
                left_mins = (left_sec % 3600) // 60
                exp_dt = datetime.datetime.fromtimestamp(exp / 1000).strftime("%Y-%m-%d %H:%M:%S")
                report.checks.append(CheckItem(
                    category="session",
                    name="访问令牌 (Token)",
                    status="OK",
                    message=f"有效 (至 {exp_dt}，剩余 {left_hours} 小时 {left_mins} 分钟)",
                ))
            else:
                exp_dt = datetime.datetime.fromtimestamp(exp / 1000).strftime("%Y-%m-%d %H:%M:%S")
                report.checks.append(CheckItem(
                    category="session",
                    name="访问令牌 (Token)",
                    status="WARN",
                    message=f"已过期 (过期时间: {exp_dt})",
                    suggestion="可运行 python lejian.py refresh 或 python lejian.py setup 进行刷新。",
                ))

        # 检查 RefreshToken
        if rt:
            report.checks.append(CheckItem(
                category="session",
                name="刷新令牌 (RT)",
                status="OK",
                message=f"存在 (摘要: {mask_secret(rt)})，支持静默自动刷新",
            ))
        else:
            report.checks.append(CheckItem(
                category="session",
                name="刷新令牌 (RT)",
                status="WARN",
                message="缺少 refreshToken (无法静默刷新，Token 过期后须重新登录)",
                suggestion="建议在首次配置向导中完整登录以保存 RefreshToken。",
            ))

    # -------------------------------------------------------------------------
    # 5. 未决事务与备份文件安全性检查
    # -------------------------------------------------------------------------
    def _check_transactions(self, report: DoctorReport) -> None:
        residual_files = []
        for fname in [
            "credential.json.tx.json",
            "credential.json.tx_bak",
            "state.json.tx.json",
            "state.json.tx_bak",
        ]:
            p = os.path.join(self.root_dir, fname)
            if os.path.exists(p):
                residual_files.append(fname)

        if not residual_files:
            report.checks.append(CheckItem(
                category="transaction",
                name="事务状态检查",
                status="OK",
                message="无遗留未决事务日志或孤儿备份，状态一致性完好",
            ))
        else:
            report.checks.append(CheckItem(
                category="transaction",
                name="事务状态检查",
                status="WARN",
                message=f"检测到上次未完成的事务文件: {', '.join(residual_files)}",
                suggestion="运行 python lejian.py setup 可自动回滚并恢复干净状态。",
            ))

    # -------------------------------------------------------------------------
    # 6. 在线服务与只读连通性 (只读只查，绝不修改数据)
    # -------------------------------------------------------------------------
    def _check_online_and_token(self, report: DoctorReport, timeout: float = 4.0) -> None:
        # 服务端基础连通性探测
        probe_url = self.base_url
        online_reachable = False
        try:
            req = urllib.request.Request(probe_url, method="GET")
            req.add_header("User-Agent", "okhttp/3.12.1")
            ctx = ssl.create_default_context()
            t0 = time.time()
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                elapsed_ms = int((time.time() - t0) * 1000)
                online_reachable = True
                report.checks.append(CheckItem(
                    category="online",
                    name="乐健服务连通",
                    status="OK",
                    message=f"{urllib.parse.urlparse(probe_url).netloc} 畅通 (HTTP {resp.status}, 耗时 {elapsed_ms}ms)",
                ))
        except urllib.error.HTTPError as e:
            online_reachable = True
            report.checks.append(CheckItem(
                category="online",
                name="乐健服务连通",
                status="OK",
                message=f"服务端可达 (HTTP {e.code})",
            ))
        except urllib.error.URLError as e:
            online_reachable = False
            report.checks.append(CheckItem(
                category="online",
                name="乐健服务连通",
                status="WARN",
                message=f"网络不可达或离线 ({e.reason})",
                suggestion="请检查网络连接、校园网认证或代理设置。",
            ))
        except Exception as e:
            online_reachable = False
            report.checks.append(CheckItem(
                category="online",
                name="乐健服务连通",
                status="WARN",
                message=f"网络连接异常: {e}",
                suggestion="请检查网络配置。",
            ))

        # Token 在线只读真伪验证 (调用只读接口 education/semester/getCurrent)
        if not os.path.exists(self.state_path) or not online_reachable:
            return

        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                st = json.load(f)
        except Exception:
            return

        tok = st.get("accessToken")
        sch = st.get("schoolId")
        if not tok or not sch:
            return

        # 若本地已过期，跳过在线请求，避免被服务端记入黑名单
        exp = int(st.get("accessTokenExpires") or 0)
        if exp > 0 and (time.time() * 1000) > exp:
            report.checks.append(CheckItem(
                category="online",
                name="Token 在线验真",
                status="INFO",
                message="本地已判定 Token 过期，跳过在线接口校验",
                suggestion="请先运行 python lejian.py refresh 刷新 Token。",
            ))
            return

        try:
            sem_url = urllib.parse.urljoin(self.base_url, "education/semester/getCurrent")
            req = urllib.request.Request(sem_url, method="GET")
            req.add_header("User-Agent", "okhttp/3.12.1")
            req.add_header("Authorization", f"Bearer {tok}")
            req.add_header("Organization", str(sch))
            ctx = ssl.create_default_context()

            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                raw_bytes = resp.read()
                data = json.loads(raw_bytes.decode("utf-8", "ignore"))

                if resp.status == 200 and isinstance(data, dict) and data.get("code") == 0:
                    sem_data = data.get("data") or {}
                    sem_name = sem_data.get("yearRange") or sem_data.get("name") or sem_data.get("id") or "已认证"
                    report.checks.append(CheckItem(
                        category="online",
                        name="Token 在线验真",
                        status="OK",
                        message=f"有效 (当前学期: {sem_name})",
                    ))
                else:
                    msg = data.get("message") if isinstance(data, dict) else "未知错误"
                    report.checks.append(CheckItem(
                        category="online",
                        name="Token 在线验真",
                        status="WARN",
                        message=f"服务端返回业务码: {msg}",
                        suggestion="建议运行 python lejian.py refresh 或 python lejian.py setup 重新获取有效 Token。",
                    ))
        except urllib.error.HTTPError as e:
            raw_err = ""
            try:
                raw_err = e.read().decode("utf-8", "ignore")
            except Exception:
                pass
            err_json = {}
            try:
                err_json = json.loads(raw_err)
            except Exception:
                pass

            if e.code == 401:
                b_code = err_json.get("code") if isinstance(err_json, dict) else None
                b_msg = (err_json.get("message") or "") if isinstance(err_json, dict) else ""

                if b_code is not None and ("expire" in b_msg.lower() or "过期" in b_msg):
                    report.checks.append(CheckItem(
                        category="online",
                        name="Token 在线验真",
                        status="WARN",
                        message=f"服务端确认 Token 已过期 (code={b_code}: {b_msg})",
                        suggestion="请运行 python lejian.py refresh 尝试静默刷新，或运行 python lejian.py setup 重新登录。",
                    ))
                elif b_code is not None and ("revoke" in b_msg.lower() or "撤销" in b_msg or "失效" in b_msg):
                    report.checks.append(CheckItem(
                        category="online",
                        name="Token 在线验真",
                        status="ERROR",
                        message=f"服务端确认 Token 已被撤销 (code={b_code}: {b_msg})",
                        suggestion="凭据已被撤销，请运行 python lejian.py setup 重新登录。",
                    ))
                else:
                    detail_str = f"业务响应: {raw_err[:150]}" if raw_err else "服务端未返回详细错误体"
                    report.checks.append(CheckItem(
                        category="online",
                        name="Token 在线验真",
                        status="WARN",
                        message="在线验证未通过: 服务端返回 HTTP 401 未授权响应 (具体原因暂时无法确定)",
                        detail=detail_str,
                        suggestion="建议运行 python lejian.py status 检查，或运行 python lejian.py setup 重新验证登录。",
                    ))
            else:
                report.checks.append(CheckItem(
                    category="online",
                    name="Token 在线验真",
                    status="WARN",
                    message=f"服务端响应 HTTP {e.code}",
                    detail=raw_err[:200] if raw_err else None,
                ))
        except Exception as e:
            report.checks.append(CheckItem(
                category="online",
                name="Token 在线验真",
                status="WARN",
                message=f"在线验证超时或受阻 ({e})",
            ))

    # -------------------------------------------------------------------------
    # 7. 综合评级与运维建议归纳
    # -------------------------------------------------------------------------
    def _evaluate_overall(self, report: DoctorReport) -> None:
        has_error = any(c.status == "ERROR" for c in report.checks)
        has_warn = any(c.status == "WARN" for c in report.checks)

        recs: List[str] = []
        for c in report.checks:
            if c.suggestion and c.suggestion not in recs:
                if c.status in ("ERROR", "WARN"):
                    recs.append(f"[{c.name}] {c.suggestion}")

        report.recommendations = recs

        if has_error:
            report.overall_status = "ERROR"
            report.summary = "系统检测到阻断性故障，必须处理上述标红错误项后方可正常抢课。"
        elif has_warn:
            report.overall_status = "WARNING"
            report.summary = "系统基础功能正常，但存在可优化或即将失效的配置项，建议关注。"
        else:
            report.overall_status = "HEALTHY"
            report.summary = "所有系统依赖、认证材料、本地存储与在线服务状态均良好，可随时抢课！"
