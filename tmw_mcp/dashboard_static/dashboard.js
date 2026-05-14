// TMW dashboard frontend. Vanilla JS, no frameworks.
//
// Connects to /events (SSE), redraws the canvas and side panels on each
// snapshot. Auto-reconnects every 2 s when the stream drops.

"use strict";

const TILE_PX = 16;          // size of each map tile in pixels
const VIEW_RADIUS = 15;      // tiles in each direction around Claudius
const VIEW_TILES = VIEW_RADIUS * 2 + 1;

const canvas = document.getElementById("map-canvas");
const ctx = canvas.getContext("2d");
canvas.width = VIEW_TILES * TILE_PX;
canvas.height = VIEW_TILES * TILE_PX;

let latestState = null;
let mapCache = new Map();    // map_name -> { width, height, walkable, warps }
let pendingMapFetches = new Set();
let hoverTile = null;        // {x, y} in tile coords, when mouse over canvas

// Transient operator-click visual feedback.
let clickRipples = [];       // { x, y, t0 } in world-tile coords
let attackHighlights = [];   // { id, t0 }
let lastBeingPickAt = 0;

// ---------------------------------------------------------------------------
// SSE connection with auto-reconnect
// ---------------------------------------------------------------------------

let evtSource = null;

function connect() {
  if (evtSource) {
    try { evtSource.close(); } catch (e) {}
  }
  const es = new EventSource("/events");
  evtSource = es;
  setConnState(false);
  es.onopen = () => setConnState(true);
  es.onerror = () => {
    setConnState(false);
    try { es.close(); } catch (e) {}
    setTimeout(connect, 2000);
  };
  es.onmessage = (e) => {
    try {
      const state = JSON.parse(e.data);
      onState(state);
    } catch (err) {
      console.error("bad state payload", err);
    }
  };
}

function setConnState(ok) {
  const el = document.getElementById("connstate");
  el.textContent = ok ? "connected" : "disconnected";
  el.className = ok ? "conn-ok" : "conn-bad";
}

// ---------------------------------------------------------------------------
// State -> UI
// ---------------------------------------------------------------------------

function onState(state) {
  latestState = state;
  updateMeta(state);
  updateChar(state);
  updateHunt(state);
  updateFollow(state);
  updateNPC(state);
  updateChat(state);
  updateInv(state);
  ensureMapLoaded(state);
  draw();
}

function updateMeta(s) {
  document.getElementById("meta-map").textContent = "map: " + (s.map || "-");
  if (s.self) {
    document.getElementById("meta-pos").textContent =
      "pos: " + s.self.x + "," + s.self.y;
  }
  if (s.ts) {
    const d = new Date(s.ts * 1000);
    document.getElementById("meta-time").textContent =
      d.toLocaleTimeString();
  }
  document.getElementById("map-name-label").textContent = s.map || "";
}

function updateChar(s) {
  if (!s.self) return;
  const me = s.self;
  setBar("hp-bar", "hp-text", me.hp, me.hp_max);
  setBar("sp-bar", "sp-text", me.sp, me.sp_max);
  setBar("exp-bar", "exp-text", me.exp, me.exp_max);
  setBar("jobexp-bar", "jobexp-text", me.job_exp, me.job_exp_max);
  document.getElementById("char-level").textContent = me.level || 0;
  document.getElementById("char-joblevel").textContent = me.job_level || 0;
  document.getElementById("char-zeny").textContent = (me.zeny || 0).toLocaleString();
  document.getElementById("char-weight").textContent =
    (me.weight || 0) + "/" + (me.weight_max || 0);
  if (me.stats) {
    document.getElementById("stat-str").textContent = me.stats.str || 0;
    document.getElementById("stat-agi").textContent = me.stats.agi || 0;
    document.getElementById("stat-vit").textContent = me.stats.vit || 0;
    document.getElementById("stat-int").textContent = me.stats.int || 0;
    document.getElementById("stat-dex").textContent = me.stats.dex || 0;
    document.getElementById("stat-luk").textContent = me.stats.luk || 0;
  }
  const auto = s.auto || {};
  const parts = [];
  if (auto.attack_target) parts.push("attacking #" + auto.attack_target);
  if (auto.follow_target) parts.push("following #" + auto.follow_target);
  document.getElementById("auto-state").textContent = parts.join(" · ");
}

