"""Tailnet/remote bridge: drive Blender Agent from any device on the tailnet.

The Blender GUI process *is* the server: this module starts a small HTTP server on
loopback, and the tailnet publishes it with `tailscale serve` (tailnet-only).
Every request that touches Blender is marshalled to the main thread through
agent.run_on_main, so it is safe to call from the server's worker threads.

Auth: a bearer token (auto-generated, stored in addon preferences). The page asks
for it once and keeps it in localStorage. The tailnet is the outer gate; the token
stops anything else on the tailnet from driving your Blender.
"""

import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import bpy

from . import agent, openrouter

DEFAULT_PORT = 8770

_server = {"httpd": None, "thread": None, "port": None, "host": "127.0.0.1"}


# ------------------------------------------------------------------ helpers --

def is_running():
    httpd = _server["httpd"]
    return httpd is not None and _server["thread"] is not None and _server["thread"].is_alive()


def url(token=None, host=None):
    port = _server["port"] or DEFAULT_PORT
    base = "http://%s:%d/" % (host or "127.0.0.1", port)
    return base + ("?token=%s" % token if token else "")


def token(prefs):
    if not (prefs.bridge_token or "").strip():
        prefs.bridge_token = secrets.token_urlsafe(24)
    return prefs.bridge_token


def new_token(prefs):
    prefs.bridge_token = secrets.token_urlsafe(24)
    return prefs.bridge_token


def set_token(prefs, value=None):
    """Regenerate (or set) the bridge token and apply it to the live server."""
    prefs.bridge_token = value or secrets.token_urlsafe(24)
    _BridgeHandler.token = prefs.bridge_token
    return prefs.bridge_token


def _json_response(handler, code, payload):
    body = json.dumps(payload, default=str).encode()
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _html_response(handler, code, html):
    body = html.encode()
    handler.send_response(code)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _prefs():
    pkg = __package__ or "blender_agent"
    try:
        return bpy.context.preferences.addons[pkg].preferences
    except KeyError:
        return None


def _state():
    """Snapshot of the session, built on the main thread."""
    from . import context as ctxmod
    sess = agent.SESSION
    prefs = _prefs()

    def build():
        objects = []
        for obj in bpy.context.scene.objects[:60]:
            try:
                objects.append(ctxmod.object_line(obj))
            except Exception:  # noqa: BLE001
                objects.append("%s (%s)" % (obj.name, obj.type))
        return {
            "blender": bpy.app.version_string,
            "file": bpy.data.filepath or "(unsaved)",
            "scene": bpy.context.scene.name,
            "objects": objects,
            "status": sess.status,
            "detail": sess.status_detail,
            "busy": sess.busy(),
            "model": prefs.resolved_model() if prefs else "",
            "usage": sess.usage,
            "pending_approval": bool(sess.pending_approval),
            "transcript": [{"kind": t["kind"], "text": t["text"],
                            "tool": t.get("meta", {}).get("name"),
                            "ok": t.get("meta", {}).get("ok"),
                            "time": t["time"]}
                           for t in sess.transcript[-40:]],
        }

    try:
        return agent.run_on_main(build, timeout=30)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def start(prefs, port=None, host="127.0.0.1"):
    if is_running():
        return True, "bridge already running on %s" % url()
    port = int(port or prefs.bridge_port or DEFAULT_PORT)
    tok = token(prefs)

    class Handler(_BridgeHandler):
        pass

    Handler.token = tok
    try:
        httpd = ThreadingHTTPServer((host, port), Handler)
    except OSError as exc:
        return False, "could not bind %s:%d (%s)" % (host, port, exc)
    thread = threading.Thread(target=httpd.serve_forever, name="blender-agent-bridge",
                              daemon=True)
    thread.start()
    _server.update(httpd=httpd, thread=thread, port=port, host=host)
    return True, "listening on http://%s:%d (token required)" % (host, port)


def stop():
    httpd = _server["httpd"]
    if httpd is None:
        return False, "bridge not running"
    try:
        httpd.shutdown()
        httpd.server_close()
    finally:
        _server.update(httpd=None, thread=None)
    return True, "bridge stopped"


