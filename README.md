# pycut — local short-video processing

A local toolbox for supplied video clips: subtitle coverage, similarity reduction,
and montage generation. Users prepare the clips; there is no public slicing command.
Developer documentation and source annotations use English. The product UI, logs,
CLI help, and established output-directory names remain Chinese.

## Desktop

The native Tauri workbench organizes product projects, material libraries, processing
tasks, published videos and material usage history. It reads media from the original
directories. Simple forms select inputs; read-only animations show each video's
actual processing state. The desktop requires an installed, logged-in Codex and reads its actual model
catalog for the shared AI settings.
The CLI/TUI remains available during desktop compatibility acceptance.

On macOS, run `./run-desktop.sh` to build and open the source workbench.
Node.js, Rust and the Tauri macOS build prerequisites must already be installed.
Project data persists in this checkout's ignored `work/desktop/app`, with disposable
media cache in `work/desktop/cache`. Existing `PYCUT_DATA_DIR` and `PYCUT_CACHE_DIR`
environment overrides take precedence.

See [Desktop workbench](docs/DESKTOP.md) for development, packaging, data boundaries
and native verification requirements. macOS 13.0 and Windows are packaging targets;
compatibility depends on the bundled runtime and requires platform execution.

## Commands and output

| Command | Purpose | Default output |
|---|---|---|
| `cover` | Locate subtitles and cover them with one frosted-glass rectangle | Input-root sibling `字幕查重/`; source preserved |
| `dedup` | Apply seeded video/audio/container variations and measure visual change | Input-root sibling `去重/` plus optional JSON report |
| `montage` | Assemble library shots around reference rhythm/audio; generate variants | One new flat video-only batch under the output root |

## Start

```bash
./run.sh                 # Interactive terminal: open the TUI
./run.sh tui             # Explicit TUI
./run.sh cover --help
./run.sh dedup --help
./run.sh montage --help
```

On Windows use `run.cmd` with the same arguments. Always run processing through the
launcher. It selects Python, installs missing requirements, checks OpenCV 4.x Haar
support and FFmpeg/FFprobe, prints gateway readiness without keys, and preserves
command exit codes. It creates `.env` from `.env.example` when missing.

Linux/macOS use the available package manager for missing Python/FFmpeg (Homebrew,
apt, dnf, yum, pacman, zypper, or apk as appropriate). Linux may require sudo. An
EXTERNALLY-MANAGED system Python uses project `.venv` instead of global pip changes.
Windows uses winget; install Microsoft App Installer if winget is unavailable.
Installation or revalidation failure stops startup. Actual Windows execution is
required for Windows acceptance.

macOS prefers the managed Python when present and Homebrew ffmpeg-full. Internal
legacy slicing uses VideoToolbox on macOS and libx264 elsewhere; dedup/montage use
libx264 where CRF is required. Override tools with `PYCUT_FFMPEG`/`PYCUT_FFPROBE`.

## Configure material and gateways

The TUI starts a local settings service and displays its complete URL. Copy that
URL into a browser manually; no browser opens automatically. Settings edit project
`.env`, reload before each task, and preserve unrelated entries/comments. The service
listens only on `127.0.0.1`, validates Host/Origin/session token, never echoes stored
keys, and stops when the TUI exits. Blank key input preserves the existing key.
External environment variables take precedence over `.env`.

| Setting | Contract |
|---|---|
| `PYCUT_REFERENCE_DIR` | Root with one reference video and same-stem SRT in each subfolder |
| `PYCUT_CLIP_DIR` | Background clip roots, also used by cover/dedup |
| `PYCUT_PIP_DIR` | Optional picture-in-picture clip roots |
| `PYCUT_INTRO_DIR` | Optional intro roots; no videos means skip |
| `PYCUT_OUTPUT_DIR` | Montage output root; created when necessary |
| `PYCUT_STICKER_DIR` | Optional local stickers; otherwise use online defaults |

CLIP/PIP/INTRO accept a single path or a JSON array string, for example:

```dotenv
PYCUT_CLIP_DIR='["/media/clips-a","/media/clips-b"]'
```

Do not split paths on commas/semicolons. Web rows support addition, removal, native
folder selection, and manual editing. The native dialog opens on the host running
pycut; headless hosts use manual paths. Cancelling leaves a row unchanged. Relative
paths resolve against the project root; home expansion, spaces, and Unicode work.
Input roots must exist. Merge all recursive roots without duplicate files or
following directory symlinks. CLI material arguments may be repeated.

Gateways are vendor-independent OpenAI-compatible `main` and `backup`:

| Variable | Default | Meaning |
|---|---|---|
| `PYCUT_MAIN_URL` | `https://api.deepseek.com/v1` | Base URL without `/chat/completions` |
| `PYCUT_MAIN_KEY` | Empty | Main key; legacy `DEEPSEEK_API_KEY` supported |
| `PYCUT_MAIN_MODEL` | `deepseek-flash` | Model identifier; cover requires vision support |
| `PYCUT_BACKUP_URL` | Main URL | Backup base URL |
| `PYCUT_BACKUP_KEY` | Empty | Empty disables automatic failover |
| `PYCUT_BACKUP_MODEL` | Main model | Backup model |

