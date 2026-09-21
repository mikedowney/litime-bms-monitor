#!/usr/bin/env python3
"""
bms_service.py — BMS collection and query service for Venus OS.

Polls the four LiTime packs over BLE on a schedule, stores every sample in a
local SQLite database, and serves query endpoints over HTTP for Node-RED.

Design notes:
  * stdlib only, plus python3-dbus and python3-gi which Venus already ships.
    No pip, no venv, nothing to install.
  * Registers NOTHING on the Victron dbus. No battery service, so DVCC cannot
    select it and chargers cannot be told a BMS exists.
  * Lives entirely under /data so Venus OS firmware updates don't wipe it.
  * Writes are batched and the database is pruned, to limit wear on the
    internal storage.

    python3 bms_service.py --config /data/bms/packs.json

Endpoints (bound to 127.0.0.1 by default):
    /health
    /bms/latest
    /bms/summary?hours=24      <- shape consumed by Build Comprehensive Prompt
    /bms/reliability?hours=24
    /bms/series?hours=6&field=measured_total_voltage
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from litime_bluez import (Session, bus, decode_status, adapter_path,  # noqa: E402
                          start_discovery, is_discovering)

def mean(values):
    """Arithmetic mean. Venus OS ships a stripped Python without `statistics`."""
    values = list(values)
    return sum(values) / len(values) if values else None

# Baselines measured during the 2026-09-11/12 characterisation run.
# Surfaced in the API so the AI prompt can judge readings against known-good.
BASELINE = {
    "current_deadband_a": 2.07,
    "resting_cell_delta_mv": [14, 21],
    "loaded_cell_delta_mv": [35, 50],
    "balance_threshold_v": 3.50,
    "current_sharing_spread_pct": 3,
    "pack_capacity_ah": 336.0,
    "pack_max_continuous_a": 200,
    "bank_capacity_ah": 1280,
    "note": "Zero current means below the 2.07A deadband, NOT disconnected.",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    ts REAL NOT NULL,
    mac TEXT NOT NULL,
    label TEXT,
    -- primary measurements
    voltage REAL, cells_sum REAL, v_drop REAL, current REAL, power REAL,
    temp_cell INTEGER, temp_mosfet INTEGER,
    -- cells
    cells TEXT, cells_all TEXT, cell_count INTEGER,
    cell_min REAL, cell_max REAL, cell_delta_mv INTEGER,
    -- capacity / charge state
    remaining_ah REAL, capacity REAL,
    soc INTEGER, soc_computed REAL, soc_drift REAL,
    soh INTEGER, cycles INTEGER, total_discharged_ah INTEGER,
    -- state and flags (raw integers so bitwise SQL queries work)
    state TEXT, state_raw INTEGER,
    protections TEXT, protection_raw INTEGER, failure_raw INTEGER,
    balancing TEXT, balancing_raw INTEGER, balance_memory_raw INTEGER,
    flags68_raw INTEGER, full_charge INTEGER, balance_latch INTEGER,
    -- regions with no observed variation, captured in case firmware uses them
    fet_flags INTEGER, fets_disabled INTEGER,
    discharge_disabled INTEGER, charge_blocked INTEGER,
    reserved56_raw INTEGER, reserved60_raw INTEGER,
    frame_len INTEGER,
    raw TEXT
);
CREATE INDEX IF NOT EXISTS idx_samples_ts   ON samples(ts);
CREATE INDEX IF NOT EXISTS idx_samples_mac  ON samples(mac, ts);

CREATE TABLE IF NOT EXISTS polls (
    ts REAL NOT NULL, mac TEXT NOT NULL, ok INTEGER NOT NULL, error TEXT
);
CREATE INDEX IF NOT EXISTS idx_polls_ts ON polls(ts);

CREATE TABLE IF NOT EXISTS daily (
    day TEXT NOT NULL, mac TEXT NOT NULL, label TEXT,
    samples INTEGER, poll_ok INTEGER, poll_total INTEGER,
    v_min REAL, v_max REAL,
    cell_min REAL, cell_max REAL, delta_max INTEGER, delta_mean REAL,
    i_min REAL, i_max REAL,
    soc_min INTEGER, soc_max INTEGER,
    t_cell_min INTEGER, t_cell_max INTEGER, t_mos_max INTEGER,
    cycles INTEGER, balancing_samples INTEGER, alarm_seen INTEGER,
    PRIMARY KEY (day, mac)
);
"""


# ------------------------------------------------------------------ storage

