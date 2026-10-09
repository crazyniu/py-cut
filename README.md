# pycut —— 短视频智能处理工具箱

统一处理抖音 / 微信等短视频的本地工具集。当前已落地**切片**功能，后续将扩展混剪、转场贴纸、平台查重等。

## 目录结构

```
pycut/
├── pycut.py                # 统一入口（命令行，后续演进为 TUI）
├── pyproject.toml          # 包元数据
├── .gitignore
├── AGENTS.md               # 给 AI 协作的规范（务必先读，AI 工具自动识别）
├── README.md
├── .env.example            # 密钥模板（复制为 .env 并填 DEEPSEEK_API_KEY）
├── docs/
│   ├── ROADMAP.md          # 后续功能规划（混剪 / 转场 / 查重）
│   └── DEDUP.md            # 去重（过查重）需求与技术方案全集（规划中，待开发）
├── pycut/                  # 主包
│   ├── cli.py              # CLI 路由（命令注册表，TUI 友好）
│   ├── tui.py              # 终端 TUI（任务设置页 + 高级参数页）
│   ├── config.py           # 全局常量 / 网关（主/备） / 路径
│   ├── core/               # 核心处理（与命令编排解耦）
│   │   ├── utils.py        # 日志 / 格式化 / 命令执行
│   │   ├── runtime.py      # 中断标志 + 信号
│   │   ├── ffmpeg.py       # 视频探测 / 启动自检
│   │   ├── llm.py          # OpenAI 兼容 LLM 客户端（纯 urllib）
│   │   ├── scene.py        # 场景检测 + 分段
│   │   ├── faces.py        # Haar 人脸分筛 + 三态判定
│   │   ├── subtitle.py     # 字幕检测 + 真实擦除
│   │   └── slice_pipeline.py  # 切片编排（场景→人脸→字幕）
│   └── commands/           # 命令实现（每个暴露 register / run）
│       ├── slice_cmd.py    # slice 命令
│       └── cover_cmd.py    # cover 命令（补跑字幕擦除）
├── tools/
│   └── verify.py           # 切片流水线独立质检
└── tests/
    └── test_smoke.py       # 冒烟测试
```

## 运行环境

- **解释器**：必须用自带 `cv2 4.13.0` 的托管解释器
  `/Users/apple/.workbuddy/binaries/python/versions/3.13.12/bin/python3`。
  **不要进 venv**（会丢失 cv2）。
- **ffmpeg**：homebrew 安装的 `ffmpeg-full`（含 videotoolbox 硬件编码），路径见 `config.py`。

## 快速开始

```bash
./run.sh                 # 自动装依赖 -> 读 .env -> 进入 TUI
./run.sh slice           # 或直接跑批量切片
```

`run.sh` 会：选好自带 cv2 的解释器 → 缺 `cv2/numpy` 时按 `requirements.txt` 安装
（已装则跳过，不重复联网）→ 校验 cv2 → 打印生效网关（**不打印密钥**）→ 运行入口。
没有 `.env` 时自动由 `.env.example` 生成并提示填密钥。

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

- 方向键选功能（slice / cover），回车进入**任务设置页**
- 任务设置页只放真正要你决定的项（如 slice：指定视频 / **切片时是否擦除字幕** / 试跑 / 重跑），
  最后一项 `▶▶ 开始执行` 回车即运行
- `↑/↓` 移动，`Enter` 编辑或切换，`A` 切到**高级参数页**（网关、并发、阈值等日常不用管），
  `R` 运行，`Q` 返回/退出
- 底部实时显示「将执行: …」的完整命令预览；非交互环境（管道）自动回退命令行帮助

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

## 后续功能

见 [docs/ROADMAP.md](docs/ROADMAP.md)：混剪视频、转场贴纸、平台查重。
其中**去重（过查重）**的完整需求与技术方��已整理为 [docs/DEDUP.md](docs/DEDUP.md)
（8 大模块、预设档位、随机化、pHash/PSNR 自检闭环），**当前只做规划、尚未开发**。

## 给协作者

改代码前先读 **AGENTS.md**。新增功能 = 在 `pycut/commands/` 加一个命令模块并在
`cli.py` 的 `COMMANDS` 注册表登记；复用 `pycut/core/` 的成熟能力，不要在命令层重复造轮子。
