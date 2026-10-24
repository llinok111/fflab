"""本地交互服务:HTTP API + 单页界面.

零第三方依赖,只用标准库。绑定 127.0.0.1,不对外网开放;
所有文件操作经由 Sandbox 守卫,API 不接受绝对路径。
"""

from __future__ import annotations

import json
import mimetypes
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .sandbox import Sandbox, SandboxViolation
from .tutorial import Tutorial

WEB_ROOT = Path(__file__).parent / "web"


def make_handler(sandbox: Sandbox, tutorial: Tutorial):
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "fflab/1.0"

        def log_message(self, fmt, *args):  # 静默,避免污染终端
            pass

        # ---- 基础响应 -------------------------------------------
        def _json(self, payload, status=HTTPStatus.OK):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _error(self, message, status=HTTPStatus.BAD_REQUEST, **extra):
            self._json({"ok": False, "error": message, **extra}, status)

        def _static(self, rel: str):
            target = (WEB_ROOT / rel).resolve()
            try:
                target.relative_to(WEB_ROOT.resolve())
            except ValueError:
                return self._error("非法路径", HTTPStatus.FORBIDDEN)
            if not target.is_file():
                return self._error("未找到资源", HTTPStatus.NOT_FOUND)
            data = target.read_bytes()
            ctype, _ = mimetypes.guess_type(str(target))
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", ctype or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        # ---- 路由 -----------------------------------------------
        def do_GET(self):
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                return self._static("index.html")
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if path == "/api/state":
                return self._json(self._state())
            return self._error("未知路径", HTTPStatus.NOT_FOUND)

        def do_POST(self):
            path = urlparse(self.path).path
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return self._error("请求体不是合法 JSON")

            try:
                if path == "/api/run":
                    return self._json(self._run(payload))
                if path == "/api/reset":
                    return self._json(self._reset(payload))
                if path == "/api/reset-progress":
                    with lock:
                        tutorial.reset_progress()
                    return self._json({"ok": True, **self._state()})
            except SandboxViolation as exc:
                return self._error(str(exc), HTTPStatus.FORBIDDEN)
            except (ValueError, FileNotFoundError) as exc:
                return self._error(str(exc))
            return self._error("未知路径", HTTPStatus.NOT_FOUND)

        # ---- 业务 -----------------------------------------------
        def _state(self):
            prog = tutorial.load_progress()
            nxt = tutorial.next_step()
            return {
                "ok": True,
                "root": str(sandbox.root),
                "steps": [
                    {
                        "id": s.id,
                        "title": s.title,
                        "why": s.why,
                        "task": s.task,
                        "outputs": s.outputs,
                        "insight": s.insight,
                        "status": prog["steps"].get(s.id, {}).get("status", "pending"),
                        "attempts": prog["steps"].get(s.id, {}).get("attempts", 0),
                        "first_passed_at": prog["steps"].get(s.id, {}).get("first_passed_at"),
                        "hint_available": True,
                    }
                    for s in tutorial.steps
                ],
                "next_step": nxt.id if nxt else None,
                "completed": tutorial.all_passed(),
                "assets": sandbox.manifest(),
            }

        def _run(self, payload: dict):
            step_id = payload.get("step")
            command = (payload.get("command") or "").strip()
            reveal = bool(payload.get("reveal_hint"))
            step = next((s for s in tutorial.steps if s.id == step_id), None)
            if step is None:
                raise ValueError(f"未知步骤: {step_id}")
            if not command:
                raise ValueError("命令为空")
            with lock:
                result = tutorial.execute(step, command)
                verdict = tutorial.verify(step, command=command)
                entry = tutorial.record(step, verdict, command)
            return {
                "ok": True,
                "step": step.id,
                "returncode": result.returncode,
                "timed_out": result.timed_out,
                "stdout": result.stdout[-4000:],
                "stderr": result.stderr[-8000:],
                "verdict": verdict,
                "progress": entry,
                "hint": step.hint if reveal else None,
                "state": {
                    "completed": tutorial.all_passed(),
                    "next_step": (tutorial.next_step().id if tutorial.next_step() else None),
                },
            }

        def _reset(self, payload: dict):
            scope = payload.get("scope", "work")
            with lock:
                info = sandbox.reset(scope=scope)
                tutorial.reset_progress()
            return {"ok": True, "reset": info, **self._state()}

    return Handler


def serve(sandbox: Sandbox, tutorial: Tutorial, host: str = "127.0.0.1", port: int = 8765) -> None:
    httpd = ThreadingHTTPServer((host, port), make_handler(sandbox, tutorial))
    print(f"fflab 已启动: http://{host}:{port}/")
    print(f"练习根目录: {sandbox.root}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
