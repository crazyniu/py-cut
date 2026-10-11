# pycut —— 短视频智能处理工具箱

统一处理抖音 / 微信等短视频的本地工具箱。已落地三大功能：**切片**、**去重（过查重）**、**混剪**。

## 功能一览

| 命令 | 做什么 | 产物位置 |
|---|---|---|
`slice` | 把长视频切成多段，分流有人脸/无人脸，擦除字幕 | `.env` 的背景切片目录（按人脸分桶） |
`cover` | 只重跑字幕擦除 | 源目录同级 `字幕查重/`，保留源文件 |
`dedup` | 调整视频画面，降低相似度 | 成片输出目录下 `去重/` |
`montage` | 组合片段生成新视频，可批量裂变 | 成片输出目录下 `混剪/` |

## 目录结构

```
pycut/
├── pycut.py                # 统一入口（命令行 + TUI）
├── run.sh                  # 一键脚本：选解释器 → 装依赖 → 读 .env → 运行
├── requirements.txt        # 依赖（numpy/OpenCV、Textual 目录组件、Windows curses）
├── pyproject.toml          # 包元数据
├── .env / .env.example     # 目录、密钥与网关配置（.env 已 gitignore）
├── .gitignore
├── AGENTS.md               # 给 AI 协作的规范（务必先读，AI 工具自动识别）
├── README.md               # 本文件
├── docs/
│   ├── ROADMAP.md          # 产品能力与演进设计（不跟踪开发状态）
│   ├── MONTAGE.md          # 混剪需求 + 处理契约 + 方案
│   ├── DEDUP.md            # 去重需求 + 8 大模块 + 实测标定
│   └── ENGINEERING.md      # 工程限制与验证记录（不管理 Bug/Todo）
├── pycut/                  # 主包
│   ├── cli.py              # 命令注册表 + 路由
│   ├── tui.py              # 终端 TUI（导航 / 引导式处理向导）
│   ├── tui_runner.py       # TUI 中的子进程运行、实时日志和结果页
│   ├── settings.py         # 项目 .env 读取、校验与原子保存
│   ├── web_config.py       # 本机 Web 配置页（目录与主备网关）
│   ├── config.py           # 全部可调常量与路径（标定值勿乱改）
│   ├── core/
│   │   ├── scan.py         # 目录/视频扫描（选目录、递归数视频、列视频）
│   │   ├── utils.py        # 日志 / 格式化 / 命令执行
│   │   ├── runtime.py      # 中断标志 + 信号
│   │   ├── ffmpeg.py       # 视频探测 / 启动自检
│   │   ├── llm.py          # LLM 客户端（纯 urllib，主备网关自动容灾）
│   │   ├── scene.py        # 场景检测 + 分段
│   │   ├── faces.py        # Haar 人脸分筛 + 三态判定
│   │   ├── subtitle.py     # 字幕检测 + 全宽矩形马赛克
│   │   ├── slice_pipeline.py   # 切片编排（场景→人脸→字幕）
│   │   ├── dedup.py        # 去重滤镜链 + 编排 + 报告
│   │   ├── dedup_presets.py # 三档预设 + seed 随机化参数
│   │   ├── fingerprint.py  # pHash + PSNR 自检
│   │   ├── montage_source.py   # 步骤A：语音节奏切槽
│   │   ├── montage_clips.py    # 步骤B：素材扫描 / 挑片 / 拼接
│   │   ├── montage_pip.py      # 步骤C：画中画（含四角阴影）
│   │   ├── montage_sticker.py  # 贴纸叠加（半透明 + 边缘羽化）
│   │   └── montage_subtitles.py# 步骤D：SRT→ASS→烧录
│   └── commands/
│       ├── slice_cmd.py / cover_cmd.py / dedup_cmd.py / montage_cmd.py
│       └── montage_pipeline.py  # 混剪五步编排
├── tools/verify.py         # 切片流水线独立质检
└── tests/                  # 回归测试与需明确启用的设备 smoke
```

## 运行环境

