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
│   └── ROADMAP.md          # 后续功能规划（混剪 / 转场 / 查重）
├── pycut/                  # 主包
│   ├── cli.py              # CLI 路由（命令注册表，TUI 友好）
│   ├── config.py           # 全局常量 / 网关预设 / 路径
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

模型固定用 **DeepSeek**（默认网关，公网稳定、支持图像）。用户只需配置密钥：

```bash
cp .env.example .env
# 编辑 .env，填入你的密钥：
#   DEEPSEEK_API_KEY=sk-xxxx
```

`.env` 已在 `.gitignore` 中，不会被提交；密钥**只从 `.env` 读取**，代码里不存任何明文密钥。
也可直接导出环境变量 `export DEEPSEEK_API_KEY=sk-xxxx`（优先级高于 `.env`）。

网关地址与模型名同样可在 `.env` 配置，默认即 DeepSeek，通常不用改：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | 无（必填） | DeepSeek 密钥 |
| `PYCUT_LLM_URL` | `https://api.deepseek.com/v1` | 网关地址（OpenAI 兼容） |
| `PYCUT_LLM_MODEL` | `deepseek-flash` | 模型名称 |
| `PYCUT_GATEWAY` | `deepseek` | 默认网关（`deepseek` / `local`） |
| `PYCUT_LOCAL_URL/KEY/MODEL` | — | 局域网备用网关，默认不用配 |

命令行 `--gateway / --llm-url / --llm-key / --llm-model` 仍可临时覆盖。

## 用法

参数**既支持命令行，也支持终端 TUI 交互**（两条等价）。

### 方式一：终端 TUI（交互，推荐）

```bash
./run.sh          # 无参数且为交互终端 -> 进入 TUI
./run.sh tui      # 显式进入 TUI
```

- 方向键选功能（slice / cover），回车进入参数表
- `↑/↓` 移动参数，`Enter` 编辑数值 / 切换开关（如 `--dry-run`、网关在 local↔deepseek 间循环）
- `R` 运行，`Q` 返回/退出；非交互环境（管道）自动回退命令行帮助

### 方式二：命令行

```bash
# 试跑单个视频：只检测分段，不产出（先看切点是否合理）
./run.sh slice --file "财源滚滚来__ (1)" --dry-run

# 正式切片（默认 deepseek 网关；也可用 --gateway local）
./run.sh slice

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

## 给协作者

改代码前先读 **AGENTS.md**。新增功能 = 在 `pycut/commands/` 加一个命令模块并在
`cli.py` 的 `COMMANDS` 注册表登记；复用 `pycut/core/` 的成熟能力，不要在命令层重复造轮子。
