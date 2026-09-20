#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
求职填表快捷键工具包 · 核心程序（单文件，无第三方依赖）

做什么：把你填表要反复输入的内容（姓名、邮箱、学历、经历、开放题答案…）
做成 macOS 系统级文本替换——在任意输入框打「缩写 + 空格」就整段展开。
同时生成一份可搜索、可点击复制的速查表放在桌面。

怎么用：
  双击工具包里的「① 开始使用.command」，浏览器会打开编辑页面，
  填完点「安装到系统」即可。之后改内容：改完再点一次「同步」。

命令行（可选）：
  python3 jobkit.py --serve        启动编辑页面
  python3 jobkit.py --install      按 profile.json 安装/更新（不开页面）
  python3 jobkit.py --uninstall    卸载本工具写入的所有条目
  python3 jobkit.py --status       查看当前安装状态

设计要点：
  * 路径全部相对本文件解析，整个文件夹可以随意移动 / 改名 / 换电脑
  * 每写一条都记进 data/state.json，卸载与更新只动自己写过的条目，
    绝不碰你自己在「系统设置 → 键盘 → 文本替换」里加的其它内容
  * 中英文分别用 `;;xxx` 与 `;;xxxcn` 两个缩写，会自动同时写入半角与全角分号两套
  * 写的是 macOS 真正生效的那份数据：~/Library/KeyboardServices/TextReplacements.db
    （Core Data SQLite）。旧版的 NSUserDictionaryReplacementItems plist 只作兼容性附带写入——
    因为新 macOS 只在首次迁移时读它一次，光写 plist 会出现「装好了却按不出来」。

⚠️ 一个绕不开的坑：这份数据会通过 iCloud 在你自己的设备间同步（iPhone / iPad）。
   如果别的设备上存着一份旧的文本替换，iCloud 一同步就可能把这里的覆盖掉。
   本工具写入时会标记「需要上传」，让这台电脑成为权威 —— 代价是那些条目也会出现在
   你的其它设备上。不想这样，就在「系统设置 → 你的名字 → iCloud」里关掉「键盘」同步。
