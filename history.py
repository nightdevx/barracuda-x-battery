"""Battery history: an SQLite log of readings, split into charging and on-battery sessions."""
import datetime as dt
import os
import sqlite3
import sys
import threading
import time
from typing import NamedTuple

GAP_SECONDS = 180        # longer without a reading = headset off or app closed: a new session starts
MIN_RATE_SECONDS = 1800  # less data gives a noisy rate, since percent steps are whole numbers
MIN_DISCHARGE_USE = 600  # a finished stretch on battery used less than this was a quick unplug, not worth a row


def default_path():
    # Not next to the exe: that folder may sync to OneDrive, and the file is written every minute.
    if os.environ.get('BARRACUDA_DB'):  # tests point the app at a copy
        return os.environ['BARRACUDA_DB']
    folder = os.path.join(os.environ.get('LOCALAPPDATA') or os.path.expanduser('~'), 'BarracudaBattery')
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, 'history.db')


class Session(NamedTuple):
    charging: bool
    points: list  # [(ts, percent), ...]

    @property
    def start(self):
        return self.points[0][0]

    @property
    def end(self):
        return self.points[-1][0]

    @property
    def duration(self):
        return self.end - self.start

    def active(self):
        """(seconds, percent gained or lost). A charge stops counting at 100%: time spent full on the
        cable would otherwise drag the charge rate down."""
        pts = self.points
        if self.charging:
            full = next((i for i, (_, p) in enumerate(pts) if p >= 100), None)
            if full is not None:
                pts = pts[:full + 1]
        change = pts[-1][1] - pts[0][1] if self.charging else pts[0][1] - pts[-1][1]
        return pts[-1][0] - pts[0][0], max(0, change)


class Discharge(NamedTuple):
    """One stretch on battery: from unplugging the charger to plugging it in again."""
    start: int            # first reading off the cable
    start_percent: int
    end: int              # last reading before the next charge, or the latest one
    end_percent: int
    used: int             # seconds the headset was on in between
    recharged: int | None  # first reading on the cable again; None = still on battery

    @property
    def drop(self):
        return max(0, self.start_percent - self.end_percent)

    @property
    def rate(self):
        """Percent per hour of use, or None with too little data."""
        return self.drop * 3600 / self.used if self.used >= 600 and self.drop else None


