# Montage processing contract

Durable requirements and implementation boundaries only. Keep validation/delivery
runs in PRs. Current source and actual encoding determine support, not old measurements.

## 1. Inputs, output, and invariants

- Users supply background/PIP/optional intro clips. Each supports multiple recursively
  merged roots without duplicate files or directory-symlink traversal.
- Each reference subfolder contains one video and same-stem SRT. Reference contributes
  speech rhythm, body duration, subtitle timing, and original audio; its picture is
  not a background source. Random library footage need not match the spoken semantics.
- Body output is fixed 720x1280, 9:16, 30 FPS and retains reference video duration.
  Optional intro increases final duration by its complete length.
- Independently select background/PIP and apply standard geometry/pixel variation.
  Background/PIP picture stages are silent; attach reference audio after subtitles.
- Default PIP is top-right 216x384 with aspect-preserving fit, random mirroring,
  feathering, and shadow. Preserve its existing subtitles; never run cover on it.
- Default stickers are newly downloaded Twemoji PNGs (Twitter/contributors, CC BY 4.0);
  temporary credits.json retains attribution. Explicit --sticker-dir uses local images;
  --no-sticker disables. Downloads try GitHub Raw and jsDelivr for the same pinned
  asset in at most two rounds (15 seconds per request), validating every response.
  Exhausted downloads report host/reason and reject publication, as do selected-image failures.
- Default seam fade is 0.2 seconds without overlap or duration loss; --seam-fade 0 disables.
- Reject repeated background material, insufficient coverage, or failed stages. Check
  inputs and plan both pools before encoding. Preserve prior output on failure/interruption.
- --fission N produces N independently seeded variants per reference. Share preparation
  only within that command, never random choices or processing-success state.
- Output is one newly created flat video-only batch for all references/variants. No
  per-video JSON is written; seeds/diagnostics remain in separate logs. Effects do not
  prove embedded-watermark removal or platform acceptance.

## 2. Stage A — reference profile and rhythm

`core/montage_source.py` reads dimensions, duration, FPS, audio presence, and speech
intervals. Reuse the full metadata query/cache shared by background/PIP/subtitle paths.
Body timing must use video-stream duration, not a potentially longer audio/container.

Run silencedetect with video/subtitle/data decoding disabled. Parse silence events
from stderr, not stdout. Speech is the complement of silence; close an unmatched
trailing silence at duration. Use calibrated -20 dB / 0.35-second silence defaults,
merge gaps below 0.05 seconds, drop speech below 0.8 seconds, and cap long intervals
at 8 seconds. Continuous background music can change detection; evaluate the audio
before tuning thresholds.

When SRT exists, derive shot-length budgets from cues; rendering still retains each
cue's original timestamp and short text. Otherwise use speech rhythm. Fill pauses/
tails with bounded slots to cover the complete body, comparing target frame budgets
rather than adding shots for sub-frame residue. Slot positions are timing budgets,
not instructions to cut pictures from the reference.

No ASR is needed for this path. Semantic selection/transcription would require a
separate design and dependency/quality evaluation.

## 3. Stage B — background selection and normalization

`core/montage_clips.py` scans supported videos using filesystem traversal, filters
landscape by width/height <=1.40 unless --allow-landscape is explicit, and retains
real source durations until selection. Concurrent probes carry explicit indices to
retain stable source order and seeded reproducibility. Per-file invalid metadata
is skipped; real directory errors surface.

Deduplicate by file size, weak first/last-chunk signature, then full digest for
collisions. A partial hash alone cannot establish identical content. Pick seeded
unused material for each duration slot. The planner can report reused/shortfall fields;
the complete generation path rejects either rather than silently repeating/filling
with frozen frames.

Apply optional seeded mirror/speed/sticker choices. Reserve take * speed source
seconds before acceleration so output slots stay full. Fit using proportional scale
and pad; never stretch or crop source content merely to fill the canvas.

Encode each segment once with fused geometry/pixel effects, sticker, and fades.
Double seek (coarse input plus fine output) ensures accurate cuts. Fade timing is
shifted by fine seek to keep the visible fade. Normalize dimensions/FPS/SAR/pixel
format/codec, use silent libx264 CFR, then concat by video stream copy. Direct
inpoint/outpoint concat or mixed formats can silently truncate despite rc=0.
Validate every segment and resulting duration before atomic publication.

## 4. Stage C — bounded picture-in-picture

`core/montage_pip.py` scans the optional pool and plans nonoverlapping intervals with
seeded source/offset choices. Never request offset+duration beyond source availability;
clamp after rounding/truncation. PIP material is not reused to disguise shortages.

Continuous mode starts at zero without gaps and plans complete shots beyond the body
for rounding slack. Prepare multi-shot tracks sequentially: scale and apply standard
variation with derive_seed(seed, "pip", i), preserve random mirroring, restore SAR=1,
and save lossless FFV1/BGRA small windows. Record actual encoded frames from progress;
individual clips may be one frame short if the whole real track covers the body.
Concat demuxer opens the prepared windows sequentially; do not open every original
source at once. Final composition has two video inputs and one main-canvas overlay.

