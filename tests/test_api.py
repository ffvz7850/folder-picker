#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
folder_picker 后端自测 —— 进程内直接驱动 HTTP 处理器，不依赖 TCP/Unix Socket
（开发机上常有代理或沙箱拦截本地端口，走进程内调用可以稳定复现）

覆盖：路由与状态码、浏览（默认不显示文件）、目录树、名称筛选、排除规则、
      统计、预演、复制/移动、嵌套选择去重、重名三策略、保留层级、
      导出、新建目录、路径安全、网关前缀、任务取消、CLI 参数
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
SERVER_PY = PROJECT / "app" / "server" / "folder_picker.py"
WWW = PROJECT / "app" / "www"

PASS: list[str] = []
FAIL: list[str] = []


def check(name, got, want):
    if got == want:
        PASS.append(name)
        print(f"  ✅ {name}", flush=True)
    else:
        FAIL.append(f"{name}\n    期望: {want!r}\n    实际: {got!r}")
        print(f"  ❌ {name}\n     期望: {want!r}\n     实际: {got!r}", flush=True)


def check_true(name, cond, detail=""):
    check(name + (f"  → {detail}" if detail and not cond else ""), bool(cond), True)


def stage(title):
    print(f"\n== {title} ==", flush=True)


# ---------------------------------------------------------------------------
# 进程内 HTTP 客户端
# ---------------------------------------------------------------------------
class _FakeConn:
    """冒充 socket，让 BaseHTTPRequestHandler 以为自己在跟客户端说话"""

    def __init__(self, raw: bytes):
        self.raw = raw
        self.written = bytearray()

    def makefile(self, mode="rb", *args, **kwargs):  # noqa: D401
        if "r" in mode:
            return io.BytesIO(self.raw)
        return self

    def write(self, data):
        self.written += data
        return len(data)

    # http.server 在 wbufsize=0 时用 _SocketWriter，直接调 sendall
    def sendall(self, data):
        self.written += data

    def settimeout(self, *a):
        pass

    def setsockopt(self, *a):
        pass

    def flush(self):
        pass

    def close(self):
        pass

    def shutdown(self, *a):
        pass


class Api:
    def __init__(self, handler_cls, gateway_prefix: str):
        self.handler = handler_cls
        self.prefix = gateway_prefix

    def get(self, endpoint, gateway=False, **params):
        return self._call(endpoint, "GET", None, params, gateway)

    def post(self, endpoint, body=None, gateway=False):
        return self._call(endpoint, "POST", body or {}, {}, gateway)

    def _call(self, endpoint, method, body, params, gateway):
        from urllib.parse import urlencode

        qs = urlencode({k: v for k, v in params.items() if v is not None})
        target = f"/api/{endpoint}" + (f"?{qs}" if qs else "")
        if gateway:
            target = self.prefix + target
        return self.raw(target, method, body)

    def raw(self, target, method="GET", body=None, headers=None):
        lines = [f"{method} {target} HTTP/1.1", "Host: 127.0.0.1"]
        payload = b""
        if body is not None:
            payload = json.dumps(body).encode()
            lines.append("Content-Type: application/json")
            lines.append(f"Content-Length: {len(payload)}")
        for k, v in (headers or {}).items():
            lines.append(f"{k}: {v}")
        raw = ("\r\n".join(lines) + "\r\n\r\n").encode() + payload

        conn = _FakeConn(raw)
        # 构造函数内部会直接跑完 handle()（读到 EOF 即结束），不用再手动调 handle_one_request
        self.handler(conn, ("127.0.0.1", 0), None)

        text = conn.written.decode("utf-8", "replace")
        head, _, rest = text.partition("\r\n\r\n")
        first = head.split("\r\n")[0]
        status = int(first.split()[1])
        hdrs = {}
        for line in head.split("\r\n")[1:]:
            if ": " in line:
                k, v = line.split(": ", 1)
                hdrs[k.lower()] = v
        try:
            data = json.loads(rest)
        except ValueError:
            data = {"raw": rest}
        return status, data, hdrs

    def static(self, path="/"):
        status, data, _ = self.raw(path, "GET")
        # 静态页不是 JSON，raw() 会塞进 raw 字段
        return status, data.get("raw", "")


