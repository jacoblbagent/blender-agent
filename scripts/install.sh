#!/usr/bin/env bash
# Install the Blender Agent addon into the local Blender user scripts dir (no root).
#
#   ./scripts/install.sh            # symlink (live edits) - default
#   ./scripts/install.sh --copy     # copy a snapshot instead
#
# Works with the distro/snap Blender: the user script path is versioned
# (~/.config/blender/<version>/scripts/addons), so nothing outside $HOME changes.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BLENDER="${BLENDER:-blender}"
MODE="link"
[[ "${1:-}" == "--copy" ]] && MODE="copy"

VER="$("$BLENDER" --version 2>/dev/null | head -1 | awk '{print $2}')"
if [[ -z "$VER" ]]; then
  echo "Could not detect Blender version; is 'blender' on PATH?" >&2
  exit 1
fi
MAJOR="${VER%.*}"
TARGET_DIR="$HOME/.config/blender/$MAJOR/scripts/addons"
TARGET="$TARGET_DIR/blender_agent"

mkdir -p "$TARGET_DIR"
rm -rf "$TARGET"
if [[ "$MODE" == "link" ]]; then
  ln -s "$HERE/blender_agent" "$TARGET"
else
  cp -r "$HERE/blender_agent" "$TARGET"
fi

echo "Blender $VER -> $TARGET ($MODE)"

"$BLENDER" -b --python-expr "
import bpy, addon_utils
try:
    bpy.ops.preferences.addon_enable(module='blender_agent')
except Exception as exc:
    print('ENABLE_FAILED', exc); raise SystemExit(1)
import blender_agent
from blender_agent import tools, openrouter
print('ADDON_OK', blender_agent.bl_info['name'], blender_agent.bl_info['version'])
print('TOOLS', len(tools.TOOL_SCHEMAS))
print('PANEL', hasattr(bpy.types, 'BLENDER_AGENT_PT_agent'))
print('OPS', hasattr(bpy.ops.blender_agent, 'send'), hasattr(bpy.ops.blender_agent, 'set_model'))
bpy.ops.wm.save_userpref()
" 2>&1 | grep -E "ADDON_OK|TOOLS|PANEL|OPS|ENABLE_FAILED|Error|Traceback" || true

cat <<EOF

Installed. In Blender: 3D viewport -> press N -> "Agent" tab.
Put your OpenRouter key in the panel (or Edit > Preferences > Add-ons > Blender Agent).
EOF
