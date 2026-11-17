// 投影畫面：全螢幕簡報＋右上燈＋右側抽屜。
// 簡報頁面全部預載；WebSocket 斷線時改為本地翻頁，重連後把本地頁碼同步回伺服器。
"use strict";

const SLIDE_WIDTH = 1920;
const slideEl = document.getElementById("slide");
const emptyEl = document.getElementById("empty");
const lightEl = document.getElementById("light");
const drawerEl = document.getElementById("drawer");
const phiEl = document.getElementById("phi");
const zoomEl = document.getElementById("figzoom");
const cardEl = document.getElementById("cardview");
const sourceEl = document.getElementById("sourceview");
const linksEl = document.getElementById("oelinks");
const voiceLight = () => ({
  listening: `● ${appName()}聆聽中`, thinking: `● ${appName()}查詢中`, speaking: `● ${appName()}回答中`, offline: "● 語音離線",
});

let snap = null;          // 最近一次伺服器快照
let online = false;
let localPage = 0;        // 目前顯示的頁碼（離線時由本地維護）
let deckVersion = null;
let images = [];          // 預載的頁面
let keys = DEFAULT_KEYS;

function preload(deck) {
  images = [];
  for (let i = 0; i < deck.page_count; i++) {
    const img = new Image();
    img.src = `/slides/${i}.png?w=${SLIDE_WIDTH}&v=${deck.version}`;
    images.push(img);
  }
}

function showPage(i) {
  if (!images.length) {
    slideEl.style.display = "none";
    emptyEl.style.display = "";
    return;
  }
  localPage = Math.max(0, Math.min(i, images.length - 1));
  slideEl.src = images[localPage].src;
  slideEl.style.display = "";
  emptyEl.style.display = "none";
  loadLinks();
}

// ---- OE 簡報（§6.2c）：參考文獻在投影幕上直接點 ----
let linksKey = null;
let links = [];

async function loadLinks() {
  const d = snap && snap.deck;
  const key = d && d.temp_kind === "oe" && snap.presenting && online ? `${d.version}:${localPage}` : null;
  if (key !== linksKey) {
    linksKey = key;
    links = [];
    if (key) {
      let got = [];
      try { got = await (await fetch(`/deck/links/${localPage}`)).json(); } catch (e) { got = []; }
      if (linksKey !== key) return;  // 等的時候已經翻頁：別頁的框不能放上來（審查 I2）
      links = got;
    }
  }
  placeLinks();
}

function placeLinks() {
  linksEl.innerHTML = "";
  if (!links.length || slideEl.style.display === "none") return;
  const r = slideEl.getBoundingClientRect();
  for (const l of links) {
    const b = document.createElement("button");
    b.className = "oelink";
    b.title = "打開這篇文獻";
    b.style.left = `${r.left + l.x0 * r.width}px`;
    b.style.top = `${r.top + l.y0 * r.height}px`;
    b.style.width = `${(l.x1 - l.x0) * r.width}px`;
    b.style.height = `${(l.y1 - l.y0) * r.height}px`;
    b.onclick = async (ev) => {
      ev.stopPropagation();
      b.classList.add("hit");
      setTimeout(() => b.classList.remove("hit"), 800);
      try {
        const res = await (await postJSON("/api/oe/ref", { ref: l.ref })).json();
        if (res.status !== "ok") showToast(res.message || "無法打開這篇文獻");  // 審查 I3：讓點的人知道
      } catch (e) { showToast("無法打開這篇文獻"); }
    };
    linksEl.appendChild(b);
  }
}
let toastTimer = null;
function showToast(text) {
  const el = document.getElementById("oetoast");
  el.textContent = text;
  el.style.display = "";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.style.display = "none"; }, 3500);
}
slideEl.addEventListener("load", placeLinks);
window.addEventListener("resize", placeLinks);

function renderLight() {
  lightEl.className = "";
  if (!online) {
    lightEl.textContent = "● 離線";
    lightEl.classList.add("offline");
  } else if (snap && voiceLight()[snap.voice_status]) {
    lightEl.textContent = voiceLight()[snap.voice_status];
    lightEl.classList.add(snap.voice_status === "offline" ? "offline" : "voice");
  } else if (snap && snap.run && snap.run.status === "open") {
    lightEl.textContent = "● 派題中";
    lightEl.classList.add("quiz");
  } else {
    lightEl.textContent = "● 待命";
  }
  phiEl.style.display = snap && snap.voice_phi_alert ? "" : "none";
  const vc = snap && snap.voice_confirm;
  document.getElementById("vconfirm").style.display = vc ? "" : "none";
  if (vc) document.getElementById("vconfirmQ").textContent = vc.question;
}

