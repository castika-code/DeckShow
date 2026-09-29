# Windows installer for Castika DeckShow. Not for direct use: Install-Win.cmd runs it.
# Installs in place, like installer/install.sh: the folder this package sits in
# (wherever the user put it) is the install folder; nothing is copied anywhere.
#   .venv\                              Python packages, isolated in the install folder
#   .venv\Scripts\Castika.DeckShow.exe  copy of the venv's pythonw.exe, so the process
#                                       and the microphone privacy list show our name
#   Castika.DeckShow.exe                launcher compiled here with Windows' own C#
#                                       compiler (installer\launcher.cs); runs the audio
#                                       daemon and the Companion adapter
#   Startup folder shortcut             starts the launcher at login
#   %APPDATA%\Elgato\StreamDeck\Plugins\com.castika.deckshow.sdPlugin
#                                       directory junction into the install folder
# Uninstall-Win.cmd unregisters all of them; the folders themselves are left for the user to delete.
$ErrorActionPreference = "Stop"

function Step($msg) { Write-Host ""; Write-Host "==> $msg" }

if ($env:DECKSHOW_ENTRY -ne "command") {
    Write-Host "Note: the double-click installer is Install-Win.cmd, one folder up. Continuing here."
}

$Src = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)   # installer\ -> package root
$Target = $Src   # in place
$PluginId = "com.castika.deckshow"
$SdPlugins = Join-Path $env:APPDATA "Elgato\StreamDeck\Plugins"
$SdExe = Join-Path $env:ProgramFiles "Elgato\StreamDeck\StreamDeck.exe"
$Startup = [Environment]::GetFolderPath("Startup")
$Shortcut = Join-Path $Startup "Castika.DeckShow.lnk"

# 0. Which host is here? The Stream Deck app launches the plugin; Companion
#    loads the module. Either one is enough; only when neither is installed is
#    there nothing to install for.
Step "Checking Stream Deck app and Companion"
$HaveSd = Test-Path $SdExe
if ($HaveSd) { Write-Host "Stream Deck app: found $SdExe" } else { Write-Host "Stream Deck app: not installed (skipping the Stream Deck plugin)" }
$HaveCompanion = (Test-Path (Join-Path $env:APPDATA "companion")) -or (Test-Path (Join-Path $env:ProgramFiles "Companion")) -or (Test-Path (Join-Path $env:ProgramFiles "Bitfocus"))
if ($HaveCompanion) { Write-Host "Bitfocus Companion: found" } else { Write-Host "Bitfocus Companion: not installed" }
if (-not $HaveSd -and -not $HaveCompanion) {
    Write-Host "Neither the Stream Deck app (https://www.elgato.com/downloads) nor Bitfocus Companion (https://bitfocus.io/companion) is installed."
    Write-Host "Install one of them first, then run Install-Win.cmd again."
    exit 1
}

