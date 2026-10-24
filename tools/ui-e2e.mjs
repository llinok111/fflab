/**
 * 浏览器端到端验证:用 CDP 真实驱动界面完成整条教学流程.
 *
 * 为什么不用截图判断"看起来对":
 *   本机读图链不可用,截图只能给人看。这里改为程序化断言:
 *   DOM 结构 + 点击真实按钮 + 读取断言表格文本。
 *
 * 用法: node tools/ui-e2e.mjs [--port 8765] [--debug 9223]
 */
import { spawn } from "node:child_process";
import { setTimeout as sleep } from "node:timers/promises";

const args = new Map();
for (let i = 2; i < process.argv.length; i += 2) {
  args.set(process.argv[i].replace(/^--/, ""), process.argv[i + 1]);
}
const APP = Number(args.get("port") || 8765);
const DEBUG = Number(args.get("debug") || 9223);
const EDGE = "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe";
// 每次运行用独立 profile:复用同名 profile 时,残留的旧 Edge 进程会抢同一调试端口,
// 表现为 "Execution context was destroyed"(实测踩到)。
const PROFILE = `C:/Users/15082/AppData/Local/Temp/edge-fflab-e2e-${process.pid}`;

const results = [];
const record = (name, ok, detail) => {
  results.push({ name, ok, detail });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}  ${detail ?? ""}`);
};

async function waitJson(url, tries = 60) {
  for (let i = 0; i < tries; i++) {
    try {
      const r = await fetch(url);
      if (r.ok) return await r.json();
    } catch {}
    await sleep(250);
  }
  throw new Error("CDP 未就绪: " + url);
}

class Cdp {
  constructor(ws) {
    this.ws = ws;
    this.id = 0;
    this.pending = new Map();
    ws.addEventListener("message", (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result);
      }
    });
  }
  send(method, params = {}) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
  async eval(expr, { awaitPromise = false } = {}) {
    const r = await this.send("Runtime.evaluate", {
      expression: expr,
      returnByValue: true,
      awaitPromise,
      userGesture: true,
    });
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.text + " :: " + expr.slice(0, 120));
    return r.result.value;
  }
}

const child = spawn(EDGE, [
  "--headless=new",
  "--disable-gpu",
  `--remote-debugging-port=${DEBUG}`,
  `--user-data-dir=${PROFILE}`,
  "--window-size=1440,1000",
  `http://127.0.0.1:${APP}/`,
], { stdio: "ignore" });

