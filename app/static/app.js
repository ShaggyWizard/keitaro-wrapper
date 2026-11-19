const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

class ApiError extends Error {
  constructor(message, status) { super(message); this.status = status; }
}

async function api(method, url, body) {
  const resp = await fetch(url, {
    method,
    // свой заголовок — защита от CSRF: чужой сайт не может его отправить (см. app/main.py)
    headers: { "X-Requested-With": "keitaro-wrapper", ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await resp.json().catch(() => null);
  if (!resp.ok) {
    let detail = data && data.detail;
    if (Array.isArray(detail)) detail = detail.map((d) => d.msg).join("; "); // ошибки валидации FastAPI
    if (typeof detail !== "string" || !detail) detail = `Ошибка сервера (${resp.status} ${resp.statusText})`;
    throw new ApiError(detail, resp.status);
  }
  return data;
}

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => t.classList.add("hidden"), 6000);
}

async function guarded(fn) {
  try { return await fn(); } catch (e) { toast(e.message); }
}

// ---------- вкладки
document.querySelectorAll(".tab").forEach((btn) =>
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b === btn));
    document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("hidden", p.id !== btn.dataset.tab));
    if (btn.dataset.tab === "editor") enterEditor();
  })
);

// ---------- автодополнение офферов (ходит в наш API, тот — в Keitaro)
let offerSeq = 0;
function offerAutocomplete(input) {
  if (!input) return;
  input.addEventListener("input", () => {
    clearTimeout(input._timer);
    input._timer = setTimeout(async () => {
      const my = ++offerSeq;
      const q = input.value.split(" — ")[0];
      let offers;
      try { offers = await api("GET", `/api/offers?q=${encodeURIComponent(q)}`); }
      catch { return; } // подсказки не критичны — без всплывающих ошибок на каждую букву
      if (my !== offerSeq) return; // пришёл ответ на старый запрос
      $("#offers-list").innerHTML = offers.map((o) => `<option value="${Number(o.id)} — ${esc(o.name)}">`).join("");
    }, 250);
  });
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && input.closest(".add-row")) {
      ev.preventDefault();
      input.closest(".add-row").querySelector("button[data-act=add]").click();
    }
  });
}
// Принимаем только чистый ID («18813») или строку из подсказки («18813 — название»).
// «888 Casino», набранное руками, не должно превратиться в оффер 888.
function offerIdFrom(value) {
  const m = String(value).trim().match(/^(\d+)(?:\s+—\s+.*)?$/);
  return m ? Number(m[1]) : null;
}
const BAD_OFFER = "Выберите оффер из подсказки или введите только его ID (цифрами)";

// ---------- часть 1: создание
async function loadMeta() {
  const meta = await guarded(() => api("GET", "/api/meta"));
  if (!meta) return;
  $("#prefix-hint").textContent = `префикс ${meta.prefix} добавится сам`;
  for (const [field, list, def] of [
    ["domain_id", meta.domains, meta.defaults.domain_id],
    ["group_id", meta.groups, meta.defaults.group_id],
    ["source_id", meta.sources, meta.defaults.source_id],
  ]) {
    const placeholder = def ? "" : `<option value="" selected>— выберите —</option>`;
    $(`select[name=${field}]`).innerHTML = placeholder + list
      .map((x) => `<option value="${Number(x.id)}" ${x.id === def ? "selected" : ""}>${esc(x.name)}</option>`)
      .join("");
  }
}

offerAutocomplete($("input[name=offer]"));

$("#create-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target;
  const offerId = offerIdFrom(f.offer.value);
  if (!offerId) return toast(BAD_OFFER);
  const btn = f.querySelector("button[type=submit]");
  btn.disabled = true;
  const c = await guarded(() =>
    api("POST", "/api/campaigns", {
      name: f.name.value,
      geo: f.geo.value,
      offer_id: offerId,
      domain_id: +f.domain_id.value || null,
      group_id: +f.group_id.value || null,
      source_id: +f.source_id.value || null,
    })
  );
  btn.disabled = false;
  if (!c) return;
  const streamsNote = c.streams.length
    ? `Потоков: ${Number(c.streams.length)}.`
    : "Кампания создана в Keitaro, но потоки не загрузились — в редакторе нажмите Fetch streams.";
  $("#create-result").innerHTML = `<div class="result">Создана кампания <b>${esc(c.name)}</b> (ID ${Number(c.id)}, alias ${esc(c.alias || "")}).
    ${streamsNote} <button id="open-created">Открыть в редакторе</button></div>`;
  $("#open-created").onclick = () => {
    currentCampaign = c.id;
    remember(c.id);
    document.querySelector(".tab[data-tab=editor]").click();
  };
});

