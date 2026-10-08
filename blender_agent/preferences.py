"""Addon preferences: credentials, model selection, agent limits."""

import json
import os

import bpy
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    StringProperty,
)

from . import openrouter

# A small 0600 sidecar next to the add-on's config dir. Blender wipes an add-on's
# preferences whenever it is re-registered, and the add-on's host service stops
# Blender without saving preferences - either way the user's API key and model
# disappeared and "nothing sends" until they were re-entered. The key is read back
# from here (or from $OPENROUTER_API_KEY) when the preference is empty.
SETUP_FILE_ENV = "BLENDER_AGENT_SETUP_FILE"
KEY_ENV = "OPENROUTER_API_KEY"

DEFAULT_SYSTEM_PROMPT = (
    "You are Blender Agent, an expert 3D artist and technical director with FULL "
    "control over the running Blender session.\n\n"
    "You act by calling tools. Always inspect the scene before you change it "
    "(get_scene_context / list_objects / get_object_details) unless the request is "
    "purely additive. Prefer the high-level tools; use execute_blender_python when a "
    "task needs precise control or something the high-level tools do not cover - it "
    "runs arbitrary Python with bpy, bmesh, mathutils and math available.\n\n"
    "Working rules:\n"
    "- Think in real units and physically plausible proportions. Get scale, contact "
    "and silhouette right before adding detail.\n"
    "- Do several tool calls per turn when they are independent; batch object creation.\n"
    "- Big tasks: build the blockout, then refine, then render a preview and LOOK at "
    "the result before declaring success.\n"
    "- After a render_preview_and_view call you receive the image itself - critique it "
    "and fix what is wrong.\n"
    "- Never claim something is done without evidence from a tool result.\n"
    "- Keep replies short: a one-line summary of what changed, plus anything the user "
    "must decide. No long prose."
)


