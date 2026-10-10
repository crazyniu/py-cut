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

# ---------- 1. 选解释器：优先自带 cv2 的托管解释器 ----------
MANAGED="/Users/apple/.workbuddy/binaries/python/versions/3.13.12/bin/python3"
if [ -x "$MANAGED" ]; then
  PY="$MANAGED"
else
  PY="$(command -v python3 || echo python3)"
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
REQUIRED_IMPORTS='import cv2, numpy'
if [ "$IS_TUI" -eq 1 ]; then
  REQUIRED_IMPORTS='import cv2, numpy, textual, textual_fspicker'
fi
if [ -f requirements.txt ]; then
  if "$PY" -c "$REQUIRED_IMPORTS" >/dev/null 2>&1; then
    echo '[pycut] 运行依赖已就绪'
  else
    echo '[pycut] 安装缺失依赖 requirements.txt ...'
    "$PY" -m pip install -q --disable-pip-version-check -r requirements.txt || \
      echo '[pycut] 警告: pip 安装失败，可能无网络或不可安装依赖'
  fi
fi
if [ "$IS_TUI" -eq 1 ] && ! "$PY" -c 'import textual, textual_fspicker' >/dev/null 2>&1; then
  echo '[pycut] 错误: 缺少纯终端目录选择组件 textual-fspicker'
  echo "        运行: $PY -m pip install -r requirements.txt"
  exit 1
fi
# ---------- 4. 硬校验 cv2（没有就别往下跑，切片必然失败）----------
if ! "$PY" -c "import cv2" >/dev/null 2>&1; then
  echo "[pycut] 错误: 当前解释器 ($PY) 没有 cv2。"
  echo "        手动安装: $PY -m pip install -r requirements.txt"
  exit 1
fi

# ---------- 5. 打印生效的网关配置（不打印密钥），缺密钥则告警 ----------
"$PY" -c "
import sys
sys.path.insert(0, '.')
from pycut import config
for line in config.gateways_summary().splitlines():
    print('[pycut] ' + line)
if not config.key_ready():
    print('[pycut] 警告: 主网关未配置密钥（.env 的 PYCUT_MAIN_KEY），LLM 步骤会 401。')
    print('         可先填密钥，或用 --no-llm 走纯 Haar（不调用大模型）。')
" 2>/dev/null || echo "[pycut] 警告: 无法读取网关配置，请检查 .env"

# ---------- 6. 运行入口（无参数且为交互终端会自动进 TUI）----------
exec "$PY" pycut.py "$@"
