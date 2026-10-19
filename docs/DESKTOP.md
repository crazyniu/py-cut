# Desktop workbench

The Tauri application provides project-scoped libraries, processing queues and
outputs over the existing cover, dedup and montage implementations. React and
Mantine own the ordinary-user forms. Users select materials, quantity and output,
start processing and view results. A read-only animated batch view shows each
video waiting, processing, completed, review, failed or cancelled from actual worker
events. Internal stages never increase final-video counts. Library selection uses a top Select; directory
management and media details open in dialogs. Internal processing graphs, technical
parameters and routine logs stay out of the interface; failures offer copyable logs. Native window controls and dialogs remain
system-owned. Videos use a 9:16 preview area with contained pictures.

## Data and processing

Each product is a project with its own libraries, roots, workbench settings,
tasks, output root and usage history. The SQLite catalog lives in the platform's
application data directory. Source media stays in place. Recursive indexing merges
canonical files, ignores directory symlinks and retains metadata for offline files.
Removing an asset changes only its library membership; restoring or rescanning
does not delete or silently reactivate a removed source. Thumbnail and playback
proxies are disposable, stat-and-tool-keyed cache files, never processing inputs.

The Rust bridge communicates with one local JSON-lines service. Media URLs are
granted for exact paths returned by indexed-asset or published-output requests.
The service accepts an explicit method list and runs jobs serially in disposable
workers. Development workers use run.sh/run.cmd; packaged workers use their frozen
executable. Cancellation signals the worker; application shutdown also owns its
process group or Windows Job Object. Interrupted work remains visible after restart.

Submission freezes active file membership and file identities. Enabled library
roots must be readable; disabled pools are not read. Changed inputs reject the job
instead of silently substituting files. Processing retains the CLI algorithms,
calibrated defaults, verification, audio and output contracts. Multiple references
share a variant count from 1 through 99 and one flat montage batch directory.

Only atomic publication creates a montage output and counts material usage. A
prepared journal identifies the exact staged file, enabling idempotent recovery
after replacement. Every successful final containing an asset counts as one use,
with actual role, source offset, output duration, speed and consumed source duration
where applicable. The ledger retains the submitted file version. Plans, failures
and cancelled work do not count; interrupted batches retain already published finals.

## Subtitle detection

The desktop requires an installed, logged-in Codex as its shared AI runtime. Its
model picker queries Codex's actual paginated model catalog. Cover currently uses
image inputs; image generation for stickers and covers is future scope. Other
Agents and legacy .env import are not desktop entry points. Codex recognition uses
ephemeral read-only threads with inherited tools, MCP, plugins and hooks disabled,
verifies the empty MCP inventory and retains existing detection deadlines. Model
discovery does not establish successful inference; uncertain results remain review.
The legacy CLI/TUI and gateway path remain available.

## Native development and packaging

Install requirements.txt for source development, then run `npm ci` and
`npm run tauri dev` inside desktop/. Rust and the platform's Tauri prerequisites
are required. A browser preview displays interface structure without inventing
catalog entries or processing results.

For installers, install requirements-desktop.txt using the target-platform Python
and run `python tools/build_desktop.py --build`. `--prepare` freezes the worker and
collects FFmpeg, FFprobe, fonts and media dependencies without building installers.
Use `--media-dir` for target-native media binaries and `--font-dir` for local
NotoSansSC font/license files. FFmpeg requires libass. Preserve runtime licenses and
corresponding source obligations before distributing binaries.

macOS targets 13.0. The packager records the minimum OS of every bundled Mach-O,
including Python extensions and relocated media libraries, in runtime-manifest.json.
Installer builds stop if any dependency requires a newer OS. Setting a Rust or
compiler deployment target alone does not make newer binaries compatible. Prepare
can report an incompatible runtime for diagnostics. Windows requires a compatible
static FFmpeg distribution. Build each architecture on its target platform; a build
does not establish installation, playback, processing or older-system acceptance.

PR checks cover lightweight Python regressions and the desktop frontend build.
Full synthetic-media regression and native installers run through manual workflows.
Native acceptance must exercise real project creation, library indexing, preview,
submission, publication, cancellation and restart. Media acceptance additionally
requires complete decoding, frame inspection and audio listening when affected.
