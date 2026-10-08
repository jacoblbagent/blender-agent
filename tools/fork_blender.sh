#!/usr/bin/env bash
# Build the Blender Agent fork from Blender source.
#
#   ./tools/fork_blender.sh --prepare      # checkout + inject addon + apply patches (no build)
#   ./tools/fork_blender.sh --check-deps   # report what a build is missing
#   ./tools/fork_blender.sh --configure    # run cmake configure (shows real dependency errors)
#   ./tools/fork_blender.sh --build        # full build (needs system dev libs; see --check-deps)
#
# What "fork" means here, concretely:
#   1. blender_agent/ is injected into release/scripts/addons_core/  -> a bundled,
#      always-installed core add-on (not something the user has to download).
#   2. patches/*.patch are applied to the C/C++ source:
#        0001 brands the version string        ("5.2.2 LTS Agent")
#        0002 enables blender_agent by default (blo_do_versions_userdef)
#   3. optionally builds a binary from that tree.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="${BLENDER_SRC:-$HERE/vendor/blender-src}"
TAG="${BLENDER_TAG:-v5.2.2}"
REMOTE="${BLENDER_REMOTE:-https://github.com/blender/blender}"
MODE="--prepare"

for arg in "$@"; do MODE="$arg"; done

log() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

# ---------------------------------------------------------------- checkout --
ensure_source() {
  local expand="${1:-false}"
  if [[ -d "$SRC/.git" ]]; then
    log "source present: $SRC"
    local cur
    cur="$(git -C "$SRC" describe --tags --always 2>/dev/null || echo unknown)"
    echo "at: $cur"
    # A full build needs the whole tree; undo any sparse checkout.
    if [[ "$expand" == "true" ]] && [[ -n "$(git -C "$SRC" sparse-checkout list 2>/dev/null)" ]]; then
      echo "expanding sparse checkout to the full tree (needed for a build)"
      git -C "$SRC" sparse-checkout disable
      git -C "$SRC" checkout "$TAG" -- . 2>/dev/null || git -C "$SRC" checkout "$TAG"
    fi
  else
    log "cloning $REMOTE @ $TAG"
    mkdir -p "$(dirname "$SRC")"
    git clone --depth 1 --branch "$TAG" "$REMOTE" "$SRC" || return 1
  fi
}

# ------------------------------------------------------------------ inject --
inject_addon() {
  local dest="$SRC/release/scripts/addons_core/blender_agent"
  log "injecting the add-on into addons_core"
  rm -rf "$dest"
  mkdir -p "$(dirname "$dest")"
  cp -r "$HERE/blender_agent" "$dest"
  find "$dest" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
  # core add-ons are extension-style: drop bl_info, keep a manifest
  python3 - "$dest" <<'PY'
import json, pathlib, re, sys
dest = pathlib.Path(sys.argv[1])
init = dest / "__init__.py"
src = init.read_text()
m = re.search(r"bl_info = \{.*?\n\}\n", src, re.S)
version = [1, 0, 0]
if m:
    vm = re.search(r"\"version\":\s*\(([^)]*)\)", m.group(0))
    if vm:
        version = [int(x) for x in vm.group(1).split(",") if x.strip()]
    src = src.replace(m.group(0), "")
    init.write_text(src)
manifest = {
    "schema_version": "1.0.0",
    "id": "blender_agent",
    "version": ".".join(str(v) for v in version),
    "name": "Blender Agent",
    "tagline": "Built-in AI agent with full control of Blender",
    "maintainer": "Blender Agent",
    "type": "add-on",
    "blender_version_min": "4.2.0",
    "license": ["SPDX:GPL-3.0-or-later"],
    "tags": ["3D View", "User Interface"],
}
(dest / "blender_manifest.toml").write_text(
    "".join('%s = %s\n' % (k, json.dumps(v) if isinstance(v, str) else
                           ("[%s]" % ", ".join(json.dumps(i) for i in v) if isinstance(v, list)
                            else json.dumps(v)))
            for k, v in manifest.items()))
print("injected:", dest)
PY
}

