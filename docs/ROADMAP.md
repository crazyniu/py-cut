# 开发路线图（ROADMAP）

> 状态约定：**规划中**（未实现）/ **进行中** / **已实现**。
> 当前已实现 `slice`、`dedup`、`montage` 三个功能。
> **已知缺陷与未完成项统一见 [`docs/ISSUES.md`](ISSUES.md)**（改代码前请先读）。

---

## 已实现

### ✅ slice —— 智能切片 + 人脸分流 + 字幕擦除
- 场景分段（ffmpeg scene 分数，最短段 ≥2s）
- 人脸三态判定（Haar 分筛 + LLM 兜底），按有人脸/无人脸分目录
- 字幕真实擦除（文字掩膜 + TELEA 修复，非毛玻璃）
- 幂等标记、CSV/JSON 双写清单
- 入口：`pycut.py slice` / `cover`
- **限制**：大号艺术标题修复后字形内部偏平滑（经典算法极限），见 ISSUES §1.4

### ✅ dedup —— 视频去重（过平台查重）
- 分层扰动：容器元数据 / 几何 / 像素色彩 / 时间 / 结构内容 / 音频 / 水印
- 三档预设（`light` / `standard` / `aggressive`）+ 单项开关
  （镜像/变速/边框/角标/画中画/贴纸/delogo）
- 按 seed 随机化参数组合，同 seed 完全可复现
- **自检闭环**：pHash 距离（分档门槛）+ PSNR 灾难下限 + 时长偏差（视频流口径）
- 支持单个切片与单个长视频（360s 已实测）
- JSON 报告（utf-8-sig），记录每条参数、距离、PSNR、门禁结果
- 入口：`pycut.py dedup`（`--check` 只自检不产出）
- **能力缺口**：8 大模块里只实现了 14 个滤镜，见 ISSUES §1.1
- 完整需求与实测标定：见 [`docs/DEDUP.md`](DEDUP.md)

### ✅ montage —— 混剪视频
五步流程（详见 [`docs/MONTAGE.md`](MONTAGE.md)）：
- **A** 从爆款视频提取语音节奏与时长基准（`silencedetect`）
- **B** 从片段库随机取**竖屏**片段、不重复（内容级去重）、镜像/加速扰动、
  贴纸叠加、静音、拼接
- **C** 从去重输出取切片，以**画中画**叠在右上角（四角带阴影融入）、全程连续
- **D** SRT → ASS → libass 烧录（54px 大字 + 半透明底框）
- **E** 贴回基准视频原声（静音成片不可用）
- 入口：`pycut.py montage`（`--probe` 只看素材规模、`--step` 单独跑某步）

---

## 规划中

### 1. transition —— 转场贴纸（婚礼视频场景）
**需求**：在婚礼视频的切片之间增加**转场贴纸**（如爱心、闪粉、相框等动态贴纸），
提升成片观感。

**要点（待细化）**：
- 在 montage 拼接的接缝处插入转场（淡入淡出 / 滑入 / 贴纸动画）。
- 贴纸资源：内置若干 PNG/WebM 贴纸，或支持用户自定义目录。
- 婚礼场景可能需要更柔和的转场与暖色调。

### 2. dedup 剩余模块（过平台查重）
完整需求与实现状态见 [`docs/DEDUP.md`](DEDUP.md)，缺口汇总见 [`docs/ISSUES.md`](ISSUES.md) §1.1。

- **P1**：M4 抽帧/关键帧重排、M5 转场 xfade/分屏/片头片尾、M6 换 BGM/叠环境音、
  M7 `removelogo`(mask)
- **P2**：暗水印检测与破坏、M8 OCR/ASR 改写、VMAF 画质门禁、账号矩阵参数表
- 合规红线：**不得引入任何功效/医疗宣称**（九源仙项目），去重只作用于画面技术层

### 3. quality high —— AI 级字幕修复（可选）
**需求**：当前 `subtitle.cover_clip` 用经典 TELEA 修复，大号艺术标题字形内部偏平滑。
提供 `--quality high` 走 AI 视频修复模型（ProPainter / LaMa）做到无痕，代价是速度慢约百倍。
**要点**：作为 slice/cover 的可选模式，默认仍用快速经典算法。

---

## 设计原则（贯穿后续）

- 所有新功能以**子命令**挂载到 `pycut.py` 入口，复用 `core/` 成熟能力，不重复造轮子。
- 目录按职责拆分，保持长期可维护（见 `Agent.md` 与 `README.md`）。
- 涉及 LLM 的视觉/文本能力统一走 `core/llm.py`。
- 涉及 ffmpeg 的封装统一走 `core/ffmpeg.py` + `core/slice_pipeline.cut_clip`。
