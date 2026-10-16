"""pycut 命令行入口（CLI 路由）。

TUI 友好设计：所有子命令通过 COMMANDS 注册表集中管理，每个命令模块暴露
register(subparsers) 与 run(args)。以后接 TUI 时，只需复用 COMMANDS 与 run，
无需改动各命令实现。
"""
from __future__ import annotations

import argparse
import sys

from . import __version__
from .commands import cover_cmd, dedup_cmd, montage_cmd
from .core.utils import log

# 命令注册表：名称 -> 命令模块（含 register / run）。新增功能在此登记即可。
COMMANDS = {
    "cover": cover_cmd,
    "dedup": dedup_cmd,
    "montage": montage_cmd,
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pycut",
        description="短视频处理工具箱（字幕擦除 / 去重 / 混剪；切片由用户提供）",
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

    # Validate only configured libraries this invocation actually uses. Explicit
    # arguments override .env, and disabled optional libraries never block tasks.
    from . import config, settings
    keys = []
    use_configured_clips = args.command in ('cover', 'dedup') and not (args.file or args.src_dir)
    if use_configured_clips:
        keys.append('PYCUT_CLIP_DIR')
    if args.command == 'montage':
        probe = bool(args.probe)
        if not args.source and not probe:
            keys.append('PYCUT_REFERENCE_DIR')
        if args.clip_dir is None and (probe or args.step != 'a'):
            keys.append('PYCUT_CLIP_DIR')
        if not probe and args.step != 'a':
            if not args.no_pip and args.dedup_dir is None:
                keys.append('PYCUT_PIP_DIR')
            if args.intro_dir is None:
                keys.append('PYCUT_INTRO_DIR')
        if not args.out_dir:
            keys.append('PYCUT_OUTPUT_DIR')
    errors = settings.problems(settings.effective(), keys=keys)
    if errors:
        log("[配置未完成] " + "；".join(errors))
        log("请启动 TUI，复制顶部设置地址到浏览器配置；也可编辑项目根目录 .env")
        return 2
    if use_configured_clips:
        args.src_dir = settings.encoded_directories(config.CLIP_DIRS)

    func = getattr(args, "func", None)
    if func is None:
        log(f"[错误] 命令 {args.command} 未绑定处理函数")
        return 2
    return func(args)


if __name__ == "__main__":
    sys.exit(main())
