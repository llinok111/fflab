"""fflab 自测. 全部为真实执行,不做 mock.

运行:
  python -m unittest discover -s tests -v
或
  python tests/run_tests.py

测试分四组:
  1. Sandbox   路径守卫 / 隔离 / 重置幂等 / 素材不可改写
  2. Measurement 探测数值与已知事实一致(帧数/时长/响度)
  3. Tutorial  正向流程全通过 + 反例必须被拒
  4. Server    HTTP API 端到端,含越界请求
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fflab import runner
from fflab.sandbox import Sandbox, SandboxViolation
from fflab.tutorial import Tutorial, split_command


class TempSandboxCase(unittest.TestCase):
    """每个用例独立练习根目录,互不干扰."""

    @classmethod
    def setUpClass(cls):
        cls._base = Path(tempfile.mkdtemp(prefix="fflab-test-"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._base, ignore_errors=True)

    def new_sandbox(self, name: str) -> Sandbox:
        root = self._base / name
        sb = Sandbox(root)
        sb.init()
        return sb


class TestSandbox(TempSandboxCase):
    def test_path_guard_relative_escape(self):
        sb = self.new_sandbox("guard-rel")
        with self.assertRaises(SandboxViolation):
            sb.resolve("../../evil.mp4")

    def test_path_guard_absolute_escape(self):
        sb = self.new_sandbox("guard-abs")
        outside = (self._base / "outside.mp4").as_posix()
        with self.assertRaises(SandboxViolation):
            sb.resolve(outside)

    def test_path_guard_allows_inside(self):
        sb = self.new_sandbox("guard-in")
        p = sb.resolve("work/a.mp4")
        self.assertTrue(str(p).startswith(str(sb.root)))

    def test_guard_rejects_overwrite_asset(self):
        sb = self.new_sandbox("guard-asset")
        with self.assertRaises(SandboxViolation):
            sb.guard_argv(["ffmpeg", "-i", "assets/clip_alpha.mp4", "-y", "assets/clip_alpha.mp4"])

    def test_guard_rejects_protocol_input(self):
        sb = self.new_sandbox("guard-proto")
        with self.assertRaises(SandboxViolation):
            sb.guard_argv(["ffmpeg", "-i", "https://example.com/a.mp4", "-y", "work/x.mp4"])

    def test_guard_rejects_movie_filter_outside(self):
        sb = self.new_sandbox("guard-movie")
        with self.assertRaises(SandboxViolation):
            sb.guard_argv([
                "ffmpeg", "-filter_complex",
                "movie=C:/Windows/win.ini[out]", "-map", "[out]", "-y", "work/x.mp4",
            ])

    def test_asset_fingerprint_detects_tamper(self):
        sb = self.new_sandbox("tamper")
        before = {m["name"]: m["sha256"] for m in sb.manifest()}
        target = sb.resolve("assets/voiceover.wav")
        target.write_bytes(b"corrupted")
        with self.assertRaises(SandboxViolation):
            sb.assert_untouched_assets(before)

    def test_reset_work_keeps_assets(self):
        sb = self.new_sandbox("reset-work")
        (sb.root / "work" / "junk.mp4").write_bytes(b"x")
        info = sb.reset(scope="work")
        self.assertEqual(info["scope"], "work")
        self.assertFalse((sb.root / "work" / "junk.mp4").exists())
        self.assertTrue((sb.root / "assets" / "clip_alpha.mp4").exists())

    def test_reset_all_rebuilds_assets(self):
        sb = self.new_sandbox("reset-all")
        info = sb.reset(scope="all")
        self.assertEqual(info["scope"], "all")
        for name in ("clip_alpha.mp4", "clip_beta.mp4", "voiceover.wav", "music_bed.wav"):
            self.assertTrue((sb.root / "assets" / name).exists(), name)

    def test_init_is_idempotent(self):
        sb = self.new_sandbox("idem")
        before = {m["name"]: m["sha256"] for m in sb.manifest()}
        out = sb.init()
        self.assertEqual(out["created_assets"], [])
        after = {m["name"]: m["sha256"] for m in sb.manifest()}
        self.assertEqual(before, after)


class TestMeasurement(TempSandboxCase):
    """断言的是 ffmpeg 的确定行为,数值来自实测而非推算."""

    def test_probe_known_facts(self):
        sb = self.new_sandbox("probe")
        a = runner.probe(sb.resolve("assets/clip_alpha.mp4"))
        self.assertEqual((a.width, a.height), (640, 360))
        self.assertAlmostEqual(a.fps, 30.0, places=2)
        self.assertAlmostEqual(a.duration, 4.0, places=1)
        self.assertEqual(a.frame_count, 120)
        self.assertEqual(a.sample_rate, 48000)
        self.assertEqual(a.extra.get("pix_fmt"), "yuv420p")

        b = runner.probe(sb.resolve("assets/clip_beta.mp4"))
        self.assertEqual((b.width, b.height), (1280, 720))
        self.assertAlmostEqual(b.fps, 25.0, places=2)
        self.assertEqual(b.frame_count, 75)

    def test_amix_normalize_drops_about_6db(self):
        """amix 默认 normalize=1 的 6dB 衰减必须真实存在,否则第 3 步教学点不成立."""
        sb = self.new_sandbox("amix")
        cmd = [
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=660:sample_rate=48000:duration=2",
            "-filter_complex", "[0:a][1:a]amix=inputs=2[a]", "-map", "[a]",
            "-c:a", "pcm_s16le", "-y", "work/norm.wav",
        ]
        self.assertEqual(runner.run_ffmpeg(cmd, cwd=sb.root).returncode, 0)
        cmd2 = [c.replace("amix=inputs=2", "amix=inputs=2:normalize=0").replace("norm.wav", "raw.wav") for c in cmd]
        self.assertEqual(runner.run_ffmpeg(cmd2, cwd=sb.root).returncode, 0)

        norm = runner.measure_volume(sb.resolve("work/norm.wav"))
        raw = runner.measure_volume(sb.resolve("work/raw.wav"))
        delta = norm["max_db"] - raw["max_db"]
        self.assertAlmostEqual(delta, -6.0, delta=0.8,
                               msg=f"归一化应衰减约 6dB,实测 {delta}")

    def test_frame_hashes_are_deterministic(self):
        sb = self.new_sandbox("hash")
        a = runner.frame_hashes(sb.resolve("assets/clip_alpha.mp4"))
        b = runner.frame_hashes(sb.resolve("assets/clip_alpha.mp4"))
        self.assertEqual(len(a), 120)
        self.assertEqual(a, b)

    def test_loudness_readable(self):
        sb = self.new_sandbox("loud")
        loud = runner.measure_loudness(sb.resolve("assets/voiceover.wav"))
        self.assertIn("integrated_lufs", loud)
        self.assertLess(loud["integrated_lufs"], 0)


class TestTutorial(TempSandboxCase):
    def test_split_command_keeps_quoted_filtergraph(self):
        toks = split_command('ffmpeg -i a.mp4 -filter_complex "[0:a][1:a]amix=inputs=2:normalize=0[a]" -map "[a]" out.wav')
        self.assertIn("[0:a][1:a]amix=inputs=2:normalize=0[a]", toks)
        self.assertEqual(toks[0], "ffmpeg")

    def test_full_flow_passes(self):
        sb = self.new_sandbox("flow")
        t = Tutorial(sb)
        t.reset_progress()
        for step in t.steps:
            res = t.execute(step, step.hint)
            self.assertTrue(res.ok, f"{step.id} 执行失败: {res.stderr_tail()}")
            verdict = t.verify(step, command=step.hint)
            self.assertTrue(verdict["passed"], f"{step.id} 未通过: {verdict['checks']}")
            t.record(step, verdict, step.hint)
        self.assertTrue(t.all_passed())
        self.assertTrue(sb.resolve("work/final.mp4").exists())

    def test_progress_persists_and_is_relative(self):
        sb = self.new_sandbox("progress")
        t = Tutorial(sb)
        t.reset_progress()
        step = t.steps[0]
        res = t.execute(step, step.hint)
        verdict = t.verify(step, command=step.hint)
        t.record(step, verdict, step.hint)
        raw = sb.layout.progress_file.read_text(encoding="utf-8")
        self.assertNotIn(str(sb.root).replace("\\", "\\\\"), raw, "进度文件不应写入绝对路径")
        reloaded = Tutorial(sb).load_progress()
        self.assertEqual(reloaded["steps"][step.id]["status"], "passed")

    def test_wrong_scale_is_rejected(self):
        """不缩放就交付 => 必须被拒(维度断言)."""
        sb = self.new_sandbox("wrong-scale")
        t = Tutorial(sb)
        step = t.steps[0]
        bad = ("ffmpeg -i assets/clip_beta.mp4 -c:v libx264 -preset veryfast "
               "-an -y work/beta_norm.mp4")
        t.execute(step, bad)
        v = t.verify(step, command=bad)
        self.assertFalse(v["passed"])
        failed = {c["key"] for c in v["checks"] if not c["ok"]}
        self.assertIn("宽", failed)

    def test_amix_without_normalize0_is_rejected(self):
        """默认归一化导致 -6dB => 必须被电平断言拒."""
        sb = self.new_sandbox("wrong-amix")
        t = Tutorial(sb)
        step = next(s for s in t.steps if s.id == "s3-mix")
        bad = ('ffmpeg -i assets/voiceover.wav -i assets/music_bed.wav '
               '-filter_complex "[0:a][1:a]amix=inputs=2[a]" -map "[a]" '
               '-c:a pcm_s16le -y work/mixed.wav')
        t.execute(step, bad)
        v = t.verify(step, command=bad)
        self.assertFalse(v["passed"])
        keys = {c["key"] for c in v["checks"] if not c["ok"]}
        self.assertTrue(any("电平" in k for k in keys), keys)

    def test_shortest_truncation_is_rejected(self):
        """少写 apad 导致成片被截成 4 秒 => 必须被时长断言拒."""
        sb = self.new_sandbox("wrong-shortest")
        t = Tutorial(sb)
        # 先跑到 s3
        for step in t.steps[:3]:
            t.execute(step, step.hint)
        step = next(s for s in t.steps if s.id == "s4-mux")
        bad = ('ffmpeg -i work/joined.mp4 -i work/mixed.wav -map 0:v:0 -map 1:a:0 '
               '-c:v libx264 -preset veryfast -crf 20 -pix_fmt yuv420p '
               '-c:a aac -b:a 160k -shortest -y work/joined_av.mp4')
        t.execute(step, bad)
        v = t.verify(step, command=bad)
        self.assertFalse(v["passed"])
        dur = next(c for c in v["checks"] if c["key"].startswith("时长"))
        self.assertLess(dur["measured"], 5.0)

    def test_shell_metacharacters_rejected(self):
        sb = self.new_sandbox("meta")
        t = Tutorial(sb)
        with self.assertRaises(ValueError):
            t.execute(t.steps[0], "ffmpeg -i a.mp4 -y work/x.mp4 && del assets")

    def test_no_shell_escape_on_stdin_pipe(self):
        """确认 execute 不接受管道符,避免注入到 shell."""
        sb = self.new_sandbox("pipe")
        t = Tutorial(sb)
        with self.assertRaises(ValueError):
            t.execute(t.steps[0], "ffmpeg -i a.mp4 -y work/x.mp4 | cat")


class TestServer(TempSandboxCase):
    def setUp(self):
        import threading
        from http.server import ThreadingHTTPServer

        from fflab.server import make_handler

        # 每个用例独立目录:同名目录会在用例之间残留进度,导致顺序相关的假失败。
        # unittest 每个用例都是新实例,不能用实例属性计数,必须用方法名作后缀。
        self.sb = self.new_sandbox(f"server-{self._testMethodName}")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.sb, Tutorial(self.sb)))
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.port}"
        self.t = Tutorial(self.sb)

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def _get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))

    def _post(self, path, payload):
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_index_served(self):
        with urllib.request.urlopen(self.base + "/", timeout=10) as r:
            body = r.read().decode("utf-8")
        self.assertEqual(r.status, 200)
        self.assertIn("fflab", body)

    def test_state_shape(self):
        d = self._get("/api/state")
        self.assertTrue(d["ok"])
        self.assertEqual(len(d["steps"]), 5)
        self.assertEqual(d["next_step"], "s1-unify")

    def test_run_endpoint_full_flow(self):
        for step in self.t.steps:
            status, body = self._post("/api/run", {"step": step.id, "command": step.hint})
            self.assertEqual(status, 200, body)
            self.assertTrue(body["verdict"]["passed"], body["verdict"])
        d = self._get("/api/state")
        self.assertTrue(d["completed"])

    def test_run_rejects_escape_path(self):
        status, body = self._post(
            "/api/run", {"step": "s1-unify", "command": "ffmpeg -i assets/clip_beta.mp4 -y ../../evil.mp4"}
        )
        self.assertEqual(status, 403)
        self.assertFalse(body["ok"])
        self.assertIn("越界", body["error"])

    def test_reset_endpoint(self):
        self._post("/api/run", {"step": "s1-unify", "command": self.t.steps[0].hint})
        status, body = self._post("/api/reset", {"scope": "work"})
        self.assertEqual(status, 200)
        self.assertFalse(body["completed"])
        self.assertFalse(self.sb.resolve("work/beta_norm.mp4").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
