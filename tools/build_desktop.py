"""Prepare a self-contained desktop runtime, then optionally build the native app.

Run on the target OS/architecture with requirements-desktop.txt installed. FFmpeg
must include libass. macOS Homebrew dynamic dependencies are copied and relinked;
Windows requires a static FFmpeg distribution. No user configuration is collected.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
RESOURCES = ROOT / "desktop/src-tauri/resources"
FONT_BASE = "https://raw.githubusercontent.com/google/fonts/main/ofl/notosanssc/"


def run(command, **kwargs):
    return subprocess.run([str(p) for p in command], check=True, **kwargs)


def media_tool(name, directory):
    suffix = ".exe" if os.name == "nt" else ""
    candidate = Path(directory) / (name + suffix) if directory else None
    found = str(candidate) if candidate and candidate.is_file() else shutil.which(name)
    if not found and sys.platform == "darwin":
        candidate = Path("/opt/homebrew/opt/ffmpeg-full/bin") / name
        if candidate.is_file():
            found = str(candidate)
    if not found:
        raise SystemExit(f"Missing {name}; supply --media-dir with target-native binaries")
    return Path(found).resolve()


def bundle_macos_libraries(binary, library_dir, copied):
    """Relocate non-system Mach-O dependencies recursively before ad-hoc signing."""
    listing = run(["otool", "-L", binary], capture_output=True, text=True).stdout
    for line in listing.splitlines()[1:]:
        dependency = line.strip().split(" (", 1)[0]
        if not dependency.startswith("/") or dependency.startswith(("/System/", "/usr/lib/")):
            continue
        source = Path(dependency).resolve()
        target = library_dir / source.name
        if source not in copied:
            copied.add(source)
            shutil.copy2(source, target)
            target.chmod(0o755)
            run(["install_name_tool", "-id", "@rpath/" + target.name, target])
            bundle_macos_libraries(target, library_dir, copied)
        relative = ("@loader_path/" if binary.parent == library_dir else "@executable_path/lib/") + target.name
        run(["install_name_tool", "-change", dependency, relative, binary])
    run(["codesign", "--force", "--sign", "-", binary], capture_output=True)


def prepare(args):
    if sys.platform not in {"darwin", "win32"}:
        raise SystemExit("Desktop distributions support macOS and Windows only")
    ffmpeg = media_tool("ffmpeg", args.media_dir)
    ffprobe = media_tool("ffprobe", args.media_dir)
    build = run([ffmpeg, "-hide_banner", "-filters"], capture_output=True, text=True)
    if not re.search(r"\bass\s+", build.stdout):
        raise SystemExit("FFmpeg is missing the required libass ASS subtitle filter")
    RESOURCES.mkdir(parents=True, exist_ok=True)
    scratch = ROOT / "work/desktop-build"
    scratch.mkdir(parents=True, exist_ok=True)
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
         "--name", "pycut-service", "--paths", ROOT,
         "--collect-all", "cv2", "--collect-all", "numpy", "--collect-submodules", "pycut",
         "--exclude-module", "textual", "--exclude-module", "textual_fspicker",
         "--exclude-module", "curses", "--exclude-module", "tkinter",
         "--distpath", scratch / "dist", "--workpath", scratch / "build", "--specpath", scratch,
         ROOT / "tools/desktop_worker.py"])
    runtime = RESOURCES / "runtime"
    if runtime.exists():
        shutil.rmtree(runtime)
    shutil.copytree(scratch / "dist/pycut-service", runtime)
    media = RESOURCES / "media"
    if media.exists():
        shutil.rmtree(media)
    media.mkdir()
    for source in (ffmpeg, ffprobe):
        destination = media / source.name
        shutil.copy2(source, destination)
        destination.chmod(0o755)
    if sys.platform == "darwin":
        library_dir = media / "lib"
        library_dir.mkdir()
        copied = set()
        for name in ("ffmpeg", "ffprobe"):
            bundle_macos_libraries(media / name, library_dir, copied)
    fonts = RESOURCES / "fonts"
    fonts.mkdir(exist_ok=True)
    for filename in ("NotoSansSC[wght].ttf", "OFL.txt"):
        destination = fonts / filename
        if args.font_dir:
            shutil.copy2(Path(args.font_dir) / filename, destination)
        elif not destination.exists():
            url = FONT_BASE + urllib.parse.quote(filename)
            with urllib.request.urlopen(url, timeout=120) as response:
                destination.write_bytes(response.read())
    version = run([ffmpeg, "-version"], capture_output=True, text=True).stdout
    (RESOURCES / "runtime-manifest.json").write_text(json.dumps({
        "platform": sys.platform, "architecture": platform.machine(),
        "python": platform.python_version(), "ffmpeg": version,
        "sources": {"ffmpeg": "https://ffmpeg.org", "font": FONT_BASE},
        "license_note": "FFmpeg build configuration determines LGPL/GPL obligations. Preserve dependency licenses and provide corresponding source for released binaries."
    }, indent=2), encoding="utf-8")
    env = dict(os.environ, PYCUT_FFMPEG=str(media / ffmpeg.name), PYCUT_FFPROBE=str(media / ffprobe.name))
    run([media / ffmpeg.name, "-version"], env=env, stdout=subprocess.DEVNULL)
    run([media / ffprobe.name, "-version"], env=env, stdout=subprocess.DEVNULL)
    print(f"Prepared {platform.machine()} runtime: {RESOURCES}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--prepare", action="store_true", help="Freeze Python and collect media/font runtime")
    action.add_argument("--build", action="store_true", help="Prepare runtime and build Tauri installers")
    parser.add_argument("--media-dir", help="Directory with target-platform ffmpeg and ffprobe")
    parser.add_argument("--font-dir", help="Directory containing NotoSansSC[wght].ttf and OFL.txt")
    args = parser.parse_args()
    prepare(args)
    if args.build:
        npm = "npm.cmd" if os.name == "nt" else "npm"
        run([npm, "ci"], cwd=ROOT / "desktop")
        run([npm, "exec", "tauri", "build"], cwd=ROOT / "desktop")


if __name__ == "__main__":
    main()
