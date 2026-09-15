/* Code Pulse — фронтенд дашборда */
"use strict";

const $ = (sel) => document.querySelector(sel);
const state = {
  overview: null,
  selected: null,
  qualityChart: null,
  churnChart: null,
  sim: null,
  subsOn: { score: true, mi: true, arch: true, cog: true, dup: true, test: false, todo: false },
};
const SUB_LABELS = {
  score: "Итог", mi: "MI", arch: "Архитектура", cog: "Когнитивная",
  dup: "Дублирование", test: "Тесты", todo: "TODO",
};

/* ---------------------------------------------------------------- утилиты */
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function scoreColor(score) {
  if (score == null) return "#8b949e";
  return `hsl(${Math.max(0, Math.min(120, score * 1.2))}, 65%, 50%)`;
}
function deltaColor(d) {
  if (d == null || Math.abs(d) < 0.5) return "#d29922";
  return d > 0 ? "#3fb950" : "#f85149";
}
function fmtDate(ts) {
  return new Date(ts * 1000).toLocaleDateString("ru-RU", { day: "2-digit", month: "short" });
}
function relTime(ts) {
  if (!ts) return "—";
  const s = Date.now() / 1000 - ts;
  if (s < 90) return "только что";
  if (s < 3600) return `${Math.floor(s / 60)} мин назад`;
  if (s < 86400) return `${Math.floor(s / 3600)} ч назад`;
  return `${Math.floor(s / 86400)} дн назад`;
}
function fmtNum(n) {
  if (n == null) return "—";
  if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (Math.abs(n) >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return String(Math.round(n * 100) / 100);
}

function sparkSVG(series, color, w = 150, h = 34) {
  const pts = series.filter((p) => p.score != null);
  if (pts.length < 2) return `<svg width="${w}" height="${h}"></svg>`;
  const lo = Math.min(...pts.map((p) => p.score));
  const hi = Math.max(...pts.map((p) => p.score));
  const span = Math.max(5, hi - lo);
  const x = (i) => 2 + (i / (pts.length - 1)) * (w - 4);
  const y = (v) => 3 + (1 - (v - lo) / span) * (h - 6);
  const d = pts.map((p, i) => `${x(i).toFixed(1)},${y(p.score).toFixed(1)}`).join(" ");
  return `<svg width="${w}" height="${h}">
    <polyline points="${d}" fill="none" stroke="${color}" stroke-width="1.6"
      stroke-linejoin="round"/><circle cx="${x(pts.length - 1)}" cy="${y(pts[pts.length - 1].score)}"
      r="2.6" fill="${color}"/></svg>`;
}

function ringSVG(score) {
  const c = scoreColor(score);
  const v = score == null ? "—" : Math.round(score);
  const frac = score == null ? 0 : score / 100;
  const r = 21, circ = 2 * Math.PI * r;
  return `<div class="ring">
    <svg width="52" height="52">
      <circle cx="26" cy="26" r="${r}" stroke="#2d333b" stroke-width="5" fill="none"/>
      <circle cx="26" cy="26" r="${r}" stroke="${c}" stroke-width="5" fill="none"
        stroke-dasharray="${(circ * frac).toFixed(1)} ${circ.toFixed(1)}" stroke-linecap="round"/>
    </svg><div class="val" style="color:${c}">${v}</div></div>`;
}

/* ------------------------------------------------------------- API-клиент */
async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) throw new Error(`${path}: ${res.status}`);
  return res.json();
}

/* ------------------------------------------------------------ статус/фон */
let pollTimer = null;
function pollStatus(onDone) {
  clearTimeout(pollTimer);
  api("/api/status").then((st) => {
    const busy = st.phase !== "idle";
    $("#progressBar").classList.toggle("hidden", !busy);
    $("#btnRefresh").disabled = busy;
    $("#btnBackfill").disabled = busy;
    if (busy) {
      const pct = st.total ? Math.round((st.done / st.total) * 100) : 5;
      $("#progressFill").style.width = pct + "%";
      $("#progressText").textContent =
        `${st.phase === "backfill" ? "Пересчёт истории" : "Сканирование"}: ` +
        `${st.done}/${st.total} ${st.current || ""} ${st.error ? "⚠ " + st.error : ""}`;
      pollTimer = setTimeout(() => pollStatus(onDone), 800);
    } else {
      if (onDone) onDone();
    }
  }).catch(() => { pollTimer = setTimeout(() => pollStatus(onDone), 2000); });
}