// ---------- часть 2: редактор
let currentCampaign = null;
let currentData = null;
let viewSeq = 0; // защита от гонок: рисуем только ответ на последний запрос кампании
const LAST_KEY = "keitaro-wrapper-last-campaign";
const remember = (id) => { try { localStorage.setItem(LAST_KEY, String(id)); } catch {} };
const recall = () => { try { return Number(localStorage.getItem(LAST_KEY)) || null; } catch { return null; } };

async function loadCampaigns() {
  const list = await guarded(() => api("GET", "/api/campaigns"));
  if (!list) return false;
  const sel = $("#campaign-select");
  if (!list.length) {
    sel.innerHTML = "";
    $("#streams").innerHTML = `<p class="hint">В Keitaro нет кампаний — создайте первую на вкладке «Новая кампания».</p>`;
    return false;
  }
  sel.innerHTML = list
    .sort((a, b) => b.id - a.id)
    .map((c) => `<option value="${Number(c.id)}">${Number(c.id)} — ${esc(c.name)}</option>`)
    .join("");
  if (currentCampaign && list.some((c) => c.id === currentCampaign)) {
    sel.value = String(currentCampaign);
  } else if (currentCampaign) {
    toast(`Кампании ${currentCampaign} больше нет в Keitaro`);
    currentCampaign = null;
  }
  return true;
}

async function enterEditor() {
  if (await loadCampaigns()) showLocal(+$("#campaign-select").value);
}

$("#fetch-btn").addEventListener("click", async (ev) => {
  const id = +$("#campaign-select").value;
  if (!id) return toast("Выберите кампанию");
  const dirty = currentData && currentData.id === id && currentData.streams.some((s) => s.dirty);
  if (dirty && !confirm("Есть неопубликованные изменения. Fetch streams загрузит поток из Keitaro и сбросит их. Продолжить?")) return;
  const btn = ev.currentTarget;
  btn.disabled = true;
  const my = ++viewSeq;
  const c = await guarded(() => api("POST", `/api/campaigns/${id}/fetch`));
  btn.disabled = false;
  if (c && my === viewSeq) showCampaign(id, c);
});

$("#campaign-select").addEventListener("change", (ev) => showLocal(+ev.target.value));

async function showLocal(id) {
  if (!id) return;
  const my = ++viewSeq;
  try {
    const c = await api("GET", `/api/campaigns/${id}`);
    if (my === viewSeq) showCampaign(id, c);
  } catch (e) {
    if (my !== viewSeq) return;
    currentCampaign = id;
    remember(id);
    currentData = null;
    $("#streams").innerHTML = `<p class="hint">Нажмите Fetch streams, чтобы загрузить потоки из Keitaro.</p>`;
    if (e.status !== 404) toast(e.message); // 404 — кампания просто ещё не загружалась
  }
}

function showCampaign(id, data) {
  currentCampaign = id;
  remember(id);
  renderCampaign(data);
}

function renderCampaign(c) {
  currentData = c;
  $("#streams").innerHTML = c.streams.map(renderStream).join("")
    || `<p class="hint">Потоки не загружены — нажмите Fetch streams.</p>`;
  document.querySelectorAll(".stream .add-row input").forEach(offerAutocomplete);
}