function setBar(barId, textId, cur, max) {
  cur = cur || 0;
  max = max || 0;
  const pct = max > 0 ? Math.max(0, Math.min(100, (cur / max) * 100)) : 0;
  document.getElementById(barId).style.width = pct + "%";
  document.getElementById(textId).textContent =
    cur.toLocaleString() + " / " + max.toLocaleString();
}

function updateHunt(s) {
  const body = document.getElementById("hunt-body");
  const h = s.hunt;
  if (!h || !h.active) {
    body.className = "";
    body.textContent = "idle";
    return;
  }
  body.className = "active";
  const lines = [];
  lines.push("targets: " + (h.targets || []).join(", "));
  if (h.home) lines.push("home: " + h.home[0] + "," + h.home[1]);
  if (h.radius != null) lines.push("roam radius: " + h.radius);
  if (h.leash != null) lines.push("leash: " + h.leash);
  body.textContent = lines.join("\n");
}

function updateFollow(s) {
  const body = document.getElementById("follow-body");
  if (!body) return;
  const f = s.follow;
  if (!f || !f.active) {
    body.className = "";
    body.textContent = "idle";
    return;
  }
  body.className = "active state-" + (f.state || "idle");
  const lines = [];
  lines.push("target: " + (f.target_name || "?"));
  lines.push("state: " + (f.state || "idle"));
  if (f.target_visible && f.target_pos) {
    lines.push("at: " + f.target_pos[0] + "," + f.target_pos[1]);
  } else if (f.last_seen_pos) {
    lines.push(
      "last seen: " + f.last_seen_pos[0] + "," + f.last_seen_pos[1]
    );
  } else {
    lines.push("(not visible)");
  }
  if (!f.target_visible && f.timeout && f.last_seen_at && s.ts) {
    const remaining = Math.max(
      0,
      Math.round(f.timeout - (s.ts - f.last_seen_at))
    );
    lines.push("timeout in: " + remaining + "s");
  }
  body.textContent = lines.join("\n");
}

function updateNPC(s) {
  const body = document.getElementById("npc-body");
  const d = s.npc_dialog;
  if (!d || (!d.open && !d.lines.length && !d.choices.length && !d.waiting)) {
    body.innerHTML = "<i>no dialog</i>";
    return;
  }
  const parts = [];
  if (d.speaker) {
    parts.push('<div class="speaker">' + escapeHTML(d.speaker) + "</div>");
  }
  for (const line of (d.lines || [])) {
    parts.push('<div class="line">' + escapeHTML(line) + "</div>");
  }
  if (d.choices && d.choices.length) {
    for (let i = 0; i < d.choices.length; i++) {
      parts.push(
        '<div class="choice">[' + (i + 1) + "] " +
        escapeHTML(d.choices[i]) + "</div>"
      );
    }
  }
  if (d.waiting) {
    parts.push('<div class="waiting">waiting: ' + escapeHTML(d.waiting) + "</div>");
  }
  body.innerHTML = parts.join("");
}

function updateChat(s) {
  const list = document.getElementById("chat-list");
  list.innerHTML = "";
  const items = s.chat_recent || [];
  document.getElementById("chat-count").textContent =
    items.length ? "(" + items.length + ")" : "";
  for (const m of items) {
    const li = document.createElement("li");
    const text = m.text || "";
    const kind = m.kind || classifyKindFromText(text);
    li.className = "kind-" + kind;

    if (m.ts) {
      const tsSpan = document.createElement("span");
      tsSpan.className = "ts";
      tsSpan.textContent = formatHMS(m.ts);
      li.appendChild(tsSpan);
    }
    const msgSpan = document.createElement("span");
    msgSpan.className = "msg";
    appendRichText(msgSpan, text);
    li.appendChild(msgSpan);

    list.appendChild(li);
  }
  // Auto-scroll to latest.
  list.scrollTop = list.scrollHeight;
}

