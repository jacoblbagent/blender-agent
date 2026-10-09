"""Photos pasted into the remote page, kept on disk for the agent's vision.

The web UI accepts a pasted image, posts it as a base64 data URL, and this
module stores it under ``CONFIG/blender_agent/pasted`` so the model client can
attach it as an ``image_url`` part. Only image MIME types are accepted, and a
decoded-size cap stops a stray paste from putting megabytes into every request.

``BLENDER_AGENT_PASTE_DIR`` redirects the directory - the test harness points it
at /tmp so a run never touches the user's real pastes.
"""

import base64
import binascii
import os
import re
import time

import bpy

DIR_ENV = "BLENDER_AGENT_PASTE_DIR"
EXT_BY_MIME = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}
EXT_BY_NAME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
               "webp": "image/webp", "gif": "image/gif"}
MAX_BYTES = 8_000_000
MAX_COUNT = 60                      # keep the directory from growing without bound
_DATA_URL = re.compile(r"^data:([A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+);base64,(.*)$", re.S)
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def directory():
    """Where pastes live. Created on demand; never raises."""
    override = os.environ.get(DIR_ENV)
    path = override or bpy.utils.user_resource("CONFIG", path="blender_agent/pasted",
                                               create=True)
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    return path


def _stem(name):
    base = os.path.basename(name or "paste")
    base = os.path.splitext(base)[0]
    base = _SAFE.sub("_", base).strip("._-") or "paste"
    return base[:40]


def save_data_url(name, data_url):
    """Store one pasted image. Returns (path, error) - exactly one is set."""
    if not isinstance(data_url, str) or not data_url.strip():
        return None, "empty image data"
    match = _DATA_URL.match(data_url.strip())
    if match:
        mime, payload = match.group(1).lower(), match.group(2)
    elif isinstance(name, str) and name:
        # No data: prefix (older clipboard code): infer the type from the name.
        mime = EXT_BY_NAME.get(os.path.splitext(name)[1].lower().lstrip("."), "")
        payload = data_url
        if not mime:
            return None, "not an image (expected a data:image/... URL)"
    else:
        return None, "not an image (expected a data:image/... URL)"
    ext = EXT_BY_MIME.get(mime)
    if not ext:
        return None, "unsupported image type %s" % (mime or "?")
    compact = "".join(payload.split())
    # Cheap pre-check before we allocate the decoded copy.
    if len(compact) > (MAX_BYTES // 3 + 4) * 4:
        return None, "image is larger than %d MB" % (MAX_BYTES // 1_000_000)
    try:
        blob = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError):
        return None, "image data is not valid base64"
    if not blob:
        return None, "image data is empty"
    if len(blob) > MAX_BYTES:
        return None, "image is larger than %d MB" % (MAX_BYTES // 1_000_000)
    fname = "%s_%s.%s" % (_stem(name), time.strftime("%Y%m%d-%H%M%S%f")[:-3], ext)
    path = os.path.join(directory(), fname)
    try:
        with open(path, "wb") as fh:
            fh.write(blob)
    except OSError as exc:
        return None, "could not save the image (%s)" % exc
    prune()
    return path, ""


def save_many(items):
    """Save a list of {"name", "data"} pastes. Returns (paths, errors)."""
    paths, errors = [], []
    for item in items or []:
        if not isinstance(item, dict):
            errors.append("ignored a paste that was not {name, data}")
            continue
        path, error = save_data_url(item.get("name"), item.get("data"))
        if path:
            paths.append(path)
        else:
            errors.append(error or "could not save the image")
    return paths, errors


def resolve(name):
    """Path for a stored paste by bare file name, or None (no traversal)."""
    base = os.path.basename(name or "")
    if not base or base != name:
        return None
    path = os.path.join(directory(), base)
    return path if os.path.isfile(path) else None


def prune(keep=MAX_COUNT):
    """Drop the oldest pastes past ``keep``. Failures are silent."""
    try:
        here = directory()
        files = sorted((f for f in os.listdir(here) if not f.startswith(".")),
                       key=lambda f: os.path.getmtime(os.path.join(here, f)))
    except OSError:
        return
    for name in (files[:-keep] if keep else files):
        try:
            os.remove(os.path.join(here, name))
        except OSError:
            pass
