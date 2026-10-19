# Deduplication requirements and architecture

The `dedup` command applies technical variations to supplied videos/clips and measures
visual change. This document separates durable contracts from candidate capabilities;
source/help and PR acceptance evidence determine support. It is not a progress tracker.

## 1. Scope and limits

Input is explicit MP4 files or recursively merged clip roots. Output is separate
processed video by default, optional explicit in-place replacement, and optional JSON
reports describing parameters/measurements. Preserve spoken meaning; technical effects
do not rewrite narration. Use only owned/authorized material. For the Jiuyuanxian
project, do not introduce efficacy/medical claims, including sticker/badge text.

No combination guarantees platform acceptance or copyright clearance. File hashes,
metadata, visual hashes/semantic features, audio, and OCR/ASR text can differ in what
they measure. A local score establishes only its defined metric. Operational behavior,
uploading, account login, titles/covers/topics, and platform policy are outside this
local processor. Do not retain unsourced platform percentages, approval thresholds,
or algorithm/version claims as implementation requirements.

## 2. Capability map

These eight families organize design options; each row distinguishes current scope
from candidates. Do not assume every listed FFmpeg filter is implemented or available
on a host. Preserve defaults and existing gates when adding effects.

| Family | Existing contract | Candidate extensions and constraints |
|---|---|---|
| M1 Container/encoding | libx264 CRF/GOP/audio-bitrate/frame-rate/metadata variation; optional --vary-bitrate | Further encoding/resolution choices need output-quality validation; do not enable ABR and CRF together |
| M2 Geometry | Bounded crop/zoom/rotation, caller-controlled mirror, optional --ken-burns | Translation/composition changes must retain valid dimensions and no unintended black edges; perspective remains disabled |
| M3 Pixels/colour | Brightness/contrast/saturation/hue/channel/noise/sharpen-or-blur | Curves/chroma-shift/blend/mask effects require actual picture checks; noise is not watermark-removal evidence |
| M4 Time/frame sequence | Explicit speed with synchronized audio and frame-rate variation | Frame dropping/interpolation, reverse, keyframe/reorder, or edge trimming require timestamp/duration/audio contracts; no generic current switches implied |
| M5 Structure/content | Optional border/badge/logo/sticker; montage changes shot composition | Split screen, animated overlays, xfade, custom intro/outro need duration, source, and rollback rules; existing montage fades do not overlap shots |
| M6 Audio | Pitch with duration compensation, synchronized speed; optional owned audio mix | Other normalization/TTS/remade tracks require authorization, listening, source/stream rules, and separate dependency evaluation |
| M7 Watermarks | Explicit visible rectangle --delogo with safe bounds | No general external-mask/inpainting module; blind-watermark tests require reproducible embedding/extraction per scheme and cannot guarantee removal |
| M8 Text | Existing cover plus SRT/ASS paths are reusable building blocks | OCR/ASR rewrite requires provenance, human review, and claim checks; no generic content_review/transcription command exists |

### Effect constraints

- Re-encoding changes bytes but does not establish semantic originality. Use libx264
  for CRF; VideoToolbox bitrate mode is not an equivalent CRF implementation.
- Geometric filters restore the original canvas. Match FFmpeg truncation for
  intermediate sizes. Crop after rotation to remove black edges. Structural layouts
  already resize, so skip redundant zoom/resampling there.
- Mirror is explicit and preset-controlled; text/logos/directional faces may become
  visibly wrong. Never treat mirror or a small overlay as acceptance proof.
- perspective_px remains zero in the stable parameter schema. The old filter distorted
  decoded pictures; do not restore without actual-version/filter/visual checks.
- Ken Burns uses animated scale(eval=frame)+crop, retaining PTS/FPS and valid crop
  margins rather than zoompan defaults that can change frame counts/rate.
- Pixel sharpening and slight blur are alternatives, not stacked cancellation.
- Pitch uses asetrate -> aresample -> inverse atempo to compensate duration. Playback
  speed adds a synchronized atempo step. Silent inputs do not claim pitch changes.
- Optional logo/sticker composition still needs filter_complex even without borders.
  Image order must match graph labels. Use single frames repeated by overlay, never
  unbounded image input loops. Average feather axes, retain bounded opacity.
- delogo requires an interior one-pixel margin. Invalid rectangles skip the filter
  rather than passing out-of-frame coordinates to FFmpeg.

## 3. Presets and reproducibility

`light`, `standard` (default), and `aggressive` select calibrated strength ranges and
probabilities from config/dedup_presets. Caller switches control mirror, speed,
border, badge, logo, sticker, and delogo; do not activate options merely because a
candidate table lists them. Preset permission/probability still applies. Keep the
stable parameter schema, including inert perspective_px, for existing consumers.

Derive seeds with stable blake2b components, never Python's process-salted hash().
Fixed path/seed/preset/switches reproduce parameter choices; binary output depends on
encoder/environment. A zero/unspecified seed retains existing source-path derivation.
Optional bitrate and Ken Burns choices use independent RNG streams so enabling them
does not shift the existing preset draws.

