# pycut —— 短视频智能处理工具箱

统一处理抖音 / 微信等短视频的本地工具箱。提供三个功能：**字幕擦除**、**去重**、**混剪**。视频切片由用户自行准备，程序不再提供切片入口。

## 功能一览

| 命令 | 做什么 | 产物位置 |
|---|---|---|
`cover` | 对用户提供的切片擦除字幕 | 源目录同级 `字幕查重/`，保留源文件 |
`dedup` | 调整视频画面，降低相似度 | 各输入目录同级 `去重/` |
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
│   │   ├── subtitle.py     # 字幕检测 + 全宽单层矩形毛玻璃
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
│       ├── cover_cmd.py / dedup_cmd.py / montage_cmd.py
│       └── montage_pipeline.py  # 混剪五步编排
├── tools/verify.py         # 切片流水线独立质检
└── tests/                  # 回归测试与需明确启用的设备 smoke
```

## 运行环境

- **解释器**：通过 `./run.sh` 自动选择 Python 3，安装并校验 OpenCV 4.x；当前环境支持 Linux、macOS 和 Windows 的终端入口。
- **ffmpeg**：使用 PATH 中的 ffmpeg/ffprobe；macOS 可优先使用 Homebrew 版本。可通过 `PYCUT_FFMPEG` / `PYCUT_FFPROBE` 覆盖工具路径；Linux/Windows 使用 libx264，macOS 使用 VideoToolbox。
- **素材目录**：用户配置话术根目录（如 `话术/a/1.mp4`、`话术/a/1.srt`），并提供混剪切片、画中画切片与可选前贴素材，不依赖个人 Mac 挂载盘。目录统一从项目根 `.env` 读取，CLI 参数可临时覆盖。

## 快速开始

```bash
./run.sh                 # 自动装依赖 -> 读 .env -> 进入 TUI（推荐）
./run.sh cover           # 对已配置的用户切片做字幕擦除
```

`run.sh` 会：选好自带 cv2 的解释器 → 缺 `cv2/numpy` 时按 `requirements.txt` 安装
（已装则跳过，不重复联网）→ 校验 cv2 → 打印生效网关（**不打印密钥**）→ 运行入口。
没有 `.env` 时自动由 `.env.example` 生成并提示填密钥。

Windows 在 CMD 中使用 `run.cmd`（无参数进入 TUI），例如 `run.cmd cover --file example.mp4`。脚本检查 Python、OpenCV、终端组件及 ffmpeg/ffprobe；缺少 Python 或 FFmpeg 时通过 winget 安装，Python 依赖按 `requirements.txt` 安装，包含 `windows-curses`。安装后刷新 PATH 并复检，失败停止启动；缺少 winget 时需先安装 Microsoft App Installer。脚本不会显示密钥，CLI 退出码原样返回。Windows 脚本需在真实 Windows 环境验收，Linux 上的静态检查不能替代运行验证。

`run.sh` 负责 Linux 和 macOS，Windows 使用上述 `run.cmd`。启动时检查 ffmpeg/ffprobe 能否运行，缺少时自动用系统包管理器安装 FFmpeg，并在安装后重新检查。macOS 使用 Homebrew；Linux 支持 apt、dnf、yum、pacman、zypper 和 apk，普通用户安装系统包时会使用 sudo。Python 缺失时也尝试安装。系统 Python 限制全局 pip 安装时，自动使用项目 `.venv`；依赖齐全时直接启动。安装失败或无可用安装器会明确报错并停止。`PYCUT_FFMPEG` / `PYCUT_FFPROBE` 自定义路径必须有效。

### 各功能常用命令

```bash
# 1. 字幕擦除：用户自行提供切片
./run.sh cover
./run.sh cover --src-dir "/素材/切片一" --src-dir "/素材/切片二"
./run.sh cover --file "切片.mp4" --force

# 2. 去重：分层扰动 + pHash/PSNR 自检
./run.sh dedup --all --preset standard  # 批量去重
./run.sh dedup --file "切片.mp4" --preset aggressive --border --badge
./run.sh dedup --file "长视频.mp4"       # 支持单个长视频（360s 已实测）
./run.sh dedup --all --check            # 只自检，不产出

