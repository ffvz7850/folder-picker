#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件夹挑选器 (folder-picker) 后端 —— 只解压后目录太乱时，逐层看清子文件夹并挑选

设计要点
  * 纯 Python 3 标准库，零第三方依赖
  * 同时监听两个地址：
      - Unix Socket  ${TRIM_APPDEST}/app.sock   ← 飞牛统一网关（不占端口，由 fnOS 校验登录态）
      - TCP          127.0.0.1:8484             ← 仅本机调试用
  * 只做目录级操作：浏览、统计、移动、复制、导出清单
  * 所有路径都要落在允许的根目录内，禁止 ..、禁止把目录移进自己的子树

启动参数
  --socket <path>   Unix Socket 路径（可省略）
  --host <ip>       TCP 监听地址，默认 127.0.0.1（设为空字符串则不监听 TCP）
  --port <n>        TCP 端口，默认 8484
  --www <dir>       前端静态文件目录
  --var <dir>       运行数据目录（日志/作业记录）
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import posixpath
import shutil
import signal
import socketserver
import sys
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APPNAME = "folder-picker"
VERSION = "1.4.1"
GATEWAY_PREFIX = "/app/" + APPNAME

# 浏览起点/目录树顶层的「聚合层」——见 aggregate_entries()。
# 默认取每块存储空间下的 1000（飞牛第一个用户的 uid 目录），可用环境变量
# AGG_SUBDIRS 覆盖（逗号分隔，按顺序取第一个存在的）。
DEFAULT_AGG_SUBDIRS = ("1000",)
DEFAULT_DEPTH = 3

# 统计与遍历的各种上限，防止在超大目录上把 NAS 拖死
MAX_LIST_CHILDREN = 5000
MAX_TREE_NODES = 20000
# 打开「显示文件」时，单个目录最多列出多少个文件（避免大目录把页面撑爆）
TREE_MAX_FILES_PER_NODE = 300
MAX_TREE_DEPTH = 8
STATS_MAX_FILES = 200000
STATS_TIME_BUDGET = 8.0
CHILD_COUNT_LIMIT = 800  # 子项超过这个数量就不再逐个统计"里面有几个文件夹"

VIDEO_EXTS = {
    ".mkv", ".mp4", ".ts", ".m2ts", ".mts", ".avi", ".rmvb", ".rm", ".wmv",
    ".mov", ".flv", ".m4v", ".mpg", ".mpeg", ".webm", ".vob", ".trp", ".iso",
    ".m2v", ".asf", ".f4v", ".3gp", ".ogm", ".divx", ".mxf",
}