/* ----------------------------------------------------------------- обзор */
function liveBadges(live) {
  if (!live) return "";
  if (live.no_git) return `<span class="badge">без git</span>`;
  let out = `<span class="badge">${esc(live.branch)}</span>`;
  if (live.dirty_total > 0) {
    out += ` <span class="badge ${live.dirty.U ? "conflict" : "dirty"}">✎ ${live.dirty_total}</span>`;
  }
  if (live.ahead) out += ` <span class="badge">↑${live.ahead}</span>`;
  if (live.behind) out += ` <span class="badge">↓${live.behind}</span>`;
  if (live.stash) out += ` <span class="badge">stash ${live.stash}</span>`;
  return out;
}

function renderSummary(projects) {
  const withScore = projects.filter((p) => p.score != null);
  const dirty = projects.filter((p) => p.live && p.live.dirty_total > 0).length;
  const totalSloc = projects.reduce((a, p) => {
    const s = p.series.at(-1);
    return a + (s?.totals?.sloc || 0);
  }, 0);
  const avg = withScore.length
    ? (withScore.reduce((a, p) => a + p.score, 0) / withScore.length).toFixed(1) : "—";
  const rising = withScore.filter((p) => (p.delta30 ?? 0) > 0.5).length;
  const falling = withScore.filter((p) => (p.delta30 ?? 0) < -0.5).length;
  $("#summaryStrip").innerHTML = `
    <div class="stat"><b>${projects.length}</b><span>проектов</span></div>
    <div class="stat"><b>${fmtNum(totalSloc)}</b><span>строк кода</span></div>
    <div class="stat"><b style="color:${scoreColor(+avg)}">${avg}</b><span>средний скор</span></div>
    <div class="stat"><b style="color:var(--green)">↑ ${rising}</b><span>растёт</span></div>
    <div class="stat"><b style="color:var(--red)">↓ ${falling}</b><span>деградирует</span></div>
    <div class="stat"><b>${dirty}</b><span>с изменениями</span></div>`;
}

function cardHTML(p) {
  const last = p.series.at(-1) || {};
  const t = last.totals || {};
  const c = last.coupling || {};
  const delta = p.delta30;
  const deltaHTML = delta == null ? "" :
    `<span class="${delta >= 0 ? "delta-up" : "delta-down"}">${delta >= 0 ? "▲" : "▼"} ${Math.abs(delta)}</span>`;
  const sparkColor = deltaColor(delta ?? (p.score != null ? 0 : null));
  const live = p.live || {};
  const lastMsg = live.last_commit?.msg
    ? ` · ${esc(live.last_commit.msg.slice(0, 44))}${live.last_commit.msg.length > 44 ? "…" : ""}` : "";
  return `
  <div class="card" data-name="${esc(p.name)}">
    <div class="head">
      ${ringSVG(p.score)}
      <div style="flex:1">
        <div class="name">${esc(p.name)} ${deltaHTML}</div>
        <div class="stack">${esc(p.stack)} · ${fmtNum(t.sloc || 0)} SLOC</div>
      </div>
    </div>
    <div class="mid">
      <div class="spark">${sparkSVG(p.series, sparkColor)}</div>
    </div>
    <div class="metrics">
      <span>MI <b>${t.mi_avg?.toFixed(0) ?? "—"}</b></span>
      <span>cog/KLOC <b>${t.cog_per_kloc?.toFixed(0) ?? "—"}</b></span>
      <span>дубл <b>${t.dup_pct != null ? t.dup_pct + "%" : "—"}</b></span>
      <span>D <b>${c.mean_D?.toFixed(2) ?? "—"}</b></span>
      <span>циклы <b style="${c.cycles ? "color:var(--red)" : ""}">${c.cycles ?? "—"}</b></span>
      <span>god-файлы <b>${t.god_files ?? "—"}</b></span>
    </div>
    <div class="live">${liveBadges(live)}<span>${relTime(live.last_commit?.ts)}${lastMsg}</span></div>
  </div>`;
}

