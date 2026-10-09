"""Blender UI: the Agent sidebar panel, chat rendering and operators."""

import os
import re
import threading
import time
import webbrowser

import bpy
from bpy.props import BoolProperty, StringProperty
from bpy.types import Menu, Operator, Panel

from . import agent, openrouter, preferences, workspace
from .agent import SESSION

CATEGORY = "Agent"
PKG = __package__ or "blender_agent"

_SPINNER = "|/-\\"
_last_redraw = [0.0]


def redraw_all(force=False):
    """Throttled redraw of every area so the panel follows the live stream."""
    now = time.time()
    if not force and now - _last_redraw[0] < 0.07:
        return
    _last_redraw[0] = now
    try:
        wm = bpy.context.window_manager
        for window in wm.windows:
            screen = window.screen
            if screen is None:
                continue
            for area in screen.areas:
                area.tag_redraw()
    except Exception:  # noqa: BLE001 - redraw must never raise
        pass



def icon_problems():
    """Validate every icon literal used by this add-on against Blender's enum.

    An unknown icon raises inside draw(), and Blender then shows a *partially
    drawn* panel instead of an error - a silent, confusing failure. Checked here
    and asserted by the test suite.
    """
    try:
        valid = {i.identifier for i in bpy.types.UILayout.bl_rna.functions["label"]
                 .parameters["icon"].enum_items}
    except Exception:  # noqa: BLE001
        return []
    here = os.path.dirname(os.path.abspath(__file__))
    problems = []
    for fname in ("ui.py", "preferences.py"):
        try:
            with open(os.path.join(here, fname), encoding="utf-8") as fh:
                lines = fh.readlines()
        except OSError:
            continue
        for number, line in enumerate(lines, 1):
            if "icon=" not in line:
                continue
            for literal in re.findall(r'"([A-Z][A-Z0-9_]*)"', line):
                if literal not in valid:
                    problems.append("%s:%d icon %r is not a Blender icon" %
                                    (fname, number, literal))
    return problems

def _wrap(text, width=54, max_lines=None):
    lines = []
    for para in (text or "").split("\n"):
        if not para.strip():
            lines.append("")
            continue
        words, cur = para.split(), ""
        for word in words:
            if len(cur) + len(word) + 1 > width and cur:
                lines.append(cur)
                cur = word
            else:
                cur = (cur + " " + word).strip()
        if cur:
            lines.append(cur)
    if max_lines and len(lines) > max_lines:
        lines = lines[:max_lines] + ["... (%d more lines)" % (len(lines) - max_lines)]
    return lines


def _prefs(context):
    return context.preferences.addons[PKG].preferences


def _status_line():
    status = SESSION.status
    spin = _SPINNER[int(time.time() * 8) % 4]
    if status in ("thinking", "tools"):
        icon = spin
    elif status == "error":
        icon = "!"
    elif status == "done":
        icon = "="
    elif status == "approval":
        icon = "?"
    else:
        icon = "."
    return "%s %s%s" % (icon, status, (" - " + SESSION.status_detail) if SESSION.status_detail else "")


# ----------------------------------------------------------- panel draw ------

