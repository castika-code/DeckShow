#!/bin/bash
# Not for direct use: install.sh calls this. Builds Castika.DeckShow.app on the user's
# Mac (a local osacompile applet passes Gatekeeper, and the microphone dialog
# then names this app) and registers it as the ONE launchd login agent; the
# applet runs deckshow.py host (audio daemon + Companion adapter in one process).
# The daemon is the ONLY process that ever touches the microphone:
# Stream Deck's own adapter process never does, by design, so this app's
# permission is the only microphone permission that matters for the whole project.
#
# daemon.py takes no CLI args, so (unlike the earlier abandoned attempt at
# wrapping the Stream Deck adapter itself) this applet does not need the
# NSProcessInfo argv workaround -- a plain "on run" is enough.
set -euo pipefail

# Only install.sh may run this (it sets DECKSHOW_INSTALLER). Run on its own it
# would build the app in the wrong place and register an agent pointing there.
if [ "${DECKSHOW_INSTALLER:-}" != "1" ]; then
  echo "This file is part of the installer and is not meant to be run by itself."
  echo "To install, double-click Install-Mac.command (one folder up). To remove, Uninstall-Mac.command."
  exit 1
fi
DIR="$(dirname "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)")"  # installer/ -> install folder (app, logs, deckshow.py live there)
APP="$DIR/Castika.DeckShow.app"  # this name is what the microphone dialog shows
APPLET="$APP/Contents/MacOS/Castika.DeckShow"  # osacompile names it "applet"; renamed below so macOS's "background activity" notice and Login Items say Castika.DeckShow
BUNDLE_ID="com.castika.deckshow.app"
LABEL="com.castika.deckshow"
OLD_LABELS="com.castika.deckledshow com.castika.deckledshow.companion com.castika.deckshow.companion"  # previous agent label(s) to retire (the adapter had its own agent before 2026-09-21)
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
MIC_TEXT="Castika.DeckShow listens to the microphone to drive the audio-reactive show on your Stream Deck."
mkdir -p "$DIR/logs"

APPLET_SRC="use framework \"Foundation\"
use scripting additions

-- The login item (Castika.DeckShow) runs one background process, deckshow.py
-- host: the audio daemon (the only thing that opens the microphone) and the
-- Companion adapter, as two threads. If it dies this applet quits and launchd
-- (KeepAlive) starts the applet, and with it the host, again.
property hostTask : missing value

