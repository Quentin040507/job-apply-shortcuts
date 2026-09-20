#!/bin/zsh
# 求职填表快捷键工具包 · 启动器
# 双击本文件即可打开编辑页面。可随意移动整个文件夹。
set -u

DIR="$(cd "$(dirname "$0")" && pwd)"          # kit 目录
ROOT="$(dirname "$DIR")"                      # 工具包根目录
MAIN="$DIR/jobkit.py"
LOG="$ROOT/data/jobkit.log"
URLFILE="/tmp/jobkit.url"
PING="http://127.0.0.1:8770/api/ping"

echo ""
echo "  求职填表快捷键工具包"
echo "  ────────────────────────────"

if [ ! -f "$MAIN" ]; then
  echo "  ❌ 找不到主程序：$MAIN"
  echo "     请确认没有把 kit 文件夹单独挪出去。"
  echo ""; read -r "?按回车键关闭…"; exit 1
fi

# ---------- 1. 找一个能用的 python3 ----------
PY="$(command -v python3 2>/dev/null || true)"
if [ -z "$PY" ]; then
  echo "  ⚠️  没有找到 python3，需要先装一个（免费，一次就好）。"
  echo ""
  echo "     正在为你打开安装程序…装完请重新双击本文件。"
  xcode-select --install >/dev/null 2>&1 || true
  echo ""; read -r "?按回车键关闭…"; exit 1
fi

if ! "$PY" -c 'import sys,json,plistlib,http.server' >/dev/null 2>&1; then
  echo "  ⚠️  python3 还没装好（macOS 会弹窗让你安装命令行工具）。"
  xcode-select --install >/dev/null 2>&1 || true
  echo "     装完请重新双击本文件。"
  echo ""; read -r "?按回车键关闭…"; exit 1
fi

mkdir -p "$ROOT/data"

# ---------- 2. 已经在跑就复用，不重复开 ----------
if curl -s -m 1 -o /dev/null "$PING" 2>/dev/null; then
  URL="$(cat "$URLFILE" 2>/dev/null || echo http://127.0.0.1:8770/)"
  echo "  ✅ 编辑器已经在运行，直接打开。"
  open "$URL"
  echo ""
  exit 0
fi

# ---------- 3. 启动服务 ----------
rm -f "$URLFILE"
nohup "$PY" "$MAIN" --serve >>"$LOG" 2>&1 &
PID=$!

URL=""
for _ in {1..40}; do
  sleep 0.25
  if [ -f "$URLFILE" ]; then
    URL="$(cat "$URLFILE")"
    [ -n "$URL" ] && break
  fi
  # 进程已经挂了就不用等了
  kill -0 "$PID" 2>/dev/null || break
done

if [ -z "$URL" ]; then
  echo "  ❌ 编辑器没能启动。下面是最后 20 行日志："
  echo "  ────────────────────────────"
  tail -n 20 "$LOG" 2>/dev/null | sed 's/^/  /'
  echo "  ────────────────────────────"
  echo "  日志文件：$LOG"
  echo ""; read -r "?按回车键关闭…"; exit 1
fi

echo "  ✅ 已启动：$URL"
echo ""
echo "  接下来：把页面里的示例内容改成你自己的 →"
echo "  点「安装到系统」→ 之后在任何输入框打「缩写 + 空格」就会整段展开。"
echo ""
open "$URL"

# 前台等待，关掉这个窗口就等于停掉编辑器
wait "$PID" 2>/dev/null