- **解释器**：通过 `./run.sh` 自动选择 Python 3，安装并校验 OpenCV 4.x；当前环境支持 Linux、macOS 和 Windows 的终端入口。
- **ffmpeg**：使用 PATH 中的 ffmpeg/ffprobe；macOS 可优先使用 Homebrew 版本。可通过 `PYCUT_FFMPEG` / `PYCUT_FFPROBE` 覆盖工具路径；Linux/Windows 使用 libx264，macOS 使用 VideoToolbox。
- **素材目录**：默认使用项目下 `materials/source`（切片源）、`materials/reference/1.mp4`（爆款视频及同名字幕）和 `materials/background`（背景片段），不依赖个人 Mac 挂载盘。目录统一从项目根 `.env` 读取，CLI 参数可临时覆盖。

## 快速开始

```bash
./run.sh                 # 自动装依赖 -> 读 .env -> 进入 TUI（推荐）
./run.sh slice           # 或直接跑批量切片
```

`run.sh` 会：选好自带 cv2 的解释器 → 缺 `cv2/numpy` 时按 `requirements.txt` 安装
（已装则跳过，不重复联网）→ 校验 cv2 → 打印生效网关（**不打印密钥**）→ 运行入口。
没有 `.env` 时自动由 `.env.example` 生成并提示填密钥。

Windows 在 CMD 中使用 `run.cmd`（无参数进入 TUI），例如 `run.cmd slice --file example.mp4`。脚本检查 Python、OpenCV、终端组件及 ffmpeg/ffprobe；缺少 Python 或 FFmpeg 时通过 winget 安装，Python 依赖按 `requirements.txt` 安装，包含 `windows-curses`。安装后刷新 PATH 并复检，失败停止启动；缺少 winget 时需先安装 Microsoft App Installer。脚本不会显示密钥，CLI 退出码原样返回。Windows 脚本需在真实 Windows 环境验收，Linux 上的静态检查不能替代运行验证。

`run.sh` 负责 Linux 和 macOS，Windows 使用上述 `run.cmd`。启动时检查 ffmpeg/ffprobe 能否运行，缺少时自动用系统包管理器安装 FFmpeg，并在安装后重新检查。macOS 使用 Homebrew；Linux 支持 apt、dnf、yum、pacman、zypper 和 apk，普通用户安装系统包时会使用 sudo。Python 缺失时也尝试安装。系统 Python 限制全局 pip 安装时，自动使用项目 `.venv`；依赖齐全时直接启动。安装失败或无可用安装器会明确报错并停止。`PYCUT_FFMPEG` / `PYCUT_FFPROBE` 自定义路径必须有效。

### 各功能常用命令

```bash
# 1. 切片：场景分段 -> 人脸分流 -> 字幕擦除
./run.sh slice                          # 批量（源目录 config.SRC_DIR）
./run.sh slice --file "视频名" --dry-run # 试跑，只看切点不产出
./run.sh slice --no-sub                 # 这次不擦字幕
./run.sh cover --all --force            # 补跑字幕擦除

# 2. 去重：分层扰动 + pHash/PSNR 自检
./run.sh dedup --all --preset standard  # 批量去重
./run.sh dedup --file "切片.mp4" --preset aggressive --border --badge
./run.sh dedup --file "长视频.mp4"       # 支持单个长视频（360s 已实测）
./run.sh dedup --all --check            # 只自检，不产出

# 3. 混剪：话术基准 -> 随机混剪 -> 画中画 -> 字幕 -> 贴原声
./run.sh montage                        # 全流程
./run.sh montage --probe                # 先看素材库规模（竖屏/横屏/时长）
./run.sh montage --seed 2026            # 固定结果可复现
./run.sh montage --sticker-chance 0.9   # 贴纸更密
./run.sh montage --no-sub --no-sticker   # 只要干净画面
```

> 每个命令的完整参数见 `./run.sh <命令> --help`。

## 固定生成规则

切片按镜头检测，默认 2–15 秒，不做语义识别。混剪固定 720×1280（9:16）、30fps，右上角默认 216×384 画中画。背景与画中画独立随机选择、标准档扰动并静音；最后贴回爆款原声，字幕使用同名 SRT 原时间轴。

