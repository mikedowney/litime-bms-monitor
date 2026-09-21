#!/usr/bin/env python3
"""
litime_bluez.py — LiTime BLE BMS reader using BlueZ over D-Bus.

Written for Venus OS, which ships python3-dbus and python3-gi but has no pip,
no venv, and under 100 MB free on /. This uses only stdlib plus those two
modules, so nothing needs installing. Keep it in /data so it survives
Venus OS firmware updates.

It registers NOTHING on the Victron dbus. It does not create a battery
service, does not participate in DVCC, and cannot influence charging.

    python3 litime_bluez.py --scan
    python3 litime_bluez.py --address AA:BB:CC:DD:EE:01
    python3 litime_bluez.py --address AA:BB:CC:DD:EE:01 --count 5 --raw

Protocol identical to litime_probe.py (verified against 3,179 frames).
"""

import argparse
import struct
import sys
import time

import dbus
import dbus.mainloop.glib
from gi.repository import GLib

BLUEZ = "org.bluez"
ADAPTER_IF = "org.bluez.Adapter1"
DEVICE_IF = "org.bluez.Device1"
CHAR_IF = "org.bluez.GattCharacteristic1"
PROPS_IF = "org.freedesktop.DBus.Properties"
OM_IF = "org.freedesktop.DBus.ObjectManager"

SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000ffe2-0000-1000-8000-00805f9b34fb"

LITIME_OUI = "C8:47:80"
HEADER_LEN = 8
OP_STATUS = 0x13

PROTECTION_FLAGS = {
    0x00000004: "over_charge",
    0x00000020: "over_discharge",
    0x00000040: "charge_over_current",
    0x00000080: "discharge_over_current",
    0x00000100: "high_temp_1",
    0x00000200: "high_temp_2",
    0x00000400: "low_temp_1",
    0x00000800: "low_temp_2",
    0x00004000: "short_circuit",
}
BATTERY_STATE = {0x0000: "idle", 0x0001: "charging", 0x0002: "discharging", 0x0004: "charge_disabled"}
# Word at offset 68 carries FET and charge-acceptance state.
#
# 0x80 = DISCHARGE DISABLED. Established 2026-09-15 by toggling the LiTime
# app's discharge switch with the collector stopped: the flag read 0xC8 with
# the switch off and 0x48 with it on, changing in both directions. The
# reference implementation independently documents 0x00000080 as "discharge
# disabled due to app button". This is the bit that matters - a pack with it
# set is isolated from the bank and carries none of the load, while reporting
# no protection or failure bit and showing perfectly healthy cells.
#
# 0x48 = charge not being accepted, i.e. the pack considers itself full and
# has opened its charge FET. This is NORMAL: every healthy pack sets it at the
# top of charge, and it appears in roughly a fifth of all logged samples
# across the bank. It is informational, never an alarm. Which of 0x08 and 0x40
# does what individually is not established; they have only ever been observed
# together.
DISCHARGE_DISABLED_MASK = 0x00000080
CHARGE_BLOCKED_MASK = 0x00000048
BALANCE_LATCH_MASK = 0x00000004


# ---------------------------------------------------------------- protocol

def build_command(opcode):
    body = bytes([0x00, 0x00, 0x04, 0x01, opcode, 0x55, 0xAA])
    return body + bytes([sum(body) & 0xFF])


def _u16(b, o):
    return struct.unpack_from("<H", b, o)[0]


def _s16(b, o):
    return struct.unpack_from("<h", b, o)[0]


def _u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def _s32(b, o):
    return struct.unpack_from("<i", b, o)[0]