# ----------------------------------------------------------------- patches --
apply_patches() {
  log "applying fork patches"
  shopt -s nullglob
  local applied=0 skipped=0 failed=0
  for p in "$HERE"/patches/*.patch; do
    if git -C "$SRC" apply --check -R "$p" 2>/dev/null; then
      echo "already applied: $(basename "$p")"
      skipped=$((skipped + 1))
    elif git -C "$SRC" apply --check "$p" 2>/dev/null; then
      git -C "$SRC" apply "$p"
      echo "applied: $(basename "$p")"
      applied=$((applied + 1))
    else
      echo "FAILED to apply: $(basename "$p")"
      failed=$((failed + 1))
    fi
  done
  echo "patches: $applied applied, $skipped already applied, $failed failed"
  [[ $failed -eq 0 ]]
}

# -------------------------------------------------------------- dependency --
REQUIRED_HEADERS=(
  "/usr/include/X11/Xlib.h:libx11-dev"
  "/usr/include/GL/gl.h:libgl-dev"
  "/usr/include/png.h:libpng-dev"
  "/usr/include/freetype2/ft2build.h:libfreetype-dev"
  "/usr/include/OpenEXR/ImfRgbaFile.h:libopenexr-dev"
  "/usr/include/python3.11/Python.h:python3-dev"
  "/usr/include/tbb/tbb.h:libtbb-dev"
)

check_deps() {
  log "system build dependencies"
  local missing=()
  for entry in "${REQUIRED_HEADERS[@]}"; do
    local header="${entry%%:*}" pkg="${entry##*:}"
    if [[ -e "$header" ]]; then
      echo "ok      $pkg"
    else
      echo "MISSING $pkg  ($header)"
      missing+=("$pkg")
    fi
  done
  for tool in cmake ninja g++; do
    if command -v "$tool" >/dev/null; then echo "ok      $tool"; else echo "MISSING $tool"; fi
  done
  if [[ ${#missing[@]} -gt 0 ]]; then
    cat <<EOF

${#missing[@]} development package(s) are missing. Blender's own helper can install them,
but that needs root (apt):

    sudo $SRC/build_files/build_environment/install_linux_packages.py

or install just what is missing:

    sudo apt-get install -y ${missing[*]}

EOF
    return 1
  fi
  echo "all required headers present - ready to configure"
  return 0
}

do_configure() {
  log "cmake configure"
  local builddir="$HERE/build"
  local logfile="$builddir/cmake_configure.log"
  mkdir -p "$builddir"
  cmake -S "$SRC" -B "$builddir" -G Ninja \
        -DCMAKE_BUILD_TYPE=Release \
        -DWITH_PYTHON_INSTALL=ON \
        -DWITH_INSTALL_PORTABLE=ON \
        -DWITH_CYCLES_DEVICE_OPTIX=OFF > "$logfile" 2>&1
  local rc=$?
  tail -40 "$logfile"
  echo "(full cmake log: $logfile)"
  return $rc
}

do_build() {
  log "building (this takes 30-90 minutes on 16 cores)"
  local builddir="$HERE/build"
  [[ -d "$builddir" ]] || do_configure || return 1
  cmake --build "$builddir" -j "$(nproc)"
}

# -------------------------------------------------------------------- main --
case "$MODE" in
  --prepare) ensure_source false || exit 1; inject_addon; apply_patches || exit 1 ;;
  --check-deps) check_deps; exit $? ;;
  --configure) ensure_source true || exit 1; check_deps; do_configure; exit $? ;;
  --build) ensure_source true || exit 1; check_deps && do_build; exit $? ;;
  *) echo "usage: $0 [--prepare|--check-deps|--configure|--build]"; exit 2 ;;
esac

log "fork tree ready"
cat <<EOF
Source tree:  $SRC
Add-on:       release/scripts/addons_core/blender_agent
Branding:     version string "... Agent" + add-on enabled by default

Next: $0 --check-deps      (or straight to --build with the deps installed)
EOF
