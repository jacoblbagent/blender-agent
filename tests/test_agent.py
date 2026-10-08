"""Headless end-to-end test for Blender Agent. Run via tests/run_tests.sh.

Executed by: blender -b --python tests/test_agent.py
Environment: MOCK_URL, MOCK_LOG, optional MOCK_PORT
"""

import json
import os
import sys
import time

import bpy

MOCK_URL = os.environ.get("MOCK_URL", "http://127.0.0.1:8899/v1")
MOCK_LOG = os.environ.get("MOCK_LOG", "/tmp/blender_agent_mock.jsonl")

RESULTS = []


def check(name, condition, detail=""):
    RESULTS.append((name, bool(condition), detail))
    print("%s %s%s" % ("PASS" if condition else "FAIL", name,
                       (" - " + str(detail)) if detail else ""), flush=True)
    return bool(condition)


def heading(text):
    print("\n=== %s ===" % text, flush=True)


def pump(seconds=60.0, until=None):
    """Drain main-thread jobs until the agent finishes (or timeout)."""
    t0 = time.time()
    while time.time() - t0 < seconds:
        from blender_agent import agent
        agent.pump_once()
        if until is not None and until():
            return True
        if not agent.SESSION.busy() and agent.SESSION.status not in ("thinking", "tools"):
            agent.pump_once()
            return True
        time.sleep(0.01)
    return False


def mock_requests():
    if not os.path.exists(MOCK_LOG):
        return []
    with open(MOCK_LOG) as fh:
        return [json.loads(line) for line in fh if line.strip()]


def reset_log():
    if os.path.exists(MOCK_LOG):
        os.remove(MOCK_LOG)


class _Region:
    """Stand-in for a UI region: the panel code only reads .width."""

    def __init__(self, width):
        self.width = width


class _Ctx:
    """Stand-in for a panel context: only .region.width is read."""

    def __init__(self, width):
        self.region = _Region(width)


# --------------------------------------------------------------- test plan --