try {
  await waitJson(`http://127.0.0.1:${DEBUG}/json/version`);
  let targets = await waitJson(`http://127.0.0.1:${DEBUG}/json/list`);
  let page = targets.find((t) => t.type === "page" && t.url.includes(String(APP)));
  if (!page) {
    const r = await fetch(`http://127.0.0.1:${DEBUG}/json/new?http://127.0.0.1:${APP}/`, { method: "PUT" });
    page = await r.json();
  }

  const ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((res, rej) => {
    ws.addEventListener("open", res, { once: true });
    ws.addEventListener("error", rej, { once: true });
  });
  const cdp = new Cdp(ws);
  await cdp.send("Runtime.enable");
  await cdp.send("Page.enable");

  const shotsDir = args.get("shots");
  const shoot = async (name) => {
    if (!shotsDir) return;
    const { data } = await cdp.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
    const fs = await import("node:fs/promises");
    await fs.mkdir(shotsDir, { recursive: true });
    await fs.writeFile(`${shotsDir}/${name}.png`, Buffer.from(data, "base64"));
    console.log(`SHOT  ${name}.png`);
  };

  // 1) 等界面渲染完成
  // 先显式导航一次,避免拿到旧 profile 里残留的空白页/已销毁上下文
  await cdp.send("Page.navigate", { url: `http://127.0.0.1:${APP}/` });
  await sleep(1200);
  await cdp.eval(`new Promise(r=>{const t=setInterval(()=>{
    if(document.querySelectorAll('.step').length>0){clearInterval(t);r(true)}},100)})`, { awaitPromise: true });

  const stepCount = await cdp.eval(`document.querySelectorAll('.step').length`);
  record("侧栏渲染出 5 个步骤", stepCount === 5, `实测 ${stepCount}`);

  const title = await cdp.eval(`document.querySelector('main h2')?.textContent || ''`);
  record("主区显示当前步骤标题", /第 1 步/.test(title), title);

  const overlap = await cdp.eval(`(() => {
    const els = [...document.querySelectorAll('main h2, main .why, main .task p, #cmd')];
    let bad = 0;
    for (let i = 0; i < els.length; i++) for (let j = i + 1; j < els.length; j++) {
      const a = els[i].getBoundingClientRect(), b = els[j].getBoundingClientRect();
      if (a.width && b.width && a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom) bad++;
    }
    return bad;
  })()`);
  record("关键文本块无重叠", overlap === 0, `重叠对数 ${overlap}`);

  const overflow = await cdp.eval(`[...document.querySelectorAll('main, aside')].map(e=>e.scrollWidth-e.clientWidth)`);
  record("无横向溢出", overflow.every((v) => v <= 1), JSON.stringify(overflow));
  await shoot("01-step1-idle");

  // 2) 逐步在界面上执行整条流程
  const commands = [
    'ffmpeg -i assets/clip_beta.mp4 -vf "scale=640:360,fps=30,format=yuv420p" -c:v libx264 -preset veryfast -crf 20 -an -y work/beta_norm.mp4',
    'ffmpeg -i assets/clip_alpha.mp4 -i work/beta_norm.mp4 -filter_complex "[0:v][1:v]concat=n=2:v=1:a=0[v]" -map "[v]" -c:v libx264 -preset veryfast -crf 20 -pix_fmt yuv420p -y work/joined.mp4',
    'ffmpeg -i assets/voiceover.wav -i assets/music_bed.wav -filter_complex "[0:a][1:a]amix=inputs=2:normalize=0[a]" -map "[a]" -c:a pcm_s16le -y work/mixed.wav',
    'ffmpeg -i work/joined.mp4 -i work/mixed.wav -filter_complex "[1:a]apad[a]" -map 0:v:0 -map "[a]" -c:v libx264 -preset veryfast -crf 20 -pix_fmt yuv420p -c:a aac -b:a 160k -shortest -y work/joined_av.mp4',
    'ffmpeg -i work/joined_av.mp4 -c:v copy -af "loudnorm=I=-16:TP=-1.5:LRA=11" -c:a aac -b:a 160k -y work/final.mp4',
  ];

  for (let i = 0; i < commands.length; i++) {
    await cdp.eval(`(() => {
      const ta = document.getElementById('cmd');
      ta.value = ${JSON.stringify(commands[i])};
      document.getElementById('btnRun').click();
      return true;
    })()`);
    // 等断言表格出现(最多 120 秒,拼接与滤波是真实编码)
    const verdict = await cdp.eval(`new Promise((resolve) => {
      const started = Date.now();
      const t = setInterval(() => {
        const v = document.querySelector('#resultSlot .verdict');
        if (v) { clearInterval(t); resolve(v.className + '|' + v.textContent.trim()); }
        else if (Date.now() - started > 120000) { clearInterval(t); resolve('TIMEOUT'); }
      }, 150);
    })`, { awaitPromise: true });

    const failed = await cdp.eval(`[...document.querySelectorAll('#resultSlot .no')].length`);
    const rows = await cdp.eval(`[...document.querySelectorAll('#resultSlot tbody tr')].length`);
    record(`步骤 ${i + 1} 界面执行通过`, verdict.startsWith("verdict pass"), `${verdict.slice(0, 40)} · 断言 ${rows} 条 · 失败 ${failed}`);
    if (i === 0) await shoot("02-step1-passed");

    // 点"进入下一步"按钮前进(界面不自动跳,必须真实点击)。
    // 注意:按钮在 /api/state 往返之后才插入,断言表格先出现 —— 必须轮询等待,
    // 直接查会误判为"按钮不存在"(实测踩到)。
    if (i < commands.length - 1) {
      const advanced = await cdp.eval(`new Promise((resolve) => {
        const started = Date.now();
        const t = setInterval(() => {
          const btn = [...document.querySelectorAll('#resultSlot button')]
            .find(b => b.textContent.includes('进入下一步'));
          if (btn) { clearInterval(t); btn.click(); resolve(true); }
          else if (Date.now() - started > 15000) { clearInterval(t); resolve(false); }
        }, 100);
      })`, { awaitPromise: true });
      if (!advanced) record(`步骤 ${i + 1} 后出现下一步按钮`, false, "等待 15 秒未出现");
      // 等主区切换到下一步
      await cdp.eval(`new Promise((resolve) => {
        const started = Date.now();
        const t = setInterval(() => {
          const h = document.querySelector('main h2');
          if (h && h.textContent.includes('第 ${i + 2} 步')) { clearInterval(t); resolve(true); }
          else if (Date.now() - started > 15000) { clearInterval(t); resolve(false); }
        }, 100);
      })`, { awaitPromise: true });
    }
  }

  // 3) 全流程完成横幅
  await cdp.eval(`new Promise(r=>setTimeout(r,600))`, { awaitPromise: true });
  const done = await cdp.eval(`!!document.querySelector('.done-banner')`);
  record("完成横幅出现", done === true);

  const pct = await cdp.eval(`document.querySelector('main .meta b')?.textContent || ''`);
  record("进度显示 100%", pct.trim() === "100%", `实测 ${pct}`);
  await shoot("03-all-completed");

  // 4) 反例:越界命令必须在界面上被拒绝
  await cdp.eval(`(() => {
    const ta = document.getElementById('cmd');
    ta.value = 'ffmpeg -i assets/clip_beta.mp4 -y ../../evil.mp4';
    document.getElementById('btnRun').click();
    return true;
  })()`);
  const rejectMsg = await cdp.eval(`new Promise((resolve) => {
    const t = setInterval(() => {
      const v = document.querySelector('#resultSlot .verdict');
      if (v) { clearInterval(t); resolve(v.textContent.trim()); }
    }, 150);
  })`, { awaitPromise: true });
  record("越界命令被界面拦下", /越界/.test(rejectMsg), rejectMsg.slice(0, 60));
  await shoot("04-escape-rejected");

  // 5) 重置按钮真的清空产物
  await cdp.eval(`(() => { window.confirm = () => true; document.getElementById('btnReset').click(); return true; })()`);
  await sleep(1500);
  const resetOk = await cdp.eval(`document.querySelectorAll('.dot.passed').length`);
  record("重置后进度归零", resetOk === 0, `已通过步骤 ${resetOk}`);

  ws.close();
} catch (err) {
  record("执行异常", false, err.message);
} finally {
  child.kill();
}

const failed = results.filter((r) => !r.ok);
console.log(`\n合计 ${results.length} 项,失败 ${failed.length} 项`);
process.exit(failed.length ? 1 : 0);
