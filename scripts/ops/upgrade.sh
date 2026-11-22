#!/usr/bin/env bash
# Owner-run upgrade only. Never enable timers, delete evidence, or print credentials.
set -euo pipefail

C="${1:-}"
if [[ ! "$C" =~ ^[0-9a-f]{40}$ ]] || [ "$#" -gt 2 ]; then
  echo "usage: upgrade.sh <40-character commit> [--force-pending]"; exit 2
fi
FORCE=()
if [ "$#" = 2 ]; then
  [ "$2" = --force-pending ] || { echo "STOP: unknown option"; exit 2; }
  FORCE=(--force-pending)
fi
[ "$(id -u)" = 0 ] || { echo "STOP: run as root"; exit 2; }
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
OLD=$(readlink -f /opt/madar/current)
OLD_COMMIT=${OLD##*/}
[[ "$OLD_COMMIT" =~ ^[0-9a-f]{40}$ ]] || { echo "STOP: current release is invalid"; exit 1; }
CLONE="/root/v3-$C"
RELEASE="$CLONE/apps/snap-bot/snap_bot/release.py"
UNITS=(
  madar-v3-snap-news-egress madar-v3-status-news-egress
  madar-v3-production-egress@snap madar-v3-production-egress@status
  madar-v3-snap-video madar-v3-status-video madar-v3-status-command-webhook
  madar-v3-snap-commands madar-v3-status-commands madar-v3-panel
  # Owner 22 Nov 2026: the Quran egress ran a week-old (deleted) release; it updates too.
  madar-v3-quran-egress@snap madar-v3-quran-egress@status
)

report() {
  local u state failed
  echo "OLD=$OLD"
  echo "CURRENT=$(readlink -f /opt/madar/current)"
  for u in "${UNITS[@]}"; do
    state=$(systemctl is-active "$u") || true
    echo "$u ${state:-unknown}"
  done
  if failed=$(systemctl list-units --failed --no-legend --plain --no-pager 'madar-v3*' | wc -l); then
    echo "FAILED=$failed"
  else
    echo "FAILED=UNKNOWN"
  fi
  echo "ROLLBACK: python3 $OLD/apps/snap-bot/snap_bot/release.py --activate-only --commit $OLD_COMMIT"
}
trap report EXIT

preflight() {
  local busy
  busy=$(systemctl list-units 'madar-v3-status-content@*' 'madar-v3-snap-render@*' \
    'madar-manual-render-*' --state=active,activating --no-legend --plain --no-pager | wc -l)
  echo "busy_clips=$busy"
  [ "$busy" = 0 ] || { echo "STOP: a clip is being built"; return 1; }
  python3 -B "$SCRIPT_DIR/upgrade_check.py" "${FORCE[@]}"
}

preflight
git clone --no-checkout -- https://github.com/Khalidshar/snap-status-v3 "$CLONE"
git -C "$CLONE" -c core.hooksPath=/dev/null checkout --detach "$C"
echo "CLONE=$CLONE"
preflight
python3 "$RELEASE" --source "$CLONE" --commit "$C" \
  --node-bin /opt/madar/node-runtime/bin/node --owner khalid \
  --telegram-env /etc/studio-v2/snap-telegram-test.env \
  --telegram-main-env /etc/madar/snap-telegram-main.env --telegram-main-bot khalid_s2ss_bot
preflight
python3 "$RELEASE" --activate-only --commit "$C"
case "$(readlink -f /opt/madar/current)" in
  "/opt/madar/releases/$C") ;;
  *) echo "STOP: activation did not switch"; exit 1 ;;
esac
systemctl restart "${UNITS[@]}"
sleep 5
# Owner 22 Nov 2026 («نظفي جميع الاكواد المكررة»): no code copies pile up. The installed
# release and the one before it (the ROLLBACK line) stay; the clone and older releases go.
rm -rf -- "$CLONE"
for old_release in /opt/madar/releases/*; do
  name=${old_release##*/}
  [[ "$name" =~ ^[0-9a-f]{40}$ ]] || continue
  [ "$name" = "$C" ] || [ "$name" = "$OLD_COMMIT" ] || rm -rf -- "$old_release"
done
