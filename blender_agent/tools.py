"""Tool schemas + dispatch. This is the agent's hands on Blender.

Everything here is designed to run on Blender's main thread (see agent.run_on_main).
Tools return either a string, or {"text": ..., "images": [paths]} so the caller can
feed rendered images back to a vision model.
"""

import ast
import io
import math
import os
import random
import re
import sys
import traceback

import bmesh
import bpy
import mathutils
from mathutils import Euler, Matrix, Quaternion, Vector

# ---------------------------------------------------------------- helpers ----

ALL = "(all)"


def _ok(msg):
    return msg


def _err(msg):
    return "ERROR: %s" % msg


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _vec3(v, default=(0.0, 0.0, 0.0)):
    if v is None:
        return list(default)
    if isinstance(v, (int, float)):
        return [float(v)] * 3
    out = list(default)
    for i, c in enumerate(list(v)[:3]):
        out[i] = _num(c, out[i])
    return out


def _enum_val(prop, name):
    """Resolve a user/LLM-supplied enum value onto a real enum identifier."""
    try:
        items = [i.identifier for i in prop.enum_items]
    except (AttributeError, TypeError):
        return name
    if name in items:
        return name
    key = str(name).strip().lower().replace(" ", "_").replace("-", "_")
    for ident in items:
        if ident.lower() == key or ident.lower().replace("_", "") == key.replace("_", ""):
            return ident
    for ident in items:
        if key and key in ident.lower():
            return ident
    return name


def resolve_objects(targets, default_active=False):
    """Turn a loose target spec into a list of objects.

    Accepts: None / "" -> active (or selected), "all"/"*" -> every object in the
    scene, a name, a wildcard pattern, a list of any of those.
    """
    scn = bpy.context.scene
    if targets in (None, "", [], ALL, "all", "*"):
        if targets in (ALL, "all", "*"):
            return list(scn.objects)
        sel = list(bpy.context.selected_objects)
        if sel:
            return sel
        act = bpy.context.view_layer.objects.active
        return [act] if act else []
    if isinstance(targets, str):
        targets = [targets]
    out, missing = [], []
    for t in targets:
        t = str(t)
        if t in ("selected", "selection"):
            out.extend([o for o in bpy.context.selected_objects if o not in out])
        elif t in ("active",):
            act = bpy.context.view_layer.objects.active
            if act and act not in out:
                out.append(act)
        elif "*" in t or "?" in t:
            import fnmatch
            out.extend([o for o in scn.objects
                        if fnmatch.fnmatch(o.name, t) and o not in out])
        else:
            obj = bpy.data.objects.get(t)
            if obj is None:
                import fnmatch
                cands = [o for o in bpy.data.objects if fnmatch.fnmatch(o.name, t + "*")]
                if len(cands) == 1:
                    obj = cands[0]
            if obj is None:
                missing.append(t)
            elif obj not in out:
                out.append(obj)
    if missing:
        raise ValueError("object(s) not found: %s" % ", ".join(missing))
    return out


def _link(obj, collection_name=None):
    scn = bpy.context.scene
    col = scn.collection
    if collection_name:
        col = bpy.data.collections.get(collection_name)
        if col is None:
            col = bpy.data.collections.new(collection_name)
            scn.collection.children.link(col)
    if obj.name not in col.objects:
        col.objects.link(obj)
    # ensure the object is only in the requested collection
    for c in list(obj.users_collection):
        if c is not col:
            c.objects.unlink(obj)


def _activate(objs, active=None):
    for o in bpy.context.scene.objects:
        o.select_set(False)
    for o in objs:
        o.select_set(True)
    if objs:
        bpy.context.view_layer.objects.active = active or objs[0]


def _spot(value):
    """Resolve a look_at / target argument.

    Accepts an object name, "x,y,z", "x, y, z", a Vector, or [x, y, z] - the tool
    schemas declare this as a string, so both spellings must work.
    """
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        obj = bpy.data.objects.get(text)
        if obj is not None:
            return obj
        parts = [p for p in re.split(r"[,;\s]+", text) if p]
        if len(parts) >= 3:
            try:
                return Vector([float(p) for p in parts[:3]])
            except ValueError:
                obj = bpy.data.objects.get(parts[0])
                if obj is not None:
                    return obj
        return None
    if isinstance(value, (list, tuple)):
        return Vector(_vec3(value))
    return value


def _spot_point(value):
    """World-space point for a look_at argument (object centre or coordinates)."""
    spot = _spot(value)
    if spot is None:
        return None
    if hasattr(spot, "matrix_world"):
        return Vector(spot.matrix_world.translation)
    return Vector(spot)


def _look_at(obj, target, roll=0.0):
    point = _spot_point(target)
    if point is None:
        return
    direction = obj.matrix_world.translation - Vector(point)
    if direction.length < 1e-6:
        return
    obj.rotation_mode = "XYZ"
    obj.rotation_euler = direction.to_track_quat("Z", "Y").to_euler("XYZ")
    obj.rotation_euler.rotate_axis("Z", roll)


def _principled(mat):
    if not mat.use_nodes or not mat.node_tree:
        mat.use_nodes = True
    for node in mat.node_tree.nodes:
        if node.type == "BSDF_PRINCIPLED":
            return node
    node = mat.node_tree.nodes.new("ShaderNodeBsdfPrincipled")
    out = next((n for n in mat.node_tree.nodes if n.type == "OUTPUT_MATERIAL"), None)
    if out is None:
        out = mat.node_tree.nodes.new("ShaderNodeOutputMaterial")
    mat.node_tree.links.new(node.outputs[0], out.inputs["Surface"])
    return node


def _set_sockets(node, values, created_links):
    applied, unknown = [], []
    for key, val in (values or {}).items():
        sock = node.inputs.get(key)
        if sock is None:
            unknown.append(key)
            continue
        if isinstance(val, dict):
            continue
        try:
            if hasattr(sock.default_value, "__len__") and not isinstance(val, str):
                data = _vec3(val)
                if sock.default_value.__len__() == 4:
                    data = data + [1.0]
                sock.default_value = data
            elif isinstance(sock.default_value, bool):
                sock.default_value = bool(val)
            else:
                sock.default_value = float(val) if isinstance(sock.default_value, float) else val
            applied.append(key)
        except (TypeError, ValueError) as exc:
            unknown.append("%s(%s)" % (key, exc))
    return applied, unknown


def _mesh_objects(targets):
    objs = resolve_objects(targets)
    meshes = [o for o in objs if o.type == "MESH"]
    if not meshes:
        raise ValueError("no mesh objects in target set")
    return meshes


def _bm_apply(obj, fn):
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    fn(bm)
    bm.normal_update()
    bm.to_mesh(obj.data)
    bm.free()
    obj.data.update()


def _prims():
    return {
        "plane": lambda **kw: bpy.ops.mesh.primitive_plane_add(**kw),
        "cube": lambda **kw: bpy.ops.mesh.primitive_cube_add(**kw),
        "sphere": lambda **kw: bpy.ops.mesh.primitive_uv_sphere_add(**kw),
        "ico_sphere": lambda **kw: bpy.ops.mesh.primitive_ico_sphere_add(**kw),
        "cylinder": lambda **kw: bpy.ops.mesh.primitive_cylinder_add(**kw),
        "cone": lambda **kw: bpy.ops.mesh.primitive_cone_add(**kw),
        "torus": lambda **kw: bpy.ops.mesh.primitive_torus_add(**kw),
        "circle": lambda **kw: bpy.ops.mesh.primitive_circle_add(**kw),
        "grid": lambda **kw: bpy.ops.mesh.primitive_grid_add(**kw),
        "monkey": lambda **kw: bpy.ops.mesh.primitive_monkey_add(**kw),
    }


# ----------------------------------------------------------------- tools -----

def t_get_scene_context(args):
    from . import context as ctxmod
    return ctxmod.scene_context(max_objects=int(args.get("max_objects") or 80))


def t_list_objects(args):
    from . import context as ctxmod
    objs = list(bpy.context.scene.objects)
    pat = args.get("filter") or ""
    typ = (args.get("type") or "").upper()
    if pat:
        import fnmatch
        objs = [o for o in objs if fnmatch.fnmatch(o.name.lower(), "*%s*" % pat.lower())]
    if typ and typ != "ANY":
        objs = [o for o in objs if o.type == typ]
    if not objs:
        return "No matching objects. Scene has %d objects." % len(bpy.context.scene.objects)
    return "Matched %d object(s):\n" % len(objs) + "\n".join(ctxmod.object_line(o) for o in objs)


def t_get_object_details(args):
    from . import context as ctxmod
    names = args.get("names")
    if isinstance(names, str):
        names = [names]
    objs = resolve_objects(names or "selected")
    return "\n\n".join(ctxmod.object_details(o.name) for o in objs)


