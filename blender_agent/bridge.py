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

from . import agent, openrouter, shots

DEFAULT_PORT = 8770
TOKEN_FILE_ENV = "BLENDER_AGENT_TOKEN_FILE"

_server = {"httpd": None, "thread": None, "port": None, "host": "127.0.0.1",
           "handler": None, "token": ""}


# ------------------------------------------------------------------ helpers --

def is_running():
    httpd = _server["httpd"]
    return httpd is not None and _server["thread"] is not None and _server["thread"].is_alive()


def url(token=None, host=None):
    port = _server["port"] or DEFAULT_PORT
    base = "http://%s:%d/" % (host or "127.0.0.1", port)
    return base + ("?token=%s" % token if token else "")


def _apply_token(value):
    """Make a token change take effect on the running server, not just on restart.

    The server is an instance of a per-start subclass of _BridgeHandler, so setting
    the base class attribute leaves the live server checking the token it was started
    with - the rotate button then looks broken (the panel copies a token the server
    rejects) until Blender restarts.
    """
    _server["token"] = value
    _BridgeHandler.token = value
    handler = _server.get("handler")
    if handler is not None:
        handler.token = value


def token(prefs):
    """The bridge token: preferences first, then a small file that survives restarts.

    Blender resets an add-on's preferences when the add-on is re-registered, and the
    add-on's host service stops Blender without saving preferences. Either way, a
    token that lives only in prefs changes under the user and every link they saved
    turns into a 401, so a generated token is pinned to disk as well.
    """
    if (prefs.bridge_token or "").strip():
        _apply_token(prefs.bridge_token)
        return prefs.bridge_token
    pinned = _read_pinned_token()
    if pinned:
        prefs.bridge_token = pinned
        _apply_token(pinned)
        return pinned
    prefs.bridge_token = secrets.token_urlsafe(24)
    _pin_token(prefs.bridge_token)
    _apply_token(prefs.bridge_token)
    return prefs.bridge_token


def new_token(prefs):
    prefs.bridge_token = secrets.token_urlsafe(24)
    _pin_token(prefs.bridge_token)
    _apply_token(prefs.bridge_token)
    return prefs.bridge_token


def set_token(prefs, value=None):
    """Regenerate (or set) the bridge token and apply it to the live server."""
    prefs.bridge_token = value or secrets.token_urlsafe(24)
    _pin_token(prefs.bridge_token)
    _apply_token(prefs.bridge_token)
    return prefs.bridge_token


def _pinned_path():
    override = os.environ.get(TOKEN_FILE_ENV)
    if override:
        return override
    # user_resource(create=True) makes a *directory*, so join the file name onto it.
    base = bpy.utils.user_resource("CONFIG", path="blender_agent", create=True)
    return os.path.join(base, "bridge_token")


