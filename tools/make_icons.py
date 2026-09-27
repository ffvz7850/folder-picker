#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成飞牛应用图标（纯标准库，自带简易抗锯齿光栅化器）

产出：
    ICON.PNG              64x64
    ICON_256.PNG          256x256
    app/ui/images/icon_64.png
    app/ui/images/icon_256.png

图案：圆角方形渐变底 + 白色文件夹 + 蓝色对勾（挑选文件夹的语义）
"""

import os
import struct
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 项目根目录
SS = 4  # 超采样倍率

BG_TOP = (88, 148, 255)
BG_BOTTOM = (37, 88, 226)
WHITE = (255, 255, 255)
CHECK = (37, 88, 226)


def png_bytes(pixels, width, height, channels=4):
    """pixels: 扁平字节串（RGBA）"""
    raw = bytearray()
    stride = width * channels
    for y in range(height):
        raw.append(0)  # filter type 0
        raw += pixels[y * stride:(y + 1) * stride]

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    color_type = 6 if channels == 4 else 2
    header = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def in_round_rect(x, y, x0, y0, x1, y1, r):
    if x < x0 or x > x1 or y < y0 or y > y1:
        return False
    cx = x0 + r if x < x0 + r else (x1 - r if x > x1 - r else x)
    cy = y0 + r if y < y0 + r else (y1 - r if y > y1 - r else y)
    if cx == x and cy == y:
        return True
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def dist_to_segment(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = 0.0 if t < 0 else (1.0 if t > 1 else t)
    return ((px - (ax + t * dx)) ** 2 + (py - (ay + t * dy)) ** 2) ** 0.5


def render(size):
    n = size
    big = n * SS
    out = bytearray()

    # 归一化几何（基于 big 画布）
    r_bg = big * 0.225
    # 文件夹：标签 + 主体
    tab = (big * 0.205, big * 0.275, big * 0.455, big * 0.375, big * 0.028)
    body = (big * 0.170, big * 0.325, big * 0.830, big * 0.745, big * 0.075)
    # 对勾
    p1 = (big * 0.355, big * 0.545)
    p2 = (big * 0.462, big * 0.648)
    p3 = (big * 0.665, big * 0.432)
    half = big * 0.030

    for oy in range(n):
        for ox in range(n):
            acc = [0.0, 0.0, 0.0]
            cover = 0
            for sy in range(SS):
                for sx in range(SS):
                    x = ox * SS + sx + 0.5
                    y = oy * SS + sy + 0.5
                    # 1) 背景渐变 + 圆角遮罩（圆角外保持透明）
                    if not in_round_rect(x, y, 1, 1, big - 1, big - 1, r_bg):
                        continue
                    cover += 1
                    t = y / big
                    col = [
                        BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t for i in range(3)
                    ]
                    # 2) 文件夹
                    if in_round_rect(x, y, *tab) or in_round_rect(x, y, *body):
                        col = list(WHITE)
                        # 3) 对勾（只画在文件夹主体上）
                        if in_round_rect(x, y, *body):
                            d = min(
                                dist_to_segment(x, y, *p1, *p2),
                                dist_to_segment(x, y, *p2, *p3),
                            )
                            if d <= half:
                                col = list(CHECK)
                    acc[0] += col[0]
                    acc[1] += col[1]
                    acc[2] += col[2]
            total = SS * SS
            if cover == 0:
                out += bytes((0, 0, 0, 0))
            else:
                out += bytes(
                    [int(round(acc[i] / cover)) & 0xFF for i in range(3)]
                    + [int(round(cover * 255 / total)) & 0xFF]
                )
    return png_bytes(bytes(out), n, n, 4)


def main():
    targets = [
        (os.path.join(ROOT, "ICON.PNG"), 64),
        (os.path.join(ROOT, "ICON_256.PNG"), 256),
        (os.path.join(ROOT, "app", "ui", "images", "icon_64.png"), 64),
        (os.path.join(ROOT, "app", "ui", "images", "icon_256.png"), 256),
    ]
    for path, size in targets:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = render(size)
        with open(path, "wb") as fh:
            fh.write(data)
        print(f"生成 {path}  {size}x{size}  {len(data)} 字节")


if __name__ == "__main__":
    main()
