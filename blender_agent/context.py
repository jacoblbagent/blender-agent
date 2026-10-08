"""Scene introspection - what the agent knows about the live session."""

import bpy


def _vec(v):
    return [round(float(c), 4) for c in v]


def _detail_level(obj):
    if obj.type == "MESH":
        try:
            return "%dv/%dt" % (len(obj.data.vertices), len(obj.data.polygons))
        except AttributeError:
            return "mesh"
    if obj.type == "LIGHT":
        return "%s %.0fW" % (obj.data.type, obj.data.energy)
    if obj.type == "CAMERA":
        return "%.0fmm" % obj.data.lens
    if obj.type == "ARMATURE":
        return "%d bones" % len(obj.data.bones)
    if obj.type == "CURVE":
        return "%d splines" % len(obj.data.splines)
    return obj.type.lower()


def object_line(obj):
    flags = []
    if obj.hide_viewport:
        flags.append("hidden")
    if obj.hide_render:
        flags.append("no-render")
    if obj.parent:
        flags.append("parent=%s" % obj.parent.name)
    loc = ", ".join("%.3f" % c for c in obj.matrix_world.translation)
    s = "%-34s %-9s %-10s @ (%s)" % (obj.name, obj.type, _detail_level(obj), loc)
    mats = getattr(getattr(obj, "data", None), "materials", None)
    if mats:
        names = [m.name for m in mats if m]
        if names:
            s += "  mat=%s" % ",".join(names)
    if obj.modifiers:
        s += "  mod=%s" % ",".join(m.type for m in obj.modifiers)
    if flags:
        s += "  [%s]" % " ".join(flags)
    return s


def scene_context(max_objects=80, detail=True):
    """Compact text summary of the whole session, suitable for an LLM prompt."""
    scn = bpy.context.scene
    if scn is None:
        return "No active scene."

    lines = []
    lines.append("=== BLENDER AGENT - LIVE SESSION CONTEXT ===")
    lines.append("Blender %s | mode: %s | file: %s%s" % (
        bpy.app.version_string,
        getattr(bpy.context, "mode", "?"),
        bpy.data.filepath or "(unsaved)",
        " *modified*" if bpy.data.is_dirty else "",
    ))
    lines.append("scene: %s | unit scale %.3f | frame %d/%d-%d @ %g fps" % (
        scn.name, scn.unit_settings.scale_length, scn.frame_current,
        scn.frame_start, scn.frame_end, scn.render.fps,
    ))
    eng = scn.render.engine
    extra = ""
    if eng.startswith("CYCLES"):
        extra = " samples=%d" % scn.cycles.samples
    elif "EEVEE" in eng:
        extra = " taa=%d" % getattr(scn.eevee, "taa_render_samples", 0)
    lines.append("render: %s %dx%d%s | out=%s | film_transparent=%s" % (
        eng, scn.render.resolution_x, scn.render.resolution_y, extra,
        scn.render.filepath or "(none)", scn.render.film_transparent,
    ))
    lines.append("world: %s" % (scn.world.name if scn.world else "None"))

    objs = list(scn.objects)
    sel = [o.name for o in bpy.context.selected_objects]
    act = bpy.context.view_layer.objects.active
    lines.append("")
    lines.append("objects: %d total | selected: %s | active: %s" % (
        len(objs), ", ".join(sel) if sel else "(none)", act.name if act else "(none)"))
    for obj in objs[:max_objects]:
        try:
            lines.append("  " + object_line(obj))
        except Exception as exc:  # noqa: BLE001 - one odd object must not hide the scene
            lines.append("  %-34s %s (unreadable: %s)" % (obj.name, obj.type, exc))
    if len(objs) > max_objects:
        lines.append("  ... %d more objects" % (len(objs) - max_objects))

    cols = [c.name for c in bpy.data.collections]
    if cols:
        lines.append("")
        lines.append("collections: %s" % ", ".join(cols))

    mats = sorted(m.name for m in bpy.data.materials)
    if mats:
        lines.append("materials: %s" % ", ".join(mats[:40]))

    if detail:
        mode = getattr(bpy.context, "mode", "OBJECT")
        if act and mode != "OBJECT":
            lines.append("")
            lines.append("active-object detail (edit/%s mode):" % mode)
            lines.append(object_details(act.name))
    lines.append("=== END CONTEXT ===")
    return "\n".join(lines)