function classifyKindFromText(text) {
  if (text.startsWith("[whisper to")) return "whisper_out";
  if (text.startsWith("[whisper")) return "whisper";
  if (text.startsWith("[party]") || text.startsWith("[Party]")) return "party";
  if (text.startsWith("[GM]")) return "gm";
  if (text.startsWith("[Operator]")) return "operator";
  if (text.startsWith("Server :") || text.startsWith("Server:")) return "server";
  if (text.startsWith("#")) return "channel";
  return "say";
}

function formatHMS(ts) {
  const d = new Date(ts * 1000);
  const pad = (n) => (n < 10 ? "0" + n : "" + n);
  return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
}

// Parse Mana's @@url|text@@ inline links AND bare http(s) URLs into a mix
// of text nodes and <a> elements. Greedy match between @@ pairs; text
// portion can include spaces. Bare URLs are matched as a fallback.
const _MANA_LINK_RE = /@@([^@]+?)@@/g;
const _BARE_URL_RE = /(https?:\/\/[^\s<>'"`]+)/g;

function appendRichText(container, raw) {
  // First pass: split on @@...@@ links.
  let lastIndex = 0;
  let m;
  _MANA_LINK_RE.lastIndex = 0;
  while ((m = _MANA_LINK_RE.exec(raw)) !== null) {
    if (m.index > lastIndex) {
      appendBareUrlText(container, raw.slice(lastIndex, m.index));
    }
    const inner = m[1];
    const pipe = inner.indexOf("|");
    let url, label;
    if (pipe >= 0) {
      url = inner.slice(0, pipe).trim();
      label = inner.slice(pipe + 1);
    } else {
      url = inner.trim();
      label = inner;
    }
    if (isSafeHref(url)) {
      const a = document.createElement("a");
      a.href = url;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      a.textContent = label;
      container.appendChild(a);
    } else {
      // Unknown / unsafe scheme: render the raw text as-is.
      container.appendChild(document.createTextNode(m[0]));
    }
    lastIndex = m.index + m[0].length;
  }
  if (lastIndex < raw.length) {
    appendBareUrlText(container, raw.slice(lastIndex));
  }
}

function appendBareUrlText(container, text) {
  let lastIndex = 0;
  let m;
  _BARE_URL_RE.lastIndex = 0;
  while ((m = _BARE_URL_RE.exec(text)) !== null) {
    if (m.index > lastIndex) {
      container.appendChild(
        document.createTextNode(text.slice(lastIndex, m.index))
      );
    }
    const url = m[1];
    if (isSafeHref(url)) {
      const a = document.createElement("a");
      a.href = url;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      a.textContent = url;
      container.appendChild(a);
    } else {
      container.appendChild(document.createTextNode(url));
    }
    lastIndex = m.index + m[0].length;
  }
  if (lastIndex < text.length) {
    container.appendChild(document.createTextNode(text.slice(lastIndex)));
  }
}

function isSafeHref(url) {
  return /^https?:\/\//i.test(url);
}

function updateInv(s) {
  const list = document.getElementById("inv-list");
  list.innerHTML = "";
  const items = s.inventory || [];
  document.getElementById("inv-count").textContent =
    items.length ? "(" + items.length + ")" : "";
  for (const it of items) {
    const li = document.createElement("li");
    if (it.equipped) li.className = "equipped";
    const name = it.item || ("item#" + (it.name_id || 0));
    const label = document.createElement("span");
    label.textContent = "[" + it.slot + "] " + name +
      (it.equipped ? " (equipped)" : "");
    const qty = document.createElement("span");
    qty.className = "qty";
    qty.textContent = "x" + (it.qty || 0);
    li.appendChild(label);
    li.appendChild(qty);
    list.appendChild(li);
  }
}

// ---------------------------------------------------------------------------
// Map collision fetch + canvas render
// ---------------------------------------------------------------------------

function ensureMapLoaded(s) {
  const name = s.map;
  if (!name) return;
  if (mapCache.has(name) || pendingMapFetches.has(name)) return;
  pendingMapFetches.add(name);
  fetch("/map/" + encodeURIComponent(name))
    .then((r) => (r.ok ? r.json() : null))
    .then((data) => {
      pendingMapFetches.delete(name);
      if (data) {
        mapCache.set(name, data);
        draw();
      }
    })
    .catch(() => {
      pendingMapFetches.delete(name);
    });
}

function draw() {
  ctx.fillStyle = "#0a0c10";
  ctx.fillRect(0, 0, canvas.width, canvas.height);

  const s = latestState;
  if (!s || !s.self) return;

  const cx = s.self.x;
  const cy = s.self.y;
  // Top-left tile coords of the viewport.
  const ox = cx - VIEW_RADIUS;
  const oy = cy - VIEW_RADIUS;

  drawTerrain(s, ox, oy);
  drawHuntZone(s, ox, oy);
  drawWalkPath(s, ox, oy);
  drawFollowLink(s, ox, oy);
  drawAggroDiscs(s, ox, oy);
  drawWarpBeings(s, ox, oy);
  drawFloorItems(s, ox, oy);
  drawBeings(s, ox, oy);
  drawSelf(s, ox, oy);
  drawClickFeedback(ox, oy);
  drawHoverLabel(ox, oy);
}

function drawFollowLink(s, ox, oy) {
  const f = s.follow;
  if (!f || !f.active) return;
  // Prefer the live target position; fall back to the last-seen tile so
  // the user still sees a hint of where the bot is heading after a warp.
  const targetPos = f.target_pos || f.last_seen_pos;
  if (!targetPos) return;
  const sx = (s.self.x - ox + 0.5) * TILE_PX;
  const sy2 = (s.self.y - oy + 0.5) * TILE_PX;
  const tx = (targetPos[0] - ox + 0.5) * TILE_PX;
  const ty = (targetPos[1] - oy + 0.5) * TILE_PX;
  // Solid yellow when target is visible, dashed when we're chasing
  // a stale last-seen position (waiting/warping).
  ctx.strokeStyle = f.target_visible
    ? "rgba(241, 196, 15, 0.7)"
    : "rgba(241, 196, 15, 0.45)";
  ctx.lineWidth = 1.5;
  if (!f.target_visible) ctx.setLineDash([4, 3]);
  ctx.beginPath();
  ctx.moveTo(sx, sy2);
  ctx.lineTo(tx, ty);
  ctx.stroke();
  ctx.setLineDash([]);
}

function drawWarpBeings(s, ox, oy) {
  for (const b of (s.beings || [])) {
    if (b.kind !== "warp") continue;
    const dx = b.x - ox;
    const dy = b.y - oy;
    if (dx < 0 || dx >= VIEW_TILES || dy < 0 || dy >= VIEW_TILES) continue;
    // Small translucent magenta square, no label, no HP bar. Drawn
    // BELOW beings so a player standing on a warp tile is still visible.
    ctx.fillStyle = "rgba(216, 112, 214, 0.45)";
    ctx.strokeStyle = "rgba(216, 112, 214, 0.85)";
    ctx.lineWidth = 1;
    const pad = 4;
    ctx.fillRect(
      dx * TILE_PX + pad,
      dy * TILE_PX + pad,
      TILE_PX - 2 * pad,
      TILE_PX - 2 * pad
    );
    ctx.strokeRect(
      dx * TILE_PX + pad,
      dy * TILE_PX + pad,
      TILE_PX - 2 * pad,
      TILE_PX - 2 * pad
    );
  }
}

function drawClickFeedback(ox, oy) {
  const now = performance.now();
  const lifetime = 400; // ms
  clickRipples = clickRipples.filter((r) => now - r.t0 < lifetime);
  attackHighlights = attackHighlights.filter((h) => now - h.t0 < lifetime);
  for (const r of clickRipples) {
    const age = (now - r.t0) / lifetime;
    const dx = r.x - ox;
    const dy = r.y - oy;
    const cx = (dx + 0.5) * TILE_PX;
    const cy = (dy + 0.5) * TILE_PX;
    ctx.strokeStyle = `rgba(241, 196, 15, ${1 - age})`;
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.arc(cx, cy, age * TILE_PX * 1.2 + 2, 0, Math.PI * 2);
    ctx.stroke();
  }
  if (!latestState) return;
  const beingById = new Map();
  for (const b of (latestState.beings || [])) beingById.set(b.id, b);
  for (const h of attackHighlights) {
    const b = beingById.get(h.id);
    if (!b) continue;
    const dx = b.x - ox;
    const dy = b.y - oy;
    if (dx < 0 || dx >= VIEW_TILES || dy < 0 || dy >= VIEW_TILES) continue;
    const age = (now - h.t0) / lifetime;
    const cx = (dx + 0.5) * TILE_PX;
    const cy = (dy + 0.5) * TILE_PX;
    ctx.strokeStyle = `rgba(255, 143, 177, ${1 - age})`;
    ctx.lineWidth = 3;
    ctx.beginPath();
    ctx.arc(cx, cy, TILE_PX / 2 + 2, 0, Math.PI * 2);
    ctx.stroke();
  }
  if (clickRipples.length || attackHighlights.length) {
    requestAnimationFrame(() => draw());
  }
}

function drawTerrain(s, ox, oy) {
  const cmap = mapCache.get(s.map);
  if (!cmap) {
    ctx.fillStyle = "#1a1d24";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    return;
  }
  for (let dy = 0; dy < VIEW_TILES; dy++) {
    for (let dx = 0; dx < VIEW_TILES; dx++) {
      const tx = ox + dx;
      const ty = oy + dy;
      let walkable = false;
      if (tx >= 0 && tx < cmap.width && ty >= 0 && ty < cmap.height) {
        walkable = cmap.walkable[ty * cmap.width + tx] === "1";
      }
      ctx.fillStyle = walkable ? "#2a2f38" : "#11131a";
      ctx.fillRect(dx * TILE_PX, dy * TILE_PX, TILE_PX, TILE_PX);
    }
  }
  // Warp zones (subtle blue tint).
  if (cmap.warps) {
    ctx.fillStyle = "rgba(93, 173, 226, 0.15)";
    for (const w of cmap.warps) {
      const [wx, wy, ww, wh] = w;
      for (let yy = wy; yy < wy + wh; yy++) {
        for (let xx = wx; xx < wx + ww; xx++) {
          const dx = xx - ox;
          const dy = yy - oy;
          if (dx >= 0 && dx < VIEW_TILES && dy >= 0 && dy < VIEW_TILES) {
            ctx.fillRect(dx * TILE_PX, dy * TILE_PX, TILE_PX, TILE_PX);
          }
        }
      }
    }
  }
}

function drawHuntZone(s, ox, oy) {
  const h = s.hunt;
  if (!h || !h.active || !h.home) return;
  const hx = h.home[0] - ox;
  const hy = h.home[1] - oy;
  const r = h.radius || 8;
  ctx.strokeStyle = "rgba(46, 204, 113, 0.6)";
  ctx.lineWidth = 1;
  ctx.setLineDash([3, 3]);
  ctx.strokeRect(
    (hx - r) * TILE_PX,
    (hy - r) * TILE_PX,
    (2 * r + 1) * TILE_PX,
    (2 * r + 1) * TILE_PX
  );
  ctx.setLineDash([]);
  // Home marker.
  ctx.fillStyle = "rgba(46, 204, 113, 0.7)";
  ctx.fillRect(hx * TILE_PX + 4, hy * TILE_PX + 4, TILE_PX - 8, TILE_PX - 8);
}

function drawWalkPath(s, ox, oy) {
  const path = s.walk_path || [];
  if (path.length < 2) return;
  ctx.strokeStyle = "#f1c40f";
  ctx.lineWidth = 2;
  ctx.beginPath();
  for (let i = 0; i < path.length; i++) {
    const px = (path[i][0] - ox + 0.5) * TILE_PX;
    const py = (path[i][1] - oy + 0.5) * TILE_PX;
    if (i === 0) ctx.moveTo(px, py);
    else ctx.lineTo(px, py);
  }
  ctx.stroke();
  // Final waypoint marker.
  const last = path[path.length - 1];
  ctx.fillStyle = "rgba(241, 196, 15, 0.7)";
  ctx.fillRect(
    (last[0] - ox) * TILE_PX + 4,
    (last[1] - oy) * TILE_PX + 4,
    TILE_PX - 8, TILE_PX - 8
  );
}

function drawAggroDiscs(s, ox, oy) {
  for (const b of (s.beings || [])) {
    if (b.kind !== "monster") continue;
    const range = b.aggro_range;
    if (!range || range <= 0) continue;
    const bx = (b.x - ox + 0.5) * TILE_PX;
    const by = (b.y - oy + 0.5) * TILE_PX;
    ctx.fillStyle = "rgba(231, 76, 60, 0.15)";
    ctx.strokeStyle = "rgba(231, 76, 60, 0.4)";
    ctx.beginPath();
    ctx.arc(bx, by, range * TILE_PX, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();
  }
}

function drawFloorItems(s, ox, oy) {
  ctx.fillStyle = "#f1c40f";
  for (const it of (s.floor_items || [])) {
    const dx = it.x - ox;
    const dy = it.y - oy;
    if (dx < 0 || dx >= VIEW_TILES || dy < 0 || dy >= VIEW_TILES) continue;
    ctx.fillRect(
      dx * TILE_PX + TILE_PX / 2 - 3,
      dy * TILE_PX + TILE_PX / 2 - 3,
      6, 6
    );
  }
}

function drawBeings(s, ox, oy) {
  for (const b of (s.beings || [])) {
    if (b.kind === "warp") continue;
    const dx = b.x - ox;
    const dy = b.y - oy;
    if (dx < 0 || dx >= VIEW_TILES || dy < 0 || dy >= VIEW_TILES) continue;
    let color = "#bbbbbb";
    if (b.kind === "monster") color = "#e74c3c";
    else if (b.kind === "player") color = "#5dade2";
    else if (b.kind === "npc") color = "#27ae60";
    const cx = (dx + 0.5) * TILE_PX;
    const cy = (dy + 0.5) * TILE_PX;
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(cx, cy, TILE_PX / 2 - 2, 0, Math.PI * 2);
    ctx.fill();
    // HP bar over monsters with known HP.
    if (b.hp_max && b.hp_max > 0) {
      const pct = Math.max(0, Math.min(1, b.hp / b.hp_max));
      ctx.fillStyle = "#11131a";
      ctx.fillRect(dx * TILE_PX + 2, dy * TILE_PX - 3, TILE_PX - 4, 2);
      ctx.fillStyle = pct > 0.5 ? "#2ecc71" : pct > 0.2 ? "#f39c12" : "#e74c3c";
      ctx.fillRect(dx * TILE_PX + 2, dy * TILE_PX - 3, (TILE_PX - 4) * pct, 2);
    }
    // Name label (small).
    if (b.name) {
      ctx.fillStyle = "rgba(0, 0, 0, 0.6)";
      const label = b.name;
      ctx.font = "9px system-ui, sans-serif";
      const w = ctx.measureText(label).width;
      ctx.fillRect(cx - w / 2 - 2, dy * TILE_PX + TILE_PX + 1, w + 4, 10);
      ctx.fillStyle = "#e0e0e0";
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      ctx.fillText(label, cx, dy * TILE_PX + TILE_PX + 2);
    }
  }
}

function drawSelf(s, ox, oy) {
  const dx = s.self.x - ox;
  const dy = s.self.y - oy;
  const cx = (dx + 0.5) * TILE_PX;
  const cy = (dy + 0.5) * TILE_PX;
  // Yellow ring + dot.
  ctx.strokeStyle = "#f1c40f";
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.arc(cx, cy, TILE_PX / 2 - 1, 0, Math.PI * 2);
  ctx.stroke();
  ctx.fillStyle = "#f1c40f";
  ctx.beginPath();
  ctx.arc(cx, cy, 3, 0, Math.PI * 2);
  ctx.fill();
}

function drawHoverLabel(ox, oy) {
  if (!hoverTile) {
    document.getElementById("hover-info").innerHTML = "&nbsp;";
    return;
  }
  const s = latestState;
  if (!s) return;
  const wx = ox + hoverTile.x;
  const wy = oy + hoverTile.y;
  let label = "tile (" + wx + "," + wy + ")";
  // Identify what's on this tile.
  for (const b of (s.beings || [])) {
    if (b.x === wx && b.y === wy) {
      const hp = b.hp_max
        ? " hp " + b.hp + "/" + b.hp_max
        : "";
      label += " - " + (b.name || b.kind) + " #" + b.id + hp;
      break;
    }
  }
  for (const it of (s.floor_items || [])) {
    if (it.x === wx && it.y === wy) {
      label += " - item " + (it.item || ("#" + it.name_id)) + " x" + it.amount;
      break;
    }
  }
  document.getElementById("hover-info").textContent = label;
}

canvas.addEventListener("mousemove", (e) => {
  const rect = canvas.getBoundingClientRect();
  const scaleX = canvas.width / rect.width;
  const scaleY = canvas.height / rect.height;
  const px = (e.clientX - rect.left) * scaleX;
  const py = (e.clientY - rect.top) * scaleY;
  const tx = Math.floor(px / TILE_PX);
  const ty = Math.floor(py / TILE_PX);
  if (tx >= 0 && tx < VIEW_TILES && ty >= 0 && ty < VIEW_TILES) {
    hoverTile = { x: tx, y: ty };
  } else {
    hoverTile = null;
  }
  draw();
});

canvas.addEventListener("mouseleave", () => {
  hoverTile = null;
  draw();
});

// ---------------------------------------------------------------------------
// Operator controls: click-to-walk, click-to-attack, chat input
// ---------------------------------------------------------------------------

canvas.addEventListener("contextmenu", (e) => {
  e.preventDefault();
});

canvas.addEventListener("click", (e) => {
  // Shift-click reserved for future use; ignore for now.
  if (e.shiftKey) return;
  const s = latestState;
  if (!s || !s.self) return;
  const rect = canvas.getBoundingClientRect();
  const scaleX = canvas.width / rect.width;
  const scaleY = canvas.height / rect.height;
  const px = (e.clientX - rect.left) * scaleX;
  const py = (e.clientY - rect.top) * scaleY;
  if (px < 0 || py < 0 || px >= canvas.width || py >= canvas.height) return;
  const ox = s.self.x - VIEW_RADIUS;
  const oy = s.self.y - VIEW_RADIUS;
  const tx = Math.floor(px / TILE_PX);
  const ty = Math.floor(py / TILE_PX);
  const worldX = ox + tx;
  const worldY = oy + ty;

  // Pick the topmost being within roughly half a tile of the click.
  // Skip warps (purely decorative) and our own avatar.
  const tileCx = (tx + 0.5) * TILE_PX;
  const tileCy = (ty + 0.5) * TILE_PX;
  let bestBeing = null;
  let bestDist = TILE_PX * 0.55;
  for (const b of (s.beings || [])) {
    if (b.kind === "warp") continue;
    const dx = b.x - ox;
    const dy = b.y - oy;
    if (dx < 0 || dx >= VIEW_TILES || dy < 0 || dy >= VIEW_TILES) continue;
    const bcx = (dx + 0.5) * TILE_PX;
    const bcy = (dy + 0.5) * TILE_PX;
    const d = Math.hypot(bcx - px, bcy - py);
    if (d <= bestDist) {
      bestDist = d;
      bestBeing = b;
    }
  }

  if (bestBeing && (bestBeing.kind === "monster" || bestBeing.kind === "npc" ||
                    bestBeing.kind === "player")) {
    attackHighlights.push({ id: bestBeing.id, t0: performance.now() });
    draw();
    postJSON("/op/attack", { being_id: bestBeing.id })
      .catch((err) => console.error("op/attack failed", err));
  } else {
    clickRipples.push({ x: worldX, y: worldY, t0: performance.now() });
    draw();
    postJSON("/op/walk", { x: worldX, y: worldY })
      .catch((err) => console.error("op/walk failed", err));
  }
});

function postJSON(path, body) {
  return fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }).then((r) => {
    if (!r.ok) {
      return r.text().then((t) => {
        throw new Error("HTTP " + r.status + ": " + t);
      });
    }
    return r.json().catch(() => ({}));
  });
}

const opForm = document.getElementById("op-form");
const opInput = document.getElementById("op-input");
if (opForm && opInput) {
  opForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = opInput.value.trim();
    if (!text) return;
    opInput.disabled = true;
    postJSON("/op/say", { text: text })
      .then(() => {
        opInput.value = "";
        opInput.classList.remove("flash-error");
      })
      .catch((err) => {
        console.error("op/say failed", err);
        opInput.classList.add("flash-error");
        setTimeout(() => opInput.classList.remove("flash-error"), 600);
      })
      .finally(() => {
        opInput.disabled = false;
        opInput.focus();
      });
  });
}

