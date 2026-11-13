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

# Lower pitch/tone while preserving speech speed
./run.sh dedup --src-dir /media/clips --copies 1 --voice-shift -1

# AB audio phase processing, optionally with RMS-matched background material
./run.sh dedup --file /media/authorized.mp4 --ab-audio hidden
./run.sh dedup --file /media/authorized.mp4 --ab-audio mislead --ab-source /media/audio
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
| `--voice-shift` | 0.0 semitones | -1.5..1.5; resample/rate/tempo pitch shift plus mild EQ, preserving speed |
| `--ab-audio` | off | hidden: opposite-phase stereo; mislead: phase processing plus background audio |
| `--ab-source` | Empty | One audio file or recursive directory; mislead only, otherwise use PYCUT_AB_AUDIO_DIR |

Use actual video-stream duration from the completed dedup picture encode, including
any enabled speed change; do not guess from source container duration. Align external
tracks at zero, resample to 48 kHz stereo, and encode only the mixed AAC audio. Copy
stage-one video with -c:v copy so mixing does not recompress picture. Linear track
gains can sum to excessive loudness; listen to the final mix.

## Voice and AB audio

The TUI sound row cycles through off, tone adjustment, hidden, and mislead. Tone
adjustment uses -1 semitone with complementary +/-1.5 dB EQ at 300/3000 Hz. The CLI
supports either pitch direction. A limiter bounds peaks, compensates latency, and
does not apply automatic makeup gain. Trim/pad the final audio to video duration.
These edits change the signal, not narration or a verified biometric identity.

AB audio follows the video preset and any optional audio mix; it does not bypass
the selected video processing. hidden computes M=0.425*(L+R), then outputs M/-M.
mislead selects a material and target RMS ratio (0.0411..0.0501) from an independent
seeded RNG. Loop/measure material over the target duration and clamp gain to
0.0001..4 using source_RMS*ratio/material_RMS. This ratio describes the pre-mix gain
reference, not an exact output loudness percentage. Reject silent selected material.

When a non-silent AB input produces RMS <=1e-6 or below 8% of source RMS, retry once
with independent 0.85*L / 0.85*R channels. If still too quiet, fail without publishing.
The stereo measurement does not detect subsequent mono downmix cancellation; the
TUI confirmation states that risk. No FFT or psychoacoustic masking is claimed.

Configure the optional AB material directory in Web settings. Only mislead requires
it; an explicit --ab-source wins. Scan once per invocation without directory symlinks,
then independently select each variant's material. Probe selected audio before muxing.
No source audio and no replacement/mix material means voice/AB processing is skipped.
Keep existing video presets, montage reference audio, and tail-only fission unchanged.

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
and temporary validation; core/ab_audio.py owns material selection, RMS measurement,
and phase filters; core/dedup.py invokes mixing after the original encoder.
Do not change preset/video calibration. Use standard-library subprocess argument lists,
core.utils.run_cmd, and configured media tools on the actual platform. Synthetic checks
verify streams/timing; real sound quality and synchronization require authorized-media
listening. macOS success does not replace other-platform execution.
