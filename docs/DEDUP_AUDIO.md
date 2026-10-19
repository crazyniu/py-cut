# Optional dedup audio editing (M6)

Process only video/audio the user owns or is authorized to modify. This feature does
not download music, identify platform accounts, or generate speech. All editing
switches default off; no audio-edit options means the existing video encoding,
source-audio/pitch/speed mapping, verification, output, and reporting path is unchanged.

## CLI

```bash
# Preserve original sound; loop licensed background music and ambience
./run.sh dedup --file /media/authorized.mp4 \
  --bgm /media/licensed-bgm.mp3 --bgm-gain 0.25 \
  --ambience /media/ambient.wav --ambience-gain 0.30 --audio-fade-out 1

# Replace original sound with recorded narration; pad a short voiceover with silence
./run.sh dedup --file /media/authorized.mp4 \
  --voiceover /media/recorded.wav --voice-gain 1.0

# Edit original gain/fade without external audio
./run.sh dedup --file /media/authorized.mp4 \
  --original-gain 0.8 --audio-fade-out 0.5
```

## Parameters and timing

| Argument | Default | Contract |
|---|---|---|
| `--bgm PATH` | Empty | Preserve original, add looping background music, trim to picture |
| `--ambience PATH` | Empty | Preserve original, add looping ambience |
| `--voiceover PATH` | Empty | Replace original; pad short voiceover, trim long voiceover; BGM/ambience may remain |
| `--original-gain` | 1.0 | Linear gain 0..2; cannot change with voiceover replacement |
| `--bgm-gain` | 0.25 | Gain 0..2, requires --bgm |
| `--ambience-gain` | 0.30 | Gain 0..2, requires --ambience |
| `--voice-gain` | 1.0 | Gain 0..2, requires --voiceover |
| `--audio-fade-out` | 0.0 seconds | End fade for the complete mix, 0..10 seconds |

Use actual video-stream duration from the completed dedup picture encode, including
any enabled speed change; do not guess from source container duration. Align external
tracks at zero, resample to 48 kHz stereo, and encode only the mixed AAC audio. Copy
stage-one video with -c:v copy so mixing does not recompress picture. Linear track
gains can sum to excessive loudness; listen to the final mix.

## Errors and publication

1. Before video processing, reject missing/unreadable files, invalid gains/fades, and
   mixing combined with --check. Probe external audio using configured FFprobe; require
   exactly one recognizable audio stream with 1..8 channels, regardless of extension.
2. Flow: existing video encode -> private audio mix plus video copy -> validate video/
   audio presence, dimensions, timing -> atomically replace staging -> existing dedup
   verification/publication. FFmpeg failure, corrupt/missing streams, or audio/video
   deviation above 0.30 seconds rejects publication and cleans mixing files.
3. In-place still requires --force and existing pHash/PSNR checks before source
   replacement. With no edit switches, do not add FFmpeg invocations.
4. A silent source may gain BGM/ambience/voiceover. Original-only gain/fade on a source
   without audio fails explicitly.
5. Only enabled mixing adds an audio_mix report object: source paths, gains, replace/
   preserve rules, fade, durations, channels, and validation. No accounts/keys/dependencies.

## Ownership and validation

commands/dedup_cmd.py owns arguments/preflight; core/dedup_audio.py owns probes, graphs,
and temporary validation; core/dedup.py invokes mixing after the original encoder.
Do not change preset/video calibration. Use standard-library subprocess argument lists,
core.utils.run_cmd, and configured media tools on the actual platform. Synthetic checks
verify streams/timing; real sound quality and synchronization require authorized-media
listening. macOS success does not replace other-platform execution.