class _BridgeHandler(BaseHTTPRequestHandler):
    token = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep Blender's console clean
        pass

    # ------------------------------------------------------------------ auth --
    def _authorised(self, query):
        given = ""
        header = self.headers.get("Authorization") or ""
        if header.startswith("Bearer "):
            given = header[7:]
        if not given:
            given = (query.get("token") or [""])[0]
        return bool(given) and secrets.compare_digest(given, self.token)

    # ------------------------------------------------------------------ verbs --
    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path = parsed.path
        if path in ("/", "/index.html"):
            return _html_response(self, 200, PAGE)
        if path == "/healthz":
            return _json_response(self, 200, {"ok": True, "service": "blender-agent"})
        if not self._authorised(query):
            return _json_response(self, 401, {"error": "unauthorised",
                                              "hint": "append ?token=<token> or send a bearer token"})
        if path == "/api/status":
            return _json_response(self, 200, _state())
        if path == "/api/models":
            return _json_response(self, 200, {"models": openrouter.models()})
        return _json_response(self, 404, {"error": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return _json_response(self, 400, {"error": "invalid json"})
        if not self._authorised(query):
            return _json_response(self, 401, {"error": "unauthorised"})
        prefs = _prefs()
        if prefs is None:
            return _json_response(self, 503, {"error": "add-on not registered"})

        path = parsed.path
        if path == "/api/ask":
            prompt = (body.get("prompt") or "").strip()
            if not prompt:
                return _json_response(self, 400, {"error": "prompt required"})
            if not (prefs.api_key or "").strip():
                return _json_response(self, 409, {"error": "no OpenRouter API key set in Blender"})
            started = agent.run_on_main(
                lambda: agent.SESSION.send(prompt, prefs), timeout=60)
            return _json_response(self, 200, {"ok": bool(started),
                                              "busy": agent.SESSION.busy()})
        if path == "/api/stop":
            agent.run_on_main(agent.SESSION.cancel, timeout=30)
            return _json_response(self, 200, {"ok": True})
        if path == "/api/clear":
            agent.run_on_main(agent.SESSION.clear, timeout=30)
            return _json_response(self, 200, {"ok": True})
        if path == "/api/model":
            model = (body.get("model") or "").strip()
            if not model:
                return _json_response(self, 400, {"error": "model required"})

            def set_model():
                try:
                    prefs.model = model
                except TypeError:
                    prefs.model = "custom"
                    prefs.model_custom = model
                return prefs.resolved_model()

            resolved = agent.run_on_main(set_model, timeout=30)
            return _json_response(self, 200, {"ok": True, "model": resolved})
        return _json_response(self, 404, {"error": "not found"})


PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Blender Agent</title>
<style>
:root{--bg:#111214;--panel:#191b1e;--line:#2a2d31;--fg:#e8e9ea;--dim:#9aa0a6;--ok:#5fd08a;--bad:#f06a6a;--accent:#7aa2f7}
*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{background:var(--bg);color:var(--fg);font:15px/1.45 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif;display:flex;flex-direction:column;height:100dvh}
header{display:flex;align-items:center;gap:8px;padding:10px 12px;border-bottom:1px solid var(--line);background:var(--panel)}
.dot{width:9px;height:9px;border-radius:50%;background:var(--dim);flex:none}
.dot.busy{background:var(--accent)}
.dot.done{background:var(--ok)}
.dot.error{background:var(--bad)}
#head{flex:1;min-width:0}
#title{font-weight:600;letter-spacing:.2px}
#meta{color:var(--dim);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
button,input,select,textarea{font:inherit;touch-action:manipulation;border-radius:4px}
button{background:#23262a;color:var(--fg);border:1px solid var(--line);padding:8px 12px;cursor:pointer}
button:disabled{opacity:.45}
#log{flex:1;overflow-y:auto;padding:10px 12px;display:flex;flex-direction:column;gap:8px;-webkit-overflow-scrolling:touch}
.row{max-width:100%}
.who{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.6px;margin-bottom:2px}
.txt{white-space:pre-wrap;word-break:break-word;background:var(--panel);border:1px solid var(--line);border-radius:4px;padding:8px 10px}
.kind-user .txt{background:#1d2432;border-color:#2b3752}
.kind-tool .txt{background:#15181a;color:var(--dim);font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px}
.kind-error .txt{border-color:#4a2626;color:#f7c9c9}
.kind-info .txt{background:transparent;border:0;color:var(--dim);font-size:12.5px;padding:2px}
footer{border-top:1px solid var(--line);background:var(--panel);padding:10px 12px;display:flex;gap:8px;flex-wrap:wrap}
#p{flex:1;min-width:0;background:#0f1012;color:var(--fg);border:1px solid var(--line);padding:10px 12px}
#token{width:100%;background:#0f1012;color:var(--fg);border:1px solid var(--line);padding:10px 12px}
#gate{display:none;padding:14px 12px;gap:8px;flex-direction:column;border-bottom:1px solid var(--line)}
#gate.show{display:flex}
#objs{color:var(--dim);font-size:12px;padding:6px 12px;border-bottom:1px solid var(--line);white-space:pre-wrap}
@media (max-width:640px){#p,#token,#model{font-size:16px}}
</style></head>
<body>
<header>
  <span class="dot" id="dot"></span>
  <div id="head">
    <div id="title">Blender Agent</div>
    <div id="meta">connecting&hellip;</div>
  </div>
  <button id="clear" title="Clear conversation">Clear</button>
  <button id="stop" title="Stop the agent">Stop</button>
</header>
<div id="gate">
  <input id="token" placeholder="Bridge token (from Blender: Agent panel)" autocomplete="off">
  <button id="save">Connect</button>
</div>
<div id="objs"></div>
<div id="log"></div>
<footer>
  <select id="model" title="Model"></select>
  <input id="p" placeholder="Ask Blender Agent&hellip;" autocomplete="off">
  <button id="send">Send</button>
</footer>
<script>
const q = new URLSearchParams(location.search);
let tok = q.get('token') || localStorage.getItem('ba_token') || '';
const $ = id => document.getElementById(id);
const api = (path, opts={}) => fetch(path + (path.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(tok), opts);
function showGate(on){ $('gate').classList.toggle('show', on); if(on) $('token').value = tok; }
$('save').onclick = () => { tok = $('token').value.trim(); localStorage.setItem('ba_token', tok); showGate(false); tick(); };
$('send').onclick = send;
$('p').addEventListener('keydown', e => { if (e.key === 'Enter') send(); });
$('stop').onclick = () => api('/api/stop', {method:'POST'});
$('clear').onclick = () => api('/api/clear', {method:'POST'}).then(tick);
$('model').onchange = () => api('/api/model', {method:'POST', headers:{'Content-Type':'application/json'},
  body: JSON.stringify({model: $('model').value})}).then(tick);
let modelsLoaded = false;
const notices = [];   // client-side messages that are not part of Blender's transcript
function notice(text){
  notices.push(text);
  while (notices.length > 4) notices.shift();
  tick();
}
async function send(){
  const v = $('p').value.trim(); if(!v) return;
  $('p').value = '';
  const r = await api('/api/ask', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({prompt: v})});
  if (r.status === 401) { showGate(true); return; }
  const j = await r.json().catch(()=>({}));
  if (j.error) { notice(j.error); return; }
  notices.length = 0;
  tick();
}
async function tick(){
  let r;
  try { r = await api('/api/status'); } catch(e) { $('meta').textContent='bridge unreachable'; return; }
  if (r.status === 401) { showGate(true); $('meta').textContent='token required'; return; }
  showGate(false);
  const s = await r.json();
  const dot = $('dot'); dot.className = 'dot ' + (s.busy ? 'busy' : (s.status==='error'?'error':(s.status==='done'?'done':'')));
  $('meta').textContent = `${s.status}${s.detail?' - '+s.detail:''} | ${s.model||'no model'} | ${s.blender} | ${s.file}`;
  $('objs').textContent = 'scene ' + s.scene + ': ' + (s.objects||[]).length + ' objects\n' + (s.objects||[]).join('\n');
  const log = $('log'); const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 40;
  log.innerHTML = '';
  (s.transcript||[]).forEach(t => {
    const d = document.createElement('div'); d.className = 'row kind-' + t.kind;
    const who = document.createElement('div'); who.className = 'who';
    who.textContent = t.tool ? (t.tool + (t.ok === false ? ' (failed)' : '')) : (t.kind === 'user' ? 'you' : t.kind);
    const txt = document.createElement('div'); txt.className = 'txt'; txt.textContent = t.text || '';
    d.appendChild(who); d.appendChild(txt); log.appendChild(d);
  });
  notices.forEach(n => {
    const d = document.createElement('div'); d.className = 'row kind-error';
    const who = document.createElement('div'); who.className = 'who'; who.textContent = 'bridge';
    const txt = document.createElement('div'); txt.className = 'txt'; txt.textContent = n;
    d.appendChild(who); d.appendChild(txt); log.appendChild(d);
  });
  if (atBottom) log.scrollTop = log.scrollHeight;
  if (!modelsLoaded) { modelsLoaded = true; loadModels(); }
}
async function loadModels(){
  const r = await api('/api/models'); if (r.status !== 200) return;
  const j = await r.json(); const sel = $('model');
  sel.innerHTML = '';
  (j.models||[]).forEach(m => { const o = document.createElement('option');
    o.value = m.id; o.textContent = m.name + '  (' + m.id + ')'; sel.appendChild(o); });
  const st = await (await api('/api/status')).json();
  sel.value = st.model;
}
setInterval(tick, 1200); tick();
</script>
</body></html>
"""
