"""The agent loop.

Blender's Python API may only be touched from the main thread, so the worker
thread does network I/O and *queues* every tool call back to the main thread
through ``run_on_main`` (drained by a bpy.app.timers callback, or manually by
``pump_once`` in headless tests).
"""

import json
import queue
import threading
import time
import traceback

import bpy

from . import context as ctxmod
from . import openrouter, tools

MAX_TRANSCRIPT = 400

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


class Session:
    """Live agent conversation. Single instance, module-level (SESSION)."""

    def __init__(self):
        self.messages = []          # OpenRouter payload history
        self.transcript = []        # what the panel shows
        self.thread = None
        self.cancel_flag = threading.Event()
        self.status = "idle"        # idle | thinking | tools | done | error | cancelled
        self.status_detail = ""
        self.error = ""
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0}
        self.last_step = 0
        self.pending_approval = None   # {"tool":..,"args":..,"event":Event,"approved":bool}
        self.abort_box = {"close": None}   # live HTTP response, so Stop is instant
        self._lock = threading.RLock()

    # ---------------------------------------------------------- transcript --
    def record(self, kind, text, **meta):
        entry = {"kind": kind, "text": text or "", "time": _stamp(), "meta": meta}
        with self._lock:
            self.transcript.append(entry)
            if len(self.transcript) > MAX_TRANSCRIPT:
                del self.transcript[: len(self.transcript) - MAX_TRANSCRIPT]
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
        with self._lock:
            self.messages = []
            self.transcript = []
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0}
        self.error = ""
        self.set_status("idle")

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

    def send(self, user_text, prefs):
        user_text = (user_text or "").strip()
        if not user_text:
            return False
        if self.busy():
            self.record("error", "Agent is already working - press Stop first.")
            return False
        self.cancel_flag.clear()
        self.error = ""
        with self._lock:
            self.messages.append({"role": "user", "content": user_text})
        self.record("user", user_text)
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
        last = next((m["content"] for m in reversed(self.messages) if m["role"] == "user"), "")
        if not last:
            return False
        with self._lock:
            self.messages = self.messages[:-1]
        self.transcript = [t for t in self.transcript if t["kind"] != "error"]
        return self.send(last, prefs)

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
        with self._lock:
            for entry in reversed(self.transcript):
                if entry["kind"] == "assistant" and entry["meta"].get("streaming"):
                    entry["text"] = text
                    if final:
                        entry["meta"]["streaming"] = False
                    break
            else:
                self.transcript.append({"kind": "assistant", "text": text, "time": _stamp(),
                                        "meta": {"streaming": not final}})
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
