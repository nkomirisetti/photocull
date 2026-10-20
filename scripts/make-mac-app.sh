#!/bin/bash
# Build PhotoCull.app (default: ~/Applications/PhotoCull.app).
#
# Opening the app starts PhotoCull in the background if it isn't already
# running, then opens it in your browser. Stop it with "Quit PhotoCull" on
# the start page. Logs go to ~/Library/Logs/PhotoCull.log.
#
# The app runs this checkout's .venv, so rerun this script if you move the repo.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
BIN="$REPO/.venv/bin/photocull"
APP="${1:-$HOME/Applications/PhotoCull.app}"
PORT=8765
LOG="$HOME/Library/Logs/PhotoCull.log"

if [ ! -x "$BIN" ]; then
  echo "Set up the virtualenv first:  python3 -m venv .venv && .venv/bin/pip install -e ." >&2
  exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

cat > "$WORK/PhotoCull.applescript" <<EOF
set pcURL to "http://127.0.0.1:$PORT/"
set checkCmd to "curl -s -o /dev/null -m 1 " & pcURL & " && echo up || echo down"
if (do shell script checkCmd) is "down" then
	do shell script "nohup " & quoted form of "$BIN" & " --no-open --port $PORT > " & quoted form of "$LOG" & " 2>&1 &"
	repeat 50 times
		if (do shell script checkCmd) is "up" then exit repeat
		delay 0.1
	end repeat
end if
open location pcURL
EOF

mkdir -p "$(dirname "$APP")"
rm -rf "$APP"
osacompile -o "$APP" "$WORK/PhotoCull.applescript"
"$REPO/.venv/bin/python" "$REPO/scripts/make_icon.py" "$APP/Contents/Resources/applet.icns"
# osacompile's asset catalog icon would win over applet.icns
rm -f "$APP/Contents/Resources/Assets.car"
/usr/libexec/PlistBuddy -c "Delete :CFBundleIconName" "$APP/Contents/Info.plist" 2>/dev/null || true
codesign --force --sign - "$APP"  # re-sign after changing the bundle
touch "$APP"  # nudge Finder/Dock to pick up the new icon

echo "Built $APP"
echo "Open it from Spotlight or Launchpad, or drag it to the Dock."
