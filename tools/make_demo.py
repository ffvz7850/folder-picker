#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成一份「演示版」单文件页面：界面与正式版完全一致，但后端是页面内假数据。
用途：不用装到 NAS 上就能先看界面长什么样、交互是否符合预期。

    python3 tools/make_demo.py     →  preview/folder-picker-demo.html
"""

from __future__ import annotations

import json
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
SRC = PROJECT / "app" / "www" / "index.html"
OUT = PROJECT / "preview" / "folder-picker-demo.html"

VERSION = "1.4.1"
VOL1 = "/vol1/1000"
VOL2 = "/vol2/1000"
DEMO_DEST = "/vol1/1000/media/剧集"


def P(*parts: str) -> str:
    return "/".join([VOL1, *parts])


def Q(*parts: str) -> str:
    return "/".join([VOL2, *parts])


def leaf(name: str, path: str, volume: str, dup: int = 1) -> dict:
    return {
        "name": name, "path": path, "children": [], "empty": False,
        "has_subdirs": False, "matched": False, "truncated": False,
        "volume": volume, "name_dup": dup,
    }


# 聚合根：/vol1/1000 与 /vol2/1000 下的文件夹并成一个列表（同名各自独立）
DEMO_TREE = {
    "name": "全部存储空间",
    "path": "",
    "aggregate": True,
    "virtual_root": True,
    "children": [
        {
            "name": "AnotherShow.2024.2160p",
            "path": P("AnotherShow.2024.2160p"),
            "volume": "vol1", "volume_path": "/vol1", "name_dup": 1,
            "children": [
                leaf("正片", P("AnotherShow.2024.2160p", "正片"), "vol1"),
                leaf("花絮", P("AnotherShow.2024.2160p", "花絮"), "vol1"),
            ],
            "empty": False, "has_subdirs": True, "matched": False, "truncated": False,
        },
        {
            "name": "Movie.2023.1080p.BluRay.x264",
            "path": P("Movie.2023.1080p.BluRay.x264"),
            "volume": "vol1", "volume_path": "/vol1", "name_dup": 2,
            "children": [], "empty": False, "has_subdirs": False, "matched": False,
            "truncated": False,
        },
        {
            "name": "Movie.2023.1080p.BluRay.x264",
            "path": Q("Movie.2023.1080p.BluRay.x264"),
            "volume": "vol2", "volume_path": "/vol2", "name_dup": 2,
            "children": [], "empty": False, "has_subdirs": False, "matched": False,
            "truncated": False,
        },
        {
            "name": "空目录",
            "path": P("空目录"),
            "volume": "vol1", "volume_path": "/vol1", "name_dup": 1,
            "children": [], "empty": True, "has_subdirs": False, "matched": False,
            "truncated": False,
        },
        {
            "name": "纪录片合集",
            "path": Q("纪录片合集"),
            "volume": "vol2", "volume_path": "/vol2", "name_dup": 1,
            "children": [], "empty": False, "has_subdirs": False, "matched": False,
            "truncated": False,
        },
        {
            "name": "【电影天堂 www.dytt.com】最新剧集合集",
            "path": P("【电影天堂 www.dytt.com】最新剧集合集"),
            "volume": "vol1", "volume_path": "/vol1", "name_dup": 1,
            "children": [
                {
                    "name": "ShowName.S01.1080p.WEB-DL",
                    "path": P("【电影天堂 www.dytt.com】最新剧集合集", "ShowName.S01.1080p.WEB-DL"),
                    "volume": "vol1", "volume_path": "/vol1", "name_dup": 1,
                    "children": [
                        leaf("Subs",
                             P("【电影天堂 www.dytt.com】最新剧集合集",
                               "ShowName.S01.1080p.WEB-DL", "Subs"), "vol1"),
                    ],
                    "empty": False, "has_subdirs": True, "matched": False, "truncated": False,
                },
                leaf("Sample", P("【电影天堂 www.dytt.com】最新剧集合集", "Sample"), "vol1"),
            ],
            "empty": False, "has_subdirs": True, "matched": False, "truncated": False,
        },
    ],
    "empty": False, "has_subdirs": True, "matched": True, "truncated": False,
}

DEMO_DIRS = {
    VOL1: ["【电影天堂 www.dytt.com】最新剧集合集", "AnotherShow.2024.2160p",
           "Movie.2023.1080p.BluRay.x264", "空目录"],
    VOL2: ["Movie.2023.1080p.BluRay.x264", "纪录片合集"],
    "/vol1": ["1000", "docker", "media"],
    "/vol2": ["1000", "影视库"],
    "/vol1/1000/media": ["剧集", "电影"],
    DEMO_DEST: [],
}

DEMO_SIZES = {
    P("【电影天堂 www.dytt.com】最新剧集合集", "ShowName.S01.1080p.WEB-DL"):
        {"files": 12, "dirs": 1, "bytes": 18_446_744_073, "videos": 12},
    P("AnotherShow.2024.2160p", "正片"):
        {"files": 8, "dirs": 0, "bytes": 42_949_672_960, "videos": 8},
    P("Movie.2023.1080p.BluRay.x264"):
        {"files": 3, "dirs": 0, "bytes": 8_589_934_592, "videos": 1},
    Q("纪录片合集"):
        {"files": 30, "dirs": 2, "bytes": 64_424_509_440, "videos": 30},
}

# 聚合来源：飞牛的存储空间里用户数据都在 <存储空间>/1000
DEMO_AGG = [{"path": VOL1, "volume": "vol1", "mapped": True},
            {"path": VOL2, "volume": "vol2", "mapped": True}]

MOCK_JS = """
<script>
/* ===================================================================
   演示模式：这里用页面内的假数据顶替真实后端。
   正式安装到飞牛 NAS 后没有这段代码，所有数据都来自你的真实目录。
   =================================================================== */
