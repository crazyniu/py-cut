# AGENTS.md —— 给 AI 协作者的项目规范

本文件是 AI（含本会话 agent）修改、扩展本项目时的**强制约定**。动手前先通读。

## 0. 一句话定位

`pycut` 是一个**长期维护**的短视频本地处理工具箱。提供 `slice`（切片）、`cover`（字幕擦除）、
`dedup`（降低视频相似度）、`montage`（混剪）四个命令。
**所有功能都从 `pycut.py` 入口以子命令执行，参数既支持命令行参数，也支持终端 TUI 交互输入。**

## 0.5 动手前必读的两份文档

| 文档 | 什么时候看 |
|---|---|
**`docs/ENGINEERING.md`** | 记录工程限制、验证经验与技术边界；不存 Bug/Todo，实时任务只看 Linear |
**`AGENTS.md`（本文件）**第 5 节 | 关键坑清单，含三大功能专属的 ffmpeg 陷阱 |

## 0.6 项目管理与开工门禁

- **任务唯一来源**：[Linear 项目](https://linear.app/gengyun/project/py-cut-development-963524cf0d78)，团队 `DEV`（gengyun）。Linear 管需求、Bug、测试、负责人、依赖、领取、状态与验收；GitHub 仓库 `gengyun-lab/py-cut` 管代码、分支、Commit、PR、Review、CI 与合并。默认分支须从远端核实，不凭聊天记忆假定。
- 不新建 GitHub Issues/Projects 或其他重复任务系统，不启用 GitHub Issues Sync。历史 GitHub Issue 仅供需求与交付溯源；关闭不代表代码已实现，尤其 `not_planned` 不是已完成。
- **每次开工先审计**：读取根及适用子目录 AGENTS.md、相关长期文档与真实源码；检查 remote/default branch、fetch、当前分支/工作树、开放 PR/Review/CI、Linear 项目/任务/负责人/依赖和其他开发活动。不覆盖未提交改动，不依赖历史对话认定现状。
- 搜索并复用现有 Linear Issue。不同可独立交付的需求按文件边界拆分，标清依赖及共享接口；避免过度拆分、重复创建和重复实现。无法访问 Linear 或领取冲突时停止受影响的仓库修改，记录阻塞；不另建 Markdown/GitHub 任务清单顶替。
- 只领取**未分配、前置依赖已满足的 Todo**：先读取 Issue 与代码，再设置实际执行身份为负责人、移至 In Progress，并重新读取确认。状态更新不是原子锁；并发领取冲突须停止协调，不能擅自接管别人任务。工具没有 AI 身份时可使用认证用户，但须在 Issue 评论说明执行会话和分支，以区分同账号多个 AI。

## 0.7 零上下文 Issue 与并行协作

- Issue 必须独立说明：业务背景/影响、目标、当前实现、核实过的文件或接口、修改边界、具体输入输出/交互/异常、技术约束、依赖及文档、可验证的验收 Checkbox、测试步骤/输入/预期、PR 与证据交付要求。不得写“按之前讨论”或模糊“优化相关问题”；未知信息显式标待核实。
- 主对话负责核查、拆分、确定共享契约、识别并行边界、协调冲突和汇总；独立对话原则上一个 Linear Issue 对应一个分支与 PR。
- 对无依赖且不共改文件的任务，环境支持时采用独立会话/子 Agent；每个会话自行读规范、确认领取、使用独立分支和 Worktree。**不得并发编辑同一工作树**。有依赖/共享文件先确定契约再串行或调整边界；不为增加并行数拆任务。
- 分支名带 Linear 编号（如 `dev-123-short-description`）；PR 描述链接 Issue，说明最终行为、测试证据和未验证边界。已有他人 PR 不另做一份。协调以 Linear/GitHub 实时状态为准；无法创建独立会话就如实说明并给出独立任务入口，不声称已委派。

## 0.8 状态、验收与 CI

- 使用现有团队工作流：Backlog → Todo → In Progress → In Review → Done；Canceled/Duplicate 为终止状态。阻塞写评论并设置依赖，不擅自创建 Ready/Blocked 等状态。
- 创建 PR 后转 In Review，更新 AC、证据、PR 链接与依赖；必要测试、Review、合并和验收全部通过才 Done。**合并不是测试完成**，父任务须必要子任务及整体验收完成后关闭。部分交付不能勾选未验证项；取消/重复也不能当作实现。
- 完工回读远端 PR/CI/合并 SHA 和 Linear 状态，修复不一致。用授权范围内的正常 PR 流程交付，不绕过保护/Review、不强推覆盖、不擅自启用自动合并。
- 日常仅运行仓库必需检查和与变更对应的轻量验证；普通 push/PR 不重复跑全量、E2E、性能或耗时扫描。完整合成媒体回归通过手动 `workflow_dispatch`；发布/部署、架构或重要公共接口、安全/权限、重大依赖变更及用户明确要求全量时才运行相应重检查。
- 调整 CI 前核实必需检查与保护；接口不可用就记录未核实，保留已知 check 名称，不擅自删除或绕过。未执行测试不可宣称通过。
- 视觉处理须用真实实现编码、完整解码、抽帧目检，音频须审听并核对流/时长；滤镜字符串、rc=0、pHash/PSNR 不足以证明画面质量、暗水印移除或平台无法识别。Mock 验证协议/错误处理，不能替代真实 LLM 准确率；短素材不证明大素材性能。测试命令/输入摘要/版本/SHA/证据记录在 Linear 或 PR，密钥始终脱敏。

## 0.9 长期文档边界

Markdown 仅保留有效需求、业务流程、架构、接口/数据契约、技术方案和开发/测试/部署规范。入口 README；产品设计 docs/ROADMAP.md、MONTAGE.md、DEDUP.md；工程边界 docs/ENGINEERING.md；AI 协作约束在本文件。
禁止 Markdown 存 Todo/Backlog/Bug、当前进度、临时计划/交接、Commit/PR 流水账、临时测试报告与 CI 运行记录。未完成事项迁到 Linear 并核实成功后再清理旧文件；有效技术知识保留，不为整理无意义增加文件。契约变化时增量更新文档，状态变化仅更新 Linear/GitHub。

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
- 可复用 QA 工具放 `tools/`；测试放 `tests/`；临时脚本/素材/报告放被忽略的 `work/`；长期需求和设计放 `docs/`。

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

输出契约：显式 CLI 输出路径和 .env 配置优先；未配置时切片放源目录同级 `切片/有人脸`、`切片/无人脸`，文件名 `原文件名_n.mp4`，不按视频名建目录。字幕擦除另存到同级 `字幕查重`，去重另存到同级 `去重`，保留既有分组和编号。混剪向导从 .env 读取爆款、切片和画中画目录；配置的输出目录优先，否则成片保存到爆款目录同级 `混剪/MMDD/原文件名_n.mp4`。显式 CLI 输出路径仍优先。

## 4. 切片流水线

三步，顺序不可乱：
1. **场景分段**（默认 2–15 秒，不识别语义，合并短尾段后仍检查上限）→ `core/scene.py`：`detect_scenes`（ffmpeg `select='gt(scene,TH)'`）+ `build_segments`（去重/合并过短/切分超长）。
2. **人脸分流** → `core/faces.py`：Haar 严格/宽松两档 → 三态 `face/noface/ambiguous`；仅 ambiguous 送 LLM 兜底。产出按「有人脸/无人脸」分目录。
3. **字幕擦除** → `core/subtitle.py`：`detect_region`（多图单请求、逐帧索引验证与跨帧并集框，高度跟随真实字幕）+ `cover_clip`（FFmpeg 内按字幕高度覆盖整幅视频宽度的单层矩形毛玻璃，音轨直接 copy；验证尺寸、时长与音轨后原子替换）。

入口编排：`core/slice_pipeline.py` 的 `process_video`。识别不确定、超时或复检可能残留返回 review（待复查），不写完成标记，继续其他片与后继去重；真正编码失败与中断仍非零。每轮检测共享30秒等待预算，单次HTTP超时15秒，等待后台片段时每秒更新进度。

## 5. 关键坑（踩过，别再踩）

- **macOS `find -iname "*.mp4"` 会静默丢文件** → 一律用 `os.scandir`（`list_videos` / `collect_clips`）。
- **双重 seek**：关键帧间隔 8.33s 而镜头中位数仅 2.03s，只靠输入侧 `-ss` 会抽到别的镜头的帧。切割与抽帧都必须输入侧粗 seek（回退 1.0s）+ 输出侧精 seek。见 `config.COARSE_BACKOFF`、`cut_clip`、`faces._extract_one`。
- **LLM 推理模型** `max_tokens` 必须 ≥500，否则 `content` 被 reasoning 吃光返回空；`finish_reason=length` 时换 1200 重试（`core/llm.py` 已处理）。
- **并发结果配对**：用 `as_completed` + 显式回传索引，绝不用 `ex.map` 的解包顺序（顺序会错位导致判定张冠李戴）。
- **字幕处理按用户要求覆盖单层矩形毛玻璃**：宽度固定为视频全宽，高度跟随检测到的字幕并加上下余量；仅一层高斯模糊和一次 overlay，禁止像素化缩放或重复叠层；不得恢复逐字掩膜修复，区域外不参与模糊。
- **幂等**：字幕已处理写 `<切片>.covered.json`；重跑须先按清单切割点从源视频还原原片，否则会把填补区当"模糊文字"误判。
- `ffprobe avg_frame_rate` 用**不带 `-select_streams`** 的通用封装（`core/ffmpeg.py: probe_scalar`）时，会同时返回视频流与音频流两行，音频流那行是 `0/0` → 只能取首行（`subtitle.py:166` 已这么处理）。实测桌面切片 20 个抽样中 18 个如此，**别用 `-select_streams v:0` 的命令去验证这条**（那样看不到 `0/0`）。`dedup.py:_video_duration` 用了 `-select_streams v:0`，不受此影响。
- **注释与报告里的「实测」数字必须是自己跑出来的，且能被独立复现**。转述他人数据即使方向对也不可查证；凭单次观察下结论（尤其喂了截断/异常样本却不自知）会让后来人无法复核。实测数字要能让人照着重跑一遍得到同样的结果。
- **要验证别人的代码，直接调用项目里的真实实现，别自己拼一条"等价"的命令**。简化版往往缺少关键前提（我曾用 `ffprobe ... | head -1` 去验证 `probe_scalar`，`head -1` 恰好把 `0/0` 过滤掉，得出"从未出现"的错误结论还写进了注释）。自己拼的命令验证不了被测函数的真实行为。
- **不要为推演出来的边界条件加防御**。判断依据来自实测枚举而非想象：本机 ffprobe 9.0.2 的 `stream.duration` 输出只有三种形态——有效数值 / `N/A` / 空（rc=1），异常输入走 rc=1，**从不输出 `nan`/`inf`**；所以 `_video_duration` 现有的一条 `except ValueError` 就够，不要加 `math.isfinite` 之类永远不成立的检查。
- **密钥不要硬编码提交**：新增网关/换 key 走 `.env` 或环境变量，禁止把明文 key 写进 `config.py` 后提交（所有默认密钥必须为空，新部署走 `.env`）。

## 6. 最小开发与验证流程

1. 按第 0.6–0.8 节核查并确认 Linear 领取；独立分支/Worktree 开工，读相关 `core/` 复用点。
2. 小步修改，先 dry-run / 单视频试跑，再需要的批量；处理任务只能用启动器。
3. 使用启动器选择的 Python 跑相关 `tests/test_*.py`；pytest 能收集现有 unittest，不连续双跑同一套。切片另用 `tools/verify.py` 独立质检，不信任自身清单；视觉/音频满足第 0.8 节。
4. 检查 `git diff --check`、必要语法/文档引用；只在长期契约变化时增量更新文档。
5. 带 Linear 编号提交独立 PR，关联验证和限制；Review、CI、合并、验收后同步并回读任务状态。

## 7. 不要做的事

- 不要把切片/混剪/查重逻辑全堆进一个 `.py`。
- 不要引入 openai/requests/ffmpeg-python 等未安装依赖（保持标准库 + cv2 + numpy）。
- 不要把用户的桌面产出（切片目录、清单）纳入 git（它们不在仓库内，且属个人数据）。
- 不要改动 `config.py` 里的标定数值，除非重新实测并写明依据。
- 不要把明文 API Key 提交进仓库（用 `.env` / 环境变量）。