After three consecutive failures, a configured backup becomes the sticky delegate.
`--gateway backup` selects it explicitly for commands supporting LLM options;
`--no-fallback` disables switching. Explicit `--llm-url/key/model` overrides suppress
automatic failover. `.env` is ignored; never commit real keys.

## TUI workflow

The home menu contains cover, dedup, and montage. Up/down moves, Right/Enter opens,
Esc returns, and only Q exits the home menu. Directory configuration belongs to Web/
.env. Missing settings lead to configuration instructions. Only Enter on the final
confirmation starts processing.

Cover/dedup preview the configured MP4 scope and output locations. Montage follows
three steps: select reference videos, enter one mandatory variant count (1..99) for
every selected reference, then confirm the summary. Returning preserves selection
and counts. Ten references with count five plan fifty final videos. PIP is optional;
unchecking it avoids reading that material pool.

Normal pages share the original five-line PYCUT logo, actual settings URL, copy-to-
browser instructions, and content divider. Wide terminals show settings beside the
logo; narrow ones stack the sections. Short windows use compact branding. Minimum
supported size is 60x14. W displays manual settings access instructions.

CLI children run through tui_runner with live logs and failure propagation.
Cover/dedup show real stage counts, elapsed time, and result paths. Montage shows
one total progress bar for planned/completed final videos plus current activity;
internal stages do not count as completed videos. Errors reveal logs automatically;
L expands full logs. Completion/review/failure/interruption remains on a result page
until Enter/Esc returns or Q exits. Stage counts are never summed into a false total.

## CLI examples

```bash
# Cover supplied clips without overwriting sources
./run.sh cover --src-dir /media/clips-a --src-dir /media/clips-b
./run.sh cover --file /media/clip.mp4 --force

# Seeded deduplication, optional effects, or verification only
./run.sh dedup --all --preset standard
./run.sh dedup --file /media/clip.mp4 --preset aggressive --border --badge
./run.sh dedup --src-dir /media/clips-a --check

# Reference-driven montage and independent variants
./run.sh montage --source /media/reference/a/1.mp4 --fission 5
./run.sh montage --probe
./run.sh montage --source /media/reference/a/1.mp4 --seed 2026
./run.sh montage --no-pip --no-sub --no-sticker --intro-dir ""
```

A single configured reference can be selected automatically; multiple references
require explicit `--source` or TUI selection. See each command's `--help` for the
complete argument contract. Fixed inputs/seed reproduce parameter choices; encoder
versions and environments can change binary output.

## Processing contracts

### Subtitle coverage

Cover detects the union of subtitle regions across five sampled frames in one
multi-image vision request. It applies one full-width Gaussian-blur rectangle with
height/margins based on detection, preserving audio and pixels outside that region.
It does not reconstruct the obscured background. Three output samples form a separate
verification request. Keep frame-specific coordinates and cross-frame union; do not
reduce sampling to save requests.

A detection round shares a 30-second wait budget; each HTTP call is capped at 15
seconds and waiting updates progress. The gateway must support multi-image requests
and per-frame JSON. Uncertainty, incomplete replies, timeouts, or residual subtitles
require review and do not write verified completion markers. Confirmed no-subtitle
results are normal. Old unverified markers are reprocessed. Sampling is not exhaustive
frame-by-frame OCR; real model accuracy and coverage need actual-media inspection.
Encoding failures and cancellation return nonzero and stop dependent work.

### Montage

The body uses a fixed 720x1280 canvas at 30 FPS. Reference video contributes duration,
rhythm, same-stem SRT, and original audio. Background/PIP selection is independent.
Both apply standard geometry/pixel variation and silent picture processing. Default
PIP is a 216x384 top-right window with independent mirroring, feathering, and shadow;
retain source subtitles without running detection/coverage on PIP material.

Multi-shot continuous PIP is prepared sequentially into lossless small-window clips,
then overlaid once. Static finite-frame masks produce feather/shadow; never clone
source tail frames. Original SRT timing is retained; ASS uses a canvas-scaled font
(about 54 px) and translucent black box. With PIP, subtitle burning shares the final
encode. Without PIP, use the separate subtitle path.

Default stickers are fresh Twemoji PNG downloads (Twitter/contributors, CC BY 4.0).
An explicit sticker directory uses local images. Default seam fading is 0.2 seconds;
`--no-sticker` and `--seam-fade 0` disable these effects. Missing requested stickers,
failed downloads, repeated material, or insufficient footage reject publication.

Copy reference audio without filtering, speed changes, or extension. Trim only excess
audio to the body duration; never use `-shortest` to truncate video. Unsupported MP4
audio may fall back to AAC with a diagnostic. Missing reference audio rejects normal
publication; explicit `--no-audio` permits silence.

