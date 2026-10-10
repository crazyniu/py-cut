# 产品功能与演进设计

> 本文只记录长期产品需求、架构边界和扩展方向，不管理 Issue、Todo 或进度。
> 当前缺陷、负责人与验收以 [GitHub Issues](https://github.com/natefox2017/py-cut/issues) 为准；未来视频能力统一由 [#19](https://github.com/natefox2017/py-cut/issues/19) 关联。
> 工程验证和算法限制见 [ISSUES.md](ISSUES.md)，去重模块的详细设计见 [DEDUP.md](DEDUP.md)。
---

## 核心功能的现有接口契约

### ✅ slice —— 智能切片 + 人脸分流 + 字幕擦除
- 场景分段（ffmpeg scene 分数，最短段 ≥2s）
- 人脸三态判定（Haar 分筛 + LLM 兜底），按有人脸/无人脸分目录
- 字幕真实擦除（文字掩膜 + TELEA 修复，非毛玻璃）
- 幂等标记、CSV/JSON 双写清单
- 入口：`pycut.py slice` / `cover`
- **限制**：大号艺术标题修复后字形内部偏平滑（经典算法极限），见 [工程限制](ISSUES.md) §1.4

### ✅ dedup —— 视频去重（过平台查重）
- 分层扰动：容器元数据 / 几何 / 像素色彩 / 时间 / 结构内容 / 音频 / 水印
- 三档预设（`light` / `standard` / `aggressive`）+ 单项开关
  （镜像/变速/边框/角标/画中画/贴纸/delogo）
- 按 seed 随机化参数组合，同 seed 完全可复现
- **自检闭环**：pHash 距离（分档门槛）+ PSNR 灾难下限 + 时长偏差（视频流口径）
- 支持单个切片与单个长视频（360s 已实测）
- JSON 报告（utf-8-sig），记录每条参数、距离、PSNR、门禁结果
- 入口：`pycut.py dedup`（`--check` 只自检不产出）
- **扩展边界**：新增去重滤镜不得破坏默认档位与已有门禁，详见 [DEDUP.md](DEDUP.md)
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

## 后续能力需求（不是开发状态）

### 1. transition —— 婚礼场景段间转场贴纸
**需求**：在婚礼视频的切片之间增加**转场贴纸**（如爱心、闪粉、相框等动态贴纸），
提升成片观感。

**要点（待细化）**：
- 在 montage 拼接的接缝处插入转场（淡入淡出 / 滑入 / 贴纸动画）。
- 贴纸资源：内置若干 PNG/WebM 贴纸，或支持用户自定义目录。
- 婚礼场景可能需要更柔和的转场与暖色调。
- 独立交付与验收需求见 [#35](https://github.com/natefox2017/py-cut/issues/35)。

### 2. dedup —— 容器、时间、结构、音频与文本扩展

**需求**：围绕 [DEDUP.md 第 3 节](DEDUP.md) 的八大功能模块扩展可选能力，
同时保留现有预设、seed 可复现、自检与音画完整性门禁。未经验证不得声称可通过平台审核。
音轨替换、水印修复、OCR/ASR 改写仅针对自有/授权素材；自动生成的文案需人工审核，
不得引入任何未经证实的功效/医疗宣称。

每一模块有独立的开发和验证契约，链接见 [#19](https://github.com/natefox2017/py-cut/issues/19)；
此处不维护优先级、认领人或完成状态。
### 3. quality high —— 高质量字幕修复（可选）
**需求**：当前 `subtitle.cover_clip` 用经典 TELEA 修复，大号艺术标题字形内部偏平滑。
提供 `--quality high` 走 AI 视频修复模型（ProPainter / LaMa）做到无痕，代价是速度慢约百倍。
**要点**：作为 slice/cover 的可选模式，默认仍用快速经典算法；需评估合法开源后端的许可、性能和音画完整性。交付与验收见 [#33](https://github.com/natefox2017/py-cut/issues/33)。

---

### 4. semantic slice —— 语义选片

**需求**：在原有场景切片候选集合上，可选使用带时间戳的语音转写与语义条件辅助筛选，
默认不得改动场景/人脸分流；ASR 不可用时应明确错误或可控回退。
独立需求与异常验收见 [#34](https://github.com/natefox2017/py-cut/issues/34)。

## 设计原则（贯穿后续）

- 所有新功能以**子命令**挂载到 `pycut.py` 入口，复用 `core/` 成熟能力，不重复造轮子。
- 目录按职责拆分，保持长期可维护（见 `AGENTS.md` 与 `README.md`）。
- 涉及 LLM 的视觉/文本能力统一走 `core/llm.py`。
- 涉及 ffmpeg 的封装统一走 `core/ffmpeg.py` + `core/slice_pipeline.cut_clip`。