def decode_status(buf):
    """Decode a status frame, capturing every field including unknowns.

    Byte map established empirically over 3,179 frames:
      0-7    header (00 00 <len> 01 <op|0x80> 55 AA 00)
      8-11   pack terminal voltage, mV (u32)
      12-15  sum of cell voltages, mV (u32)
      16-47  16 cell slots, mV (u16 each), zero = absent
      48-51  current, mA (s32), positive = charging
      52-53  cell temperature, C (s16)
      54-55  MOSFET temperature, C (s16)
      56-59  always zero in all observed frames
      60-61  always zero in all observed frames
      62-63  remaining capacity, 0.01 Ah (u16)   [SOC = remaining/full]
      64-67  full charge capacity, 0.01 Ah (u32) [per-pack learned value]
      68-71  FET state; 0x80 discharge disabled, 0x48 not accepting charge
      72-75  balance memory; bit 0x4 latches when balancing has occurred
      76-79  protection bitfield
      80-83  failure bitfield
      84-87  cell balancing bitmap
      88-89  battery state
      90-91  SoC, %
      92-95  SoH, %
      96-99  cycle count
      100-103 lifetime discharged Ah
      last   checksum = sum of preceding bytes & 0xFF
    """
    cells_all = [_u16(buf, i) for i in range(16, 48, 2)]
    cells = [round(v / 1000, 3) for v in cells_all if v]

    protection_raw = _u32(buf, 76)
    failure_raw = _u32(buf, 80)
    balancing_raw = _u32(buf, 84)
    flags_68 = _u32(buf, 68)
    balance_mem = _u32(buf, 72)
    state_raw = _u16(buf, 88)

    measured_v = _u32(buf, 8) / 1000
    summed_v = _u32(buf, 12) / 1000
    remaining_ah = _u16(buf, 62) / 100
    full_ah = _u32(buf, 64) / 100

    out = {
        # --- primary measurements
        "measured_total_voltage": round(measured_v, 3),
        "cells_summed_voltage": round(summed_v, 3),
        "internal_voltage_drop": round(summed_v - measured_v, 3),
        "current": round(_s32(buf, 48) / 1000, 3),
        "cell_temp_c": _s16(buf, 52),
        "mosfet_temp_c": _s16(buf, 54),

        # --- cells
        "cells": cells,
        "cells_all_slots": cells_all,
        "cell_count": len(cells),
        "cell_min_v": min(cells) if cells else None,
        "cell_max_v": max(cells) if cells else None,
        "cell_delta_mv": round((max(cells) - min(cells)) * 1000) if cells else None,

        # --- capacity and state of charge
        "remaining_ah": remaining_ah,
        "full_charge_capacity_ah": full_ah,
        "soc": _u16(buf, 90),
        "soc_computed": round(remaining_ah / full_ah * 100, 2) if full_ah else None,
        "soh": _u32(buf, 92),
        "cycle_count": _u32(buf, 96),
        "total_discharged_ah": _u32(buf, 100),

        # --- state and flags
        "battery_state": BATTERY_STATE.get(state_raw, "0x%04x" % state_raw),
        "battery_state_raw": state_raw,
        "protections": [n for bit, n in PROTECTION_FLAGS.items() if protection_raw & bit],
        "protection_raw": protection_raw,
        "failure_raw": failure_raw,
        "balancing_cells": [i for i in range(len(cells)) if balancing_raw & (1 << i)],
        "balancing_raw": balancing_raw,
        "balance_memory_raw": balance_mem,
        "flags_68_raw": flags_68,
        "fet_flags": flags_68 & 0xFF,
        "discharge_disabled": bool(flags_68 & DISCHARGE_DISABLED_MASK),
        "charge_blocked": bool(flags_68 & CHARGE_BLOCKED_MASK),
        "balance_latched": bool(balance_mem & BALANCE_LATCH_MASK),

        # --- regions with no observed variation; captured so a future
        #     firmware or condition that populates them is not silently lost
        "reserved_56_raw": _u32(buf, 56),
        "reserved_60_raw": _u16(buf, 60),

        "frame_len": len(buf),
    }
    out["power_w"] = round(out["measured_total_voltage"] * out["current"], 1)
    # Divergence between the BMS counter and its own capacity maths is the
    # drift signal; B01950 read 114% during characterisation.
    out["soc_drift_pct"] = (round(out["soc_computed"] - out["soc"], 2)
                            if out["soc_computed"] is not None else None)
    return out


