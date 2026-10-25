"""Razer Barracuda X (2022) over its 2.4 GHz receiver: battery and settings.

The receiver comes in two versions with different bridge chips. Both carry the same Airoha commands to the headset
(50 41 ... out, 50 49 ... back) and differ only in the HID framing around them; see Dongle.
Every conversation switches the receiver to "remote mode" (E1 01) so it forwards commands to the headset, and back
(E1 00) afterwards; the toggle itself changes no headset setting.
YS-Tech framing from github.com/mehdibouchami/razer-barracuda-x-battery (MIT), src/Protocol.cs; Macronix framing from
github.com/KhromotozzDevOut/razer-barracuda-battery-tray (MIT) and github.com/leag/razer-barracuda docs/PROTOCOL.md.
Customer-data commands (family 08) from leag's PROTOCOL.md and ANDROID_ANALYSIS.md; all GETs below were checked on a
1532:0550 receiver.
"""
import ctypes
import platform
import sys
import time
from contextlib import contextmanager

import hid

VID = 0x1532
DONGLES = {0x0550: 'Barracuda X (2022), YS-Tech alıcı', 0x0552: 'Barracuda X (2022), Macronix alıcı'}
# Other Barracuda X receivers, with other protocols; recognized only to say they aren't supported.
OTHER_DONGLES = {0x0536: 'Barracuda X (2021)', 0x0574: 'Barracuda X Chroma'}
YS_TECH, MACRONIX = 0x02, 0x01  # report id of each bridge's vendor collection
REPORT_LEN = 64

# Customer-data GET ids; the SET id is GET | 0x80. Replies: [cmd, 01, len, values...].
BATTERY = 0x21      # percent
CABLE = 0x2A        # 00 unplugged / 01 plugged
VERSION = 0x02      # six 4-byte component versions
EQ_PRESET = 0x13    # EQ_PRESETS keys
GAMING = 0x14       # 00 off / 01 on
CUSTOM_EQ = 0x15    # ten bands, 0..10, 5 = flat
DND = 0x27          # 00 off / 01 on
STANDBY = 0x2C      # minutes idle before power-off, STANDBY_MINUTES

EQ_PRESETS = {0x00: 'Varsayılan', 0x07: 'Oyun', 0x08: 'Müzik', 0x09: 'Film', 0xFF: 'Özel'}
EQ_BANDS = ('31', '63', '125', '250', '500', '1k', '2k', '4k', '8k', '16k')
STANDBY_MINUTES = (0, 5, 15, 30, 45, 60)  # 0 = never
# Allowed values per setting, checked before anything is sent to the headset.
VALID = {EQ_PRESET: set(EQ_PRESETS), GAMING: {0, 1}, DND: {0, 1}, STANDBY: set(STANDBY_MINUTES)}

_seq = 0
_trace_log = None  # a list while diagnose() records the raw traffic


class _DongleLock:
    """One conversation with the dongle at a time, across threads and processes: the tray process polls it while
    the window process reads and writes settings. A named Windows mutex; one left abandoned by a holder that died
    is still handed over."""

    def __init__(self, name):
        k32 = self.k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.CreateMutexW.restype = ctypes.c_void_p
        k32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p)
        k32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        k32.ReleaseMutex.argtypes = (ctypes.c_void_p,)
        self.handle = k32.CreateMutexW(None, False, name)

    def __enter__(self):
        if self.k32.WaitForSingleObject(self.handle, 15_000) not in (0, 0x80):  # WAIT_OBJECT_0, WAIT_ABANDONED
            raise OSError('dongle busy')

    def __exit__(self, *exc):
        self.k32.ReleaseMutex(self.handle)


_lock = _DongleLock(r'Local\BarracudaBattery.Dongle')


def _trace(direction, data):
    if _trace_log is not None:
        _trace_log.append(f'{time.monotonic():10.3f} {direction} {bytes(data).hex(" ")}')


def crc16_xmodem(data):
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else crc << 1
        crc &= 0xFFFF
    return crc