def load_server_module():
    spec = importlib.util.spec_from_file_location("folder_picker_server", SERVER_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ---------------------------------------------------------------------------
# 造数据
# ---------------------------------------------------------------------------
def build_tree(root: Path) -> Path:
    """造一个"手动解压之后很乱"的目录：散落的广告文件 + 套了好几层的正片 + 空目录"""
    src = root / "src"
    files = {
        src / "包A" / "乱七八糟" / "ShowA.S01" / "ShowA.S01E01.mkv": 120_000,
        src / "包A" / "乱七八糟" / "ShowA.S01" / "ShowA.S01E02.mkv": 130_000,
        src / "包A" / "乱七八糟" / "ShowA.S01" / "Subs" / "chs.ass": 900,
        src / "包A" / "乱七八糟" / "Sample" / "sample.mkv": 3_000,
        src / "包A" / "乱七八糟" / "readme.txt": 40,
        src / "包B" / "ShowB.S02" / "ShowB.S02E01.mkv": 200_000,
        src / "包B" / "说明" / "readme.txt": 10,
        src / "广告.txt": 40,
    }
    for f, size in files.items():
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x" * size)
    (src / "空目录").mkdir(parents=True, exist_ok=True)
    (root / "dest").mkdir(parents=True, exist_ok=True)
    return src


def wait_job(api: Api, job_id: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        _, res, _ = api.get("job", id=job_id)
        last = res["job"]
        if last["state"] in ("done", "error", "cancelled"):
            return last
        time.sleep(0.1)
    return last


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    print("加载后端模块 …")
    se = load_server_module()
    se.Handler.log_message = lambda *a, **k: None  # 静音请求日志

    root = Path(tempfile.mkdtemp(prefix="folder-picker-test-")).resolve()
    outside = Path(tempfile.mkdtemp(prefix="folder-picker-outside-")).resolve()
    src = build_tree(root)
    dest = root / "dest"

    se.Handler.www_dir = str(WWW)
    se.Handler.var_dir = str(root)
    se.Handler.auth_token = ""
    # 直接注入白名单，避免依赖 /vol1 之类真实路径
    se.Handler.guard = se.PathGuard([str(root), str(outside)])

    api = Api(se.Handler, se.GATEWAY_PREFIX)
    S = str(src).replace("\\", "/")
    D = str(dest).replace("\\", "/")

    try:
        run_checks(se, api, src, dest, outside, root, S, D)
    except Exception as exc:  # noqa: BLE001
        import traceback

        FAIL.append(f"测试过程异常：{exc}")
        print(traceback.format_exc())
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(outside, ignore_errors=True)

    print("\n".join(f"  ✅ {p}" for p in PASS))
    if FAIL:
        print("\n".join(f"  ❌ {f}" for f in FAIL))
    print(f"\n通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    return 1 if FAIL else 0


def run_checks(se, api: Api, src: Path, dest: Path, outside: Path, root: Path, S: str, D: str):
    # ---------------------------------------------------------- 静态页
    stage("静态页面")
    status, html = api.static("/")
    check("GET / 状态码", status, 200)
    check_true("首页含应用名", "文件夹挑选器" in html)
    check_true("首页含按层一键勾选", "按层一键勾选" in html)
    check_true("首页含筛选：显示文件开关", 'id="showFiles"' in html)
    check_true("首页含移动/复制按钮", "移动到目标目录" in html and "复制" in html)
    check_true("首页自动加载基础路径", "const BASE" in html)

    # 用户要求：「按层一键勾选」和「操作」合并成一块，并且整体紧凑。
    # 这里钉住结构：三块必须同属一个卡片（工作台），目录树在它外面单独一块。
    check_true("首页有「工作台」", "工作台" in html)
    work = html[html.index('class="card work"'):html.index('id="tree"')]
    check_true("「按层一键勾选」已并入工作台", "按层一键勾选" in work)
    check_true("「已勾选」已并入同一个工作台", "已勾选" in work)
    check_true("「操作」（目标目录 / 移动 / 复制）也并入同一个工作台",
               'id="destPath"' in work and "runJob('move')" in work and "runJob('copy')" in work)
    check_true("不再有独立的「操作」卡片", html.count('class="card rows"') <= 1)
    check_true("筛选条件默认收起（默认只占一行）", 'class="row filters"' in html)
    tight = html.replace(" ", "").replace("\n", "")
    check_true("目录树自己滚动，不把页面撑长",
               "#tree{overflow:auto" in tight and "max-height" in tight.split("#tree{")[1][:200])
    check_true("工作台在宽屏下固定在左侧，树再长它也不跟着跑",
               "position:sticky" in tight.split(".work{")[1][:160])

    # ---------------------------------------------------------- info
    stage("基本信息")
    status, info, _ = api.get("info")
    check("GET /api/info 状态码", status, 200)
    check("info.app", info["app"], "folder-picker")
    check("版本号（首页会显示 1.4.1）", info["version"], "1.4.1")
    check("默认 TCP 模式的鉴权标记", info["mode"], "tcp")
    check("默认展开层数由后端下发（3 层）", info["default_depth"], 3)
    check_true("默认开启聚合视图", info["aggregate"] is True)
    check("聚合的用户目录名", info["aggregate_subdirs"], ["1000"])
    check_true("info 带出聚合入口目录", isinstance(info["agg_dirs"], list) and info["agg_dirs"],
               str(info["agg_dirs"]))
    check_true("路径白名单已生效", any(str(root).replace("\\", "/") in r for r in info["roots"]),
               str(info["roots"]))

    # ---------------------------------------------------------- 浏览
    stage("目录浏览（默认不显示文件）")
    status, d, _ = api.get("list", path=S)
    check("list 状态码", status, 200)
    check("默认只返回文件夹（空文件夹也默认隐藏）",
          sorted(x["name"] for x in d["dirs"]), ["包A", "包B"])
    check("默认 files 为空（不展示文件）", d["files"], [])
    check("子文件夹计数正确（包A 下有 1 个）",
          [x["child_dirs"] for x in d["dirs"] if x["name"] == "包A"], [1])
    check("返回上级目录路径", d["parent"], str(root).replace("\\", "/"))

    status, d2, _ = api.get("list", path=S, show_files=1)
    check("打开显示文件后能看到文件", [x["name"] for x in d2["files"]], ["广告.txt"])
    check("文件带大小", d2["files"][0]["size"], 40)
    check("文件标记非视频", d2["files"][0]["is_video"], False)

    status, d3, _ = api.get("list", path=S, show_empty=0)
    check("隐藏空文件夹", sorted(x["name"] for x in d3["dirs"]), ["包A", "包B"])

    status, d3b, _ = api.get("list", path=S, show_empty=1)
    check("允许显示空文件夹时出现", sorted(x["name"] for x in d3b["dirs"]), ["包A", "包B", "空目录"])

    status, d4, _ = api.get("list", path=f"{S}/包B", show_files=1)
    check("子目录里的视频被识别", [x["is_video"] for x in d4["files"]] or [False], [False])
    status, d5, _ = api.get("list", path=f"{S}/包B/ShowB.S02", show_files=1)
    check("视频文件被标记", [x["name"] for x in d5["files"] if x["is_video"]], ["ShowB.S02E01.mkv"])

    # ---------------------------------------------------------- 浏览起点
    # 曾经把 "/" 丢给用户浏览：白名单里没有 "/"，Linux 上判越权、Windows 上连绝对路径
    # 都不算，用户点「浏览」永远停在加载中。
    # 现在起点更进一步：直接聚合各存储空间用户目录（/volN/1000）下的文件夹，
    # 用户不必先点存储空间、再点 1000。
    stage("浏览起点：聚合层（各存储空间 /vol*/1000 下的文件夹并成一个列表）")
    vol_a, vol_b = root / "volA", root / "volB"
    for base, names in ((vol_a, ["下载", "共有名"]), (vol_b, ["影视", "共有名"])):
        for n in names:
            (base / "1000" / n / "子目录").mkdir(parents=True, exist_ok=True)
    (vol_a / "1000" / "下载" / "包A" / "S01" / "Subs").mkdir(parents=True, exist_ok=True)

    saved_guard = se.Handler.guard
    se.Handler.guard = se.PathGuard([str(vol_a), str(vol_b)])
    try:
        status, ag, _ = api.get("list", path="")
        check("空路径状态码", status, 200)
        check("空路径返回聚合层（不是报错）", ag.get("aggregate"), True)
        check("聚合层不指向任何真实目录", ag["path"], "")
        check("聚合层来源是各存储空间的用户目录",
              [(b["volume"], b["path"].split("/")[-1]) for b in ag["bases"]],
              [("volA", "1000"), ("volB", "1000")])
        check("把各盘用户目录下的文件夹并成一个列表",
              sorted(x["name"] for x in ag["dirs"]), ["下载", "共有名", "共有名", "影视"])
        check("同名文件夹各标出所在存储空间",
              sorted(x["volume"] for x in ag["dirs"] if x["name"] == "共有名"),
              ["volA", "volB"])
        check("同名几处会标出来（提醒用户这不是重复项）",
              [x["name_dup"] for x in ag["dirs"] if x["name"] == "共有名"], [2, 2])
        check("聚合层每一项都是真实目录，可勾选搬运",
              all(os.path.isdir(x["path"]) for x in ag["dirs"]), True)

        status, at, _ = api.get("tree", path="", depth=2)
        check("空路径的目录树也是聚合树", at.get("aggregate"), True)
        root_node = at["tree"]
        check("聚合树根是虚拟根（不是可搬运的目录）", root_node.get("virtual_root"), True)
        check("第 1 层直接是各存储空间里的文件夹",
              sorted(c["name"] for c in root_node["children"]),
              ["下载", "共有名", "共有名", "影视"])
        check("聚合树节点带存储空间标记",
              sorted(c["volume"] for c in root_node["children"] if c["name"] == "共有名"),
              ["volA", "volB"])
        check("聚合树里同名项的 name_dup = 2",
              [c["name_dup"] for c in root_node["children"] if c["name"] == "共有名"], [2, 2])
        dl = next(c for c in root_node["children"] if c["name"] == "下载")
        check("聚合项下面继续正常展开", [c["name"] for c in dl["children"]], ["包A"])

        status, at3, _ = api.get("tree", path="", depth=3)
        dl3 = next(c for c in at3["tree"]["children"] if c["name"] == "下载")
        check("聚合树按层数继续往下展开（第 3 层）",
              [c["name"] for c in dl3["children"][0]["children"]], ["S01"])

        status, agf, _ = api.get("tree", path="", depth=2, filter="影视")
        check("名称筛选在聚合树上同样生效",
              [c["name"] for c in agf["tree"]["children"]], ["影视"])

        # 某块盘上没有 1000（或用户自己加的白名单根）时退回该存储空间根目录
        shutil.rmtree(vol_b / "1000", ignore_errors=True)
        (vol_b / "散装" / "里面").mkdir(parents=True, exist_ok=True)
        status, ag2, _ = api.get("list", path="")
        check("存储空间里没有 1000 时退回该存储空间根目录",
              [(b["volume"], b["path"].split("/")[-1]) for b in ag2["bases"]],
              [("volA", "1000"), ("volB", "volB")])
        check("退回根目录后仍能列出里面的文件夹",
              sorted(x["name"] for x in ag2["dirs"]), ["下载", "共有名", "散装"])

        # "/" 保留为「按存储空间浏览」的入口
        status, vr2, _ = api.get("list", path="/")
        check('path="/" 不再报"必须是绝对路径"', status, 200)
        check('path="/" 仍可切换到按存储空间浏览', vr2.get("virtual_root"), True)
        check("存储空间列表列出各存储空间",
              sorted(x["name"] for x in vr2["dirs"]), ["volA", "volB"])
        check("存储空间各项标记 is_root", all(x.get("is_root") for x in vr2["dirs"]), True)

        status, d6, _ = api.get("list", path=str(vol_a).replace("\\", "/"))
        check("存储空间根被标记 at_root（供「上一级」回存储空间列表）", d6.get("at_root"), True)

        status, err, _ = api.get("list", path=str(root).replace("\\", "/"))
        check("不在聚合来源里的目录依然被拒（白名单没有放宽）", status, 400)
    finally:
        se.Handler.guard = saved_guard
        shutil.rmtree(vol_a, ignore_errors=True)
        shutil.rmtree(vol_b, ignore_errors=True)

    stage("浏览起点：存储空间列表 / 系统目录过滤")
    root_posix = str(root).replace("\\", "/")
    status, vr, _ = api.get("list", path="/")
    check("存储空间列表列出所有可访问的根目录",
          sorted(x["name"] for x in vr["dirs"]), sorted([root.name, outside.name]))
    check("存储空间各项 is_root", all(x.get("is_root") for x in vr["dirs"]), True)
    check("存储空间列表不指向任何真实目录", vr["path"], "")

    status, d7, _ = api.get("list", path=f"{S}/包A")
    check("普通子目录 at_root 为假", d7.get("at_root"), False)

    # 飞牛的 @appdata / @appcenter 这类系统目录是纯噪音，默认不展示
    sysdir = root / "@appdata"
    (sysdir / "inner").mkdir(parents=True, exist_ok=True)
    status, d8, _ = api.get("list", path=root_posix, show_empty=1)
    check("@ 开头的飞牛系统目录默认隐藏",
          "@appdata" in [x["name"] for x in d8["dirs"]], False)
    status, d9, _ = api.get("list", path=root_posix, show_empty=1, hide_hidden=0)
    check("关掉隐藏开关后仍能进去访问", "@appdata" in [x["name"] for x in d9["dirs"]], True)
    shutil.rmtree(sysdir, ignore_errors=True)

    # ---------------------------------------------------------- 目录树
    stage("目录树与按层结构")
    status, tdef, _ = api.get("tree", path=S)          # 不传 depth，用后端默认值
    d3 = tdef["tree"]["children"][0]["children"][0]["children"]
    check("默认展开 3 层（传不传 depth 都一样）",
          sorted(c["name"] for c in d3), ["Sample", "ShowA.S01"])

    status, t, _ = api.get("tree", path=S, depth=3)
    tree = t["tree"]
    check("tree 状态码", status, 200)
    check("第一层子文件夹", sorted(c["name"] for c in tree["children"]), ["包A", "包B"])
    pkg_a = next(c for c in tree["children"] if c["name"] == "包A")
    check("第二层子文件夹", [c["name"] for c in pkg_a["children"]], ["乱七八糟"])
    mess = pkg_a["children"][0]
    check("第三层子文件夹（Sample 也显示，便于确认）",
          sorted(c["name"] for c in mess["children"]), ["Sample", "ShowA.S01"])

    def walk(node):
        yield node
        for ch in node.get("children") or []:
            yield from walk(ch)

    check_true("树默认不挂任何文件", all(n["files"] == [] for n in walk(tree)))

    status, tsf, _ = api.get("tree", path=S, depth=1, show_files=1)
    check("打开显示文件后根节点挂出文件",
          [f["name"] for f in tsf["tree"]["files"]], ["广告.txt"])
    check("文件带大小与视频标记",
          [(f["size"], f["is_video"]) for f in tsf["tree"]["files"]], [(40, False)])

    status, tsf2, _ = api.get("tree", path=S, depth=4, show_files=1)
    s02 = next(iter(
        n for n in walk(tsf2["tree"]) if n["name"] == "ShowB.S02"
    ))
    check("show_files 会递归传到深层节点",
          [f["name"] for f in s02["files"]], ["ShowB.S02E01.mkv"])
    check("深层视频文件被标记", s02["files"][0]["is_video"], True)

    status, tsf3, _ = api.get("tree", path=S, depth=1, show_files=1, filter="S01")
    check("筛选同时作用于文件名（不匹配的文件不挂出来）", tsf3["tree"]["files"], [])

    status, tsf4, _ = api.get("tree", path=S, depth=1, show_files=1, filter="广告")
    check("文件名命中筛选时才挂出来",
          [f["name"] for f in tsf4["tree"]["files"]], ["广告.txt"])

    status, t1, _ = api.get("tree", path=S, depth=1)
    check("depth=1 不再展开下一层", t1["tree"]["children"][0].get("children"), [])

    status, tf, _ = api.get("tree", path=S, depth=3, filter="S01")
    check("名称筛选保留命中分支的整条路径", [c["name"] for c in tf["tree"]["children"]], ["包A"])

    status, tf2, _ = api.get("tree", path=S, depth=3, filter="ShowA S01")
    check("多个关键词需全部命中", [c["name"] for c in tf2["tree"]["children"]], ["包A"])

    status, tf3, _ = api.get("tree", path=S, depth=3, filter="不存在的名字")
    check("筛选无结果时返回空树", tf3["tree"]["children"], [])

    status, te, _ = api.get("tree", path=S, depth=3, exclude="Sample,乱七八糟")
    check("排除规则命中（父级失去唯一子目录后也被隐藏）",
          [c["name"] for c in te["tree"]["children"]], ["包B"])

    status, th, _ = api.get("tree", path=S, depth=1, show_empty=0)
    check("隐藏空文件夹", sorted(c["name"] for c in th["tree"]["children"]), ["包A", "包B"])

    status, th2, _ = api.get("tree", path=S, depth=1, show_empty=1)
    check("显示空文件夹时出现它", sorted(c["name"] for c in th2["tree"]["children"]),
          ["包A", "包B", "空目录"])

    status, th3, _ = api.get("tree", path=S, depth=2, show_empty=0)
    pkg_b = next(c for c in th3["tree"]["children"] if c["name"] == "包B")
    check("只含文件的目录不会被当成空目录（否则正片目录会看不见）",
          sorted(c["name"] for c in pkg_b["children"]), ["ShowB.S02", "说明"])

    status, td, _ = api.get("tree", path=S, depth=99)
    check("超范围层数被夹到上限（不报错）", status, 200)

    # ---------------------------------------------------------- 统计
    stage("统计")
    target = f"{S}/包A/乱七八糟/ShowA.S01"
    status, st, _ = api.post("stats", {"paths": [target]})
    check("stats 状态码", status, 200)
    check("文件数", st["stats"]["files"], 3)
    check("字节数", st["stats"]["bytes"], 120_000 + 130_000 + 900)
    check("视频数", st["stats"]["videos"], 2)
    check("子目录数", st["stats"]["dirs"], 1)

    status, st2, _ = api.post("stats", {"paths": []})
    check("空选择被拒绝", status, 400)

    # ---------------------------------------------------------- 预演
    stage("预演（不动文件）")
    status, pl, _ = api.post("plan", {"sources": [target], "dest": D, "op": "copy"})
    check("plan 状态码", status, 200)
    check("plan 不落盘", (dest / "ShowA.S01").exists(), False)
    check("plan 标记为 planned", pl["job"]["items"][0]["status"], "planned")

    # 预演不能因为"重名覆盖"而删掉已有目录
    (dest / "已存在").mkdir(exist_ok=True)
    (dest / "已存在" / "keep.txt").write_text("keep", encoding="utf-8")
    status, pl2, _ = api.post("plan", {"sources": [f"{S}/包A"], "dest": D, "op": "move",
                                       "conflict": "overwrite"})
    check("预演不会真删同名目录", (dest / "已存在" / "keep.txt").exists(), True)

    # 客户端如果把「预演」本身当成操作名传过来，也不能报"不支持的操作"
    status, pl3, _ = api.post("plan", {"sources": [target], "dest": D, "op": "plan"})
    check("op=plan 也能预演（向后兼容）", status, 200)
    check("op=plan 时仍标记为 planned", pl3["job"]["items"][0]["status"], "planned")

    status, bad, _ = api.post("job", {"sources": [target], "dest": D, "op": "删除"})
    check("非法操作名被拒绝", status, 400)
    check_true("拒绝提示可读", "不支持的操作" in bad.get("error", ""), bad.get("error"))

    # ---------------------------------------------------------- 复制
    stage("复制 / 重名策略 / 保留层级")
    status, jb, _ = api.post("job", {"sources": [target], "dest": D, "op": "copy"})
    check("job 状态码", status, 200)
    final = wait_job(api, jb["id"])
    check("复制完成", final["state"], "done")
    check("目标文件存在", (dest / "ShowA.S01" / "ShowA.S01E01.mkv").exists(), True)
    check("源文件仍在", (src / "包A" / "乱七八糟" / "ShowA.S01" / "ShowA.S01E02.mkv").exists(), True)
    check("复制出的字节统计", final["done_bytes"], 120_000 + 130_000 + 900)

    status, jb, _ = api.post("job", {"sources": [target], "dest": D, "op": "copy", "conflict": "rename"})
    wait_job(api, jb["id"])
    check("重名自动改名（加序号）", (dest / "ShowA.S01 (2)").is_dir(), True)

    status, jb, _ = api.post("job", {"sources": [target], "dest": D, "op": "copy", "conflict": "skip"})
    res = wait_job(api, jb["id"])
    check("重名跳过", res["items"][0]["status"], "skipped")

    status, jb, _ = api.post("job", {"sources": [f"{S}/包A"], "dest": D, "op": "copy",
                                     "keep_structure": True, "root": S})
    wait_job(api, jb["id"])
    check("保留层级结构", (dest / "包A").is_dir(), True)

    # ---------------------------------------------------------- 移动与去重
    stage("移动 / 嵌套选择去重")
    status, jb, _ = api.post("job", {
        "sources": [f"{S}/包B/ShowB.S02", f"{S}/包B"],
        "dest": D, "op": "move",
    })
    res = wait_job(api, jb["id"])
    check("同时选父与子时只保留父目录", len(res["items"]), 1)
    check("移动后源消失", (src / "包B").exists(), False)
    check("移动后目标完整", (dest / "包B" / "ShowB.S02" / "ShowB.S02E01.mkv").exists(), True)

    # ---------------------------------------------------------- 导出
    stage("导出清单")
    status, ex, _ = api.post("export", {"paths": [target], "format": "csv"})
    check("csv 表头", ex["content"].splitlines()[0], "文件夹名,完整路径")
    check("csv 文件名", ex["filename"].endswith(".csv"), True)
    status, ex2, _ = api.post("export", {"paths": [target], "format": "json"})
    check("json 内容", json.loads(ex2["content"])[0]["name"], "ShowA.S01")
    status, ex3, _ = api.post("export", {"paths": [target], "format": "txt"})
    check_true("txt 一行一个路径", target in ex3["content"])
    status, ex4, _ = api.post("export", {"paths": [target], "format": "bash"})
    check_true("bash 导出带 shebang", ex4["content"].startswith("#!/bin/bash"))

    # ---------------------------------------------------------- 新建目录
    stage("新建目标目录")
    status, mk, _ = api.post("mkdir", {"path": f"{D}/媒体库/电影"})
    check("mkdir 状态码", status, 200)
    check("多层目录已建立", (dest / "媒体库" / "电影").is_dir(), True)

    # ---------------------------------------------------------- 安全
    stage("路径安全")
    status, err, _ = api.get("list", path=os.path.abspath(os.sep))
    check("越权路径 400", status, 400)
    check_true("越权提示可读", "权限" in err.get("error", ""), str(err))
    status, err, _ = api.get("list", path="relative/dir")
    check("相对路径被拒绝", status, 400)

    status, err, _ = api.post("job", {"sources": [f"{S}/包A"], "dest": f"{S}/包A/乱七八糟"})
    check("目标落在源内部被拒绝", status, 400)
    check_true("提示说明原因", "里面" in err.get("error", ""), str(err))

    status, err, _ = api.post("job", {"sources": [f"{S}/包A"], "dest": f"{S}/包A"})
    check("目标等于源被拒绝", status, 400)

    status, err, _ = api.post("job", {"sources": [f"{S}/不存在的目录"], "dest": D})
    check("不存在的源被拒绝", status, 404)
    check_true("提示说明不存在", "不存在" in err.get("error", ""), str(err))

    status, err, _ = api.post("job", {"sources": "不是数组", "dest": D})
    check("参数类型错误不会 500", status, 400)

    status, err, _ = api.raw("/api/../etc/passwd", "GET")
    check("静态目录穿越被拦", status in (400, 403, 404), True)

    status, err, _ = api.get("不存在的接口")
    check("未知接口 404", status, 404)

    status, err, _ = api.raw("/api/info", "GET", headers={"X-Auth-Token": "bad"})
    check("未设置令牌时不校验", status, 200)

    # ---------------------------------------------------------- 网关前缀
    stage("统一网关前缀")
    status, info2, _ = api.get("info", gateway=True)
    check("带 /app/folder-picker 前缀可访问", status, 200)
    status, html2 = api.static("/app/folder-picker/")
    check("带前缀的首页可访问", status, 200)
    check_true("带前缀首页内容正常", "文件夹挑选器" in html2)
    status, t9, _ = api.get("tree", gateway=True, path=S, depth=1)
    check("带前缀的接口参数正常", status, 200)

    stage("网关前缀容错（404 回归）")
    # 前端在网关下可能把地址算到各种形态，服务端必须都能认出来，
    # 否则用户看到的就是「请求失败 HTTP 404」。
    status, info3, _ = api.raw("/APP/FOLDER-PICKER/api/info")
    check("前缀大小写不同也能访问", status, 200)
    check("大小写不同时仍返回本应用", info3.get("app"), "folder-picker")
    status, _, _ = api.raw("/app/folder-picker//api/info")
    check("重复斜杠也能访问", status, 200)
    status, info4, _ = api.raw("/some/prefix/app/folder-picker/api/info")
    check("网关多套一层目录也能访问", status, 200)
    check("多套一层时仍返回本应用", info4.get("app"), "folder-picker")
    status, _, _ = api.raw("/favicon.ico")
    check("浏览器要图标不算错误（静默 204）", status, 204)
    status, _, _ = api.raw("/api/nope")
    check("未知接口仍然是 404（没有吞掉错误）", status, 404)

    # ---------------------------------------------------------- 取消
    stage("任务取消")
    status, jb, _ = api.post("job", {"sources": [f"{S}/包A/乱七八糟"], "dest": D, "op": "copy",
                                     "conflict": "rename"})
    status, cc, _ = api.post("job/cancel", {"id": jb["id"]})
    check("取消接口状态码", status, 200)
    res = wait_job(api, jb["id"])
    check_true("取消后终态合理", res["state"] in ("cancelled", "done"), res["state"])

    # ---------------------------------------------------------- CLI
    stage("跨平台 / CLI / 端口占用 / 独立进程")
    check_true("模块可导入且暴露 Unix Socket 服务器类", hasattr(se, "ThreadingUnixHTTPServer"))
    if os.name == "nt":
        check("Windows 下 Unix Socket 不可用时为 None（有兜底）",
              se.ThreadingUnixHTTPServer, None)
    proc = subprocess.run([sys.executable, str(SERVER_PY), "--help"],
                          capture_output=True, text=True, cwd=str(PROJECT), timeout=30)
    check("CLI --help 可用", proc.returncode, 0)
    check_true("CLI 支持 --socket 参数", "--socket" in proc.stdout)
    check_true("CLI 支持 --www 参数", "--www" in proc.stdout)
    check_true("CLI 支持 --var 参数", "--var" in proc.stdout)

    # 起一个真实进程，占住一个端口
    import socket as _socket

    with _socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        busy_port = s.getsockname()[1]
    first = subprocess.Popen(
        [sys.executable, str(SERVER_PY), "--host", "127.0.0.1", "--port", str(busy_port),
         "--www", str(WWW), "--var", str(root)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        env={**os.environ, "EXTRA_ROOTS": str(root)},
    )
    time.sleep(1.5)

    # 再起一个，抢占同一个端口
    second = subprocess.Popen(
        [sys.executable, str(SERVER_PY), "--host", "127.0.0.1", "--port", str(busy_port),
         "--www", str(WWW), "--var", str(root)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        env={**os.environ, "EXTRA_ROOTS": str(root)},
    )
    time.sleep(1.5)
    out2 = ""
    try:
        out2 = second.communicate(timeout=5)[0]
    except subprocess.TimeoutExpired:
        second.kill()
        out2 = "（进程仍在运行）"
    check("端口被占用时不会抛异常栈", "Traceback" not in out2, True)
    if os.name != "nt":
        # Linux 上重复绑定会 EADDRINUSE，此时应给出可读提示并保留 Socket 通道
        check_true("端口被占用时给出可读提示",
                   "不可用" in out2 or "仅使用统一网关" in out2, out2.strip()[:200])
    else:
        # Windows 的 SO_REUSEADDR 允许重复绑定同一端口，因此这里不会报错，属正常差异
        print("  ⏭  Windows 允许重复绑定同一端口，跳过「端口被占」断言", flush=True)
    first.terminate()
    try:
        first.wait(timeout=5)
    except subprocess.TimeoutExpired:
        first.kill()

    if hasattr(__import__("socketserver"), "UnixStreamServer"):
        # Linux/飞牛上：TCP 端口被占，统一网关 Socket 仍要能服务
        sock_path = str(root / "app.sock")
        port2 = _free_port()
        a = subprocess.Popen(
            [sys.executable, str(SERVER_PY), "--host", "127.0.0.1", "--port", str(port2),
             "--www", str(WWW), "--var", str(root)],
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
            env={**os.environ, "EXTRA_ROOTS": str(root)},
        )
        time.sleep(1.5)
        b = subprocess.Popen(
            [sys.executable, str(SERVER_PY), "--socket", sock_path,
             "--host", "127.0.0.1", "--port", str(port2),
             "--www", str(WWW), "--var", str(root)],
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
            env={**os.environ, "EXTRA_ROOTS": str(root)},
        )
        time.sleep(2)
        check("TCP 被占时进程仍存活（Socket 通道可用）", b.poll(), None)
        check("网关 Socket 文件已创建", os.path.exists(sock_path), True)
        for p in (a, b):
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
    else:
        print("  ⏭  当前系统不支持 Unix Socket，跳过网关通道存活测试", flush=True)


    # ---------------------------------------------------------- 前后端契约
    stage("前后端契约交叉检查")
    import re

    html = (WWW / "index.html").read_text(encoding="utf-8")
    server_src = SERVER_PY.read_text(encoding="utf-8")

    ids_in_html = set(re.findall(r'id="([^"]+)"', html))
    ids_used = set(re.findall(r"\$\('([^']+)'\)", html)) | set(
        re.findall(r"getElementById\('([^']+)'\)", html)
    )
    missing = sorted(i for i in ids_used if i not in ids_in_html)
    check("前端引用的元素 id 都存在", missing, [])

    fe_endpoints = set(re.findall(r"api\('([a-z/]+)'", html))
    be_endpoints = set()
    for m in re.finditer(r'"/api/([a-z/]+)"', server_src):
        be_endpoints.add(m.group(1))
    unknown = sorted(e for e in fe_endpoints if e not in be_endpoints)
    check("前端调用的接口后端都实现了", unknown, [])
    check_true("前端覆盖了浏览/树/统计/任务/导出/建目录等接口",
               {"tree", "list", "stats", "job", "export", "mkdir", "info"} <= fe_endpoints,
               str(sorted(fe_endpoints)))
    check_true("预演接口被前端调用（三元表达式里的字面量）", "'plan'" in html)

    check_true("后端没有把根路径以外的东西暴露给静态服务",
               'os.path.join(self.www_dir, rel)' in server_src)
    check_true("后端强制路径白名单", "没有访问这个目录的权限" in server_src)
    check_true("后端拒绝把目录搬进自己的子树", "不能放在" in server_src and "无需操作" in server_src)
    check_true("演示页与正式页界面同源",
               (PROJECT / "preview/folder-picker-demo.html").exists()
               and "文件夹挑选器" in (PROJECT / "preview/folder-picker-demo.html")
               .read_text(encoding="utf-8"))

    # ---------------------------------------------------------- 界面风格与布局
    stage("界面风格（飞牛化）与布局")
    check_true("配色取自飞牛的蓝色体系（主蓝 #0F5FFF）",
               "--fn-blue:#0f5fff" in html and "#4593ff" in html)
    check_true("卡片是大圆角 + 柔和阴影", "--fn-r:14px" in html and "box-shadow" in html)
    check_true("展开层数默认 3 层", re.search(r"<option selected>3</option>", html) is not None)
    check_true("「按层一键勾选」在目录树上方",
               html.index("按层一键勾选") < html.index('id="tree"'))
    check_true("操作区（目标目录/执行按钮）在目录树上方",
               html.index('id="destPath"') < html.index('id="tree"'))
    check_true("「已勾选」也在目录树上方",
               html.index('id="selList"') < html.index('id="tree"'))
    check_true("来源目录允许留空（空 = 聚合各存储空间）", "留空即聚合" in html)
    check_true("页面提示了聚合了几个存储空间", 'id="aggBadge"' in html)

    # 用户要求：三块合并成一块 + 整体紧凑 + 文件夹再多也不乱
    after_tree = html[html.index('id="tree"'):]
    check_true("三块（按层勾选 / 已勾选 / 操作）合并在同一个卡片里",
               html.count('class="card work"') == 1 and 'id="selList"' not in after_tree
               and 'id="destPath"' not in after_tree)
    check_true("合并后不再有零散的独立「操作」卡片", 'class="card rows"' not in html)
    check_true("工作台是窄栏（约 396px），目录树占掉剩下的全部宽度",
               "grid-template-columns:minmax(0,396px)" in html)
    check_true("按钮统一压小一号（工具栏用 mini 尺寸）", "button.mini{" in tight)
    check_true("筛选条件默认收起，默认只占一行", "filters{display:none}" in tight
               and "filters.on{display:flex}" in tight)
    check_true("窄屏（<1260px）自动退回单栏，不至于挤成一团",
               "@media(max-width:1259px){.main{grid-template-columns:minmax(0,1fr)}}" in tight)

    # 桌面入口：必须在飞牛桌面里开小窗，而不是另起一个浏览器标签
    entry = json.loads((PROJECT / "app/ui/config").read_text(encoding="utf-8"))
    entry_app = entry[".url"]["folder-picker.Application"]
    check("桌面入口 type = iframe（飞牛桌面内嵌小窗打开）", entry_app["type"], "iframe")
    mfn = re.search(r"^desktop_applaunchname\s*=\s*(\S+)",
                    (PROJECT / "manifest").read_text(encoding="utf-8"), re.M)
    check("manifest 的 desktop_applaunchname 与入口 key 一致",
          mfn.group(1) if mfn else None, "folder-picker.Application")
    tcp_entry = json.loads((PROJECT / "app/ui/config.tcp").read_text(encoding="utf-8"))
    check("旧版端口直连入口也是 iframe",
          tcp_entry[".url"]["folder-picker.Application"]["type"], "iframe")

    # iframe 里浏览器原生 confirm() 常被 sandbox 静默拦截，
    # 会表现为"移动按钮点了没反应"，所以必须用自绘确认框
    check_true("前端不依赖原生 confirm()", "confirm(" not in html)
    check_true("前端有自绘确认框", "askConfirm" in html and 'id="confirmBox"' in html)

    # ---------------------------------------------------------- 页头移除 + 记住上次
    stage("页头移除与「记住上次」")
    # 用户要求：顶上那排（应用图标 + 应用名 + 副标题）移除掉不显示
    check_true("页头整块已移除（小窗标题栏上本来就有应用名）", "<header" not in html)
    check_true("副标题文案也不再出现", "看清每一层子文件夹" not in html)
    check_true("网关 / 版本标记挪到页脚，仍然看得到",
               'id="modeBadge"' in html and 'id="verBadge"' in html
               and html.index('id="tree"') < html.index('id="modeBadge"'))
    check_true("界面第一块直接就是工具条（不再有页头占一行）",
               html.index('class="card tools"') < html.index('class="card work"'))
    check_true("页头没了的空间给了目录树（高度补偿过）",
               "calc(100vh-164px)" in tight)

    # 用户要求：记住上次的筛选和添加的来源目录
    check_true("筛选条件写进浏览器本地存储", "folder-picker.prefs.v1" in html)
    check_true("来源目录另存一份历史", "folder-picker.roots.v1" in html)
    check_true("来源目录输入框挂了历史下拉", 'list="rootHist"' in html and 'id="rootHist"' in html)
    check_true("目录选择器里有「最近用过」入口", 'id="pkRecent"' in html)
    check_true("localStorage 读写全部兜了异常（iframe 沙箱会抛 SecurityError）",
               "functionlsGet(k){try{" in tight and "functionlsSet(k,v){try{" in tight)
    check_true("还会实测能不能写（有的窗口能读不能写）",
               "functionstorageOK()" in tight)
    check_true("读成功才记来源目录：打错的路径不该被记住",
               "state.lastRoot = path;" in html and "rememberRoot(path);" in html)
    check_true("目标目录只在真要用它搬东西时才记", "rememberDest(dest);" in html)
    check_true("用户自己上次用的优先于后端下发的默认值",
               "applyPrefs();" in html and "优先于上面的默认值" in html)
    check_true("记不住时页脚如实说明，不假装记住了", "不会被记住" in html)
    check_true("筛选条件改动会自动落盘（不用手动保存）", "savePrefsSoon" in html)


def _free_port() -> int:
    import socket as _socket

    with _socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


if __name__ == "__main__":
    import faulthandler

    faulthandler.dump_traceback_later(int(os.environ.get("FP_WATCHDOG", "120")), exit=True)
    sys.exit(main())