on run
    set logPath to \"$DIR/logs/deckshow_applet.log\"
    set fm to current application's NSFileManager's defaultManager()
    if not (fm's fileExistsAtPath:logPath) then
        fm's createFileAtPath:logPath |contents|:(missing value) attributes:(missing value)
    end if
    set logHandle to current application's NSFileHandle's fileHandleForWritingAtPath:logPath
    logHandle's seekToEndOfFile()
    set t to current application's NSTask's alloc()'s init()
    t's setStartsNewProcessGroup:false
    t's setLaunchPath:\"$DIR/.venv/bin/python3\"
    t's setArguments:{\"$DIR/deckshow.py\", \"host\"}
    t's setStandardOutput:logHandle
    t's setStandardError:logHandle
    t's launchAndReturnError:(missing value)
    set hostTask to t
end run

on idle
    if hostTask is missing value then
        quit
        return 1
    end if
    if not ((hostTask's isRunning()) as boolean) then
        quit
        return 1
    end if
    return 5
end idle

on quit
    try
        if (hostTask's isRunning()) as boolean then
            do shell script \"kill \" & (hostTask's processIdentifier() as text)
        end if
    end try
    continue quit
end quit"

# Rebuilding re-signs the app with a fresh ad-hoc signature, and a changed
# signature makes macOS ask for the microphone permission again -- so this
# only actually rebuilds when something that matters changed, comparing a
# fingerprint stored inside the app (before
# codesign, so the signature covers it too) and comparing on every run.
STAMP_APP="$APP/Contents/Resources/build.stamp"
ICON_SRC="$DIR/installer/deckshow_icon.png"
ICON_SUM="none"
[ -f "$ICON_SRC" ] && ICON_SUM="$(shasum -a 256 "$ICON_SRC" | awk '{print $1}')"
FINGERPRINT="$DIR
$MIC_TEXT
$BUNDLE_ID
$APPLET
$APPLET_SRC
icon:$ICON_SUM"

if [ -d "$APP" ] && [ -x "$APPLET" ] && [ -f "$STAMP_APP" ] \
   && [ "$(cat "$STAMP_APP")" = "$FINGERPRINT" ]; then
  echo "Castika.DeckShow.app is up to date (kept, so the microphone permission stays granted)"
else

rm -rf "$APP"
TMP="$(mktemp -d)"
SRC="$TMP/deckshow.applescript"
printf '%s\n' "$APPLET_SRC" > "$SRC"
osacompile -s -o "$APP" "$SRC"
rm -rf "$TMP"
mv "$APP/Contents/MacOS/applet" "$APPLET"  # the applet stub finds its script through the bundle, not its own name

P="$APP/Contents/Info.plist"
# PlistBuddy's own "-c" mini-language treats quotes specially and chokes on an
# apostrophe inside a value wrapped in '...' (found the hard way: MIC_TEXT's
# "Show's" silently broke the Add call with "Parse Error: Unclosed Quotes" and
# left osacompile's generic placeholder text in place instead -- the actual
# mic-permission prompt was never showing our real text). plistlib sidesteps
# shell/PlistBuddy quoting entirely.
MIC_TEXT="$MIC_TEXT" BUNDLE_ID="$BUNDLE_ID" PLIST_PATH="$P" "$DIR/.venv/bin/python3" - <<'PY'
import os
import plistlib

path = os.environ["PLIST_PATH"]
with open(path, "rb") as f:
    data = plistlib.load(f)
data["NSMicrophoneUsageDescription"] = os.environ["MIC_TEXT"]
data["LSUIElement"] = True
data["CFBundleIdentifier"] = os.environ["BUNDLE_ID"]
data["CFBundleName"] = "Castika.DeckShow"
data["CFBundleExecutable"] = "Castika.DeckShow"
data["CFBundleDisplayName"] = "Castika.DeckShow"  # what the microphone dialog and System Settings show
data["NSHumanReadableCopyright"] = "\u00a9 2026 Castika"
with open(path, "wb") as f:
    plistlib.dump(data, f)
PY

# Icon: osacompile's
# default applet icon is otherwise what shows in the mic menu-bar dropdown and
# System Settings > Privacy > Microphone -- easy to miss since it still "works"
# without one, which is exactly how this got skipped the first time around.
if [ -f "$ICON_SRC" ]; then
  ICON_TMP="$(mktemp -d)"
  ICONSET="$ICON_TMP/deckshow.iconset"
  mkdir -p "$ICONSET"
  ICON_OK=1
  for spec in 16:icon_16x16 32:icon_16x16@2x 32:icon_32x32 64:icon_32x32@2x \
              128:icon_128x128 256:icon_128x128@2x 256:icon_256x256 512:icon_256x256@2x \
              512:icon_512x512 1024:icon_512x512@2x; do
    px="${spec%%:*}"; name="${spec#*:}"
    sips -s format png -z "$px" "$px" "$ICON_SRC" --out "$ICONSET/$name.png" >/dev/null 2>&1 || ICON_OK=0
  done
  mkdir -p "$APP/Contents/Resources"
  if [ "$ICON_OK" = "1" ] && iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/applet.icns" >/dev/null 2>&1; then
    PLIST_PATH="$P" "$DIR/.venv/bin/python3" - <<'PY'
import os
import plistlib

path = os.environ["PLIST_PATH"]
with open(path, "rb") as f:
    data = plistlib.load(f)
data["CFBundleIconFile"] = "applet"
data.pop("CFBundleIconName", None)  # would otherwise win over the .icns file
with open(path, "wb") as f:
    plistlib.dump(data, f)
PY
    rm -f "$APP/Contents/Resources/Assets.car"
    echo "Icon applied from deckshow_icon.png"
  else
    echo "Icon conversion failed; keeping the default applet icon."
  fi
  rm -rf "$ICON_TMP"
fi

printf '%s' "$FINGERPRINT" > "$STAMP_APP"
codesign -s - --force "$APP"
echo "Built $APP"

fi  # end fingerprint-check

# Tell LaunchServices about the bundle, on every run and not only after a
# rebuild. The name and the icon that the microphone dialog, the menu-bar
# microphone dropdown and System Settings show come from this record, not from
# the files, so a record that is missing or left over from an earlier version
# is what makes them show a stale name or the generic applet icon.
LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
if [ -x "$LSREGISTER" ]; then "$LSREGISTER" -f "$APP" >/dev/null 2>&1 || true; fi

# macOS keeps the rendered icons in a cache of its own, and that cache can go
# bad on its own (iconservicesagent then logs "Node exceeds minimal bounds" and
# every app it is asked about gets a placeholder icon). Nothing in an app can
# prevent or repair that, so only say something when this Mac has actually hit
# it, with the one command that rebuilds the cache.
ICON_FAULT="$(ls -t "$HOME/Library/Logs/DiagnosticReports"/*iconservicesagent*.ips 2>/dev/null | head -1)"
if [ -n "$ICON_FAULT" ]; then
  echo "Note: macOS's icon cache has reported an error on this Mac."
  echo "      If the microphone menu shows the wrong icon for Castika.DeckShow, rebuild that cache with:"
  echo "        rm -rf \"\$(getconf DARWIN_USER_CACHE_DIR)com.apple.iconservices\"; killall iconservicesagent Dock ControlCenter"
fi

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$APPLET</string>
  </array>
  <key>AssociatedBundleIdentifiers</key>
  <array><string>$BUNDLE_ID</string></array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$DIR/logs/deckshow_launchd.log</string>
  <key>StandardErrorPath</key><string>$DIR/logs/deckshow_launchd.log</string>
</dict>
</plist>
PL

for old in $OLD_LABELS; do
  launchctl bootout "gui/$(id -u)/$old" 2>/dev/null || true
  rm -f "$HOME/Library/LaunchAgents/$old.plist"
done
rm -rf "$DIR/Deck LED Show.app" "$DIR/DeckShow.app"  # bundle names before 2026-09-21
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
WAITED=0
while launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1 && [ "$WAITED" -lt 50 ]; do
  sleep 0.1
  WAITED=$((WAITED + 1))
done
ATTEMPT=1
while true; do
  if BOOTSTRAP_ERROR="$(launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>&1)"; then
    break
  fi
  if [ "$ATTEMPT" -ge 5 ]; then
    echo "The launchd agent could not be registered:"
    echo "$BOOTSTRAP_ERROR"
    exit 1
  fi
  sleep 1
  ATTEMPT=$((ATTEMPT + 1))
done
echo "Registered launchd agent: $LABEL (runs at every login, restarts if it dies)"
echo "First run: macOS should show a microphone permission dialog for \"Castika.DeckShow\"."
echo "Daemon log: $DIR/logs/deckshow.log (uncaught output: deckshow_applet.log)"
echo "launchd job log (only has output if launchd itself fails to start the applet): $DIR/logs/deckshow_launchd.log"
