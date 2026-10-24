"""命令行入口.

  python -m fflab.cli serve              启动交互界面
  python -m fflab.cli status             查看进度
  python -m fflab.cli run <step> --cmd "..."   单步执行并验证
  python -m fflab.cli demo               自动跑完整个教学流程(自检用)
  python -m fflab.cli reset [--all]      重置练习区
"""

from __future__ import annotations

import argparse
import json
import sys

from .sandbox import Sandbox
from .tutorial import Tutorial


def _sb(args) -> Sandbox:
    return Sandbox(args.root)


def cmd_init(args) -> int:
    sb = _sb(args)
    info = sb.init(rebuild=args.rebuild)
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


def cmd_status(args) -> int:
    sb = _sb(args)
    sb.init()
    t = Tutorial(sb)
    prog = t.load_progress()
    for s in t.steps:
        st = prog["steps"].get(s.id, {})
        mark = {"passed": "[通过]", "attempted": "[尝试过]"}.get(st.get("status"), "[待完成]")
        print(f"{mark} {s.id}  {s.title}  (尝试 {st.get('attempts', 0)} 次)")
    print(f"\n全部完成: {t.all_passed()}")
    if not t.all_passed():
        nxt = t.next_step()
        print(f"下一步: {nxt.id}")
    return 0


def cmd_run(args) -> int:
    sb = _sb(args)
    sb.init()
    t = Tutorial(sb)
    step = next((s for s in t.steps if s.id == args.step), None)
    if step is None:
        print(f"未知步骤: {args.step}", file=sys.stderr)
        return 2
    command = args.cmd or step.hint
    if args.hint:
        print(f"示例命令:\n  {step.hint}\n")
    res = t.execute(step, command)
    verdict = t.verify(step, command=command)
    t.record(step, verdict, command)
    print(f"退出码: {res.returncode}")
    if not res.ok:
        print(res.stderr_tail(20))
    print()
    for c in verdict["checks"]:
        print(("  [OK]   " if c["ok"] else "  [FAIL] ") + f"{c['key']} | {c['detail']}")
    print(f"\n结果: {'通过' if verdict['passed'] else '未通过'}")
    return 0 if verdict["passed"] else 1


def cmd_reset(args) -> int:
    sb = _sb(args)
    sb.init()
    info = sb.reset(scope="all" if args.all else "work")
    if not args.keep_progress:
        Tutorial(sb).reset_progress()
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


def cmd_demo(args) -> int:
    """把整条教学流程用示例命令跑一遍,用于回归自检."""
    sb = _sb(args)
    sb.reset(scope="work")
    sb.init()
    t = Tutorial(sb)
    t.reset_progress()
    failures = 0
    for s in t.steps:
        res = t.execute(s, s.hint)
        verdict = t.verify(s, command=s.hint)
        t.record(s, verdict, s.hint)
        status = "PASS" if verdict["passed"] and res.ok else "FAIL"
        print(f"{status}  {s.id}  rc={res.returncode}")
        for c in verdict["checks"]:
            if not c["ok"]:
                failures += 1
                print(f"      [FAIL] {c['key']} | {c['detail']}")
        if not res.ok:
            failures += 1
            print("      " + res.stderr_tail(6).replace("\n", "\n      "))
    print(f"\n最终: {'全流程通过' if t.all_passed() and failures == 0 else '存在失败项'}")
    return 0 if t.all_passed() and failures == 0 else 1


def cmd_serve(args) -> int:
    from .server import serve

    sb = _sb(args)
    sb.init()
    serve(sb, Tutorial(sb), host=args.host, port=args.port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fflab", description="FFmpeg filtergraph 交互式练习台")
    p.add_argument("--root", default="workspace", help="练习根目录(默认 ./workspace)")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("init", help="建立目录并合成素材")
    sp.add_argument("--rebuild", action="store_true", help="强制重建素材")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("status", help="查看进度")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("run", help="执行某一步并验证")
    sp.add_argument("step")
    sp.add_argument("--cmd", default=None, help="要执行的 ffmpeg 命令;省略则用示例命令")
    sp.add_argument("--hint", action="store_true", help="同时打印示例命令")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("reset", help="重置练习区")
    sp.add_argument("--all", action="store_true", help="连素材一起重建")
    sp.add_argument("--keep-progress", action="store_true", help="保留进度记录")
    sp.set_defaults(func=cmd_reset)

    sp = sub.add_parser("demo", help="自动跑完整流程(自检)")
    sp.set_defaults(func=cmd_demo)

    sp = sub.add_parser("serve", help="启动本地交互界面")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8765)
    sp.set_defaults(func=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
