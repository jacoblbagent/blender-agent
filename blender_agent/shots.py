"""Model screenshots: capture what the agent just built and show it in the panel.

The agent already renders previews for vision feedback (``tools.
render_preview_and_view``). This module keeps hold of *the* screenshot for the
model in progress, so the sidebar can show it:

    shots.capture(...)   render a viewport-style shot of the scene's models
    shots.adopt(path)    take an existing render as the current screenshot
    shots.latest()       {"path", "name", "label", "objects", "time", ...}
    shots.icon_id()      Blender preview icon id, so the panel can draw it
    shots.open_in_editor()/shots.open_externally()

Captures go to ``CONFIG/blender_agent/shots/shot_NNNN.png``. They are rendered
with Workbench: no lights are needed, it works in the GUI and in background mode,
and it looks like the viewport Solid shading rather than a finished render.

A screenshot never changes the scene or how the user renders afterwards - every
setting it touches is restored, and the camera it needs is temporary.
"""

import math
import os
import time

import bpy
import bpy.utils.previews          # submodule: must be imported, not just accessed
from mathutils import Vector

# Tools after which "the model changed" - a shot is taken when these succeed.
AUTO_TOOLS = frozenset((
    "create_objects", "duplicate_objects", "modify_objects", "delete_objects",
    "mesh_edit", "boolean", "add_modifiers", "apply_modifiers", "geometry_nodes",
    "set_material", "material_nodes", "set_world", "import_export",
    "scene_ops", "bpy_operator", "execute_blender_python",
))

GEOMETRY = frozenset(("MESH", "CURVE", "SURFACE", "FONT", "META"))

THUMB_SCALE = 6.0            # panel thumbnail ceiling: 32px preview icon * scale
CAM_DIRECTION = (1.0, -1.0, 0.62)   # a three-quarter view
CAM_ASPECT = 9.0 / 16.0
PREVIEW_KEY = "agent_shot"
DEFAULT_WIDTH = 960
WORKBENCH = "BLENDER_WORKBENCH"

_latest = None
_pcoll = None


# ------------------------------------------------------------------ registry --

def latest():
    """The current screenshot as a dict, or None."""
    return dict(_latest) if _latest else None


def icon_id():
    """Preview icon id for the panel, or 0 when there is nothing to draw."""
    if _pcoll is None:
        return 0
    try:
        return _pcoll[PREVIEW_KEY].icon_id or 0
    except (KeyError, AttributeError):
        return 0


def thumb_scale(context=None, ceiling=None):
    """Icon scale that keeps the thumbnail inside the panel it is drawn in.

    Blender draws a custom preview at scale * its own size but reserves less space
    than that for it, and the icon is drawn low in its slot, so an oversized scale
    bleeds out of the box and onto the rows underneath (the file name and the
    transcript). Size it from the sidebar width instead of guessing.
    """
    ceiling = THUMB_SCALE if ceiling is None else ceiling
    icon_width = 32.0
    try:
        icon_width = float(_pcoll[PREVIEW_KEY].icon_size[0]) or icon_width
    except (KeyError, AttributeError, TypeError):
        pass
    avail = 0.0
    try:
        region = getattr(context, "region", None)
        avail = float(getattr(region, "width", 0) or 0)
    except (TypeError, ValueError):
        avail = 0.0
    if avail <= 0:
        return ceiling
    # The region width is in device pixels while the icon is drawn at
    # icon_size * scale * ui_scale, so divide the interface scale back out.
    ui_scale = 1.0
    try:
        ui_scale = float(bpy.context.preferences.system.ui_scale) or 1.0
    except (AttributeError, TypeError, ValueError):
        ui_scale = 1.0
    # Leave room for the box borders, the panel indent and a scrollbar.
    return max(1.5, min(ceiling, (avail - 110.0) / (icon_width * ui_scale)))


def _load_icon(path):
    """(Re)load the PNG as a preview icon. Returns True when it loaded."""
    global _pcoll
    try:
        if _pcoll is None:
            _pcoll = bpy.utils.previews.new()
        if PREVIEW_KEY in _pcoll:
            del _pcoll[PREVIEW_KEY]      # load() refuses to reuse a name
        _pcoll.load(PREVIEW_KEY, path, "IMAGE", force_reload=True)
    except Exception:  # noqa: BLE001 - the panel just shows no thumbnail
        return False
    return True


