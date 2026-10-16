#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pycut 统一入口（所有功能的命令行入口，后续演进为 TUI）。

用法：
    ./run.sh                    # 进入 TUI（字幕擦除 / 去重 / 混剪）
    ./run.sh cover               # 读取用户提供的切片
    ./run.sh montage --source "话术/a/1.mp4"

日常使用 ./run.sh，自动检查解释器、依赖和 FFmpeg；Windows 使用 run.cmd。
主备网关和素材目录在 .env / 本机配置页设置。
"""
import sys

from pycut.cli import main

if __name__ == "__main__":
    sys.exit(main())