def _draw_transcript(layout, wm, limit):
    box = layout.box()
    entries = SESSION.transcript[-limit:]
    if not entries:
        col = box.column()
        col.label(text="No messages yet.", icon="INFO")
        col.label(text="Ask for a change in the box below.")
        return
    for entry in entries:
        kind = entry["kind"]
        meta = entry["meta"]
        if kind == "user":
            head = box.row()
            head.label(text="You", icon="USER")
            count = int(meta.get("images") or 0)
            if count:
                head.label(text="%d photo%s" % (count, "" if count == 1 else "s"),
                           icon="IMAGE_DATA")
            head.label(text=entry["time"])
        elif kind == "assistant":
            head = box.row()
            head.label(text="Agent%s" % (" writing" if meta.get("streaming") else ""),
                       icon="OUTLINER_OB_LIGHT")
        elif kind == "tool":
            row = box.row()
            row.label(text="%s  %s" % (meta.get("name", "tool"),
                                       "%.1fs" % meta.get("seconds", 0.0)),
                      icon="CHECKMARK" if meta.get("ok") else "CANCEL")
            if meta.get("args"):
                for line in _wrap(meta["args"], 58, 2):
                    box.label(text=line)
            if not wm.agent_show_full:
                box = box
        elif kind == "error":
            head = box.row()
            head.label(text="Error", icon="ERROR")
        elif kind == "approval":
            head = box.row()
            head.label(text="Approval required", icon="QUESTION")
        else:
            head = box.row()
            head.label(text=entry["time"], icon="INFO")

        max_lines = None if wm.agent_show_full else (10 if kind in ("user", "assistant") else 6)
        text = entry["text"]
        if wm.agent_show_full and kind == "tool":
            text = text
        for line in _wrap(text, 56, max_lines):
            box.label(text=line or " ")
        box.separator(factor=0.4)


def _draw_shot(layout, context):
    """The screenshot of the model the agent is building."""
    from . import shots
    shot = shots.latest()
    box = layout.box()
    head = box.row(align=True)
    head.label(text="Model Screenshot", icon="IMAGE_DATA")
    if shot:
        head.label(text=shot["time"])
    if not shot:
        box.label(text="No screenshot yet.", icon="INFO")
        box.operator("blender_agent.screenshot_model", text="Screenshot Model",
                     icon="RENDER_STILL")
        return
    icon = shots.icon_id()
    if icon:
        box.separator(factor=0.3)
        row = box.row(align=True)
        row.alignment = "CENTER"
        row.template_icon(icon_value=icon, scale=shots.thumb_scale(context))
        box.separator(factor=0.3)
    count = len(shot["objects"])
    box.label(text="%s - %d object%s" % (shot["name"], count,
                                         "" if count == 1 else "s"))
    row = box.row(align=True)
    row.operator("blender_agent.screenshot_model", text="Again", icon="RENDER_STILL")
    row.operator("blender_agent.open_screenshot", text="Open", icon="FILE_IMAGE")


def _draw_panel(layout, context, transcript_limit=None):
    wm = context.window_manager
    prefs = _prefs(context)

    head = layout.row(align=True)
    head.label(text=_status_line())
    if SESSION.usage["total_tokens"]:
        head.label(text="%s tok" % SESSION.usage["total_tokens"])
    model_row = layout.row(align=True)
    model_row.label(text=prefs.resolved_model() or "no model selected",
                    icon="OUTLINER_OB_EMPTY")
    model_row.label(text="key %s" % ("set" if (prefs.api_key or "").strip() else "missing"),
                    icon="CHECKMARK" if (prefs.api_key or "").strip() else "ERROR")

    chat_row = layout.row(align=True)
    chat_row.menu("BLENDER_AGENT_MT_chats", text=SESSION.active_title(), icon="DOWNARROW_HLT")
    chat_row.operator("blender_agent.new_chat", text="", icon="ADD")
    chat_row.operator("blender_agent.delete_chat", text="", icon="TRASH")

    row = layout.row(align=True)
    row.scale_y = 1.5
    if SESSION.busy():
        row.operator("blender_agent.stop", icon="PAUSE")
    else:
        row.operator("blender_agent.send", icon="PLAY")
    row.operator("blender_agent.clear_history", text="", icon="TRASH")
    row.operator("blender_agent.undo_last", text="", icon="LOOP_BACK")

    _draw_shot(layout, context)

    if SESSION.pending_approval:
        warn = layout.box()
        warn.alert = True
        warn.label(text="Run this code?", icon="ERROR")
        for line in _wrap(SESSION.pending_approval["args"].get("code", ""), 50, 8):
            warn.label(text=line or " ")
        r = warn.row(align=True)
        r.operator("blender_agent.approve_code", icon="CHECKMARK")
        r.operator("blender_agent.deny_code", icon="CANCEL")

    limit = transcript_limit or (12 if not wm.agent_show_more else 40)
    _draw_transcript(layout, wm, limit)

    col = layout.column(align=True)
    if not (prefs.api_key or "").strip():
        box = col.box()
        box.alert = True
        box.label(text="Add your OpenRouter key to start", icon="KEYINGSET")
        box.prop(prefs, "api_key", text="")
        box.operator("blender_agent.open_key_page", icon="URL")

    col.prop(wm, "agent_input", text="Ask", icon="TEXT")
    row = col.row(align=True)
    row.prop(wm, "agent_show_more", text="History", toggle=True)
    row.prop(wm, "agent_show_full", text="Full Output", toggle=True)
    row.prop(wm, "agent_show_tools", text="Tools", toggle=True)

    from . import bridge as bridge_mod
    if bridge_mod.is_running():
        remote = col.row(align=True)
        remote.label(text="Remote", icon="INTERNET")
        remote.operator("blender_agent.bridge_copy", text="Copy Link", icon="COPY_ID").what = "link"
        remote.operator("blender_agent.bridge_token", text="", icon="FILE_REFRESH")
        remote.operator("blender_agent.bridge_toggle", text="", icon="PAUSE")
    else:
        col.operator("blender_agent.bridge_toggle", text="Serve On Tailnet", icon="INTERNET")


