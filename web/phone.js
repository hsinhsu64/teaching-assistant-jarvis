// 手機作答頁：匿名（只用本機產生的 cid）、每 2 秒輪詢。
// 單題：點選即送出，可改選。考卷：逐題作答、可回頭改，切題時暫存，最後交卷。
"use strict";

const LETTERS = "ABCDEF";
const app = document.getElementById("app");
const token = new URLSearchParams(location.search).get("t") || "";

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function makeCid() {
  return (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2) + Date.now());
}

let cid;
try {
  cid = localStorage.getItem("xiaoshen_cid");
  if (!cid) { cid = makeCid(); localStorage.setItem("xiaoshen_cid", cid); }
} catch (e) {
  cid = makeCid();  // 無痕模式等情況：只在這次頁面有效
}

let appInfo = { name: "助理" };   // 最近一次 /api/phone/control 回的 app
const appName = () => appInfo.name;
let run = null;          // 伺服器最新的 run
let answers = {};        // 本地作答（考卷切題前的暫存）
let current = 0;         // 考卷目前題號
let message = "";
let messageIsError = false;
let fatal = "";

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (res.status === 403) { fatal = "網址已失效，請重新掃描投影幕上的 QR Code。"; throw new Error("403"); }
  return res;
}

async function submit(final) {
  try {
    const res = await api("/api/phone/submit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ t: token, cid, answers, final }),
    });
    if (res.status === 409) { setMsg("作答已截止", true); await poll(); return false; }
    if (!res.ok) { setMsg("送出失敗，請再試一次", true); return false; }
    return true;
  } catch (e) {
    if (!fatal) setMsg("網路不穩，請再試一次", true);
    render();
    return false;
  }
}

function setMsg(text, isError) {
  message = text;
  messageIsError = !!isError;
}

async function choose(qid, i) {
  answers[qid] = i;
  if (run.kind === "single") {
    render();
    if (await submit(true)) setMsg("已送出，可再改選");
  } else {
    setMsg("");
  }
  render();
}

async function go(delta) {
  if (Object.keys(answers).length) await submit(false);
  current = Math.max(0, Math.min(current + delta, run.questions.length - 1));
  setMsg("");
  render();
}

async function jump(i) {
  if (Object.keys(answers).length) await submit(false);
  current = i;
  render();
}

async function handIn() {
  const missing = run.questions.filter((q) => answers[q.id] === undefined).length;
  if (missing && !confirm(`還有 ${missing} 題未作答，確定交卷？`)) return;
  if (await submit(true)) {
    setMsg("已交卷，等待主持人收卷後公布答案");
    await poll();
  }
  render();
}

function optionButtons(q, closed) {
  return q.options.map((opt, i) => {
    let cls = "opt";
    if (closed) {
      if (i === q.answer) cls += " right";
      else if (q.mine === i) cls += " wrong";
    } else if (answers[q.id] === i) cls += " sel";
    const attrs = closed ? "disabled" : `data-q="${esc(q.id)}" data-i="${i}"`;
    return `<button class="${cls}" ${attrs}>${LETTERS[i]}. ${esc(opt)}</button>`;
  }).join("");
}

function resultBlock(q) {
  const verdict = q.mine === null || q.mine === undefined ? "未作答" : q.correct ? "答對 ✓" : "答錯 ✗";
  return `<div class="result"><b>${verdict}</b>　正解：${LETTERS[q.answer]}
    ${q.explanation ? `<div class="explain">${esc(q.explanation)}</div>` : ""}</div>`;
}