function qrBlock() {
  if (!snap.phone_url) return `<div class="tunnel-down">手機作答暫停：通道離線</div>`;
  return `<div class="qr-row"><img src="/qr.png?u=${encodeURIComponent(snap.phone_url)}" alt="QR"><div>掃碼作答</div></div>`;
}

const ROUTE_LABEL = { openevidence: "OpenEvidence", pubmed: "PubMed", model: "Gemini 一般知識（未經文獻驗證）",
  references: "參考資料", deck: "今天的簡報", page: "今天的簡報" };

function derivedHtml(a) {
  return (a.derivations || []).map((d) => `<div class="card ans-card ans-derived">
      <div><b>${esc(appName())}推導（未經主治確認）：</b>${esc(d.text)}</div><div class="source">依據：${d.sources.map(esc).join("；")}</div></div>`).join("")
    + (a.derivation_notes || []).map((n) => `<div class="card ans-error">${esc(appName())}說明（未經主治確認）：${esc(n)}</div>`).join("");
}

function tablesHtml(a) {
  return (a.tables || []).map((t) => `<div class="card ans-table"><table>
      <tr>${t.header.map((h) => `<th>${esc(h)}</th>`).join("")}</tr>
      ${t.rows.map((r) => `<tr>${r.map((c) => `<td>${esc(c)}</td>`).join("")}</tr>`).join("")}
    </table>${t.sources.length ? `<div class="source">表格出自 OpenEvidence：${t.sources.map(esc).join("；")}</div>` : ""}</div>`).join("");
}

function figuresHtml(a) {
  return (a.figures || []).map((f, i) => `<figure class="card ans-fig">
      <img src="${esc(f.url)}" alt="${esc(f.name)}" data-fig="${i}">
      <figcaption>${esc(f.name)}${f.caption ? "：" + esc(f.caption) : ""}<br>出自 ${esc(f.source)}</figcaption>
    </figure>`).join("");
}

function renderCard() {
  const c = snap && snap.card;
  if (!c) { cardEl.style.display = "none"; cardEl.innerHTML = ""; return; }
  const src = `${c.url}?v=${encodeURIComponent(c.answer_id)}`;
  if (!cardEl.querySelector(`img[src="${src}"]`)) cardEl.innerHTML = `<img src="${src}" alt="字卡">`;
  cardEl.style.display = "";
}

function renderZoom() {
  const a = snap && snap.ai_answer;
  const i = snap ? snap.figure_zoom : null;
  const f = a && i !== null && i !== undefined && a.figures ? a.figures[i] : null;
  if (!f || snap.drawer !== "answer") { zoomEl.style.display = "none"; return; }
  zoomEl.innerHTML = `<img src="${esc(f.url)}" alt=""><div class="zoom-cap">${esc(f.name)}${f.caption ? "：" + esc(f.caption) : ""}
    · 出自 ${esc(f.source)}　（點一下收回）</div>`;
  zoomEl.style.display = "";
}

function answerHtml(a) {
  let html = `<div class="head"><span>● ${esc(appName())}回答 · ${esc(ROUTE_LABEL[a.route] || "")}</span></div>`;
  html += `<div class="ask-q">${esc(a.question)}</div>`;
  if (a.interpreted) html += `<div class="ask-q">${esc(appName())}理解為：${esc(a.interpreted)}</div>`;
  if (a.fallback_note) html += `<div class="ask-q">${esc(a.fallback_note)}</div>`;
  if (a.status === "answered" && a.oe_deck) {
    // OE 簡報（§6.2c）：結果在簡報裡；還沒播時說明怎麼播（審查 I4：不留空白抽屜）
    const p = a.oe_deck.play;
    html += `<div class="card">OpenEvidence 的結果已做成簡報（${a.oe_deck.pages} 頁）${
      p === "oe_playing" ? "，投影中" : p === "oe_waiting_quiz" ? "，收題後說「播放 OE 簡報」" : "，說「播放 OE 簡報」或按主持人畫面的按鈕"}</div>`;
  } else if (a.status === "answered") {
    if (a.summary_zh) html += `<div class="ans-summary">${esc(a.summary_zh)}</div>`;
    // Gemini 一般知識：沒有文獻可驗證，用推導同樣的橘色框
    const cls = a.route === "model" ? "card ans-card ans-derived" : "card ans-card";
    // 字卡編號：語音「顯示第 N 張的原文」用（§6.2a）
    html += `<div class="ans-list">` + a.cards.map((c, i) => `<div class="${cls}">
        <div><span class="card-no">${i + 1}</span>${esc(c.point_zh)}</div>
        ${c.quote ? `<div class="ans-quote">“${esc(c.quote)}”</div>` : ""}
        <div class="source">出處：${esc(c.source)}</div></div>`).join("") + derivedHtml(a) + tablesHtml(a) + figuresHtml(a) + `</div>`;
  } else if (a.status === "clarify") {
    html += `<div class="card">${esc(a.clarify_zh)}</div>`;
  } else if (a.status === "not_found") {
    html += `<div class="card">${esc(a.summary_zh)}</div>`;
  } else {
    html += `<div class="card ans-error">${esc(a.error || "查詢失敗")}</div>`;
  }
  return html;
}