# 1. Python 3.9+: Windows has none built in. We point at python.org's Windows
#    installer, which needs no administrator rights and keeps its files where
#    the program expects them (a Microsoft Store Python runs inside a package
#    and redirects what it writes to %APPDATA%, which would put this program's
#    settings somewhere else than where the uninstaller looks).
Step "Checking Python"
$Python = $null
foreach ($cand in @("py -3", "python")) {
    try {
        $v = & cmd /c "$cand -c `"import sys; print(sys.version_info >= (3, 9))`" 2>nul"
        if ($v -match "True") { $Python = $cand; break }
    } catch { }
}
if (-not $Python) {
    Write-Host "Python 3.9 or newer was not found."
    Write-Host "Get the Windows installer from https://www.python.org/downloads/windows/ (no administrator rights needed):"
    Write-Host "take the version that page offers as its download button, and tick 'Add python.exe to PATH'."
    Write-Host "Then run Install-Win.cmd again. If a brand-new Python makes the next step fail with a pip"
    Write-Host "error, install the previous version instead (for example 3.13 where 3.14 has just appeared)."
    Start-Process "https://www.python.org/downloads/windows/"
    exit 1
}
Write-Host (& cmd /c "$Python --version")

# 2. Stop anything running from a previous install (files would be locked).
Step "Stopping a previous instance"
Get-Process -Name "Castika.DeckShow" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 500

# 2b. Was this package installed from another folder before? The Startup
#     shortcut says where. Its .venv, logs and launcher would sit there for
#     good, and the shortcut is about to point here, so clear out exactly what
#     an install creates there. Everything else in that folder is the user's.
$Prev = ""
if (Test-Path $Shortcut) {
    $PrevTarget = (New-Object -ComObject WScript.Shell).CreateShortcut($Shortcut).TargetPath
    if ($PrevTarget) { $Prev = Split-Path -Parent $PrevTarget }
}
if ($Prev -and (Test-Path $Prev) -and ($Prev.TrimEnd('\') -ine $Target.TrimEnd('\'))) {
    Step "Earlier install in another folder"
    Write-Host $Prev
    Get-Process -Name "Castika.DeckShow" -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -and $_.Path.StartsWith($Prev, [StringComparison]::OrdinalIgnoreCase) } |
        Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 500
    foreach ($leftover in @(".venv", "logs", ".runtime", "Castika.DeckShow.exe")) {
        $path = Join-Path $Prev $leftover
        if (Test-Path $path) { Remove-Item $path -Recurse -Force -ErrorAction SilentlyContinue; Write-Host "removed $leftover there" }
    }
    Write-Host "the rest of that folder is yours to delete: $Prev"
}

# 3. The install folder is this one.
Step "Install folder: $Target"
Set-Location $Target
New-Item -ItemType Directory -Force -Path (Join-Path $Target "logs") | Out-Null

# 4. Python packages, isolated in the install folder.
Step "Python packages"
if (-not (Test-Path ".venv\Scripts\python.exe")) { & cmd /c "$Python -m venv .venv"; if ($LASTEXITCODE) { throw "venv failed" } }
# pip's own output goes to logs\pip.log as well as the screen: when a package
# has no wheel for this Python, the reason is in there and nowhere else.
$PipLog = Join-Path $Target "logs\pip.log"
# --no-cache-dir: pip would otherwise keep the downloaded wheels in the
# account's shared cache (%LOCALAPPDATA%\pip\Cache), which is not ours to
# clean up on uninstall. Nothing is cached, so nothing is left behind.
& ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check --no-cache-dir -r "installer\requirements.txt" 2>&1 | Tee-Object -FilePath $PipLog
if ($LASTEXITCODE) {
    Write-Host ""
    Write-Host "pip could not install the packages. The full output is in $PipLog"
    Write-Host "Send that file if you need help with it."
    throw "pip install failed"
}
Copy-Item ".venv\Scripts\pythonw.exe" ".venv\Scripts\Castika.DeckShow.exe" -Force
Write-Host "ok"

# 5. Launcher: compiled on this machine with the C# compiler that ships in Windows.
Step "Launcher (Castika.DeckShow.exe)"
$Csc = $null
foreach ($fw in @("Framework64", "Framework")) {
    $c = Join-Path $env:WINDIR "Microsoft.NET\$fw\v4.0.30319\csc.exe"
    if (Test-Path $c) { $Csc = $c; break }
}
if (-not $Csc) { throw "csc.exe (part of .NET Framework 4, included in Windows 10/11) was not found" }
# The icon is compiled into the executable, so Task Manager, the Startup apps
# list and the microphone privacy list show it next to our name.
$Ico = Join-Path $Target "installer\deckshow_icon.ico"
$CscArgs = @("/nologo", "/target:winexe", "/optimize+", "/out:$Target\Castika.DeckShow.exe", "$Target\installer\launcher.cs")
$IconArgs = @()
if (Test-Path $Ico) { $IconArgs = @("/win32icon:$Ico") }
& $Csc ($IconArgs + $CscArgs)
if ($LASTEXITCODE -and $IconArgs.Count) {
    # An icon the compiler will not take must not cost the user the install.
    Write-Host "icon could not be embedded; building without it"
    & $Csc $CscArgs
}
if ($LASTEXITCODE) { throw "launcher build failed" }
Copy-Item "$Target\Castika.DeckShow.exe" "$Target\streamdeck\$PluginId.sdPlugin\bin\Castika.DeckShow.exe" -Force
Write-Host "built"

# 6. Start at login (Startup folder shortcut, no admin needed) and start it now.
Step "Login item"
$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut($Shortcut)
$lnk.TargetPath = "$Target\Castika.DeckShow.exe"
$lnk.WorkingDirectory = $Target
$lnk.Description = "Castika DeckShow (audio daemon + Companion adapter)"
$lnk.Save()
Start-Process "$Target\Castika.DeckShow.exe" -WorkingDirectory $Target
Write-Host "registered: $Shortcut"

# 7. Plugin: junction into Stream Deck's plugin folder (only if the app is here).
#    The Stream Deck app is never started, stopped or restarted here: it reads
#    Plugins\ only at launch, so if it is running the user restarts it
#    themselves (told at the end).
$SdRunning = $false
if ($HaveSd) {
    Step "Stream Deck plugin"
    New-Item -ItemType Directory -Force -Path $SdPlugins | Out-Null
    $Link = Join-Path $SdPlugins "$PluginId.sdPlugin"
    if (Test-Path $Link) { & cmd /c "rmdir `"$Link`"" 2>$null; if (Test-Path $Link) { Remove-Item $Link -Recurse -Force } }
    & cmd /c "mklink /J `"$Link`" `"$Target\streamdeck\$PluginId.sdPlugin`"" | Out-Null
    $SdRunning = [bool](Get-Process -Name "StreamDeck" -ErrorAction SilentlyContinue)
    if ($SdRunning) {
        Write-Host "plugin linked, but the Stream Deck app is running and does not see it yet."
    } else {
        Write-Host "plugin linked; it loads when you start the Stream Deck app"
    }
}

Step "Done"
Write-Host "Installed to: $Target"
Write-Host ""
Write-Host "  ============================================================"
Write-Host "   WHAT TO DO NOW"
Write-Host ""

$script:N = 0
function Item($t) { $script:N++; Write-Host ("   {0}. {1}" -f $script:N, $t) }
function More($t) { Write-Host ("      $t") }

if ($HaveSd) {
    if ($SdRunning) {
        Item "RESTART THE STREAM DECK APP NOW, or nothing will show up."
        More "Quit it (right-click its icon near the clock > Quit) and"
        More "start it again. A running app reads new plugins only when"
        More "it starts."
    } else {
        Item "Start the Stream Deck app. It loads the plugin as it starts."
    }
    Write-Host ""
    Item "APPROVE THE PROFILE. The app asks to add `"Castika DeckShow"
    More "Profile`" to your deck. Say yes: the show runs on that"
    More "profile of its own, so the pages you made stay as they are."
    Write-Host ""
    Item "Put a button on your deck: drag `"DeckShow Button`" (category"
    More "Castika DeckShow) onto any button."
    Write-Host ""
    Item "Settings: select that button in the app and they appear beside it."
    More "If that panel stays blank (some graphics drivers cannot draw"
    More "it), open http://127.0.0.1:18790/pi in a browser instead: same"
    More "page, same settings."
    Write-Host ""
} else {
    Item "Install the Stream Deck app if you use one, then run"
    More "Install-Win.cmd again to add the plugin."
    Write-Host ""
}

Item "Let it hear the room. Open Settings > Privacy & security >"
More "Microphone and turn on both `"Microphone access`" and"
More "`"Let desktop apps access your microphone`"."

if ($HaveCompanion) {
    Write-Host ""
    Item "Bitfocus Companion: Modules > Import module package >"
    More "$Target\companion\castika-deckshow-*.tgz, add the"
    More "connection, then drag `"DeckShow Button`" presets onto your buttons."
}
Write-Host "  ============================================================"
Write-Host ""