function renderCards() {
  const ov = state.overview;
  if (!ov) return;
  const q = $("#search").value.trim().toLowerCase();
  const st = $("#stackFilter").value;
  const gf = $("#gitFilter").value;
  const filtered = ov.projects.filter((p) => {
    if (q && !p.name.toLowerCase().includes(q)) return false;
    if (st === "—" && p.stack !== "—") return false;
    if (st && st !== "—" && !p.stack.includes(st)) return false;
    if (gf === "nogit" && p.is_git) return false;
    if (gf === "dirty" && !(p.live && p.live.dirty_total > 0)) return false;
    if (gf === "clean" && (!p.is_git || (p.live && p.live.dirty_total > 0))) return false;
    return true;
  });
  $("#countLabel").textContent = `${filtered.length} из ${ov.projects.length}`;
  $("#cards").innerHTML = filtered.map(cardHTML).join("") ||
    `<div class="muted">Ничего не найдено.</div>`;
  $("#cards").querySelectorAll(".card").forEach((el) => {
    el.onclick = () => selectProject(el.dataset.name);
  });
  renderSummary(ov.projects);
  const lastScan = Math.max(...ov.projects.map((p) => p.live?.scanned_at || 0), 0);
  $("#lastScan").textContent = lastScan ? `Последнее сканирование: ${relTime(lastScan)}` : "";
}

/* --------------------------------------------------------------- проект */
async function selectProject(name) {
  state.selected = name;
  history.replaceState(null, "", `#project=${encodeURIComponent(name)}`);
  showTab("project");
  $("#projectTab").classList.remove("hidden");
  $("#projectTab").textContent = `Проект: ${name}`;
  const d = await api(`/api/project?name=${encodeURIComponent(name)}`);
  renderProject(d);
}

function iClass(i) { return i < 0.34 ? "i-low" : i < 0.67 ? "i-mid" : "i-high"; }

