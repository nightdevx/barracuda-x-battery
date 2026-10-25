"""Barracuda X battery monitor.

One program, two processes. The tray process (the default, and --tray at Windows startup) reads the headset every
minute and whenever it reports a cable or battery change, logs the readings, keeps the tray icon up to date and warns
on low battery. It loads no UI toolkit, so it stays small. The window (--ui) is a separate process the tray starts on
demand: an HTML page (ui/) in WebView2 through pywebview, reading the log and the headset settings. Closing it ends
the process, which gives back WebView2's ~300 MB.
"""
import ctypes
import datetime as dt
import json
import os
import subprocess
import sys
import threading
import time
import winreg

import barracuda as hs
import history

POLL_SECONDS = 60
LOW_PERCENT = 20
RECENT_SECONDS = 14 * 86400   # history behind the time-left estimate
LONG_SECONDS = 12 * 7 * 86400  # history behind usage, health and charge cycles
USAGE_DAYS = 14
HEALTH_WEEKS = 12
RANGES = {'24h': 86400, '7d': 7 * 86400, '30d': 30 * 86400, 'all': None}
MAX_TABLE_ROWS = 200
RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
RUN_NAME = 'BarracudaBattery'
SHOW_EVENT = r'Local\BarracudaBattery.Show'        # set: the tray process opens the window
REFRESH_EVENT = r'Local\BarracudaBattery.Refresh'  # set: the tray process reads the headset now
WINDOW_EVENT = r'Local\BarracudaBattery.Window'    # set: the window process comes to the front
NO_ANSWER = 'Kulaklık kapalı, kapsama dışında veya Bluetooth modunda'
SETTINGS = {'eq_preset': hs.EQ_PRESET, 'custom_eq': hs.CUSTOM_EQ, 'dnd': hs.DND, 'standby': hs.STANDBY}
GREEN, BLUE, RED = '#44d62c', '#1fd8f0', '#ff3c3c'  # tray icon, as in the page

_k32 = ctypes.WinDLL('kernel32', use_last_error=True)
_k32.CreateEventW.restype = ctypes.c_void_p
_k32.CreateEventW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p)
_k32.SetEvent.argtypes = (ctypes.c_void_p,)
_k32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
_k32.WaitForMultipleObjects.argtypes = (ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p), ctypes.c_int,
                                        ctypes.c_uint32)


def named_event(name):
    """(handle, existed) of an auto-reset named event, shared by every process that opens the name."""
    handle = _k32.CreateEventW(None, False, False, name)
    return handle, ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS


def signal(name):
    _k32.SetEvent(named_event(name)[0])


def own_command(*args):
    """Command line that starts this program again."""
    if getattr(sys, 'frozen', False):  # PyInstaller exe
        return [sys.executable, *args]
    pythonw = os.path.join(os.path.dirname(sys.executable), 'pythonw.exe')
    return [pythonw, os.path.abspath(__file__), *args]


def resource(*parts):
    """A file shipped with the program: next to it, or unpacked into _MEIPASS by the PyInstaller exe."""
    return os.path.join(getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__))), *parts)


def startup_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_NAME)
        return True
    except OSError:
        return False


def set_startup(enabled):
    """At sign-in, start the tray process only; the window opens from the tray."""
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, ' '.join(f'"{a}"' for a in own_command('--tray')))
        else:
            try:
                winreg.DeleteValue(k, RUN_NAME)
            except FileNotFoundError:
                pass


def prefs_path():
    return os.path.join(os.path.dirname(history.default_path()), 'prefs.json')