function renderStream(s) {
  if (!s.has_offers) {
    return `<div class="stream" data-stream="${Number(s.id)}"><div class="stream-head">
      <b>#${Number(s.position)} ${esc(s.name)} <span class="badge">поток без офферов — редактировать нечего</span></b></div></div>`;
  }
  const rows = s.offers.map((o) => {
    const changed = o.dirty ? "changed" : "";
    const was = o.dirty && o.published_share !== null ? ` <span class="was">было ${Number(o.published_share)}%</span>` : "";
    const fresh = o.published_state === null ? ` <span class="was">новый</span>` : "";
    const btns = o.state === "disabled"
      ? `<span class="was">выключен в Keitaro, в сумму не входит</span>`
      : o.state === "removed"
      ? `<button data-act="restore" data-offer="${Number(o.offer_id)}">Bring back</button>`
      : `<button data-act="pin" data-offer="${Number(o.offer_id)}" data-pinned="${o.pinned}">${o.pinned ? "Unpin" : "Pin"}</button>
         <button data-act="remove" data-offer="${Number(o.offer_id)}">Remove</button>`;
    return `<tr class="${o.state} ${changed}">
      <td class="name">[${Number(o.offer_id)}] ${esc(o.name)}${fresh}</td>
      <td class="share">${Number(o.share)}%${o.pinned ? " 📌" : ""}${o.state === "disabled" ? ' <span class="was">не учитывается</span>' : ""}${was}</td>
      <td class="btns">${btns}</td></tr>`;
  }).join("");
  const anyActive = s.offers.some((o) => o.state === "active");
  const totalCls = s.total === 100 || !anyActive ? "" : "total-bad";
  const empty = s.offers.length ? "" : `<p class="hint">В потоке нет офферов — добавьте первый.</p>`;
  return `<div class="stream ${s.dirty ? "dirty" : ""}" data-stream="${Number(s.id)}">
    <div class="stream-head">
      <b>#${Number(s.position)} ${esc(s.name)}
        <span class="badge state">${s.dirty ? "не опубликован" : "опубликован"}</span>
        <span class="badge ${totalCls}">сумма активных ${Number(s.total)}%</span></b>
      <div class="actions">
        <button data-act="push" class="primary" ${s.dirty ? "" : "disabled"}>Push</button>
        <button data-act="cancel" ${s.dirty ? "" : "disabled"}>Cancel</button>
      </div>
    </div>
    ${empty}
    <div class="table-wrap"><table><thead><tr><th>Оффер</th><th>Доля</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>
    <div class="add-row"><input list="offers-list" placeholder="добавить оффер: название или ID" autocomplete="off">
      <button data-act="add">Добавить</button></div>
  </div>`;
}

function replaceStream(s) {
  const el = document.querySelector(`.stream[data-stream="${Number(s.id)}"]`);
  if (!el) return; // пока шёл запрос, открыли другую кампанию
  el.outerHTML = renderStream(s);
  offerAutocomplete(document.querySelector(`.stream[data-stream="${Number(s.id)}"] .add-row input`));
  if (currentData) currentData.streams = currentData.streams.map((x) => (x.id === s.id ? s : x));
}

$("#streams").addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button[data-act]");
  if (!btn) return;
  const streamEl = btn.closest(".stream");
  if (streamEl.classList.contains("busy")) return; // один запрос на поток за раз
  const sid = streamEl.dataset.stream;
  const oid = btn.dataset.offer;
  const act = btn.dataset.act;
  let call;
  if (act === "add") {
    const offerId = offerIdFrom(btn.parentElement.querySelector("input").value);
    if (!offerId) return toast(BAD_OFFER);
    call = () => api("POST", `/api/streams/${sid}/offers`, { offer_id: offerId });
  } else if (act === "remove") call = () => api("DELETE", `/api/streams/${sid}/offers/${oid}`);
  else if (act === "restore") call = () => api("POST", `/api/streams/${sid}/offers/${oid}/restore`);
  else if (act === "push") call = () => api("POST", `/api/streams/${sid}/push`);
  else if (act === "cancel") call = () => api("POST", `/api/streams/${sid}/cancel`);
  else if (act === "pin") {
    const pinned = btn.dataset.pinned === "true";
    let share = null;
    if (!pinned) {
      const current = btn.closest("tr").querySelector(".share").textContent;
      const answer = prompt("Закрепить долю, % (целое от 0 до 100)", parseInt(current, 10));
      if (answer === null) return;
      if (!/^\d{1,3}$/.test(answer.trim()) || +answer > 100) return toast("Доля — целое число от 0 до 100");
      share = +answer;
    }
    call = () => api("PUT", `/api/streams/${sid}/offers/${oid}/pin`, { pinned: !pinned, share });
  }
  streamEl.classList.add("busy");
  streamEl.querySelectorAll("button").forEach((b) => (b.disabled = true));
  const s = await guarded(call);
  if (s) {
    replaceStream(s);
  } else if (streamEl.isConnected) {
    // ошибка — возвращаем поток как был
    streamEl.classList.remove("busy");
    const prev = currentData && currentData.streams.find((x) => String(x.id) === sid);
    if (prev) replaceStream(prev);
  }
});

loadMeta();

// после перезагрузки страницы открываем последнюю кампанию с её черновиком
currentCampaign = recall();
if (currentCampaign) document.querySelector(".tab[data-tab=editor]").click();