def chunk(report, report_id):
    """The stream bytes an input report carries: YS-Tech [02 len bytes...], Macronix [01 80 len bytes...].
    b'' for anything else, such as the media-key reports."""
    if report_id == YS_TECH and len(report) >= 2 and report[0] == YS_TECH:
        return bytes(report[2:2 + report[1]])
    if report_id == MACRONIX and len(report) >= 3 and report[0] == MACRONIX and report[1] == 0x80:
        return bytes(report[3:3 + report[2]])
    return b''


class Frames:
    """Reassembles 50 49 frames from report chunks: 10-byte header (bytes 8-9 = payload length LE) plus payload.
    One report can hold several frames, and the Macronix bridge can split a frame across reports."""

    def __init__(self):
        self.buf = b''

    def feed(self, data):
        self.buf += data
        out = []
        while True:
            start = self.buf.find(b'\x50\x49')
            if start < 0:
                self.buf = self.buf[-1:] if self.buf.endswith(b'\x50') else b''  # keep a header cut in half
                return out
            self.buf = self.buf[start:]
            if len(self.buf) < 10:
                return out
            end = 10 + (self.buf[8] | self.buf[9] << 8)
            if len(self.buf) < end:
                return out
            out.append(self.buf[:end])
            self.buf = self.buf[end:]


def event(frame):
    """(cmd, value) of an unsolicited headset report (family 08, op 02), else None."""
    return (frame[10], frame[13]) if frame[2] == 0x08 and len(frame) >= 14 and frame[11] == 0x02 else None


def find_dongle():
    """hid info of the first supported receiver's vendor collection, or None."""
    return next((d for d in hid.enumerate(VID) if d['product_id'] in DONGLES and d['usage_page'] == 0xFF00), None)


class Dongle:
    """An open receiver.
    YS-Tech bridge (1532:0550, report id 02): [02 80 len crcLo crcHi frame...] padded to 64 bytes, CRC-16/XMODEM over
    the whole report with the CRC bytes zero; the bridge acknowledges through GetInputReport ('O' taken, 'F' busy).
    Replies [02 len stream...].
    Macronix bridge (1532:0552, report id 01): an "app mode" handshake [01 40] -> [01 40 01 01], then
    [01 80 len frame...] padded to 64 bytes both ways."""

    def __init__(self, info):
        self.pid = info['product_id']
        self.report_id = MACRONIX if self.pid == 0x0552 else YS_TECH
        self.frames = Frames()
        self.hid = hid.device()
        self.hid.open_path(info['path'])

    def close(self):
        self.hid.close()

    def write(self, data):
        _trace('>', data)
        self.hid.write(bytes(data).ljust(REPORT_LEN, b'\0'))

    def read(self, timeout_ms):
        r = self.hid.read(REPORT_LEN, max(1, int(timeout_ms)))
        if r:
            _trace('<', r)
        return r

    def read_frames(self, timeout_ms):
        return self.frames.feed(chunk(self.read(timeout_ms), self.report_id))

    def start(self):
        """Macronix: the handshake its Windows tool sends first. leag's Linux driver works without it, so a missing
        reply isn't fatal."""
        if self.report_id != MACRONIX:
            return
        self.write([MACRONIX, 0x40])
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            r = self.read((deadline - time.monotonic()) * 1000)
            if len(r) >= 4 and r[0] == MACRONIX and r[1] == 0x40:
                return

    def send_command(self, payload):
        """Write one Airoha command; False when the bridge refuses it."""
        if self.report_id == MACRONIX:
            self.write([MACRONIX, 0x80, len(payload), *payload])
            return True
        frame = bytearray(REPORT_LEN)
        frame[0:3] = (YS_TECH, 0x80, len(payload))
        frame[5:5 + len(payload)] = payload
        crc = crc16_xmodem(frame)  # computed with bytes 3-4 zero
        frame[3], frame[4] = crc & 0xFF, crc >> 8
        for attempt in range(5):
            if attempt:
                time.sleep(0.05)
            self.write(frame)
            status = self.hid.get_input_report(YS_TECH, REPORT_LEN)
            _trace('?', status[:2])
            if len(status) < 2 or status[1] not in b'OF':  # anything but 'O' taken / 'F' busy = rejected
                return False
            if status[1] == ord('O'):
                return True
        return False