class Store:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)
            self._migrate(c)

    MIGRATIONS = [
        # Added 2026-09-15 when bits 0x08/0x40 of flags68 were identified as
        # FET state. Backfilled from flags68_raw, which was always stored, so
        # historical rows gain the field retroactively.
        ("ALTER TABLE samples ADD COLUMN fet_flags INTEGER",
         "UPDATE samples SET fet_flags = flags68_raw & 72"),
        ("ALTER TABLE samples ADD COLUMN fets_disabled INTEGER",
         "UPDATE samples SET fets_disabled = "
         "CASE WHEN flags68_raw & 72 THEN 1 ELSE 0 END"),
        # Added 2026-09-15 after toggling the app switch identified 0x80 as
        # discharge-disabled. The earlier fets_disabled column keyed on 0x48,
        # which turned out to mean "not accepting charge" - normal at the top
        # of charge. It is left in place but no longer used.
        ("ALTER TABLE samples ADD COLUMN discharge_disabled INTEGER",
         "UPDATE samples SET discharge_disabled = "
         "CASE WHEN flags68_raw & 128 THEN 1 ELSE 0 END"),
        ("ALTER TABLE samples ADD COLUMN charge_blocked INTEGER",
         "UPDATE samples SET charge_blocked = "
         "CASE WHEN flags68_raw & 72 THEN 1 ELSE 0 END"),
    ]

    def _migrate(self, c):
        for add, backfill in self.MIGRATIONS:
            try:
                c.execute(add)
            except sqlite3.OperationalError:
                continue      # column already present
            if backfill:
                c.execute(backfill)

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=20)
        c.row_factory = sqlite3.Row
        # WAL keeps readers from blocking the poller; NORMAL sync reduces
        # write amplification on the GX device's internal storage.
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        return c

    COLUMNS = [
        "ts", "mac", "label",
        "voltage", "cells_sum", "v_drop", "current", "power",
        "temp_cell", "temp_mosfet",
        "cells", "cells_all", "cell_count",
        "cell_min", "cell_max", "cell_delta_mv",
        "remaining_ah", "capacity",
        "soc", "soc_computed", "soc_drift",
        "soh", "cycles", "total_discharged_ah",
        "state", "state_raw",
        "protections", "protection_raw", "failure_raw",
        "balancing", "balancing_raw", "balance_memory_raw",
        "flags68_raw", "full_charge", "balance_latch",
        "fet_flags", "fets_disabled",
        "discharge_disabled", "charge_blocked",
        "reserved56_raw", "reserved60_raw",
        "frame_len", "raw",
    ]

    def insert_sample(self, mac, label, d, raw_hex):
        row = (
            time.time(), mac, label,
            d["measured_total_voltage"], d["cells_summed_voltage"],
            d["internal_voltage_drop"], d["current"], d["power_w"],
            d["cell_temp_c"], d["mosfet_temp_c"],
            json.dumps(d["cells"]), json.dumps(d["cells_all_slots"]), d["cell_count"],
            d["cell_min_v"], d["cell_max_v"], d["cell_delta_mv"],
            d["remaining_ah"], d["full_charge_capacity_ah"],
            d["soc"], d["soc_computed"], d["soc_drift_pct"],
            d["soh"], d["cycle_count"], d["total_discharged_ah"],
            d["battery_state"], d["battery_state_raw"],
            json.dumps(d["protections"]), d["protection_raw"], d["failure_raw"],
            json.dumps(d["balancing_cells"]), d["balancing_raw"], d["balance_memory_raw"],
            d["flags_68_raw"], int(d["charge_blocked"]), int(d["balance_latched"]),
            d["fet_flags"], int(d["charge_blocked"]),
            int(d["discharge_disabled"]), int(d["charge_blocked"]),
            d["reserved_56_raw"], d["reserved_60_raw"],
            d["frame_len"], raw_hex,
        )
        assert len(row) == len(self.COLUMNS), "column/row length mismatch"
        with self.lock, self._conn() as c:
            c.execute("INSERT INTO samples (%s) VALUES (%s)" % (
                ",".join(self.COLUMNS), ",".join("?" * len(row))), row)

    def insert_poll(self, mac, ok, error=None):
        with self.lock, self._conn() as c:
            c.execute("INSERT INTO polls VALUES (?,?,?,?)", (time.time(), mac, int(ok), error))

    def query(self, sql, args=()):
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    def rollup_and_prune(self, raw_days):
        """Fold old raw samples into the daily table, then delete them."""
        cutoff = time.time() - raw_days * 86400
        with self.lock, self._conn() as c:
            c.execute("""
                INSERT OR REPLACE INTO daily
                SELECT date(ts,'unixepoch','localtime') AS day, mac,
                       MAX(label), COUNT(*), 0, 0,
                       MIN(voltage), MAX(voltage),
                       MIN(cell_min), MAX(cell_max),
                       MAX(cell_delta_mv), AVG(cell_delta_mv),
                       MIN(current), MAX(current),
                       MIN(soc), MAX(soc),
                       MIN(temp_cell), MAX(temp_cell), MAX(temp_mosfet),
                       MAX(cycles),
                       SUM(CASE WHEN balancing != '[]' THEN 1 ELSE 0 END),
                       MAX(CASE WHEN protection_raw != '0x00000000'
                                  OR failure_raw != 0 THEN 1 ELSE 0 END)
                FROM samples WHERE ts < ? GROUP BY day, mac
            """, (cutoff,))
            c.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
            c.execute("DELETE FROM polls WHERE ts < ?", (cutoff,))
        # VACUUM cannot run inside a transaction, so it needs its own
        # connection after the context manager has committed.
        with self.lock:
            c = sqlite3.connect(self.path, timeout=60)
            c.isolation_level = None
            try:
                c.execute("VACUUM")
            finally:
                c.close()


