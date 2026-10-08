"""Load the OpenRouter key from an existing .env into Blender Agent prefs.

Run: blender -b --python tests/set_key_from_env.py -- <path-to-env-file>
The key is never printed; only a masked fingerprint and the /key response.
"""

import os
import re
import sys

import bpy

bpy.ops.preferences.addon_enable(module="blender_agent")
from blender_agent import openrouter

env_path = None
if "--" in sys.argv:
    rest = sys.argv[sys.argv.index("--") + 1:]
    if rest:
        env_path = rest[0]
env_path = env_path or os.path.expanduser("~/Code/deal-scout/.env")

if not os.path.exists(env_path):
    print("SETKEY_FAIL no env file at %s" % env_path, flush=True)
    raise SystemExit(1)

value = None
for line in open(env_path, errors="replace"):
    m = re.match(r"\s*(?:export\s+)?OPENROUTER_API_KEY\s*=\s*(.+?)\s*$", line)
    if m:
        value = m.group(1).strip().strip('"').strip("'")
        break
if not value or not value.startswith("sk-or-"):
    print("SETKEY_FAIL no usable OPENROUTER_API_KEY in %s" % env_path, flush=True)
    raise SystemExit(1)

prefs = bpy.context.preferences.addons["blender_agent"].preferences
prefs.api_key = value
bpy.ops.wm.save_userpref()

ok, msg = openrouter.validate_key(prefs)
print("SETKEY_MASKED sk-or-...%s (len %d) from %s" % (value[-4:], len(value), env_path), flush=True)
print("SETKEY_VALIDATE ok=%s %s" % (ok, msg), flush=True)
print("SETKEY_MODEL %s" % prefs.resolved_model(), flush=True)
