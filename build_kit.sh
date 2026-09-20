#!/bin/zsh
# 重新生成可分发的工具包压缩包
# 用法：双击本文件，或在终端里运行。产物在仓库根目录：求职填表快捷键工具包.zip
#
# 注意：必须用 Python 的 zipfile 打包（会写 UTF-8 文件名标志），
#       系统自带的 zip 命令在 macOS 上会把中文名存成乱码，Windows 解压就废了。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
OUT="$ROOT/求职填表快捷键工具包.zip"
TMP="/tmp/_jobkit_build.zip"

PY="$(command -v python3)"

cd "$ROOT"

# 清掉不该带出去的东西
rm -f data/state.json data/jobkit.log 速查表.html
find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
find . -name ".DS_Store" -delete 2>/dev/null || true
find . -name "*.pyc" -delete 2>/dev/null || true

# 确保启动器有执行权限
chmod +x "kit/① 开始使用.command" 2>/dev/null || true

# 先打到 /tmp，再挪进仓库 —— 避免打包时把自己（半成品）也扫进去
rm -f "$TMP"
"$PY" - "$ROOT" "$TMP" <<'PYEOF'
import os, sys, zipfile
root, out = sys.argv[1], sys.argv[2]
os.chdir(root)
TOP = "求职填表快捷键工具包"          # zip 内的顶层文件夹，保证解压不散落
EXCLUDE = {"data/state.json", "data/jobkit.log",
           "build_kit.sh",            # 打包脚本，收件人不需要
           ".gitignore",              # 仓库用的，分发包不需要
           "求职填表快捷键工具包.zip"}  # 压缩包不能装自己
n = 0
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for dirpath, dirnames, filenames in os.walk("."):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fn in sorted(filenames):
            if fn == ".DS_Store" or fn.endswith(".pyc"):
                continue
            full = os.path.join(dirpath, fn)
            arc = os.path.relpath(full, ".")
            if arc in EXCLUDE:
                continue
            z.write(full, TOP + "/" + arc)
            n += 1
print(f"   打包了 {n} 个文件")
PYEOF

mv -f "$TMP" "$OUT"

echo ""
echo "✅ 已生成：$OUT"
echo ""
echo "里面包含："
"$PY" -c "import zipfile,sys; [print('   '+i.filename) for i in zipfile.ZipFile(sys.argv[1]).infolist() if not i.filename.endswith('/')]" "$OUT"
echo ""
echo "发给朋友前，请先做一次脱敏自检：确认 data/profile.json 里没有你自己的真实资料。"