# ------------------------------------------------------------------ reports

LOG_PATH = "/var/log/bms-collector/current"
TAI64_PREFIX = re.compile(r"^@[0-9a-f]{24,25}\s+")


def read_log_tail(path, lines=300):
    """Return the last N lines of the collector log, prefix stripped.

    multilog prefixes every line with a tai64n timestamp. The poller already
    writes its own local timestamp, so the prefix is redundant noise - and
    decoding tai64n correctly means tracking leap seconds, which is not worth
    doing for something we can simply drop.
    """
    if not os.path.exists(path):
        return None
    size = os.path.getsize(path)
    want = min(size, max(8192, lines * 220))
    with open(path, "rb") as fh:
        fh.seek(size - want)
        data = fh.read()
    text = data.decode("utf-8", "replace")
    if want < size:
        text = text.split("\n", 1)[-1]      # drop the partial first line
    out = [TAI64_PREFIX.sub("", ln) for ln in text.splitlines()]
    return out[-lines:]


LOG_PAGE = r"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BMS collector log</title>
<style>
 body{margin:0;background:#f2f3f4;color:#1f2429;
      font:13px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
 header{background:#fff;border-bottom:1px solid #dfe3e7;padding:10px 14px;
        display:flex;gap:10px;align-items:center;flex-wrap:wrap;
        position:sticky;top:0}
 h1{font-size:15px;margin:0;font-weight:600}
 a{color:#2f6fd0}
 select,button{font:inherit;padding:4px 8px;border:1px solid #dfe3e7;
               border-radius:6px;background:#fff}
 pre{margin:0;padding:12px 14px;white-space:pre-wrap;word-break:break-word;
     font:12px/1.5 ui-monospace,Menlo,Consolas,monospace}
 .err{color:#cc3b30;font-weight:600}
 .warn{color:#d98420}
</style></head><body>
<header>
 <h1>BMS collector log</h1>
 <a href="/">dashboard</a><a href="/docs">docs</a>
 <span style="flex:1"></span>
 <select id="n" onchange="go()">
  <option value="100">100 lines</option>
  <option value="300" selected>300 lines</option>
  <option value="1000">1000 lines</option>
 </select>
 <button onclick="go()">Refresh</button>
</header>
<pre id="log">loading…</pre>
<script>
function go(){
  const n=document.getElementById('n').value;
  fetch('/logs.txt?lines='+n).then(r=>r.text()).then(t=>{
    const pre=document.getElementById('log');
    pre.innerHTML=t.split('\n').map(l=>{
      const e=l.replace(/&/g,'&amp;').replace(/</g,'&lt;');
      if(/FAILED|Error|error|Exception/.test(l)) return '<span class="err">'+e+'</span>';
      if(/warning|discovery|pruned/.test(l)) return '<span class="warn">'+e+'</span>';
      return e;
    }).join('\n');
    window.scrollTo(0,document.body.scrollHeight);
  }).catch(e=>{document.getElementById('log').textContent='could not read log: '+e;});
}
go(); setInterval(go,30000);
</script></body></html>"""


def load_packs(path):
    """Read packs.json, accepting either form.

        {"AA:BB:...": "Battery 1"}

        {"AA:BB:...": {"label": "Battery 1",
                       "serial": "<serial from label>",
                       "ble_name": "L-12320BNN130-B02220",
                       "note": "outboard forward"}}

    Serial numbers are not retrievable over Bluetooth - the BMS returns 0xFF
    for that query - so they can only come from the physical label.
    Returns (poll_map, info_map).
    """
    with open(path) as fh:
        raw = json.load(fh)
    poll, info = {}, {}
    for mac, v in raw.items():
        if isinstance(v, dict):
            label = v.get("label") or mac
            info[mac] = dict(v, label=label, mac=mac)
        else:
            label = v
            info[mac] = {"label": label, "mac": mac}
        poll[mac] = label
    return poll, info


def internal_resistance(store, days=30, min_current=10.0):
    """Fit v_drop = offset + I * R per pack, to track internal resistance.

    v_drop is (sum of cell voltages - terminal voltage), both measured by the
    same BMS. It has two parts: a fixed calibration offset between the cell and
    terminal measurement chains, and a current-dependent drop across the FETs,
    shunt, busbars and welds. The slope is the useful part - it is an internal
    resistance measurement that needs no extra hardware, and it rises as
    connections and FETs age.

    Samples below min_current are excluded for two reasons. The BMS cannot
    resolve current below about 2.07 A, so a reported 0.0 A is really an
    unknown somewhere in that band, and using it as an exact zero biases the
    fit. Separately, a pack whose FET has opened reports 0.0 A while its
    terminals sit at bus voltage and its cells float free, which makes v_drop
    meaningless; the same threshold removes those samples.
    """
    since = time.time() - days * 86400
    rows = store.query(
        "SELECT label, current, v_drop FROM samples "
        "WHERE ts >= ? AND ABS(current) >= ? AND v_drop IS NOT NULL",
        (since, min_current))

    per = defaultdict(list)
    for r in rows:
        per[r["label"]].append((r["current"], r["v_drop"]))

    out = {}
    for label, pts in per.items():
        n = len(pts)
        xs = [p[0] for p in pts]
        span = max(xs) - min(xs) if xs else 0
        block = {"samples": n, "current_span_a": round(span, 1),
                 "min_current_a": min_current, "window_days": days}
        # Both gates matter: too few points or too narrow a current range and
        # the slope is dominated by measurement noise rather than resistance.
        if n < 30 or span < 20:
            block["status"] = "insufficient data"
            block["note"] = ("needs 30+ samples spanning 20 A; only accumulates "
                             "when the bank is actually being used")
            out[label] = block
            continue
        mx = sum(xs) / n
        ys = [p[1] for p in pts]
        my = sum(ys) / n
        sxx = sum((x - mx) ** 2 for x in xs)
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
        inter = my - slope * mx
        ssr = sum((y - (inter + slope * x)) ** 2 for x, y in zip(xs, ys))
        sst = sum((y - my) ** 2 for y in ys)
        block.update({
            "status": "ok",
            "resistance_mohm": round(-slope * 1000, 3),
            "offset_mv": round(inter * 1000, 1),
            "r_squared": round(1 - ssr / sst, 3) if sst else None,
        })
        out[label] = block

    vals = [b["resistance_mohm"] for b in out.values() if b.get("status") == "ok"]
    return {
        "packs": out,
        "spread_pct": (round((max(vals) - min(vals)) / max(vals) * 100, 1)
                       if len(vals) >= 2 and max(vals) else None),
        "interpretation": (
            "Absolute values are less meaningful than change over time. A pack "
            "whose resistance climbs relative to its siblings over months has a "
            "degrading internal connection, FET or cell. Expect 0.2-0.4 mOhm on "
            "a healthy 320Ah pack. Low r_squared means the fit is noisy, not "
            "that the pack is faulty."),
    }


def build_summary(store, hours, packs_expected=4, since=None, until=None):
    if since is None:
        since = time.time() - hours * 3600
    if until is None:
        until = time.time()
    rows = store.query("SELECT * FROM samples WHERE ts >= ? AND ts <= ? ORDER BY ts",
                       (since, until))
    polls = store.query("SELECT mac, ok FROM polls WHERE ts >= ? AND ts <= ?",
                        (since, until))

    if not rows:
        return {
            "status": "no_data", "window_hours": hours,
            "note": "No samples in window; the poller may be stopped.",
            # Keep the response shape consistent so callers can always check
            # whether they are looking at a live or historical window.
            "window_is_live": (time.time() - until) < 300,
            "window_end": datetime.fromtimestamp(until).astimezone().isoformat(),
            "generated": datetime.now(timezone.utc).isoformat(),
        }

    by = defaultdict(list)
    for r in rows:
        by[r["label"] or r["mac"]].append(r)

    pstat = defaultdict(lambda: [0, 0])
    for p in polls:
        pstat[p["mac"]][0] += p["ok"]
        pstat[p["mac"]][1] += 1

    def iso(ts):
        return datetime.fromtimestamp(ts).astimezone().isoformat()

    packs = {}
    # Staleness is judged against the end of the requested window, not against
    # the clock. On a historical range every pack is "old" by wall-clock time,
    # which would flag the whole bank as stale for no reason.
    now = until
    is_live = (time.time() - until) < 300
    stale = []
    for label, rs in sorted(by.items()):
        deltas = [r["cell_delta_mv"] for r in rs if r["cell_delta_mv"] is not None]
        currents = [r["current"] for r in rs if r["current"] is not None]
        active = [c for c in currents if abs(c) >= BASELINE["current_deadband_a"]]
        prot = sorted({p for r in rs for p in json.loads(r["protections"] or "[]")})
        praw = sorted({r["protection_raw"] for r in rs
                       if r["protection_raw"] and r["protection_raw"] != "0x00000000"})
        bal = [r for r in rs if r["balancing"] and r["balancing"] != "[]"]
        mac = rs[-1]["mac"]
        ok, total = pstat.get(mac, [0, 0])

        if is_live and now - rs[-1]["ts"] > 900:
            stale.append(label)

        # Discharge disabled (flag 0x80) is a genuine fault: the pack is
        # isolated and carries none of the load. Charge blocked (0x48) is
        # normal - every healthy pack sets it on reaching full.
        dis_rows = [r for r in rs if r["discharge_disabled"]]
        packs[label] = {
            "mac": mac,
            "samples": len(rs),
            "discharge_disabled_now": bool(rs[-1]["discharge_disabled"]),
            "discharge_disabled_pct_of_window": round(len(dis_rows) / len(rs) * 100, 1),
            "charge_blocked_now": bool(rs[-1]["charge_blocked"]),
            "fet_flags": rs[-1]["fet_flags"],
            "first": iso(rs[0]["ts"]), "last": iso(rs[-1]["ts"]),
            "poll_success_pct": round(ok / total * 100, 1) if total else None,
            "voltage_v": {"min": round(min(r["voltage"] for r in rs), 3),
                          "max": round(max(r["voltage"] for r in rs), 3)},
            "cell_v": {"min": round(min(r["cell_min"] for r in rs), 3),
                       "max": round(max(r["cell_max"] for r in rs), 3)},
            "cell_delta_mv": {"min": min(deltas), "max": max(deltas),
                              "mean": round(mean(deltas), 1)} if deltas else None,
            "current_a": {
                "min": round(min(currents), 2), "max": round(max(currents), 2),
                "samples_above_deadband": len(active),
            } if currents else None,
            "soc_pct": {"min": min(r["soc"] for r in rs), "max": max(r["soc"] for r in rs),
                        "last": rs[-1]["soc"]},
            "temp_c": {"cell_min": min(r["temp_cell"] for r in rs),
                       "cell_max": max(r["temp_cell"] for r in rs),
                       "mosfet_max": max(r["temp_mosfet"] for r in rs)},
            "cycle_count": rs[-1]["cycles"],
            "full_charge_capacity_ah": rs[-1]["capacity"],
            "alarms": {
                "protections_seen": prot,
                "protection_raw_nonzero": praw,
                "failure_field_nonzero": sorted({hex(r["failure_raw"]) for r in rs
                                                 if r["failure_raw"]}),
                "balancing_samples": len(bal),
                "balancing_window": [iso(bal[0]["ts"]), iso(bal[-1]["ts"])] if bal else None,
            },
        }

    # Current sharing under meaningful load is the imbalance signal.
    sharing = {}
    buckets = defaultdict(lambda: defaultdict(list))
    for r in rows:
        c = r["current"]
        if c is None or abs(c) < 10:
            continue
        buckets["charge" if c > 0 else "discharge"][r["label"] or r["mac"]].append(abs(c))
    for phase, per in buckets.items():
        means = {k: round(mean(v), 2) for k, v in per.items() if v}
        if len(means) >= 2:
            lo, hi = min(means.values()), max(means.values())
            sharing[phase] = {
                "mean_abs_current_by_pack": means,
                "spread_pct": round((hi - lo) / hi * 100, 1) if hi else None,
                "samples": sum(len(v) for v in per.values()),
            }

    any_alarm = any(p["alarms"]["protections_seen"] or p["alarms"]["protection_raw_nonzero"]
                    or p["alarms"]["failure_field_nonzero"] for p in packs.values())

    # A pack with its FETs disabled is isolated from the bank: it contributes
    # nothing and the remaining packs carry its share. The BMS sets no
    # protection or failure bit for this, so it must be surfaced separately.
    dis_off = [k for k, p in packs.items() if p["discharge_disabled_now"]]
    chg_blocked = [k for k, p in packs.items() if p["charge_blocked_now"]]

    return {
        "status": "ok",
        "window_hours": hours,
        "generated": datetime.now(timezone.utc).isoformat(),
        "total_samples": len(rows),
        "packs_reporting": len(by),
        "packs_expected": packs_expected,
        "packs_stale_over_15min": stale if is_live else [],
        "window_is_live": is_live,
        "window_end": datetime.fromtimestamp(until).astimezone().isoformat(),
        "any_alarm_flag_set": any_alarm or bool(dis_off),
        "packs_with_discharge_disabled": dis_off,
        "packs_not_accepting_charge": chg_blocked,
        "packs_participating": len(by) - len(dis_off),
        "fet_note": ("packs_with_discharge_disabled is a FAULT: that pack is "
                     "electrically isolated and carries none of the load, while "
                     "reporting no protection or failure bit and showing healthy "
                     "cells. It is set by the discharge switch in the LiTime app "
                     "and persists until switched back on. "
                     "packs_not_accepting_charge is NORMAL and must not be "
                     "reported as an anomaly - a pack that reaches full opens "
                     "its charge FET, and every healthy pack does this."),
        "baseline_reference": BASELINE,
        "packs": packs,
        "current_sharing": sharing,
        "internal_resistance": internal_resistance(store),
    }


# ------------------------------------------------------------------ poller

class Poller(threading.Thread):
    daemon = True

    def __init__(self, store, packs, interval, settle, prune_days):
        super().__init__()
        self.store = store
        self.packs = packs
        self.interval = interval
        self.settle = settle
        self.prune_days = prune_days
        self.running = True
        self.last_prune = 0.0
        self.bus = None

    def log(self, msg):
        print("%s %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)

    def poll_one(self, mac, label):
        s = None
        try:
            s = Session(self.bus, mac, verbose=False,
                        stop_discovery_on_connect=False)
            # s is assigned before connect() so the finally block below always
            # runs close(), which is what releases the D-Bus match rule.
            s.connect()
            frame = s.request()
            if frame is None:
                self.store.insert_poll(mac, False, "no status response")
                self.log("  %s: no response" % label)
                return
            d = decode_status(frame)
            self.store.insert_sample(mac, label, d, frame.hex())
            self.store.insert_poll(mac, True)
            self.log("  %s: %.3fV %+.2fA SOC %d%% d%smV T%s/%sC %s" % (
                label, d["measured_total_voltage"], d["current"], d["soc"],
                d["cell_delta_mv"], d["cell_temp_c"], d["mosfet_temp_c"],
                d["battery_state"]))
        except Exception as e:
            self.store.insert_poll(mac, False, "%s: %s" % (type(e).__name__, e))
            self.log("  %s: FAILED %s: %s" % (label, type(e).__name__, e))
            # A device BlueZ can no longer see is usually a stale cache entry.
            # Dropping it forces a clean re-discovery on the next cycle rather
            # than failing identically forever.
            if s and "not found after scan" in str(e):
                s.forget()
        finally:
            if s:
                try:
                    s.close()
                except Exception:
                    pass

    def ensure_discovery(self):
        """Keep one discovery session open for the life of the service.

        This adapter has no persistent BlueZ device storage, so devices are
        pruned from the object tree once they stop being seen. Leaving
        discovery running keeps all four packs resolvable without needing a
        rescan before every connection.
        """
        try:
            adapter = adapter_path(self.bus)
            if not is_discovering(self.bus, adapter):
                start_discovery(self.bus, adapter)
                self.log("discovery (re)started")
        except Exception as e:
            self.log("discovery check failed: %s" % e)

    def run(self):
        self.bus = bus()
        self.log("poller started, %d packs, %ss between packs" % (len(self.packs), self.settle))
        self.ensure_discovery()
        time.sleep(5)  # let the first advertisements land before polling
        cycle = 0
        while self.running:
            cycle += 1
            self.log("cycle %d" % cycle)
            self.ensure_discovery()
            for mac, label in self.packs.items():
                if not self.running:
                    break
                self.poll_one(mac, label)
                # Let the radio settle; back-to-back connects on one adapter
                # are a common source of failures.
                time.sleep(self.settle)
            if time.time() - self.last_prune > 86400:
                try:
                    self.store.rollup_and_prune(self.prune_days)
                    self.last_prune = time.time()
                    self.log("pruned raw samples older than %d days" % self.prune_days)
                except Exception as e:
                    self.log("prune failed: %s" % e)
            time.sleep(max(0, self.interval))


# ------------------------------------------------------------------ http

def make_handler(store, packs_expected, pack_info=None, log_path=LOG_PATH):
    pack_info = pack_info or {}

    class H(BaseHTTPRequestHandler):
        def _send(self, code, obj):
            body = json.dumps(obj, indent=1).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, filename="dashboard.html"):
            """Serve a static HTML file from alongside this script."""
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
            try:
                with open(path, "rb") as fh:
                    body = fh.read()
            except IOError:
                self._send(404, {"error": "%s not found next to bms_service.py" % filename})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)

            def window(default=24.0):
                """Resolve the query window to (since, until) epoch seconds.

                Accepts either ?hours=N or an explicit ?from=&to= pair of
                ISO-ish local datetimes, so the dashboard can offer presets and
                a custom range through the same endpoints.
                """
                f, t = q.get("from", [None])[0], q.get("to", [None])[0]
                if f or t:
                    def parse(v, fallback):
                        if not v:
                            return fallback
                        v = v.strip().replace("T", " ")
                        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                                    "%Y-%m-%d"):
                            try:
                                return time.mktime(time.strptime(v, fmt))
                            except ValueError:
                                continue
                        return fallback
                    now = time.time()
                    since = parse(f, now - default * 3600)
                    until = parse(t, now)
                    if until <= since:
                        until = since + 3600
                    return since, until
                try:
                    h = max(0.1, min(float(q.get("hours", [default])[0]), 8760))
                except ValueError:
                    h = default
                now = time.time()
                return now - h * 3600, now

            def hours(default=24.0):
                since, until = window(default)
                return (until - since) / 3600

            try:
                if u.path == "/health":
                    n = store.query("SELECT COUNT(*) n, MAX(ts) last FROM samples")[0]
                    self._send(200, {"ok": True, "samples": n["n"],
                                     "last_sample_age_s": round(time.time() - n["last"], 1)
                                     if n["last"] else None})
                elif u.path == "/bms/summary":
                    since, until = window()
                    self._send(200, build_summary(store, hours(), packs_expected,
                                                  since=since, until=until))
                elif u.path == "/bms/latest":
                    self._send(200, store.query(
                        "SELECT * FROM samples s WHERE ts = "
                        "(SELECT MAX(ts) FROM samples WHERE mac = s.mac)"))
                elif u.path == "/bms/reliability":
                    rows = store.query(
                        "SELECT mac, COUNT(*) total, SUM(ok) ok, "
                        "ROUND(100.0*SUM(ok)/COUNT(*),1) pct "
                        "FROM polls WHERE ts >= ? AND ts <= ? GROUP BY mac",
                        window()[:1] + window()[1:])
                    # polls is keyed by MAC; attach the human label so callers
                    # never have to show a bare address.
                    for r in rows:
                        r["label"] = (pack_info.get(r["mac"], {}).get("label")
                                      or r["mac"])
                    rows.sort(key=lambda r: r["label"])
                    self._send(200, rows)
                elif u.path == "/bms/series":
                    field = q.get("field", ["voltage"])[0]
                    if field not in ("voltage", "current", "soc", "cell_delta_mv",
                                     "cell_min", "cell_max", "temp_cell", "temp_mosfet"):
                        self._send(400, {"error": "unsupported field"})
                        return
                    self._send(200, store.query(
                        "SELECT ts, label, %s AS value FROM samples "
                        "WHERE ts >= ? ORDER BY ts" % field,
                        (time.time() - hours(6) * 3600,)))
                elif u.path == "/bms/packs":
                    self._send(200, {
                        "packs": pack_info,
                        "note": ("Serial numbers come from the physical labels. "
                                 "The BMS does not store them; a serial query "
                                 "returns 0xFF."),
                    })
                elif u.path == "/bms/resistance":
                    try:
                        days = float(q.get("days", ["30"])[0])
                        minc = float(q.get("min_current", ["10"])[0])
                    except ValueError:
                        days, minc = 30.0, 10.0
                    self._send(200, internal_resistance(store, days, minc))
                elif u.path == "/bms/states":
                    since, until = window(24)
                    h = (until - since) / 3600
                    rows = store.query(
                        "SELECT ts,label,protection_raw,failure_raw,balancing_raw,"
                        "discharge_disabled,state FROM samples WHERE ts >= ? ORDER BY ts",
                        (since,))
                    per = {}
                    for r in rows:
                        per.setdefault(r["label"], []).append(r)

                    def kind(r):
                        if r["protection_raw"] or r["failure_raw"]:
                            return "alarm"
                        if r["discharge_disabled"]:
                            return "discharge_off"
                        if r["balancing_raw"]:
                            return "balancing"
                        return "clear"

                    out = {}
                    for label, rs in per.items():
                        # Gap threshold from the observed cadence, so a stopped
                        # poller shows as a visible hole rather than a straight
                        # line between distant samples.
                        gaps = [rs[i + 1]["ts"] - rs[i]["ts"] for i in range(len(rs) - 1)]
                        gaps.sort()
                        typical = gaps[len(gaps) // 2] if gaps else 300
                        limit = max(600, typical * 3)

                        segs = []
                        for i, r in enumerate(rs):
                            k = kind(r)
                            if i and r["ts"] - rs[i - 1]["ts"] > limit:
                                segs.append({"t0": rs[i - 1]["ts"], "t1": r["ts"],
                                             "kind": "nodata"})
                                segs.append({"t0": r["ts"], "t1": r["ts"], "kind": k})
                            elif segs and segs[-1]["kind"] == k:
                                segs[-1]["t1"] = r["ts"]
                            else:
                                segs.append({"t0": rs[i - 1]["ts"] if i else r["ts"],
                                             "t1": r["ts"], "kind": k})
                        # Trailing gap if the pack has gone quiet.
                        if rs and time.time() - rs[-1]["ts"] > limit:
                            segs.append({"t0": rs[-1]["ts"], "t1": time.time(),
                                         "kind": "nodata"})
                        out[label] = segs

                    events = store.query(
                        "SELECT ts,label,protection_raw,failure_raw,balancing_raw,"
                        "protections,balancing,cell_min,cell_max,temp_cell,current "
                        "FROM samples WHERE ts >= ? AND (protection_raw != 0 "
                        "OR failure_raw != 0 OR balancing_raw != 0) AND ts <= ? "
                        "ORDER BY ts DESC LIMIT 200", (since, until))
                    self._send(200, {
                        "hours": h,
                        "t0": since,
                        "t1": until,
                        "packs": out,
                        "events": events,
                        "event_count": len(events),
                    })
                elif u.path == "/bms/history":
                    since, until = window(6)
                    h = (until - since) / 3600
                    rows = store.query(
                        "SELECT ts,label,voltage,current,soc,cell_delta_mv,"
                        "cell_min,cell_max,temp_cell,temp_mosfet,"
                        "discharge_disabled,charge_blocked,flags68_raw "
                        "FROM samples WHERE ts >= ? AND ts <= ? ORDER BY ts",
                        (since, until))
                    # Downsample so the browser gets a manageable payload
                    # regardless of how long a window is requested.
                    try:
                        cap = int(q.get("max_points", ["400"])[0])
                    except ValueError:
                        cap = 400
                    series = {}
                    for r in rows:
                        series.setdefault(r["label"], []).append(r)
                    out = {}
                    for label, rs in series.items():
                        step = max(1, len(rs) // cap)
                        pts = rs[::step]
                        out[label] = {
                            "ts": [round(p["ts"]) for p in pts],
                            "voltage": [p["voltage"] for p in pts],
                            "current": [p["current"] for p in pts],
                            "soc": [p["soc"] for p in pts],
                            "cell_delta_mv": [p["cell_delta_mv"] for p in pts],
                            "cell_min": [p["cell_min"] for p in pts],
                            "cell_max": [p["cell_max"] for p in pts],
                            "temp_cell": [p["temp_cell"] for p in pts],
                            "temp_mosfet": [p["temp_mosfet"] for p in pts],
                            # FET state as 0/1 series so it can be drawn as a
                            # step chart alongside the analogue measurements.
                            "discharge_disabled": [int(p["discharge_disabled"] or 0) for p in pts],
                            "charge_blocked": [int(p["charge_blocked"] or 0) for p in pts],
                            "flags68_raw": [p["flags68_raw"] for p in pts],
                        }
                    self._send(200, {"hours": h, "packs": out})
                elif u.path in ("/", "/dashboard", "/index.html"):
                    self._send_html()
                elif u.path == "/logs":
                    body = LOG_PAGE.encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif u.path == "/logs.txt":
                    try:
                        n = max(10, min(int(q.get("lines", ["300"])[0]), 5000))
                    except ValueError:
                        n = 300
                    lines = read_log_tail(log_path, n)
                    txt = ("\n".join(lines) if lines is not None
                           else "Log not found at %s.\n\nThis is normal if the "
                                "collector is running in the foreground rather than "
                                "under daemontools." % log_path)
                    body = txt.encode("utf-8", "replace")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif u.path in ("/docs", "/documentation", "/documentation.html"):
                    self._send_html("documentation.html")
                else:
                    self._send(404, {"error": "not found", "paths": [
                        "/", "/docs", "/logs", "/health", "/bms/summary", "/bms/latest",
                        "/bms/reliability", "/bms/series", "/bms/history", "/bms/resistance",
                        "/bms/packs",
                        "/bms/states"]})
            except Exception as e:
                self._send(500, {"error": "%s: %s" % (type(e).__name__, e)})

        def log_message(self, *a):
            pass

    return H


# ------------------------------------------------------------------ main

def main():
    p = argparse.ArgumentParser(description="BMS collector + query API for Venus OS")
    p.add_argument("--config", default="/data/bms/packs.json", help="JSON map of MAC -> label")
    p.add_argument("--db", default="/data/bms/bms.db")
    p.add_argument("--port", type=int, default=8088)
    p.add_argument("--bind", default="127.0.0.1",
                   help="127.0.0.1 keeps it local to Node-RED; 0.0.0.0 exposes it")
    p.add_argument("--interval", type=float, default=30.0, help="seconds between cycles")
    p.add_argument("--settle", type=float, default=12.0,
                   help="seconds between packs; BlueZ needs time to tear down")
    p.add_argument("--prune-days", type=int, default=30, help="keep raw samples this long")
    p.add_argument("--no-poll", action="store_true", help="serve only, don't poll")
    p.add_argument("--log", default=LOG_PATH,
                   help="collector log to expose at /logs")
    a = p.parse_args()

    packs, pack_info = load_packs(a.config)

    store = Store(a.db)

    if not a.no_poll:
        Poller(store, packs, a.interval, a.settle, a.prune_days).start()

    srv = ThreadingHTTPServer((a.bind, a.port),
                              make_handler(store, len(packs), pack_info, a.log))
    print("API on %s:%d, db %s" % (a.bind, a.port, a.db), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", flush=True)


if __name__ == "__main__":
    main()
