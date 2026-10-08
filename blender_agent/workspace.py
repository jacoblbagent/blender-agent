"""One-click 'Agent' workspace: a layout built for talking to the agent.

Blender 5.x has no data-API to create a workspace (`bpy.data.workspaces.new` does
not exist) and `Region.active_panel_category` is read-only, so the only reliable
route is duplicating the current workspace through the operator with an explicit
area override, then preparing the 3D viewport of the new screen.
"""

import bpy

WORKSPACE_NAME = "Agent"


def _view3d(area):
    return area.type == "VIEW_3D"


def _prepare_screen(screen):
    """Open the sidebar / tune shading on the new workspace's 3D viewport."""
    for area in screen.areas:
        if not _view3d(area):
            continue
        space = area.spaces.active
        try:
            space.show_region_ui = True
            space.show_region_toolbar = False
            space.show_gizmo = True
            space.overlay.show_overlays = True
            space.shading.type = "MATERIAL"
            space.shading.use_scene_lights = True
            space.shading.use_scene_world = True
        except Exception:  # noqa: BLE001
            pass
        return True
    return False


def _prepare_current_screen():
    """One-shot: re-apply sidebar/shading once the new workspace is on screen.

    Blender resets region visibility while activating a freshly duplicated
    workspace, so a single synchronous pass does not stick.
    """
    try:
        window = bpy.context.window
        if window is not None and window.screen is not None:
            _prepare_screen(window.screen)
    except Exception:  # noqa: BLE001
        pass
    return None


def schedule_prepare(delay=0.5):
    if bpy.app.background:
        return
    try:
        bpy.app.timers.register(_prepare_current_screen, first_interval=delay)
    except Exception:  # noqa: BLE001
        pass


def add_workspace(context=None):
    """Create (or reuse) an 'Agent' workspace.

    Returns (ok, message).
    """
    context = context or bpy.context
    window = getattr(context, "window", None)
    if window is None:
        windows = bpy.context.window_manager.windows
        window = windows[0] if windows else None
    if window is None:
        return False, "No window available (headless - workspaces need a GUI)"

    existing = bpy.data.workspaces.get(WORKSPACE_NAME)
    if existing is not None:
        try:
            window.workspace = existing
            if window.screen is not None:
                _prepare_screen(window.screen)
        except Exception as exc:  # noqa: BLE001
            return False, "Workspace switch failed: %s" % exc
        schedule_prepare(0.25)
        return True, "Workspace '%s' already exists" % WORKSPACE_NAME

    screen = window.screen
    if screen is None:
        return False, "No screen available"
    area = next((a for a in screen.areas if _view3d(a)), None) or \
        next((a for a in screen.areas), None)
    if area is None:
        return False, "No area to duplicate from"

    original_name = window.workspace.name
    before = {w.name for w in bpy.data.workspaces}
    try:
        with bpy.context.temp_override(window=window, screen=screen, area=area):
            result = bpy.ops.workspace.duplicate()
    except Exception as exc:  # noqa: BLE001
        return False, "Could not duplicate the workspace: %s" % exc
    if "FINISHED" not in result:
        return False, "Workspace duplicate cancelled"

    # Identify the workspace the operator just created - never rename the source.
    new_workspaces = [w for w in bpy.data.workspaces if w.name not in before]
    if not new_workspaces:
        return False, "Workspace duplicate created nothing"
    ws = new_workspaces[0]
    try:
        ws.name = WORKSPACE_NAME
    except Exception as exc:  # noqa: BLE001
        return False, "Created a workspace but could not rename it: %s" % exc
    try:
        ws.status_text = "Blender Agent"
    except Exception:  # noqa: BLE001
        pass
    try:
        window.workspace = ws
    except Exception:  # noqa: BLE001
        pass
    if window.screen is not None:
        _prepare_screen(window.screen)
    schedule_prepare()
    return True, "Workspace '%s' ready (copy of %s) - N sidebar > Agent tab" % (
        WORKSPACE_NAME, original_name)