Small-window feather width is 22% of window width; shadow spread is 3.5%, alpha 45%.
Compute spatial alpha/shadow once and repeat only static masks for a finite budget.
Never repeat source tail frames or restore per-shot full-canvas double overlays.
Optional ASS burns during this final encode.

Single-shot mode renders directly. Explicit --pip-gap retains gapped intervals with
shifted PTS and enable expressions. Continuous tails must not inherit the gapped-mode
minimum duration. Quantize cumulative frame boundaries and truncate once to the body
frame budget; framesync/CFR cannot extend it. Verify real PIP coverage on the visible
stream; insufficient frames reject publication instead of tpad/frozen-tail padding.

For the normalized stream-copy background, retain -reinit_filter 0 so BT.709/601
metadata switches do not reconstruct the graph and reset PIP counters/static masks.
Keep original diagnostics on errors/timeouts and clean small windows on every exit.

## 5. Stage D — reference subtitles

`core/montage_subtitles.py` parses SRT, builds styled ASS, and burns through libass.
Keep original timestamps, including short cues; clip only outside body bounds. Do
not assign subtitles to random shot order.

Scale text/margins with the canvas (font ratio 0.042, about 54 px at the default
height). BorderStyle=3 uses a translucent black box, bottom-centred, clear of PIP.
Style comparison helpers may explicitly request outline mode; do not change defaults
without visual validation. Discover CJK font families with fc-list, not hard-coded
font-file existence (macOS fonts can live in on-demand AssetsV2 paths). Available
font candidates and renderer behavior depend on the actual host.

Wrap text manually using ASS hard line breaks. For a single-quoted subtitles filename,
escape backslash, colon, and quote in the existing parser order; leave quoted commas,
brackets, semicolons, equals signs, spaces, and Unicode literal. Extra escaping can
break the filter. With PIP, burn ASS in its final graph; otherwise use the separate
subtitle encode. The caller owns temporary ASS cleanup.

SRT resolution: --no-sub or --srt "" disables; explicit path wins and must exist;
--srt auto (default) pairs the source stem in the same directory. Missing automatic
matches log a skip; missing explicit paths fail before encoding.

## 6. Stage E — original audio, then optional intro

Silent B/C/D processing finishes before audio attachment so later -an cannot remove
reference sound. Map video and reference audio explicitly and stream-copy both when
compatible. Do not filter, speed up, or extend reference audio. Bound excess audio to
picture duration; short audio never truncates picture via -shortest. Use an explicit
AAC fallback only for container incompatibility. Missing reference audio rejects
normal publication; --no-audio explicitly permits silence.

Absent/empty intro roots skip. A nonempty pool selects one complete intro, applies
existing variation/stickers without subtitle coverage, and retains its own sound
(or supplies silence). Adapt only intro video as needed; copy completed body video.
Join after body processing; audio may transcode to AAC for uniform format. Genuine
access/probe/encode/join errors preserve any prior final output. Temporary replacements
must share the target filesystem.

## 7. CLI, TUI, and output ownership

```bash
./run.sh montage --source /media/reference/a/1.mp4 --fission 5
./run.sh montage --step a                  # Profile only
./run.sh montage --step b                  # Keep normalized background
./run.sh montage --no-pip --intro-dir ""    # Disable optional material
./run.sh montage --no-sub --no-sticker
./run.sh montage --no-audio                # Explicit silent output
./run.sh montage --probe                   # Library statistics only
./run.sh montage --seed 2026
./run.sh montage --sticker-chance 0.9
./run.sh montage --speed-max 1.5 --mirror-chance 0.5
./run.sh montage --allow-landscape
./run.sh montage --srt /media/custom.srt
```

TUI uses Web/.env roots: select reference videos -> enter one common 1..99 count ->
confirm. Preserve selection/count when returning. Explicit --out-dir overrides
configured root; fallback is `混剪/` beside the reference root. Every invocation
creates `混剪-YYYYMMDD-HHMMSS-<unique>/`; all selected references/variants share it,
flat, with numbered filename collisions and only completed videos. Print its full path.

Use isolated backgrounds/segment directories/concat lists per invocation. Successful
step b retains its background; other success/failure/interruption paths clean it.
Later stages operate privately and publish once complete. Check cancellation around
encoding, verification, and publication; no incomplete overwrite is allowed.

## 8. Validation

Use authorized short reference/SRT/background/PIP inputs for a real full path. Check
fixed geometry, frame count, full decode, body/PIP tail coverage, subtitle timing,
stickers, composition, audio source/duration, and optional intro duration. Inspect
frames and listen to audio. --fission 2 checks count, independent seeds, and visible
variation. Test no-audio reference, invalid SRT, material shortage, and interruption
for nonzero exits and existing-output preservation. Mocks/metrics do not prove model
accuracy, visual quality, watermark removal, or platform acceptance. Short inputs do
not establish long-video performance. Tests live in test_montage_*.py/test_pip_*.py;
record actual execution/evidence in PRs.
