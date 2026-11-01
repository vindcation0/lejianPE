"""
lejianPE 自动化测试包
=====================
初始化时自动加载全局网络隔离屏障，确保所有单测完全离线执行。
"""
from tests.network_guard import install_network_guard

install_network_guard()
