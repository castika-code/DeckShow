## Castika DeckShow

Turns your deck into an audio-reactive visualizer. It listens to the room through the microphone and draws a frequency meter, or characters that move with the sound, on the buttons you choose. Pressing a button that carries its **Toggle DeckShow** action ends the show, and so does touching the keyboard or mouse while **Stop on use** is on.

This Companion module acts strictly as a control interface: it transmits button mappings and configuration parameters to the Castika DeckShow installation on the host computer (installed via `Install-Mac.command` on macOS or `Install-Win.cmd` on Windows). If the connection status indicates an error, ensure that installation is actively running.

### Setup

1. Install Castika DeckShow on the host computer (`Install-Mac.command` on macOS, `Install-Win.cmd` on Windows).
2. Add this module connection in Companion. Leave **Adapter host / port** as they are unless the installation runs on another computer on your network, or on another port.
3. Navigate to **Buttons** > **Presets** tab, and drag the **DeckShow Button** onto the desired target buttons. By default, mapped buttons display the DeckShow icon when inactive (this can be customized via the button's Style settings). Layout is flexible across any page and position; vertically stacked buttons automatically function as a unified, taller visualizer column.
4. **Idle Trigger**: The visualizer will automatically launch on the mapped buttons after the system remains free of keyboard/mouse input for the duration specified in **Start after**.

*Note: On initial launch, macOS will prompt for microphone access for **Castika.DeckShow**; on Windows, microphone access for desktop apps must be enabled (Settings > Privacy & security > Microphone). This must be granted to enable audio reactivity.*

### Start and Stop

- **Manual Toggle**: Press any configured DeckShow Button while inactive to force-start the visualizer. Press again to terminate.
- **Automatic Toggle**: Triggers automatically after the configured system idle timeout, and terminates instantly upon any mapped button press.
- **Stop on use**: With this on (the default), typing or moving the mouse ends the show, like a screen saver. Turn it off to let the show run until a button is pressed.
- **Global Disable**: Turning **Show On/Off** off in the connection settings completely prevents the visualizer from running.
- **Off for this session**: Hold a DeckShow Button for **1.5 seconds**. The show stops and does not start on idle any more; a short press brings it back. The preset does this out of the box, and the action **Temporary Off** can be placed on any button of your own.

### Settings

- **Style**:
  - **Text**: Displays audio-reactive characters across the mapped buttons.
  - **Stripe / Solid**: Renders a standard frequency spectrum meter (filling bottom-to-top).
  - **Random**: Cycles through the available styles.
- **Font** (Text only): The built-in font, or one you dropped in the installation's fonts folder (`~/Library/Application Support/DeckShow/fonts` on macOS, `%APPDATA%\DeckShow\fonts` on Windows). `.ttf`, `.otf`, `.ttc` and `.woff` are read. The folder's path is written in the Show style section, and a font added while this page is open appears when you reopen it.
- **Text** (Text only): Overrides the default visualizer text (rendered one character per button).
- **Peak hold** (Stripe only): Leaves a thin line at the highest point a meter reached; it slides down one button per second until the meter catches it again. It is lit as one rung of the ladder, which is why only Stripe has it.
- **Color** (Stripe / Solid): **Fixed** maps colors to static amplitude thresholds (Bottom / Middle / Top); **Gradient** applies a smooth color blend across the meter.
- **Bars per button** (Stripe / Solid): Defines the number of frequency columns rendered within a single button's display area.
- **Start after**: Idle minutes before the show starts by itself. A button press starts or stops it at any time.
- **Rotate**: Adjusts the rendering orientation for Stream Decks mounted vertically (portrait mode).
- **Sensitivity**: Adjusts the amplitude multiplier for the visualizer.
- **Gate**: Milliseconds a sound must persist before being rendered (0 = off), which filters out transient clicks or pops.
- **Analysis**:
  - **Relative**: Dynamically scales the amplitude based on recent peak and floor audio levels, ensuring the meter remains active even in quiet environments.
  - **Absolute**: Maps strictly to the true input amplitude.

*Changes are applied in real-time, even during an active show. The form only shows the settings the chosen style uses.*

### Building a button yourself

The preset is the easy way, but a button of your own works too: add the action **Toggle DeckShow** and the feedback **Show on this button**.

Put the action in **Press actions** if that is all you want. If you also add **Temporary Off** in a duration group (the preset uses 1500 ms for the long press), move the toggle to **Short release actions** instead: with a duration group present that is the short-press set, and leaving it in Press actions makes one press count twice.

Two more steps are needed for the picture, because Companion 5 paints a button from a stack of elements and a feedback only overrides an element that is there.

1. **Style** tab > **+** > **Image**. A new button has Text, Background and Canvas, but no image for the show to land in.
2. **Feedbacks** tab > the feedback's **Layered Styles Overrides** > **+** > Element **Image**, Property **Image**, and set its value to **png64**.

Until both are done Companion says "This feedback has no effect" and the button stays as you drew it. Buttons placed from the preset carry the image and the override already.

### Decks

Each Stream Deck surface recognized by Companion is configured individually under the **Decks** section:

- **Show off on this deck**: The deck is completely ignored by the visualizer. This is the Companion side of the Stream Deck panel's **This deck: On / Off**.
- **Show on its buttons**: Only the DeckShow Buttons on the page the deck is currently showing will participate. Ideal for integrating a small visualizer alongside standard action buttons.
- **Show on a specific page and back**: Forces the deck to jump to a dedicated visualizer page (populated with DeckShow Buttons via the Presets tab) when the show starts. When the show ends the deck goes back to the page it was on, whether it was a button press, the idle setting or the keyboard that ended it. *Requirement: This feature relies on Companion's TCP API (Enable via **Settings > Protocols > TCP Listener**). While it is off and a deck is set this way, the connection shows a warning and the deck stays where it is.*

*Note: Buttons on the same page are rendered concurrently as a unified canvas; buttons on different pages operate as independent meters.*

---

Castika DeckShow. Source, releases and issues: https://github.com/castika-code/DeckShow