--vary-bitrate uses the existing VideoInfo/config bitrate baseline and bounds its
seeded target to +/-6%. Keep CRF defaults unchanged when disabled. Reports only add
optional ABR/animation fields when used. Montage reuses geometry/pixel variation only;
its summaries must not claim audio/CRF/GOP/frame-rate effects it did not execute.

Local account preference precedence/safety is defined in [DEDUP_PROFILES.md](DEDUP_PROFILES.md).
Audio mixing is defined in [DEDUP_AUDIO.md](DEDUP_AUDIO.md). Neither adds platform login
or uploads. Optional VMAF lives in core/quality_vmaf.py, requires explicit --check
options and alignment, and does not imply automatic quality fallback.

## 4. Verification metrics

| Check | Method | Current configured boundary |
|---|---|---|
| Visual change | Sample 12 frames, 32x32 DCT pHash, mean Hamming distance | light >=1.0, standard >=1.4, aggressive >=1.6 |
| Advisory change range | Compare distance to preset expected range | Upper values 8/10/40 respectively; excess warns, does not fail |
| Gross picture damage | Mean grayscale PSNR | Base floor 12 dB; structural cases use existing gate rules |
| Duration | Compare source/output video-stream durations | Configured base 0.35 s, expected speed change, and implementation margin |
| Optional VMAF | Explicit aligned source/output assessment | Optional --vmaf-min; no implicit rollback |
| Embedded watermark | Scheme-specific independent oracle | No general default removal/acceptance gate |

The duration helper retains fixed tolerance plus `d * (1 - 1/speed)` and the existing
margin; do not divide fixed tolerance by speed. Probe video-stream duration on both
sides. Container duration follows the longest stream and can hide shortened video
behind unchanged audio. Missing fields (including N/A with rc=0) use the existing
frame-count/rate, stream, then container fallback. Unknown duration is a probe failure,
not a measured zero.

Fingerprint sampling decodes sequentially and retrieves selected indices. Random
keyframe seek can select another shot. Resampling and speed changes limit temporal
comparability; frame-paired scores are not perfect alignment. pHash emphasizes low
frequencies; high-frequency noise may barely change it. PSNR loses alignment after
geometry changes, so treat it as a damage floor, not perceptual quality. Neither
score proves platform acceptance or embedded-watermark removal.

Calibrated floors account for variation across footage and seeds, particularly the
large difference between triggered/untriggered structural effects. Do not retune from
one typical clip. Historical per-clip/batch numbers without retained reproducible inputs
are not current benchmark evidence. Perspective remains disabled regardless of a
possible hash-distance increase.

## 5. Processing, publication, and reports

Flow: probe -> derive parameters -> construct filters -> encode isolated temporary
video -> optional audio mix -> fingerprint/duration verification -> atomic publication
-> report. Preserve per-file failure isolation and input-order summaries under bounded
concurrency. Match asynchronous results by explicit index. Preflight all destination
collisions before encoding, including duplicate stems across roots/case-insensitive
filesystems. Use process/thread-isolated temporary names.

Normal output may retain an encoded below-threshold result and mark ok=False for
review. In-place replacement requires explicit --in-place --force and passed checks
before touching the source. Cancellation/failure cannot publish incomplete output or
overwrite prior media. Existing-output skips are separate from successes/failures.

Default outputs are input-root siblings `去重/`, preserving groups/numbers. Explicit
--out-dir wins. JSON reports use UTF-8 BOM and retain source/output, seed, preset,
executed parameters, measurements, gates, errors, and timing. --no-report disables.
Keep reported PSNR finite. Optional fields appear only for enabled operations;
account reports use aliases and whitelisted preferences, never credentials.

## 6. Entry points and modules

```bash
./run.sh dedup --file /media/clip.mp4 --preset standard
./run.sh dedup --src-dir /media/clips-a --src-dir /media/clips-b
./run.sh dedup --all --preset aggressive --seed 20261010
./run.sh dedup --file /media/clip.mp4 --sticker /media/sticker.png
./run.sh dedup --all --check
./run.sh dedup --help
```

TUI reads Web/.env roots, previews count/output, and requires Enter to confirm.
Advanced preset/seed/threshold/audio/profile options remain explicit CLI inputs.
Users supply clips; internal legacy slicing is not a public workflow prerequisite.

| Module | Responsibility |
|---|---|
| commands/dedup_cmd.py | argparse, profile precedence, validation, summary |
| core/dedup.py | Filter graphs, encoding, gates, atomic publication, reports |
| core/dedup_presets.py | Preset ranges, stable seed/RNG choices, parameter summaries |
| core/fingerprint.py | Independent pHash/PSNR sampling and measurements |
| core/dedup_audio.py | Optional authorized audio probe/mix/validation |
| core/dedup_profiles.py | Strict local JSON whitelist and value provenance |
| core/quality_vmaf.py | Explicit optional aligned VMAF check |
| config.py | Calibrated defaults and ranges |

Follow [../AGENTS.md](../AGENTS.md). Reuse core capabilities, avoid new dependencies,
and test real implementations. Visual/audio changes require actual encoding, full
decode, frame inspection, listening, and independent stream/timing checks. Long-video
performance requires long inputs. Keep evidence in PRs rather than this design file.