// 抽屜內容：播放中放進右側抽屜，主畫面時放進右欄（§6.0）
function drawerContent(s) {
  if (s && s.drawer === "working" && s.answer_job) {
    const j = s.answer_job;
    return { open: true, html: `<div class="head"><span>● ${esc(appName())}查詢中… · ${esc(ROUTE_LABEL[j.route] || "")}</span></div>
      <div class="ask-q">${esc(j.label)}</div>` };
  }
  if (s && s.drawer === "teacher" && (s.teacher_questions_shown || []).length) {
    // 手機「詢問老師」的提問（主持人選擇投影，可同時好幾題；不顯示是誰問的，§6.3a）
    const qs = s.teacher_questions_shown;
    return { open: true, html: `<div class="head"><span>● 與會人員提問${qs.length > 1 ? `（${qs.length} 題）` : ""}</span></div>
      <div class="ans-list">${qs.map((q, i) => `<div class="card">${qs.length > 1 ? `<span class="card-no">${i + 1}</span>` : ""}${esc(q.text)}</div>`).join("")}</div>
      <div class="hide-hint">點這裡或按遙控器 B 收起</div>` };
  }
  if (s && s.drawer === "answer" && s.ai_answer) {
    return { open: true, html: answerHtml(s.ai_answer) + `<div class="hide-hint">點這裡或按遙控器 B 收起</div>` };
  }
  const run = s && s.run;
  if (!s || s.drawer === "hidden" || !run) return { open: false, html: "" };
  const open = run.status === "open";
  let html = "";
  if (run.kind === "single") {
    const q = run.questions[0];
    html += `<div class="head"><span>${open ? "● 派題中" : "● 已公布答案"}</span><span>已作答 ${run.participants}</span></div>`;
    html += `<div class="card">${renderQuestion(q, { showAnswer: !open, showExplain: !open })}</div>`;
    if (open) html += qrBlock();
  } else if (open) {
    html += `<div class="head"><span>● 考卷作答中（${run.questions.length} 題）</span><span>已交卷 ${run.submitted} / 作答 ${run.participants}</span></div>`;
    html += qrBlock();
    html += `<div class="card">請用手機逐題作答，完成後按「交卷」。</div>`;
  } else {
    html += `<div class="head"><span>● 收卷結果（答對率低 → 高）</span><span>交卷 ${run.submitted} / 作答 ${run.participants}</span></div>`;
    html += `<div class="exam-list">` + run.questions.map((q, i) =>
      `<div class="card"><span class="rate">${rateText(q.correct_rate)}</span>${renderQuestion(q, { showAnswer: true, showExplain: i < 3 })}</div>`
    ).join("") + `</div>`;
  }
  return { open: true, html };
}

function renderDrawer() {
  const c = drawerContent(snap);
  if (snap && !snap.presenting) {
    drawerEl.classList.remove("open");
    renderHudRight(c);
    return;
  }
  if (c.open) drawerEl.innerHTML = c.html;
  drawerEl.classList.toggle("open", c.open);
}

// ---- 主畫面（HUD）----
const spaced = (s) => [...s].join(" ");
const hudStates = () => ({
  standby: ["待 命", `喊「${wakeWord()}」開始對話`],
  listening: ["聆 聽 中", "請說…（停頓 2 秒就算講完）"],
  thinking: ["查 詢 中", "查詢中，請稍候"],
  speaking: ["回 答 中", "只念已驗證的字卡"],
  offline: ["語 音 離 線", "請用主持人畫面操作"],
  off: [spaced(appName()), "語音未啟用"],
});
const hudEl = document.getElementById("hud");
const hudRightEl = document.getElementById("hudRight");
const hudBars = document.getElementById("hudBars");
for (let i = 0; i < 72; i++) {
  const b = document.createElement("i");
  b.style.transform = `rotate(${i * 5}deg) translateY(-19vh)`;
  hudBars.appendChild(b);
}
const barEls = [...hudBars.children];
let hudMode = "standby";
let lastConv = "";

