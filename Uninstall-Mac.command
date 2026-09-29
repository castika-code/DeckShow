#!/bin/bash
# Double-click to uninstall (same Gatekeeper note as Install-Mac.command).
# Terminal users can run installer/uninstall.sh directly.
cd "$(dirname "$0")" && DECKSHOW_ENTRY=command ./installer/uninstall.sh
echo
read -r -p "Press Return to close this window."