function renderProject(d) {
  const live = d.live || {};
  $("#projectHeader").innerHTML = `
    <h2>${esc(d.name)}
      ${liveBadges(live)}
      <span class="path">${esc(d.path)}</span>
    </h2>
    <div class="path">${esc(d.stack || "")} · коммитов за 30д: ${d.evo?.commits_30d ?? 0}
      · отсканировано ${relTime(live.scanned_at)}</div>`;

  renderQualityChart(d);
  renderChurnChart(d.evo?.churn_weeks || []);

  // пакеты
  const pkgs = d.coupling?.packages || [];
  $("#pkgTable").innerHTML = pkgs.length ? `
    <thead><tr><th>пакет</th><th class="num">SLOC</th><th class="num">Ca</th>
    <th class="num">Ce</th><th class="num">I</th><th class="num">A</th><th class="num">D</th></tr></thead>
    <tbody>${pkgs.map((p) => `
      <tr><td class="file">${esc(p.pkg)}</td>
      <td class="num">${fmtNum(p.sloc)}</td>
      <td class="num">${p.ca}</td><td class="num">${p.ce}</td>
      <td class="num ${iClass(p.I)}">${p.I.toFixed(2)}</td>
      <td class="num">${p.A.toFixed(2)}</td>
      <td class="num ${p.D > 0.7 ? "i-high" : p.D > 0.4 ? "i-mid" : "i-low"}">${p.D.toFixed(2)}</td>
      </tr>`).join("")}</tbody>
    <tfoot><tr><td><b>итого</b></td><td class="num"><b>${fmtNum(pkgs.reduce((a, p) => a + p.sloc, 0))}</b></td>
    <td colspan="5" class="muted">циклов зависимостей: <b style="${d.coupling?.cycles ? "color:var(--red)" : "color:var(--green)"}">${d.coupling?.cycles ?? 0}</b>
    · нарушений (I>0.5 при Ca≥3): ${d.coupling?.violations ?? 0}</td></tr></tfoot>`
    : `<div class="muted">Нет данных о пакетах.</div>`;

  // хотспоты
  const hs = d.evo?.hotspots || [];
  $("#hotspotTable").innerHTML = hs.length ? `
    <thead><tr><th>файл</th><th class="num">cog</th><th class="num">SLOC</th>
    <th class="num">коммитов</th><th class="num">churn</th><th class="num">score</th></tr></thead>
    <tbody>${hs.map((h) => `
      <tr><td class="file">${esc(h.file)}</td>
      <td class="num">${h.cog}</td><td class="num">${fmtNum(h.sloc)}</td>
      <td class="num">${h.commits}</td><td class="num">${fmtNum(h.churn)}</td>
      <td class="num" style="color:${h.score > 300 ? "var(--red)" : h.score > 100 ? "var(--amber)" : "var(--text)"}">${fmtNum(h.score)}</td></tr>`).join("")}</tbody>`
    : `<div class="muted">Нет коммитов за период — либо проект стабилен, либо спит.</div>`;

  // временное зацепление
  const tp = d.evo?.temporal || [];
  $("#temporalTable").innerHTML = tp.length ? `
    <thead><tr><th>файл A</th><th>файл B</th><th class="num">вместе</th></tr></thead>
    <tbody>${tp.map((t) => `
      <tr><td class="file">${esc(t.a)}</td><td class="file">${esc(t.b)}</td>
      <td class="num">${t.shared}</td></tr>`).join("")}</tbody>`
    : `<div class="muted">Не обнаружено — границы модулей меняются независимо (это хорошо).</div>`;

  // тяжёлые файлы
  const wf = d.worst_files || [];
  $("#worstTable").innerHTML = wf.length ? `
    <thead><tr><th>файл</th><th class="num">SLOC</th><th class="num">cog</th>
    <th class="num">cc</th><th class="num">MI</th><th class="num">LCOM4</th></tr></thead>
    <tbody>${wf.map((f) => `
      <tr><td class="file">${esc(f.file)}</td>
      <td class="num">${fmtNum(f.sloc)}</td><td class="num">${f.cog}</td>
      <td class="num">${f.cc}</td>
      <td class="num" style="color:${scoreColor(f.mi)}">${f.mi?.toFixed(0)}</td>
      <td class="num ${f.lcom4_max >= 5 ? "i-high" : ""}">${f.lcom4_max || "—"}</td></tr>`).join("")}</tbody>`
    : `<div class="muted">Нет данных.</div>`;

  // живое состояние
  const dirty = live.dirty_files || [];
  $("#liveDetail").innerHTML = `
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px">
      <span class="badge">ветка: ${esc(live.branch || "—")}</span>
      <span class="badge">изменений: ${live.dirty_total ?? 0}</span>
      <span class="badge">↑${live.ahead ?? 0} / ↓${live.behind ?? 0}</span>
      <span class="badge">stash: ${live.stash ?? 0}</span>
      <span class="badge">7д: ${live.commits_7d ?? 0} комм.</span>
      <span class="badge">30д: ${live.commits_30d ?? 0} комм.</span>
    </div>
    <div class="muted" style="margin-bottom:4px">Последний коммит: ${relTime(live.last_commit?.ts)}
      <span class="file">${esc(live.last_commit?.hash || "")}</span></div>
    <div class="file muted">${esc(live.last_commit?.msg || "")}</div>
    ${dirty.length ? `<div class="muted" style="margin:8px 0 4px">Незакоммиченные файлы:</div>
      <div class="file" style="max-height:140px;overflow:auto">${dirty.map((f) => esc(f)).join("<br>")}</div>`
      : ""}`;

  // возраст кода
  const age = d.evo?.age?.buckets || {};
  const total = Object.values(age).reduce((a, b) => a + b, 0) || 1;
  const defs = [
    ["< 7д", age.d7, "#3fb950"], ["< 30д", age.d30, "#d29922"],
    ["< 90д", age.d90, "#f0883e"], ["≥ 90д (застой)", age.stale, "#f85149"],
  ];
  $("#ageBars").innerHTML = `
    <div class="agebar">${defs.map(([lab, v, c]) =>
      `<div style="width:${(v / total * 100).toFixed(1)}%;background:${c}"
        title="${lab}: ${fmtNum(v)} SLOC">${v / total > 0.12 ? Math.round(v / total * 100) + "%" : ""}</div>`).join("")}
    </div>
    <div class="legendrow">${defs.map(([lab, v, c]) =>
      `<span><i style="background:${c}"></i>${lab}: ${fmtNum(v)}</span>`).join("")}</div>
    ${d.evo?.age?.stale_share > 0.6 ? '<div class="muted" style="margin-top:6px">⚠ Больше 60% кода не менялось 90+ дней — возможный мёртвый код.</div>' : ""}`;
}

