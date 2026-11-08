"""A local web page for deciding conflicts (``ocrbook triage BOOK``, then open the URL).

For each conflict: keep the primary reading (the default), take the other source's, type a
correction, or drop the passage. Decisions are appended to ``work/decisions.jsonl``; a later
line for the same conflict overrides an earlier one, so nothing is ever rewritten.
``ocrbook assemble`` applies them. Standard library only; it listens on 127.0.0.1.
"""
from __future__ import annotations

import html
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ocrbook import reconcile
from ocrbook.profile import Book

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>__TITLE__ — Conflict Triage</title>
<style>
  body { font: 14px/1.4 -apple-system, system-ui, sans-serif; margin: 0; background: #fafaf7; color: #222; }
  header { position: sticky; top: 0; background: #fff; border-bottom: 1px solid #ddd; padding: 8px 16px; z-index: 10; }
  header form { display: flex; flex-wrap: wrap; gap: 8px 16px; align-items: center; }
  header label { font-size: 12px; color: #555; }
  header input[type=number], header select { font: inherit; padding: 2px 6px; }
  #counts { margin-left: auto; font-size: 12px; color: #555; }
  main { padding: 12px 16px 100px; }
  .row { display: grid; grid-template-columns: 56px 1fr 280px; gap: 12px; padding: 10px 12px; border-bottom: 1px solid #eee; align-items: start; }
  .row.resolved { background: #f3f8f3; }
  .row.resolved .reading.chosen { background: #cfe7c8; }
  .pg { font-weight: 600; color: #888; font-variant-numeric: tabular-nums; }
  .ctx { color: #888; }
  .reading { display: inline-block; padding: 1px 4px; border-radius: 3px; }
  .reading.primary { background: #fde8c8; }
  .reading.secondary { background: #c8dafd; }
  .vs { color: #666; padding: 0 4px; font-size: 12px; }
  .meta { font-size: 11px; color: #999; margin-top: 4px; }
  .actions { display: flex; flex-direction: column; gap: 4px; }
  .actions .btn-row { display: flex; gap: 4px; }
  button { font: inherit; padding: 3px 8px; border: 1px solid #bbb; background: #fff; border-radius: 4px; cursor: pointer; }
  button:hover { background: #f0f0f0; }
  button.active { background: #333; color: #fff; border-color: #333; }
  input.custom { font: inherit; width: 100%; padding: 3px 6px; box-sizing: border-box; }
  #empty { padding: 40px; text-align: center; color: #888; }
</style>
</head>
<body>
<header>
  <form id="filters" onsubmit="event.preventDefault(); load();">
    <label>page <input type="number" id="page_min" min="1" style="width:60px" value="__PAGE_MIN__">–<input type="number" id="page_max" min="1" style="width:60px"></label>
    <label>kind
      <select id="kind">
        <option value="">any</option>
        <option value="replace">replace</option>
        <option value="insert">insert</option>
        <option value="delete">delete</option>
      </select>
    </label>
    <label>len <input type="number" id="min_len" min="0" style="width:50px" value="3">–<input type="number" id="max_len" style="width:50px" value="30"></label>
    <label><input type="checkbox" id="unresolved_only" checked> unresolved only</label>
    <label><input type="checkbox" id="alpha_only" checked> alpha-heavy only</label>
    <label>limit <input type="number" id="limit" style="width:60px" value="100"></label>
    <button type="submit">Apply</button>
    <span id="counts"></span>
  </form>
</header>
<main id="list"></main>
<div id="empty" style="display:none">No conflicts match the current filter.</div>
<script>
let conflicts = [];
let decisions = {};

async function load() {
  const params = new URLSearchParams({
    page_min: document.getElementById('page_min').value || '',
    page_max: document.getElementById('page_max').value || '',
    kind: document.getElementById('kind').value,
    min_len: document.getElementById('min_len').value || '',
    max_len: document.getElementById('max_len').value || '',
    unresolved_only: document.getElementById('unresolved_only').checked ? '1' : '',
    alpha_only: document.getElementById('alpha_only').checked ? '1' : '',
    limit: document.getElementById('limit').value || '100',
  });
  const r = await fetch('/api/conflicts?' + params);
  const data = await r.json();
  conflicts = data.conflicts;
  decisions = data.decisions;
  showCounts(data);
  render();
}

let lastCounts = null;
function showCounts(data) {
  if (data) lastCounts = data;
  const d = lastCounts;
  document.getElementById('counts').textContent =
    `${d.shown}/${d.matched} shown · ${d.total} total · ${Object.keys(decisions).length} decided`;
}

function escape(s) { return s.replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

function render() {
  const list = document.getElementById('list');
  list.innerHTML = '';
  document.getElementById('empty').style.display = conflicts.length ? 'none' : 'block';
  for (const c of conflicts) {
    const dec = decisions[c.id];
    const primary = c.readings[c.picked_from] || '';
    const secondaryKey = Object.keys(c.readings).find(k => k !== c.picked_from) || '';
    const secondary = c.readings[secondaryKey] || '';
    const row = document.createElement('div');
    row.className = 'row' + (dec ? ' resolved' : '');
    const isPrimary = !dec || dec.value === primary;
    const isSecondary = dec && dec.value === secondary;
    const isCustom = dec && !isPrimary && !isSecondary && dec.value !== '';
    const isDrop = dec && dec.value === '';
    row.innerHTML = `
      <div class="pg">p${c.page}<br><span style="font-size:11px;color:#aaa">${c.kind}</span></div>
      <div>
        <span class="ctx">${escape(c.context_before)}</span>
        <span class="reading primary ${isPrimary ? 'chosen' : ''}" title="${escape(c.picked_from)}">${escape(primary) || '∅'}</span>
        <span class="vs">⇄</span>
        <span class="reading secondary ${isSecondary ? 'chosen' : ''}" title="${escape(secondaryKey)}">${escape(secondary) || '∅'}</span>
        <span class="ctx">${escape(c.context_after)}</span>
        <div class="meta">id=${c.id}${isCustom ? ' · custom: ' + escape(dec.value) : ''}</div>
      </div>
      <div class="actions">
        <div class="btn-row">
          <button data-action="primary" class="${isPrimary ? 'active' : ''}">keep ${escape(c.picked_from)}</button>
          <button data-action="secondary" class="${isSecondary ? 'active' : ''}">use ${escape(secondaryKey)}</button>
          <button data-action="drop" class="${isDrop ? 'active' : ''}">drop</button>
        </div>
        <div class="btn-row">
          <input class="custom" placeholder="custom edit (enter to save)" value="${isCustom ? escape(dec.value) : ''}">
        </div>
      </div>`;
    row.querySelector('[data-action=primary]').onclick = () => decide(c, primary);
    row.querySelector('[data-action=secondary]').onclick = () => decide(c, secondary);
    row.querySelector('[data-action=drop]').onclick = () => decide(c, '');
    const custom = row.querySelector('input.custom');
    custom.addEventListener('keydown', e => {
      if (e.key === 'Enter') { e.preventDefault(); decide(c, custom.value); }
    });
    list.appendChild(row);
  }
}

async function decide(c, value) {
  const r = await fetch('/api/decision', {
    method: 'POST',
    headers: {'content-type': 'application/json'},
    body: JSON.stringify({id: c.id, value}),
  });
  if (!r.ok) { alert('save failed: ' + await r.text()); return; }
  decisions[c.id] = {value};
  if (document.getElementById('unresolved_only').checked) {
    conflicts = conflicts.filter(x => x.id !== c.id);
  }
  render();
  showCounts();
}

load();
</script>
</body>
</html>
"""


def _int(query: dict, key: str) -> int | None:
    try:
        return int(query.get(key, [""])[0])
    except ValueError:
        return None


def _alpha_heavy(s: str) -> bool:
    return bool(s) and sum(ch.isalpha() for ch in s) / len(s) >= 0.6


class Triage:
    """The conflicts, the decisions so far, and the append-only decisions file."""

    def __init__(self, conflicts_path: Path, decisions_path: Path):
        self.conflicts = reconcile.load_conflicts(conflicts_path)
        self.ids = {c["id"] for c in self.conflicts}
        self.decisions_path = decisions_path
        self.decisions = reconcile.load_decisions(decisions_path)
        self.lock = threading.Lock()

    def select(self, query: dict) -> dict:
        page_min, page_max = _int(query, "page_min"), _int(query, "page_max")
        kind = query.get("kind", [""])[0]
        min_len, max_len = _int(query, "min_len"), _int(query, "max_len")
        unresolved = query.get("unresolved_only", [""])[0] == "1"
        alpha = query.get("alpha_only", [""])[0] == "1"
        limit = _int(query, "limit") or 100
        matched = []
        for c in self.conflicts:
            if page_min is not None and c["page"] < page_min:
                continue
            if page_max is not None and c["page"] > page_max:
                continue
            if kind and c["kind"] != kind:
                continue
            primary = c["readings"].get(c["picked_from"], "")
            secondary = next((v for k, v in c["readings"].items() if k != c["picked_from"]), "")
            longest = max(len(primary), len(secondary))
            if min_len is not None and longest < min_len:
                continue
            if max_len is not None and longest > max_len:
                continue
            if alpha and not (_alpha_heavy(primary) or _alpha_heavy(secondary)):
                continue
            if unresolved and c["id"] in self.decisions:
                continue
            matched.append(c)
        return {"total": len(self.conflicts), "matched": len(matched),
                "shown": min(limit, len(matched)), "conflicts": matched[:limit],
                "decisions": self.decisions}

    def decide(self, cid, value) -> str:
        """Record a decision; returns an error message, or "" when saved."""
        if not isinstance(cid, str) or not isinstance(value, str):
            return "id and value must be strings"
        if cid not in self.ids:
            return "unknown conflict id %s" % cid
        record = {"id": cid, "value": value}
        with self.lock:
            self.decisions_path.parent.mkdir(parents=True, exist_ok=True)
            with self.decisions_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            self.decisions[cid] = record
        return ""


def make_server(book: Book, host: str = "127.0.0.1", port: int = 5050) -> ThreadingHTTPServer:
    conflicts_path = book.work_dir / "conflicts.jsonl"
    if not conflicts_path.exists():
        raise FileNotFoundError("%s not found; run `ocrbook reconcile %s`"
                                % (conflicts_path, book.name))
    state = Triage(conflicts_path, book.work_dir / "decisions.jsonl")
    page = (PAGE.replace("__TITLE__", html.escape(book.title))
                .replace("__PAGE_MIN__", str(book.triage_page_min)).encode("utf-8"))

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: bytes, kind: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, data) -> None:
            self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == "/":
                self._send(200, page, "text/html; charset=utf-8")
            elif url.path == "/api/conflicts":
                self._json(200, state.select(parse_qs(url.query, keep_blank_values=True)))
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if urlparse(self.path).path != "/api/decision":
                self._send(404, b"not found", "text/plain")
                return
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            except ValueError:
                body = None
            if not isinstance(body, dict):
                self._send(400, b"bad request", "text/plain")
                return
            problem = state.decide(body.get("id"), body.get("value", ""))
            if problem:
                self._send(400, problem.encode("utf-8"), "text/plain; charset=utf-8")
            else:
                self._json(200, {"ok": True})

        def log_message(self, format, *args):  # quiet
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    server.triage = state  # type: ignore[attr-defined]
    return server


def serve(book: Book, host: str = "127.0.0.1", port: int = 5050, *, log=print) -> None:
    server = make_server(book, host, port)
    state = server.triage  # type: ignore[attr-defined]
    log("loaded %d conflicts, %d prior decisions" % (len(state.conflicts), len(state.decisions)))
    log("open http://%s:%d in your browser (Ctrl-C to stop)" % (host, server.server_port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
