#!/bin/bash
# Installs Castika DeckShow in place: the folder this package sits in (wherever
# the user put it) is the install folder. Nothing is copied anywhere. The
# Python environment, the app and the logs are created inside it; the only
# things placed elsewhere are the macOS/Stream Deck hooks that cannot live
# anywhere else:
#   ~/Library/LaunchAgents/com.castika.deckshow.plist   (Castika.DeckShow.app at login: audio daemon + Companion adapter)
#   ~/Library/Application Support/com.elgato.StreamDeck/Plugins/com.castika.deckshow.sdPlugin -> streamdeck/ (symlink into the install folder)
#                                                          (symlink into the install folder)
#   ~/Library/Application Support/DeckShow/fonts   (user fonts, created by the plugin on demand)
# uninstall.sh unregisters all of them; the folders themselves are left for the user to delete.
#
# Usage:  double-click Install-Mac.command (or run installer/install.sh) in the
#         folder where the package should live. Run it again after moving the
#         folder or updating the files: it just re-wires.
set -euo pipefail

# Opened directly (not through Install-Mac.command)? Fine from a terminal; just say
# where the double-click entry is, in case someone clicked this file by mistake.
if [ "${DECKSHOW_ENTRY:-}" != "command" ]; then
  echo "Note: the double-click installer is Install-Mac.command, one folder up. Continuing here."
fi

SRC="$(dirname "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)")"  # installer/ -> package root
TARGET="$SRC"  # in place
PLUGIN_ID="com.castika.deckshow"
SD_PLUGINS="$HOME/Library/Application Support/com.elgato.StreamDeck/Plugins"
SD_APP="Elgato Stream Deck"

step() { printf '\n==> %s\n' "$*"; }

sd_running() { pgrep -f "$SD_APP.app/Contents/MacOS" >/dev/null; }
# The Stream Deck app is never started, quit or restarted by this script: it
# only reads Plugins/ on launch, so if it is running the user restarts it
# themselves (told at the end). A Companion user who keeps it closed must not
# have it opened for them.

# 0. Which host is here? The Stream Deck app launches the plugin; Companion
#    loads the module. Either one is enough; only when neither is installed is
#    there nothing to install for.
step "Checking Stream Deck app and Companion"
HAVE_SD=0; HAVE_COMPANION=0
if [ -d "/Applications/$SD_APP.app" ]; then
  HAVE_SD=1; echo "Stream Deck app: found /Applications/$SD_APP.app"
else
  echo "Stream Deck app: not installed (skipping the Stream Deck plugin)"
fi
if [ -d "/Applications/Companion.app" ] || [ -d "$HOME/Library/Application Support/companion" ]; then
  HAVE_COMPANION=1; echo "Bitfocus Companion: found"
else
  echo "Bitfocus Companion: not installed"
fi
if [ "$HAVE_SD" = 0 ] && [ "$HAVE_COMPANION" = 0 ]; then
  echo "Neither the Stream Deck app (https://www.elgato.com/downloads) nor Bitfocus Companion (https://bitfocus.io/companion) is installed."
  echo "Install one of them first, then run Install-Mac.command again."
  exit 1
fi

# 1. Python: the project runs on Apple's Command Line Tools python3 (3.9+).
#    On a Mac without it, this triggers Apple's own installer dialog.
step "Checking python3"
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
  echo "python3 3.9+ not found. Requesting Apple's Command Line Tools..."
  xcode-select --install 2>/dev/null || true
  echo "Finish that installer, then run Install-Mac.command again."
  exit 1
fi
python3 --version

# 1b. Was this package installed from another folder before? The login item
#     says where (it carries that folder as its working directory). Its .venv,
#     logs and app would sit there for good, and the login item is about to
#     point here, so clear out exactly what an install creates there.
#     Everything else in that folder is the user's.
PLIST="$HOME/Library/LaunchAgents/com.castika.deckshow.plist"
PREV=""
if [ -f "$PLIST" ]; then
  PREV="$(/usr/libexec/PlistBuddy -c 'Print :WorkingDirectory' "$PLIST" 2>/dev/null || true)"