def send(dev, group, body, matches, timeout=0.8):
    """Send 50 41 group seq body... and return the first reply frame (50 49 ...) for which matches(frame, seq)."""
    global _seq
    _seq = _seq % 127 + 1
    seq = _seq
    if not dev.send_command(bytes([0x50, 0x41, group, seq, *body])):
        return None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for f in dev.read_frames((deadline - time.monotonic()) * 1000):
            if len(f) >= 12 and matches(f, seq):  # skips events and other replies
                return f
    return None


def remote_mode(dev, on):
    """Reply: 50 49 01 .. [10]=0e [11]=80|seq [12]=status."""
    return send(dev, 0x0E, [0x02, 0xE1, int(on)], lambda f, seq: f[10] == 0x0E and f[11] == 0x80 | seq)


@contextmanager
def connect():
    """Open the receiver with commands routed to the headset. Yields the Dongle, or None when the headset doesn't
    answer (off, out of range or in Bluetooth mode). Raises OSError when no supported receiver is plugged in."""
    with _lock:
        info = find_dongle()
        if info is None:
            other = next((d['product_id'] for d in hid.enumerate(VID) if d['product_id'] in OTHER_DONGLES), None)
            raise OSError(f'unsupported dongle {other:04x}' if other else 'dongle not found')
        dev = Dongle(info)
        try:
            dev.start()
            r = remote_mode(dev, True)
            if r is None or r[12] != 0:
                yield None
                return
            try:
                yield dev
            finally:
                remote_mode(dev, False)
        finally:
            dev.close()


def get(dev, cmd):
    """Customer-data GET: the value bytes, or None. Op 01 = reply; op 02 would be an unsolicited report."""
    f = send(dev, 0x08, [0x03, cmd, 0x00, 0x00],
             lambda f, seq: f[2] == 0x08 and f[10] == cmd and f[11] == 0x01, timeout=1.5)
    if f is None or len(f) < 13 or len(f) < 13 + f[12]:
        return None
    return bytes(f[13:13 + f[12]])


def put(dev, cmd, values):
    """Customer-data SET, then read back. True only when the headset reports the new values."""
    if cmd == CUSTOM_EQ:
        if len(values) != len(EQ_BANDS) or not all(0 <= v <= 10 for v in values):
            raise ValueError(f'bad custom EQ {values}')
    elif len(values) != 1 or values[0] not in VALID[cmd]:
        raise ValueError(f'bad value {values} for {cmd:#x}')
    set_cmd = cmd | 0x80
    send(dev, 0x08, [3 + len(values), set_cmd, 0x00, len(values), *values],
         lambda f, seq: f[2] == 0x08 and f[10] == set_cmd, timeout=1.5)
    return get(dev, cmd) == bytes(values)


def read_battery():
    """Return (percent, charging), or None when the headset doesn't answer. Raises OSError without the dongle."""
    with connect() as dev:
        if dev is None:
            return None
        pct, cable = get(dev, BATTERY), get(dev, CABLE)
    if not pct or pct[0] > 100:
        return None
    return pct[0], cable == b'\x01'


def read_settings():
    """Dict of the headset's current settings and firmware version, or None when it doesn't answer."""
    with connect() as dev:
        if dev is None:
            return None
        raw = {cmd: get(dev, cmd) for cmd in (VERSION, EQ_PRESET, GAMING, CUSTOM_EQ, DND, STANDBY)}
    if any(v is None for v in raw.values()):
        return None
    return {
        'version': '.'.join(str(b) for b in raw[VERSION][:4]),
        EQ_PRESET: raw[EQ_PRESET][0], GAMING: raw[GAMING][0], DND: raw[DND][0], STANDBY: raw[STANDBY][0],
        CUSTOM_EQ: list(raw[CUSTOM_EQ]),
    }


def write_settings(changes):
    """Apply [(cmd, values), ...] in one conversation, stopping at the first failure. True when every readback
    matches, False when the headset didn't take one, None when it doesn't answer."""
    with connect() as dev:
        return None if dev is None else all(put(dev, cmd, values) for cmd, values in changes)