function renderQualityChart(d) {
  const pts = d.series.map((s) => ({ x: s.ts, ...s.subs }));
  const labels = pts.map((p) => fmtDate(p.x));
  const colors = {
    score: "#e6edf3", mi: "#58a6ff", arch: "#bc8cff",
    cog: "#d29922", dup: "#f0883e", test: "#3fb950", todo: "#8b949e",
  };
  $("#subToggles").innerHTML = Object.keys(SUB_LABELS).map((k) =>
    `<span class="chip ${state.subsOn[k] ? "on" : ""}" data-k="${k}"
      style="border-color:${state.subsOn[k] ? colors[k] : ""}">${SUB_LABELS[k]}</span>`).join("");
  $("#subToggles").querySelectorAll(".chip").forEach((ch) => {
    ch.onclick = () => {
      const k = ch.dataset.k;
      state.subsOn[k] = !state.subsOn[k];
      renderQualityChart(d);
    };
  });
  const datasets = Object.keys(SUB_LABELS)
    .filter((k) => state.subsOn[k])
    .map((k) => ({
      label: SUB_LABELS[k],
      data: pts.map((p) => p[k] ?? null),
      borderColor: colors[k],
      backgroundColor: colors[k] + "22",
      borderWidth: k === "score" ? 2.5 : 1.4,
      pointRadius: 3,
      tension: 0.25,
      spanGaps: true,
    }));
  if (state.qualityChart) state.qualityChart.destroy();
  state.qualityChart = new Chart($("#qualityChart"), {
    type: "line",
    data: { labels, datasets },
    options: {
      responsive: true,
      scales: { y: { min: 0, max: 100, ticks: { color: "#8b949e" } },
                x: { ticks: { color: "#8b949e", maxRotation: 0, autoSkip: true } } },
      plugins: { legend: { labels: { color: "#e6edf3", boxWidth: 12 } } },
    },
  });
}

function renderChurnChart(weeks) {
  const labels = weeks.map((w) => fmtDate(w.ts));
  if (state.churnChart) state.churnChart.destroy();
  state.churnChart = new Chart($("#churnChart"), {
    type: "bar",
    data: {
      labels,
      datasets: [
        { label: "добавлено", data: weeks.map((w) => w.add), backgroundColor: "#3fb95088" },
        { label: "удалено", data: weeks.map((w) => -w.del), backgroundColor: "#f8514988" },
      ],
    },
    options: {
      responsive: true,
      scales: { x: { stacked: true, ticks: { color: "#8b949e" } },
                y: { stacked: true, ticks: { color: "#8b949e" } } },
      plugins: { legend: { labels: { color: "#e6edf3", boxWidth: 12 } } },
    },
  });
}

/* ----------------------------------------------------------------- граф */
function startGraph() {
  api("/api/graph").then((g) => {
    const canvas = $("#graphCanvas");
    canvas.querySelectorAll?.("*");
    state.sim = new ForceSim(canvas, g.nodes, g.edges, (name) => selectProject(name));
    state.sim.start();
  }).catch((e) => console.error(e));
}

