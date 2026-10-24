"""教程引擎:步骤定义 / 命令校验 / 结果断言 / 进度记录.

三条设计原则:
  1. 断言只读测量值,不读"看起来对"的日志文本 —— 因此每个断言都能被第三方复算。
  2. 命令校验与产物校验分开:命令形态错了要立刻指出来,产物不对要指出差在哪。
  3. 进度落盘为可读 JSON,不含绝对路径,换机器可继续。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import runner
from .sandbox import Sandbox, SandboxViolation

# ---------------------------------------------------------------- 断言原语


@dataclass
class Check:
    """一条可独立复算的断言."""

    key: str            # 人类可读的检查项名
    ok: bool
    detail: str         # 实测值 vs 期望值
    measured: Any = None


def _fmt(v):
    return "缺失" if v is None else (f"{v:.3f}".rstrip("0").rstrip(".") if isinstance(v, float) else str(v))


def check_eq(key: str, measured, expected, *, tol: float | None = None) -> Check:
    if measured is None:
        return Check(key, False, f"实测缺失,期望 {_fmt(expected)}", None)
    if tol is not None:
        ok = abs(float(measured) - float(expected)) <= tol
        return Check(key, ok, f"实测 {_fmt(measured)} / 期望 {_fmt(expected)} ±{tol}", measured)
    ok = str(measured) == str(expected)
    return Check(key, ok, f"实测 {_fmt(measured)} / 期望 {_fmt(expected)}", measured)


def check_between(key: str, measured, lo: float, hi: float) -> Check:
    if measured is None:
        return Check(key, False, f"实测缺失,期望落在 [{lo}, {hi}]", None)
    ok = lo <= float(measured) <= hi
    return Check(key, ok, f"实测 {_fmt(measured)} / 期望落在 [{lo}, {hi}]", measured)


def check_le(key: str, measured, limit: float) -> Check:
    """单向上限断言:只需不超过某值(如峰值不削顶)."""
    if measured is None:
        return Check(key, False, f"实测缺失,期望 ≤ {limit}", None)
    ok = float(measured) <= limit
    return Check(key, ok, f"实测 {_fmt(measured)} / 期望 ≤ {limit}", measured)


# ---------------------------------------------------------------- 步骤模型


@dataclass
class Step:
    id: str
    title: str
    why: str                      # 为什么需要这一步(教学动机)
    task: str                     # 要做什么
    hint: str                     # 示例输入(按需展示)
    outputs: list[str]            # 期望产出的相对路径(相对练习根目录)
    # 命令形态校验:正则列表,每条都必须命中
    must_match: list[str] = field(default_factory=list)
    # 命令中禁止出现的写法(用来逼学员避开错误捷径)
    must_not_match: list[str] = field(default_factory=list)
    # 产物断言:接收 (sandbox, step) -> list[Check]
    assertions: Callable[[Sandbox, "Step"], list[Check]] | None = None
    # 完成这一步之后向学员揭示的结论
    insight: str = ""


class Tutorial:
    def __init__(self, sandbox: Sandbox):
        self.sandbox = sandbox
        self.steps: list[Step] = build_steps()

    # ---- 进度读写 -------------------------------------------------
    def load_progress(self) -> dict:
        p = self.sandbox.layout.progress_file
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        return {"tutorial": "unify-and-mux", "version": 1, "steps": {}}

    def save_progress(self, data: dict) -> None:
        p = self.sandbox.layout.progress_file
        p.parent.mkdir(parents=True, exist_ok=True)
        # 原子写:先写临时文件再替换,避免中断留下半个 JSON。
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)

    def reset_progress(self) -> None:
        p = self.sandbox.layout.progress_file
        if p.exists():
            p.unlink()

    def step_state(self, step_id: str) -> dict:
        return self.load_progress()["steps"].get(step_id, {})

    def next_step(self) -> Step | None:
        prog = self.load_progress()
        for s in self.steps:
            if prog["steps"].get(s.id, {}).get("status") != "passed":
                return s
        return None

    def all_passed(self) -> bool:
        prog = self.load_progress()
        return all(prog["steps"].get(s.id, {}).get("status") == "passed" for s in self.steps)

    # ---- 命令校验 -------------------------------------------------
    def check_command(self, step: Step, command: str) -> list[Check]:
        out = []
        for pat in step.must_match:
            ok = re.search(pat, command) is not None
            out.append(Check(f"命令含 {pat}", ok, "命中" if ok else "未命中"))
        for pat in step.must_not_match:
            hit = re.search(pat, command) is not None
            out.append(Check(f"命令不得含 {pat}", not hit, "已避开" if not hit else "出现了该写法"))
        return out

    # ---- 执行与验证 -----------------------------------------------
    def execute(self, step: Step, command: str) -> runner.RunResult:
        """真正执行学员输入的命令. 解析 argv,不接受 shell 元字符."""
        argv = split_command(command)
        if not argv or argv[0] not in {"ffmpeg", "ffmpeg.exe"}:
            raise ValueError("命令必须以 ffmpeg 开头")
        for bad in (";", "&&", "||", "|", ">", "<", "`", "$("):
            if bad in command:
                raise ValueError(f"为安全起见不接受 shell 元字符: {bad}")
        # 路径守卫:越界、覆写素材、协议式输入都在这里被拒
        self.sandbox.guard_argv(argv)
        before = {m["name"]: m["sha256"] for m in self.sandbox.manifest()}
        result = runner.run_ffmpeg(argv[1:], cwd=self.sandbox.root)
        # 素材指纹复核:manifest() 里的 sha256 前缀足够识别改写或删除
        self.sandbox.assert_untouched_assets(
            {name: digest for name, digest in before.items()}
        )
        return result

    def verify(self, step: Step, *, command: str | None = None) -> dict:
        checks: list[Check] = []
        if command:
            checks += self.check_command(step, command)

        missing = [o for o in step.outputs if not self.sandbox.resolve(o).exists()]
        checks.append(
            Check("产物文件存在", not missing,
                  "全部就位" if not missing else f"缺少 {', '.join(missing)}")
        )
        if not missing and step.assertions:
            checks += step.assertions(self.sandbox, step)

        passed = all(c.ok for c in checks)
        evidence = [{"key": c.key, "ok": c.ok, "detail": c.detail, "measured": c.measured} for c in checks]
        return {"step": step.id, "passed": passed, "checks": evidence}

    def record(self, step: Step, result: dict, command: str | None) -> dict:
        prog = self.load_progress()
        prev = prog["steps"].get(step.id, {})
        entry = {
            "status": "passed" if result["passed"] else "attempted",
            "attempts": int(prev.get("attempts", 0)) + 1,
            "last_command": command,
            "last_evidence": result["checks"],
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        if result["passed"] and not prev.get("first_passed_at"):
            entry["first_passed_at"] = entry["updated_at"]
        else:
            entry["first_passed_at"] = prev.get("first_passed_at")
        prog["steps"][step.id] = entry
        prog["updated_at"] = entry["updated_at"]
        self.save_progress(prog)
        return entry


def split_command(command: str) -> list[str]:
    """按 shell 习惯切分,支持引号包裹含空格的滤镜图."""
    tokens = re.findall(r'"[^"]*"|\'[^\']*\'|\S+', command.strip())
    out = []
    for t in tokens:
        if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
            out.append(t[1:-1])
        else:
            out.append(t)
    return out


# ---------------------------------------------------------------- 断言实现


def _assert_normalized(sb: Sandbox, step: Step) -> list[Check]:
    info = runner.probe(sb.resolve("work/beta_norm.mp4"), cwd=sb.root)
    return [
        check_eq("宽", info.width, 640),
        check_eq("高", info.height, 360),
        check_eq("帧率", info.fps, 30.0, tol=0.01),
        check_eq("像素格式 yuv420p", info.extra.get("pix_fmt"), "yuv420p"),
    ]


def _assert_concat(sb: Sandbox, step: Step) -> list[Check]:
    info = runner.probe(sb.resolve("work/joined.mp4"), cwd=sb.root)
    # alpha 4s@30 = 120 帧, beta_norm 3s@30 = 90 帧 => 210 帧 / 7.0s
    return [
        check_between("时长(秒)", info.duration, 6.9, 7.1),
        check_between("帧数", info.frame_count, 208, 212),
        check_eq("宽", info.width, 640),
        check_eq("高", info.height, 360),
    ]


def _assert_mix(sb: Sandbox, step: Step) -> list[Check]:
    voice = runner.measure_volume(sb.resolve("assets/voiceover.wav"), cwd=sb.root)
    mixed = runner.measure_volume(sb.resolve("work/mixed.wav"), cwd=sb.root)
    delta = None
    if voice.get("mean_db") is not None and mixed.get("mean_db") is not None:
        delta = mixed["mean_db"] - voice["mean_db"]
    return [
        check_between("混音相对旁白电平变化(dB)", delta, -1.0, 1.0),
        check_eq("采样率", runner.probe(sb.resolve("work/mixed.wav"), cwd=sb.root).sample_rate, 48000),
    ]


def _assert_mux(sb: Sandbox, step: Step) -> list[Check]:
    info = runner.probe(sb.resolve("work/joined_av.mp4"), cwd=sb.root)
    return [
        Check("含视频流", info.has_video, f"codec={info.video_codec}"),
        Check("含音频流", info.has_audio, f"codec={info.audio_codec}"),
        check_between("时长(秒)", info.duration, 6.8, 7.2),
    ]


def _assert_deliver(sb: Sandbox, step: Step) -> list[Check]:
    info = runner.probe(sb.resolve("work/final.mp4"), cwd=sb.root)
    loud = runner.measure_loudness(sb.resolve("work/final.mp4"), cwd=sb.root)
    vol = runner.measure_volume(sb.resolve("work/final.mp4"), cwd=sb.root)
    return [
        check_eq("视频编码", info.video_codec, "h264"),
        check_eq("音频编码", info.audio_codec, "aac"),
        check_eq("像素格式 yuv420p", info.extra.get("pix_fmt"), "yuv420p"),
        check_between("综合响度(LUFS)", loud.get("integrated_lufs"), -17.0, -15.0),
        check_le("峰值电平(dB, 不削顶)", vol.get("max_db"), -1.5),
        check_between("时长(秒)", info.duration, 6.8, 7.3),
    ]


# ---------------------------------------------------------------- 流程定义


def build_steps() -> list[Step]:
    return [
        Step(
            id="s1-unify",
            title="第 1 步 · 把两段素材统一到同一规格",
            why=(
                "concat 滤镜要求所有片段的分辨率、像素格式、SAR 完全一致。"
                "clip_beta 是 1280x720@25fps,clip_alpha 是 640x360@30fps,"
                "直接拼接会得到 `Failed to configure output pad on Parsed_concat_0` —— 报错信息里不会告诉你是哪一项不匹配。"
            ),
            task="把 assets/clip_beta.mp4 转成 640x360、30fps、yuv420p,输出到 work/beta_norm.mp4。",
            hint=(
                'ffmpeg -i assets/clip_beta.mp4 -vf "scale=640:360,fps=30,format=yuv420p" '
                '-c:v libx264 -preset veryfast -crf 20 -an -y work/beta_norm.mp4'
            ),
            outputs=["work/beta_norm.mp4"],
            must_match=[r"scale=", r"fps=", r"work/beta_norm\.mp4"],
            must_not_match=[],
            assertions=_assert_normalized,
            insight=(
                "实测 note:clip_beta 是 3 秒 25fps(75 帧),重采样到 30fps 后为 89 帧、2.97 秒,"
                "不是理论上限的 90 帧 —— 把 scale 写在 fps 前会稳定少一帧(三次重跑一致),"
                "写成 fps=30,scale=... 则得 90 帧。帧数不能靠「时长×帧率」推算,必须实测。"
            ),
        ),
        Step(
            id="s2-concat",
            title="第 2 步 · 拼接两段视频",
            why=(
                "concat 滤镜把多路输入按顺序首尾相接。它只处理画面,"
                "音频要单独用 concat 或后续混音,否则会出现音画不同步。"
            ),
            task="把 assets/clip_alpha.mp4 与 work/beta_norm.mp4 拼成 work/joined.mp4(只保留画面)。",
            hint=(
                'ffmpeg -i assets/clip_alpha.mp4 -i work/beta_norm.mp4 '
                '-filter_complex "[0:v][1:v]concat=n=2:v=1:a=0[v]" -map "[v]" '
                '-c:v libx264 -preset veryfast -crf 20 -pix_fmt yuv420p -y work/joined.mp4'
            ),
            outputs=["work/joined.mp4"],
            must_match=[r"concat=n=2:v=1:a=0", r'-map\s+"?\[v\]"?'],
            must_not_match=[r"-c:v\s+copy"],
            assertions=_assert_concat,
            insight=(
                "实测 120 + 89 = 209 帧 / 6.97 秒,不是「4+3 秒 ×30」的 210 帧 —— "
                "因为 beta_norm 本身只有 89 帧(见第 1 步)。所有拼接结果都要拿实测帧数核对,"
                "不能拿理论时长乘帧率。"
            ),
        ),
        Step(
            id="s3-mix",
            title="第 3 步 · 混音,避开 amix 的默认归一化",
            why=(
                "amix 默认 normalize=1,会把总和除以输入数。两路输入时主轨直接掉 6dB —— "
                "听起来就是「旁白忽然变小了」,而日志里没有任何警告。"
            ),
            task=(
                "把 assets/voiceover.wav 与 assets/music_bed.wav 混成 work/mixed.wav,"
                "要求旁白电平相对单独播放时变化不超过 ±1dB。"
            ),
            hint=(
                'ffmpeg -i assets/voiceover.wav -i assets/music_bed.wav '
                '-filter_complex "[0:a][1:a]amix=inputs=2:normalize=0[a]" -map "[a]" '
                '-c:a pcm_s16le -y work/mixed.wav'
            ),
            outputs=["work/mixed.wav"],
            must_match=[r"amix=", r"normalize=0", r"work/mixed\.wav"],
            must_not_match=[],
            assertions=_assert_mix,
            insight="normalize=0 关闭自动衰减,电平由各输入自己决定;要控制配比就显式加 volume。",
        ),
        Step(
            id="s4-mux",
            title="第 4 步 · 把画面和声音合到一条流",
            why=(
                "map 决定哪条流进入哪个输出。漏掉 -map 时 ffmpeg 会「自动挑一条」,"
                "结果往往是画面没了或声音没了,而且不报错。另一个坑是 -shortest:"
                "画面 7 秒、声音只有 4 秒,直接写 -shortest 会把成片截成 4 秒,画面后 3 秒全丢。"
            ),
            task=(
                "用 work/joined.mp4 的画面 + work/mixed.wav 的声音,输出 work/joined_av.mp4,"
                "时长保持在 7 秒左右(音频不足的部分要补齐)。"
            ),
            hint=(
                'ffmpeg -i work/joined.mp4 -i work/mixed.wav '
                '-filter_complex "[1:a]apad[a]" -map 0:v:0 -map "[a]" '
                '-c:v libx264 -preset veryfast -crf 20 -pix_fmt yuv420p '
                '-c:a aac -b:a 160k -shortest -y work/joined_av.mp4'
            ),
            outputs=["work/joined_av.mp4"],
            must_match=[r"-map\s+0:v:0", r"-map\s+\"?\[a\]\"?", r"-shortest", r"apad"],
            must_not_match=[r"-map\s+1:a:0\s+-shortest"], 
            assertions=_assert_mux,
            insight=(
                "apad 在音频尾部补静音,再配合 -shortest 由画面决定长度 —— "
                "这样「短音轨」不会反过来砍掉画面。"
            ),
        ),
        Step(
            id="s5-deliver",
            title="第 5 步 · 响度归一化并导出成片",
            why=(
                "混音后的综合响度是 -28 LUFS,在手机外放上几乎听不清。"
                "loudnorm 按 EBU R128 把整体拉到目标响度,并限制真峰值;"
                "这一步的产出可以直接测:综合响度、峰值、编码组合。"
            ),
            task=(
                "把 work/joined_av.mp4 归一化到 -16 LUFS、峰值不高于 -1.5dBFS,"
                "输出 work/final.mp4(H.264 / AAC / yuv420p,时长保持 7 秒左右)。"
            ),
            hint=(
                'ffmpeg -i work/joined_av.mp4 -c:v copy '
                '-af "loudnorm=I=-16:TP=-1.5:LRA=11" -c:a aac -b:a 160k -y work/final.mp4'
            ),
            outputs=["work/final.mp4"],
            must_match=[r"loudnorm=", r"I=-16", r"work/final\.mp4"],
            must_not_match=[r"-c:v\s+copy.*-vf\s+loudnorm"],
            assertions=_assert_deliver,
            insight=(
                "单遍 loudnorm 会做动态调整,精度足够日常交付;"
                "要更严格就用双遍(two-pass),第一遍输出 JSON 参数,第二遍带入 measured_* 。"
            ),
        ),
    ]
