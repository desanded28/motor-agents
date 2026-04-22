// BMW Agents dashboard — streams agent events via SSE.
// Uses fetch + ReadableStream (EventSource doesn't support POST).

async function runAgentStreamed(endpoint, body, onEvent) {
  const resp = await fetch(endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok || !resp.body) {
    onEvent({ type: "error", error: `HTTP ${resp.status}` });
    return;
  }
  const reader = resp.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) !== -1) {
      const chunk = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      if (chunk.startsWith("data: ")) {
        try {
          onEvent(JSON.parse(chunk.slice(6)));
        } catch (e) {
          console.error("parse", e, chunk);
        }
      }
    }
  }
}

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

// Extract URLs and local screenshot paths from agent report text and render them
// as interactive rows with a copy button. Keeps everything else as plain text.
function renderFinalReport(container, text) {
  clear(container);
  if (!text) return;

  const URL_RE = /(https?:\/\/[^\s)]+)/;
  const SHOT_RE = /(\/(?:Users|home)\/[^\s]+\.png)/;

  const lines = text.split("\n");
  for (const line of lines) {
    const urlMatch = line.match(URL_RE);
    const shotMatch = line.match(SHOT_RE);

    if (urlMatch) {
      renderLinkLine(container, line, urlMatch[0], /*isImage*/ false);
    } else if (shotMatch) {
      const raw = shotMatch[0];
      const filename = raw.split("/").pop();
      const webUrl = `/screenshots/${encodeURIComponent(filename)}`;
      renderLinkLine(container, line.replace(raw, webUrl), webUrl, /*isImage*/ true);
    } else {
      const p = el("div", "final-line", line || " ");
      container.appendChild(p);
    }
  }
}

function renderLinkLine(container, fullLine, url, isImage) {
  const row = el("div", "final-line link-line");
  // Split the line around the URL so we can render it as an anchor inline.
  const idx = fullLine.indexOf(url);
  if (idx > 0) row.appendChild(document.createTextNode(fullLine.slice(0, idx)));

  const a = el("a", "final-link");
  a.href = url;
  a.target = "_blank";
  a.rel = "noopener noreferrer";
  a.textContent = isImage ? url.split("/").pop() : url;
  row.appendChild(a);

  const after = fullLine.slice(idx + url.length);
  if (after) row.appendChild(document.createTextNode(after));

  const btn = el("button", "copy-btn", "COPY");
  btn.type = "button";
  btn.setAttribute("aria-label", "Copy link");
  btn.addEventListener("click", async (e) => {
    e.preventDefault();
    // Build absolute URL for screenshot paths so the copied value is shareable
    const abs = url.startsWith("http") ? url : window.location.origin + url;
    try {
      await navigator.clipboard.writeText(abs);
      btn.textContent = "COPIED";
      btn.classList.add("copied");
      setTimeout(() => {
        btn.textContent = "COPY";
        btn.classList.remove("copied");
      }, 1400);
    } catch {
      btn.textContent = "ERR";
      setTimeout(() => (btn.textContent = "COPY"), 1400);
    }
  });
  row.appendChild(btn);

  container.appendChild(row);
}

function truncate(s, n) {
  s = typeof s === "string" ? s : JSON.stringify(s);
  n = n || 200;
  return s.length > n ? s.slice(0, n) + "…" : s;
}

function renderEvent(container, ev) {
  const row = el("div", "event-row");
  if (ev.type === "start") {
    row.className += " event-start";
    row.textContent = `▸ agent started — input: ${truncate(ev.input, 80)}`;
  } else if (ev.type === "tool_call") {
    row.className += " event-tool";
    row.textContent = `→ ${ev.name}(${truncate(JSON.stringify(ev.args || {}), 160)})`;
  } else if (ev.type === "tool_result") {
    row.className += " event-result";
    row.textContent = `← ${truncate(JSON.stringify(ev.result || {}), 180)}  [${ev.duration_ms}ms]`;
  } else if (ev.type === "final") {
    row.className += " event-final";
    row.textContent = `✓ finished in ${ev.turns} turn(s)`;
  } else if (ev.type === "error") {
    row.className += " event-error";
    row.textContent = `✗ error: ${ev.error}`;
  } else if (ev.type === "keepalive" || ev.type === "done") {
    return;
  } else {
    row.textContent = JSON.stringify(ev);
  }
  container.appendChild(row);
  container.scrollTop = container.scrollHeight;
}

