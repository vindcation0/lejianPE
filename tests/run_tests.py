"""
lejianPE 统一自动化测试启动入口 (统一本地与 CI)
==============================================
执行流程：
1. 安装全局测试网络隔离屏障 (Network Guard)
2. 加载全部测试套件 (tests/test_*.py)
3. 执行全量测试并输出详细日志
4. 返回退出码 (0: 全部通过; 1: 存在失败或错误)
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tests.network_guard import install_network_guard


def run_all_tests() -> int:
    # 1. 确保在加载任何测试模块之前安装全局网络隔离屏障
    install_network_guard()

    # 2. 发现并加载所有测试模块
    tests_dir = os.path.join(ROOT, "tests")
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=tests_dir, pattern="test_*.py")

    # 3. 运行测试
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    # 4. 根据测试结果返回退出码
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(run_all_tests())