def t_create_objects(args):
    specs = args.get("objects") or args.get("specs") or []
    if not specs:
        raise ValueError("provide objects=[{...}]")
    prims = _prims()
    created, notes = [], []
    for spec in specs:
        kind = str(spec.get("type", "cube")).lower().replace(" ", "_")
        name = spec.get("name")
        loc = _vec3(spec.get("location"))
        rot = [math.radians(v) for v in _vec3(spec.get("rotation_degrees") or spec.get("rotation"))]
        scale = _vec3(spec.get("scale"), (1, 1, 1))
        if kind in prims:
            op = prims[kind]
            kw = {"location": loc, "rotation": rot}
            if kind == "cube":
                kw["size"] = _num(spec.get("size"), 2.0)
            elif kind in ("sphere", "ico_sphere", "circle"):
                kw["radius"] = _num(spec.get("radius"), 1.0)
                if kind == "ico_sphere":
                    kw["subdivisions"] = int(spec.get("subdivisions") or 4)
                if kind == "circle":
                    kw["vertices"] = int(spec.get("vertices") or 32)
            elif kind == "cylinder":
                kw["radius"] = _num(spec.get("radius") or spec.get("radius1"), 1.0)
                kw["depth"] = _num(spec.get("depth") or spec.get("height"), 2.0)
                kw["vertices"] = int(spec.get("vertices") or 32)
            elif kind == "cone":
                kw["radius1"] = _num(spec.get("radius") or spec.get("radius1"), 1.0)
                kw["radius2"] = _num(spec.get("radius2"), 0.0)
                kw["depth"] = _num(spec.get("depth") or spec.get("height"), 2.0)
                kw["vertices"] = int(spec.get("vertices") or 32)
            elif kind == "torus":
                kw = {"location": loc, "rotation": rot,
                      "major_radius": _num(spec.get("major_radius"), 1.0),
                      "minor_radius": _num(spec.get("minor_radius"), 0.25),
                      "major_segments": int(spec.get("major_segments") or 48),
                      "minor_segments": int(spec.get("minor_segments") or 12)}
            elif kind == "plane":
                kw["size"] = _num(spec.get("size"), 2.0)
            elif kind == "grid":
                kw["x_subdivisions"] = int(spec.get("x_subdivisions") or 10)
                kw["y_subdivisions"] = int(spec.get("y_subdivisions") or 10)
                kw["size"] = _num(spec.get("size"), 2.0)
            op(**kw)
            obj = bpy.context.active_object
        elif kind == "empty":
            obj = bpy.data.objects.new(name or "Empty", None)
            _link(obj, spec.get("collection"))
            obj.location = loc
        elif kind == "light":
            data = bpy.data.lights.new(name or "Light", type=str(spec.get("light_type", "AREA")).upper())
            data.energy = _num(spec.get("energy"), 1000.0)
            if spec.get("color"):
                data.color = _vec3(spec.get("color"), (1, 1, 1))
            if data.type == "AREA":
                data.size = _num(spec.get("size"), 1.0)
            if data.type == "SUN":
                data.angle = _num(spec.get("angle"), 0.00918)
            if spec.get("power_watts"):
                data.energy = _num(spec["power_watts"])
            obj = bpy.data.objects.new(name or "Light", data)
            _link(obj, spec.get("collection"))
            obj.location = loc
        elif kind == "camera":
            data = bpy.data.cameras.new(name or "Camera")
            data.lens = _num(spec.get("lens"), 50.0)
            data.sensor_width = _num(spec.get("sensor_width"), 36.0)
            obj = bpy.data.objects.new(name or "Camera", data)
            _link(obj, spec.get("collection"))
            obj.location = loc
            if spec.get("look_at"):
                _look_at(obj, spec["look_at"])
            else:
                obj.rotation_euler = rot
            bpy.context.scene.camera = obj
        elif kind == "text":
            data = bpy.data.curves.new(name or "Text", type="FONT")
            data.body = spec.get("body") or spec.get("text") or "Text"
            data.size = _num(spec.get("size"), 1.0)
            data.align_x = _enum_val(data.bl_rna.properties["align_x"], spec.get("align", "CENTER"))
            data.extrude = _num(spec.get("extrude"), 0.0)
            obj = bpy.data.objects.new(name or "Text", data)
            _link(obj, spec.get("collection"))
            obj.location = loc
            obj.rotation_euler = rot
            obj.scale = scale
        elif kind in ("mesh", "custom_mesh"):
            raw_verts = spec.get("mesh_vertices")
            if raw_verts is None and isinstance(spec.get("vertices"), (list, tuple)):
                raw_verts = spec.get("vertices")
            verts = [tuple(_vec3(v)) for v in (raw_verts or [])]
            raw_faces = spec.get("mesh_faces") or spec.get("faces") or []
            faces = [tuple(int(i) for i in f) for f in raw_faces]
            raw_edges = spec.get("mesh_edges") or spec.get("edges") or []
            edges = [tuple(int(i) for i in e) for e in raw_edges]
            if not verts:
                raise ValueError("custom mesh needs mesh_vertices")
            me = bpy.data.meshes.new(name or "Mesh")
            me.from_pydata(verts, edges, faces)
            me.validate()
            me.update()
            obj = bpy.data.objects.new(name or "Mesh", me)
            _link(obj, spec.get("collection"))
            obj.location = loc
        elif kind == "curve":
            cu = bpy.data.curves.new(name or "Curve", type="CURVE")
            cu.dimensions = "3D"
            for pts_spec in spec.get("points") or [[[0, 0, 0], [1, 0, 0]]]:
                sp = cu.splines.new("BEZIER")
                sp.bezier_points.add(len(pts_spec) - 1)
                for bp, p in zip(sp.bezier_points, pts_spec):
                    bp.co = Vector(_vec3(p))
                    bp.handle_left_type = bp.handle_right_type = "AUTO"
            if spec.get("bevel_depth"):
                cu.bevel_depth = _num(spec["bevel_depth"])
            obj = bpy.data.objects.new(name or "Curve", cu)
            _link(obj, spec.get("collection"))
            obj.location = loc
        else:
            notes.append("unknown type %r (skipped)" % kind)
            continue

        if not (bpy.data.objects.get(obj.name) and obj.name in bpy.context.scene.objects):
            _link(obj, spec.get("collection"))
        if name and obj.name != name:
            obj.name = name
        if kind not in ("empty", "light", "camera", "text"):
            obj.scale = scale
        if spec.get("rotation_degrees") and kind in ("empty", "light"):
            obj.rotation_euler = rot
        if spec.get("material"):
            _assign_material([obj], spec["material"])
        if spec.get("parent"):
            par = bpy.data.objects.get(spec["parent"])
            if par:
                obj.parent = par
        if kind in ("plane", "grid", "cube", "mesh", "custom_mesh", "text", "circle", "curve") and obj.type == "MESH":
            for p in obj.data.polygons:
                p.use_smooth = bool(spec.get("smooth", False))
        created.append(obj)
    _activate(created)
    lines = ["created %d object(s):" % len(created)]
    from . import context as ctxmod
    lines += ["  " + ctxmod.object_line(o) for o in created]
    lines += notes
    return "\n".join(lines)


def t_modify_objects(args):
    objs = resolve_objects(args.get("targets"))
    changes = args.get("changes") or args
    notes = []
    for obj in objs:
        if "location" in changes:
            obj.location = Vector(_vec3(changes["location"]))
        if "delta_location" in changes:
            obj.location = obj.location + Vector(_vec3(changes["delta_location"]))
        if "rotation_degrees" in changes:
            obj.rotation_mode = "XYZ"
            obj.rotation_euler = Euler([math.radians(v) for v in _vec3(changes["rotation_degrees"])], "XYZ")
        if "delta_rotation_degrees" in changes:
            obj.rotation_mode = "XYZ"
            obj.rotation_euler.rotate(Euler([math.radians(v) for v in _vec3(changes["delta_rotation_degrees"])], "XYZ"))
        if "scale" in changes:
            obj.scale = Vector(_vec3(changes["scale"], (1, 1, 1)))
        if "scale_uniform" in changes:
            obj.scale = Vector([_num(changes["scale_uniform"], 1.0)] * 3)
        if "dimensions" in changes:
            obj.dimensions = Vector(_vec3(changes["dimensions"], (1, 1, 1)))
        if "name" in changes:
            obj.name = str(changes["name"])
            notes.append("renamed -> %s" % obj.name)
        if "hide_viewport" in changes:
            obj.hide_viewport = bool(changes["hide_viewport"])
        if "hide_render" in changes:
            obj.hide_render = bool(changes["hide_render"])
        if "parent" in changes:
            par = bpy.data.objects.get(changes["parent"]) if changes["parent"] else None
            obj.parent = par
            if par and changes.get("keep_transform", True):
                obj.matrix_parent_inverse = par.matrix_world.inverted()
        if "collection" in changes:
            target = bpy.data.collections.get(changes["collection"])
            if target is None:
                target = bpy.data.collections.new(changes["collection"])
                bpy.context.scene.collection.children.link(target)
            if obj.name not in target.objects:
                target.objects.link(obj)
            for c in list(obj.users_collection):
                if c is not target:
                    c.objects.unlink(obj)
        if "apply_transform" in changes and changes["apply_transform"]:
            obj.data.transform(obj.matrix_basis) if obj.type == "MESH" else None
            obj.location = (0, 0, 0)
            obj.rotation_euler = (0, 0, 0)
            obj.scale = (1, 1, 1)
        if "look_at" in changes:
            _look_at(obj, changes["look_at"])
        if "smooth" in changes and obj.type == "MESH":
            for p in obj.data.polygons:
                p.use_smooth = bool(changes["smooth"])
        if "material" in changes:
            _assign_material([obj], changes["material"])
    lines = ["modified %d object(s): %s" % (len(objs), ", ".join(o.name for o in objs))]
    from . import context as ctxmod
    lines += ["  " + ctxmod.object_line(o) for o in objs[:12]]
    return "\n".join(lines + notes)


def t_delete_objects(args):
    objs = resolve_objects(args.get("targets"))
    names = [o.name for o in objs]
    for obj in objs:
        bpy.data.objects.remove(obj, do_unlink=True)
    return "deleted %d object(s): %s" % (len(names), ", ".join(names))


def t_duplicate_objects(args):
    import fnmatch
    objs = resolve_objects(args.get("targets"))
    count = int(args.get("count") or 1)
    offset = Vector(_vec3(args.get("offset"), (0, 2, 0)))
    made = []
    for i in range(count):
        for obj in objs:
            new = obj.copy()
            if obj.data:
                new.data = obj.data.copy()
            for col in obj.users_collection:
                col.objects.link(new)
            new.location = obj.location + offset * (i + 1)
            pattern = args.get("name_pattern") or "%s_%03d"
            try:
                new.name = pattern % (obj.name, i + 1)
            except TypeError:
                new.name = "%s_%03d" % (obj.name, i + 1)
            made.append(new)
    return "duplicated -> %s" % ", ".join(o.name for o in made)


def t_select_objects(args):
    objs = resolve_objects(args.get("targets"))
    deselect = args.get("deselect_others", True)
    if deselect:
        for o in bpy.context.scene.objects:
            o.select_set(False)
    for o in objs:
        o.select_set(True)
    active = args.get("active")
    if active:
        act = bpy.data.objects.get(active)
        if act:
            bpy.context.view_layer.objects.active = act
    elif objs:
        bpy.context.view_layer.objects.active = objs[0]
    return "selected %d: %s | active=%s" % (
        len(objs), ", ".join(o.name for o in objs),
        bpy.context.view_layer.objects.active.name if bpy.context.view_layer.objects.active else "-")


def _assign_material(objs, mat_spec):
    if isinstance(mat_spec, str):
        mat = bpy.data.materials.get(mat_spec)
        if mat is None:
            mat = bpy.data.materials.new(mat_spec)
    else:
        name = mat_spec.get("name") or "Agent Material"
        mat = bpy.data.materials.get(name)
        if mat is None:
            mat = bpy.data.materials.new(name)
        node = _principled(mat)
        sock = {k: v for k, v in mat_spec.items() if k not in ("name", "sockets", "replace")}
        sock.update(mat_spec.get("sockets") or {})
        if "color" in sock and "Base Color" not in sock:
            sock["Base Color"] = sock.pop("color")
        if isinstance(sock.get("Base Color"), (list, tuple)) and len(sock["Base Color"]) == 3:
            sock["Base Color"] = list(sock["Base Color"]) + [1.0]
        if "alpha" in sock and "Alpha" not in sock:
            sock["Alpha"] = sock.pop("alpha")
        if sock.get("Alpha") is not None and isinstance(sock.get("Alpha"), (int, float)) and sock["Alpha"] < 1.0:
            mat.blend_method = "BLEND"
        _set_sockets(node, sock, None)
    for obj in objs:
        if obj.type == "MESH" or hasattr(obj.data, "materials"):
            if not obj.data.materials:
                obj.data.materials.append(mat)
            else:
                obj.data.materials[0] = mat
    return mat