fi
if [ -n "$PREV" ] && [ "$PREV" != "$TARGET" ] && [ -d "$PREV" ]; then
  step "Earlier install in another folder"
  echo "$PREV"
  launchctl bootout "gui/$(id -u)/com.castika.deckshow" 2>/dev/null || true
  pkill -TERM -f "$PREV/deckshow.py" 2>/dev/null && sleep 1 || true
  for leftover in ".venv" "logs" ".runtime" "Castika.DeckShow.app"; do
    if [ -e "$PREV/$leftover" ]; then
      rm -rf "${PREV:?}/${leftover:?}"
      echo "removed $leftover there"
    fi
  done
  echo "the rest of that folder is yours to delete: $PREV"
fi

# 2. The install folder is this one.
step "Install folder: $TARGET"
chmod +x "$TARGET"/installer/*.sh "$TARGET/Install-Mac.command" "$TARGET/Uninstall-Mac.command" "$TARGET/streamdeck/$PLUGIN_ID.sdPlugin/bin/plugin"
cd "$TARGET"

# 3. Python packages, isolated in the install folder.
step "Python packages"
[ -x .venv/bin/python3 ] || python3 -m venv .venv
# --no-cache-dir: pip would otherwise keep the downloaded wheels in the
# account's shared cache (~/Library/Caches/pip), which is not ours to clean
# up on uninstall. Nothing is cached, so nothing is left behind.
.venv/bin/pip install -q --disable-pip-version-check --no-cache-dir -r installer/requirements.txt
echo "ok"

# 4. Audio daemon app + launchd agent (only process that opens the microphone).
step "Audio daemon"
DECKSHOW_INSTALLER=1 ./installer/daemon_app.sh

# (The Companion adapter is started by Castika.DeckShow.app too, so it shows up as
#  one login item named Castika.DeckShow instead of a bare "python3".)

# 5. Plugin: symlink into Stream Deck's plugin folder (only if the app is here).
SD_RESTART_NEEDED=0
if [ "$HAVE_SD" = 1 ]; then
  step "Stream Deck plugin"
  mkdir -p "$SD_PLUGINS"
  rm -rf "$SD_PLUGINS/$PLUGIN_ID.sdPlugin"
  ln -s "$TARGET/streamdeck/$PLUGIN_ID.sdPlugin" "$SD_PLUGINS/$PLUGIN_ID.sdPlugin"
  if sd_running; then
    # A plugin process from an earlier install may still be running out of the
    # install folder; stop it so the app starts the fresh one on its next launch.
    pkill -TERM -f "$TARGET/deckshow.py plugin" 2>/dev/null && sleep 1 || true
    echo "plugin linked, but the Stream Deck app is running and does not see it yet."
    SD_RESTART_NEEDED=1
  else
    echo "plugin linked; it loads when you start the Stream Deck app"
  fi
fi

step "Done"
echo "Installed to: $TARGET"
echo
echo "  ============================================================"
echo "   WHAT TO DO NOW"
echo

N=0
item() { N=$((N + 1)); printf '   %d. %s\n' "$N" "$1"; }
more() { printf '      %s\n' "$1"; }

if [ "$HAVE_SD" = 1 ]; then
  if [ "$SD_RESTART_NEEDED" = 1 ]; then
    item "RESTART THE STREAM DECK APP NOW, or nothing will show up."
    more "Quit it (Stream Deck menu > Quit) and start it again. A"
    more "running app reads new plugins only when it starts."
  else
    item "Start the Stream Deck app. It loads the plugin as it starts."
  fi
  echo
  item "APPROVE THE PROFILE. The app asks to add \"Castika DeckShow"
  more "Profile\" to your deck. Say yes: the show runs on that"
  more "profile of its own, so the pages you made stay as they are."
  echo
  item "Put a button on your deck: drag \"DeckShow Button\" (category"
  more "Castika DeckShow) onto any button."
  echo
  item "Settings: select that button in the app and they appear beside it."
  more "If that panel stays blank (some graphics drivers cannot draw"
  more "it), open http://127.0.0.1:18790/pi in a browser instead: same"
  more "page, same settings."
  echo
else
  item "Install the Stream Deck app if you use one, then run"
  more "Install-Mac.command again to add the plugin."
  echo
fi

item "Let it hear the room. macOS asks for microphone access for"
more "\"Castika.DeckShow\" when the first show starts: allow it."

if [ "$HAVE_COMPANION" = 1 ]; then
  echo
  item "Bitfocus Companion: Modules > Import module package >"
  more "$TARGET/companion/castika-deckshow-*.tgz, add the"
  more "connection, then drag \"DeckShow Button\" presets onto your buttons."
fi
echo "  ============================================================"
echo
