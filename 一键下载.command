#!/usr/bin/env bash
# 医疗影像云下载 —— macOS / Linux 一键脚本
set -e
cd "$(dirname "$0")"

LINK="$1"
if [ -z "$LINK" ]; then
  read -r -p "请粘贴影像分享链接: " LINK
fi
[ -z "$LINK" ] && exit 1

if ! command -v python3 >/dev/null 2>&1; then
  echo "未检测到 python3，请先安装 Python 3.9+"
  exit 1
fi

if ! python3 -c "import requests, pydicom, numpy, PIL" >/dev/null 2>&1; then
  echo "首次运行，正在安装依赖..."
  python3 -m pip install --quiet requests pydicom numpy pillow
fi

echo "开始下载..."
python3 yjp_dl.py "$LINK" -o "./下载结果"
echo "输出目录: $(pwd)/下载结果"
