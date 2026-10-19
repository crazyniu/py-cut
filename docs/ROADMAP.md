# Product contracts and candidate capabilities

This is durable product/design scope, not a backlog or status tracker. Current user
requests define implementation scope; source and PR evidence establish behavior.
See [ENGINEERING.md](ENGINEERING.md), [DEDUP.md](DEDUP.md), and [MONTAGE.md](MONTAGE.md).

## Public contracts

| Command | Input and behavior | Output/verification |
|---|---|---|
| `cover` | User-supplied recursive multi-root MP4s; vision localization and one full-width Gaussian blur | Separate output, original audio, atomic publication, verified markers; uncertainty requires review |
| `dedup` | Seeded container/geometry/pixel/time/structure/audio variations with three presets and explicit switches | pHash change, PSNR damage floor, video-duration checks, optional UTF-8 BOM JSON report |
| `montage` | Reference rhythm/audio/SRT plus independently selected background and optional PIP/intro pools | Fixed body canvas/FPS/duration, original SRT timing/audio, flat video-only variant batch |

Users supply clips. Do not restore a public slice command, source-video settings,
or slicing wizard. cover/dedup use Web/.env roots and preserve sources. Montage TUI
selects references, requires one shared 1..99 variant count, then confirms. All
material roots scan recursively with file deduplication and no directory-symlink
traversal. Explicit CLI arguments remain supported.

Montage retains its A/B/C/D/E flow: reference profiling, seeded varied background,
bounded continuous PIP, styled SRT/ASS, then original audio. PIP/subtitles share the
final encode when enabled; optional intro joins after the body. Preserve calibrated
subtitle style and actual frame/audio completeness. Long-video performance requires
matching input-scale evidence.

## Candidate designs, not implemented-interface promises

### Transition artwork

Wedding-oriented seams may use hearts, glitter, frames, softer motion, or warmer
art direction. Candidate implementations include fades, slide-ins, and animated
PNG/WebM artwork from bundled or user-provided material. Existing seam fades do not
imply these transitions exist. Define timing, resources, licensing, duration, and
rollback before implementing any new mode.

### Dedup capability extensions

The eight families in DEDUP.md describe container, geometry, pixel, time, structure,
audio, watermark, and text directions. Preserve preset defaults, deterministic
parameter selection, verification, and audiovisual integrity when extending them.
Audio replacement, repair, and OCR/ASR rewriting require owned/authorized sources;
automated text needs human review and must not introduce unsubstantiated efficacy/
medical claims. Do not infer CLI support or platform acceptance from design tables.

### Optional high-quality subtitle reconstruction

Current cover obscures with frosted glass; it does not reconstruct background.
A future --quality high could evaluate ProPainter/LaMa or other external backends,
subject to explicit authorization, licensing, compatibility, measured quality/speed,
and audiovisual integrity. No such public mode is implied. Keep the default Gaussian
coverage and current requirement not to depend on a local model.

### Semantic selection

ASR/OCR-guided semantic shot selection is a separate candidate from rhythm-driven
montage. Define provenance, model/dependency cost, selection quality, and human text
review before adding it. Current montage does not need transcription.

## Extension principles

Add public commands as register/run modules and register them in cli.COMMANDS.
Keep reusable operations in core; use core/llm.py for model calls and existing
FFmpeg/process helpers for media work. Keep modules focused, no duplicate processing
pipelines, and follow [../AGENTS.md](../AGENTS.md). Do not store priorities, owners,
current progress, or temporary test reports here.
