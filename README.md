# LaFlasher

One-click flasher for ESP32 firmware packages (`.laflash`). Open a package, plug in the board, click
**Flash**. The bootloader, partition table, app and LittleFS files all get written in one go.

Everything is built in: **esptool** runs inside the app, and LittleFS images are built by an
embedded copy of the littlefs library (the library `mklittlefs` is built on). You don't need Python,
Arduino or PlatformIO installed to use the exe.

## Features

- Works with every ESP32 model esptool supports (ESP32, S2, S3, C2, C3, C5, C6, C61, H2, P4, ...).
  The chip is detected automatically and the bootloader goes to the right offset for it
  (0x0 / 0x1000 / 0x2000).
- Picks the ESP32's serial port automatically, including when you plug the board in after
  starting the app.
- **LittleFS editor**: add, replace, rename, move, delete and extract files, create folders, edit
  text files (JSON, HTML, config...) directly, or drag files in and out of Explorer. A live bar
  shows how full the partition is, measured by building the real image.
- Tick or untick any image, change offsets, add or replace `.bin` files, or skip LittleFS to keep
  the board's existing files.
- Stops before flashing if the package was built for a different chip model, if images overlap,
  or if the files don't fit.
- Retries at a slower speed if the connection is unreliable, and gives plain-English hints when
  something goes wrong (board not in download mode, port in use, charge-only cable...).
- Clears the OTA data partition when a new app is written without one, so the board boots the
  new firmware instead of an older OTA slot (the same thing `idf.py flash` does).
- **Import from Build Folder** makes a package from an Arduino IDE export, a PlatformIO project or
  an ESP-IDF build (uses `flasher_args.json` for exact offsets). Picks up `data/` as the LittleFS
  files.
