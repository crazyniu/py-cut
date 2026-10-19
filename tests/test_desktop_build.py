"""Packaging must report the strongest minimum OS among all frozen dependencies."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tools.build_desktop import macos_compatibility


def test_macos_audit_includes_runtime_extensions_and_media_dependencies(tmp_path):
    versions = {"runtime/python": "13.0", "runtime/cv2.so": "14.0",
                "media/ffmpeg": "27.0", "media/lib/codec.dylib": "15.0"}
    for name in versions:
        binary = tmp_path / name
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(bytes.fromhex("cffaedfe"))
    (tmp_path / "font.ttf").write_bytes(b"font")
    def inspect(command, **kwargs):
        name = str(Path(command[-1]).relative_to(tmp_path))
        if name.endswith(".so"):
            return SimpleNamespace(stdout=f"cmd LC_VERSION_MIN_MACOSX\n cmdsize 16\n version {versions[name]}\n")
        return SimpleNamespace(stdout=f"cmd LC_BUILD_VERSION\n cmdsize 32\n minos {versions[name]}\n")
    with patch("tools.build_desktop.run", side_effect=inspect):
        audit = macos_compatibility(tmp_path, "13.0")
        assert audit == {"target": "13.0", "required": "27.0", "compatible": False, "binaries": versions}
        assert macos_compatibility(tmp_path, "27.0")["compatible"]