def t_set_material(args):
    objs = resolve_objects(args.get("targets"))
    spec = args.get("material") or {}
    if isinstance(spec, str):
        spec = {"name": spec}
    mat = _assign_material(objs, spec)
    from . import context as ctxmod
    return "material %s applied to: %s\n  %s" % (
        mat.name, ", ".join(o.name for o in objs), ctxmod.material_summary(mat))


def t_material_nodes(args):
    name = args.get("material")
    mat = bpy.data.materials.get(name) if name else None
    if mat is None:
        objs = resolve_objects(args.get("targets"))
        for o in objs:
            if o.material_slots and o.material_slots[0].material:
                mat = o.material_slots[0].material
                break
    if mat is None:
        raise ValueError("material not found; pass material=<name> or targets=[object]")
    mat.use_nodes = True
    tree = mat.node_tree
    results = []
    for op in args.get("operations") or []:
        kind = (op.get("op") or op.get("action") or "add").lower()
        if kind in ("add", "new", "create"):
            node = tree.nodes.new(str(op["node_type"]))
            if op.get("name"):
                node.name = node.label = op["name"]
            if op.get("location"):
                node.location = tuple(_vec3(op["location"])[:2])
            if op.get("inputs"):
                applied, unknown = _set_sockets(node, op["inputs"], None)
            results.append("added %s as %r" % (node.bl_idname, node.name))
        elif kind in ("link", "connect"):
            src = tree.nodes.get(op["from_node"]) or next(
                (n for n in tree.nodes if n.name == op.get("from_node")), None)
            dst = tree.nodes.get(op.get("to_node"))
            if src is None or dst is None:
                results.append("link failed: node not found (%s -> %s)" % (op.get("from_node"), op.get("to_node")))
                continue
            out_sock = src.outputs.get(op.get("from_socket") or op.get("from_output") or "")
            in_sock = dst.inputs.get(op.get("to_socket") or op.get("to_input") or "")
            if out_sock is None:
                out_sock = src.outputs.get(op.get("from_socket") or "", src.outputs[0])
            if in_sock is None:
                results.append("link failed: no input socket %r on %s" % (op.get("to_socket"), dst.name))
                continue
            tree.links.new(out_sock, in_sock)
            results.append("linked %s.%s -> %s.%s" % (src.name, out_sock.name, dst.name, in_sock.name))
        elif kind in ("set", "update", "inputs"):
            node = tree.nodes.get(op.get("node"))
            if node is None:
                results.append("node %r not found" % op.get("node"))
                continue
            applied, unknown = _set_sockets(node, op.get("inputs") or {}, None)
            results.append("set %s: %s%s" % (node.name, ",".join(applied),
                                             " (unknown: %s)" % ",".join(unknown) if unknown else ""))
        elif kind in ("remove", "delete"):
            node = tree.nodes.get(op.get("node"))
            if node:
                tree.nodes.remove(node)
                results.append("removed %s" % op.get("node"))
    return "material %s now has %d nodes: %s\n%s" % (
        mat.name, len(tree.nodes), ", ".join("%s(%s)" % (n.name, n.bl_idname) for n in tree.nodes),
        "\n".join(results))


def t_add_modifiers(args):
    objs = resolve_objects(args.get("targets"))
    specs = args.get("modifiers") or []
    if not specs and args.get("type"):
        specs = [{"type": args["type"]}]
    if not specs:
        raise ValueError("provide modifiers=[{type: ..., <params>}]")
    lines = []
    for obj in objs:
        for spec in specs:
            mod_type = str(spec.get("type") or spec.get("modifier")).upper()
            mod = obj.modifiers.new(spec.get("name") or mod_type.title(), mod_type)
            for key, val in spec.items():
                if key in ("type", "name", "modifier"):
                    continue
                if not hasattr(mod, key):
                    continue
                prop = mod.bl_rna.properties.get(key)
                if prop is not None and prop.type == "ENUM":
                    val = _enum_val(prop, val)
                if key == "object" and isinstance(val, str):
                    val = bpy.data.objects.get(val)
                try:
                    setattr(mod, key, val)
                except (TypeError, ValueError):
                    pass
            lines.append("%s: +%s (%s)" % (obj.name, mod.name, mod_type))
    return "\n".join(lines)


def t_apply_modifiers(args):
    objs = _mesh_objects(args.get("targets"))
    names = args.get("names")
    done = []
    for obj in objs:
        _activate([obj], obj)
        mods = [m for m in obj.modifiers if not names or m.name in names]
        for mod in mods:
            try:
                bpy.ops.object.modifier_apply(modifier=mod.name)
                done.append("%s/%s" % (obj.name, mod.name))
            except RuntimeError as exc:
                done.append("%s/%s FAILED (%s)" % (obj.name, mod.name, exc))
    return "applied: %s" % ", ".join(done) if done else "no modifiers applied"


def t_mesh_edit(args):
    objs = _mesh_objects(args.get("targets"))
    op = (args.get("operation") or "").lower()
    lines = []
    for obj in objs:
        me = obj.data
        if op in ("subdivide", "subdivide_smooth"):
            cuts = int(args.get("cuts") or 1)
            smooth = _num(args.get("smoothness"), 0.0)
            def fn(bm, cuts=cuts, smooth=smooth):
                edges = [e for e in bm.edges if e.is_manifold] or list(bm.edges)
                bmesh.ops.subdivide_edges(bm, edges=edges, cuts=cuts,
                                          use_grid_fill=True, smooth=smooth)
            _bm_apply(obj, fn)
        elif op == "bevel":
            width = _num(args.get("width"), 0.05)
            segs = int(args.get("segments") or 2)
            def fn(bm, width=width, segs=segs):
                bmesh.ops.bevel(bm, geom=list(bm.verts) + list(bm.edges) + list(bm.faces),
                                offset=width, segments=segs, profile=_num(args.get("profile"), 0.5),
                                affect=args.get("affect", "EDGES"))
            _bm_apply(obj, fn)
        elif op == "inset":
            thick = _num(args.get("thickness"), 0.05)
            depth = _num(args.get("depth"), 0.0)
            def fn(bm, thick=thick, depth=depth):
                faces = [f for f in bm.faces if f.select] or list(bm.faces)
                bmesh.ops.inset_region(bm, faces=faces, thickness=thick, depth=depth, use_even_offset=True)
            _bm_apply(obj, fn)
        elif op in ("extrude", "extrude_region"):
            amount = _num(args.get("amount"), 0.2)
            def fn(bm, amount=amount):
                faces = [f for f in bm.faces if f.select] or list(bm.faces)
                res = bmesh.ops.extrude_face_region(bm, geom=faces)
                verts = [v for v in res["geom"] if isinstance(v, bmesh.types.BMVert)]
                bmesh.ops.translate(bm, verts=verts, vec=Vector(_vec3(args.get("direction"), (0, 0, 1))) * amount)
            _bm_apply(obj, fn)
        elif op in ("triangulate",):
            _bm_apply(obj, lambda bm: bmesh.ops.triangulate(bm, faces=list(bm.faces)))
        elif op in ("merge_by_distance", "remove_doubles", "weld"):
            dist = _num(args.get("distance"), 0.001)
            _bm_apply(obj, lambda bm, d=dist: bmesh.ops.remove_doubles(bm, verts=list(bm.verts), dist=d))
        elif op in ("recalculate_normals", "normals_outside"):
            _bm_apply(obj, lambda bm: bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces)))
        elif op in ("flip_normals",):
            _bm_apply(obj, lambda bm: bmesh.ops.reverse_faces(bm, faces=list(bm.faces)))
        elif op in ("smooth_verts", "relax"):
            iters = int(args.get("iterations") or 4)
            fac = _num(args.get("factor"), 0.5)
            _bm_apply(obj, lambda bm, i=iters, f=fac: bmesh.ops.smooth_vert(
                bm, verts=list(bm.verts), factor=f, use_axis_x=True, use_axis_y=True,
                use_axis_z=True) if i else None)
        elif op in ("dissolve_degenerate", "clean"):
            _bm_apply(obj, lambda bm: bmesh.ops.dissolve_degenerate(bm, dist=_num(args.get("distance"), 0.0001),
                                                                    edges=list(bm.edges)))
        elif op in ("solidify",):
            thick = _num(args.get("thickness"), 0.05)
            _bm_apply(obj, lambda bm, t=thick: bmesh.ops.solidify(bm, geom=list(bm.faces), thickness=t))
        elif op in ("shade_smooth", "shade_flat"):
            smooth = op == "shade_smooth"
            for p in me.polygons:
                p.use_smooth = smooth
        elif op in ("decimate", "remesh"):
            mod = obj.modifiers.new(op.title(), "DECIMATE" if op == "decimate" else "REMESH")
            if op == "decimate":
                mod.ratio = _num(args.get("ratio"), 0.5)
            else:
                mod.voxel_size = _num(args.get("voxel_size"), 0.05)
            bpy.context.view_layer.objects.active = obj
            try:
                bpy.ops.object.modifier_apply(modifier=mod.name)
            except RuntimeError as exc:
                lines.append("%s: %s (left unapplied)" % (obj.name, exc))
        elif op in ("select_all", "deselect"):
            sel = op == "select_all"
            for v in me.vertices:
                v.select = sel
            for f in me.polygons:
                f.select = sel
            for e in me.edges:
                e.select = sel
        else:
            raise ValueError("unknown mesh operation %r" % op)
        obj.data.update()
        lines.append("%s: %s -> %dv/%df" % (obj.name, op, len(me.vertices), len(me.polygons)))
    return "\n".join(lines)


def t_boolean(args):
    target = resolve_objects(args.get("target"))[0]
    if target.type != "MESH":
        raise ValueError("target must be a mesh")
    operands = resolve_objects(args.get("operands"))
    operation = str(args.get("operation", "DIFFERENCE")).upper()
    modifier = target.modifiers.new("Boolean", "BOOLEAN")
    modifier.operation = operation
    modifier.solver = str(args.get("solver", "EXACT")).upper()
    keep = bool(args.get("keep_operands", False))
    results = []
    for op_obj in operands:
        modifier.object = op_obj
        if not args.get("append", False):
            bpy.context.view_layer.objects.active = target
            _activate([target], target)
            try:
                bpy.ops.object.modifier_apply(modifier=modifier.name)
                results.append("%s %s" % (operation, op_obj.name))
            except RuntimeError as exc:
                results.append("FAILED %s (%s)" % (op_obj.name, exc))
    if keep:
        return "boolean on %s: %s (operands kept)" % (target.name, ", ".join(results))
    if not args.get("append", False):
        for op_obj in operands:
            bpy.data.objects.remove(op_obj, do_unlink=True)
    return "boolean on %s: %s -> %dv/%df" % (target.name, ", ".join(results),
                                             len(target.data.vertices), len(target.data.polygons))


