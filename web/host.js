// 主持人畫面：自我檢查、簡報切換與翻頁、作答控制（含正解）。
"use strict";

const $ = (id) => document.getElementById(id);
let snap = null;
let keys = DEFAULT_KEYS;
let deckListLoaded = false;
let reloadedFor = null;   // 只為同一份新出現的簡報重抓一次清單
// 經對外通道開啟的就是主持人手機（§6.3b）：筆電走 http://127.0.0.1。伺服器也會拒絕手機取投影片圖。
const PHONE_HOST = location.protocol === "https:";
if (PHONE_HOST) document.body.classList.add("phone-host");
let hostPhoneTimer = null;

async function loadDecks(currentName) {
  const res = await fetch("/api/decks");
  const decks = await res.json();
  const sel = $("decks");
  sel.innerHTML = decks.length
    ? decks.map((d) => `<option value="${esc(d.path)}"${d.name === currentName ? " selected" : ""}>${esc(d.name)}</option>`).join("")
    : `<option value="">（簡報資料夾內沒有 PDF）</option>`;
  deckListLoaded = true;
}

function renderChecks(s) {
  $("checks").innerHTML = s.checks.length
    ? s.checks.map((c) => `<li class="lv-${c.level}"><b>${esc(c.name)}</b>：${esc(c.detail)}${
        c.name === "OpenEvidence" && c.level !== "ok" ? ` <button class="secondary" data-recheck="oe">重新檢查</button>` : ""}</li>`).join("")
    : `<li class="muted">尚無結果</li>`;
  $("warnings").innerHTML = s.warnings.map((w) => `<li>${esc(w)}</li>`).join("");
}

// 投影幕狀態（§6.0）：「播放簡報」一直顯示，播下拉選單選的那份；播放中要先回到主畫面（2026-11-08）
function renderPresent(s) {
  $("presentMode").textContent = s.presenting ? "投影幕：播放中" : "投影幕：主畫面";
  $("presentStop").disabled = !s.presenting;
  $("present").style.display = "";
  $("present").disabled = !s.deck || !!s.presenting;
  $("present").title = s.presenting ? "播放中請先回到主畫面" : "";
  $("decks").disabled = !!s.presenting;
  $("decks").title = s.presenting ? "播放中請先回到主畫面" : "";
  const c = s.deck_candidates || [];
  $("deckCands").textContent = c.length ? `語音找到幾份簡報：${c.join("、")}（請說出是哪一份）` : "";
}

function renderDeck(s) {
  renderPresent(s);
  if (!s.deck) {
    $("preview").removeAttribute("src");
    $("pageinfo").textContent = "—";
    return;
  }
  if (!PHONE_HOST) $("preview").src = `/slides/${s.page}.png?w=960&v=${s.deck.version}`;
  $("pageinfo").textContent = PHONE_HOST ? `第 ${s.page + 1} 頁／共 ${s.deck.page_count} 頁` : `${s.page + 1} / ${s.deck.page_count}`;
  const o = s.ocr_progress;
  $("ocr").textContent = !o || !o.total ? "" : o.done >= o.total
    ? `投影片圖片辨識完成（${o.total} 頁）`
    : `投影片圖片辨識中：${o.done}/${o.total} 頁${o.failed ? `（${o.failed} 頁失敗，下次開啟重試）` : ""}`;
  if (!deckListLoaded) loadDecks(s.deck.name);
  else {
    // 語音播放了另一份 → 下拉選單跟著顯示目前那份
    const opt = [...$("decks").options].find((o) => o.textContent === s.deck.name);
    if (opt && !opt.selected) opt.selected = true;
    else if (!opt && reloadedFor !== s.deck.name) { reloadedFor = s.deck.name; loadDecks(s.deck.name); }
  }
}

function renderPhone(s) {
  $("phone").innerHTML = s.phone_url
    ? `<img src="/qr.png?u=${encodeURIComponent(s.phone_url)}" alt="QR"><span>${esc(s.phone_url)}</span>`
    : `通道尚未就緒（手機暫時無法作答）`;
  $("phone").classList.toggle("muted", !s.phone_url);
}

function hideHostPhoneQr() {
  clearTimeout(hostPhoneTimer);
  $("hostPhoneQr").style.display = "none";
  $("hostPhoneQr").removeAttribute("src");
}