class FrameAssembler:
    """Reassemble length-prefixed frames from MTU-sized notifications."""

    def __init__(self, on_frame, on_error=None, stale_after=3.0):
        self.buf = bytearray()
        self.on_frame = on_frame
        self.on_error = on_error or (lambda m: None)
        self.stale_after = stale_after
        self.last = 0.0

    def feed(self, data):
        now = time.monotonic()
        if self.buf and now - self.last > self.stale_after:
            self.buf.clear()
        self.last = now
        self.buf.extend(data)
        while True:
            while len(self.buf) >= 7 and not (
                self.buf[0] == 0 and self.buf[1] == 0
                and self.buf[5] == 0x55 and self.buf[6] == 0xAA
            ):
                del self.buf[0]
            if len(self.buf) < HEADER_LEN:
                return
            total = self.buf[2] + 4
            if len(self.buf) < total:
                return
            frame = bytes(self.buf[:total])
            del self.buf[:total]
            if (sum(frame[:-1]) & 0xFF) != frame[-1]:
                self.on_error("checksum mismatch on %d-byte frame" % total)
                continue
            self.on_frame(frame)


# ---------------------------------------------------------------- bluez

def bus():
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    return dbus.SystemBus()


def managed_objects(b):
    om = dbus.Interface(b.get_object(BLUEZ, "/"), OM_IF)
    return om.GetManagedObjects()


def adapter_path(b, want="hci0"):
    for path, ifaces in managed_objects(b).items():
        if ADAPTER_IF in ifaces and path.endswith(want):
            return path
    raise RuntimeError("no adapter %s found" % want)


def device_path(adapter, address):
    return "%s/dev_%s" % (adapter, address.upper().replace(":", "_"))


def _adapter_props(b, adapter):
    return dbus.Interface(b.get_object(BLUEZ, adapter), PROPS_IF)


def is_discovering(b, adapter):
    try:
        return bool(_adapter_props(b, adapter).Get(ADAPTER_IF, "Discovering"))
    except dbus.DBusException:
        return False


def stop_discovery(b, adapter):
    try:
        dbus.Interface(b.get_object(BLUEZ, adapter), ADAPTER_IF).StopDiscovery(timeout=20)
    except dbus.DBusException:
        pass


def start_discovery(b, adapter):
    """Begin discovery, tolerating an already-running session.

    bluetoothd can be slow to answer StartDiscovery, and a NoReply does not
    reliably mean discovery failed to start, so treat it as non-fatal and let
    the caller check the Discovering property instead.
    """
    props = _adapter_props(b, adapter)
    try:
        if not props.Get(ADAPTER_IF, "Powered"):
            props.Set(ADAPTER_IF, "Powered", dbus.Boolean(True))
            time.sleep(1)
    except dbus.DBusException:
        pass

    if is_discovering(b, adapter):
        return True

    ad = dbus.Interface(b.get_object(BLUEZ, adapter), ADAPTER_IF)
    try:
        ad.StartDiscovery(timeout=40)
        return True
    except dbus.DBusException as e:
        msg = str(e)
        if "InProgress" in msg:
            return True
        if "NoReply" in msg or "Timeout" in msg:
            # bluetoothd was slow; discovery may still have started.
            time.sleep(2)
            return is_discovering(b, adapter)
        raise


def wait(seconds):
    """Block while letting GLib dispatch D-Bus signals."""
    loop = GLib.MainLoop()
    GLib.timeout_add(int(seconds * 1000), lambda: (loop.quit(), False)[1])
    loop.run()


def discover(b, adapter, seconds, keep_running=False):
    started = start_discovery(b, adapter)
    if not started:
        print("warning: discovery did not start", file=sys.stderr)
    wait(seconds)
    if not keep_running:
        stop_discovery(b, adapter)


def find_chars(b, devpath):
    """Return (notify_path, write_path) for the LiTime service on this device."""
    notify = write = None
    for path, ifaces in managed_objects(b).items():
        if not path.startswith(devpath + "/"):
            continue
        c = ifaces.get(CHAR_IF)
        if not c:
            continue
        uuid = str(c.get("UUID", "")).lower()
        if uuid == NOTIFY_UUID:
            notify = path
        elif uuid == WRITE_UUID:
            write = path
    return notify, write


