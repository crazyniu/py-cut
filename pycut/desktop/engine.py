"""Adapt frozen desktop jobs to the existing command defaults and processing core.

The adapter owns no video algorithm. Publication journals live in app data rather
than output batches; their inode/size/mtime identity makes recovery idempotent.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from uuid import uuid4


def command_parser(kind):
    from ..cli import build_parser
    parser = build_parser()
    action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    if kind not in ("cover", "dedup", "montage"):
        raise ValueError("未知处理类型")
    return action.choices[kind]


_TRANSPORT = {"help", "func", "file", "all", "src_dir", "source", "clip_dir", "dedup_dir",
              "intro_dir", "sticker_dir", "batch_dir", "source_stems", "gateway", "llm_url",
              "llm_key", "llm_model", "no_fallback", "probe", "step", "fission"}


def option_specs(kind):
    specs = []
    for action in command_parser(kind)._actions:
        if action.dest in _TRANSPORT or not action.option_strings:
            continue
        default = action.default
        if default is argparse.SUPPRESS:
            continue
        kind_name = ("boolean" if isinstance(default, bool) else
                     "integer" if action.type is int else "number" if action.type is float else "string")
        spec = {"name": action.dest, "label": action.option_strings[-1], "type": kind_name,
                "default": default, "help": action.help or ""}
        if action.choices is not None:
            spec["choices"] = list(action.choices)
        specs.append(spec)
    return specs


def parse_options(kind, values):
    """Validate UI values with the command's real argparse types and choices."""
    parser = command_parser(kind)
    argv = []
    by_dest = {a.dest: a for a in parser._actions if a.dest not in _TRANSPORT}
    for name, value in values.items():
        if name not in by_dest:
            continue
        action = by_dest[name]
        if value is None or not action.option_strings:
            continue
        flag = action.option_strings[0]
        if isinstance(action.default, bool):
            if not isinstance(value, bool):
                raise ValueError(f"{name} 必须是布尔值")
            if value:
                argv.append(flag)
            elif isinstance(action, argparse.BooleanOptionalAction):
                argv.append(next(option for option in action.option_strings if option.startswith("--no-")))
        else:
            argv.extend([flag, str(value)])
    try:
        return parser.parse_args(argv)
    except SystemExit as exc:
        raise ValueError("处理参数无效，请检查高级设置") from exc


def signature(path):
    stat = Path(path).stat()
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]


def check_snapshot(request):
    for asset in request["snapshot"]:
        if signature(asset["path"]) != asset["signature"]:
            raise ValueError(f"素材在任务创建后发生变化，请刷新素材库后重新提交：{Path(asset['path']).name}")


def emit(event, payload):
    print("@pycut-desktop " + json.dumps({"event": event, "payload": payload}, ensure_ascii=False), flush=True)


def _atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def publication_identity(path):
    stat = Path(path).stat()
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]


def recover_publications(store, data_dir, job_id=None):
    """Count only a file whose identity matches a prepared publication journal."""
    recovered = []
    for path in (Path(data_dir) / "publications").glob("*.json"):
        record = json.loads(path.read_text(encoding="utf-8"))
        if job_id is not None and record["job_id"] != job_id:
            continue
        try:
            current = publication_identity(record["path"])
        except FileNotFoundError:
            continue
        if current != record["identity"]:
            continue
        store.record_output(record["id"], record["project_id"], record["job_id"],
                            record["path"], record["uses"])
        path.unlink()
        recovered.append(record)
    return recovered