An optional intro is selected as a complete video, receives existing variation/
stickers, and retains its own subtitles/audio (silence is supplied when needed).
Join only after body subtitle/audio processing. Final duration is intro plus body;
joining may transcode audio to AAC for format consistency. Empty intro pools skip;
real access/probe/processing/join failures reject publication and preserve prior output.

Within a variant command, share reference profiling, pool scans, and sticker downloads
once. Each final video selects/plans/encodes independently. Metadata-only temporary
caches may span tasks when file/tool/query identity matches; changed/new/deleted media
is discovered live, cache failure falls back to probing, and processing success/random
plans are never cached. Clean downloaded sticker directories after the batch.

### Output safety

Cover/dedup default to `字幕查重`/`去重` beside each input root, retaining groups/numbers.
Preflight collisions across the whole batch and preserve source files. Explicit
`--out-dir` wins. Dedup reports remain optional JSON.

Montage creates one `混剪-YYYYMMDD-HHMMSS-<unique>/` batch below explicit/configured
output root (fallback: `混剪/` beside the reference root). All selected references and
variants are flat in that batch. Number collisions without overwriting. Keep only
videos, no JSON, and print the complete batch directory on completion. Diagnostics
remain in logs. Interrupted/failed processing never replaces a prior final video.

```text
media/
  reference/a/1.mp4
  reference/a/1.srt
  reference/b/1.mp4
  reference/b/1.srt
  clips-a/有人脸/clip_1.mp4
  clips-b/nested/clip_2.mp4
  字幕查重/有人脸/clip_1.mp4
  去重/有人脸/clip_1.mp4
  output/混剪-20261019-180000-a1b2c3d4/
    1_1.mp4
    1_2_1.mp4
```

## Repository map

| Path | Responsibility |
|---|---|
| `pycut.py`, `pycut/cli.py` | Entry point and public command registry |
| `run.sh`, `run.cmd`, `requirements.txt`, `pyproject.toml` | Runtime/bootstrap/package contract |
| `pycut/tui.py`, `pycut/tui_runner.py` | Wizards, subprocess logs, progress, results |
| `pycut/settings.py`, `web_config.py`, `native_directory_picker.py` | Settings and host-native folder selection |
| `pycut/folder_picker.py`, `web_dir_browser.py` | Compatibility picker/navigation helpers |
| `pycut/config.py` | Shared paths, gateways, calibrated defaults |
| `pycut/core/scan.py`, `output_paths.py`, `probe_cache.py` | Material discovery, safe output, metadata cache |
| `pycut/core/ffmpeg.py`, `utils.py`, `runtime.py`, `progress.py` | Probes, process execution, cancellation, progress |
| `pycut/core/llm.py`, `subtitle.py` | Vision requests, subtitle detection/coverage |
| `pycut/core/dedup*.py`, `fingerprint.py`, `quality_vmaf.py` | Effects, presets/profiles/audio, optional measurements |
| `pycut/core/montage_*.py`, `online_stickers.py` | Reference, shots, PIP, subtitles, effects, intro |
| `pycut/core/scene.py`, `faces.py`, `slice_pipeline.py` | Internal legacy/shared slicing helpers |
| `pycut/commands/` | Public command adapters and montage orchestration; internal slice adapter |
| `tools/` | Reusable QA/style-comparison tools |
| `tests/` | Lightweight and synthetic-media regression; explicit real-media opt-in |

## Development and validation

Read [AGENTS.md](AGENTS.md) before changes. This project uses GitHub PR/review/CI,
not Linear. Preserve unrelated work, use isolated `codex/` branches/worktrees, and
keep temporary media/scripts/reports in ignored `work/`. Do not commit `.env`,
`materials/`, generated videos, personal manifests, or credentials.

Use the Python path printed by the launcher for `-m pytest <affected tests> -q`.
Ordinary PRs run the required lightweight workflow; the complete synthetic suite is
manual Actions `TUI regression` / `workflow_dispatch`. Full verification is required
for relevant release/interface/architecture/security changes or explicit requests.
Do not bypass checks. `PYCUT_TEST_MEDIA=1` opts into real-media tests using test MP4s
under `materials/source`. Default mocked LLM checks do not prove vision accuracy.

Durable docs:

- [ROADMAP.md](docs/ROADMAP.md): product scope and candidate capabilities.
- [MONTAGE.md](docs/MONTAGE.md): montage timing/render/publication contracts.
- [DEDUP.md](docs/DEDUP.md): effect architecture and measurement limits.
- [DEDUP_AUDIO.md](docs/DEDUP_AUDIO.md): optional audio-editing contract.
- [DEDUP_PROFILES.md](docs/DEDUP_PROFILES.md): local JSON profile contract.
- [ENGINEERING.md](docs/ENGINEERING.md): engineering traps and validation boundaries.

Keep progress, bugs, temporary plans, test runs, and delivery evidence in chat/PRs,
not Markdown. Source/CI/merge success is distinct from actual visual/audio acceptance.