def _read_pinned_token():
    try:
        with open(_pinned_path(), encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _pin_token(value):
    """Write the token 0600, replacing the file atomically."""
    path = _pinned_path()
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(value)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        pass


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


def _png_response(handler, path):
    """Serve the screenshot. The URL carries a version, so it can be cached."""
    try:
        with open(path, "rb") as fh:
            body = fh.read()
    except OSError as exc:
        return _json_response(handler, 404, {"error": "no screenshot (%s)" % exc})
    handler.send_response(200)
    handler.send_header("Content-Type", "image/png")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "private, max-age=300")
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
        shot = shots.latest() or {}
        return {
            "blender": bpy.app.version_string,
            "file": bpy.data.filepath or "(unsaved)",
            "scene": bpy.context.scene.name,
            "objects": objects,
            "status": sess.status,
            "detail": sess.status_detail,
            "busy": sess.busy(),
            "model": prefs.resolved_model() if prefs else "",
            "key_set": bool(prefs and (prefs.api_key or "").strip()),
            "usage": sess.usage,
            "pending_approval": bool(sess.pending_approval),
            "shot": {"name": shot.get("name"), "label": shot.get("label"),
                     "objects": shot.get("objects"), "time": shot.get("time"),
                     "size": shot.get("size")} if shot else None,
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
    _server.update(httpd=httpd, thread=thread, port=port, host=host, handler=Handler)
    _apply_token(tok)
    return True, "listening on http://%s:%d (token required)" % (host, port)


def stop():
    httpd = _server["httpd"]
    if httpd is None:
        return False, "bridge not running"
    try:
        httpd.shutdown()
        httpd.server_close()
    finally:
        _server.update(httpd=None, thread=None, handler=None)
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
        if path == "/api/shot.png":
            shot = shots.latest()
            if not shot:
                return _json_response(self, 404, {"error": "no screenshot yet"})
            return _png_response(self, shot["path"])
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
        if path == "/api/shot":
            ok, msg = agent.run_on_main(lambda: shots.capture(label="remote"), timeout=180)
            return _json_response(self, 200 if ok else 409, {"ok": bool(ok), "message": msg})
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
#gatehint{color:var(--dim);font-size:12.5px;line-height:1.4}
#gateerr{color:var(--bad);font-size:12.5px;min-height:1em}
#objs{color:var(--dim);font-size:12px;padding:6px 12px;border-bottom:1px solid var(--line);white-space:pre-wrap}
#shotbox{display:none;padding:8px 12px;border-bottom:1px solid var(--line);background:var(--panel)}
#shotbox.show{display:block}
#shot{width:100%;max-width:520px;display:block;border:1px solid var(--line);border-radius:4px;background:#0b0c0d}
#shotcap{color:var(--dim);font-size:12px;margin-top:5px}
#setup{display:none;padding:8px 12px;border-bottom:1px solid var(--line);background:#2a1c1c;color:#f7c9c9;font-size:12.5px;line-height:1.45}
#setup.show{display:block}
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
  <div id="gatehint">Needs the bridge token from Blender: 3D viewport &rsaquo; N &rsaquo; Agent tab &rsaquo; the key icon on the "Remote" row (or Edit &rsaquo; Preferences &rsaquo; Add-ons &rsaquo; Blender Agent &rsaquo; Remote).</div>
  <input id="token" placeholder="Bridge token" autocomplete="off">
  <button id="save">Connect</button>
  <div id="gateerr"></div>
</div>
<div id="objs"></div>
<div id="setup"></div>
<div id="shotbox"><img id="shot" alt="Model screenshot"><div id="shotcap"></div></div>
<div id="log"></div>
<footer>
  <select id="model" title="Model"></select>
  <input id="p" placeholder="Ask Blender Agent&hellip;" autocomplete="off">
  <button id="send">Send</button>
  <button id="snap" title="Screenshot the model">Shot</button>
</footer>
<script>
const q = new URLSearchParams(location.search);
let tok = q.get('token') || localStorage.getItem('ba_token') || '';
const $ = id => document.getElementById(id);
const api = (path, opts={}) => fetch(path + (path.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(tok), opts);
function showGate(on, message){
  $('gate').classList.toggle('show', on);
  $('gateerr').textContent = message || '';
  if (on && !$('token').value) $('token').value = tok;
}
$('save').onclick = () => {
  const v = $('token').value.trim();
  if (!v) { showGate(true, 'Enter the bridge token first.'); return; }
  tok = v; localStorage.setItem('ba_token', tok); showGate(false); tick();
};
$('send').onclick = send;
$('p').addEventListener('keydown', e => { if (e.key === 'Enter') send(); });
$('stop').onclick = () => api('/api/stop', {method:'POST'});
$('clear').onclick = () => api('/api/clear', {method:'POST'}).then(tick);
$('snap').onclick = () => api('/api/shot', {method:'POST'}).then(tick);
$('model').onchange = () => api('/api/model', {method:'POST', headers:{'Content-Type':'application/json'},
  body: JSON.stringify({model: $('model').value})}).then(tick);
let modelsLoaded = false;
let lastShot = '';
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
  if (r.status === 401) { showGate(true, 'Token rejected - copy the current one from Blender (Agent panel, key icon next to Remote).'); return; }
  const j = await r.json().catch(()=>({}));
  if (j.error) { notice(j.error); return; }
  notices.length = 0;
  tick();
}
async function tick(){
  let r;
  try { r = await api('/api/status'); } catch(e) { $('meta').textContent='bridge unreachable'; return; }
  if (r.status === 401) { showGate(true, 'Token rejected - copy the current one from Blender (Agent panel, key icon next to Remote).'); $('meta').textContent='token required'; return; }
  showGate(false);
  const s = await r.json();
  const dot = $('dot'); dot.className = 'dot ' + (s.busy ? 'busy' : (s.status==='error'?'error':(s.status==='done'?'done':'')));
  $('meta').textContent = `${s.status}${s.detail?' - '+s.detail:''} | ${s.model||'no model'} | ${s.blender} | ${s.file}`;
  $('objs').textContent = 'scene ' + s.scene + ': ' + (s.objects||[]).length + ' objects\n' + (s.objects||[]).join('\n');
  const setup = $('setup');
  const missing = [];
  if (!s.key_set) missing.push('OpenRouter API key');
  if (!s.model) missing.push('model');
  if (missing.length) {
    setup.classList.add('show');
    setup.textContent = 'Nothing can be sent yet: ' + missing.join(' and no ') +
      ' set in Blender. Open Edit > Preferences > Add-ons > Blender Agent, add the key and pick a model, then reload this page.';
  } else {
    setup.classList.remove('show');
  }
  const sb = $('shotbox');
  if (s.shot && s.shot.name) {
    sb.classList.add('show');
    if (s.shot.name !== lastShot) {
      lastShot = s.shot.name;
      $('shot').src = '/api/shot.png?token=' + encodeURIComponent(tok) + '&v=' + encodeURIComponent(s.shot.name);
    }
    $('shotcap').textContent = (s.shot.label || 'model') + ' | ' + ((s.shot.objects||[]).length) + ' objects | ' + (s.shot.time || '');
  } else {
    sb.classList.remove('show');
  }
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
