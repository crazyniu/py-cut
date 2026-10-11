# AGENTS.md —— 给 AI 协作者的项目规范

本文件是 AI（含本会话 agent）修改、扩展本项目时的**强制约定**。动手前先通读。

## 0. 一句话定位

`pycut` 是一个**长期维护**的短视频本地处理工具箱。已实现 `slice`（切片）、
`dedup`（去重/过查重）、`montage`（混剪）三个功能。
**所有功能都从 `pycut.py` 入口以子命令执行，参数既支持命令行参数，也支持终端 TUI 交互输入。**

## 0.5 动手前必读的两份文档

| 文档 | 什么时候看 |
|---|---|
**`docs/ISSUES.md`** | 记录工程限制、验证经验与技术边界；不存 Bug/Todo，实时任务只看 GitHub Issues |
**`AGENTS.md`（本文件）**第 5 节 | 关键坑清单，含三大功能专属的 ffmpeg 陷阱 |

Markdown 只保留长期需求、架构、技术约定与可复现限制，不能再复制 Bug、Todo 或状态表；功能改动仅在长期契约变化时更新 README/技术文档。实际 Bug、需求实现、测试及 PR 状态统一在 GitHub Issues 管理。本仓库依既定约定不使用 Kanban，可以直接修复与提交；但涉及已有 Issue 时应先检查 GitHub 最新负责人和 PR，避免多人重复修改同一文件。

## 1. 运行环境（硬性，违反即跑不起来）

- **统一入口 `./run.sh`**：自动选解释器 → 按 `requirements.txt` 装依赖（已装则跳过）
  → 校验 cv2 → 打印生效网关 → 运行。日常跑任务一律用 `./run.sh <command> ...`，
  不要绕过它直接 `python3 pycut.py`（会踩解释器/依赖坑）。
  Windows 的对应入口为 `run.cmd`，使用 winget 安装缺失的 Python/FFmpeg，安装后重新检测；仅检查通过才启动 CLI/TUI。
- **解释器**：macOS 优先使用自带 cv2/numpy 的托管解释器
  `/Users/apple/.workbuddy/binaries/python/versions/3.13.12/bin/python3`（存在时）。
  其他环境由 `./run.sh` 选择当前解释器并校验 OpenCV 4.x 的 Haar 支持，不要求 macOS 路径存在；禁止绕过启动器运行处理任务。
  缺少依赖时启动器自动安装；系统 Python 标记 EXTERNALLY-MANAGED 时允许启动器创建项目 `.venv`，不得使用 `--break-system-packages`。缺少 FFmpeg 时通过当前系统包管理器安装并复检，失败停止。
- **依赖**：视频核心仅 `numpy` + `opencv-python`，目录选择明确复用 MIT 开源的 `textual-fspicker==1.0.1`（传递依赖 `textual`），Windows 使用 `windows-curses`。其余 LLM、.env 和 CLI 仍优先标准库，除目录选择所需依赖外不得随意扩展。
- **ffmpeg**：macOS 优先 Homebrew `ffmpeg-full` 与 VideoToolbox，其他环境使用 PATH 的 ffmpeg/ffprobe 与 libx264。
  `PYCUT_FFMPEG` / `PYCUT_FFPROBE` 环境变量可覆盖路径，集中定义在 `pycut/config.py`。
- **ffmpeg 9.0 差异**：`-vsync` 已移除，用 `-fps_mode cfr`；`-threads N` 在 scene 检测下反而更慢，**不要加**。

## 2. 目录约定（已在 README 画树）

- 逻辑按职责拆进 `pycut/core/`（与命令编排解耦）。**不要把多个功能塞进一个文件。**
- 命令实现只在 `pycut/commands/`，每个模块暴露 `register(subparsers)` 与 `run(args)`。
- 新增功能 = 新建 `commands/xxx_cmd.py` + 在 `cli.py` 的 `COMMANDS` 注册表登记。
- QA / 一次性工具放 `tools/`；测试放 `tests/`；规划放 `docs/`。

## 3. 配置与常量

- 所有标定过的数值在 `pycut/config.py`（场景阈值 `SCENE_TH=0.30`、最短段 `MIN_SEG=2.0`、
  编码码率等）。**未经重新标定不要改这些数值**——它们是本地实测得到的。
