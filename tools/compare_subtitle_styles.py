#!/usr/bin/env python3
"""Plan reproducible outline/box ASS comparisons for owned or authorized media.

Default mode writes ASS and a JSON command plan only. --execute --authorized
explicitly enables encoding into an isolated output directory. Failed renders
never replace existing results with incomplete media."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from pycut import config  # noqa: E402
from pycut.core import montage_subtitles as subtitles  # noqa: E402
from pycut.core.ffmpeg import FFMPEG, probe_video  # noqa: E402
from pycut.core.utils import run_cmd  # noqa: E402

_STYLE_MODES = (("outline", 1), ("box", 3))
_SNAPSHOT_FRACTIONS = (0.25, 0.50, 0.75)


def _canvas(text: str) -> tuple[int, int]:
    value = text.lower().replace("×", "x")
    try:
        width, height = (int(x) for x in value.split("x"))
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError("画布应为正整数 WIDTHxHEIGHT") from None
    if not 1 <= width <= 16384 or not 1 <= height <= 16384:
        raise argparse.ArgumentTypeError("画布宽高应在 1 到 16384 范围内")
    return width, height


def _with_source_audio(command: list[str]) -> list[str]:
    """Reuse the production subtitle command while preserving authorized source audio."""
    cmd = list(command)
    try:
        index = cmd.index("-an")
    except ValueError:
        raise ValueError("字幕命令缺少预期的 -an 标志；不能安全配置音轨") from None
    cmd[index:index + 1] = ["-map", "0:a?", "-c:a", "copy"]
    return cmd


def make_plan(video: Path, srt: Path, output_dir: Path,
              canvas: tuple[int, int], font: str) -> dict:
    """Write ASS styles and return FFmpeg commands without encoding or modifying the source."""
    video, srt, output_dir = Path(video), Path(srt), Path(output_dir)
    if not video.is_file():
        raise ValueError(f"授权源视频不存在: {video}")
    if not srt.is_file():
        raise ValueError(f"SRT 文件不存在: {srt}")
    cues = subtitles.parse_srt(srt)
    if not cues:
        raise ValueError(f"SRT 中没有合法的字幕时间段: {srt}")
    if not font:
        font = subtitles.pick_font()

    targets = [(name, output_dir / f"subtitle_style_{name}.mp4")
               for name, _style in _STYLE_MODES]
    if any(video.resolve() == target.resolve() for _name, target in targets):
        raise ValueError("输出文件不得指向输入视频")
    output_dir.mkdir(parents=True, exist_ok=True)

    jobs: list[dict] = []
    for (name, border_style), (_label, target) in zip(_STYLE_MODES, targets):
        ass_path, style = subtitles.build_ass(
            cues, canvas=canvas, font_family=font, border_style=border_style,
            ass_dir=output_dir / "ass",
        )
        command = subtitles.build_subtitle_cmd(video, Path(ass_path), target)
        if not command:
            raise RuntimeError("未能生成字幕烧录命令")
        jobs.append({
            "name": name,
            "border_style": border_style,
            "ass": str(ass_path),
            "output": str(target),
            "style": style,
            "argv": _with_source_audio(command),
        })
    return {
        "source": str(video),
        "srt": str(srt),
        "canvas": list(canvas),
        "font": font,
        "subtitle_count": len(cues),
        "jobs": jobs,
        "status": "planned",
        "visual_review": "required: bright/dark/busy/moving backgrounds",
    }


def _run_checked(command: list[str], description: str, timeout: int = 900) -> None:
    rc, _stdout, stderr = run_cmd(command, timeout=timeout)
    if rc:
        raise RuntimeError(f"{description} 失败 (exit={rc}): {stderr[-800:]}")


def execute_plan(plan: dict, *, overwrite: bool = False) -> dict:
    """Publish only validated videos and extract snapshots from those actual outputs."""
    source = Path(plan["source"])
    source_info = probe_video(source)
    if source_info is None:
        raise RuntimeError(f"FFprobe 无法读取授权源视频: {source}")
    targets = [Path(job["output"]) for job in plan["jobs"]]
    if any(t.exists() for t in targets) and not overwrite:
        raise FileExistsError("对照视频已存在；需显式 --overwrite 才允许替换")
    if any(t.resolve() == source.resolve() for t in targets):
        raise ValueError("禁止覆盖授权源视频")

    summary = []
    for job in plan["jobs"]:
        target = Path(job["output"])
        staging = subtitles.subtitle_tmp_path(target)
        try:
            _run_checked(job["argv"], f"{job['name']} 字幕烧录")
            if not staging.is_file() or staging.stat().st_size <= 0:
                raise RuntimeError(f"{job['name']} 编码器未生成有效暂存成片")
            result = probe_video(staging)
            if (result is None or result.width != source_info.width
                    or result.height != source_info.height
                    or result.has_audio != source_info.has_audio):
                raise RuntimeError(
                    f"{job['name']} 成片尺寸/视频流/原声存在性与输入不一致"
                )
            if result.duration <= 0:
                raise RuntimeError(f"{job['name']} 输出没有有效视频时长")
            os.replace(staging, target)
            captures = []
            for index, fraction in enumerate(_SNAPSHOT_FRACTIONS, 1):
                frame = target.with_name(
                    f"{target.stem}_snapshot_{index}.png"
                )
                _run_checked(
                    [FFMPEG, "-y", "-loglevel", "error", "-nostdin",
                     "-ss", f"{result.duration * fraction:.3f}",
                     "-i", str(target), "-frames:v", "1", str(frame)],
                    f"{job['name']} 截帧 {index}", timeout=120,
                )
                if not frame.is_file() or frame.stat().st_size <= 0:
                    raise RuntimeError(f"{job['name']} 截帧 {index} 缺失")
                captures.append(str(frame))
            summary.append({
                "name": job["name"],
                "output": str(target),
                "duration": result.duration,
                "width": result.width,
                "height": result.height,
                "audio_present": result.has_audio,
                "snapshots": captures,
            })
        finally:
            staging.unlink(missing_ok=True)
    plan["status"] = "rendered_pending_visual_review"
    plan["source_duration"] = source_info.duration
    plan["results"] = summary
    return plan


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="比较混剪 ASS 底框/描边两种样式（仅用户自有/授权素材）"
    )
    parser.add_argument("--video", type=Path, required=True, help="合法可访问的 MP4")
    parser.add_argument("--srt", type=Path, required=True, help="同一视频的 SRT")
    parser.add_argument("--output-dir", type=Path, required=True, help="独立输出目录")
    parser.add_argument("--canvas", type=_canvas,
                        default=tuple(config.MONTAGE_CANVAS),
                        help="仅规划时的画布；执行时自动采用源视频实际尺寸")
    parser.add_argument("--font", default="", help="可选字体族；空则自动选择已安装的中文字体")
    parser.add_argument("--execute", action="store_true",
                        help="实际编码两段对照视频并抽帧；默认只生成计划")
    parser.add_argument("--authorized", action="store_true",
                        help="确认对视频和 SRT 拥有使用权限（--execute 时必填）")
    parser.add_argument("--overwrite", action="store_true",
                        help="允许替换已有对照视频（永不覆盖输入）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.execute and not args.authorized:
        print("[错误] 实际执行必须提供 --authorized 确认有权处理素材", file=sys.stderr)
        return 2
    try:
        canvas = args.canvas
        if args.execute:
            info = probe_video(args.video)
            if info is None:
                raise RuntimeError("必须先由 FFprobe 探测真实视频规格")
            canvas = (info.width, info.height)
        plan = make_plan(args.video, args.srt, args.output_dir, canvas, args.font)
        if args.execute:
            execute_plan(plan, overwrite=args.overwrite)
        report = args.output_dir / "subtitle_style_comparison.json"
        report.write_text(json.dumps(plan, ensure_ascii=False, indent=2),
                          encoding="utf-8")
        print(f"[字幕样式对照] {plan['status']} · {report}")
        return 0
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"[字幕样式对照失败] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
