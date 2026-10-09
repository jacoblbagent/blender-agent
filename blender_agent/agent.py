"""The agent loop.

Blender's Python API may only be touched from the main thread, so the worker
thread does network I/O and *queues* every tool call back to the main thread
through ``run_on_main`` (drained by a bpy.app.timers callback, or manually by
``pump_once`` in headless tests).
"""

import json
import os
import queue
import threading
import time
import traceback
import uuid

import bpy

from . import context as ctxmod
from . import openrouter, shots, tools

MAX_TRANSCRIPT = 400
MAX_CHATS = 60


def _now():
    return time.time()


class Chat:
    """One independent conversation: its own payload history and transcript.

    Everything that describes "a conversation" lives here, so the user can keep
    several unrelated chats side by side and switch between them without one
    leaking its context into another.
    """

    def __init__(self, chat_id=None, title=""):
        self.id = chat_id or uuid.uuid4().hex[:12]
        self.title = title or ""
        self.created = _now()
        self.updated = self.created
        self.messages = []          # OpenRouter payload history
        self.transcript = []        # what the panel shows
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0}
        self.last_send = None       # {"text", "images"} - so Retry keeps the photo

    def title_text(self):
        """The display name: an explicit title, else the first thing you asked."""
        if self.title:
            return self.title
        first = next((t for t in self.transcript
                      if t.get("kind") == "user" and (t.get("text") or "").strip()), None)
        if first:
            return first["text"].strip().splitlines()[0][:40]
        return "New chat"

    def summary(self, active=False):
        return {"id": self.id, "title": self.title_text(), "messages": len(self.messages),
                "active": bool(active), "created": self.created, "updated": self.updated}

    def is_empty(self):
        return not self.messages and not self.transcript

    # ---------------------------------------------------------- persistence --
    def to_dict(self):
        last = self.last_send or {}
        return {
            "id": self.id,
            "title": self.title,
            "created": self.created,
            "updated": self.updated,
            "messages": _strip_images(self.messages),
            "transcript": self.transcript[-MAX_TRANSCRIPT:],
            "usage": self.usage,
            "last_send": {"text": last.get("text") or "",
                          "images": [p for p in (last.get("images") or [])
                                     if isinstance(p, str) and os.path.exists(p)]},
        }

    @classmethod
    def from_dict(cls, raw):
        if not isinstance(raw, dict):
            return None
        chat = cls(chat_id=str(raw.get("id") or "") or None,
                   title=str(raw.get("title") or ""))
        try:
            chat.created = float(raw.get("created") or chat.created)
        except (TypeError, ValueError):
            pass
        try:
            chat.updated = float(raw.get("updated") or chat.created)
        except (TypeError, ValueError):
            chat.updated = chat.created
        chat.messages = [m for m in (raw.get("messages") or [])
                         if isinstance(m, dict) and m.get("role")]
        transcript = []
        for entry in raw.get("transcript") or []:
            if not isinstance(entry, dict) or not entry.get("kind"):
                continue
            meta = entry.get("meta") if isinstance(entry.get("meta"), dict) else {}
            transcript.append({"kind": str(entry["kind"]), "text": str(entry.get("text") or ""),
                               "time": str(entry.get("time") or ""), "meta": meta})
        chat.transcript = transcript[-MAX_TRANSCRIPT:]
        usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
        for key in chat.usage:
            try:
                chat.usage[key] = int(usage.get(key) or 0)
            except (TypeError, ValueError):
                pass
        last = raw.get("last_send") if isinstance(raw.get("last_send"), dict) else {}
        images = [p for p in (last.get("images") or [])
                  if isinstance(p, str) and os.path.exists(p)]
        if (last.get("text") or "").strip() or images:
            chat.last_send = {"text": str(last.get("text") or ""), "images": images}
        return chat


def _strip_images(messages):
    """Payload history without inline image data, so saved chats stay small.

    The photo files themselves live on disk and are still referenced by
    ``last_send``, so Retry can resend them; only the base64 blobs are dropped.
    """
    out = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        copy = dict(msg)
        content = msg.get("content")
        if isinstance(content, list):
            parts, dropped = [], 0
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    dropped += 1
                    continue
                parts.append(part)
            if dropped:
                parts.append({"type": "text",
                              "text": "(%d photo%s omitted from saved history)"
                                      % (dropped, "" if dropped == 1 else "s")})
            copy["content"] = parts
        out.append(copy)
    return out