class BlenderAgentPreferences(bpy.types.AddonPreferences):
    bl_idname = "blender_agent"

    api_key: StringProperty(
        name="OpenRouter API Key",
        description="sk-or-v1-... - stored in your Blender user preferences",
        default="",
        subtype="PASSWORD",
    )
    base_url: StringProperty(
        name="API Base URL",
        description="OpenRouter-compatible endpoint",
        default="https://openrouter.ai/api/v1",
    )
    http_referer: StringProperty(
        name="HTTP Referer (optional)",
        description="Sent as HTTP-Referer; OpenRouter uses it for app attribution",
        default="https://github.com/jacoblbagent/blender-agent",
    )
    app_title: StringProperty(name="App Title (X-Title)", default="Blender Agent")

    model: EnumProperty(
        name="Model",
        description="Any model in the OpenRouter catalogue",
        items=openrouter.model_enum_items,
    )
    model_custom: StringProperty(
        name="Custom Model ID",
        description="Used when Model is set to 'custom' - must be a valid OpenRouter model id",
        default="",
    )
    temperature: FloatProperty(name="Temperature", default=0.2, min=0.0, max=2.0)
    max_tokens: IntProperty(
        name="Max Output Tokens", default=0, min=0, soft_max=64000,
        description="0 = provider default",
    )
    top_p: FloatProperty(name="Top P", default=1.0, min=0.0, max=1.0)

    max_steps: IntProperty(
        name="Max Tool Steps", default=40, min=1, max=500,
        description="Hard cap on tool-calling rounds for a single user message",
    )
    stream: BoolProperty(
        name="Stream Responses", default=True,
        description="Stream tokens live into the panel; disable for endpoints that do not support SSE",
    )
    auto_scroll: BoolProperty(name="Show Reasoning", default=False,
                              description="Display model reasoning tokens when the model emits them")
    timeout: IntProperty(name="Request Timeout (s)", default=180, min=10, max=1800)

    inject_context: BoolProperty(
        name="Inject Scene Context", default=True,
        description="Append a live scene summary to every request",
    )
    confirm_code: BoolProperty(
        name="Confirm Before Running Code", default=False,
        description="Ask for approval before execute_blender_python runs (safety net)",
    )
    undo_per_tool: BoolProperty(
        name="Undo Steps Per Tool Call", default=True,
        description="Push an undo state before each mutating tool call",
    )
    vision_feedback: BoolProperty(
        name="Send Renders Back To The Model", default=True,
        description="Attach render_preview_and_view images so vision models can see their work",
    )
    max_messages: IntProperty(
        name="Messages Kept In Context", default=60, min=6, max=400,
        description="Trim older messages from the request payload (transcript stays visible)",
    )
    auto_screenshot: BoolProperty(
        name="Screenshot After Each Build",
        default=True,
        description="Render a quick viewport-style screenshot of the model after the "
                    "agent changes it, and show it in the Agent panel",
    )
    auto_workspace: BoolProperty(
        name="Create Agent Workspace On Start", default=True,
        description="Add a ready-made 'Agent' workspace (open sidebar, material shading) "
                    "when Blender opens an untitled file",
    )

    system_prompt: StringProperty(
        name="System Prompt", default=DEFAULT_SYSTEM_PROMPT,
    )

    bridge_autostart: BoolProperty(
        name="Start Tailnet Bridge With Blender", default=False,
        description="Run the remote bridge (loopback HTTP) as soon as this add-on loads",
    )
    bridge_port: IntProperty(
        name="Bridge Port", default=8770, min=1024, max=65535,
        description="Loopback port the bridge listens on; publish it with tailscale serve",
    )
    bridge_host: StringProperty(
        name="Public Host (for links)",
        description="Hostname used in the copied link, e.g. <node>.<tailnet>.ts.net - "
                    "detected from Tailscale when empty",
        default="",
    )
    bridge_token: StringProperty(
        name="Bridge Token", default="",
        description="Bearer token required by the remote bridge (generated on first start)",
    )

    def resolved_model(self):
        if self.model == "custom":
            return self.model_custom.strip()
        return self.model

    def draw(self, context):
        layout = self.layout
        col = layout.column()

        box = col.box()
        box.label(text="Connection", icon="URL")
        col_box = box.column(align=True)
        col_box.prop(self, "api_key")
        row = col_box.row(align=True)
        row.operator("blender_agent.test_connection", icon="PLUGIN")
        row.operator("blender_agent.open_key_page", text="Get A Key", icon="URL")
        col_box.prop(self, "base_url")
        row = col_box.row(align=True)
        col_box.prop(self, "http_referer")
        col_box.prop(self, "app_title")

        box = col.box()
        box.label(text="Model", icon="OUTLINER_OB_EMPTY")
        boxr = box.row(align=True)
        boxr.prop(self, "model", text="")
        boxr.operator("blender_agent.refresh_models", text="", icon="FILE_REFRESH")
        if self.model == "custom":
            box.prop(self, "model_custom")
        box.operator("blender_agent.open_model_browser", icon="ZOOM_SELECTED")
        mt = openrouter.model_meta(self.resolved_model())
        if mt:
            grid = box.grid_flow(columns=2, even_columns=True, align=True)
            grid.label(text="Context: %s" % (mt.get("context_length") or "-"))
            grid.label(text="Price/1M in: %s" % mt.get("prompt_price", "-"))
            grid.label(text="Modalities: %s" % mt.get("modalities", "-"))
            grid.label(text="Price/1M out: %s" % mt.get("completion_price", "-"))
        colrow = box.column(align=True)
        colrow.prop(self, "temperature")
        colrow.prop(self, "top_p")
        colrow.prop(self, "max_tokens")

        box = col.box()
        box.label(text="Agent", icon="SEQUENCE")
        b = box.column(align=True)
        b.prop(self, "max_steps")
        b.prop(self, "max_messages")
        b.prop(self, "timeout")
        b.prop(self, "stream")
        b.prop(self, "auto_scroll")
        b.prop(self, "inject_context")
        b.prop(self, "undo_per_tool")
        b.prop(self, "confirm_code")
        b.prop(self, "vision_feedback")
        b.prop(self, "auto_screenshot")
        b.prop(self, "auto_workspace")

        box = col.box()
        box.label(text="System Prompt", icon="TEXT")
        box.prop(self, "system_prompt", text="")

        box = col.box()
        box.label(text="Remote (Tailnet)", icon="INTERNET")
        b = box.column(align=True)
        from . import bridge as bridge_mod
        if bridge_mod.is_running():
            b.label(text="listening on %s" % bridge_mod.url(), icon="CHECKMARK")
            b.label(text="Open this link (it carries the token):")
            b.label(text=bridge_mod.link(self))
            row = b.row(align=True)
            row.operator("blender_agent.bridge_copy", text="Copy Link", icon="COPY_ID").what = "link"
            row.operator("blender_agent.bridge_copy", text="Copy Token", icon="KEYINGSET").what = "token"
        else:
            b.label(text="bridge stopped", icon="PAUSE")
        row = b.row(align=True)
        row.operator("blender_agent.bridge_toggle", icon="PLAY" if not bridge_mod.is_running() else "PAUSE")
        row.operator("blender_agent.bridge_token", text="New Token", icon="FILE_REFRESH")
        b.prop(self, "bridge_autostart")
        b.prop(self, "bridge_port")
        b.prop(self, "bridge_host")
        if bridge_mod.is_running():
            b.label(text="publish: scripts/tailnet.sh publish")

        row = col.row(align=True)
        row.operator("blender_agent.add_workspace", icon="WORKSPACE")
        row.operator("blender_agent.clear_history", icon="TRASH")