function render() {
  if (fatal) { app.innerHTML = `<p class="note">${esc(fatal)}</p>`; return; }
  if (!run) { app.innerHTML = `<p class="note">等待題目…<br><small>主持人派題後會自動出現</small></p>`; return; }
  const closed = run.status === "closed";
  const msg = `<div class="msg${messageIsError ? " err" : ""}">${esc(message)}</div>`;

  if (run.kind === "single") {
    const q = run.questions[0];
    app.innerHTML = `<div class="meta"><span>單題</span><span>${closed ? "已公布答案" : "作答中"}</span></div>
      <div class="stem">${esc(q.stem)}</div>${optionButtons(q, closed)}${closed ? resultBlock(q) : msg}`;
  } else if (closed) {
    const right = run.questions.filter((q) => q.correct).length;
    app.innerHTML = `<div class="meta"><span>考卷結果</span><span>答對 ${right} / ${run.questions.length}</span></div>` +
      run.questions.map((q, i) => `<div style="margin-top:18px"><div class="stem">${i + 1}. ${esc(q.stem)}</div>
        ${optionButtons(q, true)}${resultBlock(q)}</div>`).join("");
  } else {
    const q = run.questions[current];
    const dots = run.questions.map((x, i) =>
      `<button class="dot${answers[x.id] !== undefined ? " done" : ""}${i === current ? " cur" : ""}" data-jump="${i}">${i + 1}</button>`).join("");
    app.innerHTML = `<div class="meta"><span>考卷 第 ${current + 1} / ${run.questions.length} 題</span>
        <span>${run.submitted ? "已交卷（仍可修改）" : "作答中"}</span></div>
      <div class="stem">${esc(q.stem)}</div>${optionButtons(q, false)}
      <div class="nav"><button class="secondary" data-go="-1" ${current === 0 ? "disabled" : ""}>上一題</button>
        <button class="secondary" data-go="1" ${current === run.questions.length - 1 ? "disabled" : ""}>下一題</button></div>
      <div class="dots">${dots}</div>
      <button class="full" data-handin="1">交卷</button>${msg}`;
  }
}

app.addEventListener("click", (ev) => {
  const b = ev.target.closest("button");
  if (!b || b.disabled) return;
  if (b.dataset.q !== undefined) choose(b.dataset.q, Number(b.dataset.i));
  else if (b.dataset.go) go(Number(b.dataset.go));
  else if (b.dataset.jump !== undefined) jump(Number(b.dataset.jump));
  else if (b.dataset.handin) handIn();
});

async function poll() {
  try {
    const res = await api(`/api/phone/state?t=${encodeURIComponent(token)}&cid=${encodeURIComponent(cid)}`);
    const data = await res.json();
    const next = data.run;
    const changed = !run || !next || next.id !== run.id || next.status !== run.status;
    if (next && (!run || next.id !== run.id)) {
      // 新題目：以伺服器上的作答為準（重新整理頁面也能接續）
      answers = {};
      next.questions.forEach((q) => { if (q.mine !== null && q.mine !== undefined) answers[q.id] = q.mine; });
      current = 0;
      setMsg("");
    }
    if (next && run && next.id === run.id && next.status === "closed" && run.status === "open") setMsg("");
    run = next;
    if (changed) render();
    else if (run && run.kind === "exam" && run.status === "open") {
      // 只更新交卷狀態文字，避免作答中整頁重畫
      const meta = app.querySelector(".meta span:last-child");
      if (meta) meta.textContent = run.submitted ? "已交卷（仍可修改）" : "作答中";
    }
  } catch (e) {
    if (fatal) render();
  }
}


// ---- 打字發問與投影控制（§6.3a）：只有翻頁、播放、結束；其他指令不開放 ----
async function postPhone(path, body) {
  const res = await fetch(path, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ t: token, ...body }),
  });
  if (res.status === 403) return { status: "error", message: "網址已失效，請重新掃描 QR Code。" };
  return res.json().catch(() => ({ status: "error", message: "送出失敗，請再試一次" }));
}

// 詢問老師（§6.3a）：只送到主持人畫面，老師決定要不要投影
document.getElementById("teacherSend").addEventListener("click", async () => {
  const msgEl = document.getElementById("teacherMsg");
  const btn = document.getElementById("teacherSend");
  btn.disabled = true;
  msgEl.className = "msg";
  msgEl.textContent = "送出中…";
  try {
    const r = await postPhone("/api/phone/teacher", { cid, text: document.getElementById("teacherText").value });
    msgEl.textContent = r.message || "";
    if (r.status === "ok") document.getElementById("teacherText").value = "";
    else msgEl.className = "msg err";
  } catch (e) {
    msgEl.className = "msg err";
    msgEl.textContent = "網路不穩，請再試一次";
  } finally {
    btn.disabled = false;
  }
});

