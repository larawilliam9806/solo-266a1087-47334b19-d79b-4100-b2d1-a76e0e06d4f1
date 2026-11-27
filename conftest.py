"""Pytest 引导：保证无论从哪个目录、用 ``pytest`` 还是 ``python -m pytest``
启动，都能导入项目根目录下的 ``app`` 包。
"""
import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