function renderHostPhone(s) {
  const paired = !!(s.host_phone && s.host_phone.paired);
  if (paired && !$("hostPhoneRevoke").style.display) return;  // 狀態沒變，不動（QR Code 可能正在顯示）
  $("hostPhoneRevoke").style.display = paired ? "" : "none";
  $("hostPhoneStatus").textContent = paired ? "已連接一支主持人手機" : "尚未連接";
  if (paired) { hideHostPhoneQr(); $("hostPhoneMsg").textContent = ""; }
}

function renderRun(s) {
  const run = s.run;
  const open = run && run.status === "open";
  $("dispatch").disabled = !s.has_bank_question || open;
  $("reveal").disabled = !(open && run.kind === "single");
  $("collect").disabled = !(open && run.kind === "exam");
  $("hide").disabled = s.drawer === "hidden";
  if (!run) {
    $("run").innerHTML = `<p class="muted">目前沒有題目</p>`;
    return;
  }
  const head = run.kind === "single"
    ? `單題 · ${open ? "作答中" : "已公布"} · 已作答 ${run.participants}`
    : `考卷 ${run.questions.length} 題 · ${open ? "作答中" : "已收卷"} · 已交卷 ${run.submitted} / 作答 ${run.participants}`;
  $("run").innerHTML = `<p class="muted">${head}</p>` + run.questions.map((q) =>
    `<div class="card"><span class="rate">答對率 ${rateText(q.correct_rate)}</span>${renderQuestion(q, { showAnswer: true, showExplain: true })}</div>`
  ).join("");
}

// ---- 問助理 ----

const ROUTE_LABEL = { openevidence: "OpenEvidence", pubmed: "PubMed", model: "Gemini 一般知識（未經文獻驗證）",
  references: "參考資料", deck: "今天的簡報", page: "目前這一頁" };

async function loadReferences() {
  const sel = $("route");
  const keep = sel.value;
  let refs = [];
  try { refs = await (await fetch("/api/references")).json(); } catch (e) { /* AI 未啟用 */ }
  if (!Array.isArray(refs)) refs = [];
  sel.innerHTML = [
    `<option value="openevidence">搜尋（OpenEvidence，查不到再查 PubMed）</option>`,
    `<option value="pubmed">搜尋（PubMed）</option>`,
    `<option value="deck">今天的簡報</option>`,
    `<option value="page">目前這一頁</option>`,
    `<option value="references:">參考資料：全部（${refs.length} 份）</option>`,
    ...refs.map((r) => `<option value="references:${esc(r.name)}"${r.scanned ? " disabled" : ""}>參考資料：${esc(r.name)}${r.scanned ? "（掃描檔）" : ""}</option>`),
  ].join("");
  if ([...sel.options].some((o) => o.value === keep)) sel.value = keep;
}

function routeBody() {
  const v = $("route").value || "deck";
  if (v.startsWith("references:")) {
    const name = v.slice("references:".length);
    return { route: "references", refs: name ? [name] : [] };
  }
  return { route: v, refs: [] };
}

async function send(url, body) {
  $("phi").textContent = "";
  const res = await postJSON(url, body);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { alert(data.detail || "送出失敗"); return; }
  if (data.sent === false) $("phi").textContent = "疑似含病人識別資訊，未送出：" + (data.phi || []).join("、");
}

function ask() {
  const text = $("askText").value.trim();
  if (text) send("/api/ask", { text, ...routeBody() });
}

function generate() {
  const n = Number($("qn").value) || 5;
  send("/api/quiz/generate", { n, focus: $("askText").value.trim(), ...routeBody() });
}

function elapsed(job) {
  return Math.max(0, Math.round(Date.now() / 1000 - job.started_at));
}

function renderJobs(s) {
  const parts = [];
  if (s.answer_job) parts.push(`${s.answer_job.kind === "summary" ? "整理重點中" : "查詢中"}…（${esc(s.answer_job.label)}，已 ${elapsed(s.answer_job)} 秒）`);
  if (s.quiz_job) parts.push(`出題中…（${esc(s.quiz_job.label)}，已 ${elapsed(s.quiz_job)} 秒）`);
  if (s.summary_note && !s.answer_job) parts.push(`<span class="err">${esc(s.summary_note)}</span>`);
  $("jobs").innerHTML = parts.map((p) => `<div class="job">${p}</div>`).join("");
  if (s.phi_hits && s.phi_hits.length) $("phi").textContent = "疑似含病人識別資訊，未送出：" + s.phi_hits.join("、");
}

