# Blender Agent

A fork of Blender with a built-in AI agent that has full access to Blender's core
functionality. The agent talks to any OpenRouter-compatible endpoint, **you pick
the model**, and it drives Blender through 35 tools plus an unrestricted `bpy`
Python hatch.

Two things ship in this repo, both verified:

1. **`blender_agent/`** — the add-on. Installs into your existing Blender today
   (`scripts/install.sh`), lives in the 3D viewport sidebar under the **Agent** tab.
2. **A real source fork** — `tools/fork_blender.sh` injects the add-on into
   `release/scripts/addons_core/` (a bundled core add-on) and applies the
   branding patches in `patches/` so a built binary reports *"5.2.2 LTS Agent"*
   and starts with the agent enabled.

```
blender_agent/        the add-on (pure Python, no dependencies)
  openrouter.py       OpenRouter client: model catalogue + streaming chat with tools
  agent.py            agent loop, main-thread marshalling, session state
  tools.py            35 tool schemas + dispatch (the agent's hands)
  context.py          scene introspection injected into every request
  ui.py               sidebar chat panel, model picker, operators
  preferences.py      API key, model, limits, system prompt
  workspace.py        one-click "Agent" workspace
patches/              C/C++ source patches for the fork build
tests/                headless end-to-end suite + a mock OpenRouter server
tools/fork_blender.sh fork builder / dependency checker
scripts/install.sh    install into the local Blender (no root needed)
```

## Install into your Blender (works right now)

```bash
./scripts/install.sh          # symlink the add-on into ~/.config/blender/<ver>/scripts/addons
```

Then in Blender: 3D viewport → `N` → **Agent** tab.

1. Paste your OpenRouter key (the panel asks for it if it is missing; it is stored
   in Blender's user preferences as a password field).
2. Pick a model — the list is the live OpenRouter catalogue (467 models at the
   time of writing) with context length, price and modality shown; a searchable
   browser and a free-text custom model id are both available.
3. Type what you want and press the play button.

`Alt`-free extras: *Object → Send Selection To Blender Agent* puts the selection's
details into the prompt, *Object → Ask Blender Agent* opens a quick-ask popup that
works without the sidebar, and **Add Agent Workspace** builds a workspace tuned for
talking to the agent (sidebar open, material shading).

## What the agent can do

35 tools, all of them executing on Blender's main thread:

| Area | Tools |
| --- | --- |
| Inspect | `get_scene_context`, `list_objects`, `get_object_details`, `search_api`, `bpy_help` |
| Create / edit | `create_objects`, `modify_objects`, `delete_objects`, `duplicate_objects`, `select_objects`, `scene_ops`, `set_units` |
| Materials | `set_material` (any Principled socket), `material_nodes` (add/link/set/remove nodes), `set_world` |
| Mesh | `mesh_edit` (subdivide, bevel, inset, extrude, weld, solidify, decimate, remesh, normals…), `boolean`, `add_modifiers`, `apply_modifiers`, `geometry_nodes`, `edit_object_mode` |
| Lighting / camera | `add_lights`, `add_cameras`, `set_active_camera_view` |
| Render | `set_render_settings`, `render_image`, `render_preview_and_view` |
| Animation | `animate`, `set_frame` |
| Files | `import_export` (obj, fbx, gltf, stl, ply, usd, abc, dae), `file_ops`, `undo_redo`, `manage_collections` |
| Escape hatch | `bpy_operator` (call **any** `bpy.ops.*`), `execute_blender_python` (arbitrary Python with `bpy`, `bmesh`, `mathutils`) |

`execute_blender_python` is the guarantee of "full access" — anything Blender can
do from Python, the agent can do, including armatures, shape keys, drivers,
particles, simulations and batch edits. `search_api`/`bpy_help` let it discover
the parts of the API the high-level tools do not cover.

### It can see its own work

`render_preview_and_view` renders a fast preview and sends the PNG back to the
model as an image, so vision-capable models critique their own output instead of
guessing. Disable with *Send Renders Back To The Model*.

### Safety

- One undo step per tool call (labelled `Blender Agent: <tool>`) — the **Undo
  Agent Change** button reverses the agent's last action in one click.
- *Confirm Before Running Code* makes `execute_blender_python` wait for an
  approve/deny prompt in the panel.
- Everything else is reversible through Blender's normal undo stack.

## How it is wired (the interesting part)

Blender's Python API is **not thread-safe**: only the main thread may touch `bpy`.
The agent therefore runs its network I/O on a worker thread and marshals every
tool call back to the main thread through a job queue drained by a
`bpy.app.timers` callback. Streaming tokens update a transcript that the panel
redraws on a throttled timer, and **Stop** closes the live HTTP response instead
of waiting for the request to finish.

Requests carry a system prompt plus a live scene summary (version, mode, units,
render settings, every object with transform/material/modifier, collections,
selection). Older messages are trimmed from the payload while keeping tool-call /
tool-result pairs intact.

## The fork

```bash
./tools/fork_blender.sh --prepare     # checkout v5.2.2 + inject add-on + apply patches
./tools/fork_blender.sh --check-deps  # what a build still needs
./tools/fork_blender.sh --build       # full build (30-90 min, needs system dev libs)
```

Patches applied to the source:

- `0001-brand-version-string.patch` — version string becomes `5.2.2 LTS Agent`
  (splash, About box, `blender --version`).
- `0002-enable-blender-agent-by-default.patch` — `blo_do_versions_userdef()` calls
  `BKE_addon_ensure(&userdef->addons, "blender_agent")`, so a fresh profile starts
  with the agent already enabled.
- The add-on is copied into `release/scripts/addons_core/blender_agent` and given a
  `blender_manifest.toml` (validated with `blender --command extension validate`).

**Build status on this machine:** the source fork and both patches are complete and
verified, but the binary build cannot finish here — there is no root access, so
none of the 7 required `-dev` packages (`libx11-dev`, `libgl-dev`, `libpng-dev`,
`libfreetype-dev`, `libopenexr-dev`, `python3-dev`, `libtbb-dev`) can be installed.
`--check-deps` reports exactly what is missing and prints the install command.
With those packages present, `--build` is the only remaining step.

## Remote control over the tailnet

The Blender process itself becomes the server, so you can drive the agent from a
phone or laptop while Blender keeps working on your desktop.

1. In Blender's Agent panel press **Serve On Tailnet** (or tick *Start Tailnet
   Bridge With Blender* so every session serves it). The bridge binds
   `127.0.0.1:8770` and prints a token; **Copy URL** / **Copy Token** put both on
   your clipboard.