class BLENDER_AGENT_PT_agent(Panel):
    bl_idname = "BLENDER_AGENT_PT_agent"
    bl_label = "Blender Agent"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = CATEGORY

    def draw(self, context):
        _draw_panel(self.layout, context)


class BLENDER_AGENT_PT_model(Panel):
    bl_idname = "BLENDER_AGENT_PT_model"
    bl_label = "Model"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = CATEGORY
    bl_parent_id = "BLENDER_AGENT_PT_agent"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        prefs = _prefs(context)
        col = self.layout.column(align=True)
        row = col.row(align=True)
        row.prop(prefs, "model", text="")
        row.operator("blender_agent.refresh_models", text="", icon="FILE_REFRESH")
        col.operator("blender_agent.open_model_browser", icon="ZOOM_SELECTED")
        if not openrouter.models():
            col.operator("blender_agent.refresh_models", icon="URL")


class BLENDER_AGENT_PT_tools(Panel):
    bl_idname = "BLENDER_AGENT_PT_tools"
    bl_label = "Capabilities"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = CATEGORY
    bl_parent_id = "BLENDER_AGENT_PT_agent"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        from . import tools as tools_mod
        col = self.layout.column(align=True)
        if context.window_manager.agent_show_tools:
            for name in tools_mod.tool_names():
                col.label(text=name)
        else:
            col.label(text="%d tools available" % len(tools_mod.TOOL_SCHEMAS))
        col.label(text="Any bpy operator via bpy_operator")
        col.label(text="Any Python via execute_blender_python")


class BLENDER_AGENT_PT_settings(Panel):
    bl_idname = "BLENDER_AGENT_PT_settings"
    bl_label = "Agent Settings"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = CATEGORY
    bl_parent_id = "BLENDER_AGENT_PT_agent"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        prefs = _prefs(context)
        col = self.layout.column(align=True)
        col.prop(prefs, "temperature")
        col.prop(prefs, "max_steps")
        col.prop(prefs, "inject_context")
        col.prop(prefs, "undo_per_tool")
        col.prop(prefs, "confirm_code")
        col.prop(prefs, "vision_feedback")
        col.label(text="Key: %s" % ("set" if (prefs.api_key or "").strip() else "missing"),
                  icon="CHECKMARK" if (prefs.api_key or "").strip() else "ERROR")
        col.operator("blender_agent.test_connection", icon="PLUGIN")
        col.operator("blender_agent.add_workspace", icon="WORKSPACE")