function renderAnswer(s) {
  const a = s.ai_answer;
  $("quotes").textContent = s.show_quotes ? "投影隱藏原文" : "投影顯示原文";
  $("showAns").disabled = !a || s.drawer === "answer" || (s.run && s.run.status === "open");
  $("hideAns").disabled = !["answer", "working"].includes(s.drawer);
  $("makeCard").disabled = !(a && a.status === "answered" && a.cards.length);
  $("hideCard").disabled = !s.card;
  // 手機（§6.3a）：投影控制（預設開）與搜尋網路（預設關）兩個開關
  $("phoneToggle").textContent = s.phone_control_enabled === false ? "手機投影控制：暫停" : "手機投影控制：開";
  $("phoneToggle").dataset.on = s.phone_control_enabled === false ? "" : "1";
  $("searchToggle").textContent = s.phone_search_enabled ? "手機搜尋網路：開" : "手機搜尋網路：關";
  $("searchToggle").dataset.on = s.phone_search_enabled ? "1" : "";
  renderTeacher(s);
  // 「關閉原文」固定在工具列（不在會整個重畫的回答區裡）
  $("sourceClose").style.display = s.source_view ? "" : "none";
  $("sourceClose").textContent = s.source_view ? `關閉原文（投影上：第 ${s.source_view.card} 張）` : "關閉原文";
  // 原文重點摘要簡報（§6.2b）：有原文畫面才能整理；整理中可取消
  const summarizing = s.answer_job && s.answer_job.kind === "summary";
  $("summaryStart").style.display = s.source_view && !s.answer_job ? "" : "none";  // 查詢或整理中不能開始（審查 #3）
  $("summaryCancel").style.display = summarizing ? "" : "none";
  if (!a) { $("answer").innerHTML = `<p class="muted">尚未發問</p>`; return; }
  let html = `<p class="muted">${esc(ROUTE_LABEL[a.route] || "")}：${esc(a.question)}</p>`;
  if (a.interpreted) html += `<p class="muted">${esc(appName())}理解為：${esc(a.interpreted)}</p>`;
  if (a.fallback_note) html += `<p class="muted">${esc(a.fallback_note)}</p>`;
  if (a.status === "answered" && a.oe_deck) {
    // OE 簡報（§6.2c）：OpenEvidence 結果完整照原文做成簡報；參考文獻在投影幕上點
    const p = a.oe_deck.play;
    html += `<div class="ans-card"><b>OE 簡報（${a.oe_deck.pages} 頁）</b>：${
      p === "oe_playing" ? "投影中" : p === "oe_ended" ? "已收回" : p === "oe_waiting_quiz" ? "派題中，收題後再播" : "已做好，尚未播放"}
      ${p === "oe_playing" ? `<button id="oeStop" class="secondary" data-oestop="1">收回 OE 簡報</button>`
        : `<button id="oePlay" data-oeplay="1">${p === "oe_ended" ? "再播一次" : "播放 OE 簡報"}</button>`}
      <div class="src">參考文獻請在投影幕上直接點選</div></div>`;
  } else if (a.status === "answered") {
    // 出處原文按鈕（§6.2a）：文獻 → 摘要／原文網頁；簡報、參考資料 → 那一頁
    const srcBtns = (c, i) => c.origin === "literature"
      ? `<button class="secondary" data-src="${i + 1}" data-kind="abstract">摘要</button>
         <button class="secondary" data-src="${i + 1}" data-kind="web">原文網頁</button>`
      : c.origin === "page" ? `<button class="secondary" data-src="${i + 1}" data-kind="page">那一頁</button>` : "";
    html += `<p class="muted">模型摘要（未逐句驗證，不投影）：${esc(a.summary_zh)}</p>` + a.cards.map((c, i) => `<div class="ans-card"><b>${i + 1}.</b> ${esc(c.point_zh)}
      ${c.quote ? `<div class="ans-quote">“${esc(c.quote)}”</div>` : ""}<div class="src">出處：${esc(c.source)} ${srcBtns(c, i)}</div></div>`).join("");
    html += (a.derivation_notes || []).map((n) => `<p class="err">${esc(appName())}說明（未經主治確認）：${esc(n)}</p>`).join("");
    html += (a.derivations || []).map((d) => `<div class="ans-card"><b>${esc(appName())}推導（未經主治確認）：</b>${esc(d.text)}
      <div class="src">依據：${d.sources.map(esc).join("；")}</div></div>`).join("");
    html += (a.tables || []).map((t) => `<div class="ans-card"><table style="border-collapse:collapse;font-size:12px">
      <tr>${t.header.map((h) => `<th style="border:1px solid #ccd;padding:2px 4px">${esc(h)}</th>`).join("")}</tr>
      ${t.rows.map((r) => `<tr>${r.map((c) => `<td style="border:1px solid #ccd;padding:2px 4px">${esc(c)}</td>`).join("")}</tr>`).join("")}
      </table><div class="src">表格出自 OpenEvidence：${t.sources.map(esc).join("；")}</div></div>`).join("");
    html += (a.figures || []).map((f, i) => `<div class="ans-card"><img src="${esc(f.url)}" alt="" style="max-width:100%;max-height:140px">
      <div class="src">${esc(f.name)}${f.caption ? "：" + esc(f.caption) : ""}（出自 ${esc(f.source)}）</div>
      ${s.figure_zoom === i ? `<button class="secondary" data-zoom="">收回放大</button>` : `<button class="secondary" data-zoom="${i}">投影放大</button>`}</div>`).join("");
  } else if (a.status === "clarify") {
    html += `<p>需要釐清：${esc(a.clarify_zh)}</p>`;
  } else if (a.status === "not_found") {
    html += `<p>${esc(a.summary_zh)}</p>`;
  } else {
    html += `<p class="err">${esc(a.error)}</p>`;
    if (a.error_detail) html += `<p class="src">${esc(a.error_detail)}</p>`;
  }
  $("answer").innerHTML = html;
}

