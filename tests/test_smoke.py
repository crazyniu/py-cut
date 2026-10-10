"""pycut 冒烟测试：验证包结构、配置、CLI 注册表、去重参数可复现性。

运行：python3 -m pytest tests/ 或 python3 tests/test_smoke.py
（仅做导入与结构断言，不触发实际 ffmpeg 编码 / LLM 调用 / 大批量视频处理。）
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pycut import cli, config  # noqa: E402
from pycut.commands import cover_cmd, dedup_cmd, slice_cmd  # noqa: E402


def _require_media_environment() -> None:
    """本机 FFmpeg 和真实素材验证仅在明确授权后运行，不污染纯逻辑测试。"""
    if os.environ.get("PYCUT_TEST_MEDIA") != "1":
        raise unittest.SkipTest(
            "需要真实媒体环境；设置 PYCUT_TEST_MEDIA=1 后才会检查本机 FFmpeg/素材"
        )


def test_config_paths_exist():
    _require_media_environment()
    assert os.path.isfile(config.FFMPEG), f"ffmpeg 不存在: {config.FFMPEG}"
    assert os.path.isfile(config.FFPROBE), f"ffprobe 不存在: {config.FFPROBE}"
    assert config.SRC_DIR.is_dir(), f"源目录不存在: {config.SRC_DIR}"


def test_gateways_present():
    # 只有主网关 / 备用网关两个，不按厂商命名
    assert set(config.GATEWAYS) == {"main", "backup"}
    assert config.DEFAULT_GATEWAY == "main"
    assert config.apply_llm_overrides("main")[0] == config.GATEWAYS["main"]["url"]
    assert config.other_gateway("main") == "backup"
    assert config.other_gateway("backup") == "main"


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
    ns = parser.parse_args(["slice", "--dry-run", "--gateway", "backup"])
    assert ns.command == "slice"
    assert ns.dry_run is True
    assert ns.gateway == "backup"
    assert ns.no_fallback is False
    ns2 = parser.parse_args(["slice", "--no-fallback"])
    assert ns2.no_fallback is True
    assert ns2.gateway == "main"


def test_dedup_registered():
    """去重命令已挂到注册表，且档位/门槛配置齐备。"""
    assert "dedup" in cli.list_commands()
    assert set(config.DEDUP_PRESETS) == {"light", "standard", "aggressive"}
    assert config.DEFAULT_GATEWAY == "main"
    ns = cli.build_parser().parse_args(["dedup", "--all", "--preset", "aggressive"])
    assert ns.command == "dedup" and ns.all is True and ns.preset == "aggressive"
    # dedup 支持 --src-dir 递归扫描
    ns2 = cli.build_parser().parse_args(["dedup", "--src-dir", "/tmp"])
    assert ns2.src_dir == "/tmp"


def test_tui_wizard_structure():
    """向导式 TUI 结构：5 功能齐全、目录扫描工具可用、混剪支持裂变。"""
    import pycut.tui as tui  # noqa: PLC0415
    from pycut.core import scan  # noqa: PLC0415

    # 主菜单应有 5 个功能（切片/字幕擦除/去重/混剪/查素材）
    names = [f[1] for f in tui.FEATURES]
    assert names == ["切片", "字幕擦除", "去重", "混剪", "查素材"]
    # 目录扫描工具
    assert hasattr(scan, "count_videos_recursive")
    assert hasattr(scan, "list_videos_flat")
    assert hasattr(scan, "list_dirs")
    assert hasattr(scan, "search_videos")
    # 递归深度限制默认 3 层
    assert scan.DEFAULT_MAX_DEPTH == 3
    # 混剪支持 --fission 裂变（直接解析验证）
    ns = cli.build_parser().parse_args(["montage", "--fission", "3"])
    assert ns.fission == 3


def test_dedup_presets_deterministic():
    """同 seed 同档位必须产出完全一致的参数（可复现是去重的基本要求）。"""
    from pycut.core import dedup_presets as dp  # noqa: PLC0415

    a = dp.build_params("standard", 42, has_audio=True, width=720, height=1280)
    b = dp.build_params("standard", 42, has_audio=True, width=720, height=1280)
    assert a == b
    c = dp.build_params("standard", 43, has_audio=True, width=720, height=1280)
    assert (a["crf"], a["crop_px"], a["noise"]) != (c["crf"], c["crop_px"], c["noise"])
    # 无音轨 -> 不允许变调
    d = dp.build_params("standard", 42, has_audio=False)
    assert d["pitch"] == 1.0
    # 轻档强度必须小于激进档
    light = dp.build_params("light", 7)
    aggr = dp.build_params("aggressive", 7)
    assert light["crop_px"] <= aggr["crop_px"]
    assert light["noise"] <= aggr["noise"]


def test_fingerprint_identical_is_zero():
    """真实视频自比依赖 FFmpeg 和测试素材，不应在跨环境 CI 静默通过。"""
    _require_media_environment()
    from pycut.core import fingerprint as fp  # noqa: PLC0415

    src = config.SRC_DIR
    videos = sorted(p for p in src.glob("*.mp4"))[:1]
    if not videos:
        raise unittest.SkipTest(f"源目录没有可用于 pHash 验收的 MP4：{src}")
    prof = fp.phash_profile(videos[0], n=6)
    assert len(prof) >= 1
    assert fp.phash_distance(prof[0], prof[0]) == 0
    r = fp.compare(videos[0], videos[0], n=6)
    assert r["phash_dist"] == 0.0
    assert r["psnr"] >= 60


if __name__ == "__main__":
    try:
        import pytest  # noqa: PLC0415
        sys.exit(pytest.main([__file__, "-q"]))
    except ModuleNotFoundError:
        # 本机没装 pytest：逐个直跑，至少把断言跑完
        ok = fail = skipped = 0
        for _name in [n for n in dir() if n.startswith("test_")]:
            try:
                globals()[_name]()
                print(f"  ✓ {_name}")
                ok += 1
            except unittest.SkipTest as e:
                print(f"  ↷ {_name} (skip): {e}")
                skipped += 1
            except Exception as e:  # noqa: BLE001
                print(f"  ✗ {_name}: {type(e).__name__}: {e}")
                fail += 1
        print(f"\n通过 {ok} / 跳过 {skipped} / 失败 {fail}")
        sys.exit(1 if fail else 0)