- LLM 走 **OpenAI 兼容协议**，用**纯标准库 urllib**（环境没装 openai/requests）。
  新增 LLM 调用复用 `core/llm.py` 的 `LLMClient.ask_vision(frame, prompt)`。
- **网关只有两个：主网关 `main` / 备用网关 `backup`**（`config.GATEWAYS` 只有这两个键，
  不按厂商命名）。主网关默认 DeepSeek 官方，但**可以在 `.env` 换成任意 OpenAI 兼容网关**；
  改地址/模型即可，代码无需改动。`config.DEFAULT_GATEWAY = "main"` 是常量，
  不再有 `PYCUT_GATEWAY` 之类的选择开关。
- **密钥从 `.env` 或环境变量读取，代码里不允许出现明文密钥**（`config.py` 里只有空串默认值）。
  主网关：`PYCUT_MAIN_URL` / `PYCUT_MAIN_KEY`（旧名 `DEEPSEEK_API_KEY` 仍兼容）/ `PYCUT_MAIN_MODEL`；
  备用网关：`PYCUT_BACKUP_URL` / `PYCUT_BACKUP_KEY` / `PYCUT_BACKUP_MODEL`（地址或模型留空时回落主网关值）。
  `.env` 已被 git 忽略，模板见 `.env.example`；真实环境变量优先于 `.env`。
- **备用网关的用途是自动容灾**：主网关连续失败达 `config.LLM_FAILOVER_FAILS`（=3）次，
  `LLMClient` 自动切到备用网关并**粘住**（`_delegate`），后续调用都走备用，日志打印切换提示。
  `--no-fallback` 可关闭；传了 `--llm-url/--llm-key/--llm-model` 显式覆盖时不挂备用。
  备用网关未配密钥时不切换（`make_llm` 里用 `config.key_ready(other)` 判断）。
  `config.key_ready()` / `config.gateway_summary()` 用于检查状态
  （**日志里绝不打印密钥本身**，占位值如 `sk-你的密钥` 视为未配置）。
- **Web / .env 是目录配置入口**：`pycut/settings.py` 统一读取与原子保存项目根 `.env`；`pycut/web_config.py` 使用标准库 HTTP 服务，只监听 `127.0.0.1`，校验 Host/Origin/会话令牌，不回显密钥。启动 TUI 自动打开 Web，W 可重新打开。
- **TUI 保留四个处理向导**：任务开始前重新加载 `.env` 并校验目录；缺配置引导 Web，不再调用终端目录选择器。切片预览三层内 MP4，与 CLI `--max-depth 3` 一致。混剪保留爆款视频多选与裂变数（1～99）。
- **键盘与执行**：↑↓ 移动，→/Enter 进入；确认页必须 Enter 执行，Esc 返回。主菜单只有 Q 退出。CLI 子进程仍经 `tui_runner` 显示实时日志，失败停止依赖任务，不能将非零退出码当成功。显式 CLI 参数保持兼容。
- **批量裂变**：同一命令内共享参考视频画像、背景/画中画扫描与在线贴纸下载；每支成片的随机规划及编码独立执行，不做跨任务持久缓存。任务结束清理下载目录。
- **前贴**：从配置目录随机选一个完整视频，复用混剪去重与贴纸，不调用字幕擦除。正文字幕与原声完成后才拼接，报告记录偏移；任何处理失败不得覆盖原成片。中间文件需与被原子替换的文件位于同一文件系统。

## 4. 切片流水线

三步，顺序不可乱：
1. **场景分段**（默认 2–15 秒，不识别语义，合并短尾段后仍检查上限）→ `core/scene.py`：`detect_scenes`（ffmpeg `select='gt(scene,TH)'`）+ `build_segments`（去重/合并过短/切分超长）。
2. **人脸分流** → `core/faces.py`：Haar 严格/宽松两档 → 三态 `face/noface/ambiguous`；仅 ambiguous 送 LLM 兜底。产出按「有人脸/无人脸」分目录。
3. **字幕擦除** → `core/subtitle.py`：`detect_region`（LLM 并集框 + 中位数聚合，高度跟随真实字幕）+ `cover_clip`（FFmpeg 内按字幕高度覆盖整幅视频宽度的矩形马赛克，音轨直接 copy；验证尺寸、时长与音轨后原子替换）。

