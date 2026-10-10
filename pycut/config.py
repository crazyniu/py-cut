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
# ---------------------------------------------------------------------------
# 去重 dedup（过平台查重）—— 需求与技术方案见 docs/DEDUP.md
#   平台是 5 层指纹比对（文件哈希/元数据/视觉 pHash+CNN+光流/音频/文本），
#   本模块按「容器→几何→像素→时间→结构→音频」分层扰动，并用 pHash 距离 + PSNR 自检。
# ---------------------------------------------------------------------------
DEDUP_OUT = DESKTOP / "去重_输出"      # 默认输出（保留切片桶的子目录结构）
DEDUP_REPORT = DESKTOP / "去重_报告"   # JSON 报告目录
DEDUP_PRESETS = ("light", "standard", "aggressive")
DEDUP_DEFAULT_PRESET = "standard"
DEDUP_WORKERS = 2                   # 并行处理数（libx264 软件编码，别开太高）
DEDUP_X264_PRESET = "veryfast"
DEDUP_PHASH_SAMPLES = 12            # 抽帧数

# 自检门禁（阈值由本机实测标定，见下方「实测标定」注释，不要凭感觉拍）
#   pHash 距离：按档位给下限。几何/像素层改动天然只把距离推到 2~8，
#   只有结构层（模糊边框/镜像）才能到 18~34，所以门槛必须分档。
#   实测标定（720x1280 素材，12 帧抽样；括号内是「长切片 39.8s」的值）：
#     light       pHash ≈ 3.2~3.5 (长片 2.2)     PSNR ≈ 25dB
#     standard    pHash ≈ 3.7~4.7 (长片 2.8)     PSNR ≈ 24dB
#     aggressive  pHash ≈ 7.0~7.7 (长片 7.0)     PSNR ≈ 22dB
#     +模糊边框    pHash ≈ 18.5~25.8              PSNR ≈ 13~17dB
#     +镜像       pHash ≈ 21~34                  PSNR ≈ 16~17dB
#   门槛取「实测下沿」再留余量：长切片运动量大、单帧改动被摊薄，
#   pHash 天然偏低，用短片的值当门槛会把长片误判成「没动手」。
#   批量实测（桌面切片 14 条、跨 3 个源视频、默认参数、换 3 个 seed）实测下沿：
#     light      最小 1.33   分布 1.3~3.0
#     standard   最小 2.00   分布 2.0~3.7
#     aggressive 最小 2.33   分布 2.3~26.7  <- 跨度极大！
#   aggressive 跨度大的原因：结构层（模糊边框/角标）是**概率触发**，
#   命中时 pHash 冲到 18~27，没命中时只有几何+像素层的 2~8，两者是不同量级。
#   所以三档门槛统一按「各档跨 seed 的实测最小值 × 0.7 向下取整」取，
#   宁可宽松——门槛的作用只是兜底「有没有真的动手」，不该因seed 挑中与否而误判。
#   PSNR 说明：它是「改动幅度」指标，不是画质指标——一旦做几何改动，
#   逐像素对齐必然崩，PSNR 掉到 13~25dB 属正常，画面观感没问题。
#   因此 PSNR 只当**灾难下限**（太低=真的糊了/错位了），不作为质量门禁。
DEDUP_MIN_PHASH_BY_PRESET = {"light": 1.0, "standard": 1.4, "aggressive": 1.6}
DEDUP_MIN_PHASH_DIST = 1.4      # 未指定档位时的兜底（= standard）
DEDUP_MIN_PSNR = 12.0           # 灾难下限：低于此值视为画面真的被破坏
#   批量实测（桌面全部 47 条切片，standard 档）PSNR 分布：
#     最小 13.4/  最大 24.1  中位 16.1  均值 16.5，低于 12.0 的 0 条。
#   原定 14.0 有 3 条误报（13.4~14.0，抽帧确认画面完好），故下调到 12.0——
#   它只兜「真的糊了/错位了」，不承担画质评分职责。
# 各档 pHash 距离的实测期望区间（超出上限只是提示「改动过头」，不判失败）。
# 下沿与 MIN_PHASH_BY_PRESET 对齐（同一批 14 条 × 3 seed 实测），
# 上沿按「结构层/镜像都命中」的最高观测值留 1.5 倍：
#   实测 light 1.3~3.0 / standard 2.0~3.7 / aggressive 2.3~26.7
# 注意跨度主要由**结构层是否被概率触发**决定，而非素材本身；
# 素材内容也会影响（静态镜头比运动镜头更容易检出改动）。
DEDUP_PHASH_EXPECT = {
    "light": (1.0, 8.0),
    "standard": (1.4, 10.0),
    "aggressive": (1.6, 40.0),
}
DEDUP_DUR_TOLERANCE = 0.35          # 时长偏差上限（启用变速时按 speed 放宽）

