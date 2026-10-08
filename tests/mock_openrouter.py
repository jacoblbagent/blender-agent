#!/usr/bin/env python3
"""A tiny OpenRouter stand-in for tests.

Serves /models, /key and /chat/completions (blocking + SSE streaming).
Responses come from a scenario file: a JSON list of turns, consumed in order,
each being either

    {"content": "text"}                       -> plain assistant message
    {"tool_calls": [{"name":..,"arguments":{..}}]}   -> tool call turn
    {"error": {"code": 402, "message": "..."}}       -> HTTP error

Every received request is appended to the JSONL log file so tests can assert
what the addon actually sent (system prompt, history, image parts, ...).
"""

import argparse
import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCENARIO = []
LOG_PATH = None
STATE = {"index": 0, "lock": threading.Lock()}


def log(record):
    if not LOG_PATH:
        return
    with STATE["lock"]:
        with open(LOG_PATH, "a") as fh:
            fh.write(json.dumps(record) + "\n")


def sse(obj):
    return ("data: " + json.dumps(obj) + "\n\n").encode()


def stream_events(message, finish):
    """Turn a complete message into a plausible delta stream."""
    out = []
    if message.get("reasoning"):
        for chunk in _chunks(message["reasoning"]):
            out.append(sse({"choices": [{"delta": {"reasoning": chunk}, "index": 0}]}))
    if message.get("content"):
        for chunk in _chunks(message["content"]):
            out.append(sse({"choices": [{"delta": {"content": chunk}, "index": 0}]}))
    for i, call in enumerate(message.get("tool_calls") or []):
        args = json.dumps(call.get("arguments") or {})
        out.append(sse({"choices": [{"delta": {"tool_calls": [{
            "index": i, "id": call.get("id") or "call_%d" % i, "type": "function",
            "function": {"name": call["name"], "arguments": ""}}]}, "index": 0}]}))
        for chunk in _chunks(args, 24):
            out.append(sse({"choices": [{"delta": {"tool_calls": [{
                "index": i, "function": {"arguments": chunk}}]}, "index": 0}]}))
    out.append(sse({"choices": [{"delta": {}, "finish_reason": finish, "index": 0}],
                    "usage": {"prompt_tokens": 111, "completion_tokens": 22,
                              "total_tokens": 133}}))
    out.append(b"data: [DONE]\n\n")
    return out


def _chunks(text, n=12):
    return [text[i:i + n] for i in range(0, len(text), n)]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # silence
        pass

    def _json(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if "/models" in self.path:
            self._json(200, {"data": [
                {"id": "mock/oracle-1", "name": "Mock Oracle 1", "context_length": 128000,
                 "architecture": {"input_modalities": ["text", "image"]},
                 "pricing": {"prompt": "0.000001", "completion": "0.000002"},
                 "supported_parameters": ["tools"]},
                {"id": "mock/scripter-2", "name": "Mock Scripter 2", "context_length": 32000,
                 "architecture": {"input_modalities": ["text"]},
                 "pricing": {"prompt": "0.0000005", "completion": "0.000001"},
                 "supported_parameters": ["tools"]},
            ]})
        elif "/key" in self.path:
            self._json(200, {"data": {"label": "mock-key", "usage": 0.5, "limit": 10}})
        else:
            self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {}
        log({"path": self.path, "headers": dict(self.headers), "payload": payload})
        if not self.path.endswith("/chat/completions"):
            return self._json(404, {"error": {"message": "not found"}})

        with STATE["lock"]:
            idx = STATE["index"]
            STATE["index"] += 1
        turn = SCENARIO[idx] if idx < len(SCENARIO) else {"content": "Mock: nothing left to do."}

        if turn.get("error"):
            return self._json(turn["error"].get("code", 500), {"error": turn["error"]})

        if turn.get("delay"):
            time.sleep(turn["delay"])

        message = {"content": turn.get("content"), "reasoning": turn.get("reasoning"),
                   "tool_calls": turn.get("tool_calls")}
        finish = "tool_calls" if message.get("tool_calls") else "stop"

        if payload.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            for ev in stream_events(message, finish):
                self.wfile.write(ev)
                self.wfile.flush()
                time.sleep(0.005)
            self.close_connection = True
            return

        self._json(200, {
            "id": "chatcmpl-mock-%s" % uuid.uuid4().hex[:8],
            "object": "chat.completion",
            "choices": [{"index": 0, "finish_reason": finish, "message": {
                "role": "assistant", "content": message.get("content"),
                "reasoning": message.get("reasoning"),
                "tool_calls": [{"id": c.get("id") or "call_%d" % i, "type": "function",
                                "function": {"name": c["name"],
                                             "arguments": json.dumps(c.get("arguments") or {})}}
                               for i, c in enumerate(message.get("tool_calls") or [])] or None,
            }}],
            "usage": {"prompt_tokens": 111, "completion_tokens": 22, "total_tokens": 133},
        })


def main():
    global SCENARIO, LOG_PATH
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--scenario", default=None)
    ap.add_argument("--log", default=None)
    ap.add_argument("--reset-log", action="store_true")
    args = ap.parse_args()
    if args.scenario:
        with open(args.scenario) as fh:
            SCENARIO = json.load(fh)
    LOG_PATH = args.log
    if LOG_PATH and args.reset_log and os.path.exists(LOG_PATH):
        os.remove(LOG_PATH)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("mock openrouter on http://127.0.0.1:%d (%d scenario turns)" % (args.port, len(SCENARIO)),
          flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
