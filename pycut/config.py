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
# LLM 网关预设（OpenAI 兼容协议）
#   默认 deepseek（官方公网，稳定，支持图像）。用户只需配置 DEEPSEEK_API_KEY
#   （环境变量或项目根 .env 文件，见 .env.example）；硬编码值为本机回退。
#   --gateway 选择预设；命令行 --llm-url / --llm-key / --llm-model 可单独覆盖。
# ---------------------------------------------------------------------------
GATEWAYS = {
    "local": {  # 局域网网关（快，但偶发 502/503/超时）
        "url": "http://192.168.1.250:20128/v1",
        "key": os.environ.get("PYCUT_LOCAL_KEY",
                              "sk-REDACTED_LOCAL-e76438-796176a8"),
        "model": "deepseek-v4.1-flash",
    },
    "deepseek": {  # 官方 DeepSeek 网关（公网，默认）
        "url": "https://api.deepseek.com/v1",
        "key": os.environ.get("DEEPSEEK_API_KEY",
                              "sk-REDACTED_DEEPSEEK"),
        "model": "deepseek-flash",
    },
}
DEFAULT_GATEWAY = "deepseek"
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
    """按名称取网关预设，未知名称回退默认。"""
    return GATEWAYS.get(name, GATEWAYS[DEFAULT_GATEWAY])


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