"""

import json
import os
import plistlib
import subprocess
import sys
import threading
import webbrowser
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)             # 工具包根目录
DATA = os.path.join(ROOT, "data")
PROFILE = os.path.join(DATA, "profile.json")
STATE = os.path.join(DATA, "state.json")

DOMAIN = "NSGlobalDomain"
KEY = "NSUserDictionaryReplacementItems"     # 2007 年那代旧存储，现在只作兼容性附带写入
# macOS 真正生效的文本替换：Core Data SQLite，且由 NSPersistentCloudKitContainer
# 通过 iCloud（CloudKit Zone: TextReplacements）在设备间同步。
TR_DB = os.environ.get("JOBKIT_TR_DB") or os.path.expanduser(
    "~/Library/KeyboardServices/TextReplacements.db")
ENTITY = "TextReplacementEntry"              # Core Data 实体名
CD_EPOCH = 978307200                         # Core Data 时间戳零点：2001-01-01 UTC
PORT_START = 8770

IS_MAC = sys.platform == "darwin"


# ---------------------------------------------------------------- 数据读写

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_profile():
    return load_json(PROFILE, {"title": "我的求职快捷键", "prefix": ";;", "groups": []})


def all_fields(profile):
    for g in profile.get("groups", []):
        for f in g.get("fields", []):
            yield g, f


# ---------------------------------------------------------------- 条目生成

def build_entries(profile):
    """把 profile 展开成 [{sc, phrase, key, lang, label}]，自动处理中英与全角半角"""
    prefix = profile.get("prefix") or ";;"
    out, seen = [], set()

    def add(sc, phrase, key, lang):
        sc = (sc or "").strip()
        if not sc or not (phrase or "").strip():
            return
        code = prefix + sc
        if code in seen:
            return                      # 缩写重复时保留先出现的那个
        seen.add(code)
        out.append({"sc": code, "phrase": phrase, "key": key, "lang": lang})

    for g, f in all_fields(profile):
        k, sc = f.get("k") or "", f.get("sc") or ""
        if not k or not sc:
            continue
        add(sc, f.get("en"), k, "en")
        add(f.get("sccn") or (sc + "cn"), f.get("cn"), k, "cn")
    return out


def with_fullwidth(entries):
    """中文输入法下 ;; 会打成全角 ；；，所以两套都要注册"""
    extra = []
    for e in entries:
        code = e["sc"]
        if code.startswith(";;"):
            extra.append({**e, "sc": "；；" + code[2:], "alias": True})
    return entries + extra


# ---------------------------------------------------------------- 系统写入
#
# 这里是整个工具最关键的地方，踩过坑才搞清楚：
#
#   macOS 真正生效的文本替换存在 ~/Library/KeyboardServices/TextReplacements.db
#   （Core Data SQLite），并由 iCloud 在设备间同步。
#   NSGlobalDomain 里的 NSUserDictionaryReplacementItems 是 10.5 时代的旧存储，
#   新系统只在首次迁移时读一次 —— 只写它的话，系统设置里看不到、键盘也按不出来。
#
# 所以：以数据库为准，plist 只作为兼容性附带写入（老系统 / 别的读取方还能看到）。


def db_available():
    return os.path.exists(TR_DB)


def _cols(cur):
    return [r[1] for r in cur.execute("PRAGMA table_info(ZTEXTREPLACEMENTENTRY)")]


def read_db():
    """读出生效中的文本替换 -> {缩写: 内容}"""
    if not db_available():
        return {}
    import sqlite3
    try:
        con = sqlite3.connect("file:" + TR_DB + "?mode=ro", uri=True)
    except Exception:
        return {}
    try:
        if "ZSHORTCUT" not in _cols(con.cursor()):
            return {}
        return {sc: ph for sc, ph in con.execute(
            "SELECT ZSHORTCUT, ZPHRASE FROM ZTEXTREPLACEMENTENTRY WHERE ZWASDELETED=0")
            if sc}
    except Exception:
        return {}
    finally:
        con.close()


def _entity_row(cur):
    """拿到 TextReplacementEntry 的实体编号与当前最大主键"""
    r = cur.execute("SELECT Z_ENT, Z_MAX FROM Z_PRIMARYKEY WHERE Z_NAME=?",
                    (ENTITY,)).fetchone()
    if r:
        return r[0], (r[1] or 0)
    ent = cur.execute("SELECT COALESCE(MAX(Z_ENT),0)+1 FROM Z_PRIMARYKEY").fetchone()[0]
    cur.execute("INSERT INTO Z_PRIMARYKEY (Z_ENT, Z_NAME, Z_SUPER, Z_MAX) VALUES (?,?,0,0)",
                (ent, ENTITY))
    return ent, 0


def write_db(want, drop):
    """把 want({缩写:内容}) 写入数据库、把 drop(集合) 标记删除。返回 (新增, 更新, 清理)"""
    import sqlite3
    import time as _t
    import uuid
    con = sqlite3.connect(TR_DB)
    con.execute("PRAGMA busy_timeout=15000")
    cur = con.cursor()
    if "ZSHORTCUT" not in _cols(cur):
        con.close()
        raise RuntimeError("TextReplacements.db 的结构和预期不符，没有改动它")
    ent, zmax = _entity_row(cur)

    rows = {sc: (pk, ph, bool(del_))
            for pk, sc, ph, del_ in cur.execute(
                "SELECT Z_PK, ZSHORTCUT, ZPHRASE, ZWASDELETED FROM ZTEXTREPLACEMENTENTRY")}
    now = _t.time() - CD_EPOCH
    added = updated = removed = 0

    for sc, ph in want.items():
        if sc in rows:
            pk, old, gone = rows[sc]
            if gone or old != ph:
                cur.execute(
                    """UPDATE ZTEXTREPLACEMENTENTRY
                          SET ZPHRASE=?, ZWASDELETED=0, ZNEEDSSAVETOCLOUD=1,
                              Z_OPT=COALESCE(Z_OPT,1)+1, ZTIMESTAMP=?
                        WHERE Z_PK=?""", (ph, now, pk))
                updated += 1
        else:
            zmax += 1
            cur.execute(
                """INSERT INTO ZTEXTREPLACEMENTENTRY
                   (Z_PK, Z_ENT, Z_OPT, ZNEEDSSAVETOCLOUD, ZWASDELETED,
                    ZTIMESTAMP, ZPHRASE, ZSHORTCUT, ZUNIQUENAME, ZREMOTERECORDINFO)
                   VALUES (?,?,1,1,0,?,?,?,?,NULL)""",
                (zmax, ent, now, ph, sc, str(uuid.uuid4()).upper()))
            added += 1

    for sc in drop:
        if sc in rows and not rows[sc][2]:
            cur.execute(
                """UPDATE ZTEXTREPLACEMENTENTRY
                      SET ZWASDELETED=1, ZNEEDSSAVETOCLOUD=1,
                          Z_OPT=COALESCE(Z_OPT,1)+1
                    WHERE Z_PK=?""", (rows[sc][0],))
            removed += 1

    cur.execute("UPDATE Z_PRIMARYKEY SET Z_MAX=? WHERE Z_ENT=?", (zmax, ent))
    con.commit()
    con.close()
    return added, updated, removed


def read_plist():
    r = subprocess.run(["defaults", "export", DOMAIN, "-"], capture_output=True)
    if r.returncode != 0:
        return []
    try:
        import tempfile
        tmp = os.path.join(tempfile.gettempdir(), "_jobkit_read.plist")
        with open(tmp, "wb") as f:
            f.write(r.stdout)
        return plistlib.load(open(tmp, "rb")).get(KEY) or []
    except Exception:
        return []


def write_plist_merged(want, drop):
    """兼容性附带写入：把 want 合并进旧 plist，并从里面移除 drop。失败不影响主流程"""
    try:
        items = read_plist()
        by = {e.get("replace"): e for e in items}
        for sc, ph in want.items():
            if sc in by:
                by[sc]["with"] = ph
                by[sc]["on"] = 1
            else:
                items.append({"replace": sc, "with": ph, "on": 1})
        items = [e for e in items if e.get("replace") not in drop]
        import tempfile
        tmp = os.path.join(tempfile.gettempdir(), "_jobkit_write.plist")
        with open(tmp, "wb") as f:
            plistlib.dump({KEY: items}, f, fmt=plistlib.FMT_XML)
        subprocess.run(["defaults", "import", DOMAIN, tmp], capture_output=True)
    except Exception:
        pass


def install(profile):
    """把 profile 的内容装进系统；返回日志行"""
    if not IS_MAC:
        return ["❌ 本工具依赖 macOS 的系统「文本替换」功能，当前系统不支持。"]

    entries = with_fullwidth(build_entries(profile))
    if not entries:
        return ["⚠️ 没有可安装的内容——请先在编辑页面填写至少一个字段。"]

    if not db_available():
        return [
            "⚠️ 没找到系统的文本替换数据库：",
            "   " + TR_DB,
            "",
            "   说明这台电脑还从没用过「文本替换」功能，系统还没建好这个库。",
            "   请先做一次：系统设置 → 键盘 → 文本替换 → 点 ＋ 随便加一条",
            "   （例如缩写 abc、内容 test），然后回到这里再点一次「安装到系统」。",
        ]

    want = {e["sc"]: e["phrase"] for e in entries}
    state = load_json(STATE, {"written": []})
    old = set(state.get("written", []))
    gone = old - set(want)

    try:
        added, updated, removed = write_db(want, gone)
    except Exception as e:
        return [f"❌ 写入系统文本替换失败：{type(e).__name__}: {e}"]

    write_plist_merged(want, gone)

    save_json(STATE, {
        "written": sorted(want),
        "updated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "title": profile.get("title", ""),
    })

    n_en = sum(1 for e in entries if e["lang"] == "en")
    n_cn = sum(1 for e in entries if e["lang"] == "cn")
    return [
        f"✅ 已同步到系统文本替换：新增 {added} · 更新 {updated} · 清理 {removed}",
        f"   共 {len(want)} 条（英文 {n_en} · 中文 {n_cn}），含全角/半角两套写法",
        "",
        "现在去任意输入框试一下：打一个缩写，再敲空格。",
        "若没立刻生效：关掉再重开那个 App；仍然不行就重启一次电脑。",
    ]


def uninstall():
    if not IS_MAC:
        return ["❌ 当前系统不支持。"]
    state = load_json(STATE, {"written": []})
    mine = set(state.get("written", []))
    if not mine:
        return ["ℹ️ 没有记录到本工具写入过任何条目，未做改动。"]
    if not db_available():
        return ["ℹ️ 没找到文本替换数据库，未做改动。"]

    n = len(mine & set(read_db()))
    try:
        write_db({}, mine)
    except Exception as e:
        return [f"❌ 卸载失败：{e}"]
    write_plist_merged({}, mine)

    save_json(STATE, {"written": [], "updated_at":
                      datetime.datetime.now().isoformat(timespec="seconds")})
    return [f"✅ 已移除本工具写入的 {n} 条，其它设置（包括你自己加的内容）未动。"]


def status():
    state = load_json(STATE, {"written": []})
    mine = set(state.get("written", []))
    have = read_db()
    missing = sorted(mine - set(have))
    return {
        "platform_ok": IS_MAC,
        "db_ok": db_available(),
        "installed": len(mine & set(have)),
        "missing": len(missing),
        "total_in_system": len(have),
        "updated_at": state.get("updated_at", ""),
        "title": state.get("title", ""),
    }


# ---------------------------------------------------------------- 速查表

def build_cheatsheet(profile):
    prefix = profile.get("prefix") or ";;"
    cards, n = [], 0
    for g in profile.get("groups", []):
        rows = []
        for f in g.get("fields", []):
            sc = (f.get("sc") or "").strip()
            if not sc:
                continue
            for lang, val, code in (
                ("en", f.get("en"), prefix + sc),
                ("cn", f.get("cn"), prefix + (f.get("sccn") or sc + "cn")),
            ):
                if not (val or "").strip():
                    continue
                rows.append({"c": "；；" + code[len(prefix):], "l": f.get("label", sc),
                             "v": val, "lang": lang})
        if rows:
            n += len(rows)
            cards.append({"g": g.get("name", ""), "rows": rows})
    if not cards:
        return None

    html = CHEAT_HTML
    html = (html.replace("__DATA__", json.dumps(cards, ensure_ascii=False))
                .replace("__TITLE__", (profile.get("title") or "求职填表快捷键"))
                .replace("__ROWS__", str(n))
                .replace("__DATE__", datetime.date.today().strftime("%Y-%m-%d")))
    out = None
    if IS_MAC:
        desk = os.path.join(os.path.expanduser("~"), "Desktop")
        if os.path.isdir(desk):
            out = os.path.join(desk, (profile.get("title") or "求职填表快捷键") + "速查.html")
    if not out:
        out = os.path.join(ROOT, "速查表.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    return out


# ---------------------------------------------------------------- 编辑页面

EDITOR_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>求职快捷键 · 编辑</title>
<style>
 :root{--ink:#1c1c1e;--muted:#6b6b70;--line:#e6e4df;--bg:#faf9f7;--card:#fff;--ac:#185FA5;
       --ok:#1D9E75;--warn:#BA7517;--dirty:#fffdf6}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.6 -apple-system,"SF Pro Text","PingFang SC",sans-serif}
 header{background:var(--card);border-bottom:1px solid var(--line);padding:16px 26px;position:sticky;top:0;z-index:9}
 h1{margin:0 0 4px;font-size:18px;font-weight:600}
 .sub{color:var(--muted);font-size:13px}
 .bar{display:flex;gap:9px;align-items:center;margin-top:12px;flex-wrap:wrap}
 button{font:inherit;padding:8px 15px;border-radius:9px;border:1px solid var(--ac);background:var(--ac);color:#fff;cursor:pointer}
 button.ghost{background:#fff;color:var(--ink);border-color:var(--line)}
 button:disabled{opacity:.45;cursor:default}
 input[type=search],input[type=text]{padding:8px 12px;border:1px solid var(--line);border-radius:9px;font:inherit;background:#fff;color:var(--ink)}
 input[type=search]{flex:1;min-width:200px;max-width:360px}
 input:focus,textarea:focus{outline:none;border-color:var(--ac)}
 #msg{font-size:13px;color:var(--muted)}
 main{padding:18px 26px 90px;max-width:1080px;margin:0 auto}
 .card{background:var(--card);border:1px solid var(--line);border-radius:12px;margin-bottom:13px;overflow:hidden}
 .card>h2{margin:0;padding:10px 18px;font-size:13.5px;font-weight:600;color:var(--ac);
   background:#f7f9fc;border-bottom:1px solid var(--line);display:flex;gap:10px;align-items:center}
 .card>h2 input{border:1px solid transparent;background:transparent;font-weight:600;color:var(--ac);font-size:13.5px;padding:2px 6px;flex:1}
 .card>h2 input:hover{border-color:var(--line);background:#fff}
 .f{padding:11px 18px;border-bottom:1px solid #f1efec;display:grid;
    grid-template-columns:180px 1fr;gap:6px 14px;align-items:start}
 .f:last-child{border-bottom:none}
 .f.dirty{background:var(--dirty)}
 .head{font-size:12.5px;color:var(--muted);padding-top:7px}
 .head code{display:block;font-family:ui-monospace,Menlo,monospace;font-size:11.5px;color:var(--ac);margin-top:3px}
 .head input{width:100%;font-size:12px;padding:3px 6px;margin-top:4px}
 .vals{display:flex;flex-direction:column;gap:7px}
 .row{display:grid;grid-template-columns:34px 1fr;gap:9px;align-items:start}
 .tag{font-size:11px;color:var(--muted);padding-top:9px;text-align:right}
 textarea{width:100%;border:1px solid var(--line);border-radius:8px;padding:7px 10px;
   font:13px/1.55 inherit;resize:vertical;min-height:36px;background:#fff;color:var(--ink)}
 .tips{background:#e8f4fd;border:1px solid #b5d4f4;border-radius:12px;padding:14px 18px;margin-bottom:16px;font-size:13px}
 .tips b{color:var(--ac)}
 .tips code{background:#fff;border:1px solid #b5d4f4;border-radius:5px;padding:1px 6px;
   font-family:ui-monospace,Menlo,monospace;font-size:12.5px}
 pre{background:#2c2c2a;color:#e8e6e0;padding:14px 16px;border-radius:10px;font-size:12px;
   overflow:auto;max-height:280px;white-space:pre-wrap}
 .hide{display:none}
 .st{font-size:12.5px;color:var(--muted);margin-left:auto}
 @media(max-width:760px){.f{grid-template-columns:1fr}.head{padding-top:0}}
</style></head><body>
<header>
  <h1 id="title">求职快捷键</h1>
  <div class="sub">填好内容 → 点「安装到系统」→ 在任何输入框打缩写 + 空格就会整段展开</div>
  <div class="bar">
    <button id="inst" onclick="install()">安装到系统</button>
    <button class="ghost" onclick="addGroup()">＋ 新增分组</button>
    <button class="ghost" onclick="uninstall()">卸载</button>
    <input type="search" id="q" placeholder="搜索：邮箱 / 经历 / 中文…" oninput="filter(this.value)">
    <span class="st" id="stat"></span>
  </div>
  <div class="bar"><span id="msg"></span></div>
</header>
<main>
  <div class="tips">
    <b>缩写怎么定：</b>只用小写字母和数字，例如 <code>name</code> → 打 <code>；；name</code>。
    中文版会自动加 cn 后缀（<code>；；namecn</code>），不用自己填。<br>
    <b>为什么不能用连字符：</b>中文输入法下 <code>-</code> 会打成全角的 <code>－</code>，缩写里带连字符会永远触发不了。<br>
    <b>装完怎么用：</b>打完缩写后必须再敲<b>空格或回车</b>才会展开（这是最常踩的坑）。<br>
    <b>装完没反应？</b>先关掉再重开那个 App；仍然不行就重启一次电脑，让系统重新加载词库。<br>
    <b>会不会影响我别的地方：</b>这些内容会通过 iCloud 同步到你登录的其它 Apple 设备（iPhone / iPad）。
    不想这样就在「系统设置 → 你的名字 → iCloud」里关掉「键盘」同步。
  </div>
  <div id="list"></div>
  <pre id="log" class="hide"></pre>
</main>
<script>
let P = null;
const CH = ['①','②','③','④','⑤','⑥','⑦','⑧','⑨','⑩','⑪','⑫','⑬','⑭','⑮'];

function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
function auto(el){el.style.height='auto';el.style.height=(el.scrollHeight+2)+'px'}

function render(){
  document.getElementById('title').textContent = P.title || '求职快捷键';
  document.title = (P.title || '求职快捷键') + ' · 编辑';
  const list = document.getElementById('list');
  let html = '';
  P.groups.forEach((g, gi) => {
    html += `<section class="card"><h2>${CH[gi]||'·'}<input value="${esc(g.name)}"
      oninput="P.groups[${gi}].name=this.value;dirty()"> </h2>`;
    (g.fields||[]).forEach((f, fi) => {
      const sc = (P.prefix||';;') + (f.sc||'');
      html += `<div class="f" data-i="${gi}-${fi}">
        <div class="head">
          ${esc(f.label||'')}
          <code>；；${esc(f.sc||'')}</code>
          <input value="${esc(f.sc||'')}" placeholder="缩写"
            oninput="setSc(${gi},${fi},this.value)" title="只用小写字母和数字">
        </div>
        <div class="vals">`;
      if(f.showen !== false) html += `<div class="row"><div class="tag">英</div>
        <textarea oninput="setV(${gi},${fi},'en',this.value)" placeholder="英文内容">${esc(f.en)}</textarea></div>`;
      html += `<div class="row"><div class="tag">中</div>
        <textarea oninput="setV(${gi},${fi},'cn',this.value)" placeholder="中文内容（留空则只生成英文缩写）">${esc(f.cn)}</textarea></div>
        </div></div>`;
    });
    html += `<div class="f" style="display:block">
      <button class="ghost" onclick="addField(${gi})">＋ 在这个分组里加一条</button>
      ${P.groups.length>1?`<button class="ghost" onclick="delGroup(${gi})">删除本分组</button>`:''}
      </div></section>`;
  });
  list.innerHTML = html;
  list.querySelectorAll('textarea').forEach(auto);
  document.querySelectorAll('.f').forEach(f=>f.classList.remove('dirty'));
}
function setV(gi,fi,lang,v){ P.groups[gi].fields[fi][lang] = v; dirty(); auto(event.target); }
function setSc(gi,fi,v){ P.groups[gi].fields[fi].sc = v.replace(/[^a-z0-9]/gi,'').toLowerCase(); dirty(); }
function dirty(){ document.getElementById('stat').textContent = '● 有改动未保存'; }
function addField(gi){
  const sc = prompt('这一条的缩写（只用小写字母和数字，例如 phone）');
  if(!sc) return;
  P.groups[gi].fields.push({k:sc, sc:sc.replace(/[^a-z0-9]/gi,'').toLowerCase(),
    label:prompt('这一条显示成什么名字？例如「手机号」') || sc, en:'', cn:''});
  render();
}
function addGroup(){
  const n = prompt('新分组的名字，例如「获奖经历」');
  if(!n) return;
  P.groups.push({name:n, fields:[]});
  render();
}
function delGroup(gi){ if(confirm('删除这个分组及其全部条目？')){ P.groups.splice(gi,1); render(); } }
function filter(q){
  q=(q||'').trim().toLowerCase();
  document.querySelectorAll('.card').forEach(card=>{
    let n=0;
    card.querySelectorAll('.f[data-i]').forEach(f=>{
      const t=f.textContent.toLowerCase();
      const hit=!q||t.includes(q);
      f.style.display=hit?'':'none'; if(hit)n++;
    });
    card.style.display=n?'':'none';
  });
}
function busy(on,t){ document.getElementById('inst').disabled=on;
  document.getElementById('msg').textContent=t||''; }
function show(d, okText){
  busy(false,'');
  const log=document.getElementById('log'); log.classList.remove('hide');
  log.textContent=(d.log||[]).join('\n');
  document.getElementById('msg').innerHTML = d.ok
    ? `<span style="color:var(--ok)">${okText}</span>`
    : `<span style="color:var(--warn)">⚠️ 出错了，看下方信息</span>`;
  if(d.ok) document.getElementById('stat').textContent='✅ 已是最新';
  if(d.status) refreshStatus(d.status);
}
function refreshStatus(s){
  if(!s||!s.platform_ok) return;
  document.getElementById('stat').textContent =
    `已装 ${s.installed} 条` + (s.missing?` · ${s.missing} 条缺失`:'') +
    ` · 系统共 ${s.total_in_system} 条`;
}
async function install(){
  busy(true,'正在写入系统…');
  const r=await fetch('/api/install',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({profile:P})});
  const d=await r.json();
  show(d, `✅ 已同步 · ${(d.cheat?('速查表已更新：'+d.cheat):'')}`);
}
async function uninstall(){
  if(!confirm('从系统里移除本工具写入的全部快捷键？你自己的其它文本替换不会被动。')) return;
  busy(true,'正在卸载…');
  const r=await fetch('/api/uninstall',{method:'POST'});
  const d=await r.json(); show(d,'✅ 已卸载');
}
fetch('/api/data').then(r=>r.json()).then(d=>{ P=d.profile; render(); refreshStatus(d.status); });
</script></body></html>
"""

