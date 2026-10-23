# AGENTS.md — AI contributor contract

Read this file and [docs/ENGINEERING.md](docs/ENGINEERING.md) before editing.
These requirements apply to all contributors, including the current agent.

## 0. Product and execution ownership

`pycut` is a maintained local short-video toolbox with three public commands:
`cover` (subtitle coverage), `dedup` (similarity reduction), and `montage` (remixing).
Users supply clips. Do not restore a public `slice` command or slicing wizard.
The TUI is the maintained interactive interface; it uses the existing CLI/core
implementations through `pycut.py` subcommands.

Codex owns requirements, implementation, review, integration, testing, acceptance,
commits, and merging. Use native Codex tools or direct CLI for desktop/browser,
terminal, worktree, GitHub, and delivery operations. Do not use Orca, OMP, or Claude
for development, review, testing, or acceptance. This project rule overrides older
parent rules assigning Codex only a coordination role; other release/PR rules apply.

### 0.1 Start-of-work audit

- Read root/applicable nested AGENTS files, relevant long-lived docs, and real source.
- Verify remotes/default branch, fetch, inspect branch/worktrees/dirty files, open
  PRs, reviews, CI, and concurrent work. Preserve unrelated changes; avoid duplicate PRs.
- This project does **not** use Linear. Do not query, create, claim, or update Linear
  tasks. Use the current request for scope and `crazyniu/py-cut` for code, commits,
  branches, PRs, reviews, CI, and merges. Historical issue closure is not implementation proof.
- Do not create another task-management system proactively.
- State goal, scope, inputs/outputs, error behavior, constraints, unknowns, and
  verifiable acceptance criteria. Resolve routine choices directly. Clarify material
  product, security, or cost ambiguity; minimally test assumptions that could invalidate a plan.

### 0.2 Collaboration and delivery

- Work directly by default. Only when the user explicitly requests parallel work,
  use independent Codex conversations with the same model, separate branches and
  worktrees, and explicit file/interface ownership. Never edit one worktree concurrently.
- Use `codex/<descriptive-name>` branches without requiring task IDs. Use an isolated
  branch/worktree. Respect existing PR ownership, dependencies, and unrelated work.
- Implement complete goals in independently verifiable batches. Review and test the
  resulting change, fix its issues, and repeat only affected checks when needed.
- Record PASS / PARTIAL / NOT RUN / BLOCKED from evidence. Missing equipment is NOT RUN;
  keep progressing on executable work. Distinguish existing failures from regressions.
- Complete required tests, review, CI, merge, and acceptance before claiming completion.
  A merge does not prove acceptance. Do not mark unverified items complete.
- Use the normal PR flow within authorization. Do not bypass protection/review or
  force-push over others. Auto-merge is allowed after required review/tests/acceptance
  pass without another confirmation. Re-read final remote PR, checks, and merge SHA.
- Before asking approval for a proposal, revise and check it twice. Deliver the result,
  actual validation, and remaining limits concisely. Use direct, positive statements.

### 0.3 Verification scope

Ordinary PRs run required lightweight checks and directly affected regression tests.
Do not repeatedly run full/E2E/performance/expensive scans for routine pushes.
Run full synthetic-media regression via `workflow_dispatch` for releases/deployment,
architecture or significant public-interface/security/permission/dependency changes,
or explicit full-test requests. Verify required checks/protection before changing CI;
if unavailable, report that and preserve known check names.

Visual changes require the real encoder, complete decoding, extracted-frame inspection,
and audio listening plus stream/duration checks when audio is affected. Filter strings,
rc=0, pHash, or PSNR alone do not prove visual quality, watermark removal, or platform
acceptance. Mocks test protocols/errors, not real LLM accuracy. Short media do not prove
large-input performance. Put commands, input summaries, versions/SHA, and evidence in
PRs; redact credentials.

### 0.4 Documentation and comment language

Use **English** for maintained project documentation, code comments, and docstrings.
Write for AI collaborators: concise module responsibility, input/output contract,
side effects, invariants, error/cleanup ownership, and reasons for non-obvious choices.
Remove stale chronology, repeated code narration, obsolete examples, and unsupported
claims. Preserve exact identifiers, CLI flags, file formats, and compatibility rules.
Keep user-visible Chinese UI/log/help text and literal filesystem/data names unchanged
unless the user explicitly requests localization.

Markdown stores only durable requirements, workflows, architecture, interfaces/data
contracts, technical designs, and development/test/deployment rules. README is the
entry point; ROADMAP/MONTAGE/DEDUP describe product contracts, ENGINEERING captures
technical boundaries, and this file governs AI collaboration. Do not store backlogs,
bugs, current progress, temporary plans/handoffs/reports, commit/PR timelines, or CI
run logs in Markdown. Put temporary evidence and remaining limits in the chat/PR.
Update durable docs incrementally when their contracts change.

