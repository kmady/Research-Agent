"use strict";

// Interface de Research-Agent : aucune dépendance. Tout le texte venant du serveur est inséré via
// textContent, sauf le rapport, déjà converti en HTML sûr côté serveur (HTML brut désactivé).

const $ = (sel) => document.querySelector(sel);
const API = "api";
const TERMINAL = new Set(["done", "failed", "cancelled"]);

const ui = {
  current: null,        // identifiant de la recherche affichée
  source: null,         // EventSource en cours
  view: "report",
  jobs: [],
};

// --- Utilitaires ------------------------------------------------------------

async function api(path, options = {}) {
  const res = await fetch(`${API}${path}`, {
    headers: { "Content-Type": "application/json" }, ...options,
  });
  if (res.status === 204) return null;
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = Array.isArray(body.detail) ? body.detail.map((d) => d.msg).join(" ; ") : body.detail;
    throw new Error(detail || `erreur ${res.status}`);
  }
  return body;
}

function toast(message) {
  const el = $("#toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.hidden = true; }, 4500);
}

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

const timeFmt = new Intl.DateTimeFormat("fr-FR", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const dateFmt = new Intl.DateTimeFormat("fr-FR", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

function minutes(seconds) {
  if (!seconds) return "";
  return seconds < 60 ? `${Math.round(seconds)} s` : `${(seconds / 60).toFixed(1).replace(".", ",")} min`;
}

// Statut affiché : « terminée » se décline selon le verdict de l'agent.
function statusOf(job) {
  if (job.status === "queued") {
    return { cls: "queued", dot: "queued", label: job.queue_position ? `En attente (n° ${job.queue_position})` : "En attente" };
  }
  if (job.status === "running") return { cls: "running", dot: "running", label: "En cours" };
  if (job.status === "failed") return { cls: "failed", dot: "failed", label: "Échec" };
  if (job.status === "cancelled") return { cls: "", dot: "", label: "Annulée" };
  if (job.search_outage) return { cls: "failed", dot: "failed", label: "Moteurs indisponibles" };
  if (job.sufficient) return { cls: "ok", dot: "done-ok", label: "Suffisante" };
  return { cls: "partial", dot: "done-partial", label: "Incomplète" };
}

// --- État des services ------------------------------------------------------

async function refreshHealth() {
  try {
    const h = await api("/health");
    setPill("ollama", h.ollama && h.model_available, h.ollama
      ? (h.model_available ? `Modèle ${h.model}` : `Modèle ${h.model} absent`) : "Ollama injoignable");
    setPill("searxng", h.searxng, h.searxng ? "Recherche" : "SearXNG injoignable");
    const q = $("#queue-pill");
    const busy = h.running + h.queued;
    q.hidden = busy === 0;
    q.textContent = h.queued ? `${h.running} en cours · ${h.queued} en attente` : `${h.running} en cours`;
  } catch {
    setPill("ollama", false, "Service injoignable");
  }
}

function setPill(key, ok, text) {
  const pill = document.querySelector(`.pill[data-key="${key}"]`);
  pill.classList.toggle("ok", !!ok);
  pill.classList.toggle("bad", !ok);
  pill.lastChild.textContent = text;
}

// --- Historique ---------------------------------------------------------------

async function refreshJobs() {
  try {
    ui.jobs = await api("/jobs?limit=100");
  } catch (e) {
    return;
  }
  const list = $("#jobs");
  list.replaceChildren();
  if (!ui.jobs.length) {
    list.append(el("li", "empty", "Aucune recherche pour l'instant."));
    return;
  }
  for (const job of ui.jobs) {
    const st = statusOf(job);
    const li = el("li");
    const btn = el("button", `job-item${job.id === ui.current ? " active" : ""}`);
    btn.type = "button";
    btn.append(el("div", "q", job.question));
    const sub = el("div", "sub");
    sub.append(el("span", `dot ${st.dot}`), el("span", "", `${st.label} · ${dateFmt.format(job.created_at * 1000)}`));
    btn.append(sub);
    btn.addEventListener("click", () => openJob(job.id));
    li.append(btn);
    list.append(li);
  }
}

// --- Nouvelle recherche ---------------------------------------------------------

function showNew() {
  closeStream();
  ui.current = null;
  history.replaceState(null, "", location.pathname);
  $("#view-job").hidden = true;
  $("#view-new").hidden = false;
  refreshJobs();
  $("#question").focus();
}

async function submitQuestion(ev) {
  ev.preventDefault();
  const question = $("#question").value.trim();
  if (question.length < 8) {
    toast("La question est trop courte.");
    return;
  }
  const btn = $("#submit");
  btn.disabled = true;
  try {
    const job = await api("/jobs", {
      method: "POST",
      body: JSON.stringify({
        question,
        max_rounds: Number($("#max-rounds").value),
        verify: $("#verify").checked,
      }),
    });
    $("#question").value = "";
    updateCounter();
    await openJob(job.id);
  } catch (e) {
    toast(`Impossible de lancer la recherche : ${e.message}`);
  } finally {
    btn.disabled = false;
  }
}

function updateCounter() {
  $("#counter").textContent = `${$("#question").value.length} / 500`;
}

// --- Vue d'une recherche ----------------------------------------------------------

function closeStream() {
  if (ui.source) {
    ui.source.close();
    ui.source = null;
  }
}

async function openJob(id) {
  closeStream();
  ui.current = id;
  ui.view = "report";
  history.replaceState(null, "", `#${id}`);
  let job;
  try {
    job = await api(`/jobs/${encodeURIComponent(id)}`);
  } catch (e) {
    toast(e.message);
    showNew();
    return;
  }
  $("#view-new").hidden = true;
  $("#view-job").hidden = false;
  $("#events").replaceChildren();
  $("#report").replaceChildren();
  $("#report-wrap").hidden = true;
  renderJob(job);
  refreshJobs();
  window.scrollTo({ top: 0 });

  const source = new EventSource(`${API}/jobs/${encodeURIComponent(id)}/events`);
  ui.source = source;
  source.addEventListener("progress", (e) => appendEvent(JSON.parse(e.data)));
  source.addEventListener("status", (e) => {
    const j = JSON.parse(e.data);
    if (j.id === ui.current) renderJob(j);
  });
  source.addEventListener("end", async () => {
    closeStream();
    const final = await api(`/jobs/${encodeURIComponent(id)}`);
    if (final.id !== ui.current) return;
    renderJob(final);
    refreshJobs();
    refreshHealth();
    if (final.available.includes("report")) {
      $("#timeline").open = false;
      loadReport();
    }
  });
}

function renderJob(job) {
  const st = statusOf(job);
  const badge = $("#job-status");
  badge.className = `badge ${st.cls}`;
  badge.textContent = st.label;
  $("#job-question").textContent = job.question;
  document.title = `${job.question} — Research-Agent`;

  const meta = [`Lancée le ${dateFmt.format(job.created_at * 1000)}`];
  if (job.stats && job.stats.seconds) meta.push(`durée ${minutes(job.stats.seconds)}`);
  if (job.stats && job.stats.rounds) meta.push(`${job.stats.rounds} tour(s)`);
  if (job.stats && job.stats.sources_read) meta.push(`${job.stats.sources_read} sources lues`);
  if (job.stats && job.stats.model) meta.push(`modèle ${job.stats.model}`);
  $("#job-meta").textContent = meta.join(" · ");

  const running = !TERMINAL.has(job.status);
  $("#cancel-btn").hidden = !running;
  $("#delete-btn").hidden = running;

  const progress = $("#progress");
  progress.hidden = !running;
  if (running) {
    const pct = job.status === "queued" ? 0 : Math.max(2, Math.min(99, job.progress || 0));
    $("#progress-bar").style.width = `${pct}%`;
    $("#progress-label").textContent = job.status === "queued" ? "en attente" : `${pct} %`;
  }

  const verdict = $("#verdict");
  verdict.replaceChildren();
  verdict.hidden = true;
  // Un rapport porte déjà son propre bandeau : l'encadré ne sert que si aucun rapport n'a été produit.
  if (job.status === "failed" || (job.status === "done" && !job.available.includes("report"))) {
    const title = job.status === "failed" ? "La recherche a échoué"
      : job.search_outage ? "Recherche interrompue : moteurs de recherche indisponibles"
        : job.sufficient ? "Couverture jugée suffisante par l'agent" : "Couverture incomplète, signalée par l'agent";
    verdict.className = `verdict ${job.status === "failed" || job.search_outage ? "failed" : job.sufficient ? "ok" : "partial"}`;
    verdict.append(el("strong", "", title));
    const why = job.status === "failed" ? job.error : job.stop_reason;
    if (why) verdict.append(el("span", "", why));
    verdict.hidden = false;
  }

  const downloads = $("#downloads");
  downloads.replaceChildren();
  const labels = { report: "Rapport .md", details: "Détail .md", trace: "Trace .json" };
  for (const kind of ["report", "details", "trace"]) {
    if (!job.available.includes(kind)) continue;
    const a = el("a", "", labels[kind]);
    a.href = `${API}/jobs/${encodeURIComponent(job.id)}/download/${kind}`;
    a.setAttribute("download", "");
    downloads.append(a);
  }
}

function appendEvent(ev) {
  const list = $("#events");
  const li = el("li", `event ${ev.kind}`);
  const t = el("time", "", timeFmt.format(ev.ts * 1000));
  li.append(t, el("span", "msg", ev.message));
  const nearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 40;
  list.append(li);
  if (nearBottom) list.scrollTop = list.scrollHeight;
  $("#event-count").textContent = `(${list.children.length})`;
}

async function loadReport() {
  const id = ui.current;
  try {
    const data = await api(`/jobs/${encodeURIComponent(id)}/report?view=${ui.view}`);
    if (id !== ui.current) return;
    const article = $("#report");
    article.innerHTML = data.html;   // HTML produit par le serveur à partir du Markdown, HTML brut désactivé
    if (ui.view === "report") linkReferences(article);
    $("#report-wrap").hidden = false;
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === ui.view));
  } catch (e) {
    toast(`Rapport indisponible : ${e.message}`);
  }
}

// Rend les références « [3] » cliquables vers l'entrée 3 de la liste des sources.
function linkReferences(article) {
  const heading = [...article.querySelectorAll("h2")].find((h) => h.textContent.trim() === "Sources");
  const list = heading && heading.nextElementSibling;
  if (!list || list.tagName !== "OL") return;
  [...list.children].forEach((li, i) => { li.id = `ref-${i + 1}`; });
  const count = list.children.length;
  const walker = document.createTreeWalker(article, NodeFilter.SHOW_TEXT, {
    acceptNode: (n) => (list.contains(n) || n.parentElement.closest("a, code")
      ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT),
  });
  const nodes = [];
  while (walker.nextNode()) if (/\[\d+\]/.test(walker.currentNode.nodeValue)) nodes.push(walker.currentNode);
  for (const node of nodes) {
    const frag = document.createDocumentFragment();
    let last = 0;
    const text = node.nodeValue;
    for (const m of text.matchAll(/\[(\d+)\]/g)) {
      const n = Number(m[1]);
      if (n < 1 || n > count) continue;
      frag.append(text.slice(last, m.index));
      const a = el("a", "ref", `[${n}]`);
      a.href = `#ref-${n}`;
      a.title = list.children[n - 1].textContent.trim();
      a.addEventListener("click", (e) => {
        e.preventDefault();
        const target = document.getElementById(`ref-${n}`);
        target.scrollIntoView({ behavior: "smooth", block: "center" });
        target.classList.remove("flash");
        void target.offsetWidth;   // relance l'animation
        target.classList.add("flash");
        history.replaceState(null, "", `#${ui.current}`);
      });
      frag.append(a);
      last = m.index + m[0].length;
    }
    frag.append(text.slice(last));
    node.replaceWith(frag);
  }
}

async function cancelJob() {
  if (!ui.current) return;
  try {
    await api(`/jobs/${encodeURIComponent(ui.current)}/cancel`, { method: "POST" });
    toast("Annulation demandée : la recherche s'arrêtera à la prochaine étape.");
  } catch (e) {
    toast(e.message);
  }
}

async function deleteJob() {
  if (!ui.current || !confirm("Supprimer cette recherche et ses rapports ?")) return;
  try {
    await api(`/jobs/${encodeURIComponent(ui.current)}`, { method: "DELETE" });
    showNew();
  } catch (e) {
    toast(e.message);
  }
}

// --- Démarrage ----------------------------------------------------------------

document.addEventListener("DOMContentLoaded", () => {
  $("#ask").addEventListener("submit", submitQuestion);
  $("#question").addEventListener("input", updateCounter);
  $("#question").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) $("#ask").requestSubmit();
  });
  $("#new-btn").addEventListener("click", showNew);
  $("#brand").addEventListener("click", (e) => { e.preventDefault(); showNew(); });
  $("#cancel-btn").addEventListener("click", cancelJob);
  $("#delete-btn").addEventListener("click", deleteJob);
  document.querySelectorAll(".chip").forEach((c) => c.addEventListener("click", () => {
    $("#question").value = c.textContent;
    updateCounter();
    $("#question").focus();
  }));
  document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => {
    ui.view = t.dataset.view;
    loadReport();
  }));

  refreshHealth();
  setInterval(refreshHealth, 30000);
  setInterval(() => { if (ui.jobs.some((j) => !TERMINAL.has(j.status))) refreshJobs(); }, 5000);

  const id = location.hash.slice(1);
  if (/^[0-9a-f]{32}$/.test(id)) openJob(id); else showNew();
});