function hudTick() {
  if (hudEl.style.display !== "none") {
    const t = Date.now() / 1000;
    barEls.forEach((b, i) => {
      let h = 1.2;
      if (hudMode === "listening") h = 1.5 + Math.abs(Math.sin(t * 6 + i * 0.6)) * 2.5 * Math.random();
      if (hudMode === "speaking") h = 1.5 + Math.abs(Math.sin(t * 9 + i * 0.35)) * 4.5;
      if (hudMode === "thinking") h = 1.2 + ((i + Math.floor(t * 20)) % 72 < 10 ? 3 : 0);
      b.style.height = h + "vh";
      b.style.opacity = hudMode === "standby" ? 0.3 : 0.75;
    });
    document.getElementById("hudClock").textContent = new Date().toLocaleString("zh-TW", { hour12: false });
  }
  requestAnimationFrame(hudTick);
}

function renderHud() {
  const show = !snap || !snap.presenting;
  hudEl.style.display = show ? "" : "none";
  document.getElementById("stage").style.display = show ? "none" : "";
  lightEl.style.display = show ? "none" : "";
  if (!show) return;
  const st = snap ? snap.voice_status || "off" : "off";
  const [name, sub] = !online ? ["暫 時 離 線", "連線恢復後自動更新"] : hudStates()[st] || hudStates().off;
  hudMode = online && ["listening", "thinking", "speaking"].includes(st) ? st : "standby";
  document.getElementById("hudName").textContent = name;
  document.getElementById("hudSub").textContent = sub;
  const lines = (snap && snap.voice_transcript) || [];
  const conv = lines.map((l) => {
    const k = l.indexOf("：");
    const who = k >= 0 ? l.slice(0, k) : "";
    const text = k >= 0 ? l.slice(k + 1) : l;
    return `<div class="msg ${who === "你" ? "me" : "xs"}"><div class="who">${esc(who)}</div><div class="t">${esc(text)}</div></div>`;
  }).join("") || `<div class="msg"><div class="t dim">（還沒有對話）</div></div>`;
  const convEl = document.getElementById("hudConv");
  if (conv !== lastConv) {
    convEl.innerHTML = conv;
    convEl.scrollTop = convEl.scrollHeight;
    lastConv = conv;
  }
  const decks = (snap && snap.decks) || [];
  const first = decks.length ? decks[0].name : "簡報名稱";
  document.getElementById("hudHint").innerHTML =
    `說 <b>「${esc(wakeWord())}，播放 ${esc(first)}」</b> 開始簡報 ・ <b>「${esc(wakeWord())}，結束簡報」</b> 回到這個畫面`;
  const qr = document.getElementById("hudQr");
  if (snap && snap.phone_url) {
    const src = `/qr.png?u=${encodeURIComponent(snap.phone_url)}`;
    if (!qr.querySelector(`img[src="${src}"]`)) qr.innerHTML = `<span>掃碼作答／發問</span><img src="${src}" alt="QR">`;
    qr.style.display = "";
  } else {
    qr.style.display = "none";
  }
}

function renderHudRight(c) {
  const title = document.getElementById("hudRightTitle");
  if (c.open) {
    title.textContent = snap.drawer === "teacher" ? "與會人員提問"
      : snap.drawer === "answer" || snap.drawer === "working" ? "回答（已驗證出處）" : "派題";
    hudRightEl.innerHTML = c.html;
    return;
  }
  const cands = snap.deck_candidates || [];
  const current = snap.deck ? snap.deck.name : null;
  if (cands.length) {
    title.textContent = "是哪一份？";
    hudRightEl.innerHTML = `<div class="decks">` + cands.map((n) => `<div>${esc(n)}</div>`).join("") + `</div>`;
    return;
  }
  const decks = snap.decks || [];
  title.textContent = "可以播放的簡報";
  hudRightEl.innerHTML = decks.length
    ? `<div class="decks">` + decks.map((d) => `<div>${esc(d.name)}${d.name === current ? "<small>（今天）</small>" : ""}</div>`).join("") + `</div>`
    : `<div class="dim">簡報資料夾內沒有 PDF</div>`;
}

