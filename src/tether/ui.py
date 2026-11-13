"""The live view: a small local web page (editable) and a terminal dashboard (read-only).

The server listens on 127.0.0.1 only. Every write needs the random token that was put
into the page when it was served, and the Host header must be local, so other websites
(CSRF, DNS rebinding) can't make it write.
"""

from __future__ import annotations

import json
import secrets as pysecrets
import threading
import time
import webbrowser
from importlib import resources
from urllib.parse import parse_qs, urlparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tether.render import ago
from tether.store import Store, TetherError

PORT = 7878


def snapshot(store: Store) -> dict:
    projects = []
    for p in store.projects():
        pid = p["id"]
        open_tasks = store.tasks(pid)
        done = store.db.execute(
            "SELECT * FROM tasks WHERE project_id=? AND kind='project' AND status='done' "
            "ORDER BY closed_at DESC LIMIT 15", (pid,)).fetchall()
        counts = store.task_counts(pid)
        projects.append({
            "id": pid, "slug": p["slug"], "name": p["name"], "goal": p["goal"], "status": p["status"],
            "updated": ago(p["updated_at"]), "root": p["root_path"],
            "last_active": p["last_active"], "last_active_ago": ago(p["last_active"]),
            "code_doing": p["code_doing"], "open": p["open_tasks"],
            "done_count": counts["done"],
            "tasks": [_task(t) for t in [*open_tasks, *done]],
            "code_tasks": [_task(t) for t in store.recent_session_tasks(pid, 10)],
            "handoffs": [{"id": h["id"], "title": h["title"], "to": h["to_surface"],
                          "from": h["from_surface"], "status": h["status"],
                          "body": h["body"], "ago": ago(h["created_at"])}
                         for h in store.handoffs(pid)],
            "decisions": [{"id": d["id"], "text": d["text"], "why": d["why"],
                           "surface": d["surface"], "ago": ago(d["created_at"])}
                          for d in store.decisions(pid, limit=12)],
            "ideas": [_mem(m, store) for m in store.memories(pid=pid, limit=30)],
            "events": [{"id": e["id"], "ts": e["ts"], "ago": ago(e["ts"]),
                        "surface": e["surface"], "text": e["summary"], "kind": e["kind"],
                        "ref": e["ref"], "project": p["slug"], "machine": e["machine"]}
                       for e in sorted(store.events(pid, limit=60), key=lambda e: (e["ts"], e["id"]),
                                       reverse=True)  # by time: other Macs' ids interleave
                       if e["kind"] != "session-task"][:30],
        })
    box = store.inbox(limit=60)
    sync_info = None
    if store.syncer:
        st = store.syncer.status()
        sync_info = {"machine": st["machine"], "error": store.sync_error,
                     "peers": [{"machine": p["machine"], "last": p["last_change"]}
                               for p in st["peers"]]}
    return {"projects": projects, "sync": sync_info,
            "inbox": [_mem(m, store) for m in box["fresh"]],
            "older": [_mem(m, store) for m in box["older"]],
            "marker": list(store.change_marker())}


def _task(t) -> dict:
    return {"id": t["id"], "title": t["title"], "status": t["status"], "for": t["for_surface"],
            "priority": t["priority"], "note": t["note"], "detail": t["detail"],
            "ago": ago(t["updated_at"]), "kind": t["kind"]}


def _mem(m, store: Store) -> dict:
    d = {"id": m["id"], "title": m["title"], "kind": m["kind"], "tags": m["tags"],
         "summary": m["summary"], "body": m["body"], "ago": ago(m["updated_at"]),
         "surface": m["surface"],
         "project": m["project_slug"] if "project_slug" in m.keys() else None, "guess": None}
    if not d["project"]:
        g = store.suggest_project(m)
        d["guess"] = g[0]["slug"] if g else None
    return d


def search(store: Store, q: str) -> list[dict]:
    out = []
    for kind, r, slug in store.recall(q, limit=25):
        if kind == "memories":
            out.append({"type": "idea", "id": r["id"], "title": r["title"], "project": slug})
        elif kind == "projects":
            out.append({"type": "project", "title": r["name"], "project": slug})
        elif kind == "tasks":
            out.append({"type": "task", "id": r["id"], "title": r["title"], "project": slug,
                        "status": r["status"]})
        elif kind == "decisions":
            out.append({"type": "decision", "id": r["id"], "title": r["text"], "project": slug})
        elif kind == "handoffs":
            out.append({"type": "handoff", "id": r["id"], "title": r["title"], "project": slug})
    return out