def _chats_path():
    """Where saved conversations live (honours the test overrides)."""
    try:
        from . import preferences as prefs_mod
        return prefs_mod.chats_path()
    except Exception:  # noqa: BLE001 - persistence must never be fatal
        return ""


def load_chats():
    try:
        return SESSION.load()
    except Exception:  # noqa: BLE001
        return False


def save_chats():
    try:
        return SESSION.save()
    except Exception:  # noqa: BLE001
        return False

# ------------------------------------------------------- main-thread bridge --

_MAIN_QUEUE = queue.Queue()
_pump_registered = False
_redraw_cb = None


def run_on_main(fn, timeout=600.0):
    """Execute fn on Blender's main thread and return its result."""
    if threading.current_thread() is threading.main_thread():
        return fn()
    box = {"done": threading.Event(), "result": None, "error": None}
    _MAIN_QUEUE.put((fn, box))
    if not box["done"].wait(timeout):
        raise TimeoutError("main-thread job timed out after %.0fs" % timeout)
    if box["error"] is not None:
        raise box["error"]
    return box["result"]


def drain_main_queue(limit=200):
    """Run queued main-thread jobs. Safe to call from the main thread only."""
    ran = 0
    while ran < limit:
        try:
            fn, box = _MAIN_QUEUE.get_nowait()
        except queue.Empty:
            break
        try:
            box["result"] = fn()
        except Exception as exc:  # noqa: BLE001 - relay to the waiting thread
            box["error"] = exc
        finally:
            box["done"].set()
        ran += 1
    return ran


def pump_once():
    """One manual tick: drain jobs. Used by headless test harnesses."""
    return drain_main_queue()


def _pump():
    drain_main_queue()
    if _redraw_cb is not None:
        try:
            _redraw_cb()
        except Exception:  # noqa: BLE001
            pass
    return 0.05


def register_timer():
    global _pump_registered
    if _pump_registered:
        return
    try:
        if not bpy.app.timers.is_registered(_pump):
            bpy.app.timers.register(_pump, first_interval=0.1, persistent=True)
        _pump_registered = True
    except Exception:  # noqa: BLE001
        _pump_registered = False


def unregister_timer():
    global _pump_registered
    try:
        if bpy.app.timers.is_registered(_pump):
            bpy.app.timers.unregister(_pump)
    except Exception:  # noqa: BLE001
        pass
    _pump_registered = False


# ------------------------------------------------------------- transcript ----

def _stamp():
    return time.strftime("%H:%M:%S")


def _text_of(message):
    """The text of a message whose content may be a multimodal parts list."""
    content = message.get("content")
    if isinstance(content, list):
        return next((p.get("text") for p in content if p.get("type") == "text"), "")
    return content or ""


def images_supported(prefs):
    """(ok, reason): can the selected model accept image input?

    An unknown model (no catalogue entry, e.g. a custom id) is allowed through -
    we would rather let the provider decide than block a working setup.
    """
    model = prefs.resolved_model()
    meta = openrouter.model_meta(model)
    if not meta:
        return True, ""
    if "image" in (meta.get("modalities") or "").lower():
        return True, ""
    return False, ("%s is a text-only model - pick a vision model to send a photo"
                   % (model or "the selected model"))


