# Desktop workbench

The Tauri application provides project-scoped libraries, processing queues and
outputs over the existing cover, dedup and montage implementations. React and
Mantine own the ordinary-user forms. Users select materials, quantity and output,
start processing and view results. The video-processing entry lists montage, cover and dedup jobs together, with type
and activity filters. A compact task table shows type, status, final-video counts
and results, with twelve batches per page. New jobs select their type and restore
that type's saved quantity and output fields. Montage uses every non-excluded reference
video in the current project's reference library without a second picker. Inclusion is
stored per library membership and can be toggled on each reference thumbnail; excluded
videos stay visible and can be included again. Submission freezes the complete current
set independently of gallery pagination, search or old saved selections. Montage asks
for a shared per-video quantity and an output directory;
material pools merge the project's configured libraries by role. Configured
optional pools are used automatically; absent pools are skipped. Cover and dedup select folders and recursively process their
MP4 files without per-video selection or changing library membership.
Each job opens its own read-only
batch view, using a single thumbnail state card for each stable input/variant identifier.
Cards follow actual worker events and link published outputs by exact output ID;
running previews show an in-image processing indicator. Completed previews have no
completion badge. State filters share the top action toolbar. Task detail has no separate aggregate progress panel or bar. Waiting, completed, review, failure and cancellation
remain distinct. Internal stages never increase final-video counts. Each new desktop task creates its own output subfolder under the chosen root (or the
command's sibling output root when omitted). CLI output contracts are unchanged.
The finished-video library presents task folders first; opening a folder shows that
job's published videos with shared thumbnail previews. Folder actions target actual
output directories. Historical jobs retain their existing file locations.
The five system material types (reference, background, PiP, intro and sticker) are
fixed, with one catalog per type in each project. Users associate multiple recursive
roots per type instead of creating custom groups. Legacy groups of the same role
merge into the system catalog; existing removals remain removed. Directory management
shows full paths, indexed counts and read status. Adding or refreshing a directory
scans only the selected roots; removal cancels the association and preserves files.
Type selection, search, sorting and view controls share the primary toolbar;
media type, source root and removed-state filters expand on request. Refresh and
directory management share the page header with matching control heights. Directory
management and media details open in dialogs. Internal processing graphs, technical
parameters and routine logs stay out of the interface; failures offer copyable logs. Native window controls and dialogs remain
system-owned. Libraries, task items and outputs share one 9:16 thumbnail card
with contained pictures and captions over the bottom of the image. Unfinished task
states are shown inside previews. Library captions include source folders only when
the folder name adds information beyond the filename stem.
Creation actions align to the right edge of their toolbar. Choice sets with at most two options
use bordered button groups; larger sets use Select inputs.
All application modals share a dimmed, blurred backdrop; system pickers stay native.

## Data and processing

Each product is a project with its own libraries, roots, workbench settings,
tasks and usage history. Output directories are selected when creating tasks.
Codex model settings are global and apply to every project. Codex detection does
not block project browsing, library management, dedup or montage; cover requires a
ready Codex account and a saved model. The last project and main page are restored
on launch. Task forms retain explicit cancel actions, show their input/output scope,
and prevent closing or changing type during submission. Native windows support a
minimum size of 1024 by 680; long task forms scroll with persistent footer actions. The SQLite catalog lives in the platform's
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
Explicit folder jobs reuse CLI traversal and output-tree exclusions; their frozen
files enter the metadata catalog for thumbnails without becoming library members.

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

On macOS, `./run-desktop.sh` installs locked frontend dependencies when needed,
builds a debug application and opens it through Launch Services. Checkout-local
data and cache default to `work/desktop/app` and `work/desktop/cache`; explicit
`PYCUT_DATA_DIR` and `PYCUT_CACHE_DIR` overrides remain supported. The debug worker
continues to bootstrap Python and media tools through the existing launcher.

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
