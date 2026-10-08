#!/usr/bin/env bash
# Publish the Blender Agent bridge on the tailnet (no root, userspace Tailscale).
#
#   ./scripts/tailnet.sh publish     # tailnet -> loopback bridge port
#   ./scripts/tailnet.sh unpublish
#   ./scripts/tailnet.sh status
#   ./scripts/tailnet.sh url
#   ./scripts/tailnet.sh verify      # proves reachability through tailscaled's proxy
#   ./scripts/tailnet.sh install-unit    # systemd --user unit so it survives reboots
#   ./scripts/tailnet.sh uninstall-unit
#
# The bridge itself runs inside Blender (Agent panel -> "Serve On Tailnet").
# This script only maps a tailnet port onto that loopback listener: userspace
# tailscaled cannot route inbound traffic without `tailscale serve`.
set -uo pipefail

PORT="${BLENDER_AGENT_PORT:-8770}"
SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
SOCK="$HOME/.tailscale/tailscaled.sock"
if [[ -S "$SOCK" ]]; then
  TS=(tailscale --socket="$SOCK")
else
  TS=(tailscale)
fi
UNIT="blender-agent-serve"
UNIT_DIR="$HOME/.config/systemd/user"

log() { printf '\n== %s\n' "$*"; }

dns_name() {
  "${TS[@]}" status --json 2>/dev/null | python3 -c \
    'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null
}

cmd_publish() {
  log "publishing tcp://$PORT (tailnet only)"
  "${TS[@]}" serve --bg --tcp="$PORT" "tcp://127.0.0.1:$PORT" || exit 1
  cmd_url
}

cmd_unpublish() {
  log "removing tailnet mapping for $PORT"
  "${TS[@]}" serve --tcp="$PORT" off || true
}

cmd_status() {
  log "serve mappings"
  "${TS[@]}" serve status 2>&1 | grep -A3 -B1 ":$PORT" || echo "port $PORT is not published"
}

cmd_url() {
  local name
  name="$(dns_name)"
  log "reachable at"
  # `tailscale serve --tcp` is a plain TCP forward: it does NOT terminate TLS, so
  # the URL must be http:// - an https:// URL fails with "wrong version number".
  echo "  http://${name:-<node>.<tailnet>.ts.net}:$PORT/?token=<token>"
  echo "  (get the token from Blender's Agent panel; traffic is WireGuard-encrypted,"
  echo "   there is just no browser padlock)"
  echo "  want https? enable HTTPS Certificates in the Tailscale admin console, then:"
  echo "     tailscale serve --bg --https=$PORT http://127.0.0.1:$PORT"
}

cmd_verify() {
  local name
  name="$(dns_name)"
  log "verifying through tailscaled's SOCKS proxy (the remote device's path)"
  for path in /healthz; do
    echo "--- $path"
    curl -sS -m 15 --socks5-hostname localhost:1055 \
      "http://${name}:$PORT${path}" || echo "FAILED - is the bridge started inside Blender?"
    echo
  done
}

write_unit() {
  mkdir -p "$UNIT_DIR"
  cat > "$UNIT_DIR/$UNIT.service" <<EOF
[Unit]
Description=Blender Agent: publish the bridge on the tailnet
After=hermes-remote-tailscaled.service
Wants=hermes-remote-tailscaled.service

[Service]
Type=oneshot
RemainAfterExit=yes
Environment=BLENDER_AGENT_PORT=$PORT
ExecStart=$SELF publish
ExecStop=$SELF unpublish

[Install]
WantedBy=default.target
EOF
  cat > "$UNIT_DIR/$UNIT.timer" <<EOF
[Unit]
Description=Re-assert the Blender Agent tailnet mapping

[Timer]
OnBootSec=90
OnUnitActiveSec=10min
Persistent=false

[Install]
WantedBy=timers.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now "$UNIT.service" "$UNIT.timer"
  log "installed"
  systemctl --user --no-pager status "$UNIT.service" | head -12
}

remove_unit() {
  systemctl --user disable --now "$UNIT.service" "$UNIT.timer" 2>/dev/null || true
  rm -f "$UNIT_DIR/$UNIT.service" "$UNIT_DIR/$UNIT.timer"
  systemctl --user daemon-reload
  log "unit removed"
}

case "${1:-}" in
  publish) shift; cmd_publish ;;
  unpublish) cmd_unpublish ;;
  status) cmd_status ;;
  url) cmd_url ;;
  verify) cmd_verify ;;
  install-unit) write_unit ;;
  uninstall-unit) remove_unit ;;
  *) sed -n '2,14p' "$0"; exit 2 ;;
esac
