"""PNG 像素统计:用来证明截图不是空帧/白屏/半渲染.

本机读图链不可用(既有结论),所以视觉证据改用可复算的数值:
尺寸、去重色数、平均亮度、分带标准差。四条阈值同时卡住常见故障:
  * 全空/全同色   -> 去重色数 ≈ 1
  * 白屏/黑屏     -> 平均亮度贴边 + 分带 stdev ≈ 0
  * 半渲染(上半有内容下半空白) -> 分带 stdev 落差过大
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path


def read_png(path: Path) -> tuple[int, int, list[tuple[int, int, int]]]:
    raw = path.read_bytes()
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"不是 PNG: {path}")
    pos = 8
    width = height = None
    bit_depth = color_type = None
    idat = bytearray()
    while pos < len(raw):
        length = struct.unpack(">I", raw[pos:pos + 4])[0]
        ctype = raw[pos + 4:pos + 8]
        data = raw[pos + 8:pos + 8 + length]
        if ctype == b"IHDR":
            width, height, bit_depth, color_type = struct.unpack(">IIBB", data[:10])
        elif ctype == b"IDAT":
            idat += data
        elif ctype == b"IEND":
            break
        pos += 12 + length

    if bit_depth != 8:
        raise ValueError(f"只支持 8 位深度,实际 {bit_depth}")
    channels = {0: 1, 2: 3, 4: 2, 6: 4}[color_type]
    buf = zlib.decompress(bytes(idat))
    stride = width * channels
    pixels: list[tuple[int, int, int]] = []
    prev = bytearray(stride)
    off = 0
    for _ in range(height):
        f = buf[off]
        off += 1
        line = bytearray(buf[off:off + stride])
        off += stride
        # 反滤波:PNG 的五种行滤波
        for i in range(stride):
            a = line[i - channels] if i >= channels else 0
            b = prev[i]
            c = prev[i - channels] if i >= channels else 0
            if f == 1:
                line[i] = (line[i] + a) & 0xFF
            elif f == 2:
                line[i] = (line[i] + b) & 0xFF
            elif f == 3:
                line[i] = (line[i] + ((a + b) >> 1)) & 0xFF
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        for x in range(0, stride, channels):
            if channels >= 3:
                pixels.append((line[x], line[x + 1], line[x + 2]))
            else:
                g = line[x]
                pixels.append((g, g, g))
        prev = line
    return width, height, pixels


def stats(path: Path) -> dict:
    w, h, px = read_png(path)
    total = len(px)
    uniq = len(set(px))
    lum = [0.2126 * r + 0.7152 * g + 0.0722 * b for r, g, b in px]
    mean = sum(lum) / total
    bands = 10
    band_means = []
    per = total // bands
    for i in range(bands):
        seg = lum[i * per:(i + 1) * per]
        band_means.append(sum(seg) / len(seg) if seg else 0.0)
    overall = sum(band_means) / len(band_means)
    var = sum((m - overall) ** 2 for m in band_means) / len(band_means)
    return {
        "file": path.name,
        "size": [w, h],
        "unique_colors": uniq,
        "mean_luma": round(mean, 2),
        "band_luma_stdev": round(var ** 0.5, 2),
        "band_means": [round(m, 1) for m in band_means],
    }


THRESHOLDS = {
    "min_unique_colors": 40,     # 真界面必然有大量抗锯齿过渡色
    "min_mean_luma": 12.0,       # 不能是纯黑
    "max_mean_luma": 245.0,      # 不能是纯白
    "min_band_stdev": 1.0,       # 各横向分带必须有差异(纯色块会接近 0)
    "min_width": 1000,
    "min_height": 600,
}


def judge(s: dict) -> list[str]:
    fails = []
    t = THRESHOLDS
    if s["unique_colors"] < t["min_unique_colors"]:
        fails.append(f"去重色数 {s['unique_colors']} < {t['min_unique_colors']}")
    if not (t["min_mean_luma"] <= s["mean_luma"] <= t["max_mean_luma"]):
        fails.append(f"平均亮度 {s['mean_luma']} 越界")
    if s["band_luma_stdev"] < t["min_band_stdev"]:
        fails.append(f"分带标准差 {s['band_luma_stdev']} < {t['min_band_stdev']}(疑似纯色/半渲染)")
    if s["size"][0] < t["min_width"] or s["size"][1] < t["min_height"]:
        fails.append(f"尺寸 {s['size']} 过小")
    return fails


def main(argv: list[str]) -> int:
    if not argv:
        print("用法: python tools/png_stats.py <png> [...]")
        return 2
    bad = 0
    for name in argv:
        p = Path(name)
        s = stats(p)
        fails = judge(s)
        tag = "OK  " if not fails else "FAIL"
        print(f"{tag} {s['file']}  {s['size'][0]}x{s['size'][1]}  "
              f"色数={s['unique_colors']}  亮度={s['mean_luma']}  分带σ={s['band_luma_stdev']}")
        for f in fails:
            print(f"       -> {f}")
        bad += len(fails)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
