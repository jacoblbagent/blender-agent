"""Startup script: open the Blender Agent UI and leave the window for the user.

Logs to /tmp/open_agent_ui.log (snapped apps do not reach the journal here).
"""

import time

import bpy

LOG = "/tmp/open_agent_ui.log"
SHOT = "/tmp/agent_opened.png"
_attempts = [0]


def p(*args):
    line = "OPEN %s" % " ".join(str(a) for a in args)
    print(line, flush=True)
    try:
        with open(LOG, "a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


try:
    bpy.ops.preferences.addon_enable(module="blender_agent")
except Exception as exc:  # noqa: BLE001
    p("addon enable failed:", exc)


def step_workspace():
    from blender_agent import workspace
    try:
        ok, msg = workspace.add_workspace(bpy.context)
        p("workspace:", ok, msg)
        win = bpy.context.window
        if win and win.screen:
            for area in win.screen.areas:
                if area.type == "VIEW_3D":
                    area.spaces.active.show_region_ui = True
    except Exception as exc:  # noqa: BLE001
        p("workspace failed:", exc)
    bpy.app.timers.register(step_panel, first_interval=2.0)
    return None


def step_panel():
    _attempts[0] += 1
    try:
        result = bpy.ops.blender_agent.open_panel("INVOKE_DEFAULT")
        p("panel invoke attempt %d -> %s" % (_attempts[0], result))
        bpy.app.timers.register(step_shot, first_interval=3.0)
        return None
    except Exception as exc:  # noqa: BLE001
        p("panel invoke attempt %d failed: %s" % (_attempts[0], exc))
    if _attempts[0] < 3:
        return 1.5                      # retry: the workspace switch may still be settling
    bpy.app.timers.register(step_shot, first_interval=2.0)
    return None


def step_shot():
    try:
        bpy.ops.screen.screenshot(filepath=SHOT)
        p("screenshot:", SHOT)
    except Exception as exc:  # noqa: BLE001
        p("screenshot failed:", exc)
    p("done at", time.strftime("%H:%M:%S"))
    return None


bpy.app.timers.register(step_workspace, first_interval=2.0)