// ---------------------------------------------------------------------------
// Map/chat splitter: drag to resize chat panel, persisted in localStorage.
// ---------------------------------------------------------------------------

const SPLIT_STORAGE_KEY = "tmw_dashboard_split_chat_px";
const CHAT_MIN_PX = 120;
const MAP_MIN_PX = 200;

const splitter = document.getElementById("map-chat-splitter");
const chatPanel = document.getElementById("chat-panel");
const mapSection = document.getElementById("map-section");

function clampChatHeight(px) {
  if (!mapSection) return px;
  const sectionH = mapSection.clientHeight;
  const splitterH = splitter ? splitter.offsetHeight : 0;
  const maxChat = Math.max(CHAT_MIN_PX, sectionH - splitterH - MAP_MIN_PX);
  if (px < CHAT_MIN_PX) px = CHAT_MIN_PX;
  if (px > maxChat) px = maxChat;
  return Math.round(px);
}

function applyChatHeight(px) {
  if (!chatPanel) return;
  const clamped = clampChatHeight(px);
  chatPanel.style.flex = "0 0 " + clamped + "px";
}

function loadSavedChatHeight() {
  try {
    const raw = localStorage.getItem(SPLIT_STORAGE_KEY);
    if (raw == null) return;
    const n = parseFloat(raw);
    if (!isFinite(n) || n <= 0) return;
    applyChatHeight(n);
  } catch (e) {
    // localStorage may be disabled; ignore.
  }
}