document.getElementById("askSend").addEventListener("click", async () => {
  const msgEl = document.getElementById("askMsg");
  const text = document.getElementById("askText").value;
  const route = "openevidence";  // 搜尋網路只查文獻
  msgEl.className = "msg";
  msgEl.textContent = "送出中…";
  try {
    const r = await postPhone("/api/phone/ask", { cid, text, route });
    msgEl.textContent = r.message || "";
    if (r.status === "ok") document.getElementById("askText").value = "";
    else msgEl.className = "msg err";
  } catch (e) {
    msgEl.className = "msg err";
    msgEl.textContent = "網路不穩，請再試一次";
  }
});

let ctlKey = "";
function renderControl(v) {
  const key = JSON.stringify(v);
  if (key === ctlKey) return;  // 沒變就不重畫（輸入頁碼時不被打斷）
  ctlKey = key;
  const el = document.getElementById("ctl");
  if (v.paused) {
    el.innerHTML = `<p class="msg">主持人已暫停手機投影控制</p>`;
    return;
  }
  if (v.quiz_open && !v.presenting) {
    el.innerHTML = `<p class="msg">作答進行中，暫不開放播放</p>`;
    return;
  }
  if (v.presenting) {
    el.innerHTML = `<div class="meta"><span>投影中：${esc(v.deck)}</span><span>第 ${v.page} / ${v.page_count} 頁</span></div>
      <div class="ctl-row"><button class="secondary" data-nav="prev">上一頁</button><button data-nav="next">下一頁</button></div>
      <div class="ctl-row"><input id="gotoPage" type="number" min="1" max="${v.page_count}" placeholder="頁碼">
        <button class="secondary" data-nav="goto">跳到這一頁</button></div>
      <div class="ctl-row"><button class="secondary" data-end="1">結束簡報</button></div>
      <div class="msg">播放中不能改播別份簡報</div>`;
  } else {
    const decks = v.decks.map((n) => `<button class="secondary deck-btn" data-play="deck" data-name="${esc(n)}">▶ ${esc(n)}</button>`).join("");
    el.innerHTML = `<div class="meta"><span>投影幕：${esc(appName())}主畫面</span><span></span></div>
      ${decks || `<p class="msg">簡報資料夾裡沒有簡報</p>`}
      ${v.has_oe ? `<button class="secondary deck-btn" data-play="oe">▶ OE 簡報</button>` : ""}
      ${v.has_summary ? `<button class="secondary deck-btn" data-play="summary">▶ 摘要簡報</button>` : ""}`;
  }
}

document.getElementById("ctl").addEventListener("click", async (ev) => {
  const b = ev.target.closest("button");
  if (!b) return;
  const msgEl = document.getElementById("ctlMsg");
  let r;
  if (b.dataset.nav === "goto") {
    r = await postPhone("/api/phone/nav", { action: "goto", page: Number(document.getElementById("gotoPage").value) || 0 });
  } else if (b.dataset.nav) {
    r = await postPhone("/api/phone/nav", { action: b.dataset.nav });
  } else if (b.dataset.play) {
    r = await postPhone("/api/phone/play", { kind: b.dataset.play, name: b.dataset.name || "" });
  } else if (b.dataset.end) {
    r = await postPhone("/api/phone/end", {});
  } else {
    return;
  }
  msgEl.className = r.status === "ok" || r.status === "converting" ? "msg" : "msg err";
  msgEl.textContent = r.status === "ok" ? "" : (r.message || "");
  pollControl();
});

async function pollControl() {
  try {
    const res = await fetch(`/api/phone/control?t=${encodeURIComponent(token)}`);
    if (!res.ok) return;
    const v = await res.json();
    if (v.app && v.app.name) appInfo = v.app;
    // 搜尋網路：主持人開放才顯示（預設關閉）
    document.getElementById("askBox").style.display = v.search_enabled ? "" : "none";
    if (document.getElementById("ctlBox").open) renderControl(v);
  } catch (e) { /* 下次再試 */ }
}
document.getElementById("ctlBox").addEventListener("toggle", pollControl);

render();
poll();
pollControl();
setInterval(() => { if (!fatal) { poll(); pollControl(); } }, 2000);
