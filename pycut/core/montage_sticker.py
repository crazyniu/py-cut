"""Static sticker overlays with bounded opacity and feathered edges.

Scale the image, convert to RGBA, apply alpha, then overlay at segment level.
Average the horizontal/vertical feather weights; their product hides the sticker.
A single image frame is repeated by overlay; never loop the image input forever."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .. import config
from .utils import log

# Read original image alpha after conversion to RGBA.
_ALPHA_VARS = "alpha(X,Y)"


def sticker_chain(img: Path, canvas: tuple[int, int],
                  scale_ratio: float = 0.28, margin_ratio: float = 0.04,
                  opacity: float = 0.55, pos: str = "br",
                  fps: int | None = None, video_label: str = "0:v") -> list[str] | None:
    """Return [filter graph, output label], or None for invalid image/canvas inputs.

    scale_ratio and margin_ratio use canvas width. opacity caps source alpha;
    pos selects tl/tr/bl/br. video_label identifies the main video stream."""
    img = Path(img)
    if not img.is_file():
        return None
    w, h = canvas
    if w <= 0 or h <= 0:
        return None
    opacity = max(0.05, min(1.0, float(opacity)))

    sw = max(16, int(round(w * float(scale_ratio))))
    margin = max(0, int(round(w * float(margin_ratio))))

    if pos == "tl":
        off = f"{margin}:{margin}"
    elif pos == "tr":
        off = f"W-w-{margin}:{margin}"
    elif pos == "bl":
        off = f"{margin}:H-h-{margin}"
    else:
        off = f"W-w-{margin}:H-h-{margin}"

    # Average axis feather weights; multiplying them makes too much of the sticker transparent.
    fade = max(8, int(sw * 0.45))
    ex = f"min(1\\,min(X\\,W-1-X)/{fade})"
    ey = f"min(1\\,min(Y\\,H-1-Y)/{fade})"
    fw = f"({ex}+{ey})/2"
    # Use the image as input 1 and overlay it onto video_label; no unused split pads.
    ch = (
        f"[1:v]scale={sw}:-2,format=rgba,"
        f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':"
        f"a='{_ALPHA_VARS}*{opacity}*({fw})'[stk]"
    )
    chain = (
        f"{ch};"
        f"[{video_label}][stk]overlay={off}:format=auto:shortest=0[vstk]"
    )
    return [chain, "vstk"]


def build_sticker_cmd(video: Path, stickers: list[Path], out: Path,
                      canvas: tuple[int, int], crf: str = "20",
                      fps: int | None = None, scale_ratio: float = 0.28,
                      margin_ratio: float = 0.04, opacity: float = 0.55,
                      seed: int = 0, chance: float = 1.0) -> tuple[list[str] | None, Path | None]:
    """Return (FFmpeg argv, selected image), or (None, None) when no image is available.

    Apply the chance gate, then select an image and position from the seeded RNG."""
    if not stickers or chance <= 0:
        return None, None
    import random
    rng = random.Random(seed)
    if rng.random() > chance:
        return None, None
    img = rng.choice(stickers)
    part = sticker_chain(img, canvas, scale_ratio, margin_ratio, opacity,
                         pos=rng.choice(["br", "bl", "tl"]), fps=fps)
    if part is None:
        return None, None
    chain, label = part
    tmp = out.with_name(out.stem + f".stk_{os.getpid()}{out.suffix}")
    cmd = [
        config.FFMPEG,
        "-y", "-loglevel", "error", "-nostdin",
        "-i", str(video),
        "-i", str(img),          # A single image frame repeats through overlay; -loop 1 would make the input unbounded.

        "-filter_complex", chain,
        "-map", f"[{label}]",
        "-an",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
        "-pix_fmt", "yuv420p", "-fps_mode", "cfr",
        "-movflags", "+faststart",
        str(tmp),                # The caller validates this temporary output before atomic replacement.
    ]
    return cmd, img


def apply_sticker_to_file(video: Path, out: Path, canvas: tuple[int, int],
                          crf: str = "20", fps: int | None = None,
                          scale_ratio: float = 0.28,
                          margin_ratio: float = 0.04, opacity: float = 0.55,
                          seed: int = 0, chance: float = 1.0,
                          sticker: Path | None = None) -> bool:
    """Apply the selected sticker atomically, using the explicit image pool first."""
    stickers = [Path(sticker)] if sticker is not None else _dir_stickers()
    cmd, used = build_sticker_cmd(video, stickers, out, canvas, crf, fps,
                                  scale_ratio, margin_ratio, opacity, seed, chance)
    if cmd is None or used is None:
        return False
    tmp = out.with_name(out.stem + f".stk_{os.getpid()}{out.suffix}")
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=600)
        if r.returncode != 0 or not tmp.is_file() or tmp.stat().st_size < 1024:
            tail = (r.stderr or b"").decode("utf-8", "replace").strip().splitlines()
            log(f"  [贴纸失败] {used.name}: {tail[-1] if tail else '无输出'}")
            _unlink(tmp)
            return False
        os.replace(str(tmp), str(out))
        return True
    except (OSError, subprocess.SubprocessError) as e:
        log(f"  [贴纸异常] {type(e).__name__}: {e}")
        _unlink(tmp)
        return False


def _dir_stickers() -> list[Path]:
    """Load images from the configured sticker directory lazily."""
    from .. import config  # noqa: PLC0415
    d = Path(config.MONTAGE_STICKER_DIR)
    if not d.is_dir():
        return []
    return [p for p in sorted(d.rglob("*"))
            if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")]


def _unlink(p) -> None:
    try:
        Path(p).unlink()
    except OSError:
        pass