def t_set_world(args):
    scn = bpy.context.scene
    world = scn.world or bpy.data.worlds.new("Agent World")
    scn.world = world
    world.use_nodes = True
    tree = world.node_tree
    bg = tree.nodes.get("Background")
    if bg is None:
        bg = tree.nodes.new("ShaderNodeBackground")
        out = tree.nodes.get("World Output") or tree.nodes.new("ShaderNodeOutputWorld")
        tree.links.new(bg.outputs[0], out.inputs["Surface"])
    if args.get("color") is not None:
        col = _vec3(args.get("color"))
        bg.inputs["Color"].default_value = col + [1.0] if len(col) == 3 else col
    if args.get("strength") is not None:
        bg.inputs["Strength"].default_value = _num(args["strength"], 1.0)
    if args.get("hdri") :
        path = args["hdri"]
        if not os.path.exists(path):
            raise ValueError("HDRI not found: %s" % path)
        env = tree.nodes.new("ShaderNodeTexEnvironment")
        env.image = bpy.data.images.load(path)
        tree.links.new(env.outputs["Color"], bg.inputs["Color"])
    if args.get("texture") and not args.get("hdri"):
        tex = tree.nodes.new("ShaderNodeTexSky") if str(args["texture"]).upper() == "SKY" else tree.nodes.new("ShaderNodeTexGradient")
        if hasattr(tex, "sky_type") and args.get("sky_type"):
            tex.sky_type = str(args["sky_type"]).upper()
        tree.links.new(tex.outputs["Color"], bg.inputs["Color"])
    return "world %s: color=%s strength=%s" % (
        world.name, list(bg.inputs["Color"].default_value), bg.inputs["Strength"].default_value)


def t_add_lights(args):
    scn = bpy.context.scene
    specs = args.get("lights") or []
    if args.get("type"):
        specs = [args]
    lines = []
    for spec in specs:
        ltype = str(spec.get("type") or spec.get("light_type") or "AREA").upper()
        if ltype not in ("POINT", "SUN", "SPOT", "AREA"):
            ltype = "AREA"
        data = bpy.data.lights.new(spec.get("name") or ("%s Light" % ltype.title()), type=ltype)
        data.energy = _num(spec.get("energy"), 1000.0 if ltype == "AREA" else 100.0)
        if spec.get("color"):
            data.color = _vec3(spec.get("color"), (1, 1, 1))
        if ltype == "AREA":
            data.size = _num(spec.get("size"), 2.0)
        if ltype == "SPOT":
            data.spot_size = math.radians(_num(spec.get("cone_angle_degrees"), 45.0))
        if ltype == "SUN":
            data.angle = math.radians(_num(spec.get("sun_angle_degrees"), 0.526))
        obj = bpy.data.objects.new(data.name, data)
        _link(obj, spec.get("collection"))
        obj.location = Vector(_vec3(spec.get("location"), (3, -3, 5)))
        look = spec.get("look_at") or spec.get("target")
        if look:
            center = _spot_point(look)
            if center is None:
                lines.append("%s: could not resolve look_at=%r" % (obj.name, look))
                continue
            con = obj.constraints.new("TRACK_TO")
            empty = bpy.data.objects.new("%s Target" % obj.name, None)
            _link(empty, spec.get("collection"))
            empty.location = center
            con.target = empty
            con.track_axis = "TRACK_NEGATIVE_Z"
            con.up_axis = "UP_Y"
            lines.append("%s: %s @ %s tracking %s" % (obj.name, ltype,
                                                      [round(c, 2) for c in obj.location],
                                                      [round(c, 2) for c in center]))
        else:
            obj.rotation_euler = [math.radians(v) for v in _vec3(spec.get("rotation_degrees"))]
            lines.append("%s: %s @ %s energy=%.0f" % (obj.name, ltype, list(obj.location), data.energy))
    return "\n".join(lines) or "no lights created"


def t_add_cameras(args):
    scn = bpy.context.scene
    specs = args.get("cameras") or []
    if args.get("lens") or args.get("location"):
        specs = [args]
    lines = []
    for spec in specs:
        data = bpy.data.cameras.new(spec.get("name") or "Camera")
        data.lens = _num(spec.get("lens"), 50.0)
        data.sensor_width = _num(spec.get("sensor_width"), 36.0)
        if spec.get("sensor_fit"):
            data.sensor_fit = str(spec["sensor_fit"]).upper()
        if spec.get("shift"):
            data.shift_x, data.shift_y = _vec3(spec.get("shift"))[:2]
        if spec.get("ortho_scale"):
            data.type = "ORTHO"
            data.ortho_scale = _num(spec["ortho_scale"])
        obj = bpy.data.objects.new(data.name, data)
        _link(obj, spec.get("collection"))
        obj.location = Vector(_vec3(spec.get("location"), (7, -7, 5)))
        look = spec.get("look_at") or spec.get("target")
        if look:
            point = _spot_point(look)
            if point is None:
                lines.append("%s: could not resolve look_at=%r" % (obj.name, look))
                continue
            _look_at(obj, point, _num(spec.get("roll_degrees"), 0.0) * math.pi / 180.0)
        else:
            obj.rotation_euler = Euler([math.radians(v) for v in _vec3(spec.get("rotation_degrees"))], "XYZ")
        if spec.get("make_active", True):
            scn.camera = obj
        lines.append("%s: %.0fmm @ %s -> %s" % (
            obj.name, data.lens, [round(c, 2) for c in obj.location],
            spec.get("look_at") or spec.get("target") or "explicit rotation"))
    return "\n".join(lines) or "no cameras created"


def t_set_render_settings(args):
    scn = bpy.context.scene
    r = scn.render
    notes = []
    if args.get("engine"):
        eng = str(args["engine"]).upper()
        mapping = {"CYCLES": "CYCLES", "EEVEE": "BLENDER_EEVEE_NEXT", "EEVEE_NEXT": "BLENDER_EEVEE_NEXT",
                   "BLENDER_EEVEE_NEXT": "BLENDER_EEVEE_NEXT", "WORKBENCH": "BLENDER_WORKBENCH",
                   "BLENDER_WORKBENCH": "BLENDER_WORKBENCH"}
        r.engine = mapping.get(eng, eng)
        notes.append("engine=%s" % r.engine)
    if args.get("resolution"):
        res = _vec3(args["resolution"], (1280, 720, 100))
        r.resolution_x, r.resolution_y = int(res[0]), int(res[1])
        r.resolution_percentage = int(res[2]) if res[2] != 100 else r.resolution_percentage
        notes.append("res=%dx%d" % (r.resolution_x, r.resolution_y))
    if args.get("resolution_percentage"):
        r.resolution_percentage = int(args["resolution_percentage"])
    if args.get("samples") is not None:
        n = int(args["samples"])
        if r.engine == "CYCLES":
            scn.cycles.samples = n
            scn.cycles.preview_samples = min(n, 32)
        else:
            try:
                scn.eevee.taa_render_samples = n
            except AttributeError:
                pass
        notes.append("samples=%d" % n)
    if args.get("filepath"):
        r.filepath = args["filepath"]
        notes.append("out=%s" % r.filepath)
    if args.get("film_transparent") is not None:
        r.film_transparent = bool(args["film_transparent"])
    if args.get("fps"):
        r.fps = int(args["fps"])
    if args.get("frame_range"):
        fr = _vec3(args["frame_range"], (1, 250, 0))
        scn.frame_start, scn.frame_end = int(fr[0]), int(fr[1])
    if args.get("image_format"):
        r.image_settings.file_format = str(args["image_format"]).upper()
    if args.get("denoise") is not None:
        for vl in scn.view_layers:
            try:
                vl.cycles.use_denoising = bool(args["denoise"])
            except AttributeError:
                pass
        notes.append("denoise=%s" % args["denoise"])
    if args.get("color_mode"):
        r.image_settings.color_mode = str(args["color_mode"]).upper()
    if args.get("camera"):
        cam = bpy.data.objects.get(args["camera"])
        if cam:
            scn.camera = cam
            notes.append("camera=%s" % cam.name)
    if args.get("world"):
        w = bpy.data.worlds.get(args["world"])
        if w:
            scn.world = w
    return "render settings: " + ", ".join(notes)