# ------------------------------------------------------------- operators -----

class BLENDER_AGENT_OT_send(Operator):
    bl_idname = "blender_agent.send"
    bl_label = "Send To Blender Agent"
    bl_description = "Send the prompt to the agent"

    def execute(self, context):
        wm = context.window_manager
        prefs = _prefs(context)
        text = (wm.agent_input or "").strip()
        if not text:
            self.report({"INFO"}, "Nothing to send")
            return {"CANCELLED"}
        if not prefs.resolved_model():
            self.report({"ERROR"}, "Pick a model in the Model panel")
            return {"CANCELLED"}
        if not (prefs.api_key or "").strip():
            self.report({"ERROR"}, "Set your OpenRouter API key first")
            return {"CANCELLED"}
        wm.agent_input = ""
        if SESSION.send(text, prefs):
            self.report({"INFO"}, "Agent working: %s" % prefs.resolved_model())
        return {"FINISHED"}


class BLENDER_AGENT_OT_stop(Operator):
    bl_idname = "blender_agent.stop"
    bl_label = "Stop Blender Agent"

    def execute(self, context):
        SESSION.cancel()
        return {"FINISHED"}


class BLENDER_AGENT_OT_clear(Operator):
    bl_idname = "blender_agent.clear_history"
    bl_label = "Clear This Chat"

    def execute(self, context):
        if SESSION.busy():
            SESSION.cancel()
        SESSION.clear()
        redraw_all(force=True)
        return {"FINISHED"}


class BLENDER_AGENT_OT_new_chat(Operator):
    bl_idname = "blender_agent.new_chat"
    bl_label = "New Chat"
    bl_description = ("Start a fresh conversation that shares no context with the "
                      "current one")

    def execute(self, context):
        if SESSION.busy():
            self.report({"ERROR"}, "Stop the agent before starting a new chat")
            return {"CANCELLED"}
        chat = SESSION.new_chat()
        redraw_all(force=True)
        self.report({"INFO"}, "New chat started (%d open)" % len(SESSION.chats))
        return {"FINISHED"} if chat else {"CANCELLED"}


class BLENDER_AGENT_OT_open_chat(Operator):
    bl_idname = "blender_agent.open_chat"
    bl_label = "Open Chat"
    bl_description = "Switch to another conversation"

    chat_id: StringProperty()

    def execute(self, context):
        if SESSION.busy():
            self.report({"ERROR"}, "Stop the agent before switching chats")
            return {"CANCELLED"}
        if not SESSION.switch_chat(self.chat_id):
            self.report({"ERROR"}, "That chat no longer exists")
            return {"CANCELLED"}
        redraw_all(force=True)
        self.report({"INFO"}, "Chat: %s" % SESSION.active_title())
        return {"FINISHED"}


class BLENDER_AGENT_OT_rename_chat(Operator):
    bl_idname = "blender_agent.rename_chat"
    bl_label = "Rename Chat"
    bl_description = "Give this conversation a name"

    title: StringProperty(name="Name", default="")

    def invoke(self, context, event):
        self.title = SESSION.active_title()
        return context.window_manager.invoke_props_dialog(self, width=420)

    def draw(self, context):
        self.layout.prop(self, "title", text="Name")

    def execute(self, context):
        if not SESSION.rename_chat(self.title):
            self.report({"ERROR"}, "Give the chat a name")
            return {"CANCELLED"}
        redraw_all(force=True)
        self.report({"INFO"}, "Renamed to %s" % SESSION.active_title())
        return {"FINISHED"}