入口编排：`core/slice_pipeline.py` 的 `process_video`。

## 5. 关键坑（踩过，别再踩）

- **macOS `find -iname "*.mp4"` 会静默丢文件** → 一律用 `os.scandir`（`list_videos` / `collect_clips`）。
- **双重 seek**：关键帧间隔 8.33s 而镜头中位数仅 2.03s，只靠输入侧 `-ss` 会抽到别的镜头的帧。切割与抽帧都必须输入侧粗 seek（回退 1.0s）+ 输出侧精 seek。见 `config.COARSE_BACKOFF`、`cut_clip`、`faces._extract_one`。
- **LLM 推理模型** `max_tokens` 必须 ≥500，否则 `content` 被 reasoning 吃光返回空；`finish_reason=length` 时换 1200 重试（`core/llm.py` 已处理）。
- **并发结果配对**：用 `as_completed` + 显式回传索引，绝不用 `ex.map` 的解包顺序（顺序会错位导致判定张冠李戴）。
- **字幕处理按用户要求覆盖矩形马赛克**：宽度固定为视频全宽，高度跟随检测到的字幕并加上下余量；不得恢复逐字掩膜修复，区域外不参与马赛克。
- **幂等**：字幕已处理写 `<切片>.covered.json`；重跑须先按清单切割点从源视频还原原片，否则会把填补区当"模糊文字"误判。
- `ffprobe avg_frame_rate` 用**不带 `-select_streams`** 的通用封装（`core/ffmpeg.py: probe_scalar`）时，会同时返回视频流与音频流两行，音频流那行是 `0/0` → 只能取首行（`subtitle.py:166` 已这么处理）。实测桌面切片 20 个抽样中 18 个如此，**别用 `-select_streams v:0` 的命令去验证这条**（那样看不到 `0/0`）。`dedup.py:_video_duration` 用了 `-select_streams v:0`，不受此影响。
- **注释与报告里的「实测」数字必须是自己跑出来的，且能被独立复现**。转述他人数据即使方向对也不可查证；凭单次观察下结论（尤其喂了截断/异常样本却不自知）会让后来人无法复核。实测数字要能让人照着重跑一遍得到同样的结果。
- **要验证别人的代码，直接调用项目里的真实实现，别自己拼一条"等价"的命令**。简化版往往缺少关键前提（我曾用 `ffprobe ... | head -1` 去验证 `probe_scalar`，`head -1` 恰好把 `0/0` 过滤掉，得出"从未出现"的错误结论还写进了注释）。自己拼的命令验证不了被测函数的真实行为。
- **不要为推演出来的边界条件加防御**。判断依据来自实测枚举而非想象：本机 ffprobe 9.0.2 的 `stream.duration` 输出只有三种形态——有效数值 / `N/A` / 空（rc=1），异常输入走 rc=1，**从不输出 `nan`/`inf`**；所以 `_video_duration` 现有的一条 `except ValueError` 就够，不要加 `math.isfinite` 之类永远不成立的检查。
- **密钥不要硬编码提交**：新增网关/换 key 走 `.env` 或环境变量，禁止把明文 key 写进 `config.py` 后提交（现有 fallback 值是本机遗留，新部署必须走 `.env`）。

## 6. 改动流程

1. 读 `AGENTS.md` + 相关 `core/` 模块，确认复用点。
2. 小步修改，**先 dry-run / 单视频试跑**再批量（如 `slice --file X --dry-run`）。
3. 跑 `tools/verify.py` 做独立质检（不信任 slicer 自己的清单）。
4. 仅当长期需求或技术约定确有变化时更新对应 README / 设计文档，不用 Markdown 管理日常 Bug 或开发进度。
5. 提交 git（本仓库根目录 `pycut/` 已 `git init`）。

## 7. 不要做的事

- 不要把切片/混剪/查重逻辑全堆进一个 `.py`。
- 不要引入 openai/requests/ffmpeg-python 等未安装依赖（保持标准库 + cv2 + numpy）。
- 不要把用户的桌面产出（切片目录、清单）纳入 git（它们不在仓库内，且属个人数据）。
- 不要改动 `config.py` 里的标定数值，除非重新实测并写明依据。
- 不要把明文 API Key 提交进仓库（用 `.env` / 环境变量）。