class History:
    """The log, shared by the tray process (writes) and the window process (reads, from several threads):
    WAL lets one process read while the other writes, and the lock serializes this process's threads."""

    def __init__(self, path=None):
        self.db = sqlite3.connect(path or default_path(), timeout=10, check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock, self.db:
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('CREATE TABLE IF NOT EXISTS readings ('
                            'ts INTEGER PRIMARY KEY, percent INTEGER NOT NULL, charging INTEGER NOT NULL)')
            # The tray process's latest attempt: 'ok', 'no_answer' (headset off, away or on Bluetooth),
            # 'no_dongle' or 'error' with its message. One row.
            self.db.execute('CREATE TABLE IF NOT EXISTS status ('
                            'id INTEGER PRIMARY KEY CHECK (id = 1), ts INTEGER, state TEXT, message TEXT)')

    def _all(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def _write(self, sql, args):
        with self.lock, self.db:
            self.db.execute(sql, args)

    def add(self, percent, charging, ts=None):
        self._write('INSERT OR REPLACE INTO readings VALUES (?, ?, ?)',
                    (int(time.time() if ts is None else ts), percent, int(charging)))

    def readings(self, since=0, until=None):
        return self._all('SELECT ts, percent, charging FROM readings WHERE ts >= ? AND ts < ? ORDER BY ts',
                         (int(since), int(time.time() + 1 if until is None else until)))

    def latest(self):
        """(ts, percent, charging) of the newest reading, or None."""
        rows = self._all('SELECT ts, percent, charging FROM readings ORDER BY ts DESC LIMIT 1')
        return rows[0] if rows else None

    def first_reading(self):
        return self._all('SELECT MIN(ts) FROM readings')[0][0]

    def last_charge(self):
        return self._all('SELECT MAX(ts) FROM readings WHERE charging = 1')[0][0]

    def charge_cycles(self):
        """Equivalent full charge cycles over all history: every percent gained while charging, / 100."""
        gained = self._all(
            'SELECT SUM(d) FROM (SELECT charging, percent - LAG(percent) OVER w AS d, ts - LAG(ts) OVER w AS gap '
            'FROM readings WINDOW w AS (ORDER BY ts)) WHERE charging = 1 AND d > 0 AND gap <= ?',
            (GAP_SECONDS,))[0][0]
        return (gained or 0) / 100

    def set_status(self, state, message=''):
        self._write('INSERT OR REPLACE INTO status VALUES (1, ?, ?, ?)', (int(time.time()), state, message))

    def status(self):
        """(ts, state, message) of the tray process's latest attempt, or None."""
        rows = self._all('SELECT ts, state, message FROM status WHERE id = 1')
        return rows[0] if rows else None


def sessions(rows):
    """Split (ts, percent, charging) rows into sessions at every charging change and every gap."""
    out = []
    for ts, pct, charging in rows:
        if not out or out[-1].charging != bool(charging) or ts - out[-1].end > GAP_SECONDS:
            out.append(Session(bool(charging), []))
        out[-1].points.append((ts, pct))
    return out


def rate(sessions_, charging):
    """Average percent per hour over the sessions of one kind, or None without enough data."""
    seconds = change = 0
    for s in sessions_:
        if s.charging == charging:
            sec, ch = s.active()
            seconds += sec
            change += ch
    return change * 3600 / seconds if seconds >= MIN_RATE_SECONDS and change >= 2 else None


def hours_left(recent, percent, charging):
    """Hours until empty (on battery) or full (charging). Uses the current session when it has enough
    data, else all recent sessions; None without enough data."""
    current = [s for s in recent[-1:] if s.charging == charging]
    r = rate(current, charging) or rate(recent, charging)
    return None if r is None else (100 - percent if charging else percent) / r


def daily_usage(sessions_, days, now=None):
    """[(date, seconds on battery, seconds charging)] for the last `days` local days, oldest first. Any reading
    means the headset was on, so both count as use. Sessions are split at local midnight."""
    today = dt.date.fromtimestamp(time.time() if now is None else now)
    out = {today - dt.timedelta(days=d): [0, 0] for d in range(days - 1, -1, -1)}
    for s in sessions_:
        start = s.start
        while start < s.end:
            day = dt.date.fromtimestamp(start)
            chunk_end = min(s.end, dt.datetime.combine(day + dt.timedelta(days=1), dt.time()).timestamp())
            if day in out:
                out[day][s.charging] += chunk_end - start
            start = chunk_end
    return [(day, on_battery, charging) for day, (on_battery, charging) in out.items()]


def weekly_life(sessions_, weeks, now=None):
    """[(Monday, hours a full charge lasts or None)] for the last `weeks` weeks, oldest first.
    A session counts for the week it started in."""
    today = dt.date.fromtimestamp(time.time() if now is None else now)
    monday = today - dt.timedelta(days=today.weekday())
    buckets = {monday - dt.timedelta(weeks=w): [] for w in range(weeks - 1, -1, -1)}
    for s in sessions_:
        day = dt.date.fromtimestamp(s.start)
        week = day - dt.timedelta(days=day.weekday())
        if not s.charging and week in buckets:
            buckets[week].append(s)
    return [(week, 100 / r if (r := rate(ss, False)) else None) for week, ss in buckets.items()]


def discharges(rows):
    """Stretches on battery between two charges, from (ts, percent, charging) rows, oldest first. Each needs the
    charge before it, so a stretch the rows begin in the middle of is left out, and so is a finished one used under
    MIN_DISCHARGE_USE. Time the headset was off counts as no use: the next reading on battery continues it."""
    out, current, prev = [], None, None  # current: [start, start_percent, end, end_percent, used]
    for ts, pct, charging in rows:
        if charging:
            if current and current[4] >= MIN_DISCHARGE_USE:
                out.append(Discharge(*current, recharged=ts))
            current = None
        elif current:
            if ts - current[2] <= GAP_SECONDS:
                current[4] += ts - current[2]
            current[2:4] = ts, pct
        elif prev and prev[2]:  # first reading off the cable
            current = [ts, pct, ts, pct, 0]
        prev = (ts, pct, charging)
    if current:
        out.append(Discharge(*current, recharged=None))
    return out


def hourly_usage(sessions_, start, end):
    """Seconds of use per local hour of the day (24 values) between the timestamps start and end."""
    out = [0] * 24
    for s in sessions_:
        t, stop = max(s.start, start), min(s.end, end)
        while t < stop:
            hour = dt.datetime.fromtimestamp(t).replace(minute=0, second=0, microsecond=0)
            chunk_end = min(stop, (hour + dt.timedelta(hours=1)).timestamp())
            out[hour.hour] += chunk_end - t
            t = chunk_end
    return out


def on_periods(sessions_):
    """[start, end] spans the headset stayed on: sessions merged across charging changes, split at gaps."""
    out = []
    for s in sessions_:
        if out and s.start - out[-1][1] <= GAP_SECONDS:
            out[-1][1] = max(out[-1][1], s.end)
        else:
            out.append([s.start, s.end])
    return out


def week_summary(sessions_, monday):
    """Numbers for the week starting on the date `monday`. Time-based ones are clipped to the week; the drain
    rate and the charges count the sessions that started in it, as weekly_life does."""
    start = dt.datetime.combine(monday, dt.time()).timestamp()
    end = dt.datetime.combine(monday + dt.timedelta(days=7), dt.time()).timestamp()
    started = [s for s in sessions_ if start <= s.start < end]
    days = daily_usage(sessions_, 7, now=end - 1)
    drain = rate(started, False)
    charges = [gain for s in started if s.charging and (gain := s.active()[1]) > 0]
    return {
        'days': days,  # Monday first
        'hours': hourly_usage(sessions_, start, end),
        'total': sum(a + b for _, a, b in days),
        'used_days': sum(1 for _, a, b in days if a + b),
        'longest': max((min(e, end) - max(s, start) for s, e in on_periods(sessions_) if e > start and s < end),
                       default=0),
        'charges': len(charges),
        'charged': sum(charges),
        'drain': drain,
        'life': 100 / drain if drain else None,
        'lowest': min((p for s in sessions_ for t, p in s.points if start <= t < end), default=None),
    }


def _selftest():
    h = History(':memory:')
    t = 1_000_000
    for i in range(301):  # 5 h on battery: 80% -> 70%
        h.add(80 - i // 30, False, t + i * 60)
    t += 300 * 60 + 600   # 10 min without readings (headset off)
    for i in range(180):  # 3 h on the cable: 70% -> 100% in 2 h, then full for 1 h
        h.add(min(100, 70 + i // 4), True, t + i * 60)
    t += 180 * 60
    h.add(100, False, t)  # unplugged
    ss = sessions(h.readings())
    assert [s.charging for s in ss] == [False, True, False]
    assert rate(ss[:1], False) == 2.0
    assert rate(ss[1:2], True) == 15.0  # the full hour is ignored
    assert hours_left(ss, 50, False) == 25.0  # current session too short: falls back to all sessions
    assert hours_left(ss, 85, True) == 1.0
    assert h.last_charge() == t - 60
    assert h.charge_cycles() == 0.3
    assert h.latest() == (t, 100, 0) and h.status() is None
    h.set_status('no_answer')
    assert h.status()[1:] == ('no_answer', '')

    # Two hours on battery across local midnight count one hour for each day.
    midnight = dt.datetime.combine(dt.date(2026, 10, 14), dt.time()).timestamp()
    night = sessions([(int(midnight) - 3600 + i * 60, 50, 0) for i in range(121)])
    assert daily_usage(night, 2, now=midnight) == [(dt.date(2026, 10, 13), 3600, 0), (dt.date(2026, 10, 14), 3600, 0)]
    week = weekly_life(ss, 2, now=ss[0].start)
    assert week[0][1] is None and week[1][1] == 50.0  # 2 %/h -> 50 h per full charge

    # A week: Monday 10:00-12:30 on battery 60% -> 55%, plugged in straight after until 13:00 (+10%),
    # Wednesday 22:00-23:00 on battery down to 40%.
    monday = dt.date(2026, 10, 12)
    at = lambda day, h, m=0: int(dt.datetime.combine(monday + dt.timedelta(days=day), dt.time(h, m)).timestamp())
    rows = [(at(0, 10) + i * 60, 60 - i // 30, 0) for i in range(151)]
    rows += [(at(0, 12, 31) + i * 60, 56 + i // 3, 1) for i in range(30)]
    rows += [(at(2, 22) + i * 60, 45 - i // 12, 0) for i in range(61)]
    w = week_summary(sessions(rows), monday)
    assert w['hours'][10] == w['hours'][11] == 3600 and w['hours'][12] == 1800 + 1740 and w['hours'][22] == 3600
    assert w['longest'] == at(0, 13) - at(0, 10) and w['used_days'] == 2
    assert w['charges'] == 1 and w['charged'] == 9 and w['lowest'] == 40
    assert [d.weekday() for d, _, _ in w['days']] == list(range(7))

    # On battery before any charge (left out), charge, 3 h 80% -> 74%, off for 2 h, 1 h 74% -> 72%,
    # charge again, then 30 min still on battery.
    t = 2_000_000
    rows = [(t + i * 60, 90, 0) for i in range(10)]
    t += 600
    rows += [(t + i * 60, 79 + i // 10, 1) for i in range(11)]
    t += 660
    rows += [(t + i * 60, 80 - i // 30, 0) for i in range(181)]
    t += 180 * 60 + 2 * 3600
    rows += [(t + i * 60, 74 - i // 30, 0) for i in range(61)]
    t += 61 * 60
    rows += [(t, 72, 1), (t + 60, 73, 1)]
    t += 120
    rows += [(t, 73, 0), (t + 60, 73, 0), (t + 120, 74, 1)]  # unplugged for a minute: left out
    t += 180
    rows += [(t + i * 60, 74, 0) for i in range(31)]
    first, second = discharges(rows)
    assert (first.start_percent, first.end_percent, first.drop) == (80, 72, 8)
    assert first.used == 4 * 3600 and first.recharged == t - 300 and first.rate == 2.0
    assert second.recharged is None and second.used == 1800 and second.rate is None


if __name__ == '__main__':
    if '--selftest' in sys.argv:
        _selftest()
        print('ok')