默认在线下载 Twemoji PNG 贴纸（Twitter 及贡献者，CC BY 4.0），并加入 0.2 秒段间淡化；`--sticker-dir` 可使用本地图片，`--no-sticker` 和 `--seam-fade 0` 可关闭。下载失败、素材重复或不足会拒绝发布。`--fission N` 指定每支爆款的生成数量，默认 1；成片旁 JSON 保存实际参数，暗水印提取结果仍需独立验证。

默认保留基准原声；基准没有音轨时明确报错，不发布静音成片。只有显式指定 `--no-audio` 才允许静音输出。处理中断或字幕、音轨处理失败时，不覆盖已有成片。

macOS 优先使用 Homebrew ffmpeg-full 与 VideoToolbox；其他环境使用 PATH 中的 ffmpeg/ffprobe 与 libx264。可用环境变量 `PYCUT_FFMPEG` / `PYCUT_FFPROBE` 指定工具路径。OpenCV 限定 4.x，以保留 Haar 级联支持。

## 网关配置

网关只有两个：**主网关 `main`**（默认 DeepSeek 官方，公网稳定、支持图像）和
**备用网关 `backup`**。两者都是 OpenAI 兼容协议，主网关可换成任意其它网关 —— 与厂商无关。

```bash
cp .env.example .env
# 编辑 .env，填入主网关密钥：
#   PYCUT_MAIN_KEY=sk-xxxx
```

`.env` 已在 `.gitignore` 中，不会被提交；密钥从 `.env` 或环境变量读取，代码里不存任何明文密钥。
真实 shell 环境变量优先级高于 `.env`。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PYCUT_MAIN_URL` | `https://api.deepseek.com/v1` | 主网关地址（OpenAI 兼容） |
| `PYCUT_MAIN_KEY` | 无（必填） | 主网关密钥（旧名 `DEEPSEEK_API_KEY` 兼容） |
| `PYCUT_MAIN_MODEL` | `deepseek-flash` | 主网关模型名 |
| `PYCUT_BACKUP_URL` | 同主网关 | 备用网关地址 |
| `PYCUT_BACKUP_KEY` | 无 | 备用网关密钥（留空 = 不启用自动切换） |
| `PYCUT_BACKUP_MODEL` | 同主网关 | 备用网关模型名 |

**自动切换**：主网关连续失败 3 次会自动切到备用网关并粘住后续调用（日志会提示）。
`--gateway {main,backup}` 可手动指定起始网关，`--no-fallback` 关闭自动切换；
`--llm-url / --llm-key / --llm-model` 临时覆盖（显式指定时不启用自动切换）。

## 用法

参数**既支持命令行，也支持终端 TUI 交互**（两条等价）。

### 方式一：终端 TUI（交互，推荐）

```bash
./run.sh          # 无参数且为交互终端 -> 进入 TUI
./run.sh tui      # 显式进入 TUI
```

启动 TUI 时自动打开本机 Web 配置页。主界面保留切片、字幕擦除、去重、混剪四个处理功能；按 **W** 可重新打开配置网页。网页仅编辑项目根 `.env`，视频任务与实时日志仍在终端执行。

直接运行 `./run.sh`（Windows 为 `run.cmd`）即可自动打开配置网页并进入 TUI，无需额外配置命令。

配置也可直接编辑 `.env`，无需重启 TUI；每次进入任务重新读取并检查路径。Web 服务只监听 `127.0.0.1`，退出程序即关闭。浏览器不能自动打开时，复制终端显示的本机 URL。

| 配置项 | 用途 |
|---|---|
| `PYCUT_SOURCE_DIR` | 待切片源视频 |
| `PYCUT_REFERENCE_DIR` | 爆款视频及同名 SRT |
| `PYCUT_CLIP_DIR` | 背景切片库，也供去字幕、去重读取；切片写入本次任务子目录 |
| `PYCUT_PIP_DIR` | 画中画切片库 |
| `PYCUT_INTRO_DIR` | 前贴片段库，每支混剪随机选一个完整片段放在最前面 |
| `PYCUT_OUTPUT_DIR` | 去重、混剪输出根目录，可自动创建 |
| `PYCUT_STICKER_DIR` | 可选本地贴纸；空则沿用在线贴纸 |