def main():
    from blender_agent import agent, openrouter, tools, preferences

    heading("1. registration")
    check("addon module imported", hasattr(bpy.types, "BLENDER_AGENT_PT_agent"))
    check("panel category", bpy.types.BLENDER_AGENT_PT_agent.bl_category == "Agent")
    check("operator send", hasattr(bpy.ops.blender_agent, "send"))
    check("operator set_model", hasattr(bpy.ops.blender_agent, "set_model"))
    check("operator quick_ask", hasattr(bpy.ops.blender_agent, "quick_ask"))
    check("operator open_panel", hasattr(bpy.ops.blender_agent, "open_panel"))
    check("operator add_workspace", hasattr(bpy.ops.blender_agent, "add_workspace"))
    check("operator screenshot_model", hasattr(bpy.ops.blender_agent, "screenshot_model"))
    check("operator open_screenshot", hasattr(bpy.ops.blender_agent, "open_screenshot"))
    check("preferences registered", "blender_agent" in bpy.context.preferences.addons)
    from blender_agent import preferences as prefs_mod
    prefs = bpy.context.preferences.addons["blender_agent"].preferences
    keep = (prefs.api_key, prefs.model, prefs.model_custom)
    prefs.api_key = "sk-test-remember"
    prefs.model, prefs.model_custom = "custom", "mock/remember-me"
    prefs_mod.remember(prefs)
    prefs.api_key = ""
    prefs.model, prefs.model_custom = "custom", ""
    prefs_mod.restore(prefs)
    check("the API key survives a preference reset",
          prefs.api_key == "sk-test-remember", prefs.api_key[:7])
    check("the model survives a preference reset",
          prefs.resolved_model() == "mock/remember-me", prefs.resolved_model())
    setup_mode = oct(os.stat(prefs_mod._setup_path()).st_mode & 0o777)
    check("the saved settings are not world readable", setup_mode == "0o600", setup_mode)
    prefs.api_key, prefs.model, prefs.model_custom = keep
    from blender_agent import ui as ui_mod
    icon_bad = ui_mod.icon_problems()
    check("every UI icon exists in Blender", not icon_bad, icon_bad[:5])
    prefs = bpy.context.preferences.addons["blender_agent"].preferences
    from blender_agent import workspace as ws_mod
    before_ws = sorted(w.name for w in bpy.data.workspaces)
    ok_ws, msg_ws = ws_mod.add_workspace(bpy.context)
    after_ws = sorted(w.name for w in bpy.data.workspaces)
    if ok_ws:
        check("workspace created", "Agent" in after_ws, "%s -> %s" % (before_ws, after_ws))
        check("no source workspace renamed/hijacked",
              set(before_ws).issubset(set(after_ws)), before_ws)
        check("workspace is idempotent", ws_mod.add_workspace(bpy.context)[0]
              and len(bpy.data.workspaces) == len(after_ws))
    else:
        check("workspace fails cleanly without a window", "window" in msg_ws.lower(), msg_ws)

    heading("2. tool layer on a real Blender scene")
    problems = tools.schema_problems()
    check("tool schemas are provider-safe", not problems,
          problems[:6] if problems else "%d schemas clean" % len(tools.TOOL_SCHEMAS))
    check("look_at accepts 'x,y,z' and an object name",
          tools._spot_point("1, 2, 3") is not None and tools._spot_point("Cube") is not None)
    bpy.ops.wm.read_homefile(use_empty=True)
    out = tools.execute("create_objects", {"objects": [
        {"type": "cube", "name": "AgentCube", "location": [0, 0, 1], "size": 2},
        {"type": "cylinder", "name": "AgentCyl", "radius": 0.5, "depth": 3, "location": [3, 0, 1]},
        {"type": "cone", "name": "AgentCone", "radius": 1.0, "depth": 2, "location": [-3, 0, 1]},
        {"type": "torus", "name": "AgentTorus", "major_radius": 1.0, "minor_radius": 0.3,
         "location": [0, 3, 1]},
    ]})
    check("create_objects returns text", isinstance(out, str) and "created 4" in out, out.split("\n")[0])
    names = sorted(o.name for o in bpy.context.scene.objects)
    check("4 objects exist", len(names) == 4, names)
    check("cylinder radius applied", abs(bpy.data.objects["AgentCyl"].dimensions.x - 1.0) < 0.01,
          bpy.data.objects["AgentCyl"].dimensions.x)
    check("cone params applied", abs(bpy.data.objects["AgentCone"].dimensions.z - 2.0) < 0.01)

    ctx = tools.execute("get_scene_context", {})
    check("scene context lists objects", "AgentCube" in ctx and "render:" in ctx)
    check("object details", "AgentTorus" in tools.execute("get_object_details",
                                                          {"names": ["AgentTorus"]}))

    out = tools.execute("set_material", {"targets": ["AgentCube"],
                                         "material": {"name": "Red", "Base Color": [0.8, 0.05, 0.05],
                                                      "Roughness": 0.3, "Metallic": 0.5}})
    mat = bpy.data.objects["AgentCube"].data.materials[0]
    node = next(n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
    check("material assigned", mat.name == "Red", out.split("\n")[0])
    check("principled sockets set",
          abs(node.inputs["Metallic"].default_value - 0.5) < 1e-6
          and abs(node.inputs["Base Color"].default_value[0] - 0.8) < 1e-6)

    tools.execute("add_modifiers", {"targets": ["AgentCube"],
                                    "modifiers": [{"type": "SUBSURF", "levels": 2},
                                                  {"type": "BEVEL", "width": 0.02}]})
    mods = [m.type for m in bpy.data.objects["AgentCube"].modifiers]
    check("modifiers added", mods == ["SUBSURF", "BEVEL"], mods)

    out = tools.execute("execute_blender_python", {
        "code": "import bpy\nsphere = bpy.data.meshes.new('S')"
                "\nobj = bpy.data.objects.new('AiSphere', sphere)"
                "\nbpy.context.scene.collection.objects.link(obj)"
                "\nprint('linked', obj.name)\nlen(bpy.context.scene.objects)",
        "description": "custom object via bpy"})
    check("python tool ran", "linked AiSphere" in out and "result: 5" in out, out[:120])

    out = tools.execute("execute_blender_python", {"code": "raise ValueError('boom')"})
    check("python tool relays errors", out.startswith("ERROR") and "boom" in out)

    out = tools.execute("search_api", {"query": "torus", "scope": "operators"})
    check("api search finds operators", "bpy.ops.mesh.primitive_torus_add" in out)
    check("bpy_operator call", "FINISHED" in tools.execute(
        "bpy_operator", {"operator": "mesh.primitive_cube_add", "parameters": {"size": 1.0}}))
    check("unknown tool is handled", tools.execute("nope", {}).startswith("ERROR"))

    heading("3. render + vision feedback")
    tools.execute("create_objects", {"objects": [
        {"type": "plane", "name": "Ground", "size": 20},
        {"type": "light", "name": "Key", "light_type": "AREA", "energy": 1200,
         "location": [4, -5, 6], "look_at": [0, 0, 0]},
        {"type": "camera", "name": "Cam", "lens": 45, "location": [7, -7, 5], "look_at": [0, 0, 1]}]})
    tools.execute("set_render_settings", {"engine": "CYCLES", "resolution": [240, 135],
                                         "samples": 1, "denoise": False})
    out = tools.execute("render_image", {"filepath": "/tmp/blender_agent_test_render.png"})
    ok_render = os.path.exists("/tmp/blender_agent_test_render.png")
    check("render_image wrote a file", ok_render, out)
    out = tools.execute("render_preview_and_view", {"width": 160})
    check("render_preview_and_view returns an image", isinstance(out, dict) and out.get("images"),
          list(out.keys()) if isinstance(out, dict) else out)
    preview = out["images"][0] if isinstance(out, dict) and out.get("images") else None
    check("preview file exists", preview and os.path.exists(preview), preview)
    check("render settings restored", bpy.context.scene.render.resolution_x == 240)

    heading("3b. model screenshot")
    from blender_agent import shots
    ok_s, msg_s = shots.capture(label="test")
    current = shots.latest() or {}
    check("screenshot rendered", ok_s and os.path.exists(current.get("path", "")), msg_s)
    check("screenshot registered for the panel",
          bool(current.get("objects")) and str(current.get("name", "")).endswith(".png"),
          current.get("name"))
    check("screenshot frames the model's objects",
          current.get("objects") and all(n in bpy.context.scene.objects
                                          for n in current["objects"]),
          current.get("objects"))
    check("screenshot leaves no camera behind",
          "AgentShotCam" not in bpy.context.scene.objects
          and not any(c.name.startswith("AgentShot") for c in bpy.data.cameras),
          [o.name for o in bpy.context.scene.objects])
    check("screenshot restores render settings",
          bpy.context.scene.render.engine == "CYCLES"
          and bpy.context.scene.render.resolution_x == 240,
          (bpy.context.scene.render.engine, bpy.context.scene.render.resolution_x))
    check("screenshot can adopt an existing render",
          shots.adopt(preview, label="adopted")[0]
          and (shots.latest() or {}).get("path") == preview)
    check("unknown targets fall back to the whole scene",
          shots.capture(label="named", objects=["NoSuchObject"])[0])
    check("object names come from the tool arguments",
          shots.names_from_args({"objects": [{"name": "A"}, {"name": "B"}]}) == ["A", "B"]
          and shots.names_from_args({"targets": "Cube"}) == ["Cube"])
    check("the thumbnail is sized to fit the panel",
          shots.thumb_scale(None) == shots.THUMB_SCALE
          and shots.thumb_scale(_Ctx(280)) < shots.THUMB_SCALE
          and shots.thumb_scale(_Ctx(2000)) == shots.THUMB_SCALE,
          (shots.thumb_scale(_Ctx(280)), shots.thumb_scale(_Ctx(2000))))
    meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    framed = shots._framing_targets(meshes)
    check("a huge ground plane does not shrink the model",
          any(o.name == "Ground" for o in meshes)
          and not any(o.name == "Ground" for o in framed),
          [o.name for o in framed])
    red = bpy.data.objects["AgentCube"].data.materials[0]
    colour = shots._principled_colour(red)
    before_colour = tuple(red.diffuse_color)
    shots.capture(label="colour")
    check("the shot picks up the material's base colour",
          colour and abs(colour[0] - 0.8) < 1e-6 and abs(colour[1] - 0.05) < 1e-6,
          colour)
    check("the viewport colour is put back afterwards",
          tuple(red.diffuse_color) == before_colour, (before_colour, tuple(red.diffuse_color)))

    heading("4. undo integration")
    n_before = len(bpy.context.scene.objects)
    tools.execute("create_objects", {"objects": [{"type": "cube", "name": "UndoMe"}]})
    check("object created before undo", "UndoMe" in bpy.context.scene.objects)
    pushed = tools.mark_undo("create_objects")
    check("undo state can be pushed", pushed or tools.execute(
        "undo_redo", {"action": "push"}).startswith("undo state"))
    try:
        bpy.ops.ed.undo()
        check("undo removed it", "UndoMe" not in bpy.context.scene.objects,
              len(bpy.context.scene.objects))
    except RuntimeError as exc:
        # Blender disables the undo operator without a window/screen: headless-only limit.
        print("SKIP undo replay (%s) - covered by the GUI test" % exc, flush=True)

    heading("5. model catalogue from the endpoint")
    openrouter.set_models([])
    models = openrouter.fetch_models("test-key", MOCK_URL)
    check("mock catalogue fetched", len(models) == 2, [m["id"] for m in models])
    check("pricing normalised", models[0]["prompt_price"] not in ("", "-"),
          models[0]["prompt_price"])
    items = [i[0] for i in openrouter.model_enum_items(None, None)]
    check("enum exposes model ids", "mock/oracle-1" in items, items[:4])
    ok, msg = openrouter.validate_key(_prefs_with(MOCK_URL, api_key="test-key"))
    check("key validation ok", ok, msg)

    heading("6. agent loop against the mock endpoint (blocking mode)")
    reset_log()
    _configure(prefs, MOCK_URL, stream=False)
    agent.SESSION.clear()
    ok = agent.SESSION.send("Build a red cube named LoopCube at 2m tall.", prefs)
    check("send accepted", ok)
    finished = pump(90)
    check("loop finished", finished, agent.SESSION.status)
    check("cube created by the agent", "LoopCube" in bpy.context.scene.objects,
          sorted(o.name for o in bpy.context.scene.objects))
    kinds = [t["kind"] for t in agent.SESSION.transcript]
    check("transcript has user/assistant/tool", "user" in kinds and "assistant" in kinds
          and "tool" in kinds, kinds)
    roles = [m["role"] for m in agent.SESSION.messages]
    check("history shape", roles[:3] == ["user", "assistant", "tool"], roles)
    tool_msgs = [m for m in agent.SESSION.messages if m["role"] == "tool"]
    check("tool result fed back", "created 1 object" in str(tool_msgs[0]["content"]),
          str(tool_msgs[0]["content"])[:80])
    check("usage tracked", agent.SESSION.usage["total_tokens"] > 0, agent.SESSION.usage)
    reqs = mock_requests()
    check("system prompt sent", "Blender Agent" in reqs[0]["payload"]["messages"][0]["content"])
    check("live scene context injected", "LoopCube" in reqs[-1]["payload"]["messages"][0]["content"]
          or "AgentCube" in reqs[-1]["payload"]["messages"][0]["content"])
    check("tools advertised", any(t["function"]["name"] == "execute_blender_python"
                                 for t in reqs[0]["payload"]["tools"]))
    check("auth header sent", reqs[0]["headers"].get("Authorization") == "Bearer test-key",
          reqs[0]["headers"].get("Authorization"))

    heading("7. streaming mode + image feedback")
    reset_log()
    _configure(prefs, MOCK_URL, stream=True)
    agent.SESSION.clear()
    agent.SESSION.send("Look at the scene and tell me how it reads.", prefs)
    ok = pump(90)
    check("streaming loop finished", ok, agent.SESSION.status)
    reqs = mock_requests()
    check("streaming produced 2 turns", len(reqs) >= 2, len(reqs))
    tool_msg = None
    for req in reqs[1:]:
        for m in req["payload"]["messages"]:
            if m["role"] == "tool" and isinstance(m.get("content"), list):
                tool_msg = m
    check("image part sent back to the model", tool_msg is not None,
          "content types: %s" % [p.get("type") for p in tool_msg["content"]] if tool_msg else "none")
    if tool_msg:
        img = [p for p in tool_msg["content"] if p["type"] == "image_url"]
        check("image is a base64 data url", img and img[0]["image_url"]["url"].startswith(
            "data:image/png;base64,"))
    check("streamed text recorded", any(t["kind"] == "assistant" and t["text"]
                                       for t in agent.SESSION.transcript))

    heading("8. error surfacing + cancel")
    reset_log()
    _configure(prefs, MOCK_URL, stream=False)
    agent.SESSION.clear()
    agent.SESSION.send("Trigger an error.", prefs)
    pump(60)
    errs = [t for t in agent.SESSION.transcript if t["kind"] == "error"]
    check("http error surfaced to the user", errs and "402" in errs[0]["text"],
          errs[0]["text"][:90] if errs else "no error entry")
    check("status is error", agent.SESSION.status == "error", agent.SESSION.status)

    reset_log()
    agent.SESSION.clear()
    agent.SESSION.send("Slow one.", prefs)
    time.sleep(0.3)
    agent.SESSION.cancel()
    pump(30, until=lambda: agent.SESSION.status == "cancelled")
    check("cancel stops the run", agent.SESSION.status == "cancelled", agent.SESSION.status)
    check("cancel recorded", any("Stopped by user" in t["text"] for t in agent.SESSION.transcript))

    heading("10. remote bridge (tailnet path)")
    import threading
    import urllib.error
    import urllib.request
    from blender_agent import bridge

    prefs.bridge_port = 8771
    bridge.set_token(prefs, "test-token-123")
    check("bridge token is pinned for restarts",
          bridge._read_pinned_token() == "test-token-123", bridge._read_pinned_token())
    prefs.bridge_token = ""
    check("a reset preference recovers the pinned token",
          bridge.token(prefs) == "test-token-123", prefs.bridge_token)
    ok_b, msg_b = bridge.start(prefs)
    check("bridge started on loopback", ok_b and bridge.is_running(), msg_b)

    base = "http://127.0.0.1:8771"
    box = {}

    def get(path, token="test-token-123"):
        req = urllib.request.Request(base + path)
        if token:
            req.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()
        except Exception as exc:  # noqa: BLE001
            return 0, str(exc)

    def get_bytes(path, token="test-token-123"):
        req = urllib.request.Request(base + path)
        if token:
            req.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except Exception as exc:  # noqa: BLE001
            return 0, str(exc).encode()

    def post(path, payload, token="test-token-123"):
        req = urllib.request.Request(base + path, data=json.dumps(payload).encode())
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return r.status, json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode() or "{}")
        except Exception as exc:  # noqa: BLE001
            return 0, {"error": str(exc)}

    def client():
        box["health"] = get("/healthz", token=None)
        box["page"] = get("/", token=None)
        box["noauth"] = get("/api/status", token=None)
        box["badtoken"] = get("/api/status", token="wrong")
        code, payload = post("/api/model", {"model": "mock/scripter-2"})
        box["setmodel"] = (code, payload)
        code, payload = post("/api/ask", {"prompt": "Add a sphere to the scene."})
        box["ask"] = (code, payload)
        deadline = time.time() + 60
        while time.time() < deadline:
            code, raw = get("/api/status")
            if code == 200:
                st = json.loads(raw)
                if not st.get("busy") and st.get("status") in ("done", "error"):
                    box["status"] = st
                    break
            time.sleep(0.3)
        box["stop"] = post("/api/stop", {})
        box["shot"] = post("/api/shot", {})
        box["shotpng"] = get_bytes("/api/shot.png")
        box["shotpng_noauth"] = get_bytes("/api/shot.png", token=None)
        box["hashes"] = get("/api/status")

    worker = threading.Thread(target=client, name="bridge-test-client", daemon=True)
    worker.start()
    t0 = time.time()
    while worker.is_alive() and time.time() - t0 < 90:
        agent.pump_once()          # main thread: runs the agent's tool calls
        time.sleep(0.01)

    # Rotating the token must reach the running server at once, not just prefs.
    # (/api/models answers without the main thread, so it works after the pump stops.)
    old_token = "test-token-123"
    fresh = bridge.set_token(prefs)
    check("rotation reaches the live server",
          get("/api/models", token=fresh)[0] == 200
          and get("/api/models", token=old_token)[0] == 401,
          (fresh == old_token, get("/api/models", token=fresh)[0],
           get("/api/models", token=old_token)[0]))
    check("rotation is pinned for restarts", bridge._read_pinned_token() == fresh,
          bridge._read_pinned_token())
    check("the link handed to a browser carries the token",
          bridge.link(prefs).endswith("?token=" + fresh), bridge.link(prefs)[:40])
    explicit = bridge.link(prefs, host="example.ts.net")
    check("an explicit public host is used in the link",
          explicit == "http://example.ts.net:8771/?token=" + fresh, explicit)

    check("healthz open without a token", box.get("health", (0, ""))[0] == 200)
    page = box.get("page", (0, ""))[1]
    check("web UI served", box.get("page", (0, ""))[0] == 200 and "Blender Agent" in page
          and "<script>" in page)
    check("page JS escapes survive Python", "join('\\n')" in page,
          "backslash-n was eaten by the Python string, which breaks the page's JS")
    check("status requires a token", box.get("noauth", (0, ""))[0] == 401,
          box.get("noauth", (0, ""))[0])
    check("wrong token rejected", box.get("badtoken", (0, ""))[0] == 401)
    check("model can be changed remotely", box.get("setmodel", (0, {}))[0] == 200
          and box["setmodel"][1].get("model") == "mock/scripter-2",
          box.get("setmodel"))
    check("ask accepted remotely", box.get("ask", (0, {}))[0] == 200
          and box["ask"][1].get("ok"), box.get("ask"))
    st = box.get("status") or {}
    check("remote turn completed", st.get("status") == "done", st.get("status"))
    check("status reports whether a key is set", "key_set" in st, sorted(st.keys())[:8])
    check("bridge report includes the live scene",
          any("BridgeSphere" in o for o in st.get("objects") or []),
          [o for o in (st.get("objects") or []) if "Bridge" in o])
    check("object actually created in Blender", "BridgeSphere" in bpy.context.scene.objects)
    check("transcript exposed to the client",
          any(t["kind"] == "tool" for t in st.get("transcript") or []),
          [t["kind"] for t in st.get("transcript") or []])
    check("used the remotely selected model", st.get("model") == "mock/scripter-2",
          st.get("model"))
    check("stop endpoint answers", box.get("stop", (0, {}))[0] == 200)
    check("screenshot can be taken remotely", box.get("shot", (0, {}))[0] == 200
          and box["shot"][1].get("ok"), box.get("shot"))
    png_status, png_body = box.get("shotpng", (0, b""))
    check("screenshot is served as a PNG", png_status == 200
          and png_body[:8] == b"\x89PNG\r\n\x1a\n", (png_status, png_body[:8]))
    check("screenshot endpoint requires a token",
          box.get("shotpng_noauth", (0, b""))[0] == 401)
    final = json.loads(box.get("hashes", (0, "{}"))[1] or "{}")
    check("status carries the screenshot", str((final.get("shot") or {}).get("name", "")
                                               ).endswith(".png"), final.get("shot"))
    ok_s, msg_s = bridge.stop()
    check("bridge stops cleanly", ok_s and not bridge.is_running(), msg_s)

    heading("11. message trimming")
    from blender_agent import agent as agent_mod
    agent.SESSION.clear()
    for i in range(40):
        agent.SESSION.messages.append({"role": "user", "content": "u%d" % i})
        agent.SESSION.messages.append({"role": "assistant", "content": "a%d" % i})
    agent.SESSION.messages.insert(0, {"role": "system", "content": "sys"})
    prefs.max_messages = 10
    trimmed = agent.SESSION._trim(prefs)
    check("trim keeps the system prompt", trimmed[0]["role"] == "system")
    check("trim bounded", len(trimmed) <= 12, len(trimmed))
    check("trim starts at a user turn",
          trimmed[1]["role"] == "user", [m["role"] for m in trimmed[:3]])
    prefs.max_messages = 60

    # ------------------------------------------------------------------ done
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print("\n%s" % ("=" * 62))
    print("RESULT %d/%d passed" % (passed, len(RESULTS)))
    for name, ok, detail in RESULTS:
        if not ok:
            print("  FAILED: %s (%s)" % (name, detail))
    print("=" * 62, flush=True)
    open(os.environ.get("MOCK_RESULT", "/tmp/blender_agent_result.json"), "w").write(
        json.dumps({"passed": passed, "total": len(RESULTS),
                    "failures": [n for n, ok, _ in RESULTS if not ok]}))
    sys.stdout.flush()


def _prefs_with(url, api_key="k"):
    prefs = bpy.context.preferences.addons["blender_agent"].preferences
    saved = (prefs.base_url, prefs.api_key)
    prefs.base_url, prefs.api_key = url, api_key
    return prefs


def _configure(prefs, url, stream=False):
    prefs.base_url = url
    prefs.api_key = "test-key"
    try:
        prefs.model = "mock/oracle-1"
    except TypeError:
        prefs.model = "custom"
        prefs.model_custom = "mock/oracle-1"
    prefs.stream = stream
    prefs.inject_context = True
    prefs.vision_feedback = True
    prefs.confirm_code = False
    prefs.max_steps = 12


try:
    main()
except Exception:
    import traceback
    traceback.print_exc()
    print("RESULT 0/0 passed (crash)", flush=True)