// ---- 出處原文（§6.2a）：摘要（引用句螢光）、那一頁（PDF 加螢光框）、原文網頁（另開視窗）----
let sourceKey = "";  // 原文畫面沒變就不重畫（每次狀態更新都重畫會閃、圖片重抓、捲動位置跑掉）
function renderSource() {
  const v = snap && snap.source_view;
  const key = v ? JSON.stringify(v) : "";
  if (key === sourceKey) return;
  sourceKey = key;
  if (!v) { sourceEl.style.display = "none"; sourceEl.innerHTML = ""; return; }
  const head = `<div class="sv-head"><span>● 第 ${v.card} 張的出處原文</span><span>${esc(v.citation || "")}</span></div>`;
  let body = "";
  if (v.kind === "abstract") {
    let text = esc(v.text || "");
    if (v.text && v.mark) {
      const [s, e] = v.mark;
      text = esc(v.text.slice(0, s)) + "<mark>" + esc(v.text.slice(s, e)) + "</mark>" + esc(v.text.slice(e));
    }
    body = `<div class="sv-title">${esc(v.title || "")}</div>` +
      (v.text ? `<div class="sv-text">${text}</div>` : `<div class="sv-note">${esc(v.note || "")}</div>`);
  } else if (v.kind === "page") {
    body = `<div class="sv-title">${esc(v.doc)} 第 ${v.page} 頁</div>
      <div class="sv-page"><img src="${esc(v.url)}" alt=""></div>
      <div class="sv-quote">引用：“${esc(v.quote || "")}”</div>`;
  } else {
    body = `<div class="sv-note">原文網頁已在另一個視窗開啟：${esc(v.url || "")}</div>`;
  }
  sourceEl.innerHTML = head + body;
  sourceEl.style.display = "";
}

function applySnapshot(s) {
  snap = s;
  setApp(s.app);
  keys = s.keys || DEFAULT_KEYS;
  if (!s.deck) {
    images = [];
    deckVersion = null;
  } else if (s.deck.version !== deckVersion) {
    deckVersion = s.deck.version;
    preload(s.deck);
  }
  showPage(s.page);
  renderLight();
  renderHud();
  renderDrawer();
  renderZoom();
  renderCard();
  renderSource();
}

// ---- 連線 ----
let ws = null;
function connect() {
  ws = new WebSocket(`ws://${location.host}/ws?audience=display`);
  ws.onopen = async () => {
    const wasOffline = !online;
    online = true;
    // 離線期間若有本地翻頁，先把頁碼同步回伺服器
    if (wasOffline && snap && snap.deck && snap.presenting && localPage !== snap.page) {
      try { await postJSON("/api/nav", { action: "goto", page: localPage }); } catch (e) { /* 下次再試 */ }
    }
    renderLight();
    renderHud();
  };
  ws.onmessage = (ev) => applySnapshot(JSON.parse(ev.data));
  ws.onclose = () => {
    online = false;
    renderLight();
    renderHud();
    setTimeout(connect, 2000);
  };
  ws.onerror = () => ws.close();
}

function nav(action) {
  if (online) {
    postJSON("/api/nav", { action }).catch(() => localNav(action));
  } else {
    localNav(action);
  }
}

function localNav(action) {
  if (snap && !snap.presenting) return;  // 主畫面上不翻頁（§6.0）
  showPage(localPage + (action === "next" ? 1 : -1));
}

// 回答抽屜擋到投影片時，點一下就收起（只限回答；作答抽屜不會被誤點收掉）
drawerEl.addEventListener("click", (ev) => {
  const img = ev.target.closest("img[data-fig]");
  if (img && online) { postJSON("/api/answer/figure", { index: Number(img.dataset.fig) }); return; }  // 點附圖 → 放大
  if (online && snap && ["answer", "working"].includes(snap.drawer)) postJSON("/api/drawer/hide");
});
hudRightEl.addEventListener("click", (ev) => {
  const img = ev.target.closest("img[data-fig]");
  if (img && online) { postJSON("/api/answer/figure", { index: Number(img.dataset.fig) }); return; }
  if (online && snap && ["answer", "working"].includes(snap.drawer)) postJSON("/api/drawer/hide");
});
sourceEl.addEventListener("click", () => { if (online) postJSON("/api/source/close"); });
zoomEl.addEventListener("click", () => { if (online) postJSON("/api/answer/figure", { index: null }); });
cardEl.addEventListener("click", () => { if (online) postJSON("/api/answer/card/hide"); });

document.addEventListener("keydown", makeKeyHandler(() => keys, {
  next: () => nav("next"),
  prev: () => nav("prev"),
  drawer: () => {
    if (!online) return;
    postJSON(snap && snap.source_view ? "/api/source/close" : "/api/drawer/hide");  // 原文畫面優先
  },
  wake: () => { if (online) postJSON("/api/voice/wake"); },
}));

renderLight();
renderHud();
hudTick();
connect();