function verdictClass(v) {
  if (!v) return "";
  if (v === "STEAL") return "v-STEAL";
  if (v === "GOOD DEAL") return "v-GOOD";
  if (v === "FAIR") return "v-FAIR";
  if (v === "OVERPRICED") return "v-OVER";
  return "v-RIP";
}

function fmtEur(n) {
  if (n == null) return "—";
  return "€" + Number(n).toLocaleString("de-DE");
}

// ---------------- Agent runners ----------------

async function runDeal(e) {
  e.preventDefault();
  const input = document.getElementById("deal-input").value.trim();
  const btn = document.getElementById("deal-btn");
  const out = document.getElementById("deal-output");
  const events = document.getElementById("deal-events");
  const final = document.getElementById("deal-final");
  clear(events);
  clear(final);
  out.hidden = false;
  btn.disabled = true;
  btn.textContent = "Running…";

  try {
    await runAgentStreamed("/api/agent/deal", { input }, (ev) => {
      renderEvent(events, ev);
      if (ev.type === "done") {
        renderFinalReport(final, ev.final || "(no final output)");
        loadTraces();
      }
    });
  } finally {
    btn.disabled = false;
    btn.textContent = "Run agent";
  }
  return false;
}

async function runConfig(e) {
  e.preventDefault();
  const input = document.getElementById("config-input").value.trim();
  const btn = document.getElementById("config-btn");
  const out = document.getElementById("config-output");
  const events = document.getElementById("config-events");
  const grid = document.getElementById("config-screenshots");
  const final = document.getElementById("config-final");
  clear(events);
  clear(grid);
  clear(final);
  out.hidden = false;
  btn.disabled = true;
  btn.textContent = "Running…";

  async function pollScreenshots() {
    try {
      const r = await fetch("/api/screenshots/recent");
      const files = await r.json();
      clear(grid);
      for (const f of files.slice(0, 8)) {
        const shot = el("div", "shot");
        const img = el("img");
        img.src = `/screenshots/${encodeURIComponent(f.name)}`;
        img.alt = f.name;
        const cap = el("div", "cap", f.name.slice(16));
        shot.appendChild(img);
        shot.appendChild(cap);
        grid.appendChild(shot);
      }
    } catch (err) {}
  }
  const pollTimer = setInterval(pollScreenshots, 3000);

  try {
    await runAgentStreamed("/api/agent/config", { input }, (ev) => {
      renderEvent(events, ev);
      if (ev.type === "tool_result" && ev.name === "take_screenshot") pollScreenshots();
      if (ev.type === "done") {
        renderFinalReport(final, ev.final || "(no final output)");
        pollScreenshots();
        loadTraces();
      }
    });
  } finally {
    clearInterval(pollTimer);
    btn.disabled = false;
    btn.textContent = "Run agent";
  }
  return false;
}

async function runHunter(e) {
  e.preventDefault();
  const input = document.getElementById("hunter-input").value.trim();
  const real = document.getElementById("hunter-real").checked;
  const btn = document.getElementById("hunter-btn");
  const out = document.getElementById("hunter-output");
  const events = document.getElementById("hunter-events");
  const table = document.getElementById("hunter-table");
  const final = document.getElementById("hunter-final");
  clear(events);
  clear(table);
  clear(final);
  out.hidden = false;
  btn.disabled = true;
  btn.textContent = "Running…";

  try {
    await runAgentStreamed(
      "/api/agent/hunter",
      { input, real_sources: real },
      async (ev) => {
        renderEvent(events, ev);
        if (ev.type === "tool_result" && ev.name === "rank_top") {
          const top = (ev.result && ev.result.top) || [];
          renderHunterTable(table, top);
        }
        if (ev.type === "done") {
          renderFinalReport(final, ev.final || "(no final output)");
          const r = await fetch("/api/db/top?limit=10");
          const deals = await r.json();
          if (!table.children.length) renderHunterTable(table, deals);
          loadStats();
          loadTraces();
        }
      }
    );
  } finally {
    btn.disabled = false;
    btn.textContent = "Run agent";
  }
  return false;
}

