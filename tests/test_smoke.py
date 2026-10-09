"""pycut 冒烟测试：验证包结构、配置、CLI 注册表。

运行：python3 -m pytest tests/ 或 python3 tests/test_smoke.py
（仅做导入与结构断言，不触发实际 ffmpeg 编码 / LLM 调用。）
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pycut import cli, config  # noqa: E402
from pycut.commands import cover_cmd, slice_cmd  # noqa: E402


def test_config_paths_exist():
    assert os.path.isfile(config.FFMPEG), f"ffmpeg 不存在: {config.FFMPEG}"
    assert os.path.isfile(config.FFPROBE), f"ffprobe 不存在: {config.FFPROBE}"
    assert config.SRC_DIR.is_dir(), f"源目录不存在: {config.SRC_DIR}"


def test_gateways_present():
    assert "local" in config.GATEWAYS
    assert "deepseek" in config.GATEWAYS
    assert config.apply_llm_overrides("local")[0] == config.GATEWAYS["local"]["url"]


def test_cli_registers_commands():
    assert set(cli.list_commands()) >= {"slice", "cover"}
    parser = cli.build_parser()
    # 每个命令模块都应暴露 register / run
    for mod in (slice_cmd, cover_cmd):
        assert hasattr(mod, "register")
        assert hasattr(mod, "run")


def test_slice_subparser_has_key_args():
    parser = cli.build_parser()
    # 通过解析验证 slice 子命令参数齐全
    ns = parser.parse_args(["slice", "--dry-run", "--gateway", "deepseek"])
    assert ns.command == "slice"
    assert ns.dry_run is True
    assert ns.gateway == "deepseek"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