def t_render_image(args):
    scn = bpy.context.scene
    if args.get("camera"):
        cam = bpy.data.objects.get(args["camera"])
        if cam:
            scn.camera = cam
    if args.get("frame") is not None:
        scn.frame_set(int(args["frame"]))
    path = args.get("filepath")
    if not path:
        base = bpy.path.abspath(scn.render.filepath or "//render")
        path = base if base.lower().endswith(".png") else base + ".png"
    scn.render.filepath = path
    scn.render.image_settings.file_format = "PNG"
    if not scn.camera:
        return _err("no camera in scene - create one with add_cameras")
    bpy.ops.render.render(write_still=True)
    real = bpy.path.abspath(scn.render.filepath)
    if not os.path.exists(real):
        real = os.path.splitext(real)[0] + ".png"
    size = os.path.getsize(real) if os.path.exists(real) else 0
    if not size:
        return _err("render produced no file at %s" % real)
    from . import shots
    shots.adopt(real, label="render_image")
    return "rendered %s (%d KB) engine=%s %dx%d" % (
        real, size // 1024, scn.render.engine, scn.render.resolution_x, scn.render.resolution_y)


def t_render_preview_and_view(args):
    scn = bpy.context.scene
    if not scn.camera:
        return _err("no camera - create one first (add_cameras)")
    keep = {
        "res": (scn.render.resolution_x, scn.render.resolution_y, scn.render.resolution_percentage),
        "samples": getattr(scn.cycles, "samples", None) if scn.render.engine == "CYCLES" else None,
        "filepath": scn.render.filepath,
        "format": scn.render.image_settings.file_format,
    }
    width = int(args.get("width") or 640)
    scn.render.resolution_percentage = 100
    scn.render.resolution_x = width
    scn.render.resolution_y = max(1, int(width * 0.5625))
    if scn.render.engine == "CYCLES":
        scn.cycles.samples = min(getattr(scn.cycles, "samples", 16), int(args.get("samples") or 16))
    out_dir = bpy.utils.user_resource("CONFIG", path="blender_agent/previews", create=True)
    path = os.path.join(out_dir, "preview_%04d.png" % (len(os.listdir(out_dir)) + 1))
    scn.render.filepath = path
    scn.render.image_settings.file_format = "PNG"
    scn.render.film_transparent = False
    if args.get("camera"):
        cam = bpy.data.objects.get(args["camera"])
        if cam:
            scn.camera = cam
    try:
        bpy.ops.render.render(write_still=True)
    finally:
        scn.render.resolution_x, scn.render.resolution_y, scn.render.resolution_percentage = keep["res"]
        if keep["samples"] is not None and scn.render.engine == "CYCLES":
            scn.cycles.samples = keep["samples"]
        scn.render.filepath = keep["filepath"]
        scn.render.image_settings.file_format = keep["format"]
    real = path
    if not os.path.exists(real):
        return _err("preview render failed (%s)" % real)
    from . import shots
    shots.adopt(real, label="preview")
    return {"text": "preview render of %s (camera %s) at %s - the image is attached, "
                    "look at it and fix problems" % (scn.name, scn.camera.name, real),
            "images": [real]}


def t_animate(args):
    objs = resolve_objects(args.get("targets"))
    path = args.get("property") or "location"
    keys = args.get("keys") or []
    interp = str(args.get("interpolation", "BEZIER")).upper()
    lines = []
    for obj in objs:
        for k in keys:
            frame = int(k.get("frame") or 1)
            val = k.get("value")
            if val is None:
                continue
            attr = getattr(obj, path, None)
            if attr is None and "." in path:
                continue
            if isinstance(val, (list, tuple)) or hasattr(attr, "__len__") and not isinstance(attr, str):
                setattr(obj, path, Vector(_vec3(val)))
            else:
                setattr(obj, path, val)
            obj.keyframe_insert(data_path=path, frame=frame)
        lines.append("%s: %d keys on %s" % (obj.name, len(keys), path))
    for obj in objs:
        if obj.animation_data and obj.animation_data.action:
            for fc in obj.animation_data.action.fcurves:
                if fc.data_path == path:
                    for kp in fc.keyframe_points:
                        kp.interpolation = interp
    return "\n".join(lines)


def t_set_frame(args):
    bpy.context.scene.frame_set(int(args.get("frame") or 1))
    return "frame %d" % bpy.context.scene.frame_current


def t_manage_collections(args):
    scn = bpy.context.scene
    lines = []
    for op in args.get("operations") or []:
        kind = (op.get("op") or "create").lower()
        name = op.get("name")
        col = bpy.data.collections.get(name) if name else None
        if kind == "create":
            if col is None:
                col = bpy.data.collections.new(name or "Collection")
                parent = bpy.data.collections.get(op.get("parent")) if op.get("parent") else scn.collection
                (parent or scn.collection).children.link(col)
                lines.append("created collection %s" % col.name)
            else:
                lines.append("collection %s exists" % col.name)
        elif kind in ("move", "add_objects", "assign"):
            if col is None:
                lines.append("no collection %r" % name)
                continue
            for obj in resolve_objects(op.get("objects")):
                if obj.name not in col.objects:
                    col.objects.link(obj)
                if op.get("exclusive", True):
                    for c in list(obj.users_collection):
                        if c is not col:
                            c.objects.unlink(obj)
            lines.append("moved objects into %s" % col.name)
        elif kind == "delete":
            if col:
                bpy.data.collections.remove(col)
                lines.append("removed collection %s" % name)
        elif kind == "select":
            if col:
                for o in scn.objects:
                    o.select_set(False)
                for o in col.objects:
                    o.select_set(True)
                lines.append("selected %d in %s" % (len(col.objects), col.name))
        elif kind == "remove_empty":
            for c in list(bpy.data.collections):
                if not c.objects and not c.children:
                    bpy.data.collections.remove(c)
                    lines.append("removed empty collection %s" % c.name)
    return "\n".join(lines) or "no collection operations"


def t_geometry_nodes(args):
    objs = _mesh_objects(args.get("targets"))
    group_name = args.get("node_group")
    lines = []
    for obj in objs:
        mod = next((m for m in obj.modifiers if m.type == "NODES"), None)
        if mod is None:
            mod = obj.modifiers.new("GeometryNodes", "NODES")
        if group_name:
            ng = bpy.data.node_groups.get(group_name)
            if ng is None:
                raise ValueError("node group %r not found" % group_name)
            mod.node_group = ng
        lines.append("%s: geometry nodes <- %s" % (
            obj.name, mod.node_group.name if mod.node_group else "none"))
        for key, val in (args.get("inputs") or {}).items():
            if mod.node_group is None:
                break
            sock = None
            for item in mod.node_group.interface.items_tree:
                if getattr(item, "name", None) == key and item.item_type == "SOCKET":
                    sock = item
                    break
            ident = sock.identifier if sock else key
            try:
                if hasattr(mod, ident):
                    setattr(mod, ident, val)
                    lines.append("  %s = %s" % (key, val))
            except (TypeError, ValueError) as exc:
                lines.append("  %s failed: %s" % (key, exc))
    return "\n".join(lines)


def t_bpy_operator(args):
    name = args.get("operator") or args.get("name")
    if not name or "." not in name:
        raise ValueError("operator must be 'category.operator_name' e.g. 'mesh.primitive_cube_add'")
    cat, op_name = name.split(".", 1)
    cat_mod = getattr(bpy.ops, cat, None)
    if cat_mod is None:
        raise ValueError("no operator category %r" % cat)
    func = getattr(cat_mod, op_name, None)
    if func is None:
        raise ValueError("no operator %s.%s" % (cat, op_name))
    params = dict(args.get("parameters") or {})
    # coerce enums/objects onto their real types
    props = getattr(func, "get_rna_type", lambda: None)()
    if props is not None:
        for key in list(params):
            prop = props.properties.get(key)
            if prop is None:
                continue
            if prop.type == "ENUM":
                params[key] = _enum_val(prop, params[key])
            elif prop.type == "POINTER" and isinstance(params[key], str):
                params[key] = bpy.data.objects.get(params[key])
    override = args.get("override") or {}
    try:
        if args.get("select_all_first"):
            for o in bpy.context.scene.objects:
                o.select_set(False)
        if override:
            ctx_kw = {}
            for k, v in override.items():
                ctx_kw[k] = bpy.data.objects.get(v) if k.endswith("object") and isinstance(v, str) else v
            with bpy.context.temp_override(**ctx_kw):
                result = func(**params)
        else:
            result = func(**params)
    except RuntimeError as exc:
        return _err("%s raised: %s" % (name, exc))
    except TypeError as exc:
        return _err("bad parameters for %s: %s" % (name, exc))
    info = ""
    reports = []
    try:
        reports = [m for m in func.__doc__.split("\n") if ":" in m][:2] if func.__doc__ else []
    except Exception:  # noqa: BLE001
        pass
    return "%s -> %s%s" % (name, ", ".join(result) or "FINISHED", info)


def t_search_api(args):
    query = (args.get("query") or "").strip().lower()
    scope = (args.get("scope") or "all").lower()
    limit = int(args.get("limit") or 25)
    hits = []
    if not query:
        raise ValueError("provide a search query")
    if scope in ("all", "operators", "ops"):
        cats = [c for c in dir(bpy.ops) if not c.startswith("_")]
        for cat in cats:
            if query.split()[0] in cat.lower():
                hits.append("operator category: bpy.ops.%s" % cat)
            cat_mod = getattr(bpy.ops, cat, None)
            if cat_mod is None:
                continue
            try:
                names = [n for n in dir(cat_mod) if not n.startswith("_")]
            except Exception:  # noqa: BLE001
                continue
            for n in names:
                if query in n.lower() or (query in cat.lower() and len(hits) < limit):
                    hits.append("bpy.ops.%s.%s" % (cat, n))
            if len(hits) > limit * 3:
                break
    if scope in ("all", "types", "api"):
        for tname in dir(bpy.types):
            if not tname.startswith("_") and query in tname.lower():
                hits.append("bpy.types.%s" % tname)
    if scope in ("all", "data", "modules"):
        for dname in dir(bpy.data):
            if not dname.startswith("_") and query in dname.lower():
                hits.append("bpy.data.%s (collection)" % dname)
        for mname in ("bpy", "bpy.ops", "bpy.types", "bmesh", "mathutils", "bpy.path",
                      "bpy.utils", "bpy.app", "bpy.context"):
            if query in mname:
                hits.append("module %s" % mname)
    seen, out = set(), []
    for h in hits:
        if h not in seen:
            seen.add(h)
            out.append(h)
    if not out:
        return "no API symbol matched %r (try a shorter, more generic term)" % query
    return "%d match(es) for %r:\n%s" % (len(out), query, "\n".join(out[:limit]))


def t_bpy_help(args):
    target = args.get("target")
    if not target:
        raise ValueError("provide target, e.g. 'bpy.ops.mesh.primitive_torus_add' or 'bpy.types.Object'")
    node = bpy
    for part in target.split("."):
        node = getattr(node, part, None)
        if node is None:
            return _err("cannot resolve %r" % target)
    out = []
    doc = (getattr(node, "__doc__", None) or "").strip()
    if doc:
        out.append(doc[:1500])
    rna = None
    try:
        rna = node.get_rna_type() if callable(getattr(node, "get_rna_type", None)) else None
    except Exception:  # noqa: BLE001
        rna = None
    if rna is None and hasattr(node, "bl_rna"):
        rna = node.bl_rna
    if rna is not None:
        out.append("identifier: %s" % rna.identifier)
        props = []
        for p in rna.properties:
            if p.is_readonly and p.identifier not in ("name",):
                continue
            if p.identifier in ("rna_type",):
                continue
            typ = p.type
            extra = ""
            if typ == "ENUM":
                extra = " in %s" % [i.identifier for i in p.enum_items][:12]
            props.append("  %s: %s (%s) default=%s%s" % (
                p.identifier, p.name, typ, getattr(p, "default", "-"), extra))
        out.append("properties/parameters:\n" + "\n".join(props[:80]))
    if not out:
        out.append("no documentation available for %s" % target)
    return "\n".join(out)


def t_execute_python(args):
    code = args.get("code") or ""
    if not code.strip():
        raise ValueError("provide code")
    description = args.get("description") or "python"
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    result_value = None
    error = None
    ns = {
        "bpy": bpy, "bmesh": bmesh, "mathutils": mathutils,
        "Vector": Vector, "Matrix": Matrix, "Euler": Euler, "Quaternion": Quaternion,
        "math": math, "random": random, "os": os, "sys": sys,
        "bpy_context": bpy.context, "scene": bpy.context.scene,
    }
    try:
        tree = ast.parse(code, mode="exec")
        body = tree.body
        last_expr = body[-1] if body and isinstance(body[-1], ast.Expr) else None
        if last_expr is not None:
            exec(compile(ast.Module(body=body[:-1], type_ignores=[]), "<agent>", "exec"), ns)
            expr = ast.Expression(last_expr.value)
            result_value = eval(compile(expr, "<agent>", "eval"), ns)
        else:
            exec(compile(tree, "<agent>", "exec"), ns)
    except Exception:  # noqa: BLE001 - hand the traceback to the model
        error = traceback.format_exc(limit=6)
    finally:
        sys.stdout = old
    printed = buf.getvalue()
    out = []
    if printed:
        out.append("stdout:\n" + printed.strip()[:4000])
    if result_value is not None:
        out.append("result: %s" % repr(result_value)[:2000])
    if error:
        return _err("code failed (%s)\n%s" % (description, error))
    return "\n".join(out) if out else "code ran (%s) with no output - print() or return an expression to see values" % description


def t_file_ops(args):
    op = (args.get("operation") or "").lower()
    path = args.get("filepath")
    if op in ("save", "save_as", "save_mainfile"):
        if path:
            bpy.ops.wm.save_as_mainfile(filepath=bpy.path.abspath(path))
        else:
            if not bpy.data.filepath:
                raise ValueError("no filepath - pass filepath=<abs path>")
            bpy.ops.wm.save_mainfile()
        return "saved %s" % bpy.data.filepath
    if op in ("open", "open_mainfile", "load"):
        if not path or not os.path.exists(bpy.path.abspath(path)):
            raise ValueError("file not found: %s" % path)
        bpy.ops.wm.open_mainfile(filepath=bpy.path.abspath(path))
        return "opened %s (%d objects)" % (bpy.data.filepath, len(bpy.context.scene.objects))
    if op in ("new", "new_file"):
        bpy.ops.wm.read_homefile(use_empty=bool(args.get("empty", False)))
        return "new file (%d objects)" % len(bpy.context.scene.objects)
    if op in ("revert",):
        bpy.ops.wm.revert_mainfile()
        return "reverted %s" % bpy.data.filepath
    raise ValueError("unknown file operation %r" % op)


def t_import_export(args):
    op = (args.get("operation") or "").lower()
    path = args.get("filepath")
    if not path:
        raise ValueError("provide filepath")
    path = bpy.path.abspath(path)
    kind = (args.get("kind") or os.path.splitext(path)[1].lstrip(".")).lower()
    if op in ("import", "load"):
        table = {
            "obj": "wm.obj_import", "fbx": "import_scene.fbx", "gltf": "import_scene.gltf",
            "glb": "import_scene.gltf", "stl": "wm.stl_import", "ply": "wm.ply_import",
            "usd": "wm.usd_import", "usdc": "wm.usd_import", "usdz": "wm.usd_import",
            "abc": "wm.alembic_import", "svg": "import_curve.svg", "dae": "wm.collada_import",
            "blend": "wm.append",
        }
        op_name = table.get(kind)
        if op_name is None:
            raise ValueError("unsupported import kind %r (have: %s)" % (kind, ", ".join(sorted(table))))
        cat, nm = op_name.split(".")
        before = set(bpy.data.objects.keys())
        getattr(getattr(bpy.ops, cat), nm)(filepath=path)
        new = [n for n in bpy.data.objects.keys() if n not in before]
        return "imported %s -> %d new object(s): %s" % (os.path.basename(path), len(new), ", ".join(new[:12]))
    table = {
        "obj": ("wm.obj_export", {"export_selected_objects": bool(args.get("selected_only", False))}),
        "fbx": ("export_scene.fbx", {"use_selection": bool(args.get("selected_only", False))}),
        "gltf": ("export_scene.gltf", {"use_selection": bool(args.get("selected_only", False))}),
        "glb": ("export_scene.gltf", {"use_selection": bool(args.get("selected_only", False))}),
        "stl": ("wm.stl_export", {"export_selected_objects": bool(args.get("selected_only", False))}),
        "ply": ("wm.ply_export", {"use_selection": bool(args.get("selected_only", False))}),
        "usd": ("wm.usd_export", {"selected_objects_only": bool(args.get("selected_only", False))}),
        "abc": ("wm.alembic_export", {"selected": bool(args.get("selected_only", False))}),
        "dae": ("wm.collada_export", {"selected": bool(args.get("selected_only", False))}),
    }
    if kind not in table:
        raise ValueError("unsupported export kind %r (have: %s)" % (kind, ", ".join(sorted(table))))
    op_name, extra = table[kind]
    cat, nm = op_name.split(".")
    params = {"filepath": path}
    params.update({k: v for k, v in extra.items()})
    params.update({k: v for k, v in (args.get("parameters") or {}).items()})
    func = getattr(getattr(bpy.ops, cat), nm)
    try:
        func(**params)
    except TypeError:
        func(filepath=path)
    return "exported %s (%d bytes)" % (path, os.path.getsize(path) if os.path.exists(path) else 0)


def t_undo_redo(args):
    action = (args.get("action") or "undo").lower()
    steps = int(args.get("steps") or 1)
    for _ in range(max(1, steps)):
        if action == "undo":
            if not bpy.ops.ed.undo.poll():
                return "nothing to undo"
            bpy.ops.ed.undo()
        elif action == "redo":
            if not bpy.ops.ed.redo.poll():
                return "nothing to redo"
            bpy.ops.ed.redo()
        elif action == "push":
            bpy.ops.ed.undo_push(message=args.get("label") or "Agent mark")
            return "undo state pushed"
        else:
            raise ValueError("action must be undo, redo or push")
    return "%s x%d done" % (action, steps)


def t_edit_object_mode(args):
    """Generic mode + operator bridge for direct mesh/curve editing workflows."""
    obj = resolve_objects(args.get("target"))[0]
    mode = (args.get("mode") or "EDIT").upper()
    _activate([obj], obj)
    bpy.ops.object.mode_set(mode=mode)
    results = []
    for step in args.get("operations") or []:
        op_name = step.get("operator") or step.get("op")
        if not op_name:
            continue
        cat, nm = op_name.split(".")
        params = dict(step.get("parameters") or {})
        props = getattr(getattr(getattr(bpy.ops, cat), nm), "get_rna_type", lambda: None)()
        if props is not None:
            for key in list(params):
                prop = props.properties.get(key)
                if prop is not None and prop.type == "ENUM":
                    params[key] = _enum_val(prop, params[key])
        try:
            res = getattr(getattr(bpy.ops, cat), nm)(**params)
            results.append("%s -> %s" % (op_name, ", ".join(res)))
        except RuntimeError as exc:
            results.append("%s failed: %s" % (op_name, exc))
    if args.get("exit_edit", True):
        bpy.ops.object.mode_set(mode="OBJECT")
    return "%s: %s\n%s" % (obj.name, mode, "\n".join(results)) if results else "%s set to %s" % (obj.name, mode)


def t_set_units(args):
    scn = bpy.context.scene
    u = scn.unit_settings
    if args.get("system"):
        u.system = str(args["system"]).upper()
    if args.get("scale_length") is not None:
        u.scale_length = _num(args["scale_length"], 1.0)
    if args.get("length_unit"):
        u.length_unit = str(args["length_unit"]).upper()
    return "units: system=%s scale=%.4f length_unit=%s" % (u.system, u.scale_length, u.length_unit)


def t_scene_ops(args):
    scn = bpy.context.scene
    lines = []
    for op in args.get("operations") or []:
        kind = (op.get("op") or "").lower()
        if kind in ("clear", "delete_all"):
            n = len(scn.objects)
            keep = bool(op.get("keep_camera"))
            cam = scn.camera
            for obj in list(scn.objects):
                if keep and obj is cam:
                    continue
                bpy.data.objects.remove(obj, do_unlink=True)
            lines.append("deleted objects (%d)" % n)
        elif kind == "purge":
            removed = 0
            for coll in (bpy.data.meshes, bpy.data.materials, bpy.data.images,
                         bpy.data.node_groups, bpy.data.curves, bpy.data.lights,
                         bpy.data.cameras, bpy.data.actions, bpy.data.collections):
                for item in list(coll):
                    if item.users == 0:
                        coll.remove(item)
                        removed += 1
            lines.append("purged %d orphans" % removed)
        elif kind == "set_active":
            obj = bpy.data.objects.get(op.get("object"))
            if obj:
                bpy.context.view_layer.objects.active = obj
                lines.append("active=%s" % obj.name)
        elif kind == "snap":
            obj = resolve_objects(op.get("objects") or "selected")
            mode = (op.get("mode") or "GROUND").upper()
            from . import context as ctxmod  # noqa: F401
            for o in obj:
                if mode in ("GROUND", "FLOOR"):
                    low = min((o.matrix_world @ Vector(c)).z for c in o.bound_box)
                    o.location.z -= low
                elif mode == "ORIGIN":
                    o.location = (0, 0, 0)
            lines.append("snapped %d (%s)" % (len(obj), mode))
        elif kind == "rename_pattern":
            import re
            pat = op.get("pattern") or "%s"
            for o in resolve_objects(op.get("objects") or "all"):
                o.name = pat % o.name
            lines.append("renamed by pattern %s" % pat)
    return "\n".join(lines) or "no scene operations"


def t_set_active_camera_view(args):
    scn = bpy.context.scene
    cam = None
    if args.get("camera"):
        cam = bpy.data.objects.get(args["camera"])
    cam = cam or scn.camera
    if cam is None:
        return _err("no camera")
    if args.get("location"):
        cam.location = Vector(_vec3(args["location"]))
    look = args.get("look_at") or args.get("target")
    if look:
        point = _spot_point(look)
        if point is None:
            return _err("could not resolve look_at=%r" % look)
        _look_at(cam, point, _num(args.get("roll_degrees"), 0.0) * math.pi / 180.0)
    if args.get("lens"):
        cam.data.lens = _num(args["lens"], cam.data.lens)
    if args.get("frame_scene"):
        objs = [o for o in scn.objects if o.type == "MESH"]
        if objs:
            pts = [o.matrix_world @ Vector(c) for o in objs for c in o.bound_box]
            center = sum(pts, Vector()) / len(pts)
            radius = max((p - center).length for p in pts)
            cam.location = center + Vector(_vec3(args.get("view_direction"), (1, -1, 0.6))).normalized() * radius * 2.6
            _look_at(cam, center)
            cam.data.lens = radius * 1.4
            return "framed %d objects: center=%s radius=%.2f cam=%s lens=%.0fmm" % (
                len(objs), [round(c, 2) for c in center], radius,
                [round(c, 2) for c in cam.location], cam.data.lens)
    scn.camera = cam
    return "camera %s @ %s lens=%.0fmm" % (cam.name, [round(c, 2) for c in cam.location], cam.data.lens)


# --------------------------------------------------------------- schemas -----

def _fn(name, description, properties=None, required=None):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties or {},
                       "required": required or []}}}


