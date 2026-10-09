"""pycut 命令行入口（CLI 路由）。

TUI 友好设计：所有子命令通过 COMMANDS 注册表集中管理，每个命令模块暴露
register(subparsers) 与 run(args)。以后接 TUI 时，只需复用 COMMANDS 与 run，
无需改动各命令实现。
"""
from __future__ import annotations

import argparse
import sys

from . import __version__
from .commands import cover_cmd, slice_cmd
from .core.utils import log

# 命令注册表：名称 -> 命令模块（含 register / run）。新增功能在此登记即可。
COMMANDS = {
    "slice": slice_cmd,
    "cover": cover_cmd,
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pycut",
        description="短视频智能处理工具箱（切片 / 混剪 / 转场 / 查重…）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--version", action="version", version=f"pycut {__version__}")
    sub = p.add_subparsers(dest="command", metavar="<command>")
    for name, mod in COMMANDS.items():
        mod.register(sub)
    return p


def list_commands() -> list[str]:
    """暴露给 TUI / 帮助，列出可用命令。"""
    return list(COMMANDS.keys())


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    # TUI 启动器：显式 `tui` 子命令，或无子命令且为交互终端时进入
    if argv and argv[0] == "tui":
        from . import tui  # noqa: PLC0415
        return tui.run()
    if not argv and sys.stdin.isatty():
        from . import tui  # noqa: PLC0415
        return tui.run()

    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "command", None):
        parser.print_help()
        return 0

    func = getattr(args, "func", None)
    if func is None:
        log(f"[错误] 命令 {args.command} 未绑定处理函数")
        return 2
    return func(args)


if __name__ == "__main__":
    sys.exit(main())
