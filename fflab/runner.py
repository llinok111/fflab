"""执行 ffmpeg 并采集机器可读度量.

关键取舍:本机 PATH 里只有 ffmpeg.exe,没有 ffprobe.exe。
因此所有探测都走 `ffmpeg -i <file> -f null -` 的输入转储 + `-progress pipe:1`,
不依赖 ffprobe。这一点在 docs/DESIGN.md 里有完整说明与验证方式。
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

FFMPEG = "ffmpeg"

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)")
_VIDEO_RE = re.compile(r"Stream #\d+:\d+.*?: Video: (\w+).*?, (\d+)x(\d+)")
_PIXFMT_RE = re.compile(r"\b(yuv\w+|rgb\w*|gray|gbrp\w*)\b")
_FPS_RE = re.compile(r",\s*([\d.]+) fps")
_AUDIO_RE = re.compile(r"Stream #\d+:\d+.*?: Audio: (\w+).*?, (\d+) Hz, (\w+)")
_LOUDNESS_RE = re.compile(r"I:\s*(-?[\d.]+) LUFS")
_MEANVOL_RE = re.compile(r"mean_volume:\s*(-?[\d.]+) dB")
_MAXVOL_RE = re.compile(r"max_volume:\s*(-?[\d.]+) dB")


@dataclass
class RunResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

    def stderr_tail(self, n: int = 12) -> str:
        lines = [ln for ln in self.stderr.replace("\r", "\n").split("\n") if ln.strip()]
        return "\n".join(lines[-n:])

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


@dataclass
class MediaInfo:
    """一段媒体的事实描述. 字段缺失即 None,不猜默认值."""

    duration: float | None = None
    video_codec: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    audio_codec: str | None = None
    sample_rate: int | None = None
    channels: str | None = None
    frame_count: int | None = None
    extra: dict = field(default_factory=dict)

    @property
    def has_video(self) -> bool:
        return self.width is not None

    @property
    def has_audio(self) -> bool:
        return self.audio_codec is not None


def run_ffmpeg(
    args: list[str],
    *,
    cwd: str | Path | None = None,
    timeout: int = 180,
    ffmpeg_bin: str = FFMPEG,
) -> RunResult:
    """执行一次 ffmpeg. 强制 -hide_banner,统一 UTF-8 解码."""
    argv = [ffmpeg_bin, "-hide_banner", *args]
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            timeout=timeout,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        return RunResult(
            argv=argv,
            returncode=-1,
            stdout=(exc.stdout or "") if isinstance(exc.stdout, str) else "",
            stderr=(exc.stderr or "") if isinstance(exc.stderr, str) else "",
            timed_out=True,
        )
    return RunResult(argv=argv, returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


def probe(path: str | Path, *, cwd: str | Path | None = None) -> MediaInfo:
    """用输入转储 + -progress 拿事实,不依赖 ffprobe."""
    path = Path(path)
    info = MediaInfo()

    # 1) 输入转储:codec / 分辨率 / 帧率 / 采样率 / 容器时长
    dump = run_ffmpeg(["-i", str(path), "-f", "null", "-"], cwd=cwd)
    text = dump.stderr.replace("\r", "\n")
    if "Invalid data found" in text or "No such file" in text:
        raise FileNotFoundError(f"无法读取媒体: {path}")

    m = _DURATION_RE.search(text)
    if m:
        h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
        info.duration = round(h * 3600 + mi * 60 + s, 3)

    m = _VIDEO_RE.search(text)
    if m:
        info.video_codec = m.group(1)
        info.width = int(m.group(2))
        info.height = int(m.group(3))
        # 像素格式位于 codec 名之后、分辨率之前:
        # "Video: h264 (High) (avc1 / 0x31637661), yuv420p(progressive), 640x360 ..."
        # 因此取 Video: 所在行按逗号切分后的第 2 段,不能按分辨率位置反查。
        vline = next((ln for ln in text.split("\n") if "Video:" in ln), "")
        parts = vline.split(",")
        if len(parts) >= 2:
            pm = re.match(r"\s*([A-Za-z0-9]+)", parts[1])
            info.extra["pix_fmt"] = pm.group(1) if pm else None
        fm = _FPS_RE.search(text)
        if fm:
            info.fps = round(float(fm.group(1)), 3)

    m = _AUDIO_RE.search(text)
    if m:
        info.audio_codec = m.group(1)
        info.sample_rate = int(m.group(2))
        info.channels = m.group(3)

    # 2) 帧数走 -progress(结构化 key=value,比解析人类可读日志稳)
    if info.has_video:
        prog = run_ffmpeg(
            ["-nostats", "-loglevel", "error", "-i", str(path),
             "-map", "0:v:0", "-f", "null", "-", "-progress", "pipe:1"],
            cwd=cwd,
        )
        frames = re.findall(r"^frame=(\d+)$", prog.stdout, re.M)
        if frames:
            info.frame_count = int(frames[-1])
    return info


def measure_loudness(path: str | Path, *, cwd: str | Path | None = None) -> dict:
    """EBU R128 综合响度 + 峰值. 用于音频电平类断言."""
    res = run_ffmpeg(
        ["-nostats", "-i", str(path), "-af", "ebur128=framelog=quiet", "-f", "null", "-"],
        cwd=cwd,
    )
    text = res.stderr.replace("\r", "\n")
    out: dict = {}
    m = _LOUDNESS_RE.search(text)
    if m:
        out["integrated_lufs"] = float(m.group(1))
    return out


def measure_volume(path: str | Path, *, cwd: str | Path | None = None) -> dict:
    """volumedetect 的 mean/max dB. 比响度更适合"相对衰减"这类断言."""
    res = run_ffmpeg(
        ["-nostats", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        cwd=cwd,
    )
    text = res.stderr.replace("\r", "\n")
    out: dict = {}
    m = _MEANVOL_RE.search(text)
    if m:
        out["mean_db"] = float(m.group(1))
    m = _MAXVOL_RE.search(text)
    if m:
        out["max_db"] = float(m.group(1))
    return out


def frame_hashes(path: str | Path, *, cwd: str | Path | None = None, limit: int = 0) -> list[str]:
    """解码后逐帧 MD5. 内容级指纹,与容器/编码参数无关."""
    args = ["-loglevel", "error", "-i", str(path), "-map", "0:v:0", "-f", "framemd5", "-"]
    res = run_ffmpeg(args, cwd=cwd)
    hashes = []
    for line in res.stdout.replace("\r", "\n").split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 6:
            hashes.append(parts[-1])
    return hashes[:limit] if limit else hashes
