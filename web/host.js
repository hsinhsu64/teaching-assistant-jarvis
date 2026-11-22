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

// 開機自我檢查（左欄最上面，和「設定與工具」一樣用箭頭收合）：結果一出來先全部列出，
// 綠燈 5 秒後收起，收起時只留黃燈、紅燈、確認中與警告；按箭頭可再看全部項目。
// 之後才變綠的（重新檢查、手機確認成功）也是 5 秒後收起；綠燈又出問題就重新出現。
const GREEN_LINGER_MS = 5000;
const greenSince = new Map();  // 項目名稱 → 這次開始是綠燈的時間
let checksShowAll = false;
let checksTimer = null;

function renderChecks(s) {
  const now = Date.now();
  for (const c of s.checks) {
    if (c.level !== "ok") greenSince.delete(c.name);
    else if (!greenSince.has(c.name)) greenSince.set(c.name, now);
  }
  const lingering = (c) => c.level === "ok" && now - greenSince.get(c.name) < GREEN_LINGER_MS;
  const rows = checksShowAll ? s.checks : s.checks.filter((c) => c.level !== "ok" || lingering(c));
  $("checks").innerHTML = rows.map((c) => `<li class="lv-${c.level}"><b>${esc(c.name)}</b>：${esc(c.detail)}${
      c.name === "OpenEvidence" && c.level !== "ok" ? ` <button class="secondary" data-recheck="oe">重新檢查</button>` : ""}${
      c.hint && c.level !== "ok" ? `<div class="hint"><b>怎麼修：</b>${esc(c.hint)}</div>` : ""}</li>`).join("");
  $("warnings").innerHTML = s.warnings.map((w) => `<li>${esc(w)}</li>`).join("");
  const folded = s.checks.length - rows.length;
  $("checksFoldStatus").dataset.folded = folded ? `（綠燈 ${folded} 項已收起，按箭頭看全部）` : "";
  // 下一個綠燈滿 5 秒時再畫一次
  clearTimeout(checksTimer);
  const waits = s.checks.filter(lingering).map((c) => greenSince.get(c.name) + GREEN_LINGER_MS - now);
  if (waits.length) checksTimer = setTimeout(() => { if (snap) renderChecks(snap); }, Math.min(...waits) + 50);
  renderChecksSummary(s);
}

function renderChecksSummary(s) {
  const count = (lv) => s.checks.filter((c) => c.level === lv).length;
  const fail = count("fail"), warn = count("warn"), pending = count("pending"), notes = s.warnings.length;
  const parts = [fail && `${fail} 項紅燈`, warn && `${warn} 項黃燈`, notes && `${notes} 則警告`,
    pending && `確認中 ${pending} 項`].filter(Boolean);
  const level = fail ? "fail" : warn || notes ? "warn" : pending || !s.checks.length ? "pending" : "ok";
  $("checksToggle").dataset.level = level;
  const text = !s.checks.length ? "尚無結果" : parts.length ? parts.join("、") : "全部正常";
  $("checksSummary").textContent = "自我檢查：" + text;
  $("secChecks").dataset.level = level;
  $("checksFoldStatus").textContent = `· ${text}${checksShowAll ? "" : $("checksFoldStatus").dataset.folded || ""}`;
  $("checksFold").setAttribute("aria-expanded", checksShowAll ? "true" : "false");
  $("checksFold").querySelector(".caret").textContent = checksShowAll ? "▼" : "▶";
}