function renderHunterTable(container, top) {
  clear(container);
  const wrap = el("div", "deals-table");
  top.forEach((l, i) => {
    const row = el("div", "deal-row");
    row.appendChild(el("div", "deal-idx", `#${i + 1}`));
    const info = el("div");
    const brandPrefix = (l.brand || l.brand_matched) ? `${l.brand || l.brand_matched} · ` : "";
    info.appendChild(el("div", "deal-title", `${l.verdict_emoji || ""} ${brandPrefix}${l.trim || l.model || "?"}`));
    const meta = `${l.model_year || "?"} · ${(l.mileage_km || 0).toLocaleString("de-DE")} km${l.location ? " · " + l.location : ""}`;
    info.appendChild(el("div", "deal-meta", meta));
    row.appendChild(info);
    row.appendChild(el("div", "deal-price", fmtEur(l.asking_price_eur)));
    row.appendChild(el("div", "deal-price", fmtEur(l.fair_value_eur)));
    const savings = -(l.delta_eur || 0);
    const vText = `${fmtEur(savings)} (${(-1 * (l.delta_pct || 0)).toFixed(1)}%)`;
    row.appendChild(el("div", `deal-verdict ${verdictClass(l.verdict)}`, vText));
    wrap.appendChild(row);
  });
  container.appendChild(wrap);
}

// ---------------- Dashboard state ----------------

async function loadStats() {
  try {
    const s = await (await fetch("/api/db/stats")).json();
    document.getElementById("stat-listings").textContent = s.total_listings;
    const keyPill = document.getElementById("key-status");
    if (s.has_api_key) {
      keyPill.textContent = "API key set";
      keyPill.className = "key-pill ok";
    } else {
      keyPill.textContent = "No API key";
      keyPill.className = "key-pill missing";
    }
  } catch (e) {
    document.getElementById("stat-listings").textContent = "!";
  }
}

async function loadTraces() {
  try {
    const traces = await (await fetch("/api/traces?limit=12")).json();
    document.getElementById("stat-traces").textContent = traces.length;
    const tbody = document.getElementById("traces-body");
    clear(tbody);
    if (!traces.length) {
      const tr = el("tr");
      const td = el("td", "muted", "No traces yet — run an agent above.");
      td.setAttribute("colspan", "7");
      tr.appendChild(td);
      tbody.appendChild(tr);
      return;
    }
    for (const t of traces) {
      const tr = el("tr");
      const when = new Date((t.started_at || 0) * 1000).toLocaleString();
      tr.appendChild(el("td", "", when));
      tr.appendChild(el("td", "", t.agent || ""));
      tr.appendChild(el("td", "muted", (t.input || "").slice(0, 70)));
      tr.appendChild(el("td", "num", String(t.turns)));
      tr.appendChild(el("td", "num", String(t.tool_count)));
      tr.appendChild(el("td", "num", `${(t.duration_s || 0).toFixed(1)}s`));
      const btnCell = el("td");
      const btn = el("button", "secondary", "View");
      btn.addEventListener("click", () => openTrace(t.path));
      btnCell.appendChild(btn);
      tr.appendChild(btnCell);
      tbody.appendChild(tr);
    }
  } catch (e) {
    console.error(e);
  }
}

async function openTrace(name) {
  const d = document.getElementById("trace-dialog");
  document.getElementById("trace-title").textContent = name;
  document.getElementById("trace-body").textContent = "Loading…";
  try {
    const t = await (await fetch(`/api/traces/${encodeURIComponent(name)}`)).json();
    document.getElementById("trace-body").textContent = JSON.stringify(t, null, 2);
  } catch (e) {
    document.getElementById("trace-body").textContent = "Failed to load: " + e;
  }
  d.showModal();
}

window.addEventListener("DOMContentLoaded", () => {
  loadStats();
  loadTraces();
  document.querySelectorAll(".nav a").forEach((a) => {
    a.addEventListener("click", () => {
      document.querySelectorAll(".nav a").forEach((x) => x.classList.remove("active"));
      a.classList.add("active");
    });
  });
});
