#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
用官方 fnpack 打包 folder-picker 为 .fpk

    python3 tools/build_fpk.py [--fnpack <fnpack 可执行文件路径>]

做的事：
  1. 把「安装包内容」拷到干净的暂存目录（排除 tests/ tools/ dist/ 等开发文件）
  2. 做打包前自检：必需文件齐全、JSON 合法、shell 脚本是 LF、Python 能编译
  3. 调用 fnpack build
  4. 把产物挪到 dist/，并列出包内文件清单
"""

from __future__ import annotations

import argparse
import json
import os
import py_compile
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
STAGE = PROJECT / "build" / "fpk"
DIST = PROJECT / "dist"

# .fpk 里实际要装的东西
INCLUDE = ["manifest", "ICON.PNG", "ICON_256.PNG", "config", "cmd", "wizard", "app"]

# 打包前必须存在的文件（对齐官方 fnpack build 的校验规则）
REQUIRED = [
    "manifest",
    "ICON.PNG",
    "ICON_256.PNG",
    "config/privilege",
    "config/resource",
    "cmd/main",
    "cmd/install_init",
    "cmd/install_callback",
    "cmd/upgrade_init",
    "cmd/upgrade_callback",
    "cmd/uninstall_init",
    "cmd/uninstall_callback",
    "cmd/config_init",
    "cmd/config_callback",
    "app/ui/config",
    "app/ui/images/icon_64.png",
    "app/ui/images/icon_256.png",
    "app/server/folder_picker.py",
    "app/www/index.html",
    "wizard/install",
    "wizard/uninstall",
]

OK, BAD = [], []


def ok(msg):
    OK.append(msg)
    print(f"  ✅ {msg}")


def bad(msg):
    BAD.append(msg)
    print(f"  ❌ {msg}")


def check_stage():
    print("\n== 打包前自检 ==")
    for rel in REQUIRED:
        p = STAGE / rel
        if p.exists():
            ok(f"存在 {rel}")
        else:
            bad(f"缺少 {rel}")

    # JSON 合法性
    for rel in ("app/ui/config", "config/privilege", "config/resource",
                "wizard/install", "wizard/uninstall"):
        p = STAGE / rel
        if not p.exists():
            continue
        try:
            json.loads(p.read_text(encoding="utf-8"))
            ok(f"JSON 合法 {rel}")
        except ValueError as exc:
            bad(f"JSON 不合法 {rel}：{exc}")

    # manifest 必填字段
    manifest = (STAGE / "manifest").read_text(encoding="utf-8") if (STAGE / "manifest").exists() else ""
    fields = {}
    for line in manifest.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            fields[k.strip()] = v.strip()
    for key in ("appname", "version", "display_name", "desc", "source"):
        if fields.get(key):
            ok(f"manifest.{key} = {fields[key][:40]}")
        else:
            bad(f"manifest 缺少必填字段 {key}")
    if not fields.get("desktop_applaunchname", "").startswith(fields.get("appname", "\0")):
        bad("desktop_applaunchname 必须以 appname 开头")

    # 桌面入口键名要与 desktop_applaunchname 对得上，网关前缀要与 appname 对得上
    try:
        ui = json.loads((STAGE / "app/ui/config").read_text(encoding="utf-8"))
        entries = ui.get(".url", {})
        launch = fields.get("desktop_applaunchname", "")
        if launch in entries:
            ok(f"桌面入口 {launch} 与 manifest 一致")
        else:
            bad(f"app/ui/config 里找不到入口 {launch}")
        for name, cfg in entries.items():
            if cfg.get("gatewaySocket"):
                if cfg.get("gatewayPrefix") == f"/app/{fields.get('appname')}":
                    ok(f"网关前缀 {cfg['gatewayPrefix']} 与 appname 一致")
                else:
                    bad(f"网关前缀 {cfg.get('gatewayPrefix')} 与 appname 不匹配")
    except ValueError as exc:
        bad(f"无法解析 app/ui/config：{exc}")

    # shell 脚本必须 LF 且不能有 BOM
    for rel in sorted((STAGE / "cmd").glob("*")) if (STAGE / "cmd").exists() else []:
        raw = rel.read_bytes()
        if b"\r\n" in raw:
            bad(f"{rel.name} 是 CRLF，Linux 下会报 bad interpreter")
        elif raw.startswith(b"\xef\xbb\xbf"):
            bad(f"{rel.name} 带 BOM")
        else:
            ok(f"{rel.name} 换行/BOM 正常")

    # Python 能编译
    server = STAGE / "app/server/folder_picker.py"
    if server.exists():
        try:
            import tempfile

            with tempfile.TemporaryDirectory() as td:
                py_compile.compile(str(server), doraise=True,
                                   cfile=str(Path(td) / "check.pyc"))
            ok("folder_picker.py 语法正常")
        except py_compile.PyCompileError as exc:
            bad(f"folder_picker.py 编译失败：{exc}")


def stage_files():
    if STAGE.exists():
        shutil.rmtree(STAGE)
    STAGE.mkdir(parents=True)
    for name in INCLUDE:
        src = PROJECT / name
        if not src.exists():
            bad(f"源目录缺少 {name}")
            continue
        dst = STAGE / name
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
    # 清掉编辑器垃圾
    for junk in list(STAGE.rglob(".DS_Store")) + list(STAGE.rglob("__pycache__")):
        if junk.is_dir():
            shutil.rmtree(junk, ignore_errors=True)
        else:
            junk.unlink(missing_ok=True)


def find_fnpack(explicit: str | None) -> str:
    if explicit:
        return explicit
    for cand in [
        PROJECT.parent / "_fnpack" / "fnpack.exe",
        PROJECT / "tools" / "fnpack.exe",
        shutil.which("fnpack") or "",
    ]:
        if cand and Path(cand).exists():
            return str(cand)
    print("找不到 fnpack，请用 --fnpack 指定路径。下载：")
    print("  https://developer.fnnas.com/docs/cli/fnpack/")
    sys.exit(2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fnpack", default=None)
    args = ap.parse_args()

    print(f"项目目录：{PROJECT}")
    stage_files()
    check_stage()

    print("\n== 打包 ==")
    fnpack = find_fnpack(args.fnpack)
    print(f"使用 fnpack：{fnpack}")
    proc = subprocess.run(
        [fnpack, "build", "--directory", str(STAGE)],
        capture_output=True, text=True, cwd=str(STAGE),
    )
    print(proc.stdout.strip() or proc.stderr.strip())
    if proc.returncode != 0:
        bad(f"fnpack build 失败（退出码 {proc.returncode}）")
        print(proc.stderr)
        return 1

    produced = sorted(STAGE.glob("*.fpk")) + sorted(PROJECT.glob("*.fpk"))
    if not produced:
        bad("没有找到生成的 .fpk")
        return 1

    DIST.mkdir(exist_ok=True)
    final = []
    for f in produced:
        target = DIST / f.name
        if f.resolve() != target.resolve():
            shutil.move(str(f), str(target))
        final.append(target)
        ok(f"产物 {target.name}（{target.stat().st_size / 1024:.1f} KB）")

    # 列出包内清单
    import tarfile

    for fpk in final:
        try:
            with tarfile.open(fpk, "r:*") as tf:
                names = sorted(n for n in tf.getnames() if not n.endswith("/"))
            print(f"\n== {fpk.name} 内含 {len(names)} 个文件 ==")
            for n in names:
                print("   ", n)
        except tarfile.TarError as exc:
            print(f"无法读取包内容（可能不是 tar 格式）：{exc}")

    print(f"\n自检通过 {len(OK)}，失败 {len(BAD)}")
    return 1 if BAD else 0


if __name__ == "__main__":
    raise SystemExit(main())
