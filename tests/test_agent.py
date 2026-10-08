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


# --------------------------------------------------------------- test plan --

def main():
    from blender_agent import agent, openrouter, tools, preferences

    heading("1. registration")
    check("addon module imported", hasattr(bpy.types, "BLENDER_AGENT_PT_agent"))
    check("panel category", bpy.types.BLENDER_AGENT_PT_agent.bl_category == "Agent")
    check("operator send", hasattr(bpy.ops.blender_agent, "send"))
    check("operator set_model", hasattr(bpy.ops.blender_agent, "set_model"))
    check("operator quick_ask", hasattr(bpy.ops.blender_agent, "quick_ask"))
    check("operator add_workspace", hasattr(bpy.ops.blender_agent, "add_workspace"))
    check("preferences registered", "blender_agent" in bpy.context.preferences.addons)
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

    heading("9. message trimming")
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