# 3. 混剪：话术基准 -> 随机混剪 -> 画中画 -> 字幕 -> 贴原声
./run.sh montage --source "话术/a/1.mp4" # 全流程
./run.sh montage --probe                # 先看素材库规模（竖屏/横屏/时长）
./run.sh montage --source "话术/a/1.mp4" --seed 2026 # 固定结果可复现
./run.sh montage --sticker-chance 0.9   # 贴纸更密
./run.sh montage --no-sub --no-sticker   # 只要干净画面
```

> 每个命令的完整参数见 `./run.sh <命令> --help`。

## 固定生成规则

切片由用户手动提供，所有素材目录递归读取。混剪固定 720×1280（9:16）、30fps，右上角默认 216×384 画中画。背景与画中画独立随机选择、标准档扰动并静音；最后贴回爆款原声，字幕使用同名 SRT 原时间轴。

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

启动 TUI 时只启动本机 Web 配置服务，不自动打开浏览器。所有正常窗口页面顶部统一显示原五行块状 PYCUT 大 Logo 和素材目录/LLM设置区；宽屏左右排列，窄屏上下排列。设置区提供完整地址及“复制地址到浏览器打开”的提示，底部分割线与任务内容分开；按 **W** 可查看访问提示，在浏览器中手动输入地址。网页仅编辑项目根 `.env`，视频任务与实时日志仍在终端执行。

直接运行 `./run.sh`（Windows 为 `run.cmd`）即可进入 TUI 并显示配置地址，无需额外配置命令。

配置也可直接编辑 `.env`，无需重启 TUI；每次进入任务重新读取并检查路径。Web 服务只监听 `127.0.0.1`，退出程序即关闭。需要配置时，复制终端顶部显示的本机 URL 到浏览器。

| 配置项 | 用途 |
|---|---|
| `PYCUT_REFERENCE_DIR` | 话术根目录，每个子文件夹一个视频和同名 SRT |
| `PYCUT_CLIP_DIR` | 混剪切片目录列表，也供字幕擦除、去重读取 |
| `PYCUT_PIP_DIR` | 画中画切片目录列表；留空或取消叠加时不读取 |
| `PYCUT_INTRO_DIR` | 可选前贴目录列表；全部目录没有视频时跳过 |
| `PYCUT_OUTPUT_DIR` | 混剪成片输出根目录，可自动创建；独立处理另存输入同级 |
| `PYCUT_STICKER_DIR` | 可选本地贴纸；空则沿用在线贴纸 |

混剪切片、画中画和前贴均可点击“添加目录”，每行可点击“选择…”打开系统原生文件夹窗口，也可手动编辑或移除；合并递归扫描所有子目录，重叠路径和同一文件不会重复读取。`.env` 旧单路径保持兼容，多目录以单行 JSON 数组保存，例如 `PYCUT_CLIP_DIR='["/素材/切片一","/素材/切片二"]'`，不使用逗号或分号拆路径。原生窗口在运行 pycut 的机器上打开；没有图形桌面时可手动输入路径，取消选择不修改目录。CLI 对应参数可重复填写。话术列表显示相对路径以区分 `a/1.mp4` 与 `b/1.mp4`，字幕始终从视频同目录按同名匹配。

输入目录需存在，支持中文、空格、`~` 和相对路径；相对路径以项目根目录为准。Web 保存保留其他配置与注释，密钥不回显、留空不改已有值。主/备用 LLM 网关的地址、模型和 Key 也在同一页面设置；真实环境变量仍优先于 `.env`。

| 功能 | 向导流程 |
|---|---|
| 字幕擦除 | 检查配置与 LLM → 统计切片 → 确认单层矩形毛玻璃 |
| 去重 | 检查配置 → 统计切片 → 确认执行 |
| 混剪 | 检查配置 → 多选话术子目录视频并设置裂变数 → 确认执行 |

目录不再通过终端选择。缺配置时引导网页填写，确认页不会直接启动任务，必须按 Enter。Esc 返回，主菜单只按 Q 退出。混剪保留空格勾选、E 编辑裂变数（1～99）、A 全选，返回多选时保留选择和数量。

同一支爆款视频的批量裂变只探测参考视频、扫描背景/画中画素材和下载贴纸一次；每支仍使用不同 seed 独立选片、扰动和编码。共享准备结果仅在本次命令内有效，任务结束清理在线贴纸临时目录，新任务重新读取素材。画中画素材不可用或前贴目录读取失败时，在正文编码前报错；前贴未配置或目录没有视频时自动跳过。

前贴可选，未配置或目录没有视频时直接输出正文；目录读取、素材处理或拼接出错时仍不发布成片。非空前贴复用混剪的去重扰动及贴纸处理，保留原字幕与原声（无声前贴补静音）；在正文字幕烧录、原声贴回完成后拼接，成片总时长为前贴加正文。报告记录前贴来源、时长、贴纸与去重 seed，跳过时前贴为 null、字幕偏移为 0。CLI 可用 `--intro-dir ""` 明确禁用前贴，`--no-sticker` 可关闭贴纸。

正文直接复制爆款视频的原声音轨，不做音量、音高、速度处理或补长；超出画面长度的音频仅截取所需时长，短音轨不会截短视频。只有原音频编码无法装入输出容器时才转码 AAC，并显示兼容提示。启用前贴拼接时，为统一音轨格式会转码 AAC，正文音频来源仍是爆款原声。

切片预览与执行一致，仅统计三层内 MP4。非交互环境自动回退 CLI 帮助；显式命令行输入路径保持兼容，省略输入时检查并读取 `.env`。

### 方式二：命令行

```bash
./run.sh cover --src-dir "/素材/切片一" --src-dir "/素材/切片二"
./run.sh dedup --src-dir "/素材/切片一" --preset standard
./run.sh montage --source "/话术/a/1.mp4" --fission 5
./run.sh cover --gateway backup --no-fallback
```

话术目录中只有一支视频时，CLI 可省略 `--source`；多支时通过 TUI 多选或用 `--source` 明确选择。

`slice` 命令和切片向导已移除；切片来自用户准备的文件。字幕擦除使用视觉模型检测字幕边界，由 FFmpeg 叠加单层毛玻璃并保留原声；不是恢复字幕后面的原始画面。

## 扩展能力与需求文档

长期产品需求见 [docs/ROADMAP.md](docs/ROADMAP.md)：段间转场、去重增强、AI 级字幕修复及 ASR 语义选片；任务、Bug、测试、负责人和验收统一使用 [Linear 项目](https://linear.app/gengyun/project/py-cut-development-963524cf0d78)，不在 Markdown 跟踪 Todo、负责人或进度。工程技术限制与复现记录见 [docs/ENGINEERING.md](docs/ENGINEERING.md)。
三个处理功能（字幕擦除 / 去重 / 混剪）详见各文档：
去重需求与实测标定见 [docs/DEDUP.md](docs/DEDUP.md)，
混剪五步流程见 [docs/MONTAGE.md](docs/MONTAGE.md)。

## 给协作者

任务从 [Linear 项目](https://linear.app/gengyun/project/py-cut-development-963524cf0d78) 查找和领取，先读 [AGENTS.md](AGENTS.md)。GitHub 只管理代码、PR、Review、CI 和合并；历史 Issues 不再用于领取，不启用 Issues Sync。Markdown 仅记录长期知识。

普通 PR 使用必要轻量 CI；完整合成媒体回归手动运行 Actions 的 `TUI regression`（`workflow_dispatch` 即完整合成媒体模式）。发布、重要接口/架构或安全变更，以及明确要求全量测试时，执行对应完整验证；不得绕过必需检查。

使用启动器输出的 Python 解释器运行 `-m pytest tests/ -q`。普通回归不访问真实 LLM；`tests/test_llm_safety.py` 使用模拟响应检查解析、重试和主网关模式。真实媒体检查需设置 `PYCUT_TEST_MEDIA=1`，并在 `materials/source` 放置测试 MP4；模拟通过不能代替真实模型识别准确率验证。

`work/`、`materials/` 和 `.env` 均不提交；测试素材、成片和密钥留在本地。

改代码前先读 **AGENTS.md**。新增功能 = 在 `pycut/commands/` 加一个命令模块并在
`cli.py` 的 `COMMANDS` 注册表登记；复用 `pycut/core/` 的成熟能力，不要在命令层重复造轮子。

### 输入与输出目录约定

Web 配置话术根目录及三组素材目录，TUI 多选话术视频、统一输入每个话术的裂变数量，再确认执行。所有素材目录递归读取。成片总输出目录可配置，内部保留话术相对父目录，避免多个 `1.mp4` 覆盖。

```text
工作目录/
├── 话术/a/1.mp4
├── 话术/a/1.srt
├── 话术/b/1.mp4
├── 话术/b/1.srt
├── 用户切片一/有人脸/片段_1.mp4
├── 用户切片二/子目录/片段_2.mp4
├── 字幕查重/有人脸/片段_1.mp4
├── 去重/有人脸/片段_1.mp4
└── 输出/混剪/
    ├── a/1_1.mp4
    └── b/1_1.mp4
