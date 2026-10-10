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
- **解释器**：必须用自带 `cv2 4.13.0` / `numpy` 的托管解释器：
  `/Users/apple/.workbuddy/binaries/python/versions/3.13.12/bin/python3`
  **禁止 `python3` 裸命令（可能指向系统 3.9）、禁止进 venv（丢 cv2）。**
- **依赖**：只有 `numpy` + `opencv-python`（见 `requirements.txt`，用 `>=` 以免重复安装）；
  其余能力（LLM、.env、CLI/TUI）全部走标准库。**不要引第三方依赖。**
- **ffmpeg**：homebrew `ffmpeg-full`（含 `h264_videotoolbox` 硬件编码），路径在
  `pycut/config.py` 的 `FFMPEG` / `FFPROBE`。**不要假设系统 `ffmpeg`。**
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
- **密钥只从 `.env` 读取，代码里不允许出现明文密钥**（`config.py` 里只有空串默认值）。
  主网关：`PYCUT_MAIN_URL` / `PYCUT_MAIN_KEY`（旧名 `DEEPSEEK_API_KEY` 仍兼容）/ `PYCUT_MAIN_MODEL`；
  备用网关：`PYCUT_BACKUP_URL` / `PYCUT_BACKUP_KEY` / `PYCUT_BACKUP_MODEL`（地址或模型留空时回落主网关值）。
  `.env` 已被 git 忽略，模板见 `.env.example`；真实环境变量优先于 `.env`。
- **备用网关的用途是自动容灾**：主网关连续失败达 `config.LLM_FAILOVER_FAILS`（=3）次，
  `LLMClient` 自动切到备用网关并**粘住**（`_delegate`），后续调用都走备用，日志打印切换提示。
  `--no-fallback` 可关闭；传了 `--llm-url/--llm-key/--llm-model` 显式覆盖时不挂备用。
  备用网关未配密钥时不切换（`make_llm` 里用 `config.key_ready(other)` 判断）。
  `config.key_ready()` / `config.gateway_summary()` 用于检查状态
  （**日志里绝不打印密钥本身**，占位值如 `sk-你的密钥` 视为未配置）。
- **参数两种入口（等价）**：
  - 命令行：`python3 pycut.py slice --file X --scene-th 0.35`
  - 终端 TUI：直接 `python3 pycut.py`（无参数且为交互终端即进入），走**引导式向导**。
    TUI 与 CLI 共用 `cli.build_parser()` 的参数定义，改一方另一方自动同步。