class BLENDER_AGENT_OT_delete_chat(Operator):
    bl_idname = "blender_agent.delete_chat"
    bl_label = "Delete Chat"
    bl_description = ("Delete this conversation (the last remaining chat is emptied, "
                      "not removed)")

    def execute(self, context):
        if SESSION.busy():
            self.report({"ERROR"}, "Stop the agent before deleting a chat")
            return {"CANCELLED"}
        if not SESSION.delete_chat():
            self.report({"ERROR"}, "Could not delete that chat")
            return {"CANCELLED"}
        redraw_all(force=True)
        self.report({"INFO"}, "Deleted - now on %s" % SESSION.active_title())
        return {"FINISHED"}


class BLENDER_AGENT_MT_chats(Menu):
    bl_idname = "BLENDER_AGENT_MT_chats"
    bl_label = "Chats"

    def draw(self, context):
        layout = self.layout
        chats = SESSION.chat_list()
        if not chats:
            layout.label(text="No chats yet", icon="INFO")
        for chat in reversed(chats):          # newest first
            label = chat["title"]
            if chat["messages"]:
                label = "%s  (%d)" % (label, chat["messages"])
            op = layout.operator("blender_agent.open_chat", text=label,
                                 icon="CHECKMARK" if chat["active"] else "BLANK1")
            op.chat_id = chat["id"]
        layout.separator()
        layout.operator("blender_agent.new_chat", icon="ADD")
        layout.operator("blender_agent.rename_chat", icon="TEXT")
        layout.operator("blender_agent.delete_chat", icon="TRASH")


class BLENDER_AGENT_OT_undo_last(Operator):
    bl_idname = "blender_agent.undo_last"
    bl_label = "Undo Agent Change"
    bl_description = "Undo the last change made in this Blender session"

    def execute(self, context):
        try:
            if not bpy.ops.ed.undo.poll():
                self.report({"INFO"}, "Nothing to undo")
                return {"CANCELLED"}
            bpy.ops.ed.undo()
        except RuntimeError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class BLENDER_AGENT_OT_approve(Operator):
    bl_idname = "blender_agent.approve_code"
    bl_label = "Run Code"

    def execute(self, context):
        SESSION.resolve_approval(True)
        redraw_all(force=True)
        return {"FINISHED"}


class BLENDER_AGENT_OT_deny(Operator):
    bl_idname = "blender_agent.deny_code"
    bl_label = "Deny"

    def execute(self, context):
        SESSION.resolve_approval(False)
        redraw_all(force=True)
        return {"FINISHED"}


class BLENDER_AGENT_OT_refresh_models(Operator):
    bl_idname = "blender_agent.refresh_models"
    bl_label = "Refresh Model List"
    bl_description = "Fetch the live OpenRouter model catalogue"

    def execute(self, context):
        prefs = _prefs(context)
        self.report({"INFO"}, "Fetching models...")

        def job():
            try:
                openrouter.fetch_models(prefs.api_key, prefs.base_url)
                SESSION.record("info", "Model catalogue: %d models." % len(openrouter.models()))
            except Exception as exc:  # noqa: BLE001
                SESSION.record("error", "Model fetch failed: %s" % exc)
            redraw_all(force=True)

        threading.Thread(target=job, name="blender-agent-models", daemon=True).start()
        return {"FINISHED"}


class BLENDER_AGENT_OT_test_connection(Operator):
    bl_idname = "blender_agent.test_connection"
    bl_label = "Test OpenRouter Connection"

    def execute(self, context):
        prefs = _prefs(context)
        ok, msg = openrouter.validate_key(prefs)
        SESSION.record("info" if ok else "error", "OpenRouter: %s" % msg)
        self.report({"INFO"} if ok else {"ERROR"}, msg)
        return {"FINISHED"} if ok else {"CANCELLED"}


class BLENDER_AGENT_OT_open_key_page(Operator):
    bl_idname = "blender_agent.open_key_page"
    bl_label = "Open OpenRouter Keys Page"

    def execute(self, context):
        webbrowser.open("https://openrouter.ai/keys")
        return {"FINISHED"}