# 参数随机化范围（seed 决定具体取值）
DEDUP_CRF_RANGE = (22, 26)
DEDUP_ABITRATE_RANGE = (96, 160)   # kbps
DEDUP_GOP_RANGE = (46, 62)
DEDUP_CROP_RANGE = (3, 8)           # 边缘微裁像素
DEDUP_ZOOM_RANGE = (1.010, 1.030)
DEDUP_ROTATE_DEG = (0.3, 0.8)
DEDUP_PERSPECTIVE_PX = 3            # 激进档梯形变形最大像素
DEDUP_BRIGHTNESS = 0.012           # ± 最大亮度偏移
DEDUP_CONTRAST_RANGE = (1.010, 1.030)
DEDUP_SATURATION_RANGE = (1.020, 1.050)
DEDUP_HUE_DEG = 3                  # ± 最大色相偏移
DEDUP_RGB_DRIFT = 0.012            # ± 通道增益漂移
DEDUP_NOISE_RANGE = (3, 6)
DEDUP_SHARPEN_RANGE = (0.3, 0.8)
DEDUP_SPEED_RANGE = (1.020, 1.050)
DEDUP_PITCH_RANGE = (1.004, 1.010)
DEDUP_FPS_JITTER = ("30000/1001", "30", "30003/1000", "60")
DEDUP_AUDIO_RATE = 44100

# ---------------------------------------------------------------------------
# 混剪 montage —— 三步流程（需求见未命名.txt / docs/MONTAGE.md）
#   步骤A：从「爆款视频」提取语音节奏与总时长基准（不做 ASR 转写，见 MONTAGE.md 说明）
#   步骤B：从「片段库」随机取竖屏片段、不重复、填满基准时长、静音
#   步骤C：从「去重输出」取切片，以画中画叠在步骤B结果右上角、静音、时长同基准
#   素材里混有横屏（1920x1080 / 3840x2160），一律排除——只吃竖屏 9:16
# ---------------------------------------------------------------------------
MONTAGE_SRC = Path("/Volumes/Lexar/DevStorage/剪辑/爆款素材/快手/1.mp4")  # 步骤A 基准
MONTAGE_CLIP_DIR = Path("/Volumes/Lexar/DevStorage/剪辑/片段")           # 步骤B 片段库
MONTAGE_DEDUP_DIR = DESKTOP / "去重_输出"                                # 步骤C 画中画素材
MONTAGE_OUT = DESKTOP / "混剪_输出"
MONTAGE_TMP = Path("/tmp/pycut_montage")   # 中间产物（步骤B 结果），不进桌面

# 目标画布：以步骤A 基准视频的分辨率/帧率为准，保证最终成片与基准同规格
MONTAGE_CANVAS = (720, 1276)        # 步骤A 实测 720x1276（注意不是 1280）
MONTAGE_FPS = 30

# 竖屏判定：宽高比在此区间才算 9:16，容差 ±12%（手机录制常见 720x1280 / 544x960）
MONTAGE_PORTRAIT_RATIO = 9 / 16
MONTAGE_RATIO_TOLERANCE = 0.12
# 边长比上限：超过则判为非竖屏（1920x1080=1.78、3840x2160=1.78 都会被排除）
MONTAGE_MAX_SIDE_RATIO = 1.40       # w/h 超过 1.40 视为横屏

