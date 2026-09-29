# companion-module-castika-deckshow

Bitfocus Companion module for **Castika DeckShow**: an idle-time, audio-reactive light show on the buttons you place in Companion.

The module is only the control layer. It needs the Castika DeckShow installation on the same computer (`Install-Mac.command` on macOS, `Install-Win.cmd` on Windows, from https://github.com/castika-code/DeckShow), which does the audio analysis and drawing and listens on 127.0.0.1:18790.

See [HELP.md](./companion/HELP.md) and [LICENSE](./LICENSE).

## Build

    yarn install
    yarn package

`src/key_icon.js` holds the button icon as base64 (the file name stays as it is: the Bitfocus repository builds from it) and is regenerated from the main project when the icon changes.