class BLENDER_AGENT_OT_model_browser(Operator):
    bl_idname = "blender_agent.open_model_browser"
    bl_label = "Browse Models"
    bl_property = "filter"

    filter: StringProperty(name="Search", default="")

    def invoke(self, context, event):
        if not openrouter.models():
            try:
                openrouter.fetch_models(_prefs(context).api_key, _prefs(context).base_url)
            except Exception as exc:  # noqa: BLE001
                self.report({"ERROR"}, "Could not fetch models: %s" % exc)
        return context.window_manager.invoke_props_dialog(self, width=520)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "filter", icon="VIEWZOOM")
        needle = (self.filter or "").lower()
        matches = [m for m in openrouter.models()
                   if needle in m["id"].lower() or needle in m["name"].lower()]
        layout.label(text="%d model(s)" % len(matches))
        grid = layout.grid_flow(columns=2, even_columns=True)
        for m in matches[:60]:
            label = m["name"][:44]
            grid.operator("blender_agent.set_model", text=label).model_id = m["id"]

    def execute(self, context):
        return {"FINISHED"}


class BLENDER_AGENT_OT_set_model(Operator):
    bl_idname = "blender_agent.set_model"
    bl_label = "Set Model"

    model_id: StringProperty()

    def execute(self, context):
        prefs = _prefs(context)
        if self.model_id == "custom":
            prefs.model = "custom"
        else:
            prefs.model = self.model_id
        SESSION.record("info", "Model set to %s" % self.model_id)
        self.report({"INFO"}, "Model: %s" % self.model_id)
        return {"FINISHED"}


class BLENDER_AGENT_OT_add_workspace(Operator):
    bl_idname = "blender_agent.add_workspace"
    bl_label = "Add Agent Workspace"

    def execute(self, context):
        ok, msg = workspace.add_workspace(context)
        self.report({"INFO"} if ok else {"ERROR"}, msg)
        SESSION.record("info" if ok else "error", msg)
        return {"FINISHED"} if ok else {"CANCELLED"}


class BLENDER_AGENT_OT_open_panel(Operator):
    bl_idname = "blender_agent.open_panel"
    bl_label = "Blender Agent Panel"
    bl_description = "Open the Blender Agent chat panel as a floating window"

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=640)

    def draw(self, context):
        _draw_panel(self.layout, context, transcript_limit=5)

    def execute(self, context):
        return {"FINISHED"}


class BLENDER_AGENT_OT_quick_ask(Operator):
    bl_idname = "blender_agent.quick_ask"
    bl_label = "Ask Blender Agent"

    prompt: StringProperty(name="Prompt")

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=560)

    def draw(self, context):
        layout = self.layout
        prefs = _prefs(context)
        layout.label(text="Model: %s%s" % (prefs.resolved_model() or "(none)",
                                           "" if (prefs.api_key or "").strip() else "  -  no key"))
        for entry in SESSION.transcript[-3:]:
            box = layout.box()
            for line in _wrap(entry["text"], 74, 6):
                box.label(text=line or " ")
        layout.prop(self, "prompt", text="")
        if SESSION.busy():
            layout.label(text="Working - see the Agent tab in the sidebar", icon="TIME")

    def execute(self, context):
        prefs = _prefs(context)
        text = (self.prompt or "").strip()
        if not text:
            return {"CANCELLED"}
        if not (prefs.api_key or "").strip():
            self.report({"ERROR"}, "Set your OpenRouter API key first")
            return {"CANCELLED"}
        SESSION.send(text, prefs)
        self.report({"INFO"}, "Sent to %s" % prefs.resolved_model())
        return {"FINISHED"}


class BLENDER_AGENT_OT_screenshot(Operator):
    bl_idname = "blender_agent.screenshot_model"
    bl_label = "Screenshot Model"
    bl_description = "Render a quick screenshot of the model and show it in the panel"

    def execute(self, context):
        from . import shots
        ok, msg = shots.capture(label="manual")
        SESSION.record("info" if ok else "error", "Model screenshot: %s" % msg)
        self.report({"INFO"} if ok else {"ERROR"}, msg)
        redraw_all(force=True)
        return {"FINISHED"} if ok else {"CANCELLED"}


