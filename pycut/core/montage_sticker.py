"""贴纸叠加：把静态图片（PNG/JPG）叠到片段上，用于增加画面差异。

用户要求的视觉规则（来自实测反馈「没看到干扰画面」）：

1. **背面半透明**：贴纸整体alpha ≈ 0.55，能看见底下的原画面，
   不是硬邦邦地盖上去；
2. **四角透明**：贴纸四角做羽化过渡，边缘融进主画面，而不是一个生硬的矩形块。

实现要点（都是本机实测踩出来的）：

- **PNG 必须显式转 RGBA + geq 求 alpha**：ffmpeg 直接叠 png 时，
  jpg 来源（无alpha 通道）会被当成全不透明，实测是一片白/黑块。
  这里用 `format=rgba,geq=r='r(X,Y)':a='alpha(X,Y)*<opacity>'` 统一施加上限，
  对有 alpha 的 png 与没 alpha 的 jpg 都成立。
- **四角羽化**：`geq` 里按到各角的距离算一个衰减因子，乘进alpha。
  ffmpeg 的 geq 表达式没有开方，衰减用**分段线性**（靠近角则快速下降）实现。
- **叠在片段上而不是成片上**：片段级叠加才不会和字幕/画中画抢位置。
- 滤镜顺序：先 scale 到目标宽度 → geq 施加上限+羽化 → overlay。

术语：贴纸 = 叠加在画面上的静态小图；画中画 = 叠加动态视频。两者互不干扰。
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .utils import log

# geq 表达式里 X/Y 的取值上界（滤镜链内统一按贴纸尺寸传入）
_ALPHA_VARS = "alpha(X,Y)"


def sticker_chain(img: Path, canvas: tuple[int, int],
                  scale_ratio: float = 0.28, margin_ratio: float = 0.04,
                  opacity: float = 0.55, pos: str = "br",
                  fps: int | None = None) -> list[str] | None:
    """构造「贴纸叠加」的 filter_complex 片段（不含输入/输出映射）。

    返回 list[str]，第一项是 filter_complex 的**链部分**，第二项是输出 label。
    返回 None 表示参数不合法（调用方应跳过贴纸而不是报错）。

    参数：
    - img: 贴纸文件（png/jpg/webp）
    - canvas: 画布 (w, h)
    - scale_ratio: 贴纸宽度 = 画布宽 × 该比例
    - margin_ratio: 距边缘留白 = 画布宽 × 该比例
    - opacity: 背面不透明度上限（0~1，0.55 = 半透明）
    - pos: 角落位置 tl / tr / bl / br
    """
    img = Path(img)
    if not img.is_file():
        return None
    w, h = canvas
    if w <= 0 or h <= 0:
        return None
    opacity = max(0.05, min(1.0, float(opacity)))

    # 贴纸缩放：按宽度定，高度按原图比例（-2 = 自动保持比例）
    sw = max(16, int(round(w * float(scale_ratio))))
    margin = max(0, int(round(w * float(margin_ratio))))

    # 位置表达式：overlay 的 x/y 用主输入的 W/H（成片尺寸）
    if pos == "tl":
        off = f"{margin}:{margin}"
    elif pos == "tr":
        off = f"W-w-{margin}:{margin}"
    elif pos == "bl":
        off = f"{margin}:H-h-{margin}"
    else:                       # br（默认）
        off = f"W-w-{margin}:H-h-{margin}"

    # 边缘羽化：让贴纸四边渐隐，融进主画面而不是矩形硬边。
    # 权重 = min(1, 到边距离 / 带宽)，再取**平均**（不是连乘）——
    # 连乘会让「距边 100px、带宽 90px」处权重只有 0.55，再乘不透明度就更看不见了
    # （实测贴纸几乎透明）。取平均后中心区恒为 1，只在真正的边缘渐变。
    fade = max(8, int(sw * 0.45))
    ex = f"min(1\\,min(X\\,W-1-X)/{fade})"
    ey = f"min(1\\,min(Y\\,H-1-Y)/{fade})"
    fw = f"({ex}+{ey})/2"
    # 滤镜图正确写法（实测踩过两种错法）：
    #   错法1 `[0:v]<geq链>[stk];[0:v][stk]overlay` -> filter 输入标签不够
    #        （"More input link labels ... than it has inputs"）
    #   错法2 `[0:v]split=2[bkg][keep];...;[keep][stk]overlay` ->
    #        split 的 bkg 输出没人消费（"output 0 (bkg) unconnected"）
    # 正解：split 只**多接一路给贴纸**，主画面那一路直接当 overlay 的底，
    #      即 `[0:v]split=2[base][unused]` 不需要——overlay 的 main 本来就是 [0:v]，
    #      所以只需 split 出一路给贴纸处理，base 直接用原始标签。
    # 实际上完全不需要 split：overlay 支持 `[0:v][stk]overlay=...`，
    # 因为 [0:v] 作为 main 输入会被读取一次，贴纸是另一路。
    ch = (
        f"[1:v]scale={sw}:-2,format=rgba,"
        f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':"
        f"a='{_ALPHA_VARS}*{opacity}*({fw})'[stk]"
    )
    chain = (
        f"{ch};"
        f"[0:v][stk]overlay={off}:format=auto:shortest=0[vstk]"
    )
    return [chain, "vstk"]


def build_sticker_cmd(video: Path, stickers: list[Path], out: Path,
                      canvas: tuple[int, int], crf: str = "20",
                      fps: int | None = None, scale_ratio: float = 0.28,
                      margin_ratio: float = 0.04, opacity: float = 0.55,
                      seed: int = 0, chance: float = 1.0) -> tuple[list[str] | None, Path | None]:
    """构造「给单段加贴纸」的完整 ffmpeg 命令。

    返回 (argv, 实际用的贴纸路径)；无贴纸可用时返回 (None, None)。
    贴纸按 seed + chance 决定是否叠（调用方已决定该段要不要贴，这里只决定用哪张）。
    """
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
        os.environ.get("PYCUT_FFMPEG", "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg"),
        "-y", "-loglevel", "error", "-nostdin",
        "-i", str(video),        # 0 = 片段画面
        "-i", str(img),          # 1 = 贴纸（单帧输入，overlay 的 eof_action=repeat
        #                            会自动保持最后一帧，**不能加 -loop 1**——
        #                            那样 ffmpeg 永不结束，实测直接挂到超时被杀）
        "-filter_complex", chain,
        "-map", f"[{label}]",
        "-an",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
        "-pix_fmt", "yuv420p", "-fps_mode", "cfr",
        "-movflags", "+faststart",
        str(tmp),                # 写 tmp，调用方跑完再 os.replace 到 out
    ]
    return cmd, img


def apply_sticker_to_file(video: Path, out: Path, canvas: tuple[int, int],
                          crf: str = "20", fps: int | None = None,
                          scale_ratio: float = 0.28,
                          margin_ratio: float = 0.04, opacity: float = 0.55,
                          seed: int = 0, chance: float = 1.0,
                          sticker: Path | None = None) -> bool:
    """给一个视频文件加贴纸；优先使用步骤B明确选中的贴纸，不再误读默认目录。"""
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
    """从 config.MONTAGE_STICKER_DIR 读取贴纸目录（延迟 import 避免循环）。"""
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
