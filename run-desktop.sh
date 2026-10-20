#!/bin/bash
# Build and open the macOS source workbench with persistent checkout-local data.
set -euo pipefail
cd "$(dirname "$0")"
PYCUT_ROOT="$PWD"
export PATH="$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

on_exit() {
  status=$?
  if [ "$status" -ne 0 ] && [ -t 0 ]; then
    echo "[pycut] Startup failed. See the error above. Press Enter to close."
    read -r _
  fi
}
trap on_exit EXIT

if [ "$(uname -s)" != Darwin ]; then
  echo '[pycut] This desktop launcher requires macOS.'
  exit 1
fi
for tool in node npm cargo; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "[pycut] Missing $tool. Install Node.js and Rust, then try again."
    exit 1
  fi
done

export PYCUT_DATA_DIR="${PYCUT_DATA_DIR:-$PYCUT_ROOT/work/desktop/app}"
export PYCUT_CACHE_DIR="${PYCUT_CACHE_DIR:-$PYCUT_ROOT/work/desktop/cache}"
mkdir -p "$PYCUT_DATA_DIR" "$PYCUT_CACHE_DIR"
cd "$PYCUT_ROOT/desktop"
if [ ! -f node_modules/.package-lock.json ] || [ package-lock.json -nt node_modules/.package-lock.json ]; then
  npm ci
fi
echo '[pycut] Building the desktop app...'
npm exec tauri build -- --debug --bundles app
/usr/bin/open --env "PYCUT_DATA_DIR=$PYCUT_DATA_DIR" \
  --env "PYCUT_CACHE_DIR=$PYCUT_CACHE_DIR" \
  "$PYCUT_ROOT/desktop/src-tauri/target/debug/bundle/macos/pycut.app"
echo '[pycut] Desktop opened. Projects, directories and task history persist automatically.'
