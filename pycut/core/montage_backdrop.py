"""Place the normalized body over independently selected moving library footage.

The body stays complete inside an inset. Bound the looping backdrop to the body's
frame budget, keep both silent, and atomically replace only a validated composition.
"""
from __future__ import annotations

import os
from pathlib import Path
import random
import tempfile

from .. import config
from .dedup import montage_variation, _video_duration
from .dedup_presets import derive_seed
from .runtime import check_stop
from .utils import log, run_cmd


def compose(body: Path, pool: list, *, seed: int, duration: float,
            excluded: set[Path], crf: str) -> dict:
    rng = random.Random(derive_seed(seed, "backdrop"))
    choices = [item for item in pool if item.path not in excluded] or pool
    source = rng.choice(choices)
    offset = rng.uniform(0, max(0, source.duration - 1 / config.MONTAGE_FPS))
    width, height = config.MONTAGE_CANVAS
    inset_width = int(width * rng.uniform(*config.MONTAGE_INSET_WIDTH_RANGE)) // 2 * 2
    inset_height = int(height * inset_width / width) // 2 * 2
    variation_seed = derive_seed(seed, "backdrop", source.path)
    params, variation = montage_variation(width, height, variation_seed)
    frames = round(duration * config.MONTAGE_FPS)
    fps = config.MONTAGE_FPS
    # Decode surplus real backdrop frames so framesync cannot end one frame early.
    # The output cap alone defines the body budget; never repeat a foreground tail.
    graph = (
        f"[1:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},setsar=1,{variation},setsar=1,gblur=sigma={config.MONTAGE_BACKDROP_BLUR},"
        f"fps={fps},trim=end_frame={frames + 2},setpts=N/({fps}*TB)[back];"
        f"[0:v]scale={inset_width}:{inset_height},setsar=1,setpts=N/({fps}*TB)[front];"
        "[back][front]overlay=(W-w)/2:(H-h)/2:shortest=0:repeatlast=0:eof_action=pass,setsar=1[out]"
    )
    fd, name = tempfile.mkstemp(prefix="backdrop_", suffix=".mp4", dir=body.parent)
    os.close(fd)
    target = Path(name)
    command = [config.FFMPEG, "-v", "error", "-y", "-nostdin",
               "-filter_complex_threads", "2", "-reinit_filter", "0",
               "-threads", "2", "-i", str(body),
               "-stream_loop", "-1", "-ss", f"{offset:.6f}", "-threads", "2",
               "-i", str(source.path), "-filter_complex", graph,
               "-map", "[out]", "-an", "-frames:v", str(frames),
               "-fps_mode", "cfr", "-c:v", "libx264", "-preset", "veryfast",
               "-crf", str(crf), "-pix_fmt", "yuv420p", "-threads", "2", str(target)]
    try:
        check_stop()
        log(f"  [动态底片] {source.path.name} · 起点 {offset:.3f}s · 主画面 {inset_width}x{inset_height}")
        rc, _, error = run_cmd(command, timeout=3600)
        check_stop()
        if rc or not target.is_file() or target.stat().st_size < 1024:
            raise RuntimeError(f"动态底片合成失败：{error[-2000:]}")
        if abs(_video_duration(target) - frames / fps) > .5 / fps:
            raise RuntimeError("动态底片合成时长校验失败")
        os.replace(target, body)
        return {"source": str(source.path), "offset": offset,
                "inset": [inset_width, inset_height], "seed": variation_seed,
                "params": params}
    finally:
        target.unlink(missing_ok=True)
