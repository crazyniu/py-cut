#!/usr/bin/env bash
# pycut 一键运行脚本：自动装依赖 -> 读 .env（密钥/网关/模型）-> 运行入口
#
# 用法:
#   ./run.sh                              # 无参数：交互终端进入 TUI
#   ./run.sh tui                          # 显式进入 TUI
#   ./run.sh slice --file "文件名" --dry-run
#   ./run.sh slice                        # 批量切片
#   ./run.sh cover --all                  # 补跑字幕擦除
#
# 说明：密钥与网关配置全部在项目根 .env（首次运行会自动从 .env.example 生成）。
set -uo pipefail

cd "$(dirname "$0")" || exit 1
PLATFORM="$(uname -s)"
case "$PLATFORM" in
  Linux|Darwin) ;;
  *) echo '[pycut] 当前启动安装脚本仅支持 Linux 和 macOS'; exit 1 ;;
esac
echo "[pycut] 系统: $PLATFORM"

# 只在缺少工具时调用系统包管理器；不下载并执行远程安装脚本。
install_system_package() {
  local package="$1"
  local admin=()
  if [ "$PLATFORM" = Darwin ]; then
    command -v brew >/dev/null 2>&1 || {
      echo '[pycut] 错误: 缺少 Homebrew，请先安装 Homebrew 后重新启动'
      return 1
    }
    [ "$package" != python3 ] || package=python@3.13
    brew install "$package"
    return $?
  fi
  if [ "$(id -u)" -ne 0 ]; then
    command -v sudo >/dev/null 2>&1 || {
      echo '[pycut] 错误: 安装系统依赖需要管理员权限，当前没有 sudo'
      return 1
    }
    admin=(sudo)
  fi
  if command -v apt-get >/dev/null 2>&1; then
    "${admin[@]}" apt-get update || return 1
    if [ "$package" = python3 ]; then
      "${admin[@]}" apt-get install -y python3 python3-pip python3-venv
    else
      "${admin[@]}" apt-get install -y "$package"
    fi
  elif command -v dnf >/dev/null 2>&1; then
    "${admin[@]}" dnf install -y "$package"
  elif command -v yum >/dev/null 2>&1; then
    "${admin[@]}" yum install -y "$package"
  elif command -v pacman >/dev/null 2>&1; then
    [ "$package" != python3 ] || package=python
    "${admin[@]}" pacman -S --needed --noconfirm "$package"
  elif command -v zypper >/dev/null 2>&1; then
    "${admin[@]}" zypper --non-interactive install "$package"
  elif command -v apk >/dev/null 2>&1; then
    "${admin[@]}" apk add "$package"
  else
    echo "[pycut] 错误: 未找到支持的包管理器，请安装 $package 后重新启动"
    return 1
  fi
}

# ---------- 1. 选解释器：优先自带 cv2 的托管解释器 ----------
MANAGED="/Users/apple/.workbuddy/binaries/python/versions/3.13.12/bin/python3"
if [ -x "$PWD/.venv/bin/python3" ]; then
  PY="$PWD/.venv/bin/python3"
elif [ -x "$MANAGED" ]; then
  PY="$MANAGED"
else
  if ! command -v python3 >/dev/null 2>&1; then
    echo '[pycut] 缺少 Python，自动安装 ...'
    install_system_package python3 || exit 1
    hash -r
  fi
  if command -v python3 >/dev/null 2>&1; then
    PY="$(command -v python3)"
  elif [ "$PLATFORM" = Darwin ]; then
    PY="$(brew --prefix python@3.13)/bin/python3.13"
    [ -x "$PY" ] || exit 1
  else
    echo '[pycut] 错误: Python 安装后仍不可用'
    exit 1
  fi
fi
echo "[pycut] 解释器: $PY"

# ---------- 2. .env：没有就由模板生成，并提示填密钥 ----------
if [ ! -f .env ]; then
  if [ -f .env.example ]; then
    cp .env.example .env
    echo "[pycut] 已由 .env.example 生成 .env —— 请编辑填入主网关密钥 PYCUT_MAIN_KEY"
  else
    echo "[pycut] 警告: 缺少 .env，也未找到 .env.example"
  fi
fi

# ---------- 3. 检查核心依赖与纯 TUI 目录组件 ----------
# CLI 只需核心视频依赖；TUI 另外使用成熟的 textual-fspicker。
IS_TUI=0
if [ "$#" -eq 0 ] || [ "${1:-}" = "tui" ]; then
  IS_TUI=1
