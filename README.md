<div align="center">

<img src="assets/logo.png" width="96" alt="">

# Barracuda X Battery

**Battery level, history and settings for the Razer Barracuda X (2022) headset on Windows — over its 2.4 GHz USB receiver, where Windows shows nothing.**

[![Download](https://img.shields.io/github/v/release/nightdevx/barracuda-x-battery?label=download)](../../releases/latest)
[![Platform](https://img.shields.io/badge/platform-Windows%2010%20%2F%2011-0078D4)](#install)
[![Python](https://img.shields.io/badge/python-3.13%2B-3776AB)](#development)
[![UI](https://img.shields.io/badge/UI-WebView2-2C7BE5)](#how-it-works)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**English** · [Türkçe](README.tr.md)

<img src="docs/images/overview.png" width="900" alt="Overview page">

</div>

---

Over Bluetooth, Windows shows the Barracuda X's battery. Over the 2.4 GHz receiver it shows nothing, and Razer's
desktop software does not support this headset at all: the level, the EQ and the standby timer live only in the
phone app.

This app talks to the receiver directly over HID. It keeps the level in the notification area, logs a reading every
minute, turns the log into history, charge cycles and usage statistics, and changes the headset's settings from the
PC.

## Features

<table>
<tr>
<td width="55%" valign="top">

**Level in the notification area.** The percentage itself is the tray icon; while charging it sits on a coloured
badge. A low-battery notification at 20 %.

**Charging shows up at once.** The headset reports cable and battery changes on its own; the app listens for them
and reads within two seconds instead of waiting for the next poll.

**History.** Battery level over 24 hours, 7 days, 30 days or everything, charging and on-battery stretches in their
own colours, every session in a table.

**Charge cycles.** From unplugging to plugging in again: hours of use, how far the battery dropped, and what a full
charge lasts at that pace.

**Usage and weekly summary.** Hours per day, a weekly full-charge figure that shows the battery ageing, equivalent
full cycles, and a week-by-week page compared with the week before.

**Headset settings.** EQ presets, a 10-band custom EQ, do not disturb and auto power-off. Every change is read back
from the headset before it counts.

**Razer-style look.** Black, neon green, chamfered panels, condensed uppercase type and a segmented battery ring,
so it sits next to the rest of a Razer setup.

</td>
<td width="45%" valign="top">

<img src="docs/images/cycles.png" width="100%" alt="Charge cycles page">

</td>
</tr>
</table>

### Screens

| History | Usage | Weekly summary |
|---|---|---|
| <img src="docs/images/history.png" alt="History page"> | <img src="docs/images/usage.png" alt="Usage page"> | <img src="docs/images/week.png" alt="Weekly summary page"> |
| Level over time, sessions, drain rate and time on battery. | Daily hours, full-charge life per week, battery health. | One week against the one before. |

| Headset settings | |
|---|---|
| <img src="docs/images/device.png" alt="Device page"> | Firmware, receiver, EQ presets, a 10-band custom EQ, do not disturb and auto power-off. Each change is read back from the headset; one it does not take is put back on screen. |

## Install

Download `BarracudaBattery-Setup-<version>.exe` from [Releases](../../releases/latest) and run it.

- No administrator rights. Installs per user under `%LOCALAPPDATA%\Programs\BarracudaBattery`.
- Python is not required; the runtime is bundled.
- The window uses Microsoft Edge WebView2, which Windows 11 includes.
- The installer stops an older copy that is running, and can start the app with Windows (on by default).
- 17 MB installer, ~41 MB installed.

> **SmartScreen.** The installer is not code-signed, so Windows warns the first time: **More info → Run anyway**.

Uninstall from Settings → Apps. The log stays in `%LOCALAPPDATA%\BarracudaBattery`; delete that folder to remove it
too.

The interface is in Turkish.

## Supported hardware

| Receiver (USB id) | Headset | Status |
|---|---|---|
| `1532:0550` (YS-Tech bridge) | Barracuda X (2022) | Tested: battery, cable, events, every setting |
| `1532:0552` (Macronix bridge) | Barracuda X (2022) | Implemented from published protocol notes; not yet confirmed on hardware |
| `1532:0536`, `1532:0574` | Barracuda X (2021), Barracuda X Chroma | Not supported; the app names the receiver and says so |

The Barracuda X (2022) ships with either receiver, and both carry the same headset commands. Over Bluetooth the
receiver cannot reach the headset; Windows' own battery display covers that case.

If yours is a `0552` and something does not work, open **Cihaz → Tanılama raporu** and attach the
`tanilama.txt` it saves to an issue. It lists the HID interfaces Windows sees and the raw traffic of one read.

## How it works

The receiver bridges USB HID to the headset's Airoha chip. Each conversation switches the receiver to forward
commands (`E1 01`), sends Airoha customer-data commands (`50 41 08 …`) — battery percent `21`, cable `2A`, EQ preset
`13`, custom EQ `15`, do not disturb `27`, standby `2C` — and switches it back. Setting a value is followed by reading
it back. The two receiver versions differ only in the HID framing around those commands:

| | YS-Tech (`0550`) | Macronix (`0552`) |
|---|---|---|
| Report id | `02` | `01` |
| Out | `02 80 len crc crc` + command, CRC-16/XMODEM | `01 80 len` + command, after a `01 40` handshake |
| Accepted | `GetInputReport`: `O` taken, `F` busy | — |
| In | `02 len` + frames | `01 80 len` + frames, a frame may span reports |

The app runs as two processes from one program:

| Process | Does | Memory |
|---|---|---|
| Tray (`--tray` at sign-in) | Reads every minute and on headset events, logs, tray icon, notifications. No UI toolkit. | ~33 MB private |
| Window (`--ui`, started from the tray) | The HTML page in WebView2: reads the log, reads and writes settings. | ~73 MB + WebView2, only while open |

WebView2 holds around 300 MB even with its window hidden, so closing the window ends that process instead of hiding
it. A named mutex serialises the receiver between the two processes; the log is SQLite in WAL mode, written by the
tray and read by the window.

```
app.pyw            tray process, window process, the page's API
barracuda.py       receiver framing, headset commands, event listener, diagnostics
history.py         log, sessions, charge cycles, daily and weekly figures
ui/
  index.html       pages
  app.css          components and the Razer-style look
  app.js           data, charts (Chart.js), animations, settings
installer/         Inno Setup script
build.ps1          self-tests, PyInstaller (one folder), installer
```

User data, all in `%LOCALAPPDATA%\BarracudaBattery`:

- `history.db` — one row per reading (time, percent, charging)
- `prefs.json` — "always on top"
- `tanilama.txt` — the diagnostic report, when you make one

## Development

```powershell
py -m pip install -r requirements-dev.txt
py app.pyw              # tray, then the window
py app.pyw --ui         # the window only
py barracuda.py         # one reading and the settings, printed
py barracuda.py --diagnose
```

```powershell
py barracuda.py --selftest   # framing, frame reassembly, events, value checks
py history.py --selftest     # sessions, rates, charge cycles, daily/weekly splits
```

The self-tests need no headset. They replay frames captured from a `0550` receiver and the `0552` examples from
leag's protocol notes, one of them split across two reports.

### Build

```powershell
.\build.ps1
```

Runs the self-tests, builds the app with PyInstaller into `build\app`, then the installer with Inno Setup 6 into
`dist\BarracudaBattery-Setup-<version>.exe`. The version lives in `installer\BarracudaBattery.iss`.

PyInstaller runs in one-folder mode: a one-file build unpacks itself into `%TEMP%` on every start, and the window
process starts every time the window opens.

## Troubleshooting

**"USB alıcı takılı değil"** — no supported receiver is plugged in.

**"Bu alıcı desteklenmiyor: …"** — a Barracuda receiver of another model is plugged in; see the table above.

**"Kulaklık kapalı, kapsama dışında veya Bluetooth modunda"** — the receiver is there but the headset does not
answer: it is off, out of range, or switched to Bluetooth.

**Gaming mode cannot be changed.** The headset accepts the command over 2.4 GHz but does not apply it; the setting is
for Bluetooth. The app shows it read-only.

## Contributing

Issues and pull requests are welcome.

- A test on a `0552` receiver is the most useful thing right now; attach the diagnostic report.
- The UI strings are Turkish only, inline in `ui/index.html` and `ui/app.js`.
- Run both self-tests before opening a PR; `build.ps1` will not build on a failure.

## Credits

The receiver framing and the headset commands come from the published work of
[mehdibouchami/razer-barracuda-x-battery](https://github.com/mehdibouchami/razer-barracuda-x-battery),
[KhromotozzDevOut/razer-barracuda-battery-tray](https://github.com/KhromotozzDevOut/razer-barracuda-battery-tray) and
[leag/razer-barracuda](https://github.com/leag/razer-barracuda). No code from them is included.

## License

[MIT](LICENSE) for this project's own code. A distributed build bundles third-party components under their own
terms; see [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).

Razer and Barracuda are trademarks of Razer Inc. This project is not affiliated with, endorsed by or sponsored by
Razer.

---

<div align="center">

[nightdijital](https://nightdijital.com) · [iletisim@nightdijital.com](mailto:iletisim@nightdijital.com)

</div>
