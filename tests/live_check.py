"""Live end-to-end run against the real OpenRouter API from inside Blender."""

import time

import bpy

bpy.ops.preferences.addon_enable(module="blender_agent")
from blender_agent import agent, tools

prefs = bpy.context.preferences.addons["blender_agent"].preferences
print("LIVE model=%s key=%s tools=%d" % (prefs.resolved_model(),
                                         bool((prefs.api_key or "").strip()),
                                         len(tools.TOOL_SCHEMAS)), flush=True)

agent.SESSION.clear()
started = agent.SESSION.send(
    "Add a red cube named LiveCube, 1.5 metres wide, sitting on top of the default "
    "cube. One create_objects call is enough, then reply in one short line.", prefs)
print("LIVE send=%s" % started, flush=True)

t0 = time.time()
while time.time() - t0 < 180:
    agent.pump_once()
    if not agent.SESSION.busy() and agent.SESSION.status not in ("thinking", "tools"):
        break
    time.sleep(0.02)

sess = agent.SESSION
print("LIVE status=%s detail=%s tokens=%s" % (sess.status, sess.status_detail, sess.usage), flush=True)
for entry in sess.transcript:
    meta = entry.get("meta") or {}
    label = meta.get("name") or entry["kind"]
    print("LIVE  [%s] %s" % (label, " ".join(entry["text"].split())[:170]), flush=True)
print("LIVE objects=%s" % sorted(o.name for o in bpy.context.scene.objects), flush=True)
mat = bpy.data.objects.get("LiveCube")
print("LIVE livecube_material=%s" % (mat.data.materials[0].name if mat and mat.data.materials else None), flush=True)
