"""沙箱:一切写入都限制在练习根目录内,并提供重建与重置.

设计约束(全部来自真实踩坑,见 docs/DESIGN.md):
  * 路径守卫必须解析符号链接/相对段后再比对,不能只做字符串前缀比较。
  * 素材重建是幂等的,且用固定种子与固定编码参数,保证重置后度量值可复现。
  * 学员的产物目录与素材目录分离,重置时可以只清产物、保留素材缓存。
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

# 素材配方:参数全部写死,不用随机数,确保任何机器上重建结果一致。
# duration/rate/size 是教学度量(帧数、时长、分辨率)的唯一来源。
ASSET_RECIPES = {
    "clip_alpha.mp4": dict(
        video="testsrc2=size=640x360:rate=30:duration=4",
        audio="sine=frequency=440:sample_rate=48000:duration=4",
        note="4 秒 / 640x360 / 30fps / 单声道 48kHz",
    ),
    "clip_beta.mp4": dict(
        video="testsrc2=size=1280x720:rate=25:duration=3",
        audio="sine=frequency=880:sample_rate=44100:duration=3",
        note="3 秒 / 1280x720 / 25fps —— 与 alpha 参数不一致,是拼接教学的反例素材",
    ),
    "voiceover.wav": dict(
        video=None,
        audio="sine=frequency=220:sample_rate=48000:duration=4,volume=0.5",
        note="4 秒旁白轨,电平刻意压到 0.5 用于混音电平教学",
    ),
    "music_bed.wav": dict(
        video=None,
        audio="sine=frequency=330:sample_rate=48000:duration=4,volume=0.08",
        note="4 秒背景乐,电平极低,用于演示 amix 归一化陷阱",
    ),
}


class SandboxViolation(RuntimeError):
    """试图越过练习根目录。"""


@dataclass(frozen=True)
class SandboxLayout:
    root: Path

    @property
    def assets(self) -> Path:
        return self.root / "assets"

    @property
    def work(self) -> Path:
        return self.root / "work"

    @property
    def state(self) -> Path:
        return self.root / ".fflab"

    @property
    def progress_file(self) -> Path:
        return self.state / "progress.json"

    @property
    def log_dir(self) -> Path:
        return self.state / "logs"


class Sandbox:
    """练习根目录的守卫与生命周期管理."""

    def __init__(self, root: str | os.PathLike[str]):
        self.layout = SandboxLayout(Path(root).expanduser())
        self.root = self.layout.root.resolve()

    # ---- 路径守卫 -------------------------------------------------
    def resolve(self, candidate: str | os.PathLike[str], *, must_exist: bool = False) -> Path:
        """把任意用户输入解析成根目录内的绝对路径,越界即抛异常."""
        raw = Path(candidate).expanduser()
        if not raw.is_absolute():
            raw = self.root / raw
        # resolve() 会展开 .. 与符号链接;父目录可能还不存在,用 strict=False。
        resolved = raw.resolve(strict=False)
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise SandboxViolation(
                f"路径越界: {resolved} 不在练习根目录 {self.root} 之内"
            ) from exc
        if must_exist and not resolved.exists():
            raise FileNotFoundError(f"文件不存在: {resolved}")
        return resolved

    def relative(self, path: Path) -> str:
        """用于日志与进度记录的稳定相对表示(永远用 /,跨平台一致)."""
        return path.resolve().relative_to(self.root).as_posix()

    # ---- 命令级守卫 -----------------------------------------------
    def guard_argv(self, argv: list[str]) -> None:
        """在沙箱内执行学员命令前的最后一道闸.

        覆盖四类真实风险(每类都有对应测试):
          1. 写/读练习根目录之外的路径(-y ../../x.mp4、C:/Windows/Temp/x.mp4)
          2. 覆写素材文件(assets/ 是只读区,改坏了后续步骤全错)
          3. movie= / amovie= 滤镜从外部路径取帧
          4. 协议前缀(http:、concat: 等)绕过本地路径检查
        """
        read_targets: set[int] = set()
        for i, token in enumerate(argv):
            if token in ("-i",):
                # -i 的下一个 token 是读取目标
                if i + 1 < len(argv):
                    read_targets.add(i + 1)

        for i, token in enumerate(argv):
            low = token.lower()
            # 协议前缀一律拒绝(网络/拼接协议不是本练习的教学内容)
            if re.match(r"^[a-z][a-z0-9+.\-]*://", low) or low.startswith(("concat:", "subfile:", "movie:", "amovie:")):
                raise SandboxViolation(f"不接受协议式输入: {token}")

            # 滤镜内部的文件读取:movie= / amovie=
            # 注意:候选值可能以 Windows 盘符开头(C:/...),盘符后的冒号不能当分隔符,
            # 否则只会取出 "C" 并被当作沙箱内相对路径而漏过。
            for m in re.finditer(r"\b(?:a?movie)=((?:[A-Za-z]:[\\/])?[^\][,:]+)", token):
                candidate = m.group(1).strip("'\"")
                if candidate:
                    self.resolve(candidate)

            if not looks_like_path(token):
                continue
            resolved = self.resolve(token)
            if i not in read_targets:
                try:
                    resolved.relative_to(self.layout.assets)
                    raise SandboxViolation(
                        f"素材区只读,不能作为输出: {self.relative(resolved)}"
                    )
                except ValueError:
                    pass

    def assert_untouched_assets(self, before: dict[str, str]) -> None:
        """执行后复核素材未被改写(写操作可能绕过 argv 守卫,例如 concat 列表).

        before 里可能是截断的指纹,统一按前缀比较,避免长度不一致造成误判。
        """
        for name, digest in before.items():
            current = self.layout.assets / name
            if not current.exists():
                raise SandboxViolation(f"素材被删除: {name}")
            if not sha256_file(current).startswith(digest):
                raise SandboxViolation(f"素材被改写: {name}")

    # ---- 生命周期 -------------------------------------------------
    def init(self, *, rebuild: bool = False) -> dict:
        """建立目录结构并按需生成素材. 幂等."""
        for d in (self.layout.assets, self.layout.work, self.layout.state, self.layout.log_dir):
            d.mkdir(parents=True, exist_ok=True)
        created = []
        for name, recipe in ASSET_RECIPES.items():
            target = self.layout.assets / name
            if target.exists() and not rebuild:
                continue
            build_asset(self, name, recipe)
            created.append(name)
        return {"root": str(self.root), "created_assets": created}

    def reset(self, *, scope: str = "work") -> dict:
        """重置练习区.

        scope="work"  只清学员产物,素材保留(快,日常用)
        scope="all"   连素材一起清后重建(验证隔离性时用)
        """
        if scope not in {"work", "all"}:
            raise ValueError("scope 只能是 'work' 或 'all'")
        removed = 0
        if self.layout.work.exists():
            for child in self.layout.work.iterdir():
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
                removed += 1
        self.layout.work.mkdir(parents=True, exist_ok=True)

        if scope == "all":
            for child in list(self.layout.assets.iterdir()) if self.layout.assets.exists() else []:
                child.unlink()
                removed += 1
            self.init(rebuild=True)
        return {"scope": scope, "removed_entries": removed}

    def manifest(self) -> list[dict]:
        """素材清单,含尺寸与 sha256,用于文档与隔离自检."""
        out = []
        for p in sorted(self.layout.assets.glob("*")):
            if not p.is_file():
                continue
            out.append(
                {
                    "name": p.name,
                    "bytes": p.stat().st_size,
                    "sha256": sha256_file(p)[:16],
                }
            )
        return out


MEDIA_EXTS = (".mp4", ".wav", ".mkv", ".mov", ".m4a", ".aac", ".txt", ".json", ".log")


def looks_like_path(token: str) -> bool:
    """判断一个 argv token 是否应按路径处理.

    判据:含路径分隔符,或带已知扩展名。纯数字/滤镜表达式不会被误判。
    """
    if not token or token.startswith("-"):
        return False
    if "/" in token or "\\" in token:
        return True
    low = token.lower()
    return any(low.endswith(ext) for ext in MEDIA_EXTS)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_asset(sandbox: Sandbox, name: str, recipe: dict) -> Path:
    """用固定参数合成一段素材. 参数固定 => 可复现."""
    from .runner import run_ffmpeg  # 延迟导入,避免循环

    target = sandbox.resolve(f"assets/{name}")
    args: list[str] = []
    if recipe["video"]:
        args += ["-f", "lavfi", "-i", recipe["video"]]
    args += ["-f", "lavfi", "-i", recipe["audio"]]

    if name.endswith(".mp4"):
        args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                 "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-shortest"]
    else:
        args += ["-c:a", "pcm_s16le"]
    args += ["-y", str(target)]

    result = run_ffmpeg(args, cwd=sandbox.root)
    if result.returncode != 0:
        raise RuntimeError(f"素材合成失败 {name}: {result.stderr_tail()}")
    return target