def _redraw_once():
    try:
        for window in bpy.context.window_manager.windows:
            screen = window.screen
            if screen is None:
                continue
            for area in screen.areas:
                area.tag_redraw()
    except Exception:  # noqa: BLE001 - redraw must never raise
        pass
    return None


# Blender decodes and downsamples the PNG in a background job, so the thumbnail
# only appears after it finishes - a couple of redraws spread over the first few
# seconds keep the panel from sitting on the placeholder until the next redraw
# that happens to come from somewhere else.
_REDRAW_DELAYS = (0.4, 1.2, 2.5, 4.5)
_redraw_chain = [False]


def _next_redraw(delays):
    _redraw_once()
    if not delays:
        _redraw_chain[0] = False
        return None
    try:
        bpy.app.timers.register(lambda: _next_redraw(delays[1:]),
                                first_interval=delays[0])
    except Exception:  # noqa: BLE001
        _redraw_chain[0] = False
    return None


def _kick_redraw():
    """Ask for a few redraws so a fresh screenshot appears on its own."""
    if bpy.app.background or _redraw_chain[0]:
        return
    _redraw_chain[0] = True
    _next_redraw(list(_REDRAW_DELAYS))


def _adopt(path, label, objects):
    global _latest
    if not path or not os.path.exists(path):
        return False, "no screenshot file at %s" % (path or "?")
    size = os.path.getsize(path) or 0
    if not size:
        return False, "screenshot file is empty (%s)" % path
    icon = _load_icon(path)
    _latest = {
        "path": path,
        "name": os.path.basename(path),
        "label": label or "model",
        "objects": [str(o) for o in (objects or [])],
        "time": time.strftime("%H:%M:%S"),
        "size": size,
        "thumb": bool(icon),
    }
    _kick_redraw()
    return True, "screenshot %s (%d KB)" % (_latest["name"], size // 1024)


def adopt(path, label="render", objects=None):
    """Take an existing render on disk as the current model screenshot."""
    return _adopt(path, label, objects)


def clear():
    global _latest
    _latest = None


# ------------------------------------------------------------------- objects --

def names_from_args(args):
    """Object names a tool call was aimed at, so the shot can frame just them."""
    names = []
    if not isinstance(args, dict):
        return names
    targets = args.get("targets") or args.get("target") or args.get("name")
    if isinstance(targets, str):
        names.extend(t for t in (s.strip() for s in targets.split(",")) if t)
    elif isinstance(targets, (list, tuple)):
        names.extend(str(t) for t in targets)
    for spec in args.get("objects") or []:
        if isinstance(spec, dict) and spec.get("name"):
            names.append(str(spec["name"]))
    seen, unique = set(), []
    for name in names:
        if name not in seen:
            seen.add(name)
            unique.append(name)
    return unique


def _resolve(objects):
    """Objects to frame: the named ones when they exist, else every visible model."""
    view_layer = bpy.context.view_layer
    source = view_layer.objects if view_layer else bpy.context.scene.objects
    available = [o for o in source if o is not None]

    def visible_meshes():
        for obj in available:
            if obj.type not in GEOMETRY:
                continue
            try:
                if not obj.visible_get() or obj.hide_render:
                    continue
            except Exception:  # noqa: BLE001
                pass
            yield obj

    if objects:
        wanted = list(objects)
        picked = [o for name in wanted for o in available if o.name == name]
        picked = [o for o in picked if o.type in GEOMETRY]
        if picked:
            return picked
    return list(visible_meshes())


def _diagonal(obj):
    try:
        return float(Vector(obj.dimensions).length)
    except Exception:  # noqa: BLE001
        return 0.0


def _framing_targets(objects):
    """Bounds for the shot, ignoring an oversized outlier such as a ground plane.

    Framing on the combined bounds is right for a model made of several parts, but
    one huge flat object (a 20 m floor under a 2 m model) otherwise shrinks the
    model to a speck. If the largest object dwarfs the next one, it still renders -
    it just does not decide the camera distance.
    """
    if len(objects) < 2:
        return objects
    sizes = sorted(((_diagonal(o), o) for o in objects), key=lambda pair: pair[0],
                   reverse=True)
    biggest, (runner_up, _) = sizes[0], sizes[1]
    if runner_up > 1e-6 and biggest[0] > 3.0 * runner_up:
        return [o for o in objects if o is not biggest[1]]
    return objects


def _principled_colour(material):
    """Base Color from the material's Principled BSDF, if it has one."""
    tree = getattr(material, "node_tree", None)
    if tree is None:
        return None
    for node in tree.nodes:
        if node.type != "BSDF_PRINCIPLED":
            continue
        try:
            value = node.inputs["Base Color"].default_value
        except Exception:  # noqa: BLE001
            return None
        return (float(value[0]), float(value[1]), float(value[2]), 1.0)
    return None


def _sync_viewport_colours(objects):
    """Workbench MATERIAL shading reads material.diffuse_color, not the node tree.

    Copy each Principled Base Color into the viewport colour for the shot, and
    return what to put back. Without this every model renders uniform grey, because
    the agent sets node sockets and never touches the viewport colour.
    """
    saved = {}
    for obj in objects:
        for slot in getattr(obj, "material_slots", ()) or ():
            material = slot.material
            if material is None or material in saved:
                continue
            try:
                original = tuple(material.diffuse_color)
            except Exception:  # noqa: BLE001
                continue
            colour = _principled_colour(material)
            if colour and tuple(round(c, 6) for c in colour) != tuple(round(c, 6) for c in original):
                saved[material] = original
                try:
                    material.diffuse_color = colour
                except Exception:  # noqa: BLE001
                    saved.pop(material, None)
    return saved


def _restore_viewport_colours(saved):
    for material, colour in saved.items():
        try:
            material.diffuse_color = colour
        except Exception:  # noqa: BLE001
            pass


def _bounds(objects):
    """World-space centre and radius of the objects' combined bounds."""
    points = []
    for obj in objects:
        try:
            points.extend(obj.matrix_world @ Vector(corner) for corner in obj.bound_box)
        except Exception:  # noqa: BLE001
            points.append(obj.matrix_world.translation.copy())
    if not points:
        return Vector((0.0, 0.0, 0.0)), 1.0
    low = Vector((min(p.x for p in points), min(p.y for p in points), min(p.z for p in points)))
    high = Vector((max(p.x for p in points), max(p.y for p in points), max(p.z for p in points)))
    center = (low + high) * 0.5
    radius = max((p - center).length for p in points)
    return center, max(radius, 1e-4)


# ------------------------------------------------------------------- capture --

def _make_camera(scene, center, radius, width, height, scale):
    """A temporary camera framed on the model. Returns (object, data)."""
    data = bpy.data.cameras.new("AgentShotCam")
    camera = bpy.data.objects.new("AgentShotCam", data)
    try:
        scene.collection.objects.link(camera)
    except RuntimeError:
        pass
    half_h = data.angle / 2.0
    half_v = math.atan(math.tan(half_h) * (float(height) / float(max(1, width))))
    distance = radius / max(1e-6, math.sin(min(half_h, half_v))) * float(scale)
    direction = Vector(CAM_DIRECTION).normalized()
    camera.location = center + direction * distance
    camera.rotation_euler = (center - camera.location).to_track_quat("-Z", "Y").to_euler()
    data.clip_start = max(0.001, distance * 0.01)
    data.clip_end = distance * 10.0 + radius * 20.0
    return camera


def _shot_dir():
    return bpy.utils.user_resource("CONFIG", path="blender_agent/shots", create=True)


def _next_path():
    directory = _shot_dir()
    used = sum(1 for f in os.listdir(directory)
               if f.startswith("shot_") and f.endswith(".png"))
    return os.path.join(directory, "shot_%04d.png" % (used + 1))


def _save_shading(shading):
    keys = ("type", "light", "color_type", "show_cavity", "show_shadows",
            "background_type", "background_color")
    saved = {}
    for key in keys:
        try:
            value = getattr(shading, key)
        except Exception:  # noqa: BLE001
            continue
        saved[key] = tuple(value) if hasattr(value, "__len__") else value
    return saved


def _restore_shading(shading, saved):
    for key, value in saved.items():
        try:
            setattr(shading, key, value)
        except Exception:  # noqa: BLE001
            pass


def capture(label="model", objects=None, width=DEFAULT_WIDTH, scale=1.15, engine=None):
    """Render a screenshot of the current model. Returns (ok, message)."""
    scene = bpy.context.scene
    targets = _resolve(objects)
    if not targets:
        return False, "nothing to screenshot - no model objects in the scene"

    height = max(1, int(width * CAM_ASPECT))
    center, radius = _bounds(_framing_targets(targets))

    render = scene.render
    shading = scene.display.shading
    keep = {
        "engine": render.engine,
        "resolution": (render.resolution_x, render.resolution_y,
                       render.resolution_percentage),
        "filepath": render.filepath,
        "format": render.image_settings.file_format,
        "film_transparent": render.film_transparent,
        "camera": scene.camera,
        "shading": _save_shading(shading),
        "samples": getattr(scene.cycles, "samples", None) if render.engine == "CYCLES" else None,
    }

    camera = None
    data = None
    colours = {}
    path = _next_path()
    try:
        render.engine = engine or WORKBENCH
        render.resolution_percentage = 100
        render.resolution_x, render.resolution_y = width, height
        render.image_settings.file_format = "PNG"
        render.film_transparent = False
        try:
            shading.light = "STUDIO"
            shading.color_type = "MATERIAL"
            shading.show_cavity = True
            shading.show_shadows = False
            shading.background_type = "VIEWPORT"
            shading.background_color = (0.16, 0.17, 0.19)
        except Exception:  # noqa: BLE001 - engine without workbench shading
            pass
        colours = _sync_viewport_colours(targets)
        camera = _make_camera(scene, center, radius, width, height, scale)
        data = camera.data
        scene.camera = camera
        render.filepath = path
        bpy.ops.render.render(write_still=True)
    except Exception as exc:  # noqa: BLE001 - report, restore, never raise
        return False, "screenshot failed (%s: %s)" % (type(exc).__name__, exc)
    finally:
        render.engine = keep["engine"]
        (render.resolution_x, render.resolution_y,
         render.resolution_percentage) = keep["resolution"]
        render.filepath = keep["filepath"]
        render.image_settings.file_format = keep["format"]
        render.film_transparent = keep["film_transparent"]
        scene.camera = keep["camera"]
        _restore_shading(scene.display.shading, keep["shading"])
        _restore_viewport_colours(colours)
        if keep["samples"] is not None and render.engine == "CYCLES":
            try:
                scene.cycles.samples = keep["samples"]
            except Exception:  # noqa: BLE001
                pass
        if camera is not None:
            try:
                bpy.data.objects.remove(camera, do_unlink=True)
            except Exception:  # noqa: BLE001
                pass
        if data is not None:
            try:
                bpy.data.cameras.remove(data)
            except Exception:  # noqa: BLE001
                pass

    if not os.path.exists(path):
        return False, "screenshot produced no file (%s)" % path
    return _adopt(path, label, [o.name for o in targets])


# --------------------------------------------------------------------- open --

def _image_editor():
    """The first Image Editor space, if the user has one open."""
    try:
        for window in bpy.context.window_manager.windows:
            screen = window.screen
            if screen is None:
                continue
            for area in screen.areas:
                if area.type == "IMAGE_EDITOR":
                    return area.spaces.active
    except Exception:  # noqa: BLE001
        pass
    return None


def open_in_editor():
    """Show the screenshot in an Image Editor, or open it externally."""
    shot = _latest
    if not shot:
        return False, "no screenshot yet"
    space = _image_editor()
    if space is not None:
        try:
            image = bpy.data.images.load(shot["path"], check_existing=True)
            space.image = image
            return True, "loaded %s into the Image Editor" % shot["name"]
        except Exception as exc:  # noqa: BLE001
            return False, "could not load the image (%s)" % exc
    return open_externally()


def open_externally():
    shot = _latest
    if not shot:
        return False, "no screenshot yet"
    try:
        bpy.ops.wm.path_open(filepath=shot["path"])
    except Exception as exc:  # noqa: BLE001
        return False, "could not open %s (%s)" % (shot["path"], exc)
    return True, "opened %s" % shot["path"]


# -------------------------------------------------------------- registration --

def register():
    """Nothing to register: the preview collection is created on the first shot."""


def unregister():
    global _pcoll, _latest
    if _pcoll is not None:
        try:
            bpy.utils.previews.remove(_pcoll)
        except Exception:  # noqa: BLE001
            pass
        _pcoll = None
    _latest = None
