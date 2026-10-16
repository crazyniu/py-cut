"""pycut —— 短视频智能处理工具箱。

统一入口，所有功能（字幕擦除、去重、混剪）都以子命令形式挂载到本包。
长期维护项目：逻辑按职责拆分到 pycut.core 各模块，命令编排在 pycut.commands。
"""
from __future__ import annotations

__version__ = "0.1.0"