class BLENDER_AGENT_OT_open_shot(Operator):
    bl_idname = "blender_agent.open_screenshot"
    bl_label = "Open Screenshot"
    bl_description = "Open the model screenshot full size"

    def execute(self, context):
        from . import shots
        ok, msg = shots.open_in_editor()
        self.report({"INFO"} if ok else {"ERROR"}, msg)
        return {"FINISHED"} if ok else {"CANCELLED"}


class BLENDER_AGENT_OT_bridge_toggle(Operator):
    bl_idname = "blender_agent.bridge_toggle"
    bl_label = "Start/Stop Remote Bridge"
    bl_description = "Serve Blender Agent on a loopback port so the tailnet can reach it"

    def execute(self, context):
        from . import bridge as bridge_mod
        prefs = _prefs(context)
        if bridge_mod.is_running():
            ok, msg = bridge_mod.stop()
        else:
            bridge_mod.token(prefs)          # reuse the saved token; "New Bridge Token" rotates it
            ok, msg = bridge_mod.start(prefs)
        SESSION.record("info" if ok else "error", "Remote bridge: %s" % msg)
        self.report({"INFO"} if ok else {"ERROR"}, msg)
        redraw_all(force=True)
        return {"FINISHED"} if ok else {"CANCELLED"}


class BLENDER_AGENT_OT_bridge_token(Operator):
    bl_idname = "blender_agent.bridge_token"
    bl_label = "New Bridge Token"

    def execute(self, context):
        from . import bridge as bridge_mod
        prefs = _prefs(context)
        bridge_mod.set_token(prefs)
        self.report({"INFO"}, "New bridge token generated")
        redraw_all(force=True)
        return {"FINISHED"}


class BLENDER_AGENT_OT_bridge_copy(Operator):
    bl_idname = "blender_agent.bridge_copy"
    bl_label = "Copy Bridge Details"
    bl_description = "Copy the bridge URL or token to the clipboard"

    what: StringProperty(default="url")

    def execute(self, context):
        from . import bridge as bridge_mod
        prefs = _prefs(context)
        if self.what == "link":
            value = bridge_mod.link(prefs)
        elif self.what == "url":
            value = bridge_mod.url()
        else:
            value = prefs.bridge_token or ""
        if not value:
            self.report({"ERROR"}, "Nothing to copy yet")
            return {"CANCELLED"}
        context.window_manager.clipboard = value
        self.report({"INFO"}, "Copied %s" % self.what)
        return {"FINISHED"}


class BLENDER_AGENT_OT_send_selection(Operator):
    bl_idname = "blender_agent.send_selection"
    bl_label = "Send Selection To Blender Agent"

    def execute(self, context):
        from . import context as ctxmod
        sel = [o for o in context.selected_objects]
        if not sel:
            self.report({"INFO"}, "Nothing selected")
            return {"CANCELLED"}
        wm = context.window_manager
        detail = "\n".join(ctxmod.object_details(o.name) for o in sel[:4])
        wm.agent_input = ("About the selected object(s):\n%s\n\n" % detail)
        self.report({"INFO"}, "Selection context added to the prompt")
        return {"FINISHED"}