function renderDrafts(s) {
  $("draftNotes").textContent = (s.draft_notes || []).join("；");
  const open = s.run && s.run.status === "open";
  $("publish").disabled = !s.drafts.length || open || !!s.quiz_job;
  $("publish").textContent = open ? "發出去（請先公布答案或收卷）"
    : `發出去（${s.draft_kind === "single" ? "單題" : `考卷 ${s.drafts.length} 題`}）`;
  $("drafts").innerHTML = s.drafts.length ? s.drafts.map((q, i) => `<div class="draft">
      <span class="muted">${i + 1}.</span> ${renderQuestion(q, { showAnswer: true, showExplain: true })}
      <div class="row"><button class="secondary" data-del="${esc(q.id)}">刪除</button>
        <button class="secondary" data-regen="${esc(q.id)}"${s.quiz_job ? " disabled" : ""}>重出這題</button></div></div>`).join("")
    : `<p class="muted">沒有草稿</p>`;
}

// ---- 語音 ----

const voiceText = () => ({
  off: "語音未啟用", standby: `待命（喊「${wakeWord()}」或按叫醒）`, listening: "聆聽中", thinking: "查詢中",
  speaking: "回答中", offline: "語音離線（稍後自動恢復；翻頁照常）",
});

let lastConfirm = null;

// 主持人手機的麥克風（§6.3b，2026-11-08）：手機按「叫醒」→ 這一輪用手機收音；回答照舊從筆電喇叭播
const VOICE_ACTIVE = ["listening", "thinking", "speaking"];
const phoneMic = { ws: null, ctx: null, stream: null, seenActive: false };

function stopPhoneMic() {
  const m = phoneMic;
  if (m.stream) m.stream.getTracks().forEach((t) => t.stop());
  if (m.ctx) m.ctx.close().catch(() => {});
  if (m.ws && m.ws.readyState <= 1) m.ws.close();
  Object.assign(m, { ws: null, ctx: null, stream: null, seenActive: false });
  if (snap) renderVoice(snap);
}

