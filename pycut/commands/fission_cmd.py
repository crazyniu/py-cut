"""Tail-only variants of supplied videos, with per-command counts and progress."""
from pathlib import Path

from .. import config
from ..core.fission import copy_with_random_tail
from ..core.output_paths import sibling, without_outputs
from ..core.progress import progress
from ..core.runtime import install_signal_handlers, reset_stop
from ..core.scan import VIDEO_EXTS, list_videos_recursive
from ..core.utils import log


def register(subparsers) -> None:
    parser = subparsers.add_parser("fission", help="抖音裂变：复制视频并在尾部追加随机字节")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--file", help="单个原视频路径")
    source.add_argument("--src-dir", help="递归读取原视频目录（默认读取配置）")
    parser.add_argument("--copies", type=int, default=1, help="每个视频生成数量（1～99）")
    parser.add_argument("--out-dir", default="", help="输出目录（默认原视频目录同级的抖音裂变）")
    parser.set_defaults(func=run)


def run(args) -> int:
    if not 1 <= args.copies <= 99:
        log("[错误] --copies 必须在 1～99 之间")
        return 2
    install_signal_handlers()
    reset_stop()
    completed = 0
    total = 0
    try:
        if args.file:
            source = Path(args.file).expanduser().resolve()
            if not source.is_file() or source.suffix.lower() not in VIDEO_EXTS:
                raise ValueError(f"原视频不存在或格式不支持：{source}")
            root, files = source.parent, [source]
        else:
            root = Path(args.src_dir or config.SRC_DIR).expanduser().resolve()
            files = list_videos_recursive(root, max_depth=None, raise_on_error=True)
        output = Path(args.out_dir).expanduser().resolve() if args.out_dir else sibling(root, "抖音裂变")
        files = without_outputs(files, root, "抖音裂变", output)
        if not files:
            raise ValueError("原视频目录及其子目录没有可处理的视频")
        total = len(files) * args.copies
        progress("抖音裂变", 0, total, success=0, output=str(output))
        for source in files:
            destination = output / source.relative_to(root).parent
            for _ in range(args.copies):
                target = copy_with_random_tail(source, destination)
                completed += 1
                log(f"[成功] {target}")
                progress("抖音裂变", completed, total, success=completed,
                         detail=str(target), output=str(output))
        log(f"[完成] 抖音裂变成功，共生成 {completed} 个视频\n输出目录：{output}")
        return 0
    except InterruptedError as exc:
        log(f"[中断] {exc}")
        return 130
    except (ValueError, OSError) as exc:
        log(f"[错误] {exc}")
        progress("抖音裂变", completed, total, success=completed, failed=1, detail=str(exc))
        return 2