(function () {
  const DEST_URL = %(dest)s;
  const TREE = %(tree)s, DIRS = %(dirs)s, SIZES = %(sizes)s, AGG = %(agg)s;
  const jobs = {};
  let jobSeq = 0;

  const info = {
    ok: true, app: 'folder-picker', version: '%(version)s（演示）', mode: 'gateway',
    user: { uid: '1000', username: 'admin', is_admin: true },
    roots: ['/vol1', '/vol2'],
    default_source: '', default_dest: DEST_URL, default_depth: 3,
    aggregate: true, aggregate_subdirs: ['1000'], agg_dirs: AGG,
  };

  const ROOTS = info.roots;

  // 聚合层：各存储空间用户目录下的文件夹并成一个列表（同名各自独立，标注来自哪块盘）
  function aggregateListing() {
    const all = [];
    AGG.forEach(a => (DIRS[a.path] || []).forEach(n => {
      const p = a.path + '/' + n;
      all.push({
        name: n, path: p, mtime: 1724244000, is_dir: true,
        volume: a.volume, volume_path: a.path.replace(/\\/1000$/, ''),
        child_dirs: (DIRS[p] || []).length, empty: (DIRS[p] || []).length === 0,
      });
    }));
    all.sort((x, y) => x.name.toLowerCase() < y.name.toLowerCase() ? -1
                     : x.name.toLowerCase() > y.name.toLowerCase() ? 1 : 0);
    const counter = {};
    all.forEach(d => { counter[d.name] = (counter[d.name] || 0) + 1; });
    all.forEach(d => { d.name_dup = counter[d.name]; });
    return {
      ok: true, path: '', name: '全部存储空间', parent: '',
      aggregate: true, virtual_root: true, at_root: true, bases: AGG,
      dirs: all, files: [], dir_total: all.length, file_total: 0, truncated: false,
    };
  }

  function listOf(path) {
    // 空路径 = 聚合层（各存储空间用户目录下的文件夹并成一个列表）
    if (!path) return aggregateListing();
    // "/" = 按存储空间浏览（列出存储空间）
    if (path === '/') {
      const tops = ROOTS.map(r => {
        const names = DIRS[r] || [];
        return {
          name: r.replace(/^[/]+/, ''), path: r, mtime: 1724244000, is_dir: true,
          child_dirs: names.length, empty: names.length === 0, is_root: true,
        };
      });
      return {
        ok: true, path: '', name: '存储空间', parent: '',
        virtual_root: true, at_root: true,
        dirs: tops, files: [], dir_total: tops.length, file_total: 0, truncated: false,
      };
    }
    const names = DIRS[path] || [];
    const parent = path.split('/').slice(0, -1).join('/');
    const base = path.replace(/\\/+$/, '');
    return {
      ok: true, path, name: path.split('/').pop() || path, parent: parent || '/',
      at_root: ROOTS.indexOf(base) > -1,
      dirs: names.map(n => ({
        name: n, path: base + '/' + n, mtime: 1724244000, is_dir: true,
        child_dirs: (DIRS[base + '/' + n] || []).length,
        empty: (DIRS[base + '/' + n] || []).length === 0,
      })),
      files: [], dir_total: names.length, file_total: 0, truncated: false,
    };
  }

  function statsFor(paths) {
    let files = 0, dirs = 0, bytes = 0, videos = 0;
    const items = [];
    paths.forEach(p => {
      const s = SIZES[p] || { files: 1, dirs: 0, bytes: 1073741824, videos: 1 };
      files += s.files; dirs += s.dirs; bytes += s.bytes; videos += s.videos;
      items.push({ path: p, name: p.split('/').pop(), ...s });
    });
    return { ok: true, stats: { files, dirs, bytes, videos, items, partial: false } };
  }

  function planFor(body) {
    return {
      ok: true,
      job: {
        id: 'demo-plan', op: 'plan', state: 'done', message: '预演完成',
        items: (body.sources || []).map(s => ({
          source: s, target: DEST_URL + '/' + s.split('/').pop(),
          status: 'planned', reason: '',
        })),
        errors: [], done_files: 0, total_files: 0, done_bytes: 0, total_bytes: 0, percent: 100,
      },
    };
  }

  function startJob(body) {
    const id = 'demo-' + (++jobSeq);
    const items = (body.sources || []).map(s => ({
      source: s, target: DEST_URL + '/' + s.split('/').pop(), status: 'done', renamed: false,
    }));
    const totalFiles = items.reduce((n, it) => n + ((SIZES[it.source] || {}).files || 1), 0);
    const totalBytes = items.reduce((n, it) => n + ((SIZES[it.source] || {}).bytes || 0), 0);
    jobs[id] = { tick: 0, totalFiles, totalBytes };
    return { ok: true, id, job: { id, state: 'pending', percent: 0, items: [] } };
  }

  function jobState(id) {
    const j = jobs[id] || { tick: 9, totalFiles: 1, totalBytes: 1 };
    j.tick += 1;
    const done = j.tick >= 4;
    const pct = Math.min(100, Math.round(j.tick / 4 * 100));
    return {
      ok: true,
      job: {
        id, op: 'move', state: done ? 'done' : 'running',
        message: done ? '完成：' + j.totalFiles + ' 个文件 / 18.3 GB' : '正在搬运…',
        current: done ? '' : 'ShowName.S01.1080p.WEB-DL',
        total_files: j.totalFiles, done_files: done ? j.totalFiles : Math.round(j.totalFiles * pct / 100),
        total_bytes: j.totalBytes, done_bytes: done ? j.totalBytes : Math.round(j.totalBytes * pct / 100),
        percent: pct, errors: [],
        items: done ? [{ source: VOL1, target: DEST_URL, status: 'done' }] : [],
      },
    };
  }

  const realFetch = window.fetch ? window.fetch.bind(window) : null;
  window.fetch = async function (url, opt) {
    const u = String(url);
    if (u.indexOf('/api/') === -1) return realFetch(url, opt);
    const body = opt && opt.body ? JSON.parse(opt.body) : {};
    const qs = {};
    u.split('?')[1] && u.split('?')[1].split('&').forEach(kv => {
      const [k, v] = kv.split('=');
      qs[decodeURIComponent(k)] = decodeURIComponent(v || '');
    });
    await new Promise(r => setTimeout(r, 180));  // 模拟一点点网络延迟
    let data;
    if (u.indexOf('/api/info') > -1) data = info;
    else if (u.indexOf('/api/tree') > -1) {
      data = qs.path
        ? { ok: true, tree: TREE, remaining_budget: 20000 }
        : { ok: true, tree: TREE, aggregate: true, remaining_budget: 20000 };
    }
    else if (u.indexOf('/api/list') > -1) data = listOf(qs.path || '');
    else if (u.indexOf('/api/stats') > -1) data = statsFor(body.paths || []);
    else if (u.indexOf('/api/plan') > -1) data = planFor(body);
    else if (u.indexOf('/api/job/cancel') > -1) data = { ok: true };
    else if (u.indexOf('/api/job') > -1) {
      if (opt && opt.method === 'POST') data = startJob(body);
      else data = jobState(qs.id);
    } else if (u.indexOf('/api/export') > -1) {
      const lines = (body.paths || []).map(p => p.split('/').pop() + ',' + p);
      data = { ok: true, filename: 'folder-picker-演示.csv',
               content: '文件夹名,完整路径\\n' + lines.join('\\n') + '\\n', mime: 'text/csv' };
    } else if (u.indexOf('/api/mkdir') > -1) data = { ok: true, path: body.path };
    else data = { ok: false, error: '演示模式不支持该接口' };
    return { ok: true, status: 200, json: async () => data };
  };
})();
</script>
"""

BANNER = """
<div style="background:linear-gradient(135deg,#4593ff,#0f5fff);color:#fff;
            padding:9px 16px;font-size:13px;text-align:center">
  演示模式：界面与正式版完全一致，但目录数据是假的，用来预览交互效果
</div>
"""


def main() -> None:
    html = SRC.read_text(encoding="utf-8")
    mock = MOCK_JS % {
        "dest": json.dumps(DEMO_DEST, ensure_ascii=False),
        "tree": json.dumps(DEMO_TREE, ensure_ascii=False),
        "dirs": json.dumps(DEMO_DIRS, ensure_ascii=False),
        "sizes": json.dumps(DEMO_SIZES, ensure_ascii=False),
        "agg": json.dumps(DEMO_AGG, ensure_ascii=False),
        "version": VERSION,
    }
    marker = "<script>\n'use strict';"
    if marker not in html:
        raise SystemExit("找不到主脚本标记，index.html 结构变了")
    html = html.replace(marker, mock + marker, 1)
    html = html.replace("<body>", "<body>" + BANNER, 1)
    html = html.replace("<title>文件夹挑选器</title>", "<title>文件夹挑选器（演示）</title>", 1)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html, encoding="utf-8")
    print(f"已生成 {OUT}（{OUT.stat().st_size / 1024:.1f} KB）")


if __name__ == "__main__":
    main()