TOOL_SCHEMAS = [
    _fn("get_scene_context", "Full live snapshot of the Blender session: version, mode, scene, "
        "render settings, every object with transform/materials/modifiers, collections, materials, "
        "selection. Call this first when you need to know the current state.", {}),
    _fn("list_objects", "List objects, optionally filtered by name substring and object type.",
        {"filter": {"type": "string"}, "type": {"type": "string",
         "description": "MESH, LIGHT, CAMERA, EMPTY, CURVE, ARMATURE, TEXT, or ANY"}}),
    _fn("get_object_details", "Deep detail on specific objects: transforms, dimensions, mesh stats, "
        "material slots, modifiers with parameters, constraints, animation.",
        {"names": {"type": "array", "items": {"type": "string"},
                   "description": "names or wildcards; omit for the current selection"}}),
    _fn("create_objects", "Create one or many objects in a single call: primitives (plane, cube, "
        "sphere, ico_sphere, cylinder, cone, torus, circle, grid, monkey), empty, light, camera, "
        "text, curve, or custom mesh from vertices/faces.",
        {"objects": {"type": "array", "items": {"type": "object", "properties": {
            "type": {"type": "string"}, "name": {"type": "string"},
            "location": {"type": "array", "items": {"type": "number"}},
            "rotation_degrees": {"type": "array", "items": {"type": "number"}},
            "scale": {"type": "array", "items": {"type": "number"}},
            "size": {"type": "number"}, "radius": {"type": "number"},
            "radius2": {"type": "number"}, "depth": {"type": "number"},
            "height": {"type": "number"},
            "major_radius": {"type": "number"}, "minor_radius": {"type": "number"},
            "major_segments": {"type": "integer"}, "minor_segments": {"type": "integer"},
            "vertices": {"type": "integer"}, "subdivisions": {"type": "integer"},
            "x_subdivisions": {"type": "integer"}, "y_subdivisions": {"type": "integer"},
            "light_type": {"type": "string", "description": "POINT, SUN, SPOT, AREA"},
            "energy": {"type": "number", "description": "watts for Blender lights"},
            "color": {"type": "array", "items": {"type": "number"}},
            "lens": {"type": "number"}, "sensor_width": {"type": "number"},
            "look_at": {"type": "string",
                        "description": "object name, or a point written as \"x,y,z\""},
            "body": {"type": "string", "description": "text content for type=text"},
            "extrude": {"type": "number"},
            "points": {"type": "array",
                       "description": "for curve: list of polylines, each a list of [x,y,z]",
                       "items": {"type": "array", "items": {"type": "number"}}},
            "bevel_depth": {"type": "number"},
            "mesh_vertices": {"type": "array",
                              "description": "for type=mesh: vertex positions [[x,y,z], ...]",
                              "items": {"type": "array", "items": {"type": "number"}}},
            "mesh_faces": {"type": "array",
                           "description": "for type=mesh: polygons as vertex-index lists",
                           "items": {"type": "array", "items": {"type": "integer"}}},
            "mesh_edges": {"type": "array",
                           "description": "for type=mesh: edges as vertex-index pairs",
                           "items": {"type": "array", "items": {"type": "integer"}}},
            "material": {"type": "object",
                         "description": "material spec, e.g. {\"name\": \"Red\", "
                                        "\"Base Color\": [0.8,0.05,0.05]}",
                         "additionalProperties": True},
            "collection": {"type": "string"}, "parent": {"type": "string"},
            "smooth": {"type": "boolean"}}}},
         }, ["objects"]),
    _fn("modify_objects", "Transform or re-parent existing objects: location, delta_location, "
        "rotation_degrees, delta_rotation_degrees, scale, scale_uniform, dimensions, name, parent, "
        "collection, hide_viewport, hide_render, look_at, smooth, material.",
        {"targets": {"type": "string",
                     "description": "names/wildcards, 'selected', 'all', or omit for selection"},
         "changes": {"type": "object", "description": "one or more of the fields above",
                    "additionalProperties": True},
         "location": {"type": "array", "items": {"type": "number"}},
         "delta_location": {"type": "array", "items": {"type": "number"}},
         "rotation_degrees": {"type": "array", "items": {"type": "number"}},
         "delta_rotation_degrees": {"type": "array", "items": {"type": "number"}},
         "scale": {"type": "array", "items": {"type": "number"}},
         "scale_uniform": {"type": "number"},
         "dimensions": {"type": "array", "items": {"type": "number"}},
         "name": {"type": "string"}, "parent": {"type": "string"},
         "collection": {"type": "string"},
         "look_at": {"type": "string",
                     "description": "object name, or a point written as \"x,y,z\""},
         "hide_viewport": {"type": "boolean"}, "hide_render": {"type": "boolean"},
         "smooth": {"type": "boolean"}, "apply_transform": {"type": "boolean"},
         "material": {"type": "object",
                      "description": "material spec, e.g. {\"name\": \"Red\", \"Base Color\": "
                                     "[0.8,0.05,0.05], \"Roughness\": 0.35}",
                      "additionalProperties": True}}, ["targets"]),
    _fn("delete_objects", "Delete objects from the scene.",
        {"targets": {"type": "string",
                     "description": "names/wildcards, 'selected' or 'all'"}}, ["targets"]),
    _fn("duplicate_objects", "Duplicate objects with an optional offset per copy.",
        {"targets": {"type": "string", "description": "names or 'selected'"},
         "count": {"type": "integer"},
         "offset": {"type": "array", "items": {"type": "number"}},
         "name_pattern": {"type": "string"}}, ["targets"]),
    _fn("select_objects", "Select objects and set the active object.",
        {"targets": {"type": "string",
                     "description": "names/wildcards, 'all' or 'selected'"},
         "active": {"type": "string"}, "deselect_others": {"type": "boolean"}}, ["targets"]),
    _fn("set_material", "Create or update a Principled material and assign it to objects. "
        "Keys: name, color/Base Color [r,g,b] (0-1), Roughness, Metallic, Alpha, IOR, Emission Color, "
        "Emission Strength, Transmission Weight, Coat Weight, Sheen Weight, Specular IOR Level; "
        "any other Principled socket name also works, or use sockets={...}.",
        {"targets": {"type": "string", "description": "names or 'selected'"},
         "material": {"type": "object", "description": "e.g. {\"name\":\"Red\",\"Base Color\":[0.8,0.05,0.05],\"Roughness\":0.35}",
                         "additionalProperties": True}},
        ["targets", "material"]),
    _fn("material_nodes", "Edit a material's shader node tree: add nodes, link sockets, set inputs, "
        "remove nodes. Node types e.g. ShaderNodeTexImage, ShaderNodeTexNoise, ShaderNodeBump, "
        "ShaderNodeMapping, ShaderNodeMixRGB, ShaderNodeTexCoord.",
        {"material": {"type": "string"},
         "targets": {"type": "string", "description": "fallback object to take material from"},
         "operations": {"type": "array", "items": {"type": "object", "properties": {
             "op": {"type": "string", "description": "add | link | set | remove"},
             "node": {"type": "string"}, "node_type": {"type": "string"},
             "name": {"type": "string"}, "location": {"type": "array", "items": {"type": "number"}},
             "inputs": {"type": "object", "description": "socket name -> value",
                        "additionalProperties": True},
             "from_node": {"type": "string"}, "from_socket": {"type": "string"},
             "to_node": {"type": "string"}, "to_socket": {"type": "string"}}}}},
        ["operations"]),
    _fn("add_modifiers", "Add modifiers to objects with parameters, e.g. "
        "{\"type\":\"SUBSURF\",\"levels\":2}, BEVEL, SOLIDIFY, ARRAY, MIRROR, DISPLACE, SHRINKWRAP, "
        "SIMPLE_DEFORM, CURVE, BOOLEAN, REMESH, DECIMATE, WELD, SKIN, SCREW, WAVE, CLOTH, PARTICLES "
        "(use modifiers=[{...}] for several).",
        {"targets": {"type": "string", "description": "names or 'selected'"},
         "modifiers": {"type": "array", "items": {"type": "object", "description": "modifier spec, e.g. {\"type\":\"SUBSURF\",\"levels\":2}"}},
         "type": {"type": "string"}, "name": {"type": "string"},
         "object": {"type": "string"}}, ["targets"]),
    _fn("apply_modifiers", "Apply (bake) modifiers on mesh objects.",
        {"targets": {"type": "string", "description": "names or 'selected'"},
         "names": {"type": "array", "items": {"type": "string"}}},
        ["targets"]),
    _fn("mesh_edit", "Direct mesh surgery on objects. operation: subdivide, subdivide_smooth, bevel, "
        "inset, extrude, triangulate, merge_by_distance, recalculate_normals, flip_normals, "
        "smooth_verts, dissolve_degenerate, solidify, shade_smooth, shade_flat, decimate, remesh, "
        "select_all, deselect.",
        {"targets": {"type": "string", "description": "mesh object names or 'selected'"},
         "operation": {"type": "string"}, "cuts": {"type": "integer"},
         "smoothness": {"type": "number"}, "width": {"type": "number"},
         "segments": {"type": "integer"}, "thickness": {"type": "number"},
         "depth": {"type": "number"}, "amount": {"type": "number"},
         "direction": {"type": "array", "items": {"type": "number"}},
         "distance": {"type": "number"}, "ratio": {"type": "number"},
         "voxel_size": {"type": "number"}, "iterations": {"type": "integer"},
         "factor": {"type": "number"}}, ["targets", "operation"]),
    _fn("boolean", "Boolean-apply operands into a target mesh (DIFFERENCE, UNION, INTERSECT).",
        {"target": {"type": "string", "description": "single mesh object name"},
         "operands": {"type": "string", "description": "names or 'selected'"},
         "operation": {"type": "string"}, "solver": {"type": "string"},
         "keep_operands": {"type": "boolean"}}, ["target", "operands"]),
    _fn("set_world", "Set the world background: color [r,g,b], strength, hdri=<abs path to .hdr/.exr>, "
        "or texture='SKY'/'GRADIENT'.",
        {"color": {"type": "array", "items": {"type": "number"}}, "strength": {"type": "number"},
         "hdri": {"type": "string"}, "texture": {"type": "string"}, "sky_type": {"type": "string"}}),
    _fn("add_lights", "Add lights: list of {type: POINT|SUN|SPOT|AREA, name, location, energy, "
        "color, size, look_at} - look_at aims the light at a point or object via a track constraint.",
        {"lights": {"type": "array", "items": {"type": "object"}},
         "type": {"type": "string"}, "location": {"type": "array", "items": {"type": "number"}},
         "look_at": {"type": "string",
                     "description": "object name, or a point written as \"x,y,z\""},
         "energy": {"type": "number"}, "size": {"type": "number"},
         "color": {"type": "array", "items": {"type": "number"}}, "name": {"type": "string"}}),
    _fn("add_cameras", "Add cameras: list of {name, lens, sensor_width, location, look_at, "
        "roll_degrees, ortho_scale}. The last one becomes scene.camera unless make_active=false.",
        {"cameras": {"type": "array", "items": {"type": "object"}},
         "lens": {"type": "number"}, "location": {"type": "array", "items": {"type": "number"}},
         "look_at": {"type": "string",
                     "description": "object name, or a point written as \"x,y,z\""},
         "name": {"type": "string"}, "ortho_scale": {"type": "number"}}),
    _fn("set_render_settings", "Configure rendering: engine (CYCLES, BLENDER_EEVEE_NEXT, "
        "BLENDER_WORKBENCH), resolution [x,y], resolution_percentage, samples, filepath, "
        "film_transparent, denoise, fps, frame_range [start,end], image_format, color_mode, "
        "camera, world.",
        {"engine": {"type": "string"}, "resolution": {"type": "array", "items": {"type": "number"}},
         "resolution_percentage": {"type": "integer"}, "samples": {"type": "integer"},
         "filepath": {"type": "string"}, "film_transparent": {"type": "boolean"},
         "denoise": {"type": "boolean"}, "fps": {"type": "integer"},
         "frame_range": {"type": "array", "items": {"type": "number"}},
         "image_format": {"type": "string"}, "color_mode": {"type": "string"},
         "camera": {"type": "string"}, "world": {"type": "string"}}),
    _fn("render_image", "Render a still to disk and report the file path.",
        {"filepath": {"type": "string"}, "camera": {"type": "string"},
         "frame": {"type": "integer"}}, []),
    _fn("render_preview_and_view", "Fast low-resolution render that is sent back to you AS AN IMAGE "
        "so you can visually inspect your work. Use this to check composition, lighting and modelling "
        "before finishing. Requires the model to accept images.",
        {"camera": {"type": "string"}, "width": {"type": "integer"},
         "samples": {"type": "integer"}}, []),
    _fn("animate", "Keyframe a property over time. property examples: location, rotation_euler, "
        "scale, data.lens, energy. keys=[{frame, value}] where value is a number or [x,y,z].",
        {"targets": {"type": "string", "description": "names or 'selected'"},
         "property": {"type": "string"},
         "keys": {"type": "array", "items": {"type": "object", "properties": {
             "frame": {"type": "integer"},
             "value": {"type": "array", "items": {"type": "number"}}}}},
         "interpolation": {"type": "string", "description": "BEZIER, LINEAR, CONSTANT, EASE_IN_OUT"}},
        ["targets", "keys"]),
    _fn("set_frame", "Jump the timeline to a frame.", {"frame": {"type": "integer"}}, ["frame"]),
    _fn("manage_collections", "Create/delete collections and move objects between them. "
        "operations=[{op: create|move|delete|select|remove_empty, name, parent, objects}]",
        {"operations": {"type": "array", "items": {"type": "object"}}}, ["operations"]),
    _fn("geometry_nodes", "Add or retarget a geometry-nodes modifier and set its inputs.",
        {"targets": {"type": "string", "description": "names or 'selected'"},
         "node_group": {"type": "string"},
         "inputs": {"type": "object", "description": "input socket name -> value",
                               "additionalProperties": True}}, ["targets"]),
    _fn("bpy_operator", "Call ANY Blender operator directly (bpy.ops.*). Use search_api first to find "
        "the exact name. Example: operator='object.shade_smooth', "
        "parameters={'keep_sharp_edges': true}.",
        {"operator": {"type": "string", "description": "category.name e.g. mesh.primitive_torus_add"},
         "parameters": {"type": "object", "additionalProperties": True},
         "override": {"type": "object", "additionalProperties": True},
         "select_all_first": {"type": "boolean"}}, ["operator"]),
    _fn("search_api", "Search Blender's Python API for operators, types and data collections - "
        "how you discover anything not covered by the high-level tools.",
        {"query": {"type": "string"}, "scope": {"type": "string", "description": "all | operators | types | data"},
         "limit": {"type": "integer"}}, ["query"]),
    _fn("bpy_help", "Show the documentation and full parameter list for any API path, e.g. "
        "'bpy.ops.wm.save_as_mainfile', 'bpy.types.Object', 'bpy.ops.mesh.primitive_torus_add'.",
        {"target": {"type": "string"}}, ["target"]),
    _fn("execute_blender_python", "Run arbitrary Python inside Blender with bpy, bmesh, mathutils "
        "(Vector/Matrix/Euler/Quaternion), math, random available. Print values or end with an "
        "expression to see results. This is full, unrestricted access - use it for anything the "
        "other tools do not cover: armatures, shape keys, drivers, custom data, particles, "
        "simulations, procedural generation, batch edits.",
        {"code": {"type": "string"}, "description": {"type": "string"}}, ["code"]),
    _fn("edit_object_mode", "Switch an object into EDIT/SCULPT/OBJECT mode and run a sequence of "
        "operators in that mode (for tools that need edit mode), then return to object mode.",
        {"target": {"type": "string"}, "mode": {"type": "string"},
         "operations": {"type": "array", "items": {"type": "object"}},
         "exit_edit": {"type": "boolean"}}, ["target"]),
    _fn("file_ops", "Blender file operations: save, save_as, open, new, revert.",
        {"operation": {"type": "string"}, "filepath": {"type": "string"}, "empty": {"type": "boolean"}},
        ["operation"]),
    _fn("import_export", "Import or export assets (obj, fbx, gltf/glb, stl, ply, usd, abc, dae, svg, blend).",
        {"operation": {"type": "string", "description": "import or export"},
         "filepath": {"type": "string"}, "kind": {"type": "string"},
         "selected_only": {"type": "boolean"},
         "parameters": {"type": "object", "additionalProperties": True}},
        ["operation", "filepath"]),
    _fn("undo_redo", "Undo, redo or push an undo mark in the Blender history.",
        {"action": {"type": "string", "description": "undo | redo | push"},
         "steps": {"type": "integer"}, "label": {"type": "string"}}),
    _fn("set_units", "Unit system and scale: system METRIC/IMPERIAL/NONE, scale_length, length_unit.",
        {"system": {"type": "string"}, "scale_length": {"type": "number"},
         "length_unit": {"type": "string"}}),
    _fn("scene_ops", "Housekeeping: clear the scene, purge orphan data, snap objects to the ground, "
        "rename by pattern, set the active object.",
        {"operations": {"type": "array", "items": {"type": "object"}}}, ["operations"]),
    _fn("set_active_camera_view", "Move/aim the active camera, change its lens, or frame the whole "
        "scene (frame_scene=true).",
        {"camera": {"type": "string"}, "location": {"type": "array", "items": {"type": "number"}},
         "look_at": {"type": "string",
                     "description": "object name, or a point written as \"x,y,z\""},
         "lens": {"type": "number"}, "roll_degrees": {"type": "number"},
         "frame_scene": {"type": "boolean"},
         "view_direction": {"type": "array", "items": {"type": "number"}}}),
]