async function startPhoneMic() {
  stopPhoneMic();
  const stream = await navigator.mediaDevices.getUserMedia(
    { audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  await ctx.resume();
  const ws = new WebSocket(`wss://${location.host}/ws/mic`);
  ws.binaryType = "arraybuffer";
  Object.assign(phoneMic, { ws, ctx, stream, seenActive: false });
  const node = ctx.createScriptProcessor(4096, 1, 1);
  const ratio = ctx.sampleRate / 16000;
  node.onaudioprocess = (e) => {  // 轉成 16 kHz int16（和筆電麥克風相同格式）
    if (ws.readyState !== 1) return;
    const x = e.inputBuffer.getChannelData(0);
    const out = new Int16Array(Math.floor(x.length / ratio));
    for (let i = 0; i < out.length; i++) {
      const a = Math.floor(i * ratio), b = Math.max(a + 1, Math.floor((i + 1) * ratio));
      let sum = 0;
      for (let j = a; j < b; j++) sum += x[j];
      out[i] = Math.max(-1, Math.min(1, sum / (b - a))) * 32767;
    }
    ws.send(out.buffer);
  };
  ctx.createMediaStreamSource(stream).connect(node);
  node.connect(ctx.destination);  // 要接到輸出才會跑；輸出是靜音
  ws.onopen = () => { postJSON("/api/voice/wake"); renderVoice(snap); };
  ws.onclose = () => { if (phoneMic.ws === ws) stopPhoneMic(); };  // 這一輪結束，伺服器會關掉
}

function renderVoice(s) {
  const st = s.voice_status || "off";
  if (phoneMic.ws) {
    if (VOICE_ACTIVE.includes(st)) phoneMic.seenActive = true;
    else if (phoneMic.seenActive) stopPhoneMic();
  }
  $("voiceStatus").textContent = "狀態：" + (voiceText()[st] || st) + (phoneMic.ws ? "（手機麥克風收音中）" : "");
  $("voiceWake").disabled = st !== "standby";
  $("voiceStop").disabled = !["listening", "thinking", "speaking"].includes(st);
  $("voicePhi").style.display = s.voice_phi_alert ? "" : "none";
  const vc = s.voice_confirm;
  $("voiceConfirm").style.display = vc ? "" : "none";
  if (vc && vc.question !== lastConfirm) $("voiceConfirmText").value = vc.question;  // 不蓋掉主持人正在改的字
  lastConfirm = vc ? vc.question : null;
  if (vc) $("voiceConfirmRoute").textContent = vc.route === "pubmed" ? "PubMed" : "OpenEvidence";
  $("voiceRef").textContent = s.voice_ref ? `語音選定的參考資料：${s.voice_ref}` : "";
  $("voiceCands").innerHTML = (s.ref_candidates || []).length
    ? "找到多份，請選：" + s.ref_candidates.map((n) => `<button class="secondary" data-ref="${esc(n)}">${esc(n)}</button>`).join(" ")
    : "";
  const box = $("voiceTranscript");
  box.innerHTML = (s.voice_transcript || []).map((l) => `<div>${esc(l)}</div>`).join("");
  box.scrollTop = box.scrollHeight;
}

function apply(s) {
  snap = s;
  setApp(s.app);
  keys = s.keys || DEFAULT_KEYS;
  renderChecks(s);
  renderDeck(s);
  renderPhone(s);
  renderHostPhone(s);
  renderRun(s);
  renderJobs(s);
  renderAnswer(s);
  renderDrafts(s);
  renderVoice(s);
}

function connect() {
  const ws = new WebSocket(`${PHONE_HOST ? "wss" : "ws"}://${location.host}/ws?audience=host`);
  ws.onopen = () => { $("conn").textContent = "已連線"; $("conn").classList.remove("off"); };
  ws.onmessage = (ev) => apply(JSON.parse(ev.data));
  ws.onclose = () => {
    $("conn").textContent = "離線，重新連線中…";
    $("conn").classList.add("off");
    setTimeout(connect, 2000);
  };
  ws.onerror = () => ws.close();
}

$("hostPhonePair").onclick = async () => {
  $("hostPhoneMsg").textContent = "";
  const res = await postJSON("/api/host-phone/pair");
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { hideHostPhoneQr(); $("hostPhoneMsg").textContent = data.detail || "無法產生 QR Code"; return; }
  $("hostPhoneQr").src = data.qr;
  $("hostPhoneQr").style.display = "block";
  $("hostPhoneMsg").textContent = `請用手機掃描，${Math.round(data.expires_in / 60)} 分鐘內有效、只能用一次。不要投到投影幕上。`;
  clearTimeout(hostPhoneTimer);
  hostPhoneTimer = setTimeout(() => { hideHostPhoneQr(); $("hostPhoneMsg").textContent = "QR Code 已過期，要連接請重按一次"; }, data.expires_in * 1000);
};
$("hostPhoneRevoke").onclick = () => {  // 解除時配對碼也一起作廢
  hideHostPhoneQr();
  $("hostPhoneMsg").textContent = "";
  postJSON("/api/host-phone/revoke");
};
$("prev").onclick = () => postJSON("/api/nav", { action: "prev" });
$("next").onclick = () => postJSON("/api/nav", { action: "next" });
$("dispatch").onclick = () => postJSON("/api/quiz/dispatch");
$("reveal").onclick = () => postJSON("/api/quiz/reveal");
$("collect").onclick = () => postJSON("/api/quiz/collect");
$("hide").onclick = () => postJSON("/api/drawer/hide");
$("decks").onchange = async (ev) => {
  if (!ev.target.value) return;
  const res = await postJSON("/api/deck", { path: ev.target.value });
  if (!res.ok) alert("無法開啟這份簡報");
};
$("presentStop").onclick = () => postJSON("/api/present/stop");
$("present").onclick = async () => {
  const res = await postJSON("/api/present");
  if (!res.ok) alert("無法播放：" + ((await res.json().catch(() => ({}))).detail || res.status));
};
$("decks").onfocus = () => loadDecks(snap && snap.deck ? snap.deck.name : null);

document.addEventListener("keydown", makeKeyHandler(() => keys, {
  next: () => postJSON("/api/nav", { action: "next" }),
  prev: () => postJSON("/api/nav", { action: "prev" }),
  // B 鍵：投影上有原文畫面就先關原文，否則收起抽屜
  drawer: () => postJSON(snap && snap.source_view ? "/api/source/close" : "/api/drawer/hide"),
  wake: () => postJSON("/api/voice/wake"),
}));

$("voiceWake").onclick = async () => {
  if (!PHONE_HOST) return postJSON("/api/voice/wake");
  try {
    await startPhoneMic();  // 連上後才叫醒：第一句就收得到
  } catch (e) {
    stopPhoneMic();
    alert("無法使用手機麥克風：" + (e && e.message ? e.message : e));
  }
};
$("voiceStop").onclick = () => postJSON("/api/voice/stop");
$("voicePhiClear").onclick = () => postJSON("/api/voice/phi/clear");
$("voiceConfirmGo").onclick = () => postJSON("/api/voice/confirm", { question: $("voiceConfirmText").value });
$("voiceConfirmCancel").onclick = () => postJSON("/api/voice/confirm/cancel");
$("voiceConfirmText").addEventListener("keydown", (ev) => { if (ev.key === "Enter") $("voiceConfirmGo").click(); });
$("voiceCands").onclick = (ev) => {
  const b = ev.target.closest("button");
  if (b && b.dataset.ref) postJSON("/api/voice/ref", { name: b.dataset.ref });
};
$("ask").onclick = ask;
$("gen").onclick = generate;
$("askText").addEventListener("keydown", (ev) => { if (ev.key === "Enter" && ev.ctrlKey) { ev.preventDefault(); ask(); } });
$("route").onfocus = loadReferences;
$("showAns").onclick = () => postJSON("/api/answer/show");
$("hideAns").onclick = () => postJSON("/api/drawer/hide");
$("makeCard").onclick = async () => {
  $("cardMsg").textContent = "整理中…";
  const res = await postJSON("/api/answer/card");
  const data = await res.json().catch(() => ({}));
  $("cardMsg").textContent = res.ok ? `已存：紀錄\\字卡\\${data.file}` : (data.detail || "無法整理成字卡");
};
$("hideCard").onclick = () => postJSON("/api/answer/card/hide");
$("answer").addEventListener("click", (ev) => {
  if (ev.target.closest("button[data-oeplay]")) postJSON("/api/oe/play");
  if (ev.target.closest("button[data-oestop]")) postJSON("/api/oe/stop");
});
$("answer").onclick = async (ev) => {
  const b = ev.target.closest("button[data-zoom]");
  if (b) postJSON("/api/answer/figure", { index: b.dataset.zoom === "" ? null : Number(b.dataset.zoom) });
  const src = ev.target.closest("button[data-src]");
  if (src) {
    $("sourceMsg").textContent = "開啟中…";
    const r = await postJSON("/api/source/show", { card: Number(src.dataset.src), kind: src.dataset.kind });
    const res = await r.json().catch(() => ({}));
    // 不用 alert：對話框沒按掉之前，整個主持人畫面的按鈕都按不動
    $("sourceMsg").textContent = res.status && res.status !== "ok" ? (res.message || "無法顯示原文") : "";
  }
};
// 結束程式：按一次只跳出確認列（不用 confirm()，避免擋住畫面更新）；10 秒沒按就自動收起
let quitTimer = null;
function hideQuitConfirm() {
  clearTimeout(quitTimer);
  $("quitConfirm").classList.remove("show");
  $("quitMsg").textContent = "";
}
$("quit").onclick = () => {
  $("quitConfirm").classList.add("show");
  $("quitYes").disabled = false;
  $("quitNo").focus();
  clearTimeout(quitTimer);
  quitTimer = setTimeout(hideQuitConfirm, 10000);
};
$("quitNo").onclick = hideQuitConfirm;
$("quitYes").onclick = async () => {
  clearTimeout(quitTimer);
  $("quitYes").disabled = true;
  $("quitMsg").textContent = "結束中…";
  try {
    const res = await postJSON("/api/quit");
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      $("quitMsg").textContent = body.detail || "無法結束";
      $("quitYes").disabled = false;
      return;
    }
    $("quitConfirm").classList.remove("show");
    $("quitDone").classList.add("show");
  } catch (e) {
    $("quitMsg").textContent = `無法連線到${appName()}`;
    $("quitYes").disabled = false;
  }
};

// OpenEvidence 黃燈那一列的「重新檢查」
$("checks").addEventListener("click", async (ev) => {
  const b = ev.target.closest("button[data-recheck]");
  if (!b) return;
  b.disabled = true;
  b.textContent = "檢查中…";
  try { await postJSON("/api/check/openevidence"); } catch (e) { /* 畫面會隨快照更新 */ }
  b.disabled = false;
  b.textContent = "重新檢查";
});
$("phoneToggle").onclick = () => postJSON("/api/phone/control-enabled", { enabled: !$("phoneToggle").dataset.on });
$("searchToggle").onclick = () => postJSON("/api/phone/search-enabled", { enabled: !$("searchToggle").dataset.on });

// 手機「詢問老師」：投影（不顯示是誰問的）、用助理查這題、移除；疑似含識別資訊的不能投影
let teacherKey = "";
function renderTeacher(s) {
  const qs = s.teacher_questions || [];
  const key = JSON.stringify(qs);
  if (key === teacherKey) return;
  teacherKey = key;
  const anyShown = qs.some((q) => q.shown);
  $("teacherList").innerHTML = (anyShown ? `<div class="row" style="margin:0 0 8px">
      <button class="secondary" data-tq="hide-all">投影上的提問全部收回</button></div>` : "")
    + (qs.length ? qs.slice().reverse().map((q) => `<div class="ans-card">
      ${q.phi ? `<div class="err">⚠ 疑似含病人識別資訊（${esc(q.phi_hits.join("、"))}），不能投影</div>` : ""}
      <div>${esc(q.text)}</div>
      <div class="row" style="margin-top:6px">
        ${q.shown ? `<span class="src">投影中</span> <button class="secondary" data-tq="hide" data-id="${esc(q.id)}">收回</button>`
                  : `<button class="secondary" data-tq="show" data-id="${esc(q.id)}" ${q.phi ? "disabled" : ""}>顯示在投影幕</button>`}
        <button class="secondary" data-tq="ask" data-id="${esc(q.id)}">用${esc(appName())}查這題</button>
        <button class="secondary" data-tq="remove" data-id="${esc(q.id)}">移除</button>
      </div></div>`).join("") : `<p class="muted">還沒有提問</p>`);
}
$("teacherList").addEventListener("click", async (ev) => {
  const b = ev.target.closest("button[data-tq]");
  if (!b) return;
  const r = await (await postJSON(`/api/teacher/${b.dataset.tq}`, { id: b.dataset.id || "" })).json().catch(() => ({}));
  if (r.ok === false && r.message) $("sourceMsg").textContent = r.message;
});
$("summaryStart").onclick = async () => {
  $("sourceMsg").textContent = "";
  const r = await postJSON("/api/summary/start");
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    $("sourceMsg").textContent = body.detail || "無法整理原文";
  }
};
$("summaryCancel").onclick = () => postJSON("/api/summary/cancel");
$("sourceClose").onclick = () => { $("sourceMsg").textContent = ""; postJSON("/api/source/close"); };
$("quotes").onclick = () => postJSON("/api/answer/quotes");
$("publish").onclick = async () => {
  const res = await postJSON("/api/drafts/publish");
  if (!res.ok) alert((await res.json().catch(() => ({}))).detail || "無法派出");
};
$("drafts").onclick = (ev) => {
  const b = ev.target.closest("button");
  if (!b) return;
  if (b.dataset.del) postJSON("/api/drafts/remove", { id: b.dataset.del });
  else if (b.dataset.regen) postJSON("/api/drafts/regenerate", { id: b.dataset.regen });
};
setInterval(() => { if (snap && (snap.answer_job || snap.quiz_job)) renderJobs(snap); }, 1000);