CHEAT_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__速查</title>
<style>
 :root{--ink:#1c1c1e;--muted:#6b6b70;--line:#e6e4df;--bg:#faf9f7;--card:#fff;--ac:#185FA5;--ok:#1D9E75}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.6 -apple-system,"SF Pro Text","PingFang SC",sans-serif}
 header{background:var(--card);border-bottom:1px solid var(--line);padding:20px 30px 16px;position:sticky;top:0;z-index:9}
 h1{margin:0 0 5px;font-size:19px;font-weight:600}
 .sub{color:var(--muted);font-size:13px}.sub b{color:var(--ink);font-weight:600}
 .tools{display:flex;gap:10px;align-items:center;margin-top:13px;flex-wrap:wrap}
 input[type=search]{flex:1;min-width:200px;max-width:400px;padding:9px 13px;border:1px solid var(--line);
   border-radius:10px;font:inherit;background:#fff;color:var(--ink)}
 input[type=search]:focus{outline:none;border-color:var(--ac)}
 .pill{font-size:12.5px;color:var(--muted);background:#fff;border:1px solid var(--line);border-radius:999px;padding:4px 11px}
 main{padding:20px 30px 60px;max-width:1040px;margin:0 auto}
 .card{background:var(--card);border:1px solid var(--line);border-radius:12px;margin-bottom:15px;overflow:hidden}
 .card h2{margin:0;padding:11px 18px;font-size:13.5px;font-weight:600;color:var(--ac);background:#f7f9fc;
   border-bottom:1px solid var(--line);display:flex;justify-content:space-between}
 .card h2 span{color:var(--muted);font-weight:400;font-size:12.5px}
 table{width:100%;border-collapse:collapse}
 tr{border-bottom:1px solid #f1efec;cursor:pointer}
 tr:last-child{border-bottom:none}tr:hover{background:#fbfcfe}
 td{padding:9px 18px;vertical-align:top}
 .code{width:150px;white-space:nowrap;font-family:ui-monospace,Menlo,monospace;font-size:13px;color:var(--ac);font-weight:500}
 .label{width:190px;font-size:13px}
 .lang{font-size:11px;color:var(--muted);border:1px solid var(--line);border-radius:4px;padding:0 4px;margin-left:5px}
 .prev{font-size:12.5px;color:var(--muted);display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
 .cp{width:62px;text-align:right;font-size:12px;color:var(--muted);white-space:nowrap}
 tr.done .cp{color:var(--ok)}
 .note{background:#fff8e6;border:1px solid #f0d9a0;border-radius:12px;padding:13px 18px;margin-bottom:17px;font-size:13px}
 .note b{color:#BA7517}
 footer{color:var(--muted);font-size:12px;text-align:center;padding:0 30px 40px}
 @media print{header{position:static}.tools,.cp,footer{display:none}
   body{background:#fff;font-size:11.5px}.card{break-inside:avoid}main{padding:0;max-width:none}}
</style></head><body>
<header>
  <h1>__TITLE__ · 快捷键速查</h1>
  <div class="sub">共 <b>__ROWS__</b> 个快捷键 · 打缩写 + <b>空格或回车</b> 即可展开 · 半角 <b>;;</b> 与全角 <b>；；</b> 都可以</div>
  <div class="tools"><input type="search" id="q" placeholder="搜索：邮箱 / 经历 / 技能…" autofocus>
    <span class="pill" id="cnt"></span><span class="pill">点击整行 = 复制内容</span></div>
</header>
<main>
  <div class="note"><b>没反应时依次检查：</b>① 缩写后面有没有敲空格或回车；
    ② 是不是在 Terminal、密码框或某些富文本编辑器里（这些地方系统不管，请从本表点击复制）；
    ③ 都不行就在编辑器里点一次「安装到系统」。</div>
  <div id="list"></div>
</main>
<footer>生成于 __DATE__ · 由求职快捷键工具包生成 · 内容改动后重新安装会自动刷新本文件</footer>
<script>
const DATA=__DATA__;
const CH=['①','②','③','④','⑤','⑥','⑦','⑧','⑨','⑩','⑪','⑫','⑬','⑭','⑮'];
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
function render(f){
  f=(f||'').trim().toLowerCase(); let shown=0, html='';
  DATA.forEach((card,ci)=>{
    const hit=card.rows.filter(r=>!f||r.l.toLowerCase().includes(f)||r.v.toLowerCase().includes(f));
    if(!hit.length) return; shown+=hit.length;
    html+=`<section class="card"><h2>${CH[ci]||'·'} ${esc(card.g)}<span>${hit.length} 条</span></h2><table>`;
    hit.forEach(r=>{
      html+=`<tr onclick="cp(this)" title="${esc(r.v)}"><td class="code">${esc(r.c)}</td>
        <td class="label">${esc(r.l)}<span class="lang">${r.lang==='cn'?'中':'EN'}</span></td>
        <td class="prev">${esc(r.v.replace(/\n/g,' · '))}</td><td class="cp">复制</td></tr>`;
    });
    html+='</table></section>';
  });
  document.getElementById('list').innerHTML=html||'<div class="card"><h2>没有匹配项</h2></div>';
  document.getElementById('cnt').textContent=shown+' / __ROWS__ 条';
}
function cp(tr){
  const v=tr.getAttribute('title')||'';
  navigator.clipboard.writeText(v).then(()=>{
    tr.classList.add('done'); const c=tr.querySelector('.cp'); c.textContent='已复制';
    setTimeout(()=>{c.textContent='复制';tr.classList.remove('done')},1200);
  }).catch(()=>{});
}
document.getElementById('q').addEventListener('input',e=>render(e.target.value));
render('');
</script></body></html>
"""


# ---------------------------------------------------------------- 服务

def serve():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path.startswith("/api/data"):
                self._send(200, json.dumps(
                    {"profile": load_profile(), "status": status()},
                    ensure_ascii=False))
            elif self.path.startswith("/api/ping"):
                self._send(200, json.dumps({"ok": True}))
            else:
                self._send(200, EDITOR_HTML, "text/html; charset=utf-8")

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")

            if self.path.startswith("/api/quit"):
                self._send(200, json.dumps({"ok": True, "log": ["服务已退出。"]}))
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return

            if self.path.startswith("/api/install"):
                try:
                    prof = body.get("profile") or load_profile()
                    save_json(PROFILE, prof)
                    log = install(prof)
                    cheat = build_cheatsheet(prof)
                    if cheat:
                        log.append("📄 桌面速查表已更新：" + os.path.basename(cheat))
                    ok = any(l.startswith("✅") for l in log)
                    self._send(200, json.dumps(
                        {"ok": ok, "log": log, "cheat": cheat or "",
                         "status": status()}, ensure_ascii=False))
                except Exception as e:
                    self._send(500, json.dumps(
                        {"ok": False, "log": [f"❌ {type(e).__name__}: {e}"],
                         "status": status()}, ensure_ascii=False))
                return

            if self.path.startswith("/api/save"):
                save_json(PROFILE, body.get("profile") or {})
                self._send(200, json.dumps({"ok": True, "log": ["已保存。"],
                                            "status": status()}, ensure_ascii=False))
                return

            if self.path.startswith("/api/uninstall"):
                log = uninstall()
                self._send(200, json.dumps({"ok": True, "log": log,
                                            "status": status()}, ensure_ascii=False))
                return

            self._send(404, json.dumps({"ok": False, "log": ["未知请求"]}))

    srv = None
    for p in range(PORT_START, PORT_START + 15):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), H)
            break
        except OSError:
            continue
    if srv is None:
        print("❌ 端口都被占用，无法启动。请关掉一些程序再试。")
        return 1

    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    with open("/tmp/jobkit.url", "w") as f:
        f.write(url)
    print("求职快捷键编辑器已启动：" + url)
    print("（这个窗口可以最小化，别关掉；用完在页面里点「卸载」旁边的退出，或直接关掉本窗口）")
    sys.stdout.flush()
    if "--open" in sys.argv:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


# ---------------------------------------------------------------- CLI

def main():
    args = set(sys.argv[1:])
    if "--uninstall" in args:
        for l in uninstall():
            print(l)
        return 0
    if "--status" in args:
        s = status()
        if not s["platform_ok"]:
            print("当前系统不是 macOS，本工具无法使用。")
            return 1
        print(f"已安装 {s['installed']} 条"
              + (f"，另有 {s['missing']} 条在系统里找不到了" if s["missing"] else "")
              + f"；系统文本替换共 {s['total_in_system']} 条"
              + (f"；上次更新 {s['updated_at']}" if s["updated_at"] else ""))
        return 0
    if "--install" in args or "--apply" in args:
        if not IS_MAC:
            print("❌ 本工具只支持 macOS。")
            return 1
        prof = load_profile()
        for l in install(prof):
            print(l)
        out = build_cheatsheet(prof)
        if out:
            print("📄 速查表：" + out)
        return 0
    return serve()


if __name__ == "__main__":
    sys.exit(main())