def listen(on_event, stop):
    """Call on_event(cmd, value) for each unsolicited headset report until stop is set. Checked on a 1532:0550
    receiver: plugging or pulling the cable sends CABLE (bouncing for about a second) and then BATTERY, with no
    command sent. Only reads, on its own handle: Windows gives every open handle a copy of each input report, so
    this runs alongside the commands sent on other handles."""
    while not stop.is_set():
        info = find_dongle()
        if info is None:
            stop.wait(5)
            continue
        dev = None
        try:
            dev = Dongle(info)
            while not stop.is_set():
                for f in dev.read_frames(500):
                    if event(f):
                        on_event(*event(f))
        except OSError:
            stop.wait(5)  # receiver pulled out: wait for it
        finally:
            if dev:
                dev.close()


def diagnose():
    """Text report for troubleshooting on someone else's PC: the Razer HID collections Windows lists, then the raw
    traffic of one battery read and one settings read."""
    global _trace_log
    lines = [f'Barracuda X tanılama  {time.strftime("%Y-%m-%d %H:%M:%S")}  {platform.platform()}', '',
             'Razer HID arayüzleri:']
    for d in hid.enumerate(VID):
        lines.append(f"  {d['product_id']:04x}  if {d['interface_number']}  page {d['usage_page']:04x}  "
                     f"usage {d['usage']:04x}  {d['product_string']}  {d['path']!r}")
    _trace_log = []
    try:
        for name, call in (('read_battery', read_battery), ('read_settings', read_settings)):
            lines.append('')
            try:
                lines.append(f'{name}: {call()!r}')
            except Exception as e:
                lines.append(f'{name}: {type(e).__name__}: {e}')
            lines += _trace_log
            _trace_log.clear()
    finally:
        _trace_log = None
    return '\n'.join(lines) + '\n'


def _selftest():
    assert crc16_xmodem(b'123456789') == 0x31C3  # standard XMODEM check value
    # YS-Tech, captured from a 0550 receiver: unsolicited ack, then the battery reply (0x26 = 38%), in one report.
    ack = bytes.fromhex('50 49 01 c0 59 31 29 01 03 00 0e 84 00')
    reply = bytes.fromhex('50 49 08 fa b8 ce 17 01 04 00 21 01 01 26')
    frames = Frames()
    assert frames.feed(chunk(bytes([2, len(ack + reply)]) + ack + reply, YS_TECH)) == [ack, reply]
    assert [event(f) for f in (ack, reply)] == [None, None]  # an ack and a reply (op 01) are not events
    # Captured when the cable was plugged in: battery 53% and cable 01 in one report.
    plugged = bytes.fromhex('02 1c 50 49 08 fc 04 2d 67 00 04 00 21 02 01 35 50 49 08 fd 04 2d 67 00 04 00 2a 02 01 01')
    assert [event(f) for f in Frames().feed(chunk(plugged, YS_TECH))] == [(BATTERY, 0x35), (CABLE, 1)]
    assert chunk(plugged, MACRONIX) == b''  # framing kept apart
    # Macronix, from leag's PROTOCOL.md: a link report, here split across two reports, then a media key report.
    link = bytes.fromhex('50 49 08 f7 0d 32 69 00 04 00 20 02 01 00')
    frames = Frames()
    assert frames.feed(chunk(bytes([1, 0x80, 6]) + link[:6], MACRONIX)) == []
    assert frames.feed(chunk(bytes([1, 0x80, 8]) + link[6:], MACRONIX)) == [link]
    assert event(link) == (0x20, 0)
    assert frames.feed(chunk(bytes.fromhex('02 00 02 00 00'), MACRONIX)) == []
    for bad in ((STANDBY, [7]), (EQ_PRESET, [1]), (GAMING, [2]), (CUSTOM_EQ, [5] * 9), (CUSTOM_EQ, [11] * 10)):
        try:
            put(None, *bad)
        except ValueError:
            continue
        raise AssertionError(f'{bad} accepted')


if __name__ == '__main__':
    if '--selftest' in sys.argv:
        _selftest()
        print('ok')
    elif '--diagnose' in sys.argv:
        print(diagnose())
    else:
        res = read_battery()
        print('headset off or out of range' if res is None else f'{res[0]}%{" charging" if res[1] else ""}')
        print(read_settings())
