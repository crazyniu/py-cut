"""前贴：复用混剪去重/贴纸编码，再拼到已经完成字幕和音轨的正文前。"""
from __future__ import annotations
import random
import tempfile
from pathlib import Path
from .. import config
from .dedup_presets import derive_seed
from .montage_clips import _probe_one, concat_clips
from .scan import list_videos_recursive
from .utils import log, run_cmd


def prepend(video: Path, folder: Path, *, seed: int, canvas: tuple[int, int],
            stickers: list[Path], crf: str, keep_audio: bool) -> dict | None:
    files = list_videos_recursive(folder, max_depth=None, raise_on_error=True)
    if not files:
        log(f'[跳过] 前贴目录没有视频，直接输出正文：{folder}')
        return None
    rng = random.Random(derive_seed(seed, 'intro'))
    source = rng.choice(files)
    item = _probe_one(source)
    if item is None or item.duration <= 0:
        raise RuntimeError(f'前贴无法探测：{source.name}')
    duration = round(item.duration * config.MONTAGE_FPS) / config.MONTAGE_FPS
    segment = {'item':item, 'offset':0.0, 'take':duration,
               'dedup_seed':derive_seed(seed, 'intro', source.name)}
    if stickers:
        segment['sticker'] = str(rng.choice(stickers))
    with tempfile.TemporaryDirectory(prefix='intro_', dir=video.parent) as td:
        intro = Path(td) / 'intro.mp4'
        combined = Path(td) / 'combined.mp4'
        if not concat_clips([segment], intro, canvas=canvas, fps=config.MONTAGE_FPS, crf=crf)["ok"]:
            raise RuntimeError('前贴去重/贴纸处理失败')
        # 已处理前贴仅含视频，前贴原声单独从源视频取。正文字幕与声音已经完成。
        cmd = [config.FFMPEG, '-y', '-loglevel', 'error', '-nostdin',
               '-i', str(intro), '-i', str(video), '-i', str(source),
               '-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo']
        graph = f'[0:v]setsar=1,setpts=PTS-STARTPTS[v0];[1:v]setsar=1,setpts=PTS-STARTPTS[v1];'
        if keep_audio:
            a0 = '2:a:0' if item.has_audio else '3:a:0'
            graph += (f'[{a0}]aresample=48000,aformat=channel_layouts=stereo,apad,atrim=duration={duration},asetpts=PTS-STARTPTS[a0];'
                      '[1:a:0]aresample=48000,aformat=channel_layouts=stereo,asetpts=PTS-STARTPTS[a1];'
                      '[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]')
        else:
            graph += '[v0][v1]concat=n=2:v=1:a=0[v]'
        cmd += ['-filter_complex', graph, '-map', '[v]']
        cmd += ['-map', '[a]', '-c:a', 'aac', '-b:a', '128k'] if keep_audio else ['-an']
        cmd += ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', crf,
                '-pix_fmt', 'yuv420p', '-r', str(config.MONTAGE_FPS),
                '-movflags', '+faststart', str(combined)]
        rc, _, _ = run_cmd(cmd, timeout=3600)
        if rc or not combined.is_file() or combined.stat().st_size < 1024:
            raise RuntimeError('前贴与正文拼接失败')
        from .dedup import _video_duration
        expected = _video_duration(video) + duration
        actual = _video_duration(combined)
        if abs(actual - expected) > 2 / config.MONTAGE_FPS:
            raise RuntimeError('前贴拼接后时长异常')
        import os
        os.replace(combined, video)
    return {'source':str(source), 'duration':duration, 'dedup_seed':segment['dedup_seed'],
            'sticker':segment.get('sticker'), 'sticker_applied':segment.get('sticker_applied', False)}
