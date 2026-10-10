"""Optional libvmaf assessment for already-aligned, authorized video pairs.

This module is intentionally independent from dedup's existing pHash/PSNR pipeline.
A caller must opt in and enforce its own calibrated acceptance threshold.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import tempfile

from . import ffmpeg


class VMAFError(RuntimeError):
    """A score cannot be established; never interpret this as a pass."""


@dataclass(frozen=True)
class VMAFResult:
    mean: float
    frames: int
    model: str


def libvmaf_available(executable: str | None = None) -> bool:
    """Probe the configured ffmpeg binary rather than assuming a build feature."""
    cmd = executable or ffmpeg.FFMPEG
    try:
        result = subprocess.run(
            [cmd, "-hide_banner", "-filters"], capture_output=True,
            text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and any(
        line.split()[-2:-1] == ["libvmaf"] or
        (len(line.split()) >= 2 and line.split()[1] == "libvmaf")
        for line in result.stdout.splitlines()
    )


def _read_score(path: Path) -> VMAFResult:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        frames = data["frames"]
        pooled = data["pooled_metrics"]["vmaf"]["mean"]
        model = data.get("version", "unknown")
        value = float(pooled)
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        raise VMAFError("libvmaf produced an invalid or missing JSON score") from exc
    if not isinstance(frames, list) or not frames or not (0 <= value <= 100):
        raise VMAFError("libvmaf returned an invalid score or zero frames")
    return VMAFResult(mean=value, frames=len(frames), model=str(model))


def assess(reference: Path, distorted: Path, *, executable: str | None = None,
           timeout: int = 600) -> VMAFResult:
    """Measure only strictly aligned videos; no silent scaling or frame matching.

    libvmaf inputs: [0:v] distorted, [1:v] reference. The caller is responsible
    for a calibrated threshold; this function does not assert quality.
    """
    exe = executable or ffmpeg.FFMPEG
    if not libvmaf_available(exe):
        raise VMAFError("configured ffmpeg does not provide libvmaf")
    ref = ffmpeg.probe_video(Path(reference))
    dist = ffmpeg.probe_video(Path(distorted))
    if ref is None or dist is None:
        raise VMAFError("cannot probe both video streams")
    if (ref.width, ref.height) != (dist.width, dist.height):
        raise VMAFError("videos have different dimensions; alignment required")
    if abs(ref.duration - dist.duration) > 0.05:
        raise VMAFError("videos have different durations; alignment required")
    with tempfile.TemporaryDirectory(prefix="pycut-vmaf-") as folder:
        output = Path(folder) / "score.json"
        graph = f"[0:v][1:v]libvmaf=log_fmt=json:log_path={output}"
        try:
            run = subprocess.run(
                [exe, "-hide_banner", "-nostdin", "-v", "error",
                 "-i", str(distorted), "-i", str(reference),
                 "-lavfi", graph, "-f", "null", "-"],
                capture_output=True, text=True, timeout=timeout, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise VMAFError(f"libvmaf execution failed: {type(exc).__name__}") from exc
        if run.returncode:
            raise VMAFError(f"libvmaf exited with status {run.returncode}: {run.stderr[-400:]}")
        return _read_score(output)
