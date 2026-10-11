# Dedup 可选音轨编辑（M6）

本功能只处理使用者拥有使用与修改权限的视频和音频。它不下载音乐、不识别账号、不包含自动配音生成。所有新增开关默认关闭；没有传递任何音频编辑开关时，原有 `dedup` 视频编码、原声映射、变调/变速、自检、输出与报告行为不变。

## CLI 使用

```bash
# 保留原声，混入授权 BGM 与环境声（背景短音轨自动循环）
./run.sh dedup --file /path/to/authorized.mp4 \
  --bgm /path/to/licensed-bgm.mp3 --bgm-gain 0.25 \
  --ambience /path/to/ambient.wav --ambience-gain 0.30 \
  --audio-fade-out 1

# 用录制的配音替换原声（短配音剩余部分自动补静音）
./run.sh dedup --file /path/to/authorized.mp4 \
  --voiceover /path/to/recorded-voice.wav --voice-gain 1.0

# 只改变原声增益，或仅增加结尾淡出（不使用外部音频）
./run.sh dedup --file /path/to/authorized.mp4 \
  --original-gain 0.8 --audio-fade-out 0.5
```

## 参数和时序合同

| 参数 | 默认 | 行为 |
| --- | --- | --- |
| `--bgm PATH` | 空 | 原声保留，并追加背景音乐；短音轨循环，按成片时长截断 |
| `--ambience PATH` | 空 | 原声保留，并追加环境音；短音轨循环 |
| `--voiceover PATH` | 空 | 配音替换原声；短配音补静音（仍可叠 BGM/环境音），长配音截断 |
| `--original-gain` | 1.0 | 原声音量 0～2；使用 `--voiceover` 时不可改变 |
| `--bgm-gain` | 0.25 | BGM 增益 0～2，需要 `--bgm` |
| `--ambience-gain` | 0.30 | 环境音增益 0～2，需要 `--ambience` |
| `--voice-gain` | 1.0 | 配音增益 0～2，需要 `--voiceover` |
| `--audio-fade-out` | 0.0 秒 | 整条合成音轨末尾淡出 0～10 秒 |

音视频时长以**原去重编码阶段实际生成的视频流**为基准，涵盖原本可能启用的变速；不按源文件容器总时长猜测。BGM、环境音与配音按输入时间从零点对齐，输入统一重采样到 48 kHz 立体声。新增混音只编码 AAC 音轨，视频通过 `-c:v copy` 原样复用第一阶段编码结果，不二次压缩画面。各音轨使用指定线性增益并混合，不保证增益叠加后不会出现响度过高，最终成片应自行审听。

## 异常与发布安全

1. 命令行在开始处理视频之前拒绝不存在/不可读取文件、非法增益、过长淡出、与 `--check` 同时启用的音轨编辑。原生 FFprobe 识别每份外部音频，必须恰好有一条可识别 1～8 声道的音轨；不以扩展名判断是否可用。
2. 处理顺序是：**现有去重视频编码 → 独立临时音轨混合与视频 stream copy → FFprobe 检测视频/音频流、尺寸和时间对齐 → 原子替换暂存文件 → 原有去重自检和发布**。FFmpeg 失败、音轨损坏、无视频或无音轨、时长偏差超过 0.30 秒时拒绝发布，并清理混音临时文件。
3. 开启 `--in-place` 时仍需明确使用现有 `--force`；在写回原始视频前先执行原有 pHash/PSNR 自检。正常不加音轨参数的处理没有额外 FFmpeg 调用。
4. 原始素材没有音频时，可以仅加入 BGM/环境音/配音以创建新音轨；若只调整原声增益或淡出但源素材本来无音轨，则明确失败。
5. JSON 报告仅在启用音轨编辑时于参数中增加 `audio_mix` 对象，记录输入路径、增益、替换/保留规则、淡出以及音画时长和声道验证结果。没有为此新增账户、密钥或第三方依赖。

## 模块边界

`pycut/commands/dedup_cmd.py` 负责 argparse 与输入合法性检查；`pycut/core/dedup_audio.py` 负责音轨探测、滤镜图生成与暂存文件验证；`pycut/core/dedup.py` 在原编码成功后按需调用音频混合，不更改 `dedup_presets.py` 或视频滤镜标定参数。所有外部进程都通过标准库 `subprocess` 列表参数、项目现有 `core.utils.run_cmd` 与已配置的 FFmpeg/FFprobe 绝对路径调用。使用当前平台可用的 FFmpeg；合成媒体测试可以验证流映射与时长，实际音质和音画同步仍需授权素材审听验收，不能只针对 macOS。