class ForceSim {
  constructor(canvas, nodes, edges, onPick) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.onPick = onPick;
    this.alpha = 1;
    this.drag = null;
    this.hover = null;
    this.nodes = nodes.map((n) => ({
      ...n, x: 0, y: 0, vx: 0, vy: 0,
      r: 8 + 18 * Math.sqrt((n.debt || 0) / Math.max(1, Math.max(...nodes.map((m) => m.debt || 0)))),
    }));
    const W = () => canvas.clientWidth, H = () => canvas.clientHeight;
    this.nodes.forEach((n, i) => {
      const a = (i / this.nodes.length) * Math.PI * 2;
      n.x = W() / 2 + Math.cos(a) * W() / 4;
      n.y = H() / 2 + Math.sin(a) * H() / 4;
    });
    this.edges = edges.map((e) => ({
      a: this.nodes.find((n) => n.name === e.a),
      b: this.nodes.find((n) => n.name === e.b),
      w: e.days,
    })).filter((e) => e.a && e.b);
    canvas.addEventListener("mousemove", (ev) => this.onMove(ev));
    canvas.addEventListener("mousedown", (ev) => this.onDown(ev));
    window.addEventListener("mouseup", () => { this.drag = null; });
    canvas.addEventListener("click", (ev) => {
      if (this.moved) return;
      const n = this.pick(ev);
      if (n) this.onPick(n.name);
    });
  }

  pos(ev) {
    const r = this.canvas.getBoundingClientRect();
    return { x: ev.clientX - r.left, y: ev.clientY - r.top };
  }
  pick(ev) {
    const p = this.pos(ev);
    for (let i = this.nodes.length - 1; i >= 0; i--) {
      const n = this.nodes[i];
      if ((n.x - p.x) ** 2 + (n.y - p.y) ** 2 < (n.r + 4) ** 2) return n;
    }
    return null;
  }
  onDown(ev) {
    const n = this.pick(ev);
    this.moved = false;
    if (n) { this.drag = n; this.alpha = Math.max(this.alpha, 0.35); }
  }
  onMove(ev) {
    const p = this.pos(ev);
    if (this.drag) {
      const dx = p.x - this.drag.x, dy = p.y - this.drag.y;
      if (Math.abs(dx) + Math.abs(dy) > 3) this.moved = true;
      this.drag.x = p.x; this.drag.y = p.y; this.drag.vx = this.drag.vy = 0;
      return;
    }
    this.hover = this.pick(ev);
    this.canvas.style.cursor = this.hover ? "pointer" : "grab";
    this.draw();
    this.renderTooltip(ev);
  }
  renderTooltip(ev) {
    const tip = $("#graphTooltip");
    const n = this.hover;
    if (!n) { tip.classList.add("hidden"); return; }
    const r = this.canvas.getBoundingClientRect();
    tip.classList.remove("hidden");
    tip.style.left = Math.min(ev.clientX - r.left + 14, r.width - 260) + "px";
    tip.style.top = (ev.clientY - r.top + 14) + "px";
    tip.innerHTML = `<b>${esc(n.name)}</b><br>скор: <b style="color:${scoreColor(n.score)}">${n.score ?? "—"}</b>
      · ${fmtNum(n.sloc)} SLOC<br>${esc(n.stack || "")}
      ${n.cycles ? `<br><span style="color:var(--red)">циклов зависимостей: ${n.cycles}</span>` : ""}`;
  }

  step() {
    const ns = this.nodes;
    const W = this.canvas.clientWidth, H = this.canvas.clientHeight;
    const rep = 4200;
    for (let i = 0; i < ns.length; i++) {
      for (let j = i + 1; j < ns.length; j++) {
        const a = ns[i], b = ns[j];
        let dx = b.x - a.x, dy = b.y - a.y;
        let d2 = dx * dx + dy * dy || 1;
        const f = (rep * this.alpha) / d2;
        const d = Math.sqrt(d2);
        dx /= d; dy /= d;
        a.vx -= dx * f; a.vy -= dy * f;
        b.vx += dx * f; b.vy += dy * f;
      }
    }
    for (const e of this.edges) {
      const k = 0.012 * this.alpha * Math.min(4, e.w / 3);
      let dx = e.b.x - e.a.x, dy = e.b.y - e.a.y;
      const d = Math.sqrt(dx * dx + dy * dy) || 1;
      const want = 150;
      e.a.vx += (dx / d) * (d - want) * k;
      e.a.vy += (dy / d) * (d - want) * k;
      e.b.vx -= (dx / d) * (d - want) * k;
      e.b.vy -= (dy / d) * (d - want) * k;
    }
    for (const n of ns) {
      n.vx += (W / 2 - n.x) * 0.0035 * this.alpha;
      n.vy += (H / 2 - n.y) * 0.0035 * this.alpha;
      if (n !== this.drag) {
        n.vx *= 0.86; n.vy *= 0.86;
        n.x += Math.max(-12, Math.min(12, n.vx));
        n.y += Math.max(-12, Math.min(12, n.vy));
      }
      n.x = Math.max(n.r, Math.min(W - n.r, n.x));
      n.y = Math.max(n.r, Math.min(H - n.r, n.y));
    }
    this.alpha *= 0.995;
  }

  draw() {
    const dpr = window.devicePixelRatio || 1;
    const W = this.canvas.clientWidth, H = this.canvas.clientHeight;
    if (this.canvas.width !== W * dpr) {
      this.canvas.width = W * dpr;
      this.canvas.height = H * dpr;
    }
    const ctx = this.ctx;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    for (const e of this.edges) {
      ctx.strokeStyle = "rgba(139,148,158,0.3)";
      ctx.lineWidth = Math.min(4, 1 + e.w / 5);
      ctx.beginPath();
      ctx.moveTo(e.a.x, e.a.y);
      ctx.lineTo(e.b.x, e.b.y);
      ctx.stroke();
      const mx = (e.a.x + e.b.x) / 2, my = (e.a.y + e.b.y) / 2;
      ctx.fillStyle = "rgba(139,148,158,0.7)";
      ctx.font = "10px system-ui";
      ctx.textAlign = "center";
      ctx.fillText(`${e.w}д`, mx, my - 3);
    }
    for (const n of this.nodes) {
      let color = "#d29922";
      const s = this.deltaByName?.get(n.name);
      if (s != null) color = deltaColor(s);
      if (n.score == null) color = "#484f58";
      ctx.beginPath();
      ctx.arc(n.x, n.y, n.r, 0, Math.PI * 2);
      ctx.fillStyle = color + (this.hover === n ? "" : "cc");
      ctx.fill();
      if (this.hover === n) {
        ctx.strokeStyle = "#e6edf3";
        ctx.lineWidth = 2;
        ctx.stroke();
      }
      ctx.fillStyle = "#0d1117";
      ctx.font = `600 ${Math.min(15, n.r)}px system-ui`;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      if (n.score != null && n.r > 11) ctx.fillText(Math.round(n.score), n.x, n.y);
      ctx.fillStyle = "#e6edf3";
      ctx.font = "11px system-ui";
      ctx.textBaseline = "top";
      ctx.fillText(n.name.length > 22 ? n.name.slice(0, 21) + "…" : n.name,
        n.x, n.y + n.r + 3);
    }
  }

  async loadDeltas() {
    const ov = await api("/api/overview");
    this.deltaByName = new Map(ov.projects.map((p) => [p.name, p.delta30]));
    this.draw();
  }

  start() {
    this.loadDeltas();
    const loop = () => {
      if (!document.body.contains(this.canvas)) return;
      if ($("#view-graph").classList.contains("hidden")) {
        requestAnimationFrame(loop);
        return;
      }
      if (this.alpha > 0.01) this.step();
      this.draw();
      requestAnimationFrame(loop);
    };
    requestAnimationFrame(loop);
  }
}