const drop = $("drop");
drop.ondragover = (ev) => { ev.preventDefault(); drop.classList.add("over"); };
drop.ondragleave = () => drop.classList.remove("over");
drop.ondrop = async (ev) => {
  ev.preventDefault();
  drop.classList.remove("over");
  for (const file of ev.dataTransfer.files) {
    drop.textContent = `上傳中：${file.name}…`;
    const fd = new FormData();
    fd.append("file", file);
    const res = await fetch("/api/references/upload", { method: "POST", body: fd });
    const data = await res.json().catch(() => ({}));
    drop.textContent = res.ok
      ? `已加入：${data.name}（${data.pages} 頁${data.scanned ? "，掃描檔無法讀取" : ""}）`
      : `無法加入 ${file.name}：${data.detail || "失敗"}`;
  }
  loadReferences();
};

// ---- 設定：名字、喚醒詞、科別、會議類型 ----

const SETTING_LABEL = { display_name: "名字", wake_word: "喚醒詞", meeting_type: "會議類型" };
const OTHER = "其它";

function syncMeetingOther() {
  $("setMeetingOther").style.display = $("setMeeting").value === OTHER ? "" : "none";
}

function settingsMessage(text, isError) {
  $("settingsMsg").textContent = text;
  $("settingsMsg").className = isError ? "err" : "src";
}