class Session:
    def __init__(self, b, address, verbose=True, stop_discovery_on_connect=True):
        self.b = b
        self.address = address
        self.verbose = verbose
        # The long-running collector keeps one discovery session open for its
        # whole lifetime and sets this False; one-shot CLI use leaves it True.
        self.stop_discovery_on_connect = stop_discovery_on_connect
        self.adapter = adapter_path(b)
        self.devpath = device_path(self.adapter, address)
        self.frames = []
        self.assembler = FrameAssembler(self.frames.append, self._warn)
        self.dev = None
        self.notify_char = None
        self.write_char = None
        # D-Bus match rules are per-connection and capped (1024 by default).
        # Every add_signal_receiver must be paired with a remove, or a
        # long-running poller exhausts the limit and every call starts failing
        # with LimitsExceeded.
        self._match = None

    def _warn(self, msg):
        if self.verbose:
            print("  ! %s" % msg, file=sys.stderr)

    def _on_props(self, iface, changed, invalidated, path=None):
        if iface == CHAR_IF and "Value" in changed:
            self.assembler.feed(bytes(bytearray(changed["Value"])))

    def connect(self, timeout=30):
        if self.devpath not in managed_objects(self.b):
            # BlueZ drops the device object while a previous link is still
            # tearing down, and prunes unpaired devices after discovery stops.
            # Give it a moment, then scan, keeping discovery running until the
            # object reappears.
            for _ in range(6):
                wait(0.5)
                if self.devpath in managed_objects(self.b):
                    break
        if self.devpath not in managed_objects(self.b):
            if self.verbose:
                print("device not cached, scanning...", file=sys.stderr)
            start_discovery(self.b, self.adapter)
            deadline = time.monotonic() + 45
            found = False
            while time.monotonic() < deadline:
                wait(1.0)
                if self.devpath in managed_objects(self.b):
                    found = True
                    break
            if not found:
                stop_discovery(self.b, self.adapter)
                raise RuntimeError("device %s not found after scan" % self.address)

        self.dev = dbus.Interface(self.b.get_object(BLUEZ, self.devpath), DEVICE_IF)
        props = dbus.Interface(self.b.get_object(BLUEZ, self.devpath), PROPS_IF)

        try:
            if not props.Get(DEVICE_IF, "Connected"):
                self.dev.Connect(timeout=timeout)
        finally:
            # Only stop discovery if we own it. This system has no persistent
            # BlueZ device storage (/var/lib/bluetooth/<adapter> does not
            # exist), so stopping discovery lets BlueZ prune unseen devices
            # and the next pack becomes unreachable.
            if self.stop_discovery_on_connect:
                stop_discovery(self.b, self.adapter)

        # Wait for GATT resolution before looking for characteristics.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if props.Get(DEVICE_IF, "ServicesResolved"):
                    break
            except dbus.DBusException:
                pass
            wait(0.25)
        else:
            raise RuntimeError("services did not resolve within %ds" % timeout)

        self.notify_char, self.write_char = find_chars(self.b, self.devpath)
        if not self.notify_char or not self.write_char:
            raise RuntimeError("LiTime characteristics not found on device")

        self._match = self.b.add_signal_receiver(
            self._on_props,
            dbus_interface=PROPS_IF,
            signal_name="PropertiesChanged",
            path=self.notify_char,
            path_keyword="path",
        )
        dbus.Interface(self.b.get_object(BLUEZ, self.notify_char), CHAR_IF).StartNotify()
        if self.verbose:
            print("connected to %s" % self.address, file=sys.stderr)

    def request(self, opcode=OP_STATUS, timeout=6.0):
        before = len(self.frames)
        w = dbus.Interface(self.b.get_object(BLUEZ, self.write_char), CHAR_IF)
        w.WriteValue(dbus.Array([dbus.Byte(x) for x in build_command(opcode)],
                                signature="y"), dbus.Dictionary({}, signature="sv"))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for f in self.frames[before:]:
                if f[4] == (opcode | 0x80):
                    return f
            wait(0.1)
        return None

    def close(self, wait_for_teardown=8.0):
        """Disconnect and wait for BlueZ to actually finish tearing down.

        Disconnect() returns before the link layer is clear. Starting the next
        pack's connection too early causes BlueZ to drop the device object
        mid-discovery, which shows up as "device not found after scan" on
        alternating packs.
        """
        # Remove the match rule first: this is the one step that must happen
        # on every single poll, successful or not.
        if self._match is not None:
            try:
                self._match.remove()
            except Exception:
                try:
                    self.b.remove_signal_receiver(
                        self._on_props, dbus_interface=PROPS_IF,
                        signal_name="PropertiesChanged", path=self.notify_char)
                except Exception:
                    pass
            self._match = None

        if self.notify_char:
            try:
                dbus.Interface(self.b.get_object(BLUEZ, self.notify_char),
                               CHAR_IF).StopNotify()
            except Exception:
                pass
        try:
            self.dev.Disconnect()
        except Exception:
            pass

        props = None
        try:
            props = dbus.Interface(self.b.get_object(BLUEZ, self.devpath), PROPS_IF)
        except Exception:
            return
        deadline = time.monotonic() + wait_for_teardown
        while time.monotonic() < deadline:
            try:
                if not props.Get(DEVICE_IF, "Connected"):
                    return
            except dbus.DBusException:
                return  # object already gone, which is also "disconnected"
            wait(0.25)
        if self.verbose:
            print("  ! %s still connected after %.0fs" % (self.address, wait_for_teardown),
                  file=sys.stderr)

    def forget(self):
        """Ask BlueZ to drop the cached device so the next scan re-creates it."""
        try:
            ad = dbus.Interface(self.b.get_object(BLUEZ, self.adapter), ADAPTER_IF)
            ad.RemoveDevice(self.devpath)
        except Exception:
            pass