TOOL_SCHEMAS_BY_NAME = {t["function"]["name"]: t for t in TOOL_SCHEMAS}

_DISPATCH = {
    "get_scene_context": t_get_scene_context,
    "list_objects": t_list_objects,
    "get_object_details": t_get_object_details,
    "create_objects": t_create_objects,
    "modify_objects": t_modify_objects,
    "delete_objects": t_delete_objects,
    "duplicate_objects": t_duplicate_objects,
    "select_objects": t_select_objects,
    "set_material": t_set_material,
    "material_nodes": t_material_nodes,
    "add_modifiers": t_add_modifiers,
    "apply_modifiers": t_apply_modifiers,
    "mesh_edit": t_mesh_edit,
    "boolean": t_boolean,
    "set_world": t_set_world,
    "add_lights": t_add_lights,
    "add_cameras": t_add_cameras,
    "set_render_settings": t_set_render_settings,
    "render_image": t_render_image,
    "render_preview_and_view": t_render_preview_and_view,
    "animate": t_animate,
    "set_frame": t_set_frame,
    "manage_collections": t_manage_collections,
    "geometry_nodes": t_geometry_nodes,
    "bpy_operator": t_bpy_operator,
    "search_api": t_search_api,
    "bpy_help": t_bpy_help,
    "execute_blender_python": t_execute_python,
    "edit_object_mode": t_edit_object_mode,
    "file_ops": t_file_ops,
    "import_export": t_import_export,
    "undo_redo": t_undo_redo,
    "set_units": t_set_units,
    "scene_ops": t_scene_ops,
    "set_active_camera_view": t_set_active_camera_view,
}