def object_details(name):
    obj = bpy.data.objects.get(name)
    if not obj:
        return "No object named %r" % name
    out = ["object %s (%s)" % (obj.name, obj.type)]
    out.append("location %s | rotation euler(deg) %s | scale %s" % (
        _vec(obj.location),
        [round(float(c) * 57.29578, 2) for c in obj.rotation_euler],
        _vec(obj.scale),
    ))
    out.append("dimensions %s | visible=%s | selectable=%s | parent=%s" % (
        _vec(obj.dimensions), not obj.hide_viewport, not obj.hide_select,
        obj.parent.name if obj.parent else "None",
    ))
    out.append("collections: %s" % ", ".join(c.name for c in obj.users_collection))
    if obj.data is not None:
        data = obj.data
        if obj.type == "MESH":
            out.append("mesh %s: %d verts, %d edges, %d polys, %d materials" % (
                data.name, len(data.vertices), len(data.edges), len(data.polygons),
                len(data.materials)))
            if data.uv_layers:
                out.append("uv layers: %s" % ", ".join(u.name for u in data.uv_layers))
            groups = [g.name for g in obj.vertex_groups]
            if groups:
                out.append("vertex groups: %s" % ", ".join(groups))
            if data.shape_keys:
                out.append("shape keys: %s" % ", ".join(k.name for k in data.shape_keys.key_blocks))
        elif obj.type == "LIGHT":
            out.append("light: type=%s energy=%.1f color=%s" % (
                data.type, data.energy, _vec(data.color)))
        elif obj.type == "CAMERA":
            out.append("camera: lens=%.1fmm sensor=%.1f shift=%s clip=%.2f-%.1f" % (
                data.lens, data.sensor_width, _vec(data.shift),
                data.clip_start, data.clip_end))
    for slot in getattr(obj, "material_slots", []) or []:
        if slot.material:
            out.append("material slot %r -> %s" % (slot.name, material_summary(slot.material)))
    for mod in obj.modifiers:
        params = ", ".join(
            "%s=%s" % (p.identifier, getattr(mod, p.identifier))
            for p in mod.bl_rna.properties
            if not p.is_readonly and p.identifier not in ("name", "type")
            and isinstance(getattr(mod, p.identifier, None), (int, float, bool, str))
        )
        out.append("modifier %s (%s): %s" % (mod.name, mod.type, params[:400]))
    for con in obj.constraints:
        out.append("constraint %s (%s) target=%s" % (
            con.name, con.type, getattr(getattr(con, "target", None), "name", "-")))
    if obj.animation_data and obj.animation_data.action:
        act = obj.animation_data.action
        paths = sorted({fc.data_path for fc in act.fcurves})
        out.append("animation action %s frames %s: %s" % (
            act.name, _vec(act.frame_range), ", ".join(paths[:12])))
    return "\n".join(out)


def material_summary(mat):
    nodes = []
    if mat.use_nodes and mat.node_tree:
        principled = None
        for node in mat.node_tree.nodes:
            if node.type == "BSDF_PRINCIPLED":
                principled = node
        if principled is not None:
            def val(sock):
                s = principled.inputs.get(sock)
                if s is None:
                    return "-"
                v = s.default_value
                try:
                    return "[" + ",".join("%.3f" % c for c in v) + "]"
                except TypeError:
                    return "%.3f" % v
            nodes.append("base=%s rough=%s metal=%s emis=%s alpha=%s ior=%s" % (
                val("Base Color"), val("Roughness"), val("Metallic"),
                val("Emission Strength"), val("Alpha"), val("IOR")))
    return "%s(use_nodes=%s%s)" % (mat.name, mat.use_nodes,
                                   " " + " ".join(nodes) if nodes else "")