输入目录需存在，支持中文、空格、`~` 和相对路径；相对路径以项目根目录为准。Web 保存保留其他配置与注释，密钥不回显、留空不改已有值。主/备用 LLM 网关的地址、模型和 Key 也在同一页面设置；真实环境变量仍优先于 `.env`。

| 功能 | 向导流程 |
|---|---|
| 切片 | 检查配置 → 统计源视频 → 选择字幕/去重开关 → 确认执行 |
| 字幕擦除 | 检查配置与 LLM → 统计切片 → 确认矩形马赛克 |
| 去重 | 检查配置 → 统计切片 → 确认执行 |
| 混剪 | 检查配置 → 多选爆款视频并设置裂变数 → 确认执行 |

目录不再通过终端选择。缺配置时引导网页填写，确认页不会直接启动任务，必须按 Enter。Esc 返回，主菜单只按 Q 退出。混剪保留空格勾选、E 编辑裂变数（1～99）、A 全选，返回多选时保留选择和数量。

同一支爆款视频的批量裂变只探测参考视频、扫描背景/画中画素材和下载贴纸一次；每支仍使用不同 seed 独立选片、扰动和编码。共享准备结果仅在本次命令内有效，任务结束清理在线贴纸临时目录，新任务重新读取素材。前贴为空或画中画素材不可用时，在正文编码前报错。

前贴复用混剪的去重扰动及贴纸处理，保留原字幕与原声（无声前贴补静音）；在正文字幕烧录、原声贴回完成后拼接，成片总时长为前贴加正文。报告记录前贴来源、时长、贴纸与去重 seed。前贴处理或拼接失败时不发布成片。CLI 可用 `--intro-dir ""` 明确禁用前贴，`--no-sticker` 可关闭贴纸。

切片预览与执行一致，仅统计三层内 MP4。非交互环境自动回退 CLI 帮助；显式命令行输入路径保持兼容，省略输入时检查并读取 `.env`。

### 方式二：命令行

```bash
# 试跑单个视频：只检测分段，不产出（先看切点是否合理）
./run.sh slice --file "财源滚滚来__ (1)" --dry-run

# 正式切片（默认走主网关）
./run.sh slice

# 直接用备用网关起步 / 关闭自动切换
./run.sh slice --gateway backup
./run.sh slice --no-fallback

# 只处理前 3 个视频
./run.sh slice --limit 3

# 补跑 / 重跑字幕擦除（--force 忽略已遮盖标记）
./run.sh cover --all
```

## 当前功能：slice

流水线三步，产出落到配置的切片目录：

1. **场景分段**：ffmpeg `scene` 分数检测切点，过短段合并（≥2s），超长段切分。
2. **人脸分流**：Haar 快速分筛，仅 ambiguous 段送 LLM 兜底；按「有人脸 / 无人脸」
   分目录落盘（`有人脸/`、`无人脸/`）。
3. **字幕遮盖**：LLM 定位字幕上下边界 → 按字幕高度增加少量余量 → 由 FFmpeg 直接覆盖视频全宽的矩形马赛克，避免整帧传回 Python；区域外像素不参与马赛克，音轨直接保留。幂等，标记 `<切片>.covered.json`。遮盖后抽帧复检，仅认可 version=2 的完成标记；旧标记须重做。

清单：成片输出目录下 `切片_清单/切片清单_<时间戳>.csv`（utf-8-sig）+ `.json`。

## 扩展能力与需求文档