# ---------------------------------------------------------------- cli

def do_scan(seconds):
    b = bus()
    ad = adapter_path(b)
    print("scanning %ds on %s..." % (seconds, ad))
    # Read properties while discovery is still active; BlueZ clears RSSI and
    # prunes unpaired devices once discovery stops.
    discover(b, ad, seconds, keep_running=True)
    rows = []
    for path, ifaces in managed_objects(b).items():
        d = ifaces.get(DEVICE_IF)
        if not d:
            continue
        addr = str(d.get("Address", ""))
        rssi = int(d.get("RSSI", -999))
        name = str(d.get("Name", d.get("Alias", "")))
        uuids = [str(u).lower() for u in d.get("UUIDs", [])]
        is_lt = addr.upper().startswith(LITIME_OUI) or SERVICE_UUID in uuids
        rows.append((is_lt, addr, rssi, name))
    rows.sort(key=lambda r: (not r[0], -r[2]))
    for is_lt, addr, rssi, name in rows:
        print("%s %s  %5s dBm  %s" % ("LITIME >>" if is_lt else "         ",
                                      addr, rssi if rssi != -999 else "?", name))
    n = sum(1 for r in rows if r[0])
    print("\n%d likely LiTime device(s) of %d total." % (n, len(rows)))
    stop_discovery(b, ad)


def do_read(address, count, interval, raw):
    b = bus()
    s = Session(b, address)
    s.connect()
    try:
        for i in range(count):
            f = s.request()
            if f is None:
                print("[%d/%d] no response" % (i + 1, count), file=sys.stderr)
            else:
                if raw:
                    print("[%d/%d] raw (%dB): %s" % (i + 1, count, len(f), f.hex()),
                          file=sys.stderr)
                d = decode_status(f)
                print("[%d/%d] %7.3f V %+8.3f A  SOC %3d%%  cells %s (d%smV)  "
                      "T %s/%sC  %s%s" % (
                          i + 1, count, d["measured_total_voltage"], d["current"],
                          d["soc"], "/".join("%.3f" % c for c in d["cells"]),
                          d["cell_delta_mv"], d["cell_temp_c"], d["mosfet_temp_c"],
                          d["battery_state"],
                          "  PROTECT:" + ",".join(d["protections"]) if d["protections"] else ""))
            if i + 1 < count:
                time.sleep(interval)
    finally:
        s.close()


def main():
    p = argparse.ArgumentParser(description="LiTime BLE reader via BlueZ D-Bus")
    p.add_argument("--scan", action="store_true")
    p.add_argument("--timeout", type=float, default=20.0)
    p.add_argument("--address")
    p.add_argument("--count", type=int, default=5)
    p.add_argument("--interval", type=float, default=3.0)
    p.add_argument("--raw", action="store_true")
    a = p.parse_args()

    if a.scan:
        do_scan(a.timeout)
    elif a.address:
        do_read(a.address, a.count, a.interval, a.raw)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