// 投影幕狀態（§6.0）：「播放簡報」一直顯示，播下拉選單選的那份；播放中要先回到主畫面（2026-11-08）
function renderPresent(s) {
  $("presentMode").textContent = s.presenting ? "投影幕：播放中" : "投影幕：主畫面";
  $("presentStop").disabled = !s.presenting;
  $("present").style.display = "";
  $("present").disabled = (!s.deck && !s.deck_converting) || !!s.presenting;
  $("present").title = s.presenting ? "播放中請先回到主畫面" : "";
  $("decks").disabled = !!s.presenting;
  $("decks").title = s.presenting ? "播放中請先回到主畫面" : "";
  const c = s.deck_candidates || [];
  $("deckCands").textContent = c.length ? `語音找到幾份簡報：${c.join("、")}（請說出是哪一份）` : "";
  const conv = s.deck_converting;
  $("deckConv").textContent = conv
    ? `「${conv.name}」要先轉成 PDF，需要一段時間，好了會自動${conv.play ? "播放" : "打開"}…` : "";
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

// 主持人手機連線（§6.3b，2026-11-19 搬到頂端）：同一顆按鈕切換。按一下產生一次性 QR Code、按鈕變「收回 QR Code」；
// 再按一下收回 QR Code（不作廢：2 分鐘內仍可掃）、按鈕恢復「主持人手機連線」。
// 連上的手機一直有效到程式結束；再產生 QR Code 讓另一支手機掃，舊的那支自動換掉。
let hostPaired = null;
let hostPhoneMsgTimer = null;

function qrShown() {
  return $("hostPhoneQr").style.display === "block";
}

function syncHostPhonePanel() {
  $("hostPhonePanel").classList.toggle("show", qrShown() || !!$("hostPhoneMsg").textContent);
  $("hostPhonePair").textContent = qrShown() ? "收回 QR Code" : "主持人手機連線";
  $("hostPhonePair").setAttribute("aria-pressed", qrShown() ? "true" : "false");
}

// 訊息；brief：幾秒後自己消失（例如「已過期」「已連上」），面板跟著收起
function setHostPhoneMsg(text, brief = false) {
  clearTimeout(hostPhoneMsgTimer);
  $("hostPhoneMsg").textContent = text;
  if (brief && text) hostPhoneMsgTimer = setTimeout(() => setHostPhoneMsg(""), 6000);
  syncHostPhonePanel();
}

function hideHostPhoneQr() {
  clearTimeout(hostPhoneTimer);
  $("hostPhoneQr").style.display = "none";
  $("hostPhoneQr").removeAttribute("src");
  syncHostPhonePanel();
}

function renderHostPhone(s) {
  const paired = !!(s.host_phone && s.host_phone.paired);
  if (paired === hostPaired) return;  // 狀態沒變，不動（QR Code 可能正在顯示）
  const justPaired = hostPaired === false && paired;
  hostPaired = paired;
  if (justPaired) {  // 手機剛掃好：QR Code 已用掉，收起來
    hideHostPhoneQr();
    setHostPhoneMsg("主持人手機已連上（到程式結束都有效）", true);
  }
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
  let refs = [];
  try { refs = await (await fetch("/api/references")).json(); } catch (e) { /* AI 未啟用 */ }
  if (!Array.isArray(refs)) refs = [];
  fillRoute($("route"), refs, "openevidence");
  fillRoute($("quizRoute"), refs, "deck");  // 出題通常出自今天的簡報
}

function fillRoute(sel, refs, initial) {
  const keep = sel.value || initial;
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

function routeBody(selId = "route") {
  const v = $(selId).value || "deck";
  if (v.startsWith("references:")) {
    const name = v.slice("references:".length);
    return { route: "references", refs: name ? [name] : [] };
  }
  return { route: v, refs: [] };
}

async function send(url, body, phiId = "phi") {
  $(phiId).textContent = "";
  const res = await postJSON(url, body);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { alert(data.detail || "送出失敗"); return; }
  if (data.sent === false) $(phiId).textContent = "疑似含病人識別資訊，未送出：" + (data.phi || []).join("、");
}

function ask() {
  const text = $("askText").value.trim();
  if (text) send("/api/ask", { text, ...routeBody() });
}

function generate() {
  const n = Number($("qn").value) || 5;
  send("/api/quiz/generate", { n, focus: $("quizFocus").value.trim(), ...routeBody("quizRoute") }, "quizPhi");
}

function elapsed(job) {
  return Math.max(0, Math.round(Date.now() / 1000 - job.started_at));
}

function renderJobs(s) {
  const parts = [];
  if (s.answer_job) parts.push(`${s.answer_job.kind === "summary" ? "整理重點中" : "查詢中"}…（${esc(s.answer_job.label)}，已 ${elapsed(s.answer_job)} 秒）`);
  if (s.summary_note && !s.answer_job) parts.push(`<span class="err">${esc(s.summary_note)}</span>`);
  $("jobs").innerHTML = parts.map((p) => `<div class="job">${p}</div>`).join("");
  $("quizJobs").innerHTML = s.quiz_job
    ? `<div class="job">出題中…（${esc(s.quiz_job.label)}，已 ${elapsed(s.quiz_job)} 秒）</div>` : "";
  if (s.phi_hits && s.phi_hits.length) $("phi").textContent = "疑似含病人識別資訊，未送出：" + s.phi_hits.join("、");
}

// 回答區工具列：依當下狀況決定每顆按鈕能不能按（白色＝能按，灰色＝不能按）。
// OE 回答（§6.2c）的播放／收回 OE 簡報也放在這排：第一組按鈕換成 OE 的用法。
function renderAnswerTools(s) {
  const a = s.ai_answer;
  const answered = !!(a && a.status === "answered");
  const oe = answered && !!a.oe_deck && !s.answer_job;  // 查詢新問題時，第一組回到「投影／收起回答」
  $("showAns").dataset.oe = oe ? "1" : "";
  if (oe) {
    const p = a.oe_deck.play;
    $("showAns").textContent = p === "oe_ended" ? "再播一次 OE 簡報" : "播放 OE 簡報";
    // 派題中先等；收題後不會自己播，要按這顆（或用語音）
    $("showAns").disabled = p === "oe_playing" || (p === "oe_waiting_quiz" && !!(s.run && s.run.status === "open"));
    $("hideAns").textContent = "收回 OE 簡報";
    $("hideAns").disabled = p !== "oe_playing";
  } else {
    $("showAns").textContent = "投影回答";
    $("showAns").disabled = !a || s.drawer === "answer" || (s.run && s.run.status === "open");
    $("hideAns").textContent = "收起回答";
    $("hideAns").disabled = !["answer", "working"].includes(s.drawer);
  }
  // 回答一出來就自動存成字卡 PNG（2026-11-19）
  $("cardMsg").textContent = answered && a.card_file ? `已存：紀錄\\字卡\\${a.card_file}` : "";
  // 原文引句：只有一般回答有；已經打開的話一定能按（才關得掉）
  $("quotes").textContent = s.show_quotes ? "投影隱藏原文" : "投影顯示原文";
  $("quotes").disabled = !s.show_quotes && !(answered && !a.oe_deck && a.cards.some((c) => c.quote));
  // 「關閉原文」固定在工具列（不在會整個重畫的回答區裡）
  $("sourceClose").disabled = !s.source_view;
  $("sourceClose").textContent = s.source_view ? `關閉原文（投影上：第 ${s.source_view.card} 張）` : "關閉原文";
  // 原文重點摘要簡報（§6.2b）：有原文畫面才能整理；整理中同一顆變成「取消整理」
  const summarizing = !!(s.answer_job && s.answer_job.kind === "summary");
  $("summaryStart").dataset.cancel = summarizing ? "1" : "";
  $("summaryStart").textContent = summarizing ? "取消整理" : "整理重點";
  $("summaryStart").disabled = !summarizing && !(s.source_view && !s.answer_job);  // 查詢或整理中不能開始（審查 #3）
}

let lastAnswerId = null;

function renderAnswer(s) {
  const a = s.ai_answer;
  renderAnswerTools(s);
  const id = a ? a.id : null;
  if (id !== lastAnswerId) {  // 換了一個回答：上一個回答的字卡／原文訊息不再適用
    if (lastAnswerId !== null) { $("sourceMsg").textContent = ""; }
    lastAnswerId = id;
  }
  // 手機（§6.3a）：投影控制與搜尋文獻兩個開關（2026-11-18 起都預設關）
  $("phoneToggle").textContent = s.phone_control_enabled ? "手機投影控制：開" : "手機投影控制：關";
  $("phoneToggle").dataset.on = s.phone_control_enabled ? "1" : "";
  $("searchToggle").textContent = s.phone_search_enabled ? "手機搜尋文獻：開" : "手機搜尋文獻：關";
  $("searchToggle").dataset.on = s.phone_search_enabled ? "1" : "";
  renderTeacher(s);
  if (!a) { $("answer").innerHTML = `<p class="muted">尚未發問</p>`; return; }
  let html = `<p class="muted">${esc(ROUTE_LABEL[a.route] || "")}：${esc(a.question)}</p>`;
  if (a.interpreted) html += `<p class="muted">${esc(appName())}理解為：${esc(a.interpreted)}</p>`;
  if (a.fallback_note) html += `<p class="muted">${esc(a.fallback_note)}</p>`;
  if (a.status === "answered" && a.oe_deck) {
    // OE 簡報（§6.2c）：OpenEvidence 結果完整照原文做成簡報；參考文獻在投影幕上點。
    // 播放／收回在上方工具列，這裡只寫狀態。
    const p = a.oe_deck.play;
    html += `<div class="ans-card"><b>OE 簡報（${a.oe_deck.pages} 頁）</b>：${
      p === "oe_playing" ? "投影中" : p === "oe_ended" ? "已收回" : p === "oe_waiting_quiz" && s.run && s.run.status === "open" ? "派題中，收題後再播" : "已做好，尚未播放"}
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

// 「出題與作答」預設收起：標題列寫目前狀況；有新草稿或新的一題開始作答就自動打開一次，
// 主持人手動收起後，要到下一批新草稿或下一題開始作答才再打開。
let seenDraftIds = new Set();
let seenRunId = null;

function renderQuizFold(s) {
  const run = s.run;
  const open = !!(run && run.status === "open");
  const parts = [];
  if (s.quiz_job) parts.push("出題中…");
  if (s.drafts.length) parts.push(`草稿 ${s.drafts.length} 題`);
  if (run) parts.push(open
    ? (run.kind === "single" ? `作答中（已作答 ${run.participants}）` : `作答中（已交卷 ${run.submitted}／作答 ${run.participants}）`)
    : (run.kind === "single" ? "已公布答案" : "已收卷"));
  $("quizSummary").textContent = parts.length ? "· " + parts.join(" · ") : "";
  $("quizSummary").classList.toggle("live", open || !!s.quiz_job);
  const ids = s.drafts.map((q) => q.id);
  if (ids.some((id) => !seenDraftIds.has(id)) || (open && run.id !== seenRunId)) $("secQuiz").open = true;
  seenDraftIds = new Set(ids);
  seenRunId = run ? run.id : null;
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

// 會議錄音：按鈕開始／停止；錄音中每秒更新時間（伺服器只在狀態改變時送快照）
let recBase = null;  // {elapsed, at}
function mmss(sec) {
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), x = sec % 60;
  return (h ? h + ":" + String(m).padStart(2, "0") : String(m).padStart(2, "0")) + ":" + String(x).padStart(2, "0");
}
function renderRecordLabel() {
  const r = snap && snap.recording;
  const btn = $("recordToggle");
  if (r && r.active && recBase) {
    btn.textContent = "■ 停止錄音";
    btn.classList.add("on");
    if (!(r.error)) $("recordMsg").textContent = `錄音中 ${mmss(recBase.elapsed + (Date.now() - recBase.at) / 1000)}（投影幕顯示「● 錄音中」）`;
  } else {
    btn.textContent = "● 開始錄音";
    btn.classList.remove("on");
  }
}
// 錄音音量條（使用者 2026-11-23）：錄音中每 0.2 秒（主持人手機 0.5 秒）問一次音量
let levelTimer = null;
function drawLevel(v) {
  const bars = $("recMeter").children;
  const n = Math.round(Math.max(0, Math.min(1, v)) * bars.length);
  for (let i = 0; i < bars.length; i++) bars[i].className = i < n ? (i >= 9 ? "lit hot" : i >= 7 ? "lit warn" : "lit") : "";
}
async function pollLevel() {
  try {
    const r = await (await fetch("/api/recording/level")).json();
    drawLevel(r.active ? r.level : 0);
  } catch (e) { /* 下一次再試 */ }
}
function setLevelPolling(on) {
  $("recMeter").classList.toggle("on", on);
  if (on && !levelTimer) levelTimer = setInterval(pollLevel, PHONE_HOST ? 500 : 200);
  if (!on && levelTimer) { clearInterval(levelTimer); levelTimer = null; drawLevel(0); }
}

function renderRecording(s) {
  const r = s.recording || {};
  setLevelPolling(!!r.active);
  $("recordToggle").disabled = !r.available;
  recBase = r.active ? { elapsed: r.elapsed || 0, at: Date.now() } : null;
  renderRecordLabel();
  if ((r.saved || null) !== lastSaved) { lastSaved = r.saved || null; loadMeetings(); }
  if (typeof renderMeetings === "function" && $("meetingList")) renderMeetings();
  const msg = $("recordMsg");
  msg.classList.toggle("err", !!r.error);
  if (r.error) msg.textContent = r.error;
  else if (!r.active) msg.textContent = r.saved ? `已存檔：${r.saved}（等待轉寫）` : "";
  renderRecordLabel();
}
setInterval(renderRecordLabel, 1000);

function apply(s) {
  snap = s;
  setApp(s.app);
  keys = s.keys || DEFAULT_KEYS;
  renderChecks(s);
  renderDeck(s);
  renderHostPhone(s);
  renderRun(s);
  renderJobs(s);
  renderAnswer(s);
  renderDrafts(s);
  renderQuizFold(s);
  renderVoice(s);
  renderRecording(s);
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
  if (qrShown()) {  // 收回 QR Code：只是收起來，不作廢（2 分鐘內仍可掃）
    hideHostPhoneQr();
    setHostPhoneMsg("");
    return;
  }
  setHostPhoneMsg("");
  const res = await postJSON("/api/host-phone/pair");
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { hideHostPhoneQr(); setHostPhoneMsg(data.detail || "無法產生 QR Code", true); return; }
  $("hostPhoneQr").src = data.qr;
  $("hostPhoneQr").style.display = "block";
  setHostPhoneMsg(`請用手機掃描，${Math.round(data.expires_in / 60)} 分鐘內有效、只能用一次。不要投到投影幕上。`);
  clearTimeout(hostPhoneTimer);
  hostPhoneTimer = setTimeout(() => { hideHostPhoneQr(); setHostPhoneMsg("QR Code 已過期，要連接請重按一次", true); },
                              data.expires_in * 1000);
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
  if (!res.ok) alert((await res.json().catch(() => ({}))).detail || "無法開啟這份簡報");
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
$("recordToggle").onclick = async () => {
  const active = snap && snap.recording && snap.recording.active;
  $("recordToggle").disabled = true;  // 存檔要幾秒：避免連按
  const res = await postJSON(active ? "/api/recording/stop" : "/api/recording/start");
  if (!res.ok) alert((await res.json().catch(() => ({}))).detail || "錄音失敗");
  $("recordToggle").disabled = false;
};
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
$("quizRoute").onfocus = loadReferences;
$("showAns").onclick = () => postJSON($("showAns").dataset.oe ? "/api/oe/play" : "/api/answer/show");
$("hideAns").onclick = () => postJSON($("showAns").dataset.oe ? "/api/oe/stop" : "/api/drawer/hide");
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
// 左欄的箭頭：展開／收起全部項目；頂端摘要：展開並捲到左欄的自我檢查
$("checksFold").onclick = () => { checksShowAll = !checksShowAll; if (snap) renderChecks(snap); };
$("checksToggle").onclick = () => {
  checksShowAll = true;
  if (snap) renderChecks(snap);
  $("secChecks").scrollIntoView({ behavior: "smooth", block: "start" });
};
// 頂端「⚙ 設定」：展開最下面的「設定與工具」並捲過去
$("toolsToggle").onclick = () => {
  $("secTools").open = true;
  $("secTools").scrollIntoView({ behavior: "smooth", block: "start" });
};

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
                  : `<button data-tq="show" data-id="${esc(q.id)}" ${q.phi ? "disabled" : ""}>顯示在投影幕</button>`}
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
  if ($("summaryStart").dataset.cancel) return postJSON("/api/summary/cancel");
  $("sourceMsg").textContent = "";
  const r = await postJSON("/api/summary/start");
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    $("sourceMsg").textContent = body.detail || "無法整理原文";
  }
};
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

// ---- 設定：資料夾（2026-11-19）：跳出這台電腦的選資料夾視窗，選已有的或新增；重新啟動後生效 ----

$("setFolderPick").onclick = async () => {
  const msg = $("folderMsg");
  msg.className = "src";
  msg.textContent = "請在這台電腦跳出的視窗裡選資料夾（可按「新增資料夾」建立）…";
  $("setFolderPick").disabled = true;
  try {
    const res = await postJSON("/api/settings/folder");
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      msg.className = "src err";
      msg.textContent = body.detail || "無法更換資料夾";
    } else if (body.cancelled) {
      msg.textContent = "沒有更換";
    } else {
      $("setFolder").textContent = body.folder;
      msg.textContent = body.restart_needed
        ? "已換好，重新啟動後生效（按「結束程式」再重新啟動）。原本資料夾裡的檔案不會搬，需要的話請自己複製過去。"
        : "沒有變更（就是目前的資料夾）";
    }
  } finally {
    $("setFolderPick").disabled = false;
  }
};

// ---- 設定：名字、喚醒詞、科別、會議類型 ----

const SETTING_LABEL = { display_name: "名字", wake_word: "喚醒詞", meeting_type: "會議類型",
  audio_device: "麥克風", speaker_device: "喇叭", wake_sensitivity: "喚醒靈敏度" };

function fillDeviceSelect(id, options, value) {
  $(id).innerHTML = options.map((o) => `<option value="${esc(o.value)}">${esc(o.label)}</option>`).join("");
  $(id).value = value;
}
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
    $("setFolder").textContent = s.data_root || "（尚未設定）";
    $("setName").value = s.display_name;
    $("setWake").value = s.wake_word;
    $("setSpecialty").value = s.specialty;
    $("setMeeting").innerHTML = s.meeting_types.map((m) => `<option value="${esc(m)}">${esc(m)}</option>`).join("");
    $("setMeeting").value = s.meeting_type;
    $("setMeetingOther").value = s.meeting_type_other;
    syncMeetingOther();
    fillDeviceSelect("setMic", s.mic_options, s.audio_device);
    fillDeviceSelect("setSpeaker", s.speaker_options, s.speaker_device);
    fillDeviceSelect("setWakeSens", s.wake_sensitivity_options, s.wake_sensitivity);
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
      audio_device: $("setMic").value, speaker_device: $("setSpeaker").value,
      wake_sensitivity: $("setWakeSens").value,
    });
    const body = await res.json().catch(() => ({}));
    if (res.status === 422 && body.errors) {
      settingsMessage(Object.entries(body.errors).map(([k, v]) => `${SETTING_LABEL[k] || k}：${v}`).join("；"), true);
    } else if (!res.ok) {
      settingsMessage("儲存失敗，請再試一次", true);
    } else {
      settingsMessage(body.restart_needed
        ? "已儲存。名字、喚醒詞、麥克風／喇叭與喚醒靈敏度在重新啟動後生效（按「結束程式」再重新啟動）。"
        : "已儲存，立即生效。", false);
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

// ---- 會議記錄（錄音 → 逐字稿 → 填講者、確認去識別 → Gemini 整理）----
const MEETING_STATUS = {
  waiting: "等待家裡電腦轉寫", overdue: "家裡電腦還沒轉好", transcribing: "轉寫中",
  transcribed: "逐字稿好了", minutes_done: "會議記錄好了", error: "轉寫失敗",
};
const KIND_LABEL = { person: "人名", chart: "病歷號", id: "身分證", phone: "電話", birthday: "生日", bed: "床號", other: "其他" };
let meetingRows = [];
let review = null;        // {id, speakers, candidates, known_names}
let lastSaved = null;
const watchingJobs = new Set();

function fmtMinutes(sec) {
  const m = Math.round(sec / 60);
  return m >= 1 ? `${m} 分` : `${Math.round(sec)} 秒`;
}

async function loadMeetings() {
  try {
    // 主持人手機（經對外通道）只拿進度清單：沒有病人資料、檔案路徑
    const res = await fetch(PHONE_HOST ? "/api/meeting-status" : "/api/meetings");
    if (!res.ok) return;
    meetingRows = await res.json();
  } catch (e) { return; }
  if (PHONE_HOST) for (const r of meetingRows) if (r.job) r.minutes_job = { status: r.job, phone: true };
  renderMeetings();
  if (PHONE_HOST) return;  // 手機不追整理細節；清單每 15 秒更新
  for (const r of meetingRows) {
    if (r.minutes_job && r.minutes_job.status === "running") watchJob(r.id);
  }
}

function jobText(job) {
  if (!job) return "";
  if (job.phone) {
    const t = { running: "整理中（約 1～3 分鐘）…", ok: "會議記錄好了（在筆電檢視）",
                blocked: "沒有送出：還有疑似病人資料，請在筆電處理", failed: "整理失敗，請在筆電查看" }[job.status];
    return t ? `<span class="st">${esc(t)}</span>` : "";
  }
  if (job.status === "running") return `<span class="st">整理中（約 1～3 分鐘）…</span>`;
  if (job.status === "ok") {
    const warn = (job.unresolved || []).length
      ? `<div class="err">● 有代號換不回真名，請打開檔案手動改：${job.unresolved.map(esc).join("、")}</div>` : "";
    return `<div class="src">已存：${esc(job.docx)}（另有 .md）</div>${warn}`;
  }
  if (job.status === "blocked") {
    return `<div class="err">● 換完代號後還有疑似病人資料，沒有送出。請在去識別清單勾選或手動新增：</div>` +
      job.hits.map((h) => `<div class="src">「${esc(h.text)}」：${esc(h.context)}</div>`).join("");
  }
  if (job.status === "failed") return `<div class="err">● 整理失敗：${esc(job.error || "")}</div>`;
  return "";
}

function renderMeetings() {
  const rec = snap && snap.recording;
  const rows = [];
  const busy = (r) => r.minutes_job && r.minutes_job.status === "running";
  if (rec && rec.active) rows.push(`<div class="meeting-row"><b>${esc(rec.id || "")}</b><span class="st" style="color:#c62828">● 錄音中</span></div>`);
  for (const r of meetingRows) {
    let st = MEETING_STATUS[r.status] || r.status;
    if (r.status === "overdue")  // 設成 0 分鐘＝沒有家裡的工作站（公開版）：錄完就直接在這台轉
      st = r.fallback_minutes ? `${st}（超過 ${r.fallback_minutes} 分鐘）` : "可以在這台電腦轉寫";
    if (PHONE_HOST && r.status === "transcribed") st += "（在筆電填講者、生成會議記錄）";
    if (r.status === "error" && r.error) st += `：${r.error}`;
    const btns = [];
    if (r.status === "minutes_done" && !PHONE_HOST)
      btns.push(`<button data-act="view" data-id="${esc(r.id)}">檢視記錄</button>`);
    if ((r.status === "transcribed" || r.status === "minutes_done") && !PHONE_HOST) {
      btns.push(`<button data-act="review" data-id="${esc(r.id)}"${busy(r) ? " disabled" : ""}>${r.status === "minutes_done" ? "重新生成" : "生成會議記錄"}</button>`);
      btns.push(`<button class="secondary" data-act="transcript" data-id="${esc(r.id)}"${busy(r) ? " disabled" : ""}>逐字稿</button>`);
    }
    if (r.status === "minutes_done" && !PHONE_HOST)
      btns.push(`<button class="secondary" data-act="delmin" data-id="${esc(r.id)}"${busy(r) ? " disabled" : ""}>刪除記錄</button>`);
    if (r.can_transcribe_local)
      btns.push(`<button class="secondary" data-act="local" data-id="${esc(r.id)}">在這台電腦轉寫</button>`);
    if (!r.has_minutes && r.status !== "transcribing" && !PHONE_HOST)
      btns.push(`<button class="secondary" data-act="delete" data-id="${esc(r.id)}">刪除</button>`);
    rows.push(`<div class="meeting-row"><b>${esc(r.id)}</b><span class="st">${esc(st)}</span>${btns.join("")}` +
      `<div style="flex-basis:100%">${jobText(r.minutes_job)}</div></div>`);
  }
  // 只顯示最近一筆（錄音中那一場＋最新一場），其他收起來，要看再展開
  const shown = rec && rec.active ? 2 : 1;
  const more = $("meetingMore");
  const open = more ? more.open : false;
  $("meetingList").innerHTML = !rows.length ? `<p class="muted">還沒有錄音</p>`
    : rows.slice(0, shown).join("") + (rows.length > shown
      ? `<details id="meetingMore"${open ? " open" : ""}><summary>其他 ${rows.length - shown} 筆</summary>${rows.slice(shown).join("")}</details>` : "");
  if (review) $("mrSend").disabled = review.sending || busy(meetingRows.find((r) => r.id === review.id) || {});
}

function personChecks() {
  return [...document.querySelectorAll("#mrItems input[type=checkbox]")];
}

const NEW_NAME = "__new__";

function nameOptions(speaker, names) {
  return `<option value="">（不指定：講者${speaker + 1}）</option>`
    + names.map((n) => `<option value="${esc(n)}">${esc(n)}</option>`).join("")
    + `<option value="${NEW_NAME}">＋ 其他名字…</option>`;
}

function speakerNames() {
  // 講者 → 名字（下拉選單；選「其他名字」時用旁邊的輸入框）
  const out = {};
  for (const sel of document.querySelectorAll("#mrSpeakers select")) {
    const box = document.querySelector(`#mrSpeakers input[data-spk="${sel.dataset.spk}"]`);
    const name = (sel.value === NEW_NAME ? box.value : sel.value).trim();
    if (name) out[sel.dataset.spk] = name;
  }
  return out;
}

function addSpeakerName(box) {
  // 新打的名字加進每位講者的選單（名字一樣＝同一人），這位講者改成選它
  const name = box.value.trim();
  if (!name) return;
  for (const sel of document.querySelectorAll("#mrSpeakers select")) {
    if (![...sel.options].some((o) => o.value === name)) {
      const opt = document.createElement("option");
      opt.value = opt.textContent = name;
      sel.insertBefore(opt, sel.querySelector(`option[value="${NEW_NAME}"]`));
    }
  }
  document.querySelector(`#mrSpeakers select[data-spk="${box.dataset.spk}"]`).value = name;
  box.value = "";
  box.style.display = "none";
}

function syncNameChecks() {
  // 填了講者名字（醫師、學員）的人名不換代號
  const names = new Set(Object.values(speakerNames()));
  for (const box of personChecks()) {
    const t = box.dataset.text;
    const isSpeaker = [...names].some((n) => n.includes(t) || (n.length >= 2 && t.includes(n)));
    if (box.dataset.kind === "person" && isSpeaker && !box.dataset.touched) {
      box.checked = false;
      review.candidates[+box.dataset.i].checked = false;
    }
  }
}

function renderReview() {
  $("meetingReview").style.display = review ? "" : "none";
  if (!review) return;
  $("mrTitle").textContent = `生成會議記錄：${review.id}`;
  $("mrSpeakers").innerHTML = review.speakers.map((s) =>
    `<div class="mr-spk"><b>講者${s.speaker + 1}</b>　發言 ${fmtMinutes(s.seconds)}
      <select data-spk="${s.speaker}">${nameOptions(s.speaker, review.known_names || [])}</select>
      <input data-spk="${s.speaker}" placeholder="輸入名字，按 Enter" autocomplete="off" style="display:none">
      <div class="samples">${s.samples.map((x) => "「" + esc(x.slice(0, 40)) + "」").join(" ")}</div></div>`).join("")
    || `<p class="muted">沒有分出講者</p>`;
  renderReviewItems();
  const qs = review.questions || [];
  $("mrQuestionsBox").style.display = qs.length ? "" : "none";
  $("mrQuestions").innerHTML = qs.map((q) =>
    `<label class="mr-item"><input type="checkbox" data-q="${q.i}" checked>
      <span class="src">[${mmss(q.t)}]</span> ${esc(q.text)}</label>`).join("");
  $("mrType").value = review.meeting_type || "";
  $("mrMsg").textContent = "";
}

function renderReviewItems() {
  $("mrItems").innerHTML = review.candidates.map((c, i) =>
    `<label class="mr-item"><input type="checkbox" data-i="${i}" data-kind="${esc(c.kind)}" data-text="${esc(c.text)}"${c.checked ? " checked" : ""}>
      ${esc(c.text)}　<span class="src">${KIND_LABEL[c.kind] || c.kind}${c.count ? " " + c.count + " 次" : ""}</span></label>`).join("")
    || `<p class="muted">沒有找到候選；有漏掉的請手動新增</p>`;
}

let mvId = null;
function minutesEditing(on) {
  $("mvBody").style.display = on ? "none" : "";
  $("mvText").style.display = on ? "" : "none";
  $("mvEdit").textContent = on ? "取消" : "修改";
  $("mvSave").style.display = on ? "" : "none";
}

async function viewMinutes(id, msg = "") {
  const res = await fetch(`/api/meetings/${encodeURIComponent(id)}/minutes-view`);
  if (!res.ok) { alert((await res.json().catch(() => ({}))).detail || "讀不到會議記錄"); return; }
  const data = await res.json();
  mvId = data.id;
  $("mvTitle").textContent = `會議記錄：${data.id}`;
  $("mvBody").innerHTML = data.html;  // 伺服器已跳脫內容
  $("mvText").value = data.md;
  $("mvPath").textContent = `Word 檔：${data.docx}`;
  $("mvMsg").textContent = msg;
  minutesEditing(false);
  $("minutesView").style.display = "";
  $("minutesView").scrollIntoView({ block: "nearest" });
}
$("mvClose").onclick = () => { $("minutesView").style.display = "none"; mvId = null; };
$("mvEdit").onclick = () => {
  const editing = $("mvText").style.display !== "none";
  if (editing) return viewMinutes(mvId);  // 取消：重新讀檔
  minutesEditing(true);
  $("mvText").focus();
};
$("mvSave").onclick = async () => {
  // 以畫面為準：.md 與 Word 都依這裡的內容重寫（直接在 Word 裡改過的會被蓋掉）
  $("mvSave").disabled = true;
  try {
    const res = await postJSON(`/api/meetings/${encodeURIComponent(mvId)}/minutes-md`, { md: $("mvText").value });
    if (!res.ok) { $("mvMsg").textContent = (await res.json().catch(() => ({}))).detail || "無法儲存"; return; }
    await viewMinutes(mvId, "已儲存（Word 檔已依畫面內容更新）");
  } finally {
    $("mvSave").disabled = false;
  }
};

// 逐字稿：看＋改字、改講者（使用者 2026-11-23）
let tv = null;  // {id, lines, speakers}
function speakerOptions(cur, speakers) {
  const ids = [...speakers];
  const next = (ids.length ? Math.max(...ids) : -1) + 1;
  const opts = ids.map((s) => `<option value="${s}"${s === cur ? " selected" : ""}>講者${s + 1}</option>`);
  if (cur === null || cur === undefined) opts.unshift(`<option value="" selected>講者?</option>`);
  opts.push(`<option value="${next}">新講者（講者${next + 1}）</option>`);
  return opts.join("");
}

async function viewTranscript(id, msg = "") {
  const res = await fetch(`/api/meetings/${encodeURIComponent(id)}/transcript`);
  if (!res.ok) { alert((await res.json().catch(() => ({}))).detail || "讀不到逐字稿"); return; }
  tv = await res.json();
  $("tvTitle").textContent = `逐字稿：${tv.id}`;
  $("tvBody").innerHTML = tv.lines.map((l) =>
    `<div class="tv-line"><span class="t">[${mmss(l.start)}]</span>
      <select data-i="${l.i}">${speakerOptions(l.speaker, tv.speakers)}</select>
      <input data-i="${l.i}" value="${esc(l.text)}" autocomplete="off"></div>`).join("")
    || `<p class="muted">逐字稿是空的</p>`;
  $("tvMsg").textContent = msg || (tv.edited_at ? `上次修改：${tv.edited_at}` : "");
  $("transcriptView").style.display = "";
  $("transcriptView").scrollIntoView({ block: "nearest" });
}
$("tvClose").onclick = () => { $("transcriptView").style.display = "none"; tv = null; };
$("tvSave").onclick = async () => {
  if (!tv) return;
  const edits = new Map();
  for (const el of $("tvBody").querySelectorAll("input[data-i]")) {
    const l = tv.lines[+el.dataset.i];
    if (el.value.trim() !== l.text) edits.set(l.i, { ...(edits.get(l.i) || { i: l.i }), text: el.value });
  }
  for (const el of $("tvBody").querySelectorAll("select[data-i]")) {
    const l = tv.lines[+el.dataset.i];
    if (el.value !== "" && +el.value !== l.speaker) edits.set(l.i, { ...(edits.get(l.i) || { i: l.i }), speaker: +el.value });
  }
  if (!edits.size) { $("tvMsg").textContent = "沒有修改"; return; }
  $("tvSave").disabled = true;
  try {
    const res = await postJSON(`/api/meetings/${encodeURIComponent(tv.id)}/transcript`, { lines: [...edits.values()] });
    if (!res.ok) { $("tvMsg").textContent = (await res.json().catch(() => ({}))).detail || "無法儲存"; return; }
    await viewTranscript(tv.id, `已儲存 ${edits.size} 句（會議記錄要重新生成才會跟著改）`);
    if (review && review.id === tv.id) { review = null; renderReview(); }  // 確認畫面的候選已過時
  } finally {
    $("tvSave").disabled = false;
  }
};

async function openReview(id) {
  const res = await fetch(`/api/meetings/${encodeURIComponent(id)}/review`);
  if (!res.ok) { alert((await res.json().catch(() => ({}))).detail || "讀不到逐字稿"); return; }
  review = { id, ...(await res.json()) };
  renderReview();
  renderMeetings();
  $("meetingReview").scrollIntoView({ block: "nearest" });
}

async function watchJob(id) {
  if (watchingJobs.has(id)) return;
  watchingJobs.add(id);
  try {
    for (;;) {
      await new Promise((r) => setTimeout(r, 2000));
      const job = await (await fetch(`/api/meetings/${encodeURIComponent(id)}/minutes`)).json();
      const row = meetingRows.find((r) => r.id === id);
      if (row) row.minutes_job = job;
      renderMeetings();
      if (job.status !== "running") break;
    }
  } finally {
    watchingJobs.delete(id);
    loadMeetings();
  }
}

async function startLocal(id, btn) {
  const res = await postJSON(`/api/meetings/${encodeURIComponent(id)}/transcribe-local`);
  const data = await res.json().catch(() => ({}));
  const d = data.detail;
  if (res.status === 409 && d && d.need_download) {
    if (!confirm(d.message)) return;
    await postJSON("/api/local-models/download");
    for (;;) {
      await new Promise((r) => setTimeout(r, 2000));
      const st = await (await fetch("/api/local-models")).json();
      if (btn) btn.textContent = `下載模型中 ${Math.round((st.progress || 0) * 100)}%`;
      if (st.status === "failed") { alert("模型下載失敗：" + (st.error || "")); return loadMeetings(); }
      if (st.ready) break;
    }
    return startLocal(id, null);
  }
  if (!res.ok) alert((typeof d === "string" ? d : d && d.message) || "無法開始");
  return loadMeetings();
}

$("meetingList").onclick = async (ev) => {
  const b = ev.target.closest("button");
  if (!b) return;
  const id = b.dataset.id;
  if (b.dataset.act === "review") return openReview(id);
  if (b.dataset.act === "view") return viewMinutes(id);
  if (b.dataset.act === "transcript") return viewTranscript(id);
  if (b.dataset.act === "delmin") {
    if (!confirm(`刪除 ${id} 的會議記錄（Word 與 .md）？\n錄音與逐字稿會留著，可以重新生成。雲端硬碟會先放進垃圾桶（約 30 天後永久刪除）。`)) return;
    const res = await fetch(`/api/meetings/${encodeURIComponent(id)}/minutes`, { method: "DELETE" });
    if (!res.ok) alert((await res.json().catch(() => ({}))).detail || "無法刪除");
    if (mvId === id) { $("minutesView").style.display = "none"; mvId = null; }
    return loadMeetings();
  }
  if (b.dataset.act === "delete") {
    if (!confirm(`刪除 ${id} 的錄音與逐字稿？\n刪除後無法在程式裡復原；雲端硬碟會先放進垃圾桶（約 30 天後永久刪除）。`)) return;
    const res = await fetch(`/api/meetings/${encodeURIComponent(id)}`, { method: "DELETE" });
    if (!res.ok) alert((await res.json().catch(() => ({}))).detail || "無法刪除");
    if (review && review.id === id) { review = null; renderReview(); }
    return loadMeetings();
  }
  if (b.dataset.act === "local") {
    if (!confirm("在這台電腦轉寫：比家裡電腦慢很多（約 1 小時錄音要 1 小時以上），會在背景跑。開始嗎？")) return;
    return startLocal(id, b);
  }
};
$("mrSpeakers").addEventListener("input", syncNameChecks);
$("mrSpeakers").addEventListener("change", (ev) => {
  const el = ev.target;
  if (el.tagName === "SELECT") {
    const box = document.querySelector(`#mrSpeakers input[data-spk="${el.dataset.spk}"]`);
    box.style.display = el.value === NEW_NAME ? "" : "none";
    if (el.value === NEW_NAME) box.focus();
  } else if (el.tagName === "INPUT") {
    addSpeakerName(el);
  }
  syncNameChecks();
});
$("mrSpeakers").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter" && ev.target.tagName === "INPUT") { ev.preventDefault(); ev.target.blur(); }
});
$("mrItems").addEventListener("change", (ev) => {
  const box = ev.target;
  if (box.dataset.i === undefined) return;
  box.dataset.touched = "1";
  review.candidates[+box.dataset.i].checked = box.checked;
});
$("mrAdd").onclick = () => {
  const text = $("mrAddText").value.trim();
  if (!text || !review) return;
  if (!review.candidates.some((c) => c.text === text))
    review.candidates.unshift({ text, kind: $("mrAddKind").value, count: 0, checked: true });
  $("mrAddText").value = "";
  renderReviewItems();
};
$("mrAddText").addEventListener("keydown", (ev) => { if (ev.key === "Enter") $("mrAdd").click(); });
$("mrCancel").onclick = () => { review = null; renderReview(); };
$("mrSend").onclick = async () => {
  if (!review || review.sending) return;
  review.sending = true;
  $("mrSend").disabled = true;  // 整理中：不要送出第二次
  const speaker_names = speakerNames();
  const questions = [...document.querySelectorAll("#mrQuestions input[type=checkbox]")]
    .filter((b) => b.checked).map((b) => +b.dataset.q);
  const items = personChecks().filter((b) => b.checked).map((b) => ({ text: b.dataset.text, kind: b.dataset.kind }));
  const id = review.id;
  const res = await postJSON(`/api/meetings/${encodeURIComponent(id)}/minutes`,
                             { speaker_names, items, meeting_type: $("mrType").value.trim(), questions });
  if (!res.ok) {
    $("mrMsg").textContent = (await res.json().catch(() => ({}))).detail || "無法送出";
    review.sending = false;
    $("mrSend").disabled = false;
    return;
  }
  review = null;
  renderReview();
  await loadMeetings();
  watchJob(id);
};
setInterval(loadMeetings, 15000);
loadMeetings();

loadSettings();
loadReferences();
connect();
