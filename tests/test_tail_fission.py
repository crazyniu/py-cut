"""Tail-only copy integrity, output ownership, task settings, and interruption."""
import os
from unittest import mock

import pytest

from pycut import cli, config, settings
from pycut.core import fission
from pycut.core.progress import parse


def test_copies_keep_exact_source_prefix_and_do_not_replace_existing_files(tmp_path):
    source = tmp_path / "原片.MOV"
    original = os.urandom(1024 * 1024 + 3)
    source.write_bytes(original)
    output = tmp_path / "输出"
    output.mkdir()
    existing = output / "原片_1.MOV"
    existing.write_bytes(b"keep")
    copies = [fission.copy_with_random_tail(source, output) for _ in range(2)]
    assert [p.name for p in copies] == ["原片_2.MOV", "原片_3.MOV"]
    tails = []
    for path in copies:
        content = path.read_bytes()
        assert content[:len(original)] == original
        tail = content[len(original):]
        assert 50 * 1024 <= len(tail) <= 500 * 1024
        tails.append(tail)
    assert tails[0] != tails[1]
    assert source.read_bytes() == original
    assert existing.read_bytes() == b"keep"
    assert not list(output.glob(".fission-*"))


@pytest.mark.parametrize("error", [InterruptedError("stop"), OSError("write failed")])
def test_failure_before_publication_removes_private_copy(tmp_path, error):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"original")
    output = tmp_path / "output"
    with mock.patch.object(fission, "check_stop", side_effect=[None, None, error]):
        with pytest.raises(type(error)):
            fission.copy_with_random_tail(source, output)
    assert list(output.iterdir()) == []
    assert source.read_bytes() == b"original"


def test_recursive_cli_outputs_keep_folders_and_report_actual_counts(tmp_path, capsys, monkeypatch):
    root = tmp_path / "原视频"
    for folder in ("a", "b/deep/more/nested"):
        parent = root / folder
        parent.mkdir(parents=True)
        (parent / "同名.mp4").write_bytes(folder.encode())
    monkeypatch.setenv("PYCUT_PROGRESS", "1")
    assert cli.main(["fission", "--src-dir", str(root), "--copies", "2"]) == 0
    output = tmp_path / "抖音裂变"
    assert len(list(output.rglob("*.mp4"))) == 4
    events = [event for line in capsys.readouterr().out.splitlines() if (event := parse(line))]
    assert events[-1]["success"] == events[-1]["completed"] == events[-1]["total"] == 4
    assert events[-1]["output"] == str(output)
    for source in root.rglob("*.mp4"):
        for number in (1, 2):
            path = output / source.relative_to(root).with_name(f"同名_{number}.mp4")
            assert path.read_bytes().startswith(source.read_bytes())
    assert cli.main(["fission", "--src-dir", str(root), "--out-dir", str(root)]) == 2


def test_source_setting_roundtrip_and_task_specific_validation(tmp_path):
    with mock.patch.object(config, "PROJECT_ROOT", tmp_path):
        settings.save({"PYCUT_SOURCE_DIR": str(tmp_path)})
        assert settings.read_env()["PYCUT_SOURCE_DIR"] == str(tmp_path)
        values = {"PYCUT_SOURCE_DIR": str(tmp_path), "PYCUT_REFERENCE_DIR": "/missing"}
        assert settings.problems(values, keys=("PYCUT_SOURCE_DIR",)) == []
        with mock.patch.object(settings, "effective", return_value={}), \
             mock.patch.object(config, "SRC_DIR", tmp_path):
            assert cli.main(["fission"]) == 2
        with mock.patch.object(settings, "effective", return_value=values), \
             mock.patch.object(config, "SRC_DIR", tmp_path):
            (tmp_path / "original.mp4").write_bytes(b"video")
            assert cli.main(["fission", "--out-dir", str(tmp_path / "output")]) == 0


def test_invalid_counts_and_interruption_return_nonzero(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    for count in (0, 100):
        assert cli.main(["fission", "--file", str(source), "--copies", str(count)]) == 2
    with mock.patch("pycut.commands.fission_cmd.copy_with_random_tail", side_effect=InterruptedError("stop")):
        assert cli.main(["fission", "--file", str(source)]) == 130
