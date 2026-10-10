# pycut —— 短视频智能处理工具箱

统一处理抖音 / 微信等短视频的本地工具箱。已落地三大功能：**切片**、**去重（过查重）**、**混剪**。

## 功能一览

| 命令 | 做什么 | 产物位置 |
|---|---|---|
`slice` | 把长视频切成多段，分流有人脸/无人脸，擦除字幕 | `~/Desktop/切片_有人脸`、`切片_无人脸` |
`cover` | 只重跑字幕擦除 | 同上（幂等，靠 `.covered.json` 标记） |
`dedup` | 去除重复、相似的视频（过平台查重） | `~/Desktop/去重_输出` |
`montage` | 组合片段生成新视频，可批量裂变 | `~/Desktop/混剪_输出` |

## 目录结构

```
pycut/
├── pycut.py                # 统一入口（命令行 + TUI）
├── run.sh                  # 一键脚本：选解释器 → 装依赖 → 读 .env → 运行
├── requirements.txt        # 依赖（仅 numpy + opencv-python）
├── pyproject.toml          # 包元数据
├── .env / .env.example     # 密钥与网关配置（.env 已 gitignore）
├── .gitignore
├── AGENTS.md               # 给 AI 协作的规范（务必先读，AI 工具自动识别）
├── README.md               # 本文件
├── docs/
│   ├── ROADMAP.md          # 产品能力与演进设计（不跟踪开发状态）
│   ├── MONTAGE.md          # 混剪需求 + 素材实测 + 方案
│   ├── DEDUP.md            # 去重需求 + 8 大模块 + 实测标定
│   └── ISSUES.md           # 工程限制与验证记录（不管理 Bug/Todo）
├── pycut/                  # 主包
│   ├── cli.py              # 命令注册表 + 路由
│   ├── tui.py              # 终端 TUI（导航 / 引导式向导 / 目录选择器）
│   ├── tui_runner.py       # TUI 中的子进程运行、实时日志和结果页
│   ├── config.py           # 全部可调常量与路径（标定值勿乱改）
│   ├── core/
│   │   ├── scan.py         # 目录/视频扫描（选目录、递归数视频、列视频）
│   │   ├── utils.py        # 日志 / 格式化 / 命令执行
│   │   ├── runtime.py      # 中断标志 + 信号
│   │   ├── ffmpeg.py       # 视频探测 / 启动自检
│   │   ├── llm.py          # LLM 客户端（纯 urllib，主备网关自动容灾）
│   │   ├── scene.py        # 场景检测 + 分段
│   │   ├── faces.py        # Haar 人脸分筛 + 三态判定
│   │   ├── subtitle.py     # 字幕检测 + 真实擦除（掩膜+修复）
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

- **解释器**：必须用自带 `cv2 4.13.0` 的托管解释器
  `/Users/apple/.workbuddy/binaries/python/versions/3.13.12/bin/python3`。
  **不要进 venv**（会丢失 cv2）。
- **ffmpeg**：homebrew 安装的 `ffmpeg-full`（含 videotoolbox 硬件编码），路径见 `config.py`。

## 快速开始

```bash
./run.sh                 # 自动装依赖 -> 读 .env -> 进入 TUI（推荐）
./run.sh slice           # 或直接跑批量切片
```

`run.sh` 会：选好自带 cv2 的解释器 → 缺 `cv2/numpy` 时按 `requirements.txt` 安装
（已装则跳过，不重复联网）→ 校验 cv2 → 打印生效网关（**不打印密钥**）→ 运行入口。
没有 `.env` 时自动由 `.env.example` 生成并提示填密钥。

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

## 配置（只需配密钥）

网关只有两个：**主网关 `main`**（默认 DeepSeek 官方，公网稳定、支持图像）和
**备用网关 `backup`**。两者都是 OpenAI 兼容协议，主网关可换成任意其它网关 —— 与厂商无关。

```bash
cp .env.example .env
# 编辑 .env，填入主网关密钥：
#   PYCUT_MAIN_KEY=sk-xxxx
```

`.env` 已在 `.gitignore` 中，不会被提交；密钥**只从 `.env` 读取**，代码里不存任何明文密钥。
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

主界面展示 PYCUT Logo、实际程序版本号（来自 `pycut.__version__`）和 4 个处理功能（切片 / 字幕擦除 / 去重 / 混剪），
选中后进入**引导式向导**，每步只问一个决定。所有操作使用终端原生键盘交互，确认执行后留在 TUI 中显示实时日志、错误结果和后续操作：

| 功能 | 向导流程 |
|---|---|
| 切片 | 选源目录 → 报视频数 → 选「擦除字幕/去重」开关 → 确认执行 |
| 字幕擦除 | 选目录 → 报视频数 → 确认执行（只有擦除） |
| 去重 | 选目录 → 报视频数 → 确认执行（只有去重） |
| 混剪 | 选片段目录 → 选画中画目录 → 选爆款视频目录 → 多选视频+设裂变数 → 必要时选贴纸目录 → 确认执行 |

- **目录选择器**：默认从用户主目录打开；先列 `../`（上一级），再列文件夹，最后显示「使用当前文件夹」。
  `↑/↓` 选择，`Enter` 打开文件夹或确认当前目录，`←`/Backspace 返回上级，`Space` 直接选用当前目录，`~` 返回主目录，`/` 前往根目录（外接磁盘可经 `/Volumes` 访问），`Esc` 回到向导上一步。目录列表不主动隐藏任何文件夹，包含 `.git` 等点开头目录、`Library`/`System` 等系统目录及指向文件夹的符号链接。受操作系统权限限制的目录仍可能无法打开；这不改变视频处理时已有的递归过滤与深度限制。
- **内部素材统计**：各处理向导最多下探 3 层；切片仅统计可处理的 MP4，执行端显式传 `--max-depth 3`，确保确认数量与处理集合一致。CLI 默认仍保持原有无限递归，也可指定 `--max-depth N`
- **混剪多选**：空格勾选，`E` 编辑每支裂变数（1～99），数字 1～9 可快捷设置，`A` 全选/取消；确认页展示总产出数量，输出到桌面 `out` 目录
- **统一快捷键**：`↑/↓` 上下移动，`←` 返回上一级，`→` 进入下一级；`Space` 在混剪视频列表勾选/取消**多个视频**，在目录页仅选择**当前一个文件夹**；`→` 在最终确认页只聚焦操作按钮，必须显式 `Enter` 才会开始处理。
- **视觉与响应**：焦点采用边栏和文字高亮，不再整行反色；页脚按实际终端列宽选择短提示。`Esc` 默认设置为 75ms 识别等待，避免传统 curses 的较长按键延迟；用户自定义 `ESCDELAY` 时不覆盖。
- **返回逻辑**：`Esc` 从确认页返回上一目录步骤；混剪按「确认 → 多选 → 爆款目录 → 画中画目录 → 片段目录 → 主菜单」逐步返回，重新进入多选时保留已选视频和裂变数量。在主菜单按 `Esc`/`←`/Backspace 不会关闭程序，**只允许在主菜单按 `Q` 主动退出**。
- **执行状态**：实时显示每个任务的 CLI 日志；`X` 请求中断，`↑/↓`、`PgUp/PgDn` 翻日志；成功或失败后 `Enter`/`Esc` 返回主菜单、`Q` 退出。前序命令失败后不会继续执行依赖它的后续任务。
- 主菜单 `1`～`4` 可快速进入，`↑/↓` 移动，`Enter` 确认；非交互环境（管道）自动回退命令行帮助.

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

流水线三步，产出落到桌面：

1. **场景分段**：ffmpeg `scene` 分数检测切点，过短段合并（≥2s），超长段切分。
2. **人脸分流**：Haar 快速分筛，仅 ambiguous 段送 LLM 兜底；按「有人脸 / 无人脸」
   分目录落盘（`~/Desktop/切片_有人脸`、`~/Desktop/切片_无人脸`）。
3. **字幕擦除**：LLM 定位叠加文字 → 逐帧文字掩膜 + TELEA 修复（真实擦除，非毛玻璃）
   → 按周边纹理补偿颗粒。幂等，标记 `<切片>.covered.json`。

清单：`~/Desktop/切片_清单/切片清单_<时间戳>.csv`（utf-8-sig）+ `.json`。

## 扩展能力与需求文档

长期产品需求见 [docs/ROADMAP.md](docs/ROADMAP.md)：段间转场、去重增强、AI 级字幕修复及 ASR 语义选片；具体任务统一引用 [GitHub Issues #19](https://github.com/natefox2017/py-cut/issues/19)，不在 Markdown 跟踪 Todo、负责人或进度。工程技术限制与复现记录见 [docs/ISSUES.md](docs/ISSUES.md)。
已实现的三大功能（切片 / 去重 / 混剪）详见各文档：
去重需求与实测标定见 [docs/DEDUP.md](docs/DEDUP.md)，
混剪五步流程见 [docs/MONTAGE.md](docs/MONTAGE.md)。

## 给协作者

改代码前先读 **AGENTS.md**。新增功能 = 在 `pycut/commands/` 加一个命令模块并在
`cli.py` 的 `COMMANDS` 注册表登记；复用 `pycut/core/` 的成熟能力，不要在命令层重复造轮子。
