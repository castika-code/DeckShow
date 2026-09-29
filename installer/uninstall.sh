#!/bin/bash
# Reverses install.sh. Not for direct use: Uninstall-Mac.command runs it.
# What it does, in order:
#   1. stops Castika.DeckShow.app (audio daemon + Companion adapter) and removes
#      its login item (~/Library/LaunchAgents/com.castika.deckshow.plist)
#   2. stops the plugin process, if the Stream Deck app is running it
#   3. removes the plugin link from the Stream Deck app's Plugins folder
#   4. removes ~/Library/Application Support/DeckShow (user fonts, Companion
#      settings): data this program created outside the install folder
#   5. removes what the installer made inside this folder (.venv, logs,
#      .runtime, Castika.DeckShow.app), and with the app the entry in System
#      Settings > Privacy & Security > Microphone, so what is left is exactly
#      the files that came out of the ZIP
# What it does NOT do: it never quits, starts or restarts the Stream Deck app,
# and it does not delete this folder or anything you put in it: the package is
# the install folder, and it is yours to delete when you want.
set -uo pipefail

if [ "${DECKSHOW_ENTRY:-}" != "command" ]; then
  echo "Note: the double-click uninstaller is Uninstall-Mac.command, one folder up. Continuing here."
fi

TARGET="$(dirname "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)")"  # installer/ -> the install folder (the package itself)
PLUGIN_ID="com.castika.deckshow"
LABEL="com.castika.deckshow"
SD_PLUGINS="$HOME/Library/Application Support/com.elgato.StreamDeck/Plugins"
SD_APP="Elgato Stream Deck"
USER_DATA="$HOME/Library/Application Support/DeckShow"
APP_NAME="Castika.DeckShow.app"
APP_ID="com.castika.deckshow.app"

step() { printf '\n==> %s\n' "$*"; }

# If the login item points at another folder, this package was moved (or this
# is a copy of it): that folder holds a .venv, logs and an app too, and after
# this run nothing would point at them any more. Clear out there what an
# install creates, and nothing else.
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PREV=""
if [ -f "$PLIST" ]; then
  PREV="$(/usr/libexec/PlistBuddy -c 'Print :WorkingDirectory' "$PLIST" 2>/dev/null || true)"
fi
if [ -n "$PREV" ] && [ "$PREV" != "$TARGET" ] && [ -d "$PREV" ]; then
  step "An install in another folder"
  echo "$PREV"
  pkill -TERM -f "$PREV/deckshow.py" 2>/dev/null && sleep 1 || true
  for leftover in ".venv" "logs" ".runtime" "__pycache__" "$APP_NAME"; do
    if [ -e "$PREV/$leftover" ]; then
      rm -rf "${PREV:?}/${leftover:?}"
      echo "removed $leftover there"
    fi
  done
  echo "the rest of that folder is yours to delete: $PREV"
fi

step "Login item"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
launchctl bootout "gui/$(id -u)/$LABEL.companion" 2>/dev/null  # agent of versions before 2026-09-21
rm -f "$HOME/Library/LaunchAgents/$LABEL.companion.plist"
echo "Castika.DeckShow (audio daemon + Companion adapter) stopped and removed from login items"

step "Stream Deck plugin"
# The plugin process belongs to the Stream Deck app; stop it so nothing keeps
# running from the install folder. The app itself is left exactly as it is.
pkill -TERM -f "$TARGET/deckshow.py plugin" 2>/dev/null && sleep 1
rm -rf "$SD_PLUGINS/$PLUGIN_ID.sdPlugin"
if pgrep -f "$SD_APP.app/Contents/MacOS" >/dev/null; then
  echo "plugin link removed. The Stream Deck app is running; it forgets the plugin the next time you start it."
else
  echo "plugin link removed"
fi

step "User data"
rm -rf "$USER_DATA"
echo "removed $USER_DATA (fonts, Companion settings)"

step "What the installer made inside this folder"
# The microphone permission is attached to the app bundle, so give it back
# before the bundle goes: afterwards macOS cannot resolve the id any more.
if [ -d "$TARGET/$APP_NAME" ]; then
  tccutil reset Microphone "$APP_ID" >/dev/null 2>&1 && echo "microphone permission for Castika.DeckShow given back"
fi
for leftover in ".venv" "logs" ".runtime" "__pycache__" "$APP_NAME"; do
  if [ -e "$TARGET/$leftover" ]; then
    rm -rf "$TARGET/$leftover"
    echo "removed $leftover"
  fi
done
echo "what is left in this folder is what came out of the ZIP"

step "Done"
echo "Nothing of Castika DeckShow runs or starts at login any more."
echo "This folder is yours to delete when you like: $TARGET"