长期产品需求见 [docs/ROADMAP.md](docs/ROADMAP.md)：段间转场、去重增强、AI 级字幕修复及 ASR 语义选片；任务、Bug、测试、负责人和验收统一使用 [Linear 项目](https://linear.app/gengyun/project/py-cut-development-963524cf0d78)，不在 Markdown 跟踪 Todo、负责人或进度。工程技术限制与复现记录见 [docs/ENGINEERING.md](docs/ENGINEERING.md)。
已实现的三大功能（切片 / 去重 / 混剪）详见各文档：
去重需求与实测标定见 [docs/DEDUP.md](docs/DEDUP.md)，
混剪五步流程见 [docs/MONTAGE.md](docs/MONTAGE.md)。

## 给协作者

任务从 [Linear 项目](https://linear.app/gengyun/project/py-cut-development-963524cf0d78) 查找和领取，先读 [AGENTS.md](AGENTS.md)。GitHub 只管理代码、PR、Review、CI 和合并；历史 Issues 不再用于领取，不启用 Issues Sync。Markdown 仅记录长期知识。

普通 PR 使用必要轻量 CI；完整合成媒体回归手动运行 Actions 的 `TUI regression`（`workflow_dispatch` 即完整合成媒体模式）。发布、重要接口/架构或安全变更，以及明确要求全量测试时，执行对应完整验证；不得绕过必需检查。

使用启动器输出的 Python 解释器运行 `-m pytest tests/ -q`。普通回归不访问真实 LLM；`tests/test_llm_safety.py` 使用模拟响应检查解析、重试和主网关模式。真实媒体检查需设置 `PYCUT_TEST_MEDIA=1`，并在 `materials/source` 放置测试 MP4；模拟通过不能代替真实模型识别准确率验证。

切片后可独立验证自定义目录：

```bash
python3 tools/verify.py --video example.mp4 --source-dir materials/source \
  --out-dir work/slices --manifest "work/slices/清单/切片清单_<时间戳>.csv"
```

`work/`、`materials/` 和 `.env` 均不提交；测试素材、成片和密钥留在本地。

改代码前先读 **AGENTS.md**。新增功能 = 在 `pycut/commands/` 加一个命令模块并在
`cli.py` 的 `COMMANDS` 注册表登记；复用 `pycut/core/` 的成熟能力，不要在命令层重复造轮子。

### 输入与输出目录约定

混剪向导依次选择 **爆款素材目录、切片目录、画中画目录**，再选择爆款视频及各自生成数量。输出无需另选目录；日期按运行机器本地时间生成。

```text
工作目录/
├── 源视频/
├── 切片/
│   ├── 有人脸/原文件名_1.mp4
│   ├── 无人脸/原文件名_2.mp4
│   └── 清单/
├── 字幕查重/有人脸/原文件名_1.mp4
├── 去重/有人脸/原文件名_1.mp4
├── 爆款素材/爆款文件名.mp4
└── 混剪/MMDD/爆款文件名_1.mp4
```

切片不再按视频名称建子目录。字幕擦除和去重保留输入中的有人脸／无人脸分组及已有 `_n` 编号；未分组的输入平铺到输出根目录，未编号的文件追加 `_1`。同名输出冲突会在处理前报错。`--out-dir` 仍可覆盖默认位置，去重只有显式 `--in-place` 才覆盖源文件。

主页面使用单一醒目的 PYCUT 字标；顶部只显示标识和版本，功能说明留在菜单，当前流程及键盘用法集中在底部，不重复显示。小窗口自动使用紧凑字标，至少支持 60×14 终端。

### 视频任务进度与结果

处理页优先显示当前流程、阶段进度、通过/失败/跳过数量、耗时和结果目录，详细文件日志默认仅显示最近三行，按 L 展开/收起。切片先编码，再检测、修复及抽帧复检字幕；未完成字幕阶段的切片不是最终处理结果。选择后继去重时，字幕处理失败会阻断去重；切片向导的后继去重会刷新本次源视频的成品，避免修复字幕后继续复用旧文件。去重结果位于同级 `去重/`，界面显示实际 pHash/PSNR 自检与是否跳过，不把旧产物当作本次新处理。

字幕区域按采样帧并集合并，避免漏掉位置变化的文字。修复成品仍检出字幕或复检失败时返回失败，不写新版完成标记；旧版未复检标记会重新处理。复检是抽帧检测，不代表逐帧 OCR；艺术字等经典修复算法的限制仍见技术文档。
