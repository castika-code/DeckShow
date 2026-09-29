# Developer Guide

Users only interact with `Install-Mac.command` / `Uninstall-Mac.command` (macOS) and `Install-Win.cmd` / `Uninstall-Win.cmd` (Windows) located in the package root. The `installer/` directory contains the underlying deployment scripts. This package folder is strictly for source code and distribution, holding only what is needed to modify and build the project.

## Directory Layout

| Path | Used By | Description |
|---|---|---|
| `Install-Mac.command`, `Uninstall-Mac.command`, `Install-Win.cmd`, `Uninstall-Win.cmd` | User (via double-click) | The only entry points for end-users. They run `installer/install.sh` and `uninstall.sh` (macOS) or `installer/install.ps1` and `uninstall.ps1` (Windows). |
| `Install-Win.cmd`, `Uninstall-Win.cmd` | User (via double-click, Windows) | Same on Windows; they run `installer/install.ps1` and `uninstall.ps1`. |
| `deckshow.py` | Plugin, Daemon, Companion adapter | The whole program in one file, macOS and Windows. Run as `deckshow.py host` (what the login item runs: microphone daemon + Companion adapter in one process, two threads, separate log files), `deckshow.py plugin <SDK args>` (Stream Deck plugin, started by the Stream Deck app), or `daemon` / `companion` alone for debugging. Sections in file order: per-OS paths, idle and display watchers, rendering, GridShow, audio analyzer/client/daemon, Stream Deck protocol and plugin, Companion API/registry/page file/module endpoint/adapter, entry point. |
| `streamdeck/com.castika.deckshow.sdPlugin/` | Stream Deck App (via symlink) | The Stream Deck plugin payload. Contains `manifest.json`, `ui/inspector.html` (Property Inspector), `bin/plugin` (launcher), assets (`fonts/`, `imgs/`, `default_glyphs.txt`), and the 8 generated `Castika DeckShow Profile (<model>).streamDeckProfile` files. |
| `companion/castika-deckshow-<ver>.tgz` | Companion (Modules > Import) | Pre-built Companion module. Users import this directly; Node.js is not required on the host environment. |
| `installer/` | `Install-Mac.command` / `Install-Win.cmd` | Deployment assets. macOS: `install.sh` (in place: venv, daemon app, launchd, plugin symlink), `uninstall.sh`, `daemon_app.sh` (packages `Castika.DeckShow.app` locally; refuses standalone execution). Windows: `install.ps1`, `uninstall.ps1`, `launcher.cs` (compiled on the user's PC by Windows' built-in C# compiler into `Castika.DeckShow.exe`). Shared: `requirements.txt`, `deckshow_icon.png`. |
| `dev/make_profiles.py` | Developer | Generates the 8 profiles and updates the `Profiles[]` array in the manifest. Uses only the Python standard library; output is deterministic (same input, byte-identical files). |
| `dev/companion-module/` | Developer | Source code for the Companion module (`main.js`, `package.json`, `companion/manifest.json`, `companion/HELP.md`) and the `build.sh` script. |

### What the installer creates
*(All by `Install-Mac.command` / `Install-Win.cmd`. Nothing is copied: the folder the user put the package in is the installation.)*

| Location | Description |
|---|---|
| the package folder | Gains `.venv/`, `logs/`, `.runtime/` and the launcher: `Castika.DeckShow.app` (macOS, built by `installer/daemon_app.sh` with osacompile) or `Castika.DeckShow.exe` (Windows, compiled from `installer/launcher.cs` by the built-in C# compiler, also copied into the plugin's `bin\`). |
| `~/Library/LaunchAgents/com.castika.deckshow.plist` (macOS) / Startup-folder shortcut `Castika.DeckShow.lnk` (Windows) | Starts the launcher at login. It runs `deckshow.py host` (the microphone daemon and the Companion adapter as two threads of one process), so Login Items / Startup apps show one entry, Castika.DeckShow, with one Python process under it. |
| `~/Library/Application Support/com.elgato.StreamDeck/Plugins/com.castika.deckshow.sdPlugin` (macOS symlink) / `%APPDATA%\Elgato\StreamDeck\Plugins\com.castika.deckshow.sdPlugin` (Windows junction) | Points at `streamdeck/com.castika.deckshow.sdPlugin` inside the package. |
| `~/Library/Application Support/DeckShow/` / `%APPDATA%\DeckShow\` | User fonts and Companion settings. |

*Note: `Uninstall-Mac.command` / `Uninstall-Win.cmd` stop the processes and remove the login item, the plugin link and the user data folder. The package folder itself is left for the user to delete. Neither script ever starts, quits or restarts the Stream Deck app; when it is running, the user is told to restart it. On Windows the venv's `pythonw.exe` is also copied as `.venv\Scripts\Castika.DeckShow.exe` so Task Manager and the microphone privacy list show that name (the copy keeps the Python Software Foundation's Authenticode signature); no PyInstaller and no code-signing certificate are involved.*

## Out-of-Tree Builds

The package directory is treated as source and distribution only. All build processes are executed inside `~/deckshow-build/` (or the `DECKSHOW_BUILD_DIR` override), never in the package. Only the final build artifacts are written back to this source tree:

| Command (Run from package root) | Dependencies | Output Artifacts |
|---|---|---|
| `python3 dev/make_profiles.py` | Python 3 | 8 Stream Deck profiles and the `Profiles[]` array in the plugin manifest. |
| `dev/companion-module/build.sh` | Node.js (npm) | `companion/castika-deckshow-<ver>.tgz` |

*Note on Companion:* End-users never need Node.js installed. Companion runs the imported `.tgz` module utilizing its own bundled runtime.

## Release Checklist

1. Bump the `Version` in `streamdeck/com.castika.deckshow.sdPlugin/manifest.json` and the `version` in `dev/companion-module/package.json` + `companion/manifest.json`. *(Companion refuses to re-import a module if the version number remains unchanged).*
2. Run `python3 dev/make_profiles.py` *(Only required if a grid layout or an action UUID was modified).*
3. Run `dev/companion-module/build.sh`.
4. Verify a clean package state: Ensure no paths or references point outside the package and no local build leftovers exist (e.g., `.venv`, `node_modules`, `pkg`, `__pycache__`, `logs`, `.runtime`, `*.app`).

## License

MIT for the whole package, including the Companion module (Companion requires MIT for module sources; keep `package.json` and `companion/manifest.json` at `MIT`).