def load_prefs():
    try:
        with open(prefs_path(), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def dongle_error(e):
    text = str(e)
    if text == 'dongle not found':
        return 'USB alıcı takılı değil'
    if text.startswith('unsupported dongle'):
        pid = int(text.split()[-1], 16)
        return f'Bu alıcı desteklenmiyor: {hs.OTHER_DONGLES[pid]} (USB 1532:{pid:04X})'
    return f'Hata: {type(e).__name__}: {e}' if not isinstance(e, OSError) else f'Hata: {e}'


# ---- tray process ----

def tray_image(text, color, charging=False):
    """Digits as large as fit, so they stay readable at the 16 px tray size. Charging: dark digits on a
    badge of the color, recognizable at a glance."""
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    width = 64
    if charging:
        draw.rounded_rectangle((0, 4, 63, 59), radius=12, fill=color)
        color, width = '#0b1a22', 56
    try:
        font = next(f for f in (ImageFont.truetype('arialbd.ttf', size) for size in range(64, 8, -2))
                    if draw.textlength(text, font=f) <= width)
    except OSError:
        font = ImageFont.load_default()
    draw.text((32, 32), text, fill=color, font=font, anchor='mm')
    return img


def promote_tray_icon():
    """Windows 11 hides new tray icons in the overflow menu; show ours on the taskbar.
    Only when IsPromoted is missing, so a later choice by the user in Settings is kept."""
    exe = os.path.normcase(sys.executable)
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Control Panel\NotifyIconSettings') as root:
            for i in range(winreg.QueryInfoKey(root)[0]):
                with winreg.OpenKey(root, winreg.EnumKey(root, i), 0,
                                    winreg.KEY_READ | winreg.KEY_SET_VALUE) as k:
                    values = dict(winreg.EnumValue(k, j)[:2] for j in range(winreg.QueryInfoKey(k)[1]))
                    if os.path.normcase(values.get('ExecutablePath', '')) == exe and 'IsPromoted' not in values:
                        winreg.SetValueEx(k, 'IsPromoted', 0, winreg.REG_DWORD, 1)
    except OSError:
        pass  # older Windows has no such key


class Tray:
    """Reads the headset, logs, and shows the level in the tray. The window process is started on demand."""

    def __init__(self, show_event):
        import pystray
        self.history = history.History()
        self.show_event = show_event
        self.refresh_event = named_event(REFRESH_EVENT)[0]
        self.stop = threading.Event()
        self.read_lock = threading.Lock()
        self.debounce = None
        self.window = None  # the window process
        self.warned = False
        self.icon = pystray.Icon('barracuda', tray_image('..', 'gray'), 'Barracuda X', menu=pystray.Menu(
            pystray.MenuItem('Göster', lambda: self.open_window(), default=True),  # default = left click
            pystray.MenuItem('Şimdi oku', lambda: threading.Thread(target=self.read, daemon=True).start()),
            pystray.MenuItem('Çıkış', lambda: self.quit()),
        ))

    def run(self, open_window):
        if getattr(sys, 'frozen', False) and startup_enabled():
            set_startup(True)  # keep the sign-in entry pointing at this exe and its current flags
        threading.Thread(target=self.poll, daemon=True).start()
        threading.Thread(target=hs.listen, args=(lambda cmd, value: self.on_headset_event(), self.stop),
                         daemon=True).start()
        threading.Thread(target=self.watch_events, daemon=True).start()
        if open_window:
            self.open_window()
        self.icon.run(setup=self.setup)

    def setup(self, icon):
        icon.visible = True
        threading.Timer(3, promote_tray_icon).start()  # Windows registers the icon's entry once it appears

    def poll(self):
        while not self.stop.is_set():
            self.read()
            self.stop.wait(POLL_SECONDS)

    def watch_events(self):
        """Another launch asks for the window; the window asks for a fresh reading."""
        handles = (ctypes.c_void_p * 2)(self.show_event, self.refresh_event)
        while not self.stop.is_set():
            hit = _k32.WaitForMultipleObjects(2, handles, False, 1000)
            if hit == 0:
                self.open_window()
            elif hit == 1:
                self.read()

    def on_headset_event(self):
        """Cable plugged or pulled, or a battery report: read now instead of at the next poll. Waits for the burst
        to settle, since the cable contact bounces for about a second."""
        if self.debounce:
            self.debounce.cancel()
        self.debounce = threading.Timer(1.5, self.read)
        self.debounce.start()

    def read(self):
        """One reading at a time. A request that comes in meanwhile waits and then reads again, since the one in
        flight may predate the change that prompted it."""
        with self.read_lock:
            try:
                res = hs.read_battery()
                state, message = ('ok' if res else 'no_answer'), ''
            except Exception as e:  # anything: this runs on the poll thread, which must survive to try again
                res = None
                state, message = ('no_dongle' if str(e) == 'dongle not found' else 'error'), dongle_error(e)
            if res:
                self.history.add(*res)
            self.history.set_status(state, message)
            self.show(res, message or NO_ANSWER)

    def show(self, res, problem):
        if res is None:
            self.icon.icon, self.icon.title = tray_image('--', 'gray'), f'Barracuda X: {problem}'
            return
        pct, charging = res
        color = BLUE if charging else RED if pct <= LOW_PERCENT else GREEN
        self.icon.icon = tray_image(str(pct), color, charging)
        self.icon.title = f'Barracuda X: %{pct}{" · şarj oluyor" if charging else ""}'
        if pct <= LOW_PERCENT and not charging and not self.warned:
            self.icon.notify(f'Pil seviyesi %{pct}', 'Barracuda X')
            self.warned = True
        elif pct > LOW_PERCENT or charging:
            self.warned = False

    def open_window(self):
        if self.window and self.window.poll() is None:
            signal(WINDOW_EVENT)  # already open: bring it to the front
        else:
            self.window = subprocess.Popen(own_command('--ui'))

    def quit(self):
        self.stop.set()
        if self.window and self.window.poll() is None:
            self.window.terminate()
        self.icon.stop()


# ---- window process ----

def compress(points):
    """Keep the ends of each run of equal percent: the same line in a fraction of the points."""
    return [pt for i, pt in enumerate(points)
            if i in (0, len(points) - 1) or pt[1] != points[i - 1][1] or pt[1] != points[i + 1][1]]


def series(sessions):
    return [{'charging': s.charging, 'points': compress(s.points)} for s in sessions]


def discharge_dict(d):
    return d and {'start': d.start, 'start_percent': d.start_percent, 'end': d.end, 'end_percent': d.end_percent,
                  'used': d.used, 'recharged': d.recharged, 'drop': d.drop, 'rate': d.rate}


def monday_of(day):
    return day - dt.timedelta(days=day.weekday())


def midnight(day):
    return dt.datetime.combine(day, dt.time()).timestamp()


class Api:
    """What the page can ask for. Methods run on pywebview's worker threads and return JSON-able values;
    underscore attributes stay hidden from the page."""

    def __init__(self):
        self._history = history.History()
        self._window = None

    def meta(self):
        return {'eq_presets': [[k, v] for k, v in hs.EQ_PRESETS.items()], 'eq_bands': list(hs.EQ_BANDS),
                'standby': list(hs.STANDBY_MINUTES), 'low_percent': LOW_PERCENT, 'gap': history.GAP_SECONDS}

    def state(self):
        """The newest reading and the tray process's latest attempt. The page asks every 2 s and reloads its data
        when rev changes."""
        h, now = self._history, time.time()
        latest, status = h.latest(), h.status()
        live = bool(status and status[1] == 'ok' and latest and now - latest[0] < 3 * POLL_SECONDS)
        eta = None
        if live:
            eta = history.hours_left(history.sessions(h.readings(now - RECENT_SECONDS)), latest[1], bool(latest[2]))
        return {'rev': [latest and latest[0], status and status[0]], 'now': now,
                'state': status[1] if status else 'unknown', 'message': status[2] if status else '',
                'checked': status and status[0], 'live': live, 'eta_hours': eta,
                'reading': latest and {'ts': latest[0], 'percent': latest[1], 'charging': bool(latest[2])}}

    def overview(self):
        h, now = self._history, time.time()
        rows = h.readings(now - LONG_SECONDS)
        week = history.sessions([r for r in rows if r[0] >= now - 7 * 86400])
        days = history.daily_usage(history.sessions(rows), 7)
        drain = history.rate(week, False)
        ds = history.discharges(rows)
        return {'t0': now - 86400, 't1': now,
                'series': series(history.sessions([r for r in rows if r[0] >= now - 86400])),
                'today': days[-1][1] + days[-1][2], 'week_usage': sum(a + b for _, a, b in days),
                'drain': drain, 'last_charge': h.last_charge(), 'cycles': h.charge_cycles(),
                'discharge': discharge_dict(ds[-1] if ds else None)}

    def history(self, key):
        h, now = self._history, time.time()
        seconds = RANGES[key]
        rows = h.readings(now - seconds if seconds else 0)
        ss = history.sessions(rows)
        table = []
        for s in reversed(ss[-MAX_TABLE_ROWS:]):
            secs, change = s.active()
            table.append({'charging': s.charging, 'start': s.start, 'end': s.end, 'duration': s.duration,
                          'from': s.points[0][1], 'to': s.points[-1][1],
                          'rate': change * 3600 / secs if secs >= 600 and change else None,
                          'ongoing': s is ss[-1] and now - s.end <= history.GAP_SECONDS})
        return {'t0': now - seconds if seconds else (rows[0][0] if rows else now - 86400), 't1': now,
                'series': series(ss), 'sessions': table, 'drain': history.rate(ss, False),
                'usage': sum(s.duration for s in ss if not s.charging), 'last_charge': h.last_charge()}

    def cycles(self):
        rows = self._history.readings(time.time() - LONG_SECONDS)
        return {'items': [discharge_dict(d) for d in history.discharges(rows)]}

    def usage(self):
        h = self._history
        ss = history.sessions(h.readings(time.time() - LONG_SECONDS))
        weekly = history.weekly_life(ss, HEALTH_WEEKS)
        lives = [life for _, life in weekly if life]
        return {'daily': [[d.isoformat(), a, b] for d, a, b in history.daily_usage(ss, USAGE_DAYS)],
                'weekly': [[d.isoformat(), life] for d, life in weekly], 'cycles': h.charge_cycles(),
                'health': lives[-1] / lives[0] * 100 if len(lives) >= 2 else None}

    def week(self, offset):
        """Week summary; offset 0 = this week, -1 = the one before..."""
        h = self._history
        this_monday = monday_of(dt.date.today())
        monday = this_monday + dt.timedelta(weeks=int(offset))
        prev = monday - dt.timedelta(weeks=1)
        ss = history.sessions(h.readings(midnight(prev), midnight(monday + dt.timedelta(weeks=1))))
        first = h.first_reading()
        first_monday = monday_of(dt.date.fromtimestamp(first)) if first else this_monday

        def plain(w):
            return {**w, 'days': [[d.isoformat(), a, b] for d, a, b in w['days']]}

        return {'monday': monday.isoformat(), 'offset': int(offset), 'can_prev': monday > first_monday,
                'can_next': monday < this_monday, 'week': plain(history.week_summary(ss, monday)),
                'prev': plain(history.week_summary(ss, prev))}

    def settings(self):
        try:
            s = hs.read_settings()
        except Exception as e:
            return {'error': dongle_error(e)}
        if s is None:
            return {'error': NO_ANSWER}
        info = hs.find_dongle()
        return {'version': s['version'], 'gaming': s[hs.GAMING], 'receiver': info and hs.DONGLES[info['product_id']],
                **{name: s[cmd] for name, cmd in SETTINGS.items()}}

    def diagnose(self):
        """Write the troubleshooting report next to the log, select it in Explorer and return its path."""
        try:
            text = hs.diagnose()
        except Exception as e:
            text = f'Tanılama yapılamadı: {type(e).__name__}: {e}\n'
        path = os.path.join(os.path.dirname(history.default_path()), 'tanilama.txt')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
        subprocess.Popen(['explorer', f'/select,{path}'])
        return path

    def apply(self, changes):
        """changes: [[setting name, [values]], ...], written in one conversation and read back."""
        try:
            ok = hs.write_settings([(SETTINGS[name], [int(v) for v in values]) for name, values in changes])
        except OSError as e:
            return {'ok': False, 'error': dongle_error(e)}
        except (KeyError, ValueError) as e:
            return {'ok': False, 'error': f'Geçersiz ayar: {e}'}
        return {'ok': bool(ok), 'error': None if ok else 'Kulaklık ayarı kabul etmedi' if ok is False else NO_ANSWER}

    def prefs(self):
        return {'startup': startup_enabled(), 'on_top': is_topmost(self._window)}

    def set_startup(self, on):
        set_startup(bool(on))
        return startup_enabled()

    def set_on_top(self, on):
        set_topmost(self._window, bool(on))
        with open(prefs_path(), 'w', encoding='utf-8') as f:
            json.dump({**load_prefs(), 'on_top': bool(on)}, f)
        return is_topmost(self._window)

    def refresh(self):
        signal(REFRESH_EVENT)


_u32 = ctypes.WinDLL('user32')
_u32.SetWindowPos.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                              ctypes.c_uint)
_u32.GetWindowLongW.argtypes = (ctypes.c_void_p, ctypes.c_int)


