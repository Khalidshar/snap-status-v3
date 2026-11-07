#!/usr/bin/env bash
# Madar operations from docs/INVENTORY.md (master plan item 1). Run as root on the server.
#
#   madar.sh status            read-only: tools, release, keys (size only), data, units
#   madar.sh start [--apply]   start the long-running services in order (dry run by default)
#   madar.sh stop  [--apply]   stop them in reverse order (dry run by default)
#
# Every unit is named in full: no wildcard ever selects a unit. Key files are never
# opened; only their presence and size are shown. No timers are created or started.
set -u

# Long-running services, in start order (stop runs the reverse). "unit|key files".
SERVICES=(
  "madar-v3-production-egress@snap.service|"
  "madar-v3-production-egress@status.service|"
  "madar-v3-quran-egress@snap.service|/etc/madar/quran.env"
  "madar-v3-quran-egress@status.service|/etc/madar/quran.env"
  "madar-v3-snap-news-egress.service|"
  "madar-v3-status-news-egress.service|"
  "madar-v3-snap-video.service|/etc/madar/media.env"
  "madar-v3-status-video.service|/etc/madar/media.env"
  "madar-v3-snap-commands.service|/etc/madar/snap-telegram-main.env"
  "madar-v3-status-command-webhook.service|/etc/madar/status-whatsapp.env /etc/madar/status-command-webhook.json"
  "madar-v3-status-commands.service|/etc/madar/status-whatsapp.env"
  "madar-v3-whatsapp-origin.service|"
  "madar-v3-whatsapp-tunnel.service|/etc/madar/whatsapp-v3-tunnel.token"
)
KEYS=(
  /etc/madar/snap-telegram-main.env
  /etc/studio-v2/snap-telegram-test.env
  /etc/madar/status-whatsapp.env
  /etc/madar/status-command-webhook.json
  /etc/madar/whatsapp-v3-tunnel.token
  /etc/madar/quran.env
  /etc/madar/media.env
  /etc/madar/panel.env
)
TOOLS=(python3.12 ffmpeg ffprobe cloudflared git /opt/madar/node-runtime/bin/node)
DATA=(/var/lib/madar /var/lib/madar/snap /var/lib/madar/status /var/lib/madar/render-work
      /var/lib/madar/quran-snap /var/lib/madar/quran-status)
OVERRIDES=(madar-v3-snap-render@ madar-v3-snap-render madar-v3-status-commands madar-v3-status-content@)

unit_of() { printf '%s' "${1%%|*}"; }
keys_of() { printf '%s' "${1#*|}"; }
ready() { # every key file of a service exists and is not empty
  local f
  for f in $(keys_of "$1"); do [ -s "$f" ] || return 1; done
}

status() {
  echo "== tools"
  local t
  for t in "${TOOLS[@]}"; do printf '  %-34s %s\n' "$t" "$(command -v "$t" 2>/dev/null || echo MISSING)"; done
  echo "== release"
  printf '  current=%s\n' "$(readlink -f /opt/madar/current 2>/dev/null || echo NONE)"
  echo "== keys (presence and size only, never read)"
  local k
  for k in "${KEYS[@]}"; do
    if [ -e "$k" ]; then printf '  %-44s size=%s mode=%s\n' "$k" "$(stat -c %s "$k")" "$(stat -c '%a %U' "$k")"
    else printf '  %-44s MISSING\n' "$k"; fi
  done
  echo "== data"
  local d
  for d in "${DATA[@]}"; do
    if [ -d "$d" ]; then printf '  %-30s %s\n' "$d" "$(stat -c '%U:%G %a' "$d") $(du -sh "$d" 2>/dev/null | cut -f1)"
    else printf '  %-30s MISSING\n' "$d"; fi
  done
  echo "== owner overrides (zz-*)"
  local o
  for o in "${OVERRIDES[@]}"; do printf '  %-30s %s\n' "$o" "$(ls /etc/systemd/system/$o.service.d/ 2>/dev/null | grep '^zz-' | tr '\n' ' ')"; done
  echo "== services"
  local s u
  for s in "${SERVICES[@]}"; do
    u=$(unit_of "$s")
    printf '  %-44s load=%-9s active=%-9s keys=%s\n' "$u" "$(systemctl show "$u" -p LoadState --value)" \
      "$(systemctl is-active "$u")" "$(ready "$s" && echo ok || echo MISSING)"
  done
  echo "== timers (must be none for madar)"
  systemctl list-timers --all --no-legend --plain | awk '{print $(NF-1)}' | grep -E '^(madar|snap-bot|status-bot)' || echo "  none"
}

act() { # act start|stop apply
  local verb="$1" apply="$2" list=("${SERVICES[@]}") s u i
  if [ "$verb" = stop ]; then
    list=(); for ((i=${#SERVICES[@]}-1; i>=0; i--)); do list+=("${SERVICES[$i]}"); done
  fi
  for s in "${list[@]}"; do
    u=$(unit_of "$s")
    if [ "$(systemctl show "$u" -p LoadState --value)" != loaded ]; then echo "  skip $u (not installed)"; continue; fi
    if [ "$verb" = start ] && ! ready "$s"; then echo "  skip $u (key file missing or empty)"; continue; fi
    if [ "$apply" = yes ]; then systemctl "$verb" "$u" && echo "  $verb $u -> $(systemctl is-active "$u")"
    else echo "  would $verb $u"; fi
  done
  [ "$apply" = yes ] || echo "dry run: nothing changed (add --apply)"
}

case "${1:-status}" in
  status) status ;;
  start|stop)
    [ "$(id -u)" = 0 ] || { echo "run as root"; exit 2; }
    act "$1" "$([ "${2:-}" = --apply ] && echo yes || echo no)" ;;
  *) echo "usage: madar.sh status|start|stop [--apply]"; exit 2 ;;
esac
