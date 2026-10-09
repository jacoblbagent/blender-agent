"""Blender Agent - a built-in AI agent for Blender.

A full-access agentic assistant integrated into Blender. It connects to any
OpenRouter-compatible endpoint, the user picks the model, and the agent drives
Blender through ~30 high-level tools plus a raw ``bpy`` Python escape hatch.

Module layout:
    preferences.py  addon preferences (API key, model, limits, system prompt)
    openrouter.py   OpenRouter client: model catalogue + streaming chat w/ tools
    context.py      scene introspection injected into the system prompt
    tools.py        tool schemas + dispatch (the agent's hands)
    agent.py        agent loop, main-thread marshalling, session state
    ui.py           sidebar chat panel, operators, model picker
    workspace.py    one-click "Agent" workspace creation
    attachments.py  photos pasted into the remote page, saved for the model's vision
"""

bl_info = {
    "name": "Blender Agent",
    "author": "Blender Agent",
    "version": (1, 0, 0),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar (N) > Agent",
    "description": "Built-in AI agent with full access to Blender, powered by OpenRouter",
    "category": "3D View",
}

import importlib

from . import (preferences, openrouter, context, tools, agent, bridge, shots, ui, workspace,
               attachments)

_modules = (preferences, openrouter, context, tools, agent, bridge, shots, ui, workspace,
            attachments)


def register():
    for mod in _modules:
        importlib.reload(mod)
    for mod in _modules:
        if hasattr(mod, "register"):
            mod.register()
    agent.load_chats()
    agent.register_timer()
    ui.register_handlers()


def unregister():
    ui.unregister_handlers()
    try:
        agent.save_chats()
    except Exception:  # noqa: BLE001 - never block shutdown on persistence
        pass
    try:
        bridge.stop()
    except Exception:  # noqa: BLE001
        pass
    agent.unregister_timer()
    for mod in reversed(_modules):
        if hasattr(mod, "unregister"):
            mod.unregister()
