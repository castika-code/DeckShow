#!/bin/bash
# Double-click to install. macOS Gatekeeper blocks this file the first time (it
# came from a download): System Settings > Privacy & Security > "Open Anyway",
# then double-click again. Terminal users can run installer/install.sh directly.
cd "$(dirname "$0")" && DECKSHOW_ENTRY=command ./installer/install.sh
echo
read -r -p "Press Return to close this window."