def hwnd(window):
    return ctypes.c_void_p(window.native.Handle.ToInt64())


def set_topmost(window, on):
    """Pin the window above the others, or unpin it, through SetWindowPos, which any thread may call. pywebview's
    on_top sets the WinForms property on the calling thread instead, and from a page request that froze the window
    (pywebview 6.2)."""
    _u32.SetWindowPos(hwnd(window), ctypes.c_void_p(-1 if on else -2), 0, 0, 0, 0,
                      0x0013)  # HWND_TOPMOST / HWND_NOTOPMOST; SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE


def is_topmost(window):
    return bool(_u32.GetWindowLongW(hwnd(window), -20) & 0x8)  # GWL_EXSTYLE has WS_EX_TOPMOST


def style_window(window):
    """Dark title bar in the page's background color (Windows 11), and the saved "always on top"."""
    for attribute, value in ((20, 1), (35, 0x00050505)):  # immersive dark mode; caption color #050505 as BGR
        v = ctypes.c_int(value)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd(window), attribute, ctypes.byref(v), ctypes.sizeof(v))
    if load_prefs().get('on_top'):
        set_topmost(window, True)


def run_window():
    import webview
    focus, existed = named_event(WINDOW_EVENT)
    if existed:  # already open
        _k32.SetEvent(focus)
        return
    api = Api()
    window = webview.create_window('Barracuda X', url=resource('ui', 'index.html'), js_api=api, width=1240,
                                   height=840, min_size=(1080, 760), background_color='#050505')
    api._window = window
    window.events.shown += lambda: style_window(window)

    def watch_focus():
        while True:
            _k32.WaitForSingleObject(focus, 0xFFFFFFFF)
            window.restore()
            window.show()
            pinned = is_topmost(window)
            set_topmost(window, True)  # raise it above the window that has the focus
            if not pinned:
                set_topmost(window, False)

    threading.Thread(target=watch_focus, daemon=True).start()
    webview.start()


if __name__ == '__main__':
    if '--ui' in sys.argv:
        run_window()
    else:
        show_event, existed = named_event(SHOW_EVENT)
        if existed:  # the tray process is running: have it open the window
            _k32.SetEvent(show_event)
        else:
            # --minimized: the sign-in entry of the earlier Tk version
            Tray(show_event).run(open_window=not {'--tray', '--minimized'} & set(sys.argv))