## 1. Runtime constraints

- Run processing through `./run.sh <command> ...`, never directly through
  `python3 pycut.py`. The launcher selects Python, installs missing requirements,
  validates cv2/media tools, prints the effective gateway without secrets, and starts.
- On macOS prefer `/Users/apple/.workbuddy/binaries/python/versions/3.13.12/bin/python3`
  when present. Other hosts use the launcher-selected interpreter. Require OpenCV 4.x
  Haar support. For EXTERNALLY-MANAGED system Python, create project `.venv`; never
  use `--break-system-packages`. Install missing FFmpeg through the platform package
  manager, recheck, and stop on failure.
- Windows uses `run.cmd`, with winget for missing Python/FFmpeg; re-detect after install.
  Start CLI/TUI only after validation. Static checks do not replace Windows execution.
- Video dependencies are numpy and opencv-python. The terminal picker explicitly
  uses MIT `textual-fspicker==1.0.1` and transitive Textual; Windows uses windows-curses.
  Keep LLM, .env, and CLI code standard-library based; do not add casual dependencies.
- Prefer Homebrew ffmpeg-full and VideoToolbox on macOS; use PATH FFmpeg/FFprobe and
  libx264 elsewhere. Centralize `PYCUT_FFMPEG`/`PYCUT_FFPROBE` overrides in config.py.
- FFmpeg 9 removed `-vsync`: use `-fps_mode cfr`. Do not add `-threads N` to scene
  detection; explicit thread limits regressed that path.

## 2. Module layout

Keep reusable processing in `pycut/core/` and command orchestration in
`pycut/commands/`. Command modules expose `register(subparsers)` and `run(args)`;
register new public commands in `cli.COMMANDS`. Do not combine multiple features
into one file. QA tools belong in `tools/`, tests in `tests/`, temporary scripts,
media, and reports in ignored `work/`, and durable designs in `docs/`.

## 3. Configuration and task contracts

### 3.1 Constants and gateways

Keep calibrated values in config.py (including SCENE_TH=0.30 and MIN_SEG=2.0).
Do not change calibrated numbers without reproducible recalibration.

Use standard-library urllib and reuse `LLMClient.ask_vision(frame, prompt)`.
`config.GATEWAYS` has exactly `main` and `backup`; DEFAULT_GATEWAY is always `main`,
with no PYCUT_GATEWAY selector. Main defaults to DeepSeek but may be any compatible
endpoint configured by URL/model without code changes.

Credentials come only from environment/.env, never committed defaults. Main uses
PYCUT_MAIN_URL/KEY/MODEL (legacy DEEPSEEK_API_KEY remains supported); backup uses
PYCUT_BACKUP_URL/KEY/MODEL. Blank backup URL/model inherit main. Real environment
variables override .env. Keep .env ignored and .env.example safe.

After LLM_FAILOVER_FAILS=3 consecutive failures, switch to a configured backup and
retain `_delegate` for subsequent requests. `--no-fallback` disables this; explicit
`--llm-url/key/model` suppresses automatic backup attachment. Do not switch without
a ready backup key. key_ready/gateway_summary must recognize placeholders and never
print key contents.

### 3.2 Settings and TUI

settings.py owns .env parsing and atomic saves. web_config.py is standard-library HTTP,
loopback-only at 127.0.0.1, with Host/Origin/session-token checks and no key echo.
TUI starts the service without opening a browser. Normal pages share the original
five-line block logo, actual settings URL, copy-to-browser/configuration instructions,
and one divider whose occupied height is returned to content layout. Wide windows
place settings beside the logo; narrow windows stack them. Use compact branding only
when height is insufficient. W displays manual URL access instructions.

Reload .env and validate task-used directories before each wizard. Missing settings
lead to Web configuration, not terminal directory selection. Keep only cover/dedup/
montage. Recursively read material without a three-level limit. Montage selection is:
multiple reference videos -> one mandatory count (1..99) for every selected reference
-> final summary. Returning preserves selections and entered counts.

Up/down move; Right/Enter enters; only Enter executes on confirmation. Esc returns;
only Q exits the home menu. tui_runner launches CLI children and shows real-time logs;
nonzero exits stop dependent jobs. Preserve explicit CLI compatibility. Montage shows
one batch progress bar with planned/completed final-video counts and animated activity;
internal steps do not increase the total. Show errors automatically. All tasks stop at
an explicit complete/review/failed/interrupted page until Enter/Esc returns. Keep stage
counts and directories separate rather than adding them into a false video total.

### 3.3 Materials, variants, and output

- Explicit CLI output wins. Each PYCUT_REFERENCE_DIR subfolder contains a reference
  video and same-stem SRT; list relative names to distinguish references.
