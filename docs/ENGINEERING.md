# Engineering boundaries and validation

This document stores durable constraints, design parameters, and reproducible lessons.
Keep bugs, progress, temporary reports, and CI/PR history in chat/PRs. Product contracts
are in [DEDUP.md](DEDUP.md), [MONTAGE.md](MONTAGE.md), and [ROADMAP.md](ROADMAP.md).
Current source and acceptance evidence determine capability; closed issues do not.

## 1. Similarity measurements and effect boundaries

The eight dedup capability families are a design map, not an implemented-feature list.
Inspect core/dedup.py, dedup_presets.py, fingerprint.py, command help, and tests before
claiming support. Perspective distortion remains disabled: perspective_px stays zero
in the stable parameter schema. Restore it only after checking actual FFmpeg options,
output geometry, and decoded pictures; do not assume an `s=` option exists.

pHash uses low-frequency DCT coefficients and is insensitive to much high-frequency
noise/sharpening. A small hash change does not establish that a pixel effect failed,
and no hash score proves embedded-watermark removal. Use a reproducible embedding/
extraction oracle for each watermark scheme.

PSNR compares corresponding pixels. Crop/scale/rotation destroys alignment, so it
is a damage floor (`DEDUP_MIN_PSNR=12.0`), not a perceptual quality score. Neither
PSNR nor pHash guarantees platform acceptance. Probabilistic structural effects
require calibration covering both triggered and untriggered seeds; never use a
single typical high distance as the minimum for all presets/material.

Historical measurements without retained inputs/commands/versions are research
context, not current evidence or permission to change calibrated constants.

## 2. Subtitle coverage

Vision-model localization determines height; the rectangle spans the full frame
width with vertical margins. Apply one Gaussian blur and one overlay without
pixelation or per-glyph inpainting. This obscures text, not restores its background.
Failed/uncertain detection must not persist absence or verified-completion markers.
Encoding failure preserves the source. See README and AGENTS for shared detection
budgets, multi-frame requests, verification samples, and review semantics.

## 3. Montage timing and encoding

### Reference rhythm and original timing

Continuous background music can prevent silence detection from finding useful
speech breaks. The calibrated silence threshold is -20 dB; evaluate new material
before tuning it. Prefer SRT-derived shot budgets when subtitles exist, otherwise
speech intervals plus filler slots. Shot budgets select library footage, never
reference pictures. Rendering retains original SRT timestamps and short cues,
clipping only at body boundaries. Body duration equals reference video duration.

Burn ASS inside the final PIP encode when PIP is enabled; retain the separate subtitle
path otherwise. PIP material keeps its own subtitles and never runs cover detection.
Finish body subtitles/audio before optional intro concatenation; final duration is
intro plus body.

Copy original body audio without filtering, speed changes, or extension. Bound excess
audio to picture duration; never use `-shortest` to shorten the video. If the output
container cannot represent the audio codec, use explicit AAC compatibility fallback.
Intro joining unifies/transcodes audio, so do not claim packet-identical audio after
joining. Empty intro pools skip; genuine read/probe/processing failures must surface.

### Background normalization

Fuse segment geometry/pixel effects, mirror/speed, sticker, and seam fades into one
encode. Offset fades along the same pre-output-seek timeline so fine seek does not
cut them off. Normalize dimensions, FPS, SAR, pixel format, and codec before concat;
then stream-copy their concatenation. The optional dynamic-backdrop composition
adds one bounded full-body encode before PIP/subtitles/audio, preserving the body
frame budget. Validate every
selected segment and final duration before atomic publication.

Compare filled/required frame budgets at target FPS. Once the budget is covered, do
not add a one-frame shot for a sub-frame seconds-rounding residue. Successful step b
uses only background/output settings; disabled PIP/intro paths cannot block it.

### Bounded continuous PIP

Bound decoder/filter/output threads instead of allocating CPU-wide pools per source.
Prepare multi-shot continuous PIP sequentially as FFV1/BGRA lossless small-window
clips, then read them through concat demuxer. Final composition has just background
and small-window video inputs regardless of source count. Single-shot/gapped paths
retain their direct composition.

Scale before applying shared stronger geometry/pixel variation with independent
segment seeds, retain random mirroring, and restore SAR=1. Compute feather/shadow
spatial masks once and repeat only these static masks for the finite frame budget.
Continuous mode overlays the main canvas once; gapped mode precomposes each small
window without restoring per-shot full-canvas shadow/feather chains.

