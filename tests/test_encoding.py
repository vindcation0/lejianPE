"""
控制台编码容错与安全降级专项测试套件 (E01 ~ E04)。
=====================================================
验证 _safe_print 在 Windows cp1252/cp437、缺失 encoding 属性、
以及底层写入二次异常时的完整鲁棒性。
"""
import sys
import unittest
from unittest.mock import patch

from lejian import _safe_print as lejian_safe_print
from timetable.compatibility import _safe_print as compat_safe_print


class MockStream:
    def __init__(self, encoding="utf-8"):
        self.encoding = encoding
        self.data = []

    def write(self, s):
        self.data.append(s)

    def flush(self):
        pass

    def getvalue(self):
        return "".join(self.data)


class FragileStream:
    """模拟在降级输出时再次出现异常的流"""
    def __init__(self, encoding="ascii"):
        self.encoding = encoding

    def write(self, s):
        raise OSError("模拟底层管道断开或致命写入故障")

    def flush(self):
        pass


class EncodingTestSuite(unittest.TestCase):
    def test_E01_utf8_normal_print(self):
        """E01: UTF-8 环境下中文字符正常输出"""
        buf = MockStream(encoding="utf-8")
        with patch("sys.stdout", buf):
            compat_safe_print("旧版课表兼容正常中文测试")
            lejian_safe_print("主程序日志正常中文测试")
        out = buf.getvalue()
        self.assertIn("旧版课表兼容正常中文测试", out)
        self.assertIn("主程序日志正常中文测试", out)

    def test_E02_cp1252_encoding_fallback(self):
        """E02: cp1252 编码不支持中文时，自动捕获 UnicodeEncodeError 并执行降级替换输出"""
        records = []

        class CP1252MockStream:
            def __init__(self):
                self.encoding = "cp1252"

            def write(self, s):
                if any(ord(c) > 255 for c in s):
                    raise UnicodeEncodeError("charmap", s, 0, len(s), "character maps to <undefined>")
                records.append(s)

            def flush(self):
                pass

        stream = CP1252MockStream()
        with patch("sys.stdout", stream):
            # 两个模块的 _safe_print 均不能抛出 NameError 或 UnicodeEncodeError
            compat_safe_print("课表兼容: 包含中文")
            lejian_safe_print("日志: 包含中文")

        # 降级后的输出应该记录了内容（中文字符被替换为 ?）
        combined = "".join(records)
        self.assertTrue(len(combined) > 0)
        self.assertIn("?", combined, "降级输出应包含替换字符 '?'")

    def test_E03_stdout_missing_encoding_attribute(self):
        """E03: stdout 缺失 encoding 属性或为 None 时，默认回退至 utf-8，不抛异常"""
        records = []

        class NoEncodingStream:
            def write(self, s):
                records.append(s)

            def flush(self):
                pass

        stream = NoEncodingStream()
        with patch("sys.stdout", stream):
            compat_safe_print("测试无 encoding 属性")
            lejian_safe_print("测试无 encoding 属性")

        self.assertTrue(len(records) > 0)

    def test_E04_secondary_exception_never_crashes(self):
        """E04: 降级分支内部再次抛出异常（如底层管道断开），静默保护不使程序崩溃"""
        fragile = FragileStream()
        with patch("sys.stdout", fragile):
            try:
                compat_safe_print("极端异常测试")
                lejian_safe_print("极端异常测试")
            except Exception as e:
                self.fail(f"_safe_print 严禁向外抛出任何异常，实际抛出: {e}")


if __name__ == "__main__":
    unittest.main()