fi
REQUIRED_IMPORTS='import cv2, numpy; assert cv2.__version__.split(".")[0] == "4"; assert hasattr(cv2, "CascadeClassifier")'
if [ "$IS_TUI" -eq 1 ]; then
  REQUIRED_IMPORTS="$REQUIRED_IMPORTS; import curses, textual, textual_fspicker"
fi
if [ -f requirements.txt ]; then
  if "$PY" -c "$REQUIRED_IMPORTS" >/dev/null 2>&1; then
    echo '[pycut] 运行依赖已就绪'
  else
    echo '[pycut] 安装缺失依赖 requirements.txt ...'
    if "$PY" -c 'import pathlib, sysconfig, sys; sys.exit(not (sys.prefix == sys.base_prefix and (pathlib.Path(sysconfig.get_path("stdlib")) / "EXTERNALLY-MANAGED").exists()))' >/dev/null 2>&1; then
      echo '[pycut] 系统 Python 禁止全局 pip 安装，使用项目 .venv'
      "$PY" -m venv .venv || exit 1
      PY="$PWD/.venv/bin/python3"
    fi
    if ! "$PY" -m pip --version >/dev/null 2>&1; then
      "$PY" -m ensurepip --upgrade || exit 1
    fi
    "$PY" -m pip install -q --disable-pip-version-check -r requirements.txt || {
      echo '[pycut] 错误: 依赖安装失败，请检查网络及 Python 安装权限'
      exit 1
    }
  fi
fi

if ! "$PY" -c "$REQUIRED_IMPORTS" >/dev/null 2>&1; then
  echo '[pycut] 错误: 安装后依赖仍未就绪，停止启动'
  exit 1
fi

# ---------- FFmpeg：使用与处理代码一致的工具路径，安装后复检 ----------
check_media_tools() {
  "$PY" - <<'PY'
import subprocess
from pycut import config
for name in ('ffmpeg', 'ffprobe'):
    path = config._media_tool(name)
    try:
        result = subprocess.run([path, '-version'], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=15)
        if result.returncode:
            raise OSError('version check failed')
    except (OSError, subprocess.TimeoutExpired):
        print(f'[pycut] 缺失或不可用: {name} ({path})')
        raise SystemExit(1)
PY
}
if ! check_media_tools; then
  echo '[pycut] 自动安装 FFmpeg（含 ffprobe）...'
  install_system_package ffmpeg || exit 1
  hash -r
  if ! check_media_tools; then
    echo '[pycut] 错误: 安装后 FFmpeg 仍不可用，请检查 PATH 或 PYCUT_FFMPEG/PYCUT_FFPROBE 配置'
    exit 1
  fi
fi
echo '[pycut] FFmpeg / ffprobe 已就绪'
if [ "$IS_TUI" -eq 1 ] && ! "$PY" -c 'import textual, textual_fspicker' >/dev/null 2>&1; then
  echo '[pycut] 错误: 缺少纯终端目录选择组件 textual-fspicker'
  echo "        运行: $PY -m pip install -r requirements.txt"
  exit 1
fi
# ---------- 4. 硬校验 cv2（没有就别往下跑，切片必然失败）----------
if ! "$PY" -c 'import cv2; assert hasattr(cv2, "CascadeClassifier")' >/dev/null 2>&1; then
  echo "[pycut] 错误: 当前解释器 ($PY) 缺少兼容的 OpenCV 4 Haar 支持。"
  echo "        手动安装: $PY -m pip install -r requirements.txt"
  exit 1
fi

# ---------- 5. 打印生效的网关配置（不打印密钥），缺密钥则告警 ----------
"$PY" -c "
import sys
sys.path.insert(0, '.')
from pycut import config
for line in config.gateway_summary().splitlines():
    print('[pycut] ' + line)
if not config.key_ready():
    print('[pycut] 警告: 主网关未配置密钥（.env 的 PYCUT_MAIN_KEY），LLM 步骤会 401。')
    print('         可先填密钥，或用 --no-llm 走纯 Haar（不调用大模型）。')
" 2>/dev/null || echo "[pycut] 警告: 无法读取网关配置，请检查 .env"

# ---------- 6. 运行入口（无参数且为交互终端会自动进 TUI）----------
exec "$PY" pycut.py "$@"