classes = (
    BLENDER_AGENT_PT_agent, BLENDER_AGENT_PT_model, BLENDER_AGENT_PT_tools,
    BLENDER_AGENT_PT_settings,
    BLENDER_AGENT_MT_chats,
    BLENDER_AGENT_OT_send, BLENDER_AGENT_OT_stop, BLENDER_AGENT_OT_clear,
    BLENDER_AGENT_OT_new_chat, BLENDER_AGENT_OT_open_chat, BLENDER_AGENT_OT_rename_chat,
    BLENDER_AGENT_OT_delete_chat,
    BLENDER_AGENT_OT_undo_last, BLENDER_AGENT_OT_approve, BLENDER_AGENT_OT_deny,
    BLENDER_AGENT_OT_refresh_models, BLENDER_AGENT_OT_test_connection,
    BLENDER_AGENT_OT_open_key_page, BLENDER_AGENT_OT_model_browser,
    BLENDER_AGENT_OT_set_model, BLENDER_AGENT_OT_add_workspace,
    BLENDER_AGENT_OT_send_selection, BLENDER_AGENT_OT_quick_ask,
    BLENDER_AGENT_OT_open_panel,
    BLENDER_AGENT_OT_screenshot, BLENDER_AGENT_OT_open_shot,
    BLENDER_AGENT_OT_bridge_toggle, BLENDER_AGENT_OT_bridge_token,
    BLENDER_AGENT_OT_bridge_copy,
)


def _menu_object(self, context):
    self.layout.separator()
    self.layout.operator("blender_agent.open_panel", icon="OUTLINER_OB_LIGHT")
    self.layout.operator("blender_agent.quick_ask", icon="TRIA_RIGHT")
    self.layout.operator("blender_agent.send_selection", icon="SELECT_SET")


def _maybe_add_workspace():
    """One-shot: give untitled files a ready-made Agent workspace."""
    try:
        prefs = _prefs(bpy.context)
        if not prefs.auto_workspace or bpy.app.background:
            return None
        if bpy.data.filepath or workspace.WORKSPACE_NAME in bpy.data.workspaces:
            return None
        workspace.add_workspace(bpy.context)
    except Exception:  # noqa: BLE001 - never break startup
        return None
    return None


def _on_load(*_args):
    try:
        bpy.app.timers.register(_maybe_add_workspace, first_interval=0.75)
    except Exception:  # noqa: BLE001
        pass


def _maybe_start_bridge():
    """One-shot: honour the 'start with Blender' preference."""
    try:
        from . import bridge as bridge_mod
        prefs = _prefs(bpy.context)
        if prefs.bridge_autostart and not bridge_mod.is_running():
            bridge_mod.token(prefs)          # reuse the saved token, never regenerate it
            ok, msg = bridge_mod.start(prefs)
            if not ok:
                agent.SESSION.record("error", "Remote bridge: %s" % msg)
    except Exception:  # noqa: BLE001 - never break startup
        pass
    return None


def register_handlers():
    agent._redraw_cb = redraw_all
    openrouter.load_cache()
    bpy.types.VIEW3D_MT_object.append(_menu_object)
    _on_load()
    try:
        bpy.app.handlers.load_post.append(_on_load)
    except Exception:  # noqa: BLE001
        pass
    try:
        bpy.app.timers.register(_maybe_start_bridge, first_interval=1.0)
    except Exception:  # noqa: BLE001
        pass


def unregister_handlers():
    agent._redraw_cb = None
    try:
        bpy.types.VIEW3D_MT_object.remove(_menu_object)
    except Exception:  # noqa: BLE001
        pass
    try:
        bpy.app.handlers.load_post.remove(_on_load)
    except Exception:  # noqa: BLE001
        pass


def register():
    bpy.types.WindowManager.agent_input = StringProperty(
        name="Prompt", description="Ask Blender Agent for anything", default="")
    bpy.types.WindowManager.agent_show_full = BoolProperty(
        name="Full Output", default=False)
    bpy.types.WindowManager.agent_show_more = BoolProperty(
        name="History", default=False)
    bpy.types.WindowManager.agent_show_tools = BoolProperty(
        name="Tools", default=False)
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    for prop in ("agent_input", "agent_show_full", "agent_show_more", "agent_show_tools"):
        try:
            delattr(bpy.types.WindowManager, prop)
        except AttributeError:
            pass