- **Standalone flashers**: export a package as one `.exe` that anyone can run. It opens a small
  window (connect the board, fill in the settings such as Wi-Fi details, click Flash), with an
  **Open Full Editor** button for everything else. See [below](#standalone-flashers).
- A success popup with confetti all over the screen when a board is flashed, showing the chip
  and its MAC address, with **Flash Another Board** for flashing a batch. The confetti can be
  turned off under *Tools > Confetti After Flashing*.
- Extras: export the LittleFS image, export a merged firmware `.bin` for web flashers, erase the
  flash, detect the board, and register `.laflash` files so they open with a double-click.

## The .laflash format

A `.laflash` file is a ZIP archive with a different extension:

```
my-project.laflash
├── laflash.json        optional metadata (see below)
├── bootloader.bin
├── partitions.bin      partition table (a partitions.csv also works)
├── boot_app0.bin       optional OTA data
├── firmware.bin        the application
└── littlefs/           files for the LittleFS partition (any folders)
    ├── index.html
    └── config.json
```

Without `laflash.json`, LaFlasher works out where each image goes from its name and the
partition table:

| File name contains              | Flashed to                                       |
|---------------------------------|--------------------------------------------------|
| `bootloader`                    | 0x0, 0x1000 or 0x2000, whichever the chip needs  |
| `partition`                     | 0x8000                                           |
| `boot_app0`, `ota_data`, `otadata` | the `otadata` partition                       |
| `merged`, `*.factory.bin`       | 0x0 (unticked if separate images exist)          |
| `littlefs`, `spiffs`            | unpacked into the LittleFS editor                |
| a partition label (`nvs.bin`)   | that partition                                   |
| anything else                   | the first app partition (factory or ota_0)       |
| a hex address (`0x10000_x.bin`) | exactly that address                             |

The `littlefs/` folder is packed into a LittleFS image (4 KB blocks, disk version 2.0, the same
defaults as `mklittlefs`) the size of the `spiffs`/`littlefs` partition, and written with the
firmware. Arduino's `LittleFS.begin()` and ESP-IDF's `esp_littlefs` mount it as is.

### laflash.json

Every field is optional. LaFlasher writes this file when you save a package.

```json
{
  "format": 1,
  "name": "Weather Station",
  "version": "1.2.0",
  "description": "Firmware for the balcony sensor",
  "chip": "esp32s3",
  "flash": { "mode": "keep", "freq": "keep", "size": "keep", "baud": 921600 },
  "partition_table": "partitions.bin",
  "images": [
    { "file": "bootloader.bin", "offset": "bootloader" },
    { "file": "partitions.bin", "offset": "0x8000" },
    { "file": "boot_app0.bin",  "offset": "0xe000" },
    { "file": "firmware.bin",   "offset": "0x10000" },
    { "file": "extra.bin",      "offset": "auto", "enabled": false }
  ],
  "littlefs": {
    "folder": "littlefs",
    "enabled": true,
    "partition": "spiffs",
    "block_size": 4096,
    "name_max": 255,
    "disk_version": "2.0"
  }
}
```

- `chip`: `auto` (any), or an esptool chip name such as `esp32`, `esp32s3`, `esp32c3`.
  Flashing stops if the connected board is a different model.
- `offset`: a hex address, `"bootloader"` (chip-dependent), or `"auto"` (from the partition table).
- `littlefs.partition`: the partition label to use. Set `"offset"` and `"size"` instead to
  write somewhere that isn't in the partition table.
- `standalone` (written by the export dialog): what an exported flasher shows, e.g.
  `{"title": "Balcony Sensor", "settings_file": "config.json", "allow_files": true,
  "allow_editor": true}`.

## Using it

1. **Open** a `.laflash` file (or drag it onto the window).
2. Plug in the board. Its port is selected automatically.
3. Click **Flash**.

If the board doesn't connect, hold **BOOT**, tap **RESET/EN**, release **BOOT**, and click Flash
again.

To make a package from your own project: **File > Import from Build Folder**, choose the sketch,
PlatformIO or ESP-IDF project folder, check the images, then **File > Save**.

Changes in the LittleFS tab are flashed right away. Use **File > Save** to store them in the
package.

## Standalone flashers

**File > Export Standalone Flasher (.exe)** (or **Export .exe** on the toolbar) saves the open
package as a single program, for example `Balcony Sensor Flasher.exe`. LaFlasher is built into it,
so whoever gets it doesn't need to install anything: they double-click it and see a small window:

1. **Connect your board**: the port is found automatically when the board is plugged in.
2. **Check the settings**: the package's settings file shown as a simple form. Passwords are
   hidden (with a Show button), numbers get number boxes and true/false values get checkboxes.
3. **Flash**: one big button, with progress, the same plain-English error hints as LaFlasher,
   and the confetti success popup. **Change Files...** adds, replaces or removes the files that
   go onto the board.

**Open Full Editor...** switches to the complete LaFlasher window with the same package; closing
it goes back to the small window. Changes are used for flashing until the program is closed;
the `.exe` itself never changes.

In the export dialog you choose:

- the **title** shown at the top,
- which file is shown as the **settings form**: any `.json` file (nested objects become
  groups), or a `KEY=value` file (`.env`, `.ini` with `[sections]`, `.cfg`, `.conf`,
  `.properties`). A file named like `config.json`, `settings.ini` or `wifi.env` is picked
  automatically. Saving changes only the values; comments, order and formatting stay as they
  were.
- whether people may **change the files** and **open the full editor**.

**Preview...** opens the window people will see before you export. The choices are saved in
the package's `laflash.json` for next time.

From the command line (for build scripts), using the options saved in the package:

```bash
LaFlasher.exe --export-standalone "Balcony Sensor.laflash" --output "Balcony Sensor Flasher.exe"
```

The exported `.exe` is about 33 MB plus the package, and takes a second or two to start
because it unpacks itself first. It isn't code-signed, so Windows SmartScreen may show "Windows protected
your PC" when it was downloaded: click **More info > Run anyway**.

To try the small window while developing: `python LaFlasher.py --standalone my.laflash`.

## Running from source

Requires Python 3.10+.

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python LaFlasher.py
```

## Building the Windows exe and installer

```bash
python build.py
```

The script creates `.venv`, installs the requirements and PyInstaller, builds the app from
`LaFlasher.spec` twice (a single-file and a folder version), launches each exe once to check
that it starts (and exports and runs a test standalone flasher), and writes:

- `dist/LaFlasher-<version>-windows-x64.zip`: unzip anywhere and run `LaFlasher.exe`.
- `dist/LaFlasher-<version>-windows-x64-onefile.zip`: the same app as one portable `.exe`.
- `dist/LaFlasher-<version>-setup.exe`: the installer, compiled from
  [installer/LaFlasher.iss](installer/LaFlasher.iss). Requires
  [Inno Setup 6](https://jrsoftware.org/isdl.php) (`winget install JRSoftware.InnoSetup`).
  Without it the zips are still built.

The single-file exe is also the base of exported standalone flashers, so the folder version and
the installer carry a copy of it (`LaFlasher-standalone-runtime.exe`).

The installer installs for the current user without asking for admin rights (or for all users
if chosen), adds a Start menu shortcut, can add a desktop icon, and can make `.laflash` files
open with LaFlasher. Upgrades keep the same install and uninstalling removes everything,
including LaFlasher's settings.

Options:

- `python build.py --onefile`: only the single-file `LaFlasher.exe` and its zip (quicker). No
  installer.
- `python build.py --no-installer`: skip the installer.
- `python build.py --no-venv`: build with the current Python environment.
- `python build.py --skip-test`: don't launch the exe after building.

## Credits

- **lab.krts**: [krts.dev/laflasher](https://krts.dev/laflasher)
- **DrBust**: [drbust.dev/projects/#laflasher](https://drbust.dev/projects/#laflasher)
- **Claude**: [claude.ai](https://claude.ai)

Built with [esptool](https://github.com/espressif/esptool) (GPL-2.0-or-later),
[littlefs-python](https://github.com/jrast/littlefs-python) (BSD-3-Clause),
[PyQt6](https://www.riverbankcomputing.com/software/pyqt/) / Qt (GPL-3.0) and
[pySerial](https://github.com/pyserial/pyserial) (BSD-3-Clause).
