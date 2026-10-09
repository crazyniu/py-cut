# pycut —— 短视频智能处理工具箱

统一处理抖音 / 微信等短视频的本地工具集。当前已落地**切片**功能，后续将扩展混剪、转场贴纸、平台查重等。

## 目录结构

```
pycut/
├── pycut.py                # 统一入口（命令行，后续演进为 TUI）
├── pyproject.toml          # 包元数据
├── .gitignore
├── Agent.md                # 给 AI 协作的规范（务必先读）
├── README.md
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

## 用法

```bash
# 试跑单个视频：只检测分段，不产出（先看切点是否合理）
python3 pycut.py slice --file "财源滚滚来__ (1)" --dry-run

# 正式切片（默认 local 网关；公网稳定用 deepseek）
python3 pycut.py slice --gateway deepseek

# 只处理前 3 个视频
python3 pycut.py slice --limit 3

# 补跑 / 重跑字幕擦除（--force 忽略已遮盖标记）
python3 pycut.py cover --all --gateway deepseek
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

改代码前先读 **Agent.md**。新增功能 = 在 `pycut/commands/` 加一个命令模块并在
`cli.py` 的 `COMMANDS` 注册表登记；复用 `pycut/core/` 的成熟能力，不要在命令层重复造轮子。
