"""pycut 全局配置与默认常量。

所有数值均在本机实测调优，未经重新标定请勿随意改动。
路径 / 网关等可通过环境变量或命令行覆盖（见各命令实现）。
"""
from __future__ import annotations

import os
from pathlib import Path


# ---------------------------------------------------------------------------
# .env 加载（纯标准库，无第三方依赖）
#   用户只需在 .env 里配置 DEEPSEEK_API_KEY 即可；.env 已被 git 忽略。
#   真实 shell 环境变量优先于 .env；.env 只补充未设置的变量。
# ---------------------------------------------------------------------------
def _load_dotenv() -> None:
    env_path = PROJECT_ROOT / ".env"
    if not env_path.is_file():
        return
    try:
        with open(env_path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if not k:
                    continue
                v = v.strip("\"'")            # 去掉可能的引号包裹
                os.environ.setdefault(k, v)   # 不覆盖已存在的真实环境变量
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 外部工具（ffmpeg-full 由 homebrew 安装，含 videotoolbox 硬件编码）
# ---------------------------------------------------------------------------
FFMPEG = "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg"
FFPROBE = "/opt/homebrew/opt/ffmpeg-full/bin/ffprobe"

# ---------------------------------------------------------------------------
# 目录约定
#   源视频：待处理的原始短视频（抖音/微信导出等）
#   产出  ：桌面两个分桶目录（有人脸 / 无人脸）+ 清单目录
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent          # pycut/.. => 项目根
_load_dotenv()   # 在读取任何密钥前加载 .env（用户只需在 .env 配置 DEEPSEEK_API_KEY）

SRC_DIR = Path("/Volumes/Lexar/DevStorage/剪辑/爆款素材/抖音")
DESKTOP = Path.home() / "Desktop"
OUT_FACE = DESKTOP / "切片_有人脸"
OUT_NOFACE = DESKTOP / "切片_无人脸"
OUT_MANIFEST = DESKTOP / "切片_清单"

# ---------------------------------------------------------------------------
# 场景检测 / 分段
# ---------------------------------------------------------------------------
SCENE_TH = 0.30          # 帧差阈值（ffmpeg select='gt(scene,TH)' 标定：0.25→26 / 0.30→16 / 0.40→16 切点）
MIN_SEG = 2.0            # 最短段落：不足 2.0s 并入相邻段（保证每段 > 2s）
MAX_SEG = 60.0           # 超长段落强制切分
DEDUP = 0.20             # 切点间距 < 0.2s 视为重复，合并
FRAME_WIDTH = 480        # 抽帧宽度
SAMPLE_RATIOS = (0.25, 0.50, 0.75)   # 段内采样位置
INSET = 0.12             # 两端内缩比例，避开转场帧

# ---------------------------------------------------------------------------
# 人脸 Haar 两档
# ---------------------------------------------------------------------------
HAAR_STRICT = dict(scaleFactor=1.15, minNeighbors=7, min_size_ratio=0.06)
HAAR_LOOSE = dict(scaleFactor=1.05, minNeighbors=3, min_size_ratio=0.03)
FACE_WIDTH_RATIO = 0.06  # 人脸宽度 >= 帧高 6% 才算有效人脸
MAJORITY = 0.5           # 严格命中帧数 >= 一半 -> face

# ---------------------------------------------------------------------------
# 编码
# ---------------------------------------------------------------------------
BITRATE_LOW = "5M"       # 高 <= 720
BITRATE_HIGH = "8M"      # 高 > 720
WORKERS_ENC = 2          # 1080p 实测 2 是拐点

# ---------------------------------------------------------------------------
# LLM 网关（OpenAI 兼容协议）—— 只有「主网关 / 备用网关」两个，不按厂商命名
#   主网关 main  ：默认 DeepSeek 官方，可在 .env 换成任意 OpenAI 兼容网关
#   备用网关 backup：主网关连续失败时自动顶上（可用 --no-fallback 关闭）
#   --gateway {main,backup} 手动指定起始网关；--llm-* 可临时覆盖当前网关。
# ---------------------------------------------------------------------------
#   全部从 .env（或环境变量）读取，代码里没有硬编码密钥。见 .env.example：
#     PYCUT_MAIN_URL / PYCUT_MAIN_KEY / PYCUT_MAIN_MODEL       主网关（默认 DeepSeek）
#     PYCUT_BACKUP_URL / PYCUT_BACKUP_KEY / PYCUT_BACKUP_MODEL 备用网关（可选）
#     DEEPSEEK_API_KEY   主网关密钥的旧名，仍兼容（PYCUT_MAIN_KEY 优先）
_MAIN_KEY = (os.environ.get("PYCUT_MAIN_KEY")
             or os.environ.get("DEEPSEEK_API_KEY") or "")
GATEWAYS = {
    "main": {  # 主网关：默认 DeepSeek 官方（公网、稳定、支持图像）
        "url": (os.environ.get("PYCUT_MAIN_URL")
                or os.environ.get("PYCUT_LLM_URL")
                or "https://api.deepseek.com/v1"),
        "key": _MAIN_KEY,
        "model": (os.environ.get("PYCUT_MAIN_MODEL")
                  or os.environ.get("PYCUT_LLM_MODEL")
                  or "deepseek-flash"),
    },
    "backup": {  # 备用网关：留空即视为未启用，主网关失败时不会切换
        "url": os.environ.get("PYCUT_BACKUP_URL", ""),
        "key": os.environ.get("PYCUT_BACKUP_KEY", ""),
        "model": os.environ.get("PYCUT_BACKUP_MODEL", ""),
    },
}
# 备用网关未填地址时，回落到主网关地址（只换 key/model 场景也允许）
if not GATEWAYS["backup"]["url"]:
    GATEWAYS["backup"]["url"] = GATEWAYS["main"]["url"]
if not GATEWAYS["backup"]["model"]:
    GATEWAYS["backup"]["model"] = GATEWAYS["main"]["model"]

DEFAULT_GATEWAY = "main"     # 固定：默认走主网关（不再有 PYCUT_GATEWAY 开关）
BACKUP_GATEWAY = "backup"
LLM_FAILOVER_FAILS = 3       # 主网关连续失败 N 次 -> 自动切换备用网关
WORKERS_LLM = 8          # 实测比 4 快 19 倍
LLM_MAX_TOKENS = 500     # 推理模型：30 会被 reasoning 吃光返回空
LLM_MAX_TOKENS_RETRY = 1200
LLM_TIMEOUT = 60
LLM_RETRY_STATUS = {429, 500, 502, 503, 504}
LLM_BACKOFF = (0.1, 2.1, 4.2)
LLM_MAX_CONSEC_FAIL = 5  # 连续失败 5 次 -> 全局禁用 LLM
HTTP_TIMEOUT = 90

# ---------------------------------------------------------------------------
# seek / 磁盘
# ---------------------------------------------------------------------------
COARSE_BACKOFF = 1.0     # 输入侧粗 seek 回退秒数（关键帧间隔 8.33s 而镜头中位数仅 2.03s）
MIN_FREE_BYTES = 2 * 1024 ** 3   # 输出盘最小剩余空间

# ---------------------------------------------------------------------------
# 清单列（CSV utf-8-sig + JSON 双写）
# ---------------------------------------------------------------------------
MANIFEST_COLUMNS = [
    "video", "seg_index", "start", "end", "duration",
    "verdict", "decided_by",
    "haar_strict_frames", "haar_loose_frames", "haar_max_ratio",
    "llm_face", "llm_confidence", "llm_reason",
    "out_path", "out_bytes",
]


def resolve_gateway(name: str) -> dict:
    """按名称取网关预设，未知名称回退主网关。"""
    return GATEWAYS.get(name, GATEWAYS[DEFAULT_GATEWAY])


def other_gateway(name: str) -> str:
    """取另一个网关名（主 <-> 备）。"""
    n = name if name in GATEWAYS else DEFAULT_GATEWAY
    return BACKUP_GATEWAY if n == DEFAULT_GATEWAY else DEFAULT_GATEWAY


def apply_llm_overrides(name: str, url: str | None = None,
                        key: str | None = None,
                        model: str | None = None) -> tuple[str, str, str]:
    """把 --gateway 预设与 --llm-* 覆盖合并为 (url, key, model)。"""
    preset = resolve_gateway(name)
    return (
        url or preset["url"],
        key or preset["key"],
        model or preset["model"],
    )


# 模板里的占位值（.env.example 的 sk-你的密钥 等）不算已配置
_KEY_PLACEHOLDERS = ("你的", "your", "xxxx", "changeme", "placeholder", "todo", "xxx")


def _is_placeholder(key: str) -> bool:
    k = (key or "").strip()
    if not k:
        return True
    low = k.lower()
    return any(p in low for p in _KEY_PLACEHOLDERS)


def key_ready(name: str | None = None) -> bool:
    """密钥是否已真实配置（来自 .env / 环境变量）。空或占位都算未配置。"""
    return not _is_placeholder(resolve_gateway(name or DEFAULT_GATEWAY)["key"])


def _state(k: str) -> str:
    if not _is_placeholder(k):
        return "已配置"
    return "未配置(占位)" if k.strip() else "未配置"


def gateway_summary(name: str | None = None) -> str:
    """用于日志：网关名 / 地址 / 模型 / 密钥状态（不打印密钥本身）。"""
    n = name if name in GATEWAYS else DEFAULT_GATEWAY
    g = resolve_gateway(n)
    return f"{n} @ {g['url']} model={g['model']} key={_state(g['key'])}"


def gateways_summary() -> str:
    """主网关 + 备用网关 两行摘要（不打印密钥本身）。"""
    b = resolve_gateway(BACKUP_GATEWAY)
    tail = "" if key_ready(BACKUP_GATEWAY) else "   (主网关失败时不会切换)"
    return (f"主网关 {gateway_summary(DEFAULT_GATEWAY)}\n"
            f"备用网关 {gateway_summary(BACKUP_GATEWAY)}{tail}")


def backup_ready() -> bool:
    """备用网关是否可用（密钥已真实配置且与主网关不是完全同一个地址/密钥）。"""
    if not key_ready(BACKUP_GATEWAY):
        return False
    m, b = resolve_gateway(DEFAULT_GATEWAY), resolve_gateway(BACKUP_GATEWAY)
    return (m["url"], m["key"], m["model"]) != (b["url"], b["key"], b["model"])
