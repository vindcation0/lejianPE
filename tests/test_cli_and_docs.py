"""
乐健体育 · CLI 命令、菜单路由与文档一致性自动化测试套件 (C01 ~ C07).
========================================================================
覆盖执行计划 M5 (第五阶段) 验收要求：
1. 验证 README.md 所列出的全部命令行子命令均已在 lejian.py 正确注册且可执行。
2. 验证 menu.py 中全部菜单动作项与 README 界面说明完全一致。
3. 验证端到端各子命令的异常兜底与路由完整性。
"""
import io
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import lejian
import menu


class CliAndDocsTestSuite(unittest.TestCase):
    """M5 CLI 命令与文档一致性测试"""

    def test_C01_unknown_command_fallback_to_status(self):
        """C01: 未知命令或空命令时安全回退至 status 而非抛出异常崩溃"""
        with patch.object(sys, "argv", ["lejian.py", "unknown_cmd_abc"]):
            with patch("lejian.cmd_status") as mock_status:
                lejian.main()
                mock_status.assert_called_once()

    def test_C02_setup_and_wizard_routing(self):
        """C02: setup 与 wizard 命令均路由至 cmd_setup"""
        for cmd_name in ["setup", "wizard"]:
            with patch.object(sys, "argv", ["lejian.py", cmd_name]):
                with patch("lejian.cmd_setup") as mock_setup:
                    lejian.main()
                    mock_setup.assert_called_once()

    def test_C03_doctor_and_flag_routing(self):
        """C03: doctor 与 --doctor 命令均路由至 cmd_doctor"""
        for cmd_name in ["doctor", "--doctor"]:
            with patch.object(sys, "argv", ["lejian.py", cmd_name]):
                with patch("lejian.cmd_doctor") as mock_doc:
                    lejian.main()
                    mock_doc.assert_called_once()

    def test_C04_reinit_routing(self):
        """C04: reinit 命令路由至 cmd_reinit"""
        with patch.object(sys, "argv", ["lejian.py", "reinit"]):
            with patch("lejian.cmd_reinit") as mock_reinit:
                lejian.main()
                mock_reinit.assert_called_once()

    def test_C05_login_routing(self):
        """C05: login 命令路由至 cmd_login"""
        with patch.object(sys, "argv", ["lejian.py", "login"]):
            with patch("lejian.cmd_login") as mock_login:
                lejian.main()
                mock_login.assert_called_once()

    def test_C06_menu_actions_comprehensive(self):
        """C06: menu.py ACTIONS 包含全部 14 个核心功能动作项"""
        keys = [item[0] for item in menu.ACTIONS]
        expected_keys = ["1", "2", "3", "4", "5", "6", "t", "7", "8", "9", "b", "s", "d", "a"]
        for ek in expected_keys:
            self.assertIn(ek, keys, f"menu.py 缺失动作项 [{ek}]")

    def test_C07_readme_documents_all_commands_and_keys(self):
        """C07: 校验 README.md 中完整包含所有菜单键与 CLI 命令说明"""
        readme_path = os.path.join(ROOT, "README.md")
        self.assertTrue(os.path.exists(readme_path), "README.md 文件必须存在")
        with open(readme_path, "r", encoding="utf-8") as f:
            content = f.read()

        # 校验菜单项
        for key in ["[1]", "[2]", "[3]", "[4]", "[5]", "[6]", "[t]", "[7]", "[8]", "[9]", "[b]", "[s]", "[d]", "[a]"]:
            self.assertIn(key, content, f"README.md 缺少对菜单项 {key} 的说明")

        # 校验 CLI 子命令
        for cmd in ["lejian.py setup", "lejian.py login", "lejian.py reinit", "lejian.py doctor", "lejian.py status"]:
            self.assertIn(cmd, content, f"README.md 缺少对子命令 {cmd} 的说明")

        # 校验关键故障码说明
        self.assertIn("50004", content)
        self.assertIn("401", content)
        self.assertIn("429", content)


if __name__ == "__main__":
    unittest.main()