Plan complete shots beyond body duration to absorb boundary rounding. Record actual
encoded frames from FFmpeg progress; a one-frame short individual clip is acceptable
when the whole track still covers the body. Quantize cumulative boundaries, truncate
once at final composition, and verify the required real tail on the visible stream.
Do not clone source frames with tpad, use background duration as proof of PIP coverage,
or discard continuous-mode short tails using the gapped-mode minimum. Clamp after
millisecond truncation while retaining allow_short. framesync/CFR cannot extend the
body frame budget. Clean small-window files on success, failure, and cancellation.

Stream-copied H.264 backgrounds may switch BT.709/601 colour metadata despite fixed
geometry/FPS. Use `-reinit_filter 0` for that normalized background input during final
PIP rendering to prevent graph reconstruction from resetting counters/static masks.
Keep actual PIP tail validation; do not suppress it or freeze-pad to hide missing frames.

Retain the first FFmpeg root cause and trailing diagnostics, explicitly mark omitted
middle output, and propagate original stage-C errors. Timeout diagnostics must include
output captured before termination, not just the elapsed wait.

## 4. Publication, cancellation, and temporary files

Each CLI montage creates a batch; TUI reference subprocesses share one batch. Flatten
all reference/variant videos, avoid filename collisions by numbering, keep only videos,
and print the absolute batch path. Diagnostics belong in separate logs, not output JSON.

Validate inputs and plan background/PIP before encoding. Isolate backgrounds, segment
directories, and concat lists by invocation/process/thread. Keep the background only
for successful step b; otherwise clean it on every exit path. Render all later stages
privately and publish only the complete result.

Check cancellation before stages, after encoders, after verification, and before final
publication. A command-summary-only stop check is insufficient. Dedup verifies its
temporary result first: normal output may retain a below-threshold result, but in-place
replacement requires passed checks. Cancellation publishes nothing and preserves prior
outputs/sources. Cover also checks cancellation at command publication boundaries;
uncertain detection remains review.

## 5. Material enumeration and metadata cache

Background/PIP use one full JSON metadata query. Temporary SQLite stores valid read-only
metadata, keyed by resolved path, dev/inode/size/mtime_ns/ctime_ns, FFprobe executable
identity, and query arguments. Never cache probe failure/invalid values or files that
change while probing. Corruption, permission errors, and lock timeout fall back to
real probing. Enumerate directories live so additions/deletions remain visible.
Do not cache random plans, processed results, or success/preparation state.

Default landscape exclusion is `w/h > MONTAGE_MAX_SIDE_RATIO` (1.40). Rotation
metadata determines displayed width/height before this comparison. Full-content
hash confirmation prevents sampled-signature collisions from removing distinct media.
Inspect current capacity; never infer it from an old library count.

## 6. FFmpeg and verification traps

- Any explicit `-map` disables automatic stream selection. Map `0:v:0` as well as
  optional audio; otherwise a valid-looking command can produce audio only.
- Do not add `-loop 1` to still-image inputs. overlay repeats the single frame;
  unbounded looping can keep FFmpeg alive indefinitely.
- probe_scalar without `-select_streams` can return video rate followed by audio
  `0/0`. Parse the first line; selected-stream probes do not test this behavior.
- Prefer video-stream duration. Container format=duration is the longest stream and
  can hide shortened video behind unchanged audio. Missing values need the existing
  frame-count/rate fallback, with container duration only as the last resort.
- Request scalar default output for video duration. CSV can append an empty field
  when the stream carries ambient-viewing side data, making a numeric duration
  fail float parsing.
- Direct concat inpoint/outpoint or mixed-format inputs can return rc=0 with shortened
  output. Double-seek/normalize each segment before stream-copy concat.
- Speed effects need take * speed source time before setpts compresses the interval.
  Do not alter default mirror/speed behavior when their options are disabled.
- FFmpeg scale expressions truncate dimensions. Match that integer behavior; rounding
  can overestimate the available crop width by one pixel.
- Average feather weights across axes rather than multiplying them.
- drawbox uses iw/ih; overlay uses main-input W/H and secondary w/h.
- Sequentially decode fingerprint samples and retrieve selected indices; random
  keyframe seeks can select another shot. Different duration/speed also limits what
  frame-paired scores establish; do not present them as perfect temporal alignment.

Test real implementation functions, not hand-built equivalents that accidentally
remove a failure mode. Complete decoding and frame inspection are required for
visual changes; listen when audio is affected. Filter strings, rc=0, reports, and
mocks cannot replace that evidence. TUI exposes frequent choices while advanced
thresholds/concurrency/seed remain CLI options; noninteractive execution shows help.