/* ---------------------------------------------------------------- табы */
function showTab(name) {
  document.querySelectorAll(".tab").forEach((t) =>
    t.classList.toggle("active", t.dataset.tab === name));
  ["overview", "graph", "project"].forEach((v) =>
    $("#view-" + v).classList.toggle("hidden", v !== name));
  if (name === "graph" && !state.sim) startGraph();
  if (name === "overview") renderCards();
}

/* ------------------------------------------------------------- настройки */
let settingsData = null;

async function openSettings() {
  $("#settingsModal").classList.remove("hidden");
  $("#settingsSearch").value = "";
  settingsData = await api("/api/settings");
  renderSettings();
}

function closeSettings() { $("#settingsModal").classList.add("hidden"); }

function renderSettings() {
  if (!settingsData) return;
  const q = $("#settingsSearch").value.trim().toLowerCase();
  const items = settingsData.projects.filter((p) =>
    !q || p.name.toLowerCase().includes(q));
  const on = settingsData.projects.filter((p) => !p.excluded).length;
  $("#settingsCounts").textContent =
    `${on} включено · ${settingsData.projects.length - on} исключено`;
  $("#settingsList").innerHTML = items.map((p) => {
    const meta = [
      p.excluded ? "исключён — не сканируется"
        : (!p.tracked ? "новый — попадёт в дашборд при скане" : (p.stack || "")),
      p.score != null ? `скор ${Math.round(p.score)}` : "",
      p.is_git ? "" : "без git",
    ].filter(Boolean).join(" · ");
    return `
    <div class="settings-row ${p.excluded ? "excluded" : ""}">
      <label class="switch" title="${p.excluded ? "Включить" : "Исключить"}">
        <input type="checkbox" data-name="${esc(p.name)}" ${p.excluded ? "" : "checked"}>
        <span class="slider"></span>
      </label>
      <span class="name">${esc(p.name)}</span>
      <span class="meta">${esc(meta)}</span>
    </div>`;
  }).join("") || `<div class="muted" style="padding:12px">Не найдено.</div>`;
  $("#settingsList").querySelectorAll("input[type=checkbox]").forEach((inp) => {
    inp.onchange = () => toggleExclude(inp.dataset.name, !inp.checked, inp);
  });
}