def act(store: Store, req: dict) -> str:
    """Apply one edit from the page. Uses the same Store methods as the CLI and MCP."""
    kind = req.get("action")
    sf = "cli"
    pid = lambda: (store.find_project(req.get("project", "")) or _bad("unknown project"))["id"]  # noqa: E731
    if kind == "task_status":
        store.update_task(int(req["id"]), status=req["status"], surface=sf)
    elif kind == "edit_task":
        store.update_task(int(req["id"]), title=req.get("title"), for_surface=req.get("for"),
                          priority=req.get("priority"), surface=sf)
    elif kind == "add_task":
        store.add_task(pid(), req["title"], req.get("detail", ""), req.get("for", "any"),
                       int(req.get("priority", 2)), sf)
    elif kind == "promote":
        store.promote_task(int(req["id"]), sf)
    elif kind == "attach":
        store.attach_memory(int(req["id"]), pid(), sf)
    elif kind == "archive_memory":
        store.archive_memory(int(req["id"]), bool(req.get("archived", True)))
    elif kind == "edit_memory":
        store.update_memory(int(req["id"]), title=req.get("title"), body=req.get("body"),
                            summary=req.get("summary"), surface=sf)
    elif kind == "add_memory":
        store.add_memory(req["title"], req.get("body", ""), req.get("tags", ""), sf,
                         pid() if req.get("project") else None, req.get("kind", "idea"))
    elif kind == "handoff_status":
        store.update_handoff(int(req["id"]), req["status"], surface=sf)
    elif kind == "decide":
        store.add_decision(pid(), req["text"], req.get("why", ""), sf)
    elif kind == "set_status":
        store.update_project(pid(), status=req["text"], surface=sf)
    elif kind == "set_goal":
        store.update_project(pid(), goal=req["text"], surface=sf)
    else:
        _bad(f"unknown action {kind!r}")
    return "ok"


def _bad(msg: str):
    raise TetherError(msg)


def page(token: str) -> bytes:
    html = resources.files("tether").joinpath("static/index.html").read_text()
    return html.replace("__TOKEN__", token).encode()


def already_running(port: int = PORT) -> bool:
    """True if a Tether live view already answers on this port."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/marker", timeout=0.5) as r:
            return r.status == 200 and r.read(1) == b"["
    except OSError:
        return False


def serve(port: int = PORT, open_browser: bool = True) -> None:
    store = Store()
    lock = threading.Lock()
    token = pysecrets.token_urlsafe(24)
    body = page(token)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _local(self) -> bool:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            return host in ("127.0.0.1", "localhost")

        def _send(self, code: int, data: bytes, ctype: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            if ctype.startswith("text/html"):
                self.send_header("Content-Security-Policy",
                                 "default-src 'self'; script-src 'unsafe-inline'; "
                                 "style-src 'unsafe-inline'; img-src 'self' data:; "
                                 "connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self._local():
                return self._send(403, b"{}")
            url = urlparse(self.path)
            if url.path in ("/", "/index.html"):
                return self._send(200, body, "text/html; charset=utf-8")
            with lock:
                if url.path == "/api/marker":
                    store.pull(force=False)  # other Macs' changes, at most every few seconds
                    return self._send(200, json.dumps(store.change_marker()).encode())
                if url.path == "/api/state":
                    return self._send(200, json.dumps(snapshot(store)).encode())
                if url.path == "/api/search":
                    q = (parse_qs(url.query).get("q") or [""])[0]
                    try:
                        res = search(store, q) if q.strip() else []
                    except TetherError:
                        res = []
                    return self._send(200, json.dumps(res).encode())
            self._send(404, b"{}")

        def do_POST(self):
            if not self._local() or self.headers.get("X-Tether-Token") != token:
                return self._send(403, b'{"error":"forbidden"}')
            if self.path != "/api/act":
                return self._send(404, b"{}")
            try:
                n = int(self.headers.get("Content-Length") or 0)
                req = json.loads(self.rfile.read(min(n, 200_000)) or b"{}")
                with lock:
                    act(store, req)
                self._send(200, b'{"ok":true}')
            except (TetherError, KeyError, ValueError, TypeError) as e:
                self._send(400, json.dumps({"error": str(e)}).encode())

    httpd = ThreadingHTTPServer(("127.0.0.1", port), H)
    url = f"http://127.0.0.1:{port}"
    print(f"Tether live view: {url}  (Ctrl-C to stop)", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


# -- terminal dashboard ----------------------------------------------------------------

def render_watch(snap: dict, only: str | None = None) -> str:
    scope = f"focused on {only}" if only else "all active projects"
    L = [f"\033[1mTether\033[0m live — {scope} — Ctrl-C to quit", ""]
    for p in snap["projects"]:
        if only and p["slug"] != only:
            continue
        open_n = sum(1 for t in p["tasks"] if t["status"] in ("todo", "doing", "blocked"))
        L.append(f"\033[1m{p['slug']}\033[0m  {p['status'] or p['goal']}  ({open_n} open)")
        for t in p["code_tasks"][:5]:
            mark = {"doing": "▶", "done": "✓", "todo": "·"}.get(t["status"], "·")
            L.append(f"   code {mark} {t['title'][:70]}  {t['ago']}")
        for t in [t for t in p["tasks"] if t["status"] in ("doing", "blocked", "todo")][:5]:
            L.append(f"   t{t['id']} [{t['status']}] {t['title'][:70]}")
        for h in p["handoffs"][:3]:
            L.append(f"   h{h['id']} → {h['to']}: {h['title'][:60]}")
        for e in p["events"][:4]:
            L.append(f"   \033[2m{e['ago']:>8} {e['surface']:7} {e['text'][:70]}\033[0m")
        L.append("")
    if snap["inbox"]:
        L.append("\033[1mIdeas inbox\033[0m")
        for m in snap["inbox"][:6]:
            hint = f"  → {m['guess']}?" if m["guess"] else ""
            L.append(f"   m{m['id']} {m['title'][:60]} {m['ago']}{hint}")
    return "\n".join(L)


def watch(only: str | None = None, interval: float = 1.0) -> None:
    store = Store()
    last = None
    try:
        while True:
            store.pull(force=False)
            marker = store.change_marker()
            if marker != last:
                last = marker
                print("\033[2J\033[H" + render_watch(snapshot(store), only), flush=True)
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