READ_ONLY = {
    "get_scene_context", "list_objects", "get_object_details", "search_api", "bpy_help",
}
NEEDS_UNDO = set(_DISPATCH) - READ_ONLY - {"undo_redo", "render_image", "set_frame",
                                           "set_active_camera_view", "search_api"}


def tool_names():
    return sorted(_DISPATCH)


def schema_problems():
    """Validate the tool schemas against what strict providers accept.

    Anthropic (via Bedrock) rejects a tool whose property has no JSON Schema
    ``type`` with an opaque "HTTP 400: Provider returned error", which costs a
    debugging round trip. Anything that can be checked locally is checked here.
    """
    problems = []

    def walk(props, tool, path):
        for name, spec in props.items():
            where = "%s.%s%s" % (tool, path, name)
            if not isinstance(spec, dict) or not spec:
                problems.append("%s: empty schema %r" % (where, spec))
                continue
            declared = spec.get("type")
            if declared is None:
                problems.append("%s: no type" % where)
            if declared == "array" and "items" not in spec:
                problems.append("%s: array without items" % where)
            if declared == "object" and "properties" not in spec and "additionalProperties" not in spec:
                problems.append("%s: object without properties" % where)
            items = spec.get("items")
            if declared == "array" and isinstance(items, dict):
                if items.get("type") is None and "oneOf" not in items and "anyOf" not in items:
                    problems.append("%s: array items without type" % (where + "[]"))
                if items.get("type") == "object":
                    walk(items.get("properties") or {}, tool, path + name + "[].")

    for schema in TOOL_SCHEMAS:
        fn = schema["function"]
        params = fn["parameters"]
        if params.get("type") != "object":
            problems.append("%s: parameters must be an object" % fn["name"])
        walk(params.get("properties") or {}, fn["name"], "")
    return problems


def mark_undo(name, args=None):
    """Push a labelled undo state so one agent action can be undone in one step.

    Also initialises the undo system in background/headless sessions.
    """
    label = "Blender Agent: %s" % name
    try:
        bpy.ops.ed.undo_push(message=label)
        return True
    except Exception:  # noqa: BLE001
        return False


def execute(name, arguments):
    """Run one tool. Returns str or {"text":.., "images":[...]}. Never raises."""
    fn = _DISPATCH.get(name)
    if fn is None:
        return _err("unknown tool %r. Available: %s" % (name, ", ".join(tool_names())))
    args = arguments if isinstance(arguments, dict) else {}
    try:
        out = fn(args)
    except ValueError as exc:
        return _err(str(exc))
    except Exception:  # noqa: BLE001
        return _err("tool %s crashed:\n%s" % (name, traceback.format_exc(limit=5)))
    if isinstance(out, dict):
        return out
    return str(out)
