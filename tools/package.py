"""打包 ZIP 并做独立解压验证.

为什么不用 Compress-Archive:它静默丢弃隐藏文件(.gitignore),已在既往项目踩过。
为什么不用 Add-Type/System.IO.Compression:安全策略会拦。
本脚本用标准库 zipfile + os.walk,并把 os.sep 规范成 "/"。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT.parent / "deliver-fflab"
PKG_NAME = "fflab"

EXCLUDE_DIRS = {"__pycache__", "workspace", "_run", ".git"}
EXCLUDE_FILES = {".DS_Store", "Thumbs.db"}
EXCLUDE_SUFFIX = {".pyc", ".pyo"}


def collect(base: Path) -> list[Path]:
    files = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDE_DIRS)
        for name in sorted(filenames):
            if name in EXCLUDE_FILES or Path(name).suffix in EXCLUDE_SUFFIX:
                continue
            files.append(Path(dirpath) / name)
    return files


def build(version: str) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    target = OUT_DIR / f"{PKG_NAME}-{version}.zip"
    if target.exists():
        target.unlink()
    files = collect(ROOT)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for f in files:
            arc = (Path(PKG_NAME) / f.relative_to(ROOT)).as_posix()
            zf.write(f, arc)
    return target


def verify(zip_path: Path) -> dict:
    """解压到干净目录,独立重装并跑测试,不接受"原目录能跑"当证据."""
    tmp = Path(os.environ.get("TEMP", "/tmp")) / f"fflab-verify-{os.getpid()}"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    with zipfile.ZipFile(zip_path) as zf:
        bad = zf.testzip()
        names = zf.namelist()
        zf.extractall(tmp)

    pkg = tmp / PKG_NAME
    hidden = [n for n in names if Path(n).name.startswith(".")]
    result = {
        "entries": len(names),
        "bad_member": bad,
        "hidden_included": hidden,
        "extract_root": str(pkg),
    }

    # 独立目录里跑单元测试
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
        cwd=pkg, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    result["test_rc"] = proc.returncode
    result["test_tail"] = proc.stderr.strip().split("\n")[-3:]

    # 独立目录里跑全流程
    proc2 = subprocess.run(
        [sys.executable, "-m", "fflab.cli", "demo"],
        cwd=pkg, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    result["demo_rc"] = proc2.returncode
    result["demo_tail"] = proc2.stdout.strip().split("\n")[-3:]
    result["final_exists"] = (pkg / "workspace" / "work" / "final.mp4").exists()

    sha = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    result["zip_sha256"] = sha
    result["zip_bytes"] = zip_path.stat().st_size
    shutil.rmtree(tmp, ignore_errors=True)
    return result


def main() -> int:
    version = "1.0.0"
    z = build(version)
    r = verify(z)
    print(f"ZIP: {z}")
    print(f"大小: {r['zip_bytes']:,} 字节")
    print(f"条目: {r['entries']}  损坏成员: {r['bad_member']}")
    print(f"隐藏文件已包含: {r['hidden_included']}")
    print(f"独立解压后单元测试 rc={r['test_rc']}  {r['test_tail']}")
    print(f"独立解压后全流程   rc={r['demo_rc']}  {r['demo_tail']}")
    print(f"成片产出: {r['final_exists']}")
    print(f"sha256: {r['zip_sha256']}")
    ok = r["test_rc"] == 0 and r["demo_rc"] == 0 and r["final_exists"] and r["bad_member"] is None
    print("结论：" + ("打包与独立验证通过" if ok else "存在失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