```

字幕擦除与去重默认另存各输入根目录同级的 `字幕查重`、`去重`，保留既有人脸分组及编号；同名冲突会在编码前报错，不覆盖用户素材。`--out-dir` 仍可显式指定。混剪默认采用配置的输出根目录，未配置时使用话术目录同级 `混剪/MMDD`。

主页面使用单一醒目的 PYCUT 字标；所有页面顶部统一显示标识和实际本机设置地址，功能说明留在菜单，当前流程及键盘用法集中在底部，不重复显示。高度至少20行的正常窗口统一使用原五行块状大 Logo，宽屏在右侧说明如何设置素材目录和模型，窄屏在下方显示访问说明；共用分割线下方显示任务内容。高度不足时使用紧凑字标，至少支持 60×14 终端，设置地址保持完整。

### 视频任务进度与结果

字幕擦除与去重显示阶段进度、成功/待复查/失败/跳过数量、耗时和结果目录；混剪只显示整批计划成片数、已生成数与一个总进度条，报错自动显示错误日志。完整日志按 L 展开。无字幕是正常结果；识别不确定、超时或复检残留列为待复查，不标记擦除成功。编码失败与中断仍停止依赖任务。

字幕区域按采样帧并集合并，避免漏掉位置变化的文字。遮盖成品仍可能检出字幕或复检不确定时标记待复查，不写新版完成标记；旧版未复检标记会重新处理。复检是抽帧检测，不代表逐帧 OCR；识别准确率与毛玻璃遮盖效果须对实际素材检查。每轮多帧识别共用30秒等待预算，单次HTTP最多15秒，后台等待时每秒更新进度；不能用“待复查”表示已经清除字幕。

字幕定位的5张采样帧合并到一条多图视觉请求，成品3帧复检另用一条请求；保持各帧独立坐标及跨帧并集，不减少抽样。视觉网关须支持 OpenAI 兼容多图输入及逐帧 JSON 返回，返回不完整时提示待复查。识别并发可通过 CLI 调整；进度显示字幕并发、阶段用时及实际请求数。毛玻璃用一层连续高斯模糊，仅叠加一次，不做像素化缩放；模糊遮盖无法恢复字幕下面的原始画面。

混剪向导依次为：选择话术视频 → 统一输入每个话术的裂变数量（1～99）→ 汇总确认执行。数量是必经输入步骤，Esc 返回保留已填数；所有选中话术应用相同数量，例如选中 10 个话术、输入 5，则共生成 50 支成片；最终确认显示每支视频与总成片数量。

三类任务结束后停留在结果页，明确显示完成、待复查、失败或中断，以及各流程的真实阶段计数和结果目录。Enter/Esc 确认返回主菜单，Q退出；失败会阻断后续依赖任务，阶段计数不相加冒充视频总数。

混剪进度按整批成片数显示：所选话术数 × 统一裂变数为计划总数，只将完整生成并检查的成片计入已完成，内部合成步骤不计入总进度。正常运行只显示总进度条，失败时自动显示错误日志；L 可查看完整日志。
