#!/usr/bin/env python3
"""Generates the bundled show profiles, one per Stream Deck model, and registers
them in manifest.json's Profiles[].

A .streamDeckProfile is a zip of four JSON files; the only model-specific part
is the key grid, so instead of exporting one from the app per model (which
needs that model on the desk) they are written here from a grid table. The
layout of the JSON is copied from the profile the Stream Deck app exported for
the Mini on 2026-09-19.
The device model code and device UUID that export carried are left out here:
the app copies a bundled profile into whichever device it is first switched
to, so those fields describe the exporting device, not a requirement.

Run from the project root after changing GRIDS or the plugin UUID:
    python3 dev/make_profiles.py   (from the package root)
"""
import io
import json
import uuid
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # dev/ sits inside the package root
PLUGIN_DIR = ROOT / "streamdeck" / "com.castika.deckshow.sdPlugin"
MANIFEST = PLUGIN_DIR / "manifest.json"
PROFILE_NAME = "Castika DeckShow Profile ({label})"  # manifest Profiles[].Name == file stem

# DeviceType (manifest enum, docs.elgato.com/streamdeck/sdk/references/manifest)
# -> (label, columns, rows). Only key grids; touch strips, dials and info bars
# are not part of the show. Verified on real hardware: Mini (1) only.
# The manifest can only tie a profile to a DeviceType, never to a grid size,
# and one type can come in several sizes (Virtual Stream Deck: 3x2, 5x3, ...
# as the user resizes it, seen 2026-09-20). So each type gets the LARGEST grid
# it can have: keys outside a smaller device's grid are simply not shown, and
# whatever is shown is all ours (사용자 확정: "격자값으로 처리해야").
GRIDS = {
    1: ("Mini", 3, 2),
    0: ("Stream Deck", 5, 3),
    2: ("XL", 8, 4),
    7: ("Plus", 4, 2),
    9: ("Neo", 4, 2),
    13: ("Plus XL", 9, 4),
    12: ("Galleon K100", 3, 4),
    11: ("Virtual", 9, 4),  # resizable; oversize on purpose, see above
}
# Grids match node-elgato-stream-deck's models/definitions.ts (2026-09-20).
# Studio (10, 16x2 non-square keys) is not supported by the renderer.

KEY_STATE = {
    "FontFamily": "", "FontSize": 12, "FontStyle": "", "FontUnderline": False,
    "OutlineThickness": 2, "ShowTitle": True, "TitleAlignment": "middle", "TitleColor": "#ffffff",
}


def build_profile(manifest, dtype, label, cols, rows):
    plugin_uuid = manifest["UUID"]
    action = manifest["Actions"][0]
    name = PROFILE_NAME.format(label=label)
    # Deterministic ids: regenerating must yield byte-identical profiles, or the
    # app could treat the same-named profile as new and ask to install it again.
    ns = uuid.uuid5(uuid.NAMESPACE_URL, f"{plugin_uuid}/{name}")
    profile_id = str(uuid.uuid5(ns, "profile")).upper()
    home_page = str(uuid.uuid5(ns, "home"))  # empty, as in the app's export
    show_page = str(uuid.uuid5(ns, "show"))  # every key = our action

    actions = {}
    for col in range(cols):
        for row in range(rows):
            actions[f"{col},{row}"] = {
                "ActionID": str(uuid.uuid5(ns, f"key/{col},{row}")),
                "LinkedTitle": True,
                "Name": action["Name"],
                "Plugin": {"Name": manifest["Name"], "UUID": plugin_uuid, "Version": manifest["Version"]},
                "Resources": None,
                "Settings": {},
                "State": 0,
                "States": [dict(KEY_STATE)],
                "UUID": action["UUID"],
            }

    files = {
        "package.json": {
            "AppVersion": "7.5.1.22901", "DeviceModel": "", "DeviceSettings": None,
            "FormatVersion": 1, "OSType": "macOS", "OSVersion": "",
            "RequiredPlugins": [plugin_uuid],
        },
        f"Profiles/{profile_id}.sdProfile/manifest.json": {
            "Device": {"Model": "", "UUID": ""},
            "Name": name,
            "Pages": {"Current": "00000000-0000-0000-0000-000000000000", "Default": home_page, "Pages": [show_page]},
            "Version": "3.0",
        },
        f"Profiles/{profile_id}.sdProfile/Profiles/{home_page.upper()}/manifest.json": {
            "Controllers": [{"Actions": None, "Type": "Keypad"}], "Icon": "", "Name": "",
        },
        f"Profiles/{profile_id}.sdProfile/Profiles/{show_page.upper()}/manifest.json": {
            "Controllers": [{"Actions": actions, "Type": "Keypad"}], "Icon": "", "Name": "",
        },
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path, data in files.items():
            info = zipfile.ZipInfo(path, date_time=(2026, 1, 1, 0, 0, 0))  # fixed: no timestamp churn
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, json.dumps(data, indent=2))
    return name, buf.getvalue()


def main():
    manifest = json.loads(MANIFEST.read_text())
    for old in PLUGIN_DIR.glob("*.streamDeckProfile"):
        old.unlink()
    profiles = []
    for dtype, (label, cols, rows) in GRIDS.items():
        name, data = build_profile(manifest, dtype, label, cols, rows)
        (PLUGIN_DIR / f"{name}.streamDeckProfile").write_bytes(data)
        profiles.append({"Name": name, "DeviceType": dtype, "Readonly": True, "DontAutoSwitchWhenInstalled": True})
        print(f"{name}: type {dtype}, {cols}x{rows}")
    manifest["Profiles"] = profiles
    MANIFEST.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    print(f"manifest Profiles[] updated ({len(profiles)} entries)")


if __name__ == "__main__":
    main()