- **TUI 是引导式向导，不甩一堆参数**（用户明确要求）：
  - 主菜单只提供 4 个现有处理功能（切片 / 字幕擦除 / 去重 / 混剪），
    进入「选目录 → 报视频数 → 选开关（如适用）→ 确认执行」的分步向导。
    不提供独立素材搜索/扫描功能；各处理流程仍可读取目录、统计素材数量。
  - 目录选择器 `tui._pick_dir` 采用常见文件选择器顺序：默认从用户主目录开始，
    `../` 上级目录 → 子文件夹（按名称排序）→「使用当前文件夹」。
    `↑/↓` 移动，`Enter` 进入文件夹/确认当前文件夹，`←`/`h`/Backspace 上级，
    `Space` 选用当前目录，`~` 跳主目录、`/` 跳根目录，`Esc` 返回上个向导步骤。
    **只隐藏以 `.` 开头的目录名称**：`scan.list_dirs` 不显示 `.git`、`.ssh` 等点前缀目录或同名目录软链接；
    其余系统目录（`Library`、`System` 等）及有效目录符号链接都显示，并遵守系统访问权限。
    首次打开目录选择器默认使用当前系统用户的 `Path.home()`，不能写死 `/Users/apple`。
    可从根目录进入 `/Volumes`。禁止改回预置候选目录列表。
    仅修改浏览列表，视频处理递归时仍使用 `_is_system_dir` 剪枝及原有深度限制，不得混为一谈。
  - **目录快速跳转与粘贴（现有 TUI 交互）**：目录页 `1` 跳当前 `Path.home()`、`2` 跳 `~/Desktop`、`3` 跳 `~/Documents`、`4` 跳 `~/Downloads`、`5` 跳挂载磁盘目录（macOS `/Volumes`；非 macOS 按系统挂载路径探测）。按键仅切换正在浏览的目录，绝不立即确认或启动命令；目录不存在或无权访问必须保留原目录并展示提示。
    `P` 进入可粘贴路径输入框，复用 Unicode `get_wch` 文本编辑器，支持中文路径、空格、终端拖入路径的引号/转义、`~`、环境变量及相对当前目录路径。`Enter` 必须确认目标真实存在且是文件夹；不允许选择普通文件或不存在路径；错误留在输入框内反馈，支持 `Ctrl+U` 清空重填、`Esc` 取消。不得执行粘贴内容作为 shell 命令或创建目录。macOS `/Volumes` 下挂载的外接硬盘由 `scan.list_dirs` 正常列举，仍沿用仅隐藏 `.` 前缀的既有规则。
  - 目录扫描统一走 `core/scan.py`：递归统计/列出**默认最多下探 3 层**
    （`DEFAULT_MAX_DEPTH=3`，用户要求控制深度），系统目录（Library/System/.git 等）自动剪枝。
  - 混剪向导用 `tui._multi_select` 多选爆款视频 + 每支设「裂变数量」（E 编辑 1～99），
    每个选中视频生成独立 argv；按顺序执行，失败停止后续依赖任务。
    `Esc` 必须逐步返回前一向导步骤，回到多选时保留先前选中视频及裂变数。
  - **统一键盘规范**：`↑/↓` 移动，`←` 返回上一层，`→` 打开目录/选择工作流程；
    混剪多选列表 `Space` 勾选/取消多个视频、`→`/`Enter` 进入确认；目录页 `Space` 仅确定单个当前文件夹。
    确认执行页 `→` 只移动焦点，不能直接启动处理，必须显式 `Enter` 才执行。菜单、目录列表及多选行避免整行反色高亮。
    curses 的默认 Esc 等待可能过长，初始化时在没有用户 `ESCDELAY` 的情况下调用 `curses.set_escdelay(75)`；不能破坏方向键序列。
  - TUI 以 `cli.build_parser()` 定义的命令参数生成 argv，交给 `pycut/tui_runner.py`
    使用 `sys.executable` 启动 CLI 子进程；主线程独占 curses，子进程 stdout/stderr
    显示为实时日志，完成页 `Enter`/`Esc` 回到主菜单，只有 `Q` 退出。
    主菜单的 `Esc`/`←`/Backspace 不能直接关闭应用。**不得把错误退出码当成功，也不得在失败后继续
    执行后继去重**。不执行 shell 命令拼接、不回显 API Key。
  - TUI 切片从 `core/scan.py` 预览「三层内 MP4」，CLI 显式传 `--max-depth 3`；
    直接 CLI 不传参数时保持原有无限递归。预览视频集合必须与实际处理一致。
  - 当前仅维护这 4 个处理向导，不得额外添加 TUI 功能；修复 Bug 和改善现有交互时须保持 CLI 参数及行为兼容。

## 4. 切片流水线

三步，顺序不可乱：
1. **场景分段** → `core/scene.py`：`detect_scenes`（ffmpeg `select='gt(scene,TH)'`）+ `build_segments`（去重/合并过短/切分超长）。
2. **人脸分流** → `core/faces.py`：Haar 严格/宽松两档 → 三态 `face/noface/ambiguous`；仅 ambiguous 送 LLM 兜底。产出按「有人脸/无人脸」分目录。
3. **字幕擦除** → `core/subtitle.py`：`detect_region`（LLM 并集框 + 中位数聚合，高度跟随真实字幕）+ `cover_clip`（逐帧文字掩膜 + TELEA 修复 + 颗粒补偿）。

入口编排：`core/slice_pipeline.py` 的 `process_video`。

## 5. 关键坑（踩过，别再踩）

- **macOS `find -iname "*.mp4"` 会静默丢文件** → 一律用 `os.scandir`（`list_videos` / `collect_clips`）。
- **双重 seek**：关键帧间隔 8.33s 而镜头中位数仅 2.03s，只靠输入侧 `-ss` 会抽到别的镜头的帧。切割与抽帧都必须输入侧粗 seek（回退 1.0s）+ 输出侧精 seek。见 `config.COARSE_BACKOFF`、`cut_clip`、`faces._extract_one`。
- **LLM 推理模型** `max_tokens` 必须 ≥500，否则 `content` 被 reasoning 吃光返回空；`finish_reason=length` 时换 1200 重试（`core/llm.py` 已处理）。
- **并发结果配对**：用 `as_completed` + 显式回传索引，绝不用 `ex.map` 的解包顺序（顺序会错位导致判定张冠李戴）。
- **字幕擦除**不要用毛玻璃/中值净底：动态背景下中值会出块状伪影；掩膜法只动文字像素。大号艺术标题字形内部偏平滑是经典算法极限，要无痕需 AI 修复（ProPainter/LaMa，慢百倍），列为可选 `--quality high`（未实现）。
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