if (splitter && chatPanel && mapSection) {
  let dragging = false;
  let pointerId = null;

  splitter.addEventListener("pointerdown", (e) => {
    // Left button / primary pointer only.
    if (e.button !== undefined && e.button !== 0) return;
    dragging = true;
    pointerId = e.pointerId;
    splitter.classList.add("dragging");
    try { splitter.setPointerCapture(e.pointerId); } catch (err) {}
    e.preventDefault();
  });

  splitter.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    const rect = mapSection.getBoundingClientRect();
    // Chat sits below the splitter; its top edge is approximately the
    // pointer Y. New chat height is the distance from there to the
    // bottom of the section.
    const desired = rect.bottom - e.clientY - splitter.offsetHeight / 2;
    applyChatHeight(desired);
  });

  const endDrag = (e) => {
    if (!dragging) return;
    dragging = false;
    splitter.classList.remove("dragging");
    if (pointerId != null) {
      try { splitter.releasePointerCapture(pointerId); } catch (err) {}
      pointerId = null;
    }
    // Persist final size.
    try {
      const h = chatPanel.getBoundingClientRect().height;
      localStorage.setItem(SPLIT_STORAGE_KEY, String(Math.round(h)));
    } catch (err) {}
  };
  splitter.addEventListener("pointerup", endDrag);
  splitter.addEventListener("pointercancel", endDrag);

  // Reclamp on window resize so a previously-valid size remains valid.
  window.addEventListener("resize", () => {
    if (!chatPanel) return;
    const cur = chatPanel.getBoundingClientRect().height;
    applyChatHeight(cur);
  });

  loadSavedChatHeight();
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function escapeHTML(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[c]);
}

connect();
