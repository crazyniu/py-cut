# Agent.md —— 给 AI 协作者的项目规范

本文件是 AI（含本会话 agent）修改、扩展本项目时的**强制约定**。动手前先通读。

## 0. 一句话定位

`pycut` 是一个**长期维护**的短视频本地处理工具箱。当前只实现 `slice`（切片），
后续要加 `montage`（混剪）、转场贴纸、平台查重等。**所有功能都从 `pycut.py` 入口以子命令执行。**

## 1. 运行环境（硬性，违反即跑不起来）

- **解释器**：必须用自带 `cv2 4.13.0` / `numpy` 的托管解释器：
  `/Users/apple/.workbuddy/binaries/python/versions/3.13.12/bin/python3`
  **禁止 `python3` 裸命令（可能指向系统 3.9）、禁止进 venv（丢 cv2）。**
- **ffmpeg**：homebrew `ffmpeg-full`（含 `h264_videotoolbox` 硬件编码），路径在
  `pycut/config.py` 的 `FFMPEG` / `FFPROBE`。**不要假设系统 `ffmpeg`。**
- **ffmpeg 9.0 差异**：`-vsync` 已移除，用 `-fps_mode cfr`；`-threads N` 在 scene 检测下反而更慢，**不要加**。

## 2. 目录约定（已在 README 画树）

- 逻辑按职责拆进 `pycut/core/`（与命令编排解耦）。**不要把多个功能塞进一个文件。**
- 命令实现只在 `pycut/commands/`，每个模块暴露 `register(subparsers)` 与 `run(args)`。
- 新增功能 = 新建 `commands/xxx_cmd.py` + 在 `cli.py` 的 `COMMANTS` 注册表登记。
- QA / 一次性工具放 `tools/`；测试放 `tests/`；规划放 `docs/`。

## 3. 配置与常量

- 所有标定过的数值在 `pycut/config.py`（场景阈值 `SCENE_TH=0.30`、最短段 `MIN_SEG=2.0`、
  网关预设、编码码率等）。**未经重新标定不要改这些数值**——它们是本地实测得到的。
- LLM 走 **OpenAI 兼容协议**，用**纯标准库 urllib**（环境没装 openai/requests）。
  新增 LLM 调用复用 `core/llm.py` 的 `LLMClient.ask_vision(frame, prompt)`。
- 网关预设 `local`（局域网，快但偶发 502/503）/ `deepseek`（公网备用）。
  `--gateway` 选择，`--llm-url/-key/-model` 覆盖。

## 4. 切片流水线（当前唯一落地功能）

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
- `ffprobe avg_frame_rate` 可能带音频流的 `0/0` 行 → 取首行（`subtitle.cover_clip` 已处理）。

## 6. 改动流程

1. 读 `Agent.md` + 相关 `core/` 模块，确认复用点。
2. 小步修改，**先 dry-run / 单视频试跑**再批量（如 `slice --file X --dry-run`）。
3. 跑 `tools/verify.py` 做独立质检（不信任 slicer 自己的清单）。
4. 改完更新 `README.md` / `ROADMAP.md` / 本文件（如有约定变化）。
5. 提交 git（本仓库根目录 `pycut/` 已 `git init`）。

## 7. 不要做的事

- 不要把切片/混剪/查重逻辑全堆进一个 `.py`。
- 不要引入 openai/requests/ffmpeg-python 等未安装依赖（保持标准库 + cv2 + numpy）。
- 不要把用户的桌面产出（切片目录、清单）纳入 git（它们不在仓库内，且属个人数据）。
- 不要改动 `config.py` 里的标定数值，除非重新实测并写明依据。