2. Publish it on the tailnet — loopback listeners are unreachable without this,
   because tailscaled runs in userspace mode:

   ```bash
   ./scripts/tailnet.sh publish      # tailscale serve --bg --tcp=8770 tcp://127.0.0.1:8770
   ./scripts/tailnet.sh verify       # proves reachability through tailscaled's SOCKS proxy
   ./scripts/tailnet.sh install-unit # systemd --user unit + 10-min self-heal timer
   ```

3. Open `http://<node>.<tailnet>.ts.net:8770/?token=<token>` on any tailnet device.

   **Use `http://`, not `https://`.** `tailscale serve --tcp` is a plain TCP
   forward — it does not terminate TLS, so an `https://` URL fails with
   "wrong version number". Traffic is WireGuard-encrypted end to end regardless;
   there is just no browser padlock. If you want a real certificate, enable
   *HTTPS Certificates* in the Tailscale admin console and publish with
   `tailscale serve --bg --https=8770 http://127.0.0.1:8770` instead.

What the page gives you: live status (Blender version, scene, object list with
transforms, agent state), the running transcript, a model dropdown built from the
live catalogue, Send/Stop/Clear, and inline errors (e.g. an unset API key). The
same thing is available as JSON: `GET /api/status`, `GET /api/models`,
`POST /api/ask {prompt}`, `POST /api/model {model}`, `POST /api/stop`, `POST /api/clear`.

Safety: the listener is loopback-only, `tailscale serve` is tailnet-only (never
funnel), and every endpoint except `/healthz` and the static page needs the bearer
token. Note this is a chat box wired to a scriptable 3D app — the tailnet plus the
token are the whole gate, so treat the token like a password and rotate it with
**New Bridge Token** if a device goes missing.

Verified from a real browser routed through the tailnet (`socks5://localhost:1055`):
the page loads, renders live status (`idle | <model> | 5.2.2 LTS`), lists the scene
objects and populates the model dropdown from the live catalogue.

## Tests

```bash
./tests/run_tests.sh        # 72 checks, headless, against a local mock OpenRouter
```

The mock server (`tests/mock_openrouter.py`) speaks real SSE and tool-call
deltas, so the suite exercises the whole path: tool implementations against a
real Blender scene, model catalogue + key validation, a two-turn agent loop in
both blocking and streaming mode, image feedback (asserts a base64 data URL was
sent back), HTTP error surfacing, cancellation, message trimming, undo, and the
remote bridge (token enforcement, remote ask → tool call → real object created,
remote model switching, web page served).

GUI checks (run separately, needs a display): workspace creation, a live streamed
agent turn, one-step undo of all agent-created objects, and screenshots of the UI.

## Notes

- No third-party Python packages: the client uses `requests`, which Blender bundles.
- The API key lives in Blender's user preferences (`userpref.blend`, password field);
  it is never written to this repo.
- GPL-3.0-or-later, matching Blender itself.