def _montage(request, data_dir, store):
    from .. import config
    from ..commands.montage_pipeline import MontageOptions, run_pipeline
    from ..core.dedup_presets import derive_seed
    from ..core.ffmpeg import self_check
    from ..core.output_paths import montage_batch, montage_stem
    from ..core.progress import progress
    from ..core.runtime import check_stop

    options = dict(request["options"])
    for key, inverted in (("use_pip", "no_pip"), ("use_sub", "no_sub"),
                          ("use_sticker", "no_sticker"), ("keep_audio", "no_audio")):
        if key in options:
            options[inverted] = not options[key]
    args = parse_options("montage", options)
    self_check(require_src=False)
    root = Path(request["output_dir"])
    batch = montage_batch(root)
    batch.mkdir(parents=True, exist_ok=False)
    pools = request["pools"]
    material_files = {role: [Path(a["path"]) for a in items]
                      for role, items in pools.items()}
    shared = {}
    stickers = material_files.get("sticker", [])
    if request.get("sticker_library_id"):
        shared["stickers"] = stickers
    completed = failed = 0
    try:
        for reference in request["references"]:
            check_stop()
            prepared = shared
            prepared.pop("source", None)
            source = Path(reference["path"])
            count = request["fission"]
            stem = montage_stem(source, batch, count)
            base_seed = args.seed or derive_seed(source)
            subtitle = reference.get("subtitle_path", "") if not args.no_sub else ""
            for variant in range(1, count + 1):
                check_stop()
                output_id = uuid4().hex
                def before_publish(final, staged, uses):
                    check_snapshot(request)
                    versions = {item["path"]: item["signature"] for item in request["snapshot"]}
                    uses = [{**use, "signature": json.dumps(
                        versions[use["path"]] if use["path"] in versions else signature(use["path"]))}
                        for use in uses]
                    _atomic_json(Path(data_dir) / "publications" / (output_id + ".json"),
                                 {"id": output_id, "job_id": request["id"],
                                  "project_id": request["project_id"], "path": str(final.resolve()),
                                  "identity": publication_identity(staged), "uses": uses})
                opts = MontageOptions(
                    prepared=prepared, material_files=material_files, before_publish=before_publish,
                    source=source, out_dir=batch, out_name=f"{stem}_{variant}.mp4",
                    clip_dir=[Path(p) for p in request["roots"].get("background", [])],
                    dedup_dir=[Path(p) for p in request["roots"].get("pip", [])],
                    intro_dir=([Path(p) for p in request["roots"].get("intro", [])]
                               if options.get("intro_enabled", False) else None),
                    seed=base_seed if variant == 1 else base_seed + variant * 10007,
                    use_pip=not args.no_pip and bool(material_files.get("pip")),
                    pip_continuous=not args.pip_gap, keep_audio=not args.no_audio,
                    allow_landscape=args.allow_landscape, pip_slot=args.pip_slot,
                    mirror_chance=max(0.0, min(1.0, args.mirror_chance)),
                    speed_range=(1.0, max(1.0, args.speed_max)), seam_fade=args.seam_fade,
                    sticker_chance=0 if args.no_sticker else max(0.0, min(1.0, args.sticker_chance)),
                    online_stickers=not request.get("sticker_library_id") and not args.no_sticker,
                    srt=subtitle, crf=args.crf, fill_ratio=args.fill_ratio,
                    noise_db=args.noise_db, min_silence=args.min_silence,
                    min_clip=args.min_clip, max_clip=args.max_clip, pip_scale=args.pip_scale,
                    workers=args.workers)
                if opts.seam_fade < 0 or opts.fill_ratio != 1.0:
                    raise ValueError("淡化不能为负数，正文时长比例必须为 1")
                result = run_pipeline(opts)
                if result.get("final") and not result.get("error"):
                    recover_publications(store, data_dir, request["id"])
                    completed += 1
                    emit("output", {"path": result["final"], "id": output_id})
                elif result.get("cancelled"):
                    check_stop()
                else:
                    failed += 1
                    emit("failure", {"message": result.get("error", "成片未生成")})
                for key in ("background", "pip", "stickers", "temporary_dirs"):
                    if key in prepared:
                        shared[key] = prepared[key]
                progress("整批成片", completed, request["planned"], success=completed,
                         failed=failed, output=str(batch))
                emit("counts", {"completed": completed, "failed": failed, "review": 0})
        return {"completed": completed, "failed": failed, "review": 0,
                "status": "partial" if failed and completed else "failed" if failed else "completed"}
    finally:
        for directory in shared.get("temporary_dirs", []):
            directory.cleanup()


def run_job(request, data_dir, store):
    """Run in one disposable worker; never share global STOP/config across jobs."""
    from ..core.runtime import STOP, install_signal_handlers, reset_stop
    reset_stop()
    install_signal_handlers()
    os.environ["PYCUT_PROGRESS"] = "1"
    os.environ["PYCUT_DESKTOP_JOB"] = "1"
    def control():
        import sys
        for line in sys.stdin:
            if line.strip() == "cancel":
                STOP.flag = True
    threading.Thread(target=control, daemon=True, name="desktop-cancel").start()
    check_snapshot(request)
    if request["kind"] == "montage":
        return _montage(request, data_dir, store)
    kind = request["kind"]
    args = parse_options(kind, request["options"])
    args.runtime_initialized = True
    args.file = [a["path"] for a in request["inputs"]]
    args.src_dir = None
    args.input_roots = request["input_roots"]
    completed = failed = review = skipped = 0
    lock = threading.Lock()
    def record(source, target, status):
        nonlocal completed, failed, review, skipped
        with lock:
            if status == "fail":
                failed += 1
            elif status == "review":
                review += 1
            elif status == "skip":
                skipped += 1
            else:
                completed += 1
            if status not in ("fail", "skip", "dryrun") and Path(target).is_file() and not STOP.flag:
                store.record_output(uuid4().hex, request["project_id"], request["id"], str(target), [])
            emit("counts", {"completed": completed, "failed": failed, "review": review, "skipped": skipped})
    if kind == "cover":
        from .local_vision import make_local_vision
        from ..commands.cover_cmd import run
        settings = request["agent_settings"]
        client = make_local_vision(settings.get("vision_provider", "codex"),
                                   settings.get("agent_path", ""), settings.get("vision_model", ""))
        args.vision_client = client
        args.on_result = record
        try:
            code = run(args)
        finally:
            client.close()
    else:
        from ..commands.dedup_cmd import run
        def results(items):
            for item in items:
                status = ("skip" if item.get("skipped") else "covered" if item.get("ok")
                          else "review" if not item.get("error") and Path(item.get("dst", "")).is_file()
                          else "fail")
                record(item["src"], item.get("dst", ""), status)
        args.on_results = results
        code = run(args)
    status = ("cancelled" if STOP.flag or code == 130 else
              "partial" if failed and completed else "failed" if failed or (code and not review)
              else "review" if review else "completed")
    return {"status": status, "completed": completed, "failed": failed,
            "review": review, "skipped": skipped}
