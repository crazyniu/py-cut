"""Automatic runs refresh plans; explicit seeds reproduce every batch variant."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from pycut import cli, config
from pycut.commands import montage_cmd, montage_pipeline
from pycut.core import montage_clips, montage_source


@pytest.fixture
def planning_run(tmp_path, monkeypatch):
    source = tmp_path / "reference.mp4"
    source.touch()
    profile = SimpleNamespace(duration=4, width=720, height=1280,
                              segments=[], speech_total=0, has_audio=False)
    pool = [montage_clips.ClipItem(Path(f"clip-{i}.mp4"), 10, 720, 1280, False)
            for i in range(12)]
    plans = []

    def encode(items, target, **kwargs):
        # Exercise the real planner/variation; isolate only media I/O.
        plans.append(deepcopy(items))
        target.write_bytes(b"video" * 300)
        return {"ok": True}

    monkeypatch.setattr(config, "MONTAGE_TMP", tmp_path / "tmp")
    monkeypatch.setattr(montage_cmd, "self_check", lambda **kwargs: None)
    monkeypatch.setattr(montage_cmd, "install_signal_handlers", lambda: None)
    monkeypatch.setattr(montage_source, "probe_source", lambda *a, **k: profile)
    monkeypatch.setattr(montage_clips, "scan_library", lambda *a, **k: pool)
    monkeypatch.setattr(montage_clips, "concat_clips", encode)
    monkeypatch.setattr(montage_pipeline, "_duration", lambda path: 4)

    def run(*extra):
        args = cli.build_parser().parse_args([
            "montage", "--source", str(source), "--out-dir", str(tmp_path / "out"),
            "--step", "b", "--no-sub", "--no-sticker", "--no-backdrop", "--intro-dir", "",
            "--min-clip", "2", "--max-clip", "2", *extra])
        assert montage_cmd.run(args) == 0
        return deepcopy(plans)

    return run, plans, source


@pytest.mark.parametrize("automatic", [[], ["--seed", "0"]])
def test_repeated_auto_batches_refresh_all_variants(planning_run, monkeypatch, automatic):
    run, plans, _ = planning_run
    draws = iter([100, 200])
    monkeypatch.setattr(montage_pipeline.secrets, "randbelow", lambda n: next(draws))
    first = run("--fission", "3", *automatic)
    plans.clear()
    second = run("--fission", "3", *automatic)
    assert len(first) == len(second) == 3
    for before, after in zip(first, second):
        assert [(p["item"].path, p["offset"]) for p in before] != [
            (p["item"].path, p["offset"]) for p in after]
        assert [p["dedup_params"] for p in before] != [p["dedup_params"] for p in after]
    assert len({p[0]["dedup_seed"] for p in first + second}) == 6


@pytest.mark.parametrize("seed", [42, -42])
def test_explicit_seed_reproduces_all_variants(planning_run, monkeypatch, seed):
    run, plans, _ = planning_run

    def unexpected_entropy(n):
        pytest.fail("Explicit seeds must not draw entropy")

    monkeypatch.setattr(montage_pipeline.secrets, "randbelow", unexpected_entropy)
    first = run("--fission", "3", "--seed", str(seed))
    plans.clear()
    assert run("--fission", "3", "--seed", str(seed)) == first
    assert len({p[0]["dedup_seed"] for p in first}) == 3


def test_direct_pipeline_refreshes_seed_without_mutating_options(planning_run, monkeypatch):
    _, _, source = planning_run
    draws = iter([300, 400])
    monkeypatch.setattr(montage_pipeline.secrets, "randbelow", lambda n: next(draws))
    opts = montage_pipeline.MontageOptions(source=source, step="a", srt="",
                                            out_dir=source.parent / "direct")
    first = montage_pipeline.run_pipeline(opts)
    second = montage_pipeline.run_pipeline(opts)
    assert (first["seed"], second["seed"]) == (301, 401)
    assert opts.seed is None
