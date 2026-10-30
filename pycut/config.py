"""Shared configuration and calibrated defaults.

Load paths and gateways from the environment; keep calibrated numbers unchanged
unless a reproducible calibration justifies a change."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


# Environment values override .env; remember injected values so reload can replace them.
_dotenv_values: dict[str, str] = globals().get("_dotenv_values", {})


def _load_dotenv() -> None:
    from .settings import read_env
    # Remove only values injected by the previous .env load; preserve external overrides.
    for key, value in _dotenv_values.items():
        if os.environ.get(key) == value:
            os.environ.pop(key, None)
    _dotenv_values.clear()
    for key, value in read_env().items():
        if key not in os.environ:
            os.environ[key] = value
            _dotenv_values[key] = value


def _path(key: str, fallback: Path) -> Path:
    value = os.environ.get(key, '').strip()
    path = Path(value).expanduser() if value else fallback
    return path if path.is_absolute() else PROJECT_ROOT / path


# Prefer ffmpeg-full when available; environment overrides still win.
def _media_tool(name: str) -> str:
    bundled = Path("/opt/homebrew/opt/ffmpeg-full/bin") / name
    return os.environ.get(f"PYCUT_{name.upper()}") or (
        str(bundled) if bundled.is_file() else shutil.which(name) or name)


SLICE_ENCODER = "h264_videotoolbox" if sys.platform == "darwin" else "libx264"


# Paths shared by public commands and internal legacy slicing helpers.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
_load_dotenv()   # Load .env before resolving paths or credentials.
FFMPEG = _media_tool("ffmpeg")
FFPROBE = _media_tool("ffprobe")

SRC_DIR = _path("PYCUT_SOURCE_DIR", PROJECT_ROOT / "materials" / "source")
from .settings import directories as _directories
def _configured_directories(key: str) -> list[Path]:
    try:
        return _directories(os.environ.get(key, ""))
    except ValueError:
        # Keep the settings service usable so malformed .env lists can be fixed.
        # settings.problems reports the invalid value before any task is run.
        return []

CLIP_DIRS = _configured_directories("PYCUT_CLIP_DIR")
PIP_DIRS = _configured_directories("PYCUT_PIP_DIR")
INTRO_DIRS = _configured_directories("PYCUT_INTRO_DIR")
CLIP_DIR = CLIP_DIRS[0] if CLIP_DIRS else PROJECT_ROOT / "materials" / "background"
PIP_DIR = PIP_DIRS[0] if PIP_DIRS else SRC_DIR.parent / "切片"
REFERENCE_DIR = _path("PYCUT_REFERENCE_DIR", PROJECT_ROOT / "materials" / "reference")
OUTPUT_DIR = _path("PYCUT_OUTPUT_DIR", PROJECT_ROOT / "output")
DESKTOP = Path.home() / "Desktop"
OUT_FACE = PIP_DIR / "有人脸"
OUT_NOFACE = PIP_DIR / "无人脸"
OUT_MANIFEST = OUTPUT_DIR / "切片_清单"


# Calibrated scene splitting and sampling defaults.
SCENE_TH = 0.30          # Calibrated FFmpeg scene score; do not substitute a differently scaled detector.
MIN_SEG = 2.0            # Merge shorter intervals into neighbours without discarding footage.
MAX_SEG = 15.0
DEDUP = 0.20
FRAME_WIDTH = 480
SAMPLE_RATIOS = (0.25, 0.50, 0.75)
INSET = 0.12             # Inset sampling positions to avoid transition frames.


# Strict and loose Haar passes separate confident from ambiguous results.
HAAR_STRICT = dict(scaleFactor=1.15, minNeighbors=7, min_size_ratio=0.06)
HAAR_LOOSE = dict(scaleFactor=1.05, minNeighbors=3, min_size_ratio=0.03)
FACE_WIDTH_RATIO = 0.06  # Minimum face width as a fraction of frame height.


# Encoding defaults; retain calibrated bitrate and worker limits.
BITRATE_LOW = "5M"
BITRATE_HIGH = "8M"
WORKERS_ENC = 2


# OpenAI-compatible main/backup gateways; explicit --llm-* overrides suppress failover.
# Read credentials from .env/environment only; legacy DEEPSEEK_API_KEY remains supported.
_MAIN_KEY = (os.environ.get("PYCUT_MAIN_KEY")
             or os.environ.get("DEEPSEEK_API_KEY") or "")
GATEWAYS = {
    "main": {
        "url": (os.environ.get("PYCUT_MAIN_URL")
                or os.environ.get("PYCUT_LLM_URL")
                or "https://api.deepseek.com/v1"),
        "key": _MAIN_KEY,
        "model": (os.environ.get("PYCUT_MAIN_MODEL")
                  or os.environ.get("PYCUT_LLM_MODEL")
                  or "deepseek-flash"),
    },
    "backup": {  # Backup is enabled only when its key is configured.
        "url": os.environ.get("PYCUT_BACKUP_URL", ""),
        "key": os.environ.get("PYCUT_BACKUP_KEY", ""),
        "model": os.environ.get("PYCUT_BACKUP_MODEL", ""),
    },
}
# Allow a backup that overrides only key or model.
if not GATEWAYS["backup"]["url"]:
    GATEWAYS["backup"]["url"] = GATEWAYS["main"]["url"]
if not GATEWAYS["backup"]["model"]:
    GATEWAYS["backup"]["model"] = GATEWAYS["main"]["model"]

DEFAULT_GATEWAY = "main"
BACKUP_GATEWAY = "backup"
LLM_FAILOVER_FAILS = 3
WORKERS_LLM = 8
LLM_MAX_TOKENS = 500     # Reasoning tokens share the output budget; small limits can leave content empty.
LLM_MAX_TOKENS_RETRY = 1200
LLM_RETRY_STATUS = {429, 500, 502, 503, 504}
LLM_BACKOFF = (0.1, 2.1, 4.2)
LLM_MAX_CONSEC_FAIL = 5  # Disable this client after consecutive failures to bound wasted work.
HTTP_TIMEOUT = 90


# Seek accuracy and output-space constraints.
COARSE_BACKOFF = 1.0     # Back off input seek before applying accurate output seek across long GOPs.
MIN_FREE_BYTES = 2 * 1024 ** 3


# Deduplication defaults; contracts and limitations are in docs/DEDUP.md.
DEDUP_OUT = OUTPUT_DIR / "去重"
DEDUP_REPORT = OUTPUT_DIR / "去重_报告"
DEDUP_PRESETS = ("light", "standard", "aggressive")
DEDUP_DEFAULT_PRESET = "standard"
DEDUP_WORKERS = 2                   # Bound concurrent software encoders.
DEDUP_X264_PRESET = "veryfast"
DEDUP_PHASH_SAMPLES = 12

# Preset-specific change floors tolerate probabilistic structural effects.
# Geometry/pixel edits and structural rearrangements produce very different hash distances.
# PSNR is only a damage floor after geometry changes, not a perceptual quality score.
DEDUP_MIN_PHASH_BY_PRESET = {"light": 1.0, "standard": 1.4, "aggressive": 1.6}
DEDUP_MIN_PHASH_DIST = 1.4
DEDUP_MIN_PSNR = 12.0


# Upper expected distances are advisory; exceeding them does not fail verification.
DEDUP_PHASH_EXPECT = {
    "light": (1.0, 8.0),
    "standard": (1.4, 10.0),
    "aggressive": (1.6, 40.0),
}
DEDUP_DUR_TOLERANCE = 0.35          # Base duration tolerance; speed-induced change is accounted for separately.

# Seeded parameter ranges; changing these requires recalibration.
DEDUP_CRF_RANGE = (22, 26)
DEDUP_ABITRATE_RANGE = (96, 160)   # kbps
DEDUP_GOP_RANGE = (46, 62)
DEDUP_CROP_RANGE = (3, 8)
DEDUP_ZOOM_RANGE = (1.010, 1.030)
DEDUP_ROTATE_DEG = (0.3, 0.8)
DEDUP_BRIGHTNESS = 0.012
DEDUP_CONTRAST_RANGE = (1.010, 1.030)
DEDUP_SATURATION_RANGE = (1.020, 1.050)
DEDUP_HUE_DEG = 3
DEDUP_RGB_DRIFT = 0.012
DEDUP_NOISE_RANGE = (3, 6)
DEDUP_SHARPEN_RANGE = (0.3, 0.8)
DEDUP_SPEED_RANGE = (1.020, 1.050)
DEDUP_PITCH_RANGE = (1.004, 1.010)
DEDUP_FPS_JITTER = ("30000/1001", "30", "30003/1000", "60")
DEDUP_AUDIO_RATE = 44100


# Montage rhythm, background selection, optional PIP, stickers, and subtitle defaults.
MONTAGE_SRC = REFERENCE_DIR / "1.mp4"
MONTAGE_CLIP_DIR = CLIP_DIR
MONTAGE_DEDUP_DIR = PIP_DIRS[0] if PIP_DIRS else OUTPUT_DIR / "去重"  # Compatibility default for internal single-root callers.
MONTAGE_OUT = OUTPUT_DIR / "混剪"
MONTAGE_TMP = Path("/tmp/pycut_montage")

# Reference contributes timing, subtitles, and audio; output canvas stays fixed.
MONTAGE_CANVAS = (720, 1280)
MONTAGE_FPS = 30
MONTAGE_INSET_WIDTH_RANGE = (.82, .90)
MONTAGE_BACKDROP_BLUR = 4.0
MONTAGE_BACKDROP_FEATHER = .07  # Foreground edge blend width relative to the inset width.
MONTAGE_STICKER_PAIR_SCALE = .75


# Reject landscape above this width/height ratio unless explicitly allowed.
MONTAGE_MAX_SIDE_RATIO = 1.40

# Speech rhythm detection; background music requires a higher silence threshold.
MONTAGE_SILENCE_NOISE_DB = -20     # Silence threshold in dB.
MONTAGE_SILENCE_MIN_DUR = 0.35      # Minimum silence interval in seconds.
# Drop speech fragments below this duration.
MONTAGE_SEG_MIN_DUR = 0.8
# Cap long speech intervals to retain shot changes.
MONTAGE_SEG_MAX_DUR = 8.0
MONTAGE_TARGET_FILL_RATIO = 1.0     # The complete body must retain the reference duration.

# Background clip duration limits apply when selecting source intervals.
MONTAGE_CLIP_MIN_DUR = 1.2
MONTAGE_CLIP_MAX_DUR = 12.0
MONTAGE_WORKERS = 2

# PIP dimensions and margins are canvas fractions.
MONTAGE_PIP_SCALE = 0.30
MONTAGE_PIP_MARGIN = 0.08
MONTAGE_PIP_TOP_MARGIN = 0.075
# Generate feather/shadow on the small window, then overlay the main canvas once.
MONTAGE_PIP_FEATHER = 0.22          # Feather width as a fraction of small-window width.
MONTAGE_PIP_SHADOW = 0.035         # Shadow spread as a fraction of small-window width.
MONTAGE_PIP_SHADOW_ALPHA = 0.45    # Shadow opacity.
MONTAGE_STICKER_DIR = _path("PYCUT_STICKER_DIR", DESKTOP / "混剪_贴纸")
# Stickers retain translucency and feathered edges.
MONTAGE_STICKER_CHANCE = 1.0

MONTAGE_STICKER_SCALE = 0.28
MONTAGE_STICKER_OPACITY = 0.55
MONTAGE_STICKER_MARGIN = 0.04

# ASS geometry scales with the canvas; a translucent box keeps random backgrounds readable.
MONTAGE_SUB_BORDER_STYLE = 3
MONTAGE_SUB_MIN_READABLE = 0.8

# Internal default only; the public CLI pairs SRT by reference stem.
MONTAGE_SUB_PATH = MONTAGE_SRC.with_suffix(".srt")

MANIFEST_COLUMNS = [
    "video", "seg_index", "start", "end", "duration",
    "verdict", "decided_by",
    "haar_strict_frames", "haar_loose_frames", "haar_max_ratio",
    "llm_face", "llm_confidence", "llm_reason",
    "out_path", "out_bytes",
]


def resolve_gateway(name: str) -> dict:
    """Return the named gateway, falling back to the default."""
    return GATEWAYS.get(name, GATEWAYS[DEFAULT_GATEWAY])


def other_gateway(name: str) -> str:
    """Return the other gateway name (main or backup)."""
    n = name if name in GATEWAYS else DEFAULT_GATEWAY
    return BACKUP_GATEWAY if n == DEFAULT_GATEWAY else DEFAULT_GATEWAY


def apply_llm_overrides(name: str, url: str | None = None,
                        key: str | None = None,
                        model: str | None = None) -> tuple[str, str, str]:
    """Merge gateway defaults and explicit overrides into (url, key, model)."""
    preset = resolve_gateway(name)
    return (
        url or preset["url"],
        key or preset["key"],
        model or preset["model"],
    )


# Template placeholders do not count as configured credentials.
_KEY_PLACEHOLDERS = ("你的", "your", "xxxx", "changeme", "placeholder", "todo", "xxx")


def _is_placeholder(key: str) -> bool:
    k = (key or "").strip()
    if not k:
        return True
    low = k.lower()
    return any(p in low for p in _KEY_PLACEHOLDERS)


def key_ready(name: str | None = None) -> bool:
    """Return whether the gateway has a nonempty, non-placeholder key."""
    return not _is_placeholder(resolve_gateway(name or DEFAULT_GATEWAY)["key"])


def _state(k: str) -> str:
    if not _is_placeholder(k):
        return "已配置"
    return "未配置(占位)" if k.strip() else "未配置"


def gateway_summary(name: str | None = None) -> str:
    """Describe the gateway and key readiness without exposing the key."""
    n = name if name in GATEWAYS else DEFAULT_GATEWAY
    g = resolve_gateway(n)
    return f"{n} @ {g['url']} model={g['model']} key={_state(g['key'])}"


def refresh() -> None:
    """Reload configuration after Web or manual .env edits."""
    import importlib
    importlib.reload(sys.modules[__name__])