- PYCUT_CLIP_DIR, PYCUT_PIP_DIR, and PYCUT_INTRO_DIR accept a legacy single path or a
  JSON path-array string. Use settings.directories consistently. Web supports row
  addition/removal, native folder selection, cancellation without changes, and manual
  input on headless hosts. CLI material arguments are repeatable. Retire source-video
  settings from the public workflow.
- Merge recursive roots without duplicate files or following directory symlinks.
  Optional empty PIP/intro pools may be skipped, but real read/probe errors must surface.
- Each montage invocation creates a batch under explicit --out-dir or configured
  output root. TUI references/variants share that one batch. Flatten all videos there,
  number collisions without overwriting, keep only videos (no JSON), and print the
  complete batch path on completion.
- Cover/dedup write siblings named `字幕查重`/`去重` for each input root, preserving
  existing groups/numbers. Preflight whole-batch collisions; never overwrite sources.
- Within one variant batch, share reference analysis, background/PIP scans, and online
  sticker download. Each final video independently plans randomness and encoding.
  Do not persist random selections, processing results, or preparation/success state.
- Read-only metadata may be cached in temporary storage only with real path, file
  size/mtime/ctime/inode, probe-tool identity, and query validation. Failures, invalid
  caches, or changed files fall back to real probes. Clean downloaded stickers at end.
- Intro is optional; absent/empty skips it. For a nonempty pool choose one complete
  video, reuse montage variation/stickers, and preserve its subtitles without cover.
  Finish body subtitles/reference audio before joining the intro. Duration is intro
  plus body. Body audio is copied without filtering/speed change/extension; joining
  may transcode audio for uniform format. Failure preserves prior final output.
  Atomic-replacement intermediates must be on the same filesystem as the target.

## 4. Subtitle coverage

Cover recursively reads supplied clips, locates subtitles across sampled frames with
a vision model, applies one full-width rectangular frosted-glass layer, and samples
again for verification. Persist completion only after verification. No subtitles is
normal; uncertainty/timeouts/residue require review, never false success. Encoding
failure and interruption return nonzero. Share 30 seconds per detection round, cap
individual HTTP at 15 seconds, and update progress while waiting. Legacy slicing
algorithms remain internal reuse/test surfaces, not public commands.

## 5. Known implementation traps

- Use os.scandir/os.walk traversal (list_videos/collect_clips); macOS find -iname has
  silently omitted MP4 files.
- Use coarse input seek backed off by COARSE_BACKOFF=1.0 plus accurate output seek
  for cuts and precise frame extraction. Long GOPs can otherwise select another shot.
- Reasoning-model max_tokens must be >=500; retry length exhaustion with 1200 so
  reasoning does not consume the entire content budget.
- Pair asynchronous results using as_completed plus explicit indices. Never infer
  identity from completion/unpacking order.
- Subtitle coverage is one Gaussian blur and one overlay, full video width with
  detected height plus margins. No pixelation, repeated layers, or per-glyph inpainting.
  Pixels outside the rectangle remain untouched.
- `<clip>.covered.json` belongs to the original source. Keep input/output separate;
  never treat an already covered output as an original on rerun.
- probe_scalar without stream selection can return video rate then audio `0/0` on
  another line. Parse the first line. A selected v:0 probe cannot reproduce this case.
- Test the real implementation, not a hand-built equivalent that hides its behavior
  (for example, piping probe output to head removes the extra audio line).
- Measurements in comments/reports must be personally reproducible with commands,
  inputs, and versions. Do not promote historical numbers or one truncated sample into
  current evidence. Keep unreproducible historical observations as research context.
- Do not add speculative defenses. Observed FFprobe duration forms are numeric, N/A,
  or empty failure; do not add nan/inf branches based only on imagined output.
- Never commit plaintext credentials. Gateway changes belong in .env/environment;
  code key defaults stay empty.

## 6. Minimal development loop

1. Audit and isolate work; read relevant reusable core implementations.
2. Make focused edits; start with dry-run/single-video checks before necessary batches.
   Processing always goes through the launcher.
3. Run affected tests using the launcher-selected Python. Pytest collects unittest;
   do not immediately repeat the same suite with another runner. Independently check
   frame counts, duration, audio streams, and actual visual/audio results when affected.
4. Run git diff --check, syntax/reference checks, and required CI. Update durable docs
   only for contract changes; keep temporary evidence in PR/chat.
5. Submit an independent PR explaining final behavior, evidence, and unverified limits.
   Complete review, CI, merge, acceptance, and remote verification.

Do not commit personal desktop outputs, manifests, temporary media, or secrets.
Do not add openai/requests/ffmpeg-python or bypass launchers/dependency boundaries.