async function toggleExclude(name, excluded, inp) {
  inp.disabled = true;
  try {
    const res = await api("/api/settings/exclude", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, excluded }),
    });
    if (!res.ok) { inp.disabled = false; return; }
    // локально обновляем состояние строки
    const item = settingsData.projects.find((p) => p.name === name);
    if (item) item.excluded = excluded;
    renderSettings();
    if (excluded) {
      await loadOverview();                       // проект исчез сразу
    } else {
      pollStatus(async () => {                    // включён — ждём точечный скан
        await loadOverview();
        const it = settingsData?.projects.find((p) => p.name === name);
        if (it && it.tracked) renderSettings();
      });
    }
  } catch (e) {
    inp.disabled = false;
    console.error(e);
  }
}

/* ---------------------------------------------------------------- старт */
async function loadOverview() {
  state.overview = await api("/api/overview");
  $("#rootPath").textContent = "D:\\Projects · живое состояние и телеметрия";
  renderCards();
}

function applyHash() {
  const m = location.hash.match(/^#project=(.+)$/);
  if (m) {
    selectProject(decodeURIComponent(m[1]));
    return;
  }
  if (location.hash === "#graph") showTab("graph");
  if (location.hash === "#settings") openSettings();
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".tab").forEach((t) => {
    t.onclick = () => {
      if (t.dataset.tab === "project" && !state.selected) return;
      showTab(t.dataset.tab);
    };
  });
  $("#search").oninput = renderCards;
  $("#stackFilter").onchange = renderCards;
  $("#gitFilter").onchange = renderCards;

  $("#btnRefresh").onclick = async () => {
    await api("/api/refresh", { method: "POST" });
    pollStatus(async () => { await loadOverview(); });
  };
  $("#btnBackfill").onclick = async () => {
    await api("/api/backfill", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ days: 30, step_days: 3 }),
    });
    pollStatus(async () => { await loadOverview(); });
  };

  $("#btnSettings").onclick = openSettings;
  $("#btnCloseSettings").onclick = closeSettings;
  $("#settingsModal").onclick = (ev) => {
    if (ev.target === $("#settingsModal")) closeSettings();
  };
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") closeSettings();
  });
  $("#settingsSearch").oninput = renderSettings;

  pollStatus(loadOverview);
  loadOverview().then(applyHash);
});