class ApiError(Exception):
    """返回给前端的可读错误"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


# ---------------------------------------------------------------------------
# 路径与权限
# ---------------------------------------------------------------------------
class PathGuard:
    def __init__(self, extra_roots: list[str]):
        self.roots = self._collect(extra_roots)
        # 比较时做一下大小写归一（Windows 上 C:\ 和 c:\ 是同一个地方）
        self._roots_key = [os.path.normcase(r) for r in self.roots]

    @staticmethod
    def _collect(extra_roots: list[str]) -> list[str]:
        raw: list[str] = []
        env = os.environ.get("TRIM_DATA_ACCESSIBLE_PATHS", "")
        raw += [p.strip() for p in env.split(os.pathsep) if p.strip()]
        # 存储空间：/vol1 ... /vol32
        for i in range(1, 33):
            raw.append(f"/vol{i}")
        raw += [p for p in extra_roots if p]

        found = []
        for p in raw:
            try:
                if os.path.isdir(p):
                    found.append(os.path.realpath(p))
            except OSError:
                continue
        # 去重，并去掉被父目录覆盖的子目录
        found = sorted(set(found), key=len)
        result: list[str] = []
        for p in found:
            if not any(p == r or p.startswith(r.rstrip("/\\") + os.sep) for r in result):
                result.append(p)
        return result

    def is_allowed(self, path: str) -> bool:
        key = os.path.normcase(os.path.realpath(path))
        return any(
            key == r or key.startswith(r.rstrip("/\\") + os.sep) for r in self._roots_key
        )

    def is_root(self, path: str) -> bool:
        """path 本身是否就是某个允许的根目录（用于判断"到顶了，再上一级是虚拟根"）"""
        key = os.path.normcase(os.path.realpath(path)).rstrip("/\\")
        return any(key == r.rstrip("/\\") for r in self._roots_key)

    def check_path(self, raw: str, require_parent: bool = True) -> str:
        """校验并归一化路径：必须是绝对路径、必须落在白名单根目录内"""
        if not raw or not str(raw).strip():
            raise ApiError("缺少路径参数")
        raw = str(raw).strip()
        if not os.path.isabs(raw):
            raise ApiError(f"路径必须是绝对路径，收到：{raw}")

        path = os.path.realpath(os.path.abspath(raw))
        if not self.is_allowed(path):
            raise ApiError(
                f"没有访问这个目录的权限：{path}"
                "（应用只能读写存储空间等授权目录，可用设置里的 EXTRA_ROOTS 追加）"
            )

        if require_parent and not os.path.isdir(os.path.dirname(path)):
            raise ApiError(f"上级目录不存在：{os.path.dirname(path)}")
        return path

    def check_dir(self, raw: str, must_exist: bool = True) -> str:
        path = self.check_path(raw, require_parent=not must_exist)
        if must_exist:
            if not os.path.exists(path):
                raise ApiError(f"路径不存在：{path}", 404)
            if not os.path.isdir(path):
                raise ApiError(f"不是文件夹：{path}")
        return path


def is_subpath(child: str, parent: str) -> bool:
    """child 是否位于 parent 之下（含相等）；用于防止把目录搬进自己的子树"""
    c = os.path.normcase(os.path.realpath(child))
    p = os.path.normcase(os.path.realpath(parent))
    if c == p:
        return True
    return c.startswith(p.rstrip("/\\") + os.sep)


def to_posix(path: str) -> str:
    return path.replace("\\", "/")


def is_hidden_name(name: str) -> bool:
    """是否属于「系统/隐藏」目录，默认不展示

    - "." 开头：常规隐藏目录
    - "@" 开头：飞牛自己的系统目录（@appdata / @appcenter / @apphome / @apptemp），
      浏览存储空间时它们是纯噪音
    """
    return name.startswith(".") or name.startswith("@")


# ---------------------------------------------------------------------------
# 目录扫描
# ---------------------------------------------------------------------------
def split_tokens(value: str) -> list[str]:
    if not value:
        return []
    parts = []
    for chunk in str(value).replace(",", " ").replace(";", " ").split():
        chunk = chunk.strip()
        if chunk:
            parts.append(chunk)
    return parts


def match_filter(name: str, tokens: list[str], excludes: list[str]) -> bool:
    low = name.lower()
    for ex in excludes:
        if low == ex.lower():
            return False
    if not tokens:
        return True
    # 多个关键词是"都要包含"的关系，便于逐步缩小范围
    return all(t.lower() in low for t in tokens)


def scan_dir_summary(path: str, excludes: list[str]) -> tuple[int, bool]:
    """返回 (可见子文件夹数, 是否真的空目录)

    "空"只表示里面什么都没有；只含文件的目录不算空——
    解压出来的正片目录通常就是这么个结构，不能隐藏它。
    """
    child_dirs = 0
    has_entry = False
    try:
        with os.scandir(path) as it:
            for entry in it:
                has_entry = True
                try:
                    if is_hidden_name(entry.name):
                        continue
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    if any(entry.name.lower() == e.lower() for e in excludes):
                        continue
                    child_dirs += 1
                except OSError:
                    continue
    except OSError:
        return 0, False
    return child_dirs, not has_entry


def virtual_root_listing(roots: list[str], excludes: list[str]) -> dict:
    """浏览起点：把应用有权访问的根目录（飞牛各存储空间等）列成一层「虚拟根」

    不能直接把 "/" 丢给用户浏览：白名单里根本没有 "/"，
    Linux 上会被判越权、Windows 上连绝对路径都不算，
    结果就是点「浏览」永远停在加载中。

    注意这里**不做"隐藏空文件夹"过滤**——存储空间列表是导航入口，
    空的盘（刚建的、或暂时没数据的）也必须列出来，否则用户会以为盘丢了。
    """
    dirs = []
    for root in roots:
        try:
            st = os.stat(root)
        except OSError:
            continue
        n, empty = scan_dir_summary(root, excludes)
        dirs.append(
            {
                "name": os.path.basename(root.rstrip("/\\")) or root,
                "path": to_posix(root),
                "mtime": int(st.st_mtime),
                "is_dir": True,
                "child_dirs": n,
                "empty": empty,
                "is_root": True,
            }
        )
    dirs.sort(key=lambda x: x["name"].lower())

    return {
        "path": "",
        "name": "存储空间",
        "parent": "",
        "virtual_root": True,
        "at_root": True,
        "dirs": dirs,
        "files": [],
        "dir_total": len(dirs),
        "file_total": 0,
        "truncated": False,
    }


# ---------------------------------------------------------------------------
# 聚合层：把各存储空间「用户数据目录」下的文件夹并成一个列表
# ---------------------------------------------------------------------------
def aggregate_subdirs() -> tuple[str, ...]:
    raw = os.environ.get("AGG_SUBDIRS", "")
    items = tuple(p.strip() for p in raw.replace(";", ",").split(",") if p.strip())
    return items or DEFAULT_AGG_SUBDIRS


def aggregate_entries(roots: list[str]) -> list[dict]:
    """把每个存储空间折算成「用户数据目录」

    飞牛把用户数据放在 <存储空间>/1000（第一个用户的 uid 目录），
    而用户根本不关心某个文件夹落在哪块盘上。所以列表和目录树都从这一层起步，
    不必先点存储空间、再点 1000。

    某块盘上没有 1000（比如用户手填的 EXTRA_ROOTS）就退回存储空间根目录本身。
    """
    subs = aggregate_subdirs()
    out: list[dict] = []
    seen: set[str] = set()
    for root in roots:
        base = root
        for sub in subs:
            cand = os.path.join(root, sub)
            try:
                if os.path.isdir(cand):
                    base = cand
                    break
            except OSError:
                continue
        key = os.path.normcase(os.path.realpath(base))
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "path": base,
                "volume": os.path.basename(root.rstrip("/\\")) or root,
                "volume_path": root,
                "mapped": os.path.normcase(base) != os.path.normcase(root),
            }
        )
    out.sort(key=lambda e: e["volume"].lower())
    return out


def aggregate_root_listing(entries: list[dict], excludes: list[str], show_empty: bool = True) -> dict:
    """聚合浏览层：各存储空间用户目录下的子文件夹并成一个列表

    同名文件夹出现在多块盘上时**不合并物理路径**——勾选必须一一对应真实目录，
    否则搬运会搬错地方。这里只把同名的排在一起，并标注它来自哪块盘、同名几处。
    """
    dirs: list[dict] = []
    truncated = False
    for ent in entries:
        base = ent["path"]
        try:
            with os.scandir(base) as it:
                children = [e for e in it if e.is_dir(follow_symlinks=False)]
        except OSError:
            continue
        for entry in children:
            if is_hidden_name(entry.name):
                continue
            if any(entry.name.lower() == x.lower() for x in excludes):
                continue
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            dirs.append(
                {
                    "name": entry.name,
                    "path": to_posix(entry.path),
                    "mtime": int(st.st_mtime),
                    "is_dir": True,
                    "volume": ent["volume"],
                    "volume_path": to_posix(ent["volume_path"]),
                    "child_dirs": None,
                    "empty": False,
                }
            )

    dirs.sort(key=lambda x: (x["name"].lower(), x["volume"].lower()))
    if len(dirs) > MAX_LIST_CHILDREN:
        dirs = dirs[:MAX_LIST_CHILDREN]
        truncated = True

    if len(dirs) <= CHILD_COUNT_LIMIT:
        for item in dirs:
            n, empty = scan_dir_summary(item["path"], excludes)
            item["child_dirs"] = n
            item["empty"] = empty

    if not show_empty:
        dirs = [d for d in dirs if not d.get("empty")]

    # 同名几处：前端据此决定要不要打「重名 ×2」的标，避免用户以为是重复项
    counter: dict[str, int] = {}
    for d in dirs:
        counter[d["name"]] = counter.get(d["name"], 0) + 1
    for d in dirs:
        d["name_dup"] = counter[d["name"]]

    return {
        "path": "",
        "name": "全部存储空间",
        "parent": "",
        "aggregate": True,
        "virtual_root": True,
        "at_root": True,
        "bases": [
            {"path": to_posix(e["path"]), "volume": e["volume"], "mapped": e["mapped"]}
            for e in entries
        ],
        "dirs": dirs,
        "files": [],
        "dir_total": len(dirs),
        "file_total": 0,
        "truncated": truncated,
    }


def list_children(
    path: str,
    show_files: bool = False,
    show_empty: bool = True,
    hide_hidden: bool = True,
    filter_text: str = "",
    exclude_text: str = "",
) -> dict:
    tokens = split_tokens(filter_text)
    excludes = split_tokens(exclude_text)

    dirs, files = [], []
    try:
        with os.scandir(path) as it:
            entries = list(it)
    except PermissionError:
        raise ApiError(f"没有读取权限：{path}（应用以 root 运行，一般不会出现）", 403)
    except OSError as exc:
        raise ApiError(f"读取目录失败：{exc}", 500)

    truncated = False
    for entry in entries:
        try:
            if hide_hidden and is_hidden_name(entry.name):
                continue
            if not match_filter(entry.name, tokens, excludes):
                continue
            is_dir = entry.is_dir(follow_symlinks=False)
            if not is_dir and not show_files:
                continue
            st = entry.stat(follow_symlinks=False)
            item = {
                "name": entry.name,
                "path": to_posix(entry.path),
                "mtime": int(st.st_mtime),
                "is_dir": is_dir,
            }
            if is_dir:
                item["child_dirs"] = None
                item["empty"] = False
            else:
                item["size"] = st.st_size
                item["is_video"] = os.path.splitext(entry.name)[1].lower() in VIDEO_EXTS
            (dirs if is_dir else files).append(item)
        except OSError:
            continue

    dirs.sort(key=lambda x: x["name"].lower())
    files.sort(key=lambda x: x["name"].lower())

    if len(dirs) > MAX_LIST_CHILDREN:
        dirs = dirs[:MAX_LIST_CHILDREN]
        truncated = True

    # 数量不多时才去统计每个子目录里的情况，避免大目录卡顿
    if len(dirs) <= CHILD_COUNT_LIMIT:
        for item in dirs:
            n, empty = scan_dir_summary(item["path"], excludes)
            item["child_dirs"] = n
            item["empty"] = empty

    if not show_empty:
        dirs = [d for d in dirs if not d.get("empty")]

    parent = os.path.dirname(path)
    parent = parent if parent and parent != path else ""

    return {
        "path": to_posix(path),
        "name": os.path.basename(path) or path,
        "parent": to_posix(parent),
        "dirs": dirs,
        "files": files,
        "dir_total": len(dirs),
        "file_total": len(files) if show_files else 0,
        "truncated": truncated,
    }


def build_tree(
    path: str,
    depth: int,
    hide_hidden: bool,
    show_empty: bool,
    filter_text: str,
    exclude_text: str,
    budget: list[int],
    show_files: bool = False,
) -> dict:
    """构建只含目录的树

    两条关键语义（都是踩过坑之后定下来的）：
      * empty 指"这个目录里真的什么都没有"（既没子文件夹也没文件）。
        绝不能把"只有视频文件、没有子文件夹"的目录当成空的——正片目录往往就是这样，
        隐藏它会让用户找不到要勾选的目标。
      * 带名称筛选时，保留的是"命中分支的整条路径"：
        例如筛选 S01，父级 乱七八糟 / 包A 也要显示出来，否则用户看不到层级。
      * show_files 只影响"这一层挂了哪些文件"，文件不会成为可勾选节点，
        也不会参与按层全选——搬运的最小单位始终是文件夹。
    """
    tokens = split_tokens(filter_text)
    excludes = split_tokens(exclude_text)

    def is_hidden(name: str) -> bool:
        return hide_hidden and is_hidden_name(name)

    def is_excluded(name: str) -> bool:
        return bool(excludes) and any(name.lower() == e.lower() for e in excludes)

    def name_match(name: str) -> bool:
        return (not tokens) or all(t.lower() in name.lower() for t in tokens)

    node = {
        "name": os.path.basename(path) or path,
        "path": to_posix(path),
        "children": [],
        "truncated": False,
        "empty": False,
        "has_subdirs": False,
        "matched": False,
        "files": [],
        "files_truncated": False,
    }

    if depth <= 0 or budget[0] <= 0:
        # 层数用尽，只做一次轻量探测，不能当空目录（否则父级整条分支会被过滤掉）
        has_entry = False
        try:
            with os.scandir(path) as it:
                for entry in it:
                    has_entry = True
                    if entry.is_dir(follow_symlinks=False) \
                            and not is_hidden(entry.name) and not is_excluded(entry.name):
                        node["has_subdirs"] = True
                        break
        except OSError:
            pass
        node["empty"] = not has_entry
        node["matched"] = name_match(node["name"])
        return node

    subdirs = []
    files = []
    has_file = False
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if not is_hidden(entry.name) and not is_excluded(entry.name):
                            subdirs.append(entry)
                        continue
                    has_file = True
                    if not show_files:
                        continue
                    if is_hidden(entry.name) or is_excluded(entry.name):
                        continue
                    if not name_match(entry.name):
                        continue
                    if len(files) < TREE_MAX_FILES_PER_NODE:
                        files.append(entry)
                    else:
                        node["files_truncated"] = True
                except OSError:
                    continue
    except OSError:
        pass

    subdirs.sort(key=lambda e: e.name.lower())
    node["has_subdirs"] = bool(subdirs)
    node["empty"] = not subdirs and not has_file
    node["matched"] = name_match(node["name"])

    if files:
        rows = []
        for entry in sorted(files, key=lambda e: e.name.lower()):
            try:
                st = entry.stat(follow_symlinks=False)
                size = st.st_size
            except OSError:
                size = 0
            rows.append(
                {
                    "name": entry.name,
                    "path": to_posix(entry.path),
                    "size": size,
                    "is_video": os.path.splitext(entry.name)[1].lower() in VIDEO_EXTS,
                }
            )
        node["files"] = rows

    for entry in subdirs:
        if budget[0] <= 0:
            node["truncated"] = True
            break
        budget[0] -= 1
        child = build_tree(
            entry.path,
            depth - 1,
            hide_hidden,
            show_empty,
            filter_text,
            exclude_text,
            budget,
            show_files=show_files,
        )
        if child["matched"]:
            node["matched"] = True
        keep = child["matched"] if tokens else (show_empty or not child["empty"])
        if keep:
            node["children"].append(child)
    return node


def build_aggregate_tree(
    entries: list[dict],
    depth: int,
    hide_hidden: bool,
    show_empty: bool,
    filter_text: str,
    exclude_text: str,
    budget: list[int],
    show_files: bool = False,
) -> dict:
    """聚合根下的目录树：顶层直接是各存储空间用户目录里的文件夹

    虚拟根（"全部存储空间"）占第 0 层，depth 从第 1 层（聚合项本身）开始算，
    这样"展开层数 = 3"跟用户看到的最深层级是一致的。
    同名文件夹各自成节点（path 必须是真实目录），只是排在一起并带上 volume 标记。
    """
    tokens = split_tokens(filter_text)
    excludes = split_tokens(exclude_text)

    root = {
        "name": "全部存储空间",
        "path": "",
        "aggregate": True,
        "virtual_root": True,
        "children": [],
        "truncated": False,
        "empty": False,
        "has_subdirs": False,
        "matched": True,
        "files": [],
        "files_truncated": False,
    }

    for ent in entries:
        try:
            with os.scandir(ent["path"]) as it:
                children = [e for e in it if e.is_dir(follow_symlinks=False)]
        except OSError:
            continue
        for entry in sorted(children, key=lambda e: e.name.lower()):
            if hide_hidden and is_hidden_name(entry.name):
                continue
            if excludes and any(entry.name.lower() == x.lower() for x in excludes):
                continue
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue

            if budget[0] <= 0:
                root["truncated"] = True
                break
            budget[0] -= 1
            child = build_tree(
                entry.path,
                depth,
                hide_hidden,
                show_empty,
                filter_text,
                exclude_text,
                budget,
                show_files=show_files,
            )
            child["volume"] = ent["volume"]
            child["volume_path"] = to_posix(ent["volume_path"])
            if tokens:
                # 名称筛选下保留"命中分支的整条路径"
                keep = child["matched"] or bool(child["children"])
            else:
                keep = show_empty or not child["empty"]
            if keep:
                root["children"].append(child)
        if root["truncated"]:
            break

    root["children"].sort(key=lambda c: (c["name"].lower(), c.get("volume", "").lower()))
    root["has_subdirs"] = bool(root["children"])
    # 同名几处，供前端提示
    counter: dict[str, int] = {}
    for c in root["children"]:
        counter[c["name"]] = counter.get(c["name"], 0) + 1
    for c in root["children"]:
        c["name_dup"] = counter[c["name"]]
    return root


def compute_stats(paths: list[str], guard: PathGuard) -> dict:
    started = time.time()
    total_files = total_dirs = total_bytes = videos = 0
    partial = False
    per_path = []

    for raw in paths:
        path = guard.check_dir(raw)
        p_files = p_dirs = p_bytes = 0
        p_videos = 0
        stack = [path]
        while stack:
            cur = stack.pop()
            try:
                with os.scandir(cur) as it:
                    for entry in it:
                        if total_files >= STATS_MAX_FILES:
                            partial = True
                            break
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                p_dirs += 1
                                stack.append(entry.path)
                            else:
                                st = entry.stat(follow_symlinks=False)
                                p_files += 1
                                p_bytes += st.st_size
                                if os.path.splitext(entry.name)[1].lower() in VIDEO_EXTS:
                                    p_videos += 1
                        except OSError:
                            continue
            except OSError:
                partial = True
            if partial or time.time() - started > STATS_TIME_BUDGET:
                partial = True
                break

        total_files += p_files
        total_dirs += p_dirs
        total_bytes += p_bytes
        videos += p_videos
        per_path.append(
            {
                "path": to_posix(path),
                "name": os.path.basename(path) or path,
                "files": p_files,
                "dirs": p_dirs,
                "bytes": p_bytes,
                "videos": p_videos,
            }
        )
        if partial:
            break

    return {
        "files": total_files,
        "dirs": total_dirs,
        "bytes": total_bytes,
        "videos": videos,
        "items": per_path,
        "partial": partial,
    }


# ---------------------------------------------------------------------------
# 传输任务
# ---------------------------------------------------------------------------
class Cancelled(Exception):
    pass


def iter_files(src: str):
    """深度优先遍历，产出 (相对路径, 绝对路径, 大小)"""
    stack = [src]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            rel = os.path.relpath(entry.path, src)
                            yield rel, entry.path, entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue


def resolve_conflict(target: str, conflict: str, dry_run: bool = False) -> str | None:
    """返回实际可用的目标路径；None 表示跳过。dry_run 时绝不删任何东西"""
    if not os.path.exists(target):
        return target
    if conflict == "skip":
        return None
    if conflict == "overwrite":
        if dry_run:
            return target
        if os.path.isdir(target) and not os.path.islink(target):
            shutil.rmtree(target)
        else:
            os.remove(target)
        return target

    parent = os.path.dirname(target)
    base = os.path.basename(target)
    # 目录名里的点不是扩展名：ShowA.S01 要变成 "ShowA.S01 (2)"，不能变成 "ShowA (2).S01"
    if os.path.isdir(target) and not os.path.islink(target):
        stem, ext = base, ""
    else:
        stem, ext = os.path.splitext(base)
    i = 2
    while True:
        cand = os.path.join(parent, f"{stem} ({i}){ext}")
        if not os.path.exists(cand):
            return cand
        i += 1
        if i > 9999:
            raise ApiError(f"无法为 {target} 找到可用的新名字")


class Job:
    def __init__(self, op: str, sources: list[str], dest: str, options: dict):
        self.id = f"job{int(time.time() * 1000)}"
        self.op = op  # move | copy | plan
        self.sources = sources
        self.dest = dest
        self.options = options
        self.state = "pending"
        self.message = ""
        self.current = ""
        self.total_files = 0
        self.total_bytes = 0
        self.done_files = 0
        self.done_bytes = 0
        self.errors: list[str] = []
        self.items: list[dict] = []
        self.started_at = time.time()
        self.finished_at = 0.0
        self._cancel = threading.Event()
        self._lock = threading.Lock()

    def cancel(self):
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def snapshot(self) -> dict:
        with self._lock:
            percent = 0.0
            if self.total_bytes > 0:
                percent = min(99.9, self.done_bytes * 100.0 / self.total_bytes)
            elif self.total_files > 0:
                percent = min(99.9, self.done_files * 100.0 / self.total_files)
            if self.state in ("done", "error", "cancelled"):
                percent = 100.0
            return {
                "id": self.id,
                "op": self.op,
                "state": self.state,
                "message": self.message,
                "current": self.current,
                "total_files": self.total_files,
                "done_files": self.done_files,
                "total_bytes": self.total_bytes,
                "done_bytes": self.done_bytes,
                "percent": round(percent, 1),
                "errors": self.errors[:50],
                "items": self.items,
                "elapsed": round((self.finished_at or time.time()) - self.started_at, 1),
            }

    # -- 内部 ---------------------------------------------------------------
    def _tick(self, size: int):
        if self.cancelled:
            raise Cancelled()
        with self._lock:
            self.done_files += 1
            self.done_bytes += size

    def _prepare(self):
        """预统计总文件数与总字节数，用于进度条"""
        for src in self.sources:
            for _, _, size in iter_files(src):
                self.total_files += 1
                self.total_bytes += size
                if self._cancel.is_set():
                    return

    def run(self, guard: PathGuard):
        try:
            self.state = "running"
            self._prepare()
            if self.cancelled:
                raise Cancelled()
            self._execute(guard)
            self.state = "done"
            self.message = f"完成：{self.done_files} 个文件 / {human(self.done_bytes)}"
        except Cancelled:
            self.state = "cancelled"
            self.message = "已取消（已完成的部分不会回滚）"
        except ApiError as exc:
            self.state = "error"
            self.message = exc.message
        except Exception as exc:  # noqa: BLE001
            self.state = "error"
            self.message = f"执行失败：{exc}"
            self.errors.append(traceback.format_exc(limit=3))
        finally:
            self.finished_at = time.time()

    def _execute(self, guard: PathGuard):
        dest_root = self.dest
        root = self.options.get("root") or ""
        keep_structure = bool(self.options.get("keep_structure"))
        conflict = self.options.get("conflict", "rename")
        dry_run = self.op == "plan"

        if not dry_run:
            os.makedirs(dest_root, exist_ok=True)

        for src in self.sources:
            name = os.path.basename(src) or src
            if keep_structure and root and os.path.realpath(root) not in (
                os.path.realpath(src),
            ):
                rel = os.path.relpath(src, root)
                if not rel.startswith(".."):
                    target = os.path.join(dest_root, rel)
                else:
                    target = os.path.join(dest_root, name)
            else:
                target = os.path.join(dest_root, name)

            if is_subpath(dest_root, src):
                self.errors.append(f"跳过 {name}：目标目录位于其自身内部")
                self.items.append({"source": src, "target": target, "status": "skipped",
                                   "reason": "目标在源内部"})
                continue

            existing = os.path.exists(target)
            final = resolve_conflict(target, conflict, dry_run) if existing else target
            if final is None:
                self.items.append({"source": src, "target": target, "status": "skipped",
                                   "reason": "目标已存在（按跳过处理）"})
                continue

            if dry_run:
                self.items.append(
                    {
                        "source": to_posix(src),
                        "target": to_posix(final),
                        "status": "planned",
                        "reason": "目标已存在，将重命名" if existing else "",
                    }
                )
                continue

            os.makedirs(os.path.dirname(final) or dest_root, exist_ok=True)
            self.current = name

            same_dev = False
            try:
                same_dev = os.stat(src).st_dev == os.stat(dest_root).st_dev
            except OSError:
                same_dev = False

            if self.op == "move" and same_dev:
                # 同一文件系统内直接改名，瞬间完成
                count = 0
                size = 0
                for _, _, s in iter_files(src):
                    count += 1
                    size += s
                    if self.cancelled:
                        raise Cancelled()
                os.rename(src, final)
                with self._lock:
                    self.done_files += count
                    self.done_bytes += size
            else:
                self._copy_tree(src, final)
                if self.op == "move":
                    shutil.rmtree(src, ignore_errors=True)

            self.items.append(
                {
                    "source": to_posix(src),
                    "target": to_posix(final),
                    "status": "done",
                    "renamed": bool(existing and final != target),
                }
            )

    def _copy_tree(self, src: str, dst: str):
        os.makedirs(dst, exist_ok=True)
        for dirpath, dirnames, filenames in os.walk(src):
            if self.cancelled:
                raise Cancelled()
            rel = os.path.relpath(dirpath, src)
            cur = dst if rel == "." else os.path.join(dst, rel)
            os.makedirs(cur, exist_ok=True)
            for fn in filenames:
                if self.cancelled:
                    raise Cancelled()
                s = os.path.join(dirpath, fn)
                d = os.path.join(cur, fn)
                size = 0
                try:
                    size = os.stat(s, follow_symlinks=False).st_size
                    shutil.copy2(s, d)
                except OSError as exc:
                    self.errors.append(f"{s}：{exc}")
                self._tick(size)


def human(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    v = float(n)
    for u in units:
        if v < 1024 or u == units[-1]:
            return f"{v:.1f} {u}" if u != "B" else f"{int(v)} B"
        v /= 1024
    return f"{v:.1f} TB"


# ---------------------------------------------------------------------------
# HTTP 处理
# ---------------------------------------------------------------------------
JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    server_version = f"FolderPicker/{VERSION}"
    protocol_version = "HTTP/1.1"

    # 由 main() 注入
    guard: PathGuard = None  # type: ignore
    www_dir = ""
    var_dir = ""
    auth_token = ""

    @property
    def mode(self) -> str:
        """当前请求来自统一网关还是 TCP 直连（决定是否需要额外令牌校验）"""
        return getattr(self.server, "kind", "tcp")

    # ------------------------------ 工具 ------------------------------
    def log_message(self, fmt, *args):  # noqa: A003
        sys.stdout.write(
            "%s - %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), fmt % args)
        )
        sys.stdout.flush()

    def _route_path(self) -> tuple[str, dict]:
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)

        # 统一网关会把 /app/folder-picker 前缀带进来，这里剥掉。
        # 不同网关版本在大小写、结尾斜杠、重复斜杠上可能不一致，
        # 一律宽容处理——前端只要有一个字符对不上，表现就是满屏 404。
        lower = path.lower()
        prefix = GATEWAY_PREFIX.lower()
        stripped = False
        if lower == prefix:
            path = "/"
            stripped = True
        elif lower.startswith(prefix + "/"):
            path = path[len(GATEWAY_PREFIX):]
            stripped = True

        # 兜底：万一网关多套了一层目录导致前缀没被识别，
        # 只要路径里出现 /api/ 就按接口处理，别让网关差异变成 404。
        if not stripped and not path.startswith("/api/"):
            idx = path.find("/api/")
            if idx > 0:
                path = path[idx:]

        # 折叠重复斜杠（/api//info、/app/folder-picker// 之类）
        while "//" in path:
            path = path.replace("//", "/")
        return path, urllib.parse.parse_qs(parsed.query)

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _fail(self, message: str, status: int = 400):
        self._json({"ok": False, "error": message}, status)

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        # 先看声明再读：不能把超大请求体先整个吞进内存再判断
        if length > 4 * 1024 * 1024:
            raise ApiError("请求体过大")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            raise ApiError("请求体不是合法 JSON")

    def _authorize(self) -> bool:
        """统一网关已经完成登录校验；TCP 直连时按需校验令牌"""
        if self.mode == "gateway":
            return True
        if not self.auth_token:
            return True
        supplied = self.headers.get("X-Auth-Token", "")
        if not supplied:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            supplied = (qs.get("token") or [""])[0]
        return supplied == self.auth_token

    def _whoami(self) -> dict:
        return {
            "uid": self.headers.get("X-Trim-Userid", ""),
            "username": self.headers.get("X-Trim-Username", ""),
            "is_admin": self.headers.get("X-Trim-Isadmin", "") == "true",
        }

    # ------------------------------ 路由 ------------------------------
    def do_GET(self):  # noqa: N802
        self._dispatch("GET")

    def do_HEAD(self):  # noqa: N802
        self._dispatch("GET")

    def do_POST(self):  # noqa: N802
        self._dispatch("POST")

    def _dispatch(self, method: str):
        try:
            path, qs = self._route_path()
            if not self._authorize():
                return self._fail("未授权：缺少或错误的访问令牌", 401)

            if method == "POST":
                return self._handle_post(path, qs)
            return self._handle_get(path, qs)
        except ApiError as exc:
            self._fail(exc.message, exc.status)
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001
            self.log_message("未处理异常: %s", exc)
            self._fail(f"服务内部错误：{exc}", 500)

    # ------------------------------ GET ------------------------------
    def _handle_get(self, path: str, qs: dict):
        if path.startswith("/api/"):
            return self._api_get(path, qs)

        # 静态文件
        rel = path.lstrip("/") or "index.html"
        full = os.path.realpath(os.path.join(self.www_dir, rel))
        if not (full == os.path.realpath(self.www_dir) or is_subpath(full, self.www_dir)):
            return self._fail("路径非法", 403)
        if os.path.isdir(full):
            full = os.path.join(full, "index.html")
        if not os.path.isfile(full):
            if rel == "favicon.ico":
                # 浏览器会顺手要图标，别在日志里刷一片红色 404
                return self._send(204, b"", "image/x-icon")
            # 记下原始请求路径：以后再有"打不开"，看日志一眼就知道谁在请求什么
            self.log_message("静态资源未找到: rel=%s raw=%s", rel, urllib.parse.unquote(self.path))
            return self._fail(f"未找到：{rel}", 404)

        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".png": "image/png",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
        }.get(os.path.splitext(full)[1].lower(), "application/octet-stream")
        with open(full, "rb") as fh:
            self._send(200, fh.read(), ctype)

    def _api_get(self, path: str, qs: dict):
        if path == "/api/info":
            agg = aggregate_entries(self.guard.roots)
            return self._json(
                {
                    "ok": True,
                    "app": APPNAME,
                    "version": VERSION,
                    "mode": self.mode,
                    "user": self._whoami(),
                    "roots": [to_posix(r) for r in self.guard.roots],
                    "default_source": os.environ.get("DEFAULT_SOURCE", ""),
                    "default_dest": os.environ.get("DEFAULT_DEST", ""),
                    "default_depth": DEFAULT_DEPTH,
                    "aggregate": True,
                    "aggregate_subdirs": list(aggregate_subdirs()),
                    "agg_dirs": [
                        {
                            "path": to_posix(e["path"]),
                            "volume": e["volume"],
                            "mapped": e["mapped"],
                        }
                        for e in agg
                    ],
                    "limits": {
                        "max_tree_nodes": MAX_TREE_NODES,
                        "max_tree_depth": MAX_TREE_DEPTH,
                    },
                }
            )

        if path == "/api/list":
            base = (self._arg(qs, "path") or "").strip()
            show_empty = True if self._flag(qs, "show_empty") else False
            excludes = split_tokens(self._arg(qs, "exclude"))
            # 空路径 = 聚合层：各存储空间用户目录（/volN/1000）下的文件夹并成一个列表。
            # 用户不必先点存储空间、再点 1000，上来就是他要挑的那些文件夹。
            if base in ("", "\\"):
                return self._json(
                    {
                        "ok": True,
                        **aggregate_root_listing(
                            aggregate_entries(self.guard.roots), excludes, show_empty=show_empty
                        ),
                    }
                )
            # "/" = 按存储空间浏览（退回老的虚拟根），"浏览"弹窗里的备用入口
            if base == "/":
                data = virtual_root_listing(self.guard.roots, excludes)
                return self._json({"ok": True, **data})
            target = self.guard.check_dir(base)
            data = list_children(
                target,
                show_files=self._flag(qs, "show_files"),
                show_empty=show_empty,
                hide_hidden=True if self._flag(qs, "hide_hidden", True) else False,
                filter_text=self._arg(qs, "filter"),
                exclude_text=self._arg(qs, "exclude"),
            )
            # 告诉前端"这一层就是能访问的最上一层"，再往上返回虚拟根
            data["at_root"] = self.guard.is_root(target)
            return self._json({"ok": True, **data})

        if path == "/api/tree":
            base = self._arg(qs, "path")
            try:
                depth = int(self._arg(qs, "depth") or DEFAULT_DEPTH)
            except ValueError:
                depth = DEFAULT_DEPTH
            depth = max(1, min(MAX_TREE_DEPTH, depth))
            budget = [MAX_TREE_NODES]
            common = {
                "hide_hidden": True if self._flag(qs, "hide_hidden", True) else False,
                "show_empty": self._flag(qs, "show_empty"),
                "filter_text": self._arg(qs, "filter"),
                "exclude_text": self._arg(qs, "exclude"),
                "show_files": self._flag(qs, "show_files"),
            }
            # 空路径 = 聚合树：顶层直接是各存储空间用户目录里的文件夹
            if not (base or "").strip():
                tree = build_aggregate_tree(
                    aggregate_entries(self.guard.roots), depth, budget=budget, **common
                )
                return self._json(
                    {
                        "ok": True,
                        "tree": tree,
                        "aggregate": True,
                        "remaining_budget": budget[0],
                    }
                )
            target = self.guard.check_dir(base)
            tree = build_tree(target, depth, budget=budget, **common)
            return self._json(
                {"ok": True, "tree": tree, "remaining_budget": budget[0]}
            )

        if path == "/api/job":
            job_id = self._arg(qs, "id")
            job = JOBS.get(job_id)
            if not job:
                raise ApiError("任务不存在", 404)
            return self._json({"ok": True, "job": job.snapshot()})

        if path == "/api/jobs":
            with JOBS_LOCK:
                items = [j.snapshot() for j in list(JOBS.values())[-20:]]
            return self._json({"ok": True, "jobs": items})

        self.log_message("未知接口: %s", path)
        raise ApiError(f"未知接口：{path}", 404)

    @staticmethod
    def _arg(qs: dict, key: str, default: str = "") -> str:
        vals = qs.get(key)
        return vals[0] if vals else default

    @staticmethod
    def _flag(qs: dict, key: str, default: bool = False) -> bool:
        vals = qs.get(key)
        if not vals:
            return default
        return vals[0].lower() in ("1", "true", "yes", "on")

    # ------------------------------ POST ------------------------------
    def _handle_post(self, path: str, qs: dict):
        data = self._body()

        if path == "/api/stats":
            paths = data.get("paths") or []
            if not paths:
                raise ApiError("没有选择任何文件夹")
            if len(paths) > 500:
                raise ApiError("一次最多统计 500 个文件夹")
            return self._json({"ok": True, "stats": compute_stats(paths, self.guard)})

        if path in ("/api/plan", "/api/job"):
            sources = self._normalize_sources(data.get("sources") or [])
            requested = data.get("op") or "copy"
            if requested == "plan":
                # 兼容把「预演」当成操作名传过来的客户端：预演不改变搬运动作本身
                requested = "copy"
            if requested not in ("copy", "move"):
                raise ApiError(f"不支持的操作（只支持 move / copy）：{requested}")
            # plan 是"预演"，用同一套代码跑一遍但不落盘
            op = "plan" if path == "/api/plan" else requested

            dest_raw = data.get("dest") or ""
            if not dest_raw:
                raise ApiError("请先选择目标目录")
            dest = self.guard.check_dir(dest_raw, must_exist=False)

            for src in sources:
                if is_subpath(src, dest):
                    raise ApiError(f"「{os.path.basename(src)}」已位于目标目录内，无需操作")
                if is_subpath(dest, src):
                    raise ApiError(f"目标目录不能放在「{os.path.basename(src)}」里面")

            options = {
                "root": data.get("root") or "",
                "keep_structure": bool(data.get("keep_structure")),
                "conflict": data.get("conflict") or "rename",
                "planned_op": requested,
            }
            if options["conflict"] not in ("rename", "skip", "overwrite"):
                options["conflict"] = "rename"

            job = Job(op, sources, dest, options)
            with JOBS_LOCK:
                JOBS[job.id] = job
                if len(JOBS) > 100:
                    for old in sorted(JOBS.values(), key=lambda j: j.started_at)[:20]:
                        JOBS.pop(old.id, None)

            if path == "/api/plan":
                # 预演：同步执行，直接给结果，不动文件
                job.run(self.guard)
                return self._json({"ok": True, "job": job.snapshot()})

            threading.Thread(target=job.run, args=(self.guard,), daemon=True).start()
            return self._json({"ok": True, "id": job.id, "job": job.snapshot()})

        if path == "/api/job/cancel":
            job = JOBS.get(data.get("id") or "")
            if not job:
                raise ApiError("任务不存在", 404)
            job.cancel()
            return self._json({"ok": True, "job": job.snapshot()})

        if path == "/api/mkdir":
            # 允许一次建多层目录，所以不要求上级已存在
            target = self.guard.check_path(data.get("path") or "", require_parent=False)
            try:
                os.makedirs(target, exist_ok=True)
            except OSError as exc:
                raise ApiError(f"创建目录失败：{exc}")
            return self._json({"ok": True, "path": to_posix(target)})

        if path == "/api/export":
            paths = data.get("paths") or []
            if not paths:
                raise ApiError("没有选择任何文件夹")
            fmt = (data.get("format") or "txt").lower()
            for p in paths:
                self.guard.check_dir(p)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            if fmt == "csv":
                buf = io.StringIO()
                writer = csv.writer(buf)
                writer.writerow(["文件夹名", "完整路径"])
                for p in paths:
                    writer.writerow([os.path.basename(p), to_posix(p)])
                content, name, ctype = buf.getvalue(), f"folder-picker-{stamp}.csv", "text/csv"
            elif fmt == "json":
                content = json.dumps(
                    [{"name": os.path.basename(p), "path": to_posix(p)} for p in paths],
                    ensure_ascii=False,
                    indent=2,
                )
                name, ctype = f"folder-picker-{stamp}.json", "application/json"
            elif fmt == "bash":
                lines = ["#!/bin/bash", "# 由文件夹挑选器导出：勾选的文件夹清单", ""]
                for p in paths:
                    lines.append(f'echo "{to_posix(p)}"')
                content, name, ctype = "\n".join(lines) + "\n", f"folder-picker-{stamp}.sh", "text/x-shellscript"
            else:
                content = "\n".join(to_posix(p) for p in paths) + "\n"
                name, ctype = f"folder-picker-{stamp}.txt", "text/plain"
            return self._json({"ok": True, "filename": name, "content": content, "mime": ctype})

        self.log_message("未知接口(POST): %s", path)
        raise ApiError(f"未知接口：{path}", 404)

    def _normalize_sources(self, raw: list) -> list[str]:
        if not raw:
            raise ApiError("没有选择任何文件夹")
        cleaned: list[str] = []
        for item in raw:
            cleaned.append(self.guard.check_dir(item))
        if len(cleaned) > 2000:
            raise ApiError("一次最多处理 2000 个文件夹")

        # 去掉重复项；若同时选了父目录和它的子孙目录，只保留父目录。
        # realpath/normcase 每条只算一次再复用——勾了两千个文件夹时，
        # 原来的写法是 O(n²) 次系统调用，在大目录上会卡住接口。
        uniq = sorted(set(cleaned), key=lambda p: (len(p), p))
        keys = [os.path.normcase(os.path.realpath(p)) for p in uniq]
        result: list[str] = []
        result_keys: list[str] = []
        for p, key in zip(uniq, keys):
            covered = any(
                key == rk or key.startswith(rk.rstrip("/\\") + os.sep)
                for rk in result_keys
            )
            if covered:
                continue
            result.append(p)
            result_keys.append(key)
        return result


# ---------------------------------------------------------------------------
# 服务器
# ---------------------------------------------------------------------------
if hasattr(socketserver, "UnixStreamServer"):
    # 统一网关用 Unix Socket 转发；Windows 上没有该实现，只在开发机测试时才走 TCP
    class ThreadingUnixHTTPServer(
        socketserver.ThreadingMixIn, socketserver.UnixStreamServer  # type: ignore[attr-defined]
    ):
        daemon_threads = True
        allow_reuse_address = True
        request_queue_size = 128
        kind = "gateway"  # 来自统一网关，登录态已由 fnOS 校验
else:  # pragma: no cover - 仅 Windows 开发环境
    ThreadingUnixHTTPServer = None  # type: ignore[assignment]


def make_unix_server(sock_path: str, handler):
    if os.path.exists(sock_path):
        try:
            os.unlink(sock_path)
        except OSError:
            pass
    server = ThreadingUnixHTTPServer(sock_path, handler)
    try:
        os.chmod(sock_path, 0o777)
    except OSError:
        pass
    return server


def main() -> int:
    ap = argparse.ArgumentParser(description="文件夹挑选器后端")
    ap.add_argument("--socket", default="", help="Unix Socket 路径（统一网关用）")
    ap.add_argument("--host", default="127.0.0.1", help="TCP 监听地址，空字符串表示不监听")
    ap.add_argument("--port", type=int, default=8484, help="TCP 端口")
    ap.add_argument("--www", default="", help="前端静态目录")
    ap.add_argument("--var", default="", help="运行数据目录")
    ap.add_argument(
        "--root",
        action="append",
        default=[],
        help="额外允许访问的根目录（可重复指定，主要用于本机调试）",
    )
    args = ap.parse_args()

    handler = Handler
    handler.www_dir = os.path.realpath(args.www) if args.www else os.path.realpath(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "www")
    )
    handler.var_dir = os.path.realpath(args.var) if args.var else "/tmp"
    handler.auth_token = os.environ.get("AUTH_TOKEN", "").strip()

    extra = [p.strip() for p in os.environ.get("EXTRA_ROOTS", "").split(os.pathsep) if p.strip()]
    user_roots = [p.strip() for p in os.environ.get("USER_ROOTS", "").split(os.pathsep) if p.strip()]
    handler.guard = PathGuard(extra + user_roots + list(args.root or []))

    servers = []
    sockets: list[str] = []

    if args.socket:
        if ThreadingUnixHTTPServer is None:
            print(
                f"当前系统不支持 Unix Socket，跳过网关模式（{args.socket}）",
                file=sys.stderr,
            )
        else:
            try:
                unix_server = make_unix_server(args.socket, handler)
                servers.append(unix_server)
                sockets.append(args.socket)
                print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 统一网关 Socket: {args.socket}")
            except Exception as exc:  # noqa: BLE001
                print(f"无法创建 Unix Socket {args.socket}：{exc}", file=sys.stderr)

    if args.host:
        # 调试用的 TCP 监听失败（端口被占用等）绝不能影响统一网关这条主通道
        try:
            tcp_server = ThreadingHTTPServer((args.host, args.port), handler)
            tcp_server.daemon_threads = True
            tcp_server.kind = "tcp"  # type: ignore[attr-defined]
            servers.append(tcp_server)
            host, real_port = tcp_server.server_address[0], tcp_server.server_address[1]
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] TCP 调试监听: {host}:{real_port}")
        except OSError as exc:
            print(
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] TCP 调试端口 "
                f"{args.host}:{args.port} 不可用（{exc}），仅使用统一网关 Socket",
                file=sys.stderr,
            )

    if not servers:
        print("没有可用的监听地址，退出", file=sys.stderr)
        return 2

    print(f"允许的根目录: {', '.join(handler.guard.roots) or '(无)'}")
    print(f"前端目录: {handler.www_dir}")

    def shutdown(signum, frame):  # noqa: ARG001
        print(f"收到信号 {signum}，正在退出 ...")
        for srv in servers:
            threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    threads = []
    for srv in servers:
        t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True)
        t.start()
        threads.append(t)

    try:
        while any(t.is_alive() for t in threads):
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        for srv in servers:
            try:
                srv.server_close()
            except OSError:
                pass
        for path in sockets:
            if os.path.exists(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