def get_prefs(context=None):
    context = context or bpy.context
    return context.preferences.addons[__package__].preferences


# --------------------------------------------------------------- persistence --

def _setup_path():
    override = os.environ.get(SETUP_FILE_ENV)
    if override:
        return override
    # user_resource(create=True) makes a *directory*; join the file name onto it.
    base = bpy.utils.user_resource("CONFIG", path="blender_agent", create=True)
    return os.path.join(base, "setup.json")


def _read_setup():
    try:
        with open(_setup_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_setup(**values):
    """Merge values into the sidecar and write it 0600, atomically."""
    data = _read_setup()
    for key, value in values.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    path = _setup_path()
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        pass
    return data


def resolved_api_key(prefs=None):
    """The key to use: the preference, then our own last-known key, then the env.

    The sidecar beats the environment on purpose: it records the key the user last
    actually used in Blender, so changing the key in the panel is not silently undone
    by a stale OPENROUTER_API_KEY the next time the add-on loads.
    """
    prefs = prefs or get_prefs()
    for candidate in ((prefs.api_key or "").strip(),
                      str(_read_setup().get("api_key") or "").strip(),
                      (os.environ.get(KEY_ENV) or "").strip()):
        if candidate:
            return candidate
    return ""


def remember(prefs=None):
    """Persist the key/model so a preference reset cannot lose them."""
    prefs = prefs or get_prefs()
    return _write_setup(model=prefs.resolved_model().strip() or None,
                        api_key=(prefs.api_key or "").strip() or None)


def restore(prefs=None):
    """Re-apply the key/model after Blender reset the add-on's preferences."""
    prefs = prefs or get_prefs()
    saved = _read_setup()
    store = {}
    if not (prefs.api_key or "").strip():
        key = resolved_api_key(prefs)
        if key:
            prefs.api_key = key
            store["api_key"] = key
    if not prefs.resolved_model().strip():
        model = str(saved.get("model") or "").strip()
        if model:
            prefs.model = "custom"
            prefs.model_custom = model
            store["model"] = model
    if store:
        _write_setup(**store)
    return prefs


classes = (BlenderAgentPreferences,)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    try:
        restore()
    except Exception:  # noqa: BLE001 - never break add-on registration
        pass


def unregister():
    try:
        remember()
    except Exception:  # noqa: BLE001
        pass
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
