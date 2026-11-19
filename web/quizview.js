// 投影與主持人畫面共用：把一題（含 counts）畫成選項＋長條圖。
// showAnswer=false 時絕不標示正解（投影幕開放作答期間）。
"use strict";

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

const LETTERS = "ABCDEF";

// 助理名稱：取自最近一次快照的 app.name；快照還沒到時先用「助理」。
// 頁面上標了 class="app-name" 的元素與 <title data-suffix> 會一起更新。
// 「喊…」的提示用 app.wake（實際生效的喚醒詞，可能和名字不同）。
let appInfo = { name: "助理", wake: "", audience: "與會人員" };
function appName() { return appInfo.name; }
function wakeWord() { return appInfo.wake || appInfo.name; }
function setApp(a) {
  if (!a || !a.name) return;
  appInfo = a;
  document.querySelectorAll(".app-name").forEach((e) => { e.textContent = a.name; });
  const t = document.querySelector("title");
  if (t && t.dataset.suffix) document.title = `${a.name} · ${t.dataset.suffix}`;
}

function renderQuestion(q, { showAnswer, showExplain }) {
  const total = q.counts ? q.counts.reduce((a, b) => a + b, 0) : 0;
  const rows = q.options.map((opt, i) => {
    const n = q.counts ? q.counts[i] : 0;
    const pct = total ? Math.round((n / total) * 100) : 0;
    const correct = showAnswer && q.answer === i ? " is-correct" : "";
    return `<div class="opt-row">
        <span class="opt-label${correct}">${LETTERS[i]}. ${esc(opt)}</span>
        <span class="opt-count">${n}</span>
        <div class="bar"><span class="${correct.trim()}" style="width:${pct}%"></span></div>
      </div>`;
  }).join("");
  let extra = "";
  if (showExplain && q.explanation) extra += `<div class="explain">${esc(q.explanation)}</div>`;
  if (showExplain && q.source) extra += `<div class="source">出處：${esc(q.source)}</div>`;
  return `<div class="q-stem">${esc(q.stem)}</div>${rows}${extra}`;
}

function rateText(r) {
  return r === null || r === undefined ? "—" : `${Math.round(r * 100)}%`;
}

// 依 snapshot.keys 對應按鍵 → 動作；回傳 handler
function makeKeyHandler(getKeys, actions) {
  return (ev) => {
    if (ev.target && /INPUT|SELECT|TEXTAREA/.test(ev.target.tagName)) return;
    const keys = getKeys();
    if (keys.next.includes(ev.key)) { ev.preventDefault(); actions.next(); }
    else if (keys.prev.includes(ev.key)) { ev.preventDefault(); actions.prev(); }
    else if (keys.drawer.includes(ev.key)) { ev.preventDefault(); actions.drawer(); }
    else if (keys.wake && keys.wake.includes(ev.key) && actions.wake) { ev.preventDefault(); actions.wake(); }
  };
}

const DEFAULT_KEYS = { next: ["PageDown", "ArrowRight", "ArrowDown", " "], prev: ["PageUp", "ArrowLeft", "ArrowUp"], drawer: ["b", "B", "."], wake: [] };

function postJSON(url, body) {
  return fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body ?? {}) });
}