class Session:
    """Live agent session: the set of conversations plus the running turn.

    A conversation's data lives in a :class:`Chat`; the run state (worker
    thread, status, cancel flag, pending approval) is session-wide because only
    one turn can be in flight at a time. ``messages`` / ``transcript`` / ``usage``
    / ``last_send`` are properties that proxy to the *active* chat, so every
    existing caller keeps working unchanged.
    """

    def __init__(self):
        self.chats = [Chat()]
        self._active_id = self.chats[0].id
        self.thread = None
        self.cancel_flag = threading.Event()
        self.status = "idle"        # idle | thinking | tools | done | error | cancelled
        self.status_detail = ""
        self.error = ""
        self.last_step = 0
        self.pending_approval = None   # {"tool":..,"args":..,"event":Event,"approved":bool}
        self.abort_box = {"close": None}   # live HTTP response, so Stop is instant
        self._lock = threading.RLock()

    # ------------------------------------------------------------ active chat --
    @property
    def active_chat(self):
        for chat in self.chats:
            if chat.id == self._active_id:
                return chat
        if not self.chats:
            self.chats.append(Chat())
        self._active_id = self.chats[0].id
        return self.chats[0]

    @property
    def messages(self):
        return self.active_chat.messages

    @messages.setter
    def messages(self, value):
        self.active_chat.messages = value

    @property
    def transcript(self):
        return self.active_chat.transcript

    @transcript.setter
    def transcript(self, value):
        self.active_chat.transcript = value

    @property
    def usage(self):
        return self.active_chat.usage

    @usage.setter
    def usage(self, value):
        self.active_chat.usage = value

    @property
    def last_send(self):
        return self.active_chat.last_send

    @last_send.setter
    def last_send(self, value):
        self.active_chat.last_send = value

    def chat_list(self):
        return [c.summary(active=(c.id == self._active_id)) for c in self.chats]

    def active_title(self):
        return self.active_chat.title_text()

    # ------------------------------------------------------- chat management --
    def _reset_run_state(self):
        self.status = "idle"
        self.status_detail = ""
        self.error = ""
        self.last_step = 0
        self.pending_approval = None
        self.cancel_flag.clear()
        self.abort_box["close"] = None

    def _begin_fresh_chat(self, title=None):
        """Open a brand-new empty conversation and make it the active one."""
        chat = Chat(title=title or "")
        self.chats.append(chat)
        self._active_id = chat.id
        self._reset_run_state()
        return chat

    def new_chat(self, title=None):
        """Start a fresh, unrelated conversation. Returns the new Chat."""
        if self.busy():
            return None
        chat = self._begin_fresh_chat(title=title)
        self._prune_chats()
        self.save()
        return chat

    def switch_chat(self, chat_id):
        if self.busy() or not any(c.id == chat_id for c in self.chats):
            return False
        self._active_id = chat_id
        self._reset_run_state()
        self.save()
        return True

    def rename_chat(self, title, chat_id=None):
        title = (title or "").strip()
        if not title:
            return False
        target = chat_id or self._active_id
        for chat in self.chats:
            if chat.id == target:
                chat.title = title[:60]
                chat.updated = _now()
                self.save()
                return True
        return False

    def delete_chat(self, chat_id=None):
        """Delete a conversation; the only remaining chat is emptied, not removed."""
        if self.busy():
            return False
        target = chat_id or self._active_id
        if not any(c.id == target for c in self.chats):
            return False
        if len(self.chats) == 1:
            self.clear()
            return True
        index = next(i for i, c in enumerate(self.chats) if c.id == target)
        del self.chats[index]
        if target == self._active_id:
            self._active_id = self.chats[min(index, len(self.chats) - 1)].id
            self._reset_run_state()
        self.save()
        return True

    def _prune_chats(self):
        """Bound growth: drop the oldest empty chats first, never the active one."""
        if len(self.chats) <= MAX_CHATS:
            return
        active = self.active_chat
        others = [c for c in self.chats if c is not active]
        others.sort(key=lambda c: (not c.is_empty(), c.updated))
        drop = {id(c) for c in others[: len(self.chats) - MAX_CHATS]}
        self.chats = [c for c in self.chats if id(c) not in drop]

    # ----------------------------------------------------------- persistence --
    def to_dict(self):
        return {"version": 1, "active": self._active_id,
                "chats": [c.to_dict() for c in self.chats if not c.is_empty()]}

    def save(self):
        """Persist every non-empty conversation to the config dir, 0600."""
        path = _chats_path()
        if not path:
            return False
        try:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            tmp = path + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.to_dict(), fh)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except (OSError, TypeError, ValueError):
            return False
        return True

    def load(self):
        """Restore saved conversations into the list. A corrupt/absent file is
        not an error.

        A loaded session always opens a *fresh, empty* conversation: the saved
        ones come back in the list and stay reachable from the menu, but none of
        them is resumed, so starting the add-on never silently continues an
        earlier chat.
        """
        path = _chats_path()
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return False
        if not isinstance(data, dict):
            return False
        chats = []
        for raw in data.get("chats") or []:
            try:
                chat = Chat.from_dict(raw)
            except Exception:  # noqa: BLE001 - skip one bad chat, keep the rest
                chat = None
            if chat is not None:
                chats.append(chat)
        if not chats:
            return False
        self.chats = chats
        self._begin_fresh_chat()
        self._prune_chats()
        return True

    # ---------------------------------------------------------- transcript --
    def record(self, kind, text, **meta):
        entry = {"kind": kind, "text": text or "", "time": _stamp(), "meta": meta}
        chat = self.active_chat
        with self._lock:
            chat.transcript.append(entry)
            if len(chat.transcript) > MAX_TRANSCRIPT:
                del chat.transcript[: len(chat.transcript) - MAX_TRANSCRIPT]
            chat.updated = _now()
        if _redraw_cb is not None:
            try:
                _redraw_cb()
            except Exception:  # noqa: BLE001
                pass
        return entry

    def set_status(self, status, detail=""):
        self.status = status
        self.status_detail = detail
        if _redraw_cb is not None:
            try:
                _redraw_cb()
            except Exception:  # noqa: BLE001
                pass

    def busy(self):
        return self.thread is not None and self.thread.is_alive()

    def cancel(self):
        self.cancel_flag.set()
        self.set_status("cancelling")
        closer = self.abort_box.get("close")
        if closer is not None:
            try:
                closer()   # closes the live HTTP stream immediately
            except Exception:  # noqa: BLE001
                pass

    def clear(self):
        """Empty the active conversation (the chat itself stays in the list)."""
        chat = self.active_chat
        with self._lock:
            chat.messages = []
            chat.transcript = []
        chat.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0}
        chat.last_send = None
        chat.updated = _now()
        self.error = ""
        self.set_status("idle")
        self.save()

    def resolve_approval(self, approved):
        pending = self.pending_approval
        if not pending:
            return False
        pending["approved"] = bool(approved)
        pending["event"].set()
        self.pending_approval = None
        return True

    # ------------------------------------------------------------ requests --
    def _trim(self, prefs):
        keep = max(6, int(prefs.max_messages))
        msgs = self.messages
        if len(msgs) <= keep + 1:
            return msgs
        start = len(msgs) - keep
        while start > 1 and msgs[start].get("role") not in ("user",):
            start -= 1
        return [msgs[0]] + msgs[start:]

    def _payload(self, prefs, user_text=None):
        msgs = []
        system = prefs.system_prompt or "You are Blender Agent."
        if prefs.inject_context:
            try:
                system += "\n\n" + run_on_main(ctxmod.scene_context, timeout=30)
            except Exception as exc:  # noqa: BLE001
                system += "\n\n(scene context unavailable: %s)" % exc
        msgs.append({"role": "system", "content": system})
        msgs.extend(self._trim(prefs))
        return msgs

    def send(self, user_text, prefs, images=None):
        user_text = (user_text or "").strip()
        images = [p for p in (images or []) if p and os.path.exists(p)]
        if not user_text and not images:
            return False
        if self.busy():
            self.record("error", "Agent is already working - press Stop first.")
            return False
        if images:
            ok, why = images_supported(prefs)
            if not ok:
                self.record("error", why)
                return False
        self.cancel_flag.clear()
        self.error = ""
        content = user_text
        if images:
            # A multimodal user turn: the text plus one image_url part per photo.
            content = [{"type": "text",
                        "text": user_text or "Look at the attached photo."}]
            for path in images:
                part = openrouter.image_part(path)
                if part:
                    content.append(part)
        with self._lock:
            self.messages.append({"role": "user", "content": content})
        self.last_send = {"text": user_text, "images": list(images)}
        files = [os.path.basename(p) for p in images]
        self.record("user", user_text or ("(photo)" if len(files) == 1 else "(photos)"),
                    images=len(files), files=files)
        try:
            payload = self._payload(prefs)
        except Exception:  # noqa: BLE001
            self.record("error", traceback.format_exc(limit=4))
            return False
        self.thread = threading.Thread(
            target=self._worker, args=(prefs, payload), name="blender-agent", daemon=True)
        self.set_status("thinking", "waiting for %s" % prefs.resolved_model())
        self.thread.start()
        return True

    def retry_last(self, prefs):
        """Re-send after stripping the trailing failed assistant/tool tail."""
        with self._lock:
            while self.messages and self.messages[-1]["role"] != "user":
                self.messages.pop()
        last = self.last_send or {}
        if not self.messages:
            last = {}
        if not last:
            message = next((m for m in reversed(self.messages) if m["role"] == "user"), None)
            last = {"text": _text_of(message or {}), "images": []}
        text = (last.get("text") or "").strip()
        if not text and not last.get("images"):
            return False
        with self._lock:
            self.messages = self.messages[:-1]
        self.transcript = [t for t in self.transcript if t["kind"] != "error"]
        return self.send(text, prefs, images=last.get("images"))

    # -------------------------------------------------------------- worker --
    def _worker(self, prefs, payload):
        max_steps = max(1, int(prefs.max_steps))
        try:
            for step in range(1, max_steps + 1):
                if self.cancel_flag.is_set():
                    self.set_status("cancelled")
                    self.record("info", "Stopped by user.")
                    return
                self.last_step = step
                self.set_status("thinking", "step %d/%d - %s" % (step, max_steps, prefs.resolved_model()))
                content, tool_calls, finish = self._one_turn(prefs, payload)
                if self.cancel_flag.is_set():
                    self.set_status("cancelled")
                    self.record("info", "Stopped by user.")
                    return
                if finish == "error":
                    self.set_status("error")
                    return
                assistant = {"role": "assistant", "content": content or ""}
                if tool_calls:
                    assistant["tool_calls"] = tool_calls
                with self._lock:
                    self.messages.append(assistant)
                payload.append(assistant)
                if not tool_calls:
                    self.set_status("done", "finished in %d step(s)" % step)
                    self.record("info", "Finished (%d step%s)." % (step, "" if step == 1 else "s"))
                    return
                self.set_status("tools", "%d tool call(s) in step %d" % (len(tool_calls), step))
                for call in tool_calls:
                    if self.cancel_flag.is_set():
                        self.set_status("cancelled")
                        return
                    tool_msg = self._run_tool(prefs, call)
                    with self._lock:
                        self.messages.append(tool_msg)
                    payload.append(tool_msg)
            self.set_status("done", "step limit reached")
            self.record("info", "Reached the %d-step limit for this message. "
                                "Raise Max Tool Steps in preferences or ask me to continue."
                        % max_steps)
        except Exception:  # noqa: BLE001
            self.error = traceback.format_exc(limit=6)
            self.record("error", self.error)
            self.set_status("error")
        finally:
            # Keep the saved conversation current after every turn.
            self.save()

    def _one_turn(self, prefs, payload):
        """Stream one assistant turn. Returns (content, tool_calls, finish)."""
        content_parts = []
        tool_calls = []
        finish = "stop"
        first_token = True
        stream = openrouter.stream_chat(prefs, payload, tools.TOOL_SCHEMAS,
                                        cancel_flag=self.cancel_flag,
                                        abort_box=self.abort_box)
        for event in stream:
            if self.cancel_flag.is_set():
                return "".join(content_parts), [], "cancelled"
            etype = event["type"]
            if etype == "content":
                if first_token:
                    first_token = False
                    self.set_status("thinking", "receiving response")
                content_parts.append(event["text"])
                self._stream_into_transcript("".join(content_parts))
            elif etype == "reasoning" and prefs.auto_scroll:
                self.record("reasoning", event["text"][:2000])
            elif etype == "tool_calls":
                tool_calls = event["tool_calls"]
            elif etype == "done":
                finish = event.get("finish_reason") or "stop"
                self._count_usage(event.get("usage"))
            elif etype == "error":
                self.record("error", event["message"])
                self.set_status("error")
                return "".join(content_parts), [], "error"
        final = "".join(content_parts).strip()
        if final:
            self._stream_into_transcript(final, final=True)
        return final, tool_calls, finish

    def _stream_into_transcript(self, text, final=False):
        chat = self.active_chat
        with self._lock:
            for entry in reversed(chat.transcript):
                if entry["kind"] == "assistant" and entry["meta"].get("streaming"):
                    entry["text"] = text
                    if final:
                        entry["meta"]["streaming"] = False
                    break
            else:
                chat.transcript.append({"kind": "assistant", "text": text, "time": _stamp(),
                                        "meta": {"streaming": not final}})
            chat.updated = _now()
        if _redraw_cb is not None:
            try:
                _redraw_cb()
            except Exception:  # noqa: BLE001
                pass

    def _count_usage(self, usage):
        if not usage:
            return
        with self._lock:
            self.usage["calls"] = self.usage.get("calls", 0) + 1
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                self.usage[key] = self.usage.get(key, 0) + int(usage.get(key) or 0)

    def _run_tool(self, prefs, call):
        fn = call.get("function") or {}
        name = fn.get("name") or "unknown"
        raw_args = fn.get("arguments") or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
        except ValueError:
            args = {}
            self.record("error", "Could not parse arguments for %s: %s" % (name, raw_args[:200]))
        if name == "execute_blender_python" and prefs.confirm_code:
            if not self._request_approval(name, args):
                text = "USER DENIED permission to run that code. Ask what to do instead."
                self.record("tool", text, name=name, ok=False)
                return {"role": "tool", "tool_call_id": call.get("id", ""), "name": name, "content": text}
        if prefs.undo_per_tool and name in tools.NEEDS_UNDO:
            try:
                run_on_main(lambda: tools.mark_undo(name, args), timeout=30)
            except Exception:  # noqa: BLE001
                pass
        t0 = time.time()
        self.set_status("tools", "running %s" % name)
        try:
            result = run_on_main(lambda: tools.execute(name, args), timeout=max(
                120, int(prefs.timeout) + 60))
        except Exception as exc:  # noqa: BLE001
            result = "ERROR: %s" % exc
        dt = time.time() - t0
        images = []
        if isinstance(result, dict):
            images = result.get("images") or []
            result = result.get("text", "")
        result = str(result)
        ok = not result.startswith("ERROR")
        self.record("tool", result, name=name, ok=ok, seconds=dt,
                    args=self._brief_args(name, args))
        self._keep_screenshot(prefs, name, args, images, ok)
        content = result
        if images and prefs.vision_feedback:
            parts = [{"type": "text", "text": result}]
            for path in images:
                part = openrouter.image_part(path)
                if part:
                    parts.append(part)
            if len(parts) > 1:
                content = parts
        return {"role": "tool", "tool_call_id": call.get("id", ""), "name": name, "content": content}

    @staticmethod
    def _brief_args(name, args):
        if name == "execute_blender_python":
            return "code: " + (args.get("code") or "").strip().split("\n")[0][:90]
        try:
            return json.dumps(args)[:180]
        except (TypeError, ValueError):
            return str(args)[:180]

    def _keep_screenshot(self, prefs, name, args, images, ok):
        """Keep the panel's model screenshot current. Never fails a turn."""
        if not ok:
            return
        try:
            if images:
                path = next((p for p in images if p and os.path.exists(p)), None)
                if path:
                    run_on_main(lambda: shots.adopt(
                        path, label=name, objects=shots.names_from_args(args)), timeout=60)
                    return
            if not getattr(prefs, "auto_screenshot", True):
                return
            if name not in shots.AUTO_TOOLS:
                return
            names = shots.names_from_args(args)
            captured, msg = run_on_main(
                lambda: shots.capture(label=name, objects=names), timeout=180)
            if not captured:
                self.record("info", "screenshot skipped: %s" % msg)
        except Exception:  # noqa: BLE001 - a screenshot must never break a turn
            pass

    def _request_approval(self, name, args):
        pending = {"tool": name, "args": args, "event": threading.Event(), "approved": False}
        self.pending_approval = pending
        self.set_status("approval", "waiting for your approval to run code")
        self.record("approval", "Approval needed to run execute_blender_python:\n"
                                + (args.get("code") or "")[:600])
        if not pending["event"].wait(timeout=600):
            self.record("tool", "Approval timed out - skipped.", name=name, ok=False)
            self.pending_approval = None
            return False
        return bool(pending["approved"])


SESSION = Session()


def active_prefs():
    pkg = __package__ or "blender_agent"
    try:
        return bpy.context.preferences.addons[pkg].preferences
    except KeyError:
        return None
