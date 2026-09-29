# Reverses install.ps1. Not for direct use: Uninstall-Win.cmd runs it.
# What it does, in order:
#   1. stops Castika.DeckShow (launcher, audio daemon, Companion adapter, plugin)
#      and removes the Startup-folder shortcut
#   2. removes the plugin junction from the Stream Deck app's Plugins folder
#   3. removes %APPDATA%\DeckShow (user fonts, Companion settings): data this
#      program created outside the install folder
#   4. removes what the installer made inside this folder (.venv, logs,
#      .runtime, Castika.DeckShow.exe and the copy of it in the plugin's bin),
#      so what is left is exactly the files that came out of the ZIP
# What it does NOT do: it never starts, stops or restarts the Stream Deck app,
# and it does not delete this folder or anything you put in it: the package is
# the install folder, and it is yours to delete when you want.
$ErrorActionPreference = "Continue"

function Step($msg) { Write-Host ""; Write-Host "==> $msg" }

if ($env:DECKSHOW_ENTRY -ne "command") {
    Write-Host "Note: the double-click uninstaller is Uninstall-Win.cmd, one folder up. Continuing here."
}

$Target = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)   # installer\ -> the install folder (the package itself)
$PluginId = "com.castika.deckshow"
$SdPlugins = Join-Path $env:APPDATA "Elgato\StreamDeck\Plugins"
$Shortcut = Join-Path ([Environment]::GetFolderPath("Startup")) "Castika.DeckShow.lnk"
$UserData = Join-Path $env:APPDATA "DeckShow"

# If the Startup shortcut points at another folder, this package was moved (or
# this is a copy of it): that folder holds a .venv, logs and a launcher too, and
# after this run nothing would point at them any more. Clear out there what an
# install creates, and nothing else.
$Prev = ""
if (Test-Path $Shortcut) {
    $PrevTarget = (New-Object -ComObject WScript.Shell).CreateShortcut($Shortcut).TargetPath
    if ($PrevTarget) { $Prev = Split-Path -Parent $PrevTarget }
}

Step "Login item"
# The launcher, the venv's renamed pythonw.exe (daemon, adapter) and the plugin
# process all carry this name.
Get-Process -Name "Castika.DeckShow" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 500
if (Test-Path $Shortcut) { Remove-Item $Shortcut -Force }
Write-Host "Castika.DeckShow (audio daemon + Companion adapter) stopped and removed from Startup"

Step "Stream Deck plugin"
$Link = Join-Path $SdPlugins "$PluginId.sdPlugin"
if (Test-Path $Link) { & cmd /c "rmdir `"$Link`"" 2>$null; if (Test-Path $Link) { Remove-Item $Link -Recurse -Force } }
if (Get-Process -Name "StreamDeck" -ErrorAction SilentlyContinue) {
    Write-Host "plugin link removed. The Stream Deck app is running; it forgets the plugin the next time you start it."
} else {
    Write-Host "plugin link removed"
}

Step "User data"
if (Test-Path $UserData) { Remove-Item $UserData -Recurse -Force }
Write-Host "removed $UserData (fonts, Companion settings)"

Step "What the installer made inside this folder"
$Leftovers = @(".venv", "logs", ".runtime", "__pycache__", "Castika.DeckShow.exe",
               "streamdeck\$PluginId.sdPlugin\bin\Castika.DeckShow.exe")
foreach ($l in $Leftovers) {
    $path = Join-Path $Target $l
    if (Test-Path $path) {
        Remove-Item $path -Recurse -Force -ErrorAction SilentlyContinue
        if (Test-Path $path) { Write-Host "could not remove $l (a program may still be using it)" }
        else { Write-Host "removed $l" }
    }
}
Write-Host "what is left in this folder is what came out of the ZIP"

if ($Prev -and (Test-Path $Prev) -and ($Prev.TrimEnd('\') -ine $Target.TrimEnd('\'))) {
    Step "An install in another folder"
    Write-Host $Prev
    foreach ($l in $Leftovers) {
        $path = Join-Path $Prev $l
        if (Test-Path $path) { Remove-Item $path -Recurse -Force -ErrorAction SilentlyContinue; Write-Host "removed $l there" }
    }
    Write-Host "the rest of that folder is yours to delete: $Prev"
}

Step "Done"
Write-Host "Nothing of Castika DeckShow runs or starts at login any more."
Write-Host "This folder is yours to delete when you like: $Target"
Write-Host "Microphone: Windows keeps its setting per app type, so nothing of ours is left in Settings > Privacy & security > Microphone."