async function loadSettings() {
  try {
    const res = await fetch("/api/settings");
    if (!res.ok) return;
    const s = await res.json();
    $("setName").value = s.display_name;
    $("setWake").value = s.wake_word;
    $("setSpecialty").value = s.specialty;
    $("setMeeting").innerHTML = s.meeting_types.map((m) => `<option value="${esc(m)}">${esc(m)}</option>`).join("");
    $("setMeeting").value = s.meeting_type;
    $("setMeetingOther").value = s.meeting_type_other;
    syncMeetingOther();
  } catch (e) { /* 連線恢復後重新整理頁面即可 */ }
}

$("setMeeting").onchange = syncMeetingOther;
$("settingsSave").onclick = async () => {
  $("settingsSave").disabled = true;
  settingsMessage("儲存中…", false);
  try {
    const res = await postJSON("/api/settings", {
      display_name: $("setName").value, wake_word: $("setWake").value, specialty: $("setSpecialty").value,
      meeting_type: $("setMeeting").value, meeting_type_other: $("setMeetingOther").value,
    });
    const body = await res.json().catch(() => ({}));
    if (res.status === 422 && body.errors) {
      settingsMessage(Object.entries(body.errors).map(([k, v]) => `${SETTING_LABEL[k] || k}：${v}`).join("；"), true);
    } else if (!res.ok) {
      settingsMessage("儲存失敗，請再試一次", true);
    } else {
      settingsMessage(body.restart_needed ? "已儲存。名字與喚醒詞在重新啟動後生效。" : "已儲存，立即生效。", false);
    }
  } catch (e) {
    settingsMessage("無法連線，請再試一次", true);
  }
  $("settingsSave").disabled = false;
};

$("diagBtn").onclick = async () => {
  $("diagBtn").disabled = true;
  $("diagMsg").className = "src";
  $("diagMsg").textContent = "產生中…";
  try {
    const res = await postJSON("/api/diagnostics", {});
    const body = await res.json().catch(() => ({}));
    if (res.ok && body.ok) {
      $("diagMsg").textContent = `已產生：${body.path}（請把這個檔案寄給開發者）`;
    } else {
      $("diagMsg").className = "err";
      $("diagMsg").textContent = body.detail || "產生診斷包失敗，請再試一次";
    }
  } catch (e) {
    $("diagMsg").className = "err";
    $("diagMsg").textContent = "無法連線，請再試一次";
  }
  $("diagBtn").disabled = false;
};

loadSettings();
loadReferences();
connect();