# 语音切分（silencedetect）
# 阈值实测（基准 1.mp4 77.32s / 有持续背景音乐，volumedetect: mean -13.6dB、max 0.0dB）：
#   -32dB → 0 段静音（整条被判连续语音，截成 1 个 8s 槽，缺口 67.8s）
#   -25dB → 3 段；-22dB → 6 段；-20dB → 7 段（语音合计 43.1s，槽最碎）
# 素材带 BGM 时音量不往下掉，阈值必须比常规语音高得多，否则切不出节奏。
# 取 -20dB：槽数最多、碎片感最强，配合 _fill_slots 补齐后节奏最自然。
MONTAGE_SILENCE_NOISE_DB = -20     # 静音阈值 dB
MONTAGE_SILENCE_MIN_DUR = 0.35      # 静音最短持续秒数，短于此不算断句
# 语音段最短保留时长（太碎的段丢弃，避免切出 0.2s 的闪帧）
MONTAGE_SEG_MIN_DUR = 0.8
# 语音段最长保留时长（超过则截断，防止一句长话铺满全屏）
MONTAGE_SEG_MAX_DUR = 8.0
MONTAGE_TARGET_FILL_RATIO = 0.98    # 拼接后目标时长 = 基准时长 × 该系数（留一点余量补齐）

# 片段挑选（步骤B）
MONTAGE_CLIP_MIN_DUR = 1.2          # 太短的片段不用（会闪）
MONTAGE_CLIP_MAX_DUR = 12.0         # 太长的片段截到上限
MONTAGE_WORKERS = 2

# 画中画（步骤C）
MONTAGE_PIP_SCALE = 0.26            # 画中画宽度 = 基准宽 × 该比例（右上角）
MONTAGE_PIP_MARGIN = 0.03# 距右/上边缘留白比例
MONTAGE_PIP_CONTINUOUS = True       # 画中画首尾相接（不设空档，实测设空档会有 19% 时间没有）
# 画中画四角融入（尺寸不变，只改边缘过渡）——用户反馈「现在是直接盖上去的」
MONTAGE_PIP_FEATHER = 0.22          # 四角羽化带 = 画中画短边 × 该比例
MONTAGE_PIP_SHADOW = 0.035# 投影扩散 = 画中画短边 × 该比例
MONTAGE_PIP_SHADOW_ALPHA = 0.45      # 投影浓度（0=无投影）
MONTAGE_STICKER_DIR = DESKTOP / "混剪_贴纸"   # 贴纸素材目录（png/jpg/webp）
# 贴纸叠加（用户要求：背面半透明 + 四角透明，让贴纸融进主画面而不是硬贴上去）
MONTAGE_STICKER_CHANCE = 0.65      # 每个片段叠贴纸的概率（用户要求「给主画面增加贴纸」，
                                   # 实测 0.35 时 48 段只命中 14 段，主画面偏稀疏）
MONTAGE_STICKER_SCALE = 0.28        # 贴纸宽度 = 画布宽 × 该比例
MONTAGE_STICKER_OPACITY = 0.55      # 背面不透明度（0.55 = 半透明，能看见底下的画面）
MONTAGE_STICKER_MARGIN = 0.04       # 贴纸距边缘留白

# 字幕烧录（步骤D）。样式按画布比例自适应，不写死像素。
# 默认 BorderStyle=3（半透明黑底框）而非 BorderStyle=1（纯描边）：
# 混剪素材随机、底部颜色不可控，底框能保证任何背景下白字都清晰（已实拍两种对比）。
MONTAGE_SUB_FONT_RATIO = 0.042      # 字号 = 画布高 × 该比例（1276 → 54px）
MONTAGE_SUB_MARGIN_V_RATIO = 0.08   # 底边距 = 画布高 × 该比例
MONTAGE_SUB_MARGIN_H_RATIO = 0.05   # 左右安全边距
MONTAGE_SUB_BORDER_STYLE = 3# 3=半透明底框；改 1 则切纯描边
MONTAGE_SUB_MIN_READABLE = 0.8      # 字幕最短可读时长，槽比它短就不给字幕
MONTAGE_SUB_PATH = Path.home() / "Desktop/10月8日.srt"   # 默认字幕文件
MONTAGE_SUB_X264_PRESET = "veryfast"

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
