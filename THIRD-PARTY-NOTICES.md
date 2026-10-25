# Third-party notices

Barracuda X Battery's own source is MIT ([LICENSE](LICENSE)). A distributed build bundles the components below,
each under its own terms.

## Bundled in the installer

### CPython — PSF License Agreement

- Project: <https://www.python.org/>
- License: <https://docs.python.org/3/license.html>

### pywebview — BSD 3-Clause

- Project: <https://github.com/r0x0r/pywebview>
- License: <https://github.com/r0x0r/pywebview/blob/master/LICENSE>

pywebview ships the Microsoft WebView2 SDK assemblies (`Microsoft.Web.WebView2.Core.dll`,
`Microsoft.Web.WebView2.WinForms.dll`, `WebView2Loader.dll`) under Microsoft's WebView2 SDK license
(<https://www.nuget.org/packages/Microsoft.Web.WebView2/>). The WebView2 *runtime* itself is not bundled; the app
uses the one that comes with Windows 11.

### pythonnet and clr_loader — MIT

- Projects: <https://github.com/pythonnet/pythonnet>, <https://github.com/pythonnet/clr-loader>

### hidapi (cython-hidapi) — BSD, used under its BSD-style option

- Project: <https://github.com/trezor/cython-hidapi>
- The package and the HIDAPI C library it wraps are offered under a choice of BSD, GPL v3 or the original HIDAPI
  license.

### pystray — LGPL v3

- Project: <https://github.com/moses-palmer/pystray>
- License: GNU Lesser General Public License v3 (<https://www.gnu.org/licenses/lgpl-3.0.html>)

pystray is used unmodified. Its source is linked above, and this repository holds everything needed to rebuild the
app (`build.ps1`) against a modified pystray.

### Pillow — MIT-CMU (HPND)

- Project: <https://python-pillow.github.io/>
- License: <https://github.com/python-pillow/Pillow/blob/main/LICENSE>

## In the page (`ui/`)

### Chart.js 4.5.1 — MIT

- Project: <https://www.chartjs.org/>
- File: `ui/vendor/chart.umd.min.js`, with its license header intact.

### Lucide icons — ISC

- Project: <https://lucide.dev/>
- License: <https://lucide.dev/license>
- `ui/icons.js` holds the shape elements of the icons the page uses, generated from lucide-static 1.61.0.
  Portions of Lucide are derived from Feather (MIT, Cole Bemis).

## Protocol research

No code from these projects is included, but the receiver framing and the headset commands come from their
published work:

- [mehdibouchami/razer-barracuda-x-battery](https://github.com/mehdibouchami/razer-barracuda-x-battery) (MIT) —
  YS-Tech receiver framing, `1532:0550`
- [KhromotozzDevOut/razer-barracuda-battery-tray](https://github.com/KhromotozzDevOut/razer-barracuda-battery-tray)
  (MIT) — Macronix receiver framing, `1532:0552`
- [leag/razer-barracuda](https://github.com/leag/razer-barracuda) — protocol notes: battery, cable, EQ, gaming mode,
  do not disturb and standby commands, and the Razer Audio app analysis

## Trademarks

Razer and Barracuda are trademarks of Razer Inc. This project is not affiliated with, endorsed by or sponsored by
Razer.
