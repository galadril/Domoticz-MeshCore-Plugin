"""
<plugin key="MeshCore" name="MeshCore" author="galadril" version="0.0.1" wikilink="" externallink="https://github.com/galadril/Domoticz-MeshCore-Plugin">
    <description>
        MeshCore LoRa mesh integration for Domoticz.
        Requires: pip install -r requirements.txt
    </description>
    <params>
        <param field="Address"  label="MeshCore Host"               width="200px" required="true" default="192.168.1.50"/>
        <param field="Port"     label="MeshCore Port"               width="80px"  required="true" default="5000"/>
        <param field="Mode4"    label="Install Custom Dashboard" width="150px">
            <options>
                <option label="Yes" value="true" default="true"/>
                <option label="No"  value="false"/>
            </options>
        </param>
        <param field="Mode6"   label="Debug Level" width="150px">
            <options>
                <option label="None"  value="0" default="true"/>
                <option label="Basic" value="62"/>
                <option label="All"   value="-1"/>
            </options>
        </param>
    </params>
</plugin>
"""

import Domoticz
import asyncio
import json
import os
import queue
import threading
import time
import traceback

try:
    from meshcore import MeshCore
    from meshcore.events import EventType
    MESHCORE_AVAILABLE = True
except ImportError:
    MESHCORE_AVAILABLE = False

# ── Device unit scheme ────────────────────────────────────────────────────────
# Units 1-9: global devices
# Units 10+: NODE_SLOTS slots per node (index 0 = self node, 1..N = tracked nodes)
UNIT_INBOX      = 1
UNIT_SEND       = 2   # Text device: write "[node: ]message" here to send
UNIT_MSGS_RECV  = 3   # Custom counter: messages received today
UNIT_MSGS_SENT_ = 4   # Custom counter: messages sent today

NODE_BASE  = 10
NODE_SLOTS = 20   # device slots reserved per node (max 11 nodes → unit 219)

# Offsets within each node's slot block
OFF_STATUS    = 0   # Switch:      online / offline
OFF_BATT_PCT  = 1   # Percentage:  battery %
OFF_BATT_V    = 2   # Custom (V):  battery voltage
OFF_RSSI      = 3   # Custom (dBm): last RSSI
OFF_SNR       = 4   # Custom (dB):  last SNR
OFF_NOISE     = 5   # Custom (dBm): noise floor
OFF_LASTSEEN  = 6   # Text:        timestamp of last received message/advert
OFF_TEMP      = 7   # Temperature: °C
OFF_HUMID     = 8   # Humidity:    %
OFF_HOPS      = 9   # Custom:      path length (hops)
OFF_UPTIME    = 10  # Custom (min): node uptime
OFF_AIRTIME   = 11  # Custom (%):  TX airtime utilization
OFF_MSGS_SENT = 12  # Custom:      total messages sent
OFF_MSGS_RECV = 13  # Custom:      total messages received

# Cayenne LPP sensor type codes (used in self_telemetry LPP list entries)
LPP_TEMPERATURE = 103
LPP_HUMIDITY    = 104
LPP_VOLTAGE     = 116   # channel 1 = battery

# Battery voltage range for % calculation (mV)
BAT_VMIN_MV = 3000
BAT_VMAX_MV = 4200

# Node is considered online if last_advert is newer than this (8 h)
ONLINE_THRESHOLD_S = 28800

# How many heartbeats between self-node stats polls (heartbeat=30s, so 10 = ~5 min)
SELF_STATS_HEARTBEATS = 10

# Connection timeout for each short-lived TCP session (seconds)
CONNECT_TIMEOUT    = 8
COMMAND_TIMEOUT    = 10
WORKER_TIMEOUT     = 60    # max seconds a worker thread may run before being abandoned

# Backoff on consecutive connection failures
BACKOFF_BASE_S     = 30     # first extra delay on failure (added on top of the heartbeat interval)
BACKOFF_MAX_S      = 300    # 5 min max extra delay
BACKOFF_FACTOR     = 2.0


def _bat_pct(mv: int) -> int:
    return max(0, min(100, int((mv - BAT_VMIN_MV) / (BAT_VMAX_MV - BAT_VMIN_MV) * 100)))


class BasePlugin:

    def __init__(self):
        self._queue          = queue.Queue()  # worker → main thread
        self.host            = ""
        self.port            = 5000
        self._contact_names  = []   # contact names discovered from mc.contacts (non-self)
        self.initialized     = False
        self._self_name      = ""   # name of the connected node
        # pubkey_prefix (12 hex chars) → adv_name, rebuilt from contacts
        self._prefix_to_name = {}
        # node_name → last Unix timestamp we saw ANY activity from it
        self._node_last_activity: dict = {}
        # node_name → {"lat": float, "lon": float} from contact adv_lat/adv_lon
        self._node_locations: dict = {}
        # Last sValue we already dispatched — prevents re-sending on every heartbeat
        self._last_sent_text = ""
        # Message counters (reset when Domoticz restarts the plugin)
        self._recv_count = 0
        self._sent_count = 0
        # Heartbeat counter for periodic self-stats poll
        self._hb_count = 0
        # Backoff state for consecutive connection failures
        self._consec_failures = 0
        self._skip_until = 0.0  # monotonic time: skip heartbeats until this
        # Channel names already fetched flag (only need once)
        self._channels_fetched = False
        # Lock to serialise all TCP connections (ESP32 accepts only one at a time)
        self._conn_lock = threading.Lock()
        # Current mc instance (set by worker thread for cleanup on error)
        self._current_mc = None
        # Active worker thread reference (for onStop cleanup)
        self._worker_thread: threading.Thread | None = None
        self._worker_started: float = 0.0
        # Flag to prevent new connections during shutdown
        self._stopping = False

    def _force_kill_socket(self, mc):
        """Kill the raw TCP socket without touching the event loop."""
        import socket as _socket
        import struct
        try:
            transport = mc.connection_manager.connection.transport
            if transport:
                raw_sock = transport.get_extra_info("socket")
                if raw_sock:
                    try:
                        raw_sock.setsockopt(
                            _socket.SOL_SOCKET, _socket.SO_LINGER,
                            struct.pack("ii", 1, 0)
                        )
                    except OSError:
                        pass
                transport.close()
                if raw_sock:
                    try:
                        raw_sock.close()
                    except OSError:
                        pass
        except Exception:
            pass
        try:
            if mc.dispatcher and mc.dispatcher._task and not mc.dispatcher._task.done():
                mc.dispatcher.running = False
                mc.dispatcher._task.cancel()
        except Exception:
            pass

    def _node_index(self, node_name: str) -> int:
        """Return slot index: 0 = self node, 1..N = contacts."""
        if node_name == self._self_name:
            return 0
        if node_name in self._contact_names:
            return self._contact_names.index(node_name) + 1
        return -1

    def _node_unit(self, node_idx: int, offset: int) -> int:
        return NODE_BASE + node_idx * NODE_SLOTS + offset

    def _all_node_names(self):
        """Self node (if known) + all discovered contacts."""
        names = []
        if self._self_name:
            names.append(self._self_name)
        names.extend(self._contact_names)
        return names

    def _safe_disconnect(self, mc, loop):
        """Aggressively tear down the TCP connection so the ESP32 releases it immediately."""
        if mc is None:
            return
        Domoticz.Log("_safe_disconnect: killing socket…")
        self._force_kill_socket(mc)
        # Cancel any remaining asyncio tasks (don't await — just cancel)
        try:
            for task in asyncio.all_tasks(loop):
                task.cancel()
        except Exception:
            pass
        time.sleep(0.1)
        Domoticz.Log("_safe_disconnect: done.")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def onStart(self):
        if not MESHCORE_AVAILABLE:
            Domoticz.Error("meshcore package not installed. Run: pip install meshcore")
            return

        Domoticz.Debugging(int(Parameters["Mode6"] or 0))
        self.host     = Parameters["Address"].strip()
        self.port     = int(Parameters["Port"].strip() or 5000)
        self._create_base_devices()
        self._load_manual_locations()

        if Parameters.get("Mode4", "true") == "true":
            self._install_custom_page()
            self._install_manual_locations()

        self.initialized = True
        Domoticz.Heartbeat(30)
        Domoticz.Log(f"MeshCore plugin started – {self.host}:{self.port}")

    def onStop(self):
        self._stopping = True
        self.initialized = False

        # Forcefully RST-close any active TCP connection so the ESP32 releases it
        mc = self._current_mc
        if mc is not None:
            Domoticz.Log("onStop: force-killing active connection…")
            self._force_kill_socket(mc)
            Domoticz.Log("onStop: socket killed.")

        # Wait for any running worker thread to finish
        t = self._worker_thread
        if t is not None and t.is_alive():
            t.join(timeout=5)
            if t.is_alive():
                Domoticz.Log("onStop: worker thread did not stop within 5s.")

        self._remove_custom_page()
        Domoticz.Log("MeshCore plugin stopped.")

    def onHeartbeat(self):
        if not self.initialized or self._stopping:
            return

        # Drain results from the worker thread (device updates, logging)
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            self._dispatch(item)

        # Backoff: skip this heartbeat if we are in a cooldown period
        now = time.monotonic()
        if now < self._skip_until:
            remaining = int(self._skip_until - now)
            Domoticz.Debug(f"Backoff active — skipping heartbeat ({remaining}s remaining)")
            return

        # Watchdog: if a worker thread has been running longer than WORKER_TIMEOUT,
        # force-kill its connection and release the lock.
        t = self._worker_thread
        if t is not None and t.is_alive():
            elapsed = time.monotonic() - self._worker_started
            if elapsed > WORKER_TIMEOUT:
                Domoticz.Error(f"Watchdog: worker thread hung for {int(elapsed)}s — force-killing connection")
                mc = self._current_mc
                if mc is not None:
                    self._force_kill_socket(mc)
                # Give the thread a moment to die after socket kill
                t.join(timeout=2)
                if not t.is_alive():
                    Domoticz.Log("Watchdog: worker thread exited after socket kill.")
                else:
                    Domoticz.Error("Watchdog: worker thread still alive — force-releasing lock.")
                    # Force-release the lock so we can continue
                    try:
                        self._conn_lock.release()
                    except RuntimeError:
                        pass
                self._worker_thread = None

        # Prevent overlapping connections (previous heartbeat or send still running)
        if not self._conn_lock.acquire(blocking=False):
            Domoticz.Debug("Previous connection still active — skipping heartbeat")
            return

        self._hb_count += 1
        t = threading.Thread(target=self._heartbeat_worker, daemon=True, name="MeshCorePoll")
        self._worker_thread = t
        self._worker_started = time.monotonic()
        t.start()

    def onDeviceModified(self, unit: int):
        if unit != UNIT_SEND or self._stopping:
            return
        dev = Devices.get(UNIT_SEND)
        if not dev or not dev.sValue:
            return
        text = dev.sValue.strip()
        if not text:
            return
        if text == self._last_sent_text:
            return
        self._last_sent_text = text
        Domoticz.Log(f"Sending message immediately: {text}")
        t = threading.Thread(
            target=self._immediate_send_worker, args=(text,),
            daemon=True, name="MeshCoreSend"
        )
        self._worker_thread = t
        self._worker_started = time.monotonic()
        t.start()

    # ── Device creation ───────────────────────────────────────────────────────

    def _create_base_devices(self):
        if UNIT_INBOX not in Devices:
            Domoticz.Device(Name="Mesh Inbox", Unit=UNIT_INBOX, TypeName="Text").Create()
        if UNIT_SEND not in Devices:
            Domoticz.Device(Name="Mesh Send",  Unit=UNIT_SEND,  TypeName="Text").Create()
        if UNIT_MSGS_RECV not in Devices:
            Domoticz.Device(Name="Mesh Msgs Received", Unit=UNIT_MSGS_RECV,
                            TypeName="Custom", Options={"Custom": "1;msgs"}).Create()
        if UNIT_MSGS_SENT_ not in Devices:
            Domoticz.Device(Name="Mesh Msgs Sent", Unit=UNIT_MSGS_SENT_,
                            TypeName="Custom", Options={"Custom": "1;msgs"}).Create()

    def _load_manual_locations(self):
        """Load meshcore_locations.json from the plugin directory as seed locations.

        Format: {"NodeName": {"lat": 52.123, "lon": 4.567}, ...}
        These are used as fallback — live GPS data from contacts overwrites them.
        """
        loc_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "meshcore_locations.json")
        if not os.path.isfile(loc_file):
            return
        try:
            with open(loc_file, "r") as f:
                manual = json.load(f)
            for name, loc in manual.items():
                lat = loc.get("lat", 0)
                lon = loc.get("lon", 0)
                if lat and lon:
                    self._node_locations.setdefault(name, {"lat": lat, "lon": lon})
            Domoticz.Log(f"Loaded manual locations for {len(manual)} node(s) from meshcore_locations.json")
        except Exception as exc:
            Domoticz.Error(f"Could not load meshcore_locations.json: {exc}")

    def _ensure_node_devices(self, node_name: str):
        """Create per-node devices on first data for that node."""
        idx = self._node_index(node_name)
        if idx < 0:
            return
        is_self = (idx == 0)
        if is_self:
            specs = [
                (OFF_STATUS,    f"{node_name} Status",      "Switch",      {}),
                (OFF_BATT_PCT,  f"{node_name} Battery",     "Percentage",  {}),
                (OFF_BATT_V,    f"{node_name} Battery V",   "Custom",      {"Custom": "1;V"}),
                (OFF_RSSI,      f"{node_name} RSSI",        "Custom",      {"Custom": "1;dBm"}),
                (OFF_SNR,       f"{node_name} SNR",         "Custom",      {"Custom": "1;dB"}),
                (OFF_NOISE,     f"{node_name} Noise Floor", "Custom",      {"Custom": "1;dBm"}),
                (OFF_LASTSEEN,  f"{node_name} Last Seen",   "Text",        {}),
                (OFF_UPTIME,    f"{node_name} Uptime",      "Custom",      {"Custom": "1;min"}),
                (OFF_AIRTIME,   f"{node_name} Airtime TX",  "Custom",      {"Custom": "1;s"}),
                (OFF_MSGS_SENT, f"{node_name} Pkts Sent",   "Custom",      {"Custom": "1;pkts"}),
                (OFF_MSGS_RECV, f"{node_name} Pkts Recv",   "Custom",      {"Custom": "1;pkts"}),
            ]
        else:
            # Remote contacts: only data reliably available from contacts list and messages
            specs = [
                (OFF_STATUS,   f"{node_name} Status",    "Switch", {}),
                (OFF_SNR,      f"{node_name} SNR",       "Custom", {"Custom": "1;dB"}),
                (OFF_LASTSEEN, f"{node_name} Last Seen", "Text",   {}),
                (OFF_HOPS,     f"{node_name} Hops",      "Custom", {"Custom": "1;hops"}),
            ]
        created = False
        for offset, name, typename, opts in specs:
            unit = self._node_unit(idx, offset)
            if unit not in Devices:
                Domoticz.Device(Name=name, Unit=unit, TypeName=typename, Options=opts).Create()
                created = True
        if created:
            Domoticz.Log(f"Created devices for node '{node_name}' (idx={idx})")
            self._write_device_map()

    # ── Custom dashboard page ──────────────────────────────────────────────────

    def _install_custom_page(self):
        template = os.path.join(os.path.dirname(os.path.abspath(__file__)), "meshcore.html")
        if not os.path.isfile(template):
            Domoticz.Error("meshcore.html template not found — dashboard not installed.")
            return

        plugin_dir    = os.path.dirname(os.path.abspath(__file__))
        domoticz_root = os.path.abspath(os.path.join(plugin_dir, "..", ".."))
        dest_dir      = os.path.join(domoticz_root, "www", "templates")
        dest          = os.path.join(dest_dir, "meshcore.html")

        try:
            import shutil
            os.makedirs(dest_dir, exist_ok=True)
            shutil.copy2(template, dest)
            Domoticz.Log(f"MeshCore dashboard installed: {dest}")
        except Exception as exc:
            Domoticz.Error(f"Failed to install dashboard: {exc}")

    def _install_manual_locations(self):
        """Copy meshcore_locations.json to the templates dir so the dashboard can fetch it."""
        plugin_dir    = os.path.dirname(os.path.abspath(__file__))
        src = os.path.join(plugin_dir, "meshcore_locations.json")
        if not os.path.isfile(src):
            return
        domoticz_root = os.path.abspath(os.path.join(plugin_dir, "..", ".."))
        dest = os.path.join(domoticz_root, "www", "templates", "meshcore_locations.json")
        try:
            import shutil
            shutil.copy2(src, dest)
        except Exception as exc:
            Domoticz.Debug(f"Could not install meshcore_locations.json: {exc}")

    def _write_channel_names(self, channel_names: dict):
        """Write channel index→name map as JSON for the dashboard to fetch."""
        plugin_dir    = os.path.dirname(os.path.abspath(__file__))
        domoticz_root = os.path.abspath(os.path.join(plugin_dir, "..", ".."))
        dest = os.path.join(domoticz_root, "www", "templates", "meshcore_channels.json")
        try:
            with open(dest, "w") as f:
                json.dump(channel_names, f)
        except Exception as exc:
            Domoticz.Debug(f"Could not write channel names: {exc}")

    def _write_device_map(self):
        """Write meshcore_devices.json so the dashboard can look up devices by idx
        rather than by name — rename-proof and collision-free.

        Format:
        {
          "inbox": <idx>,
          "self": "<node_name>",          # or null
          "nodes": {
            "<node_name>": {
              "status":    <idx|null>,
              "battery":   <idx|null>,
              "battery_v": <idx|null>,
              "rssi":      <idx|null>,
              "snr":       <idx|null>,
              "noise":     <idx|null>,
              "last_seen": <idx|null>,
              "hops":      <idx|null>,
              "uptime":    <idx|null>,
              "airtime":   <idx|null>,
              "pkts_sent": <idx|null>,
              "pkts_recv": <idx|null>
            },
            ...
          }
        }
        """
        def _slot(unit):
            """Return {idx, value, online} for a device unit, or None if not created yet."""
            d = Devices.get(unit)
            if not d:
                return None
            return {
                "idx":    d.ID,
                "value":  d.sValue if d.sValue else None,
                "online": d.nValue == 1,
            }

        nodes = {}
        for node_name in self._all_node_names():
            ni = self._node_index(node_name)
            if ni < 0:
                continue
            loc = self._node_locations.get(node_name, {})
            nodes[node_name] = {
                "status":    _slot(self._node_unit(ni, OFF_STATUS)),
                "battery":   _slot(self._node_unit(ni, OFF_BATT_PCT)),
                "battery_v": _slot(self._node_unit(ni, OFF_BATT_V)),
                "rssi":      _slot(self._node_unit(ni, OFF_RSSI)),
                "snr":       _slot(self._node_unit(ni, OFF_SNR)),
                "noise":     _slot(self._node_unit(ni, OFF_NOISE)),
                "last_seen": _slot(self._node_unit(ni, OFF_LASTSEEN)),
                "hops":      _slot(self._node_unit(ni, OFF_HOPS)),
                "uptime":    _slot(self._node_unit(ni, OFF_UPTIME)),
                "airtime":   _slot(self._node_unit(ni, OFF_AIRTIME)),
                "pkts_sent": _slot(self._node_unit(ni, OFF_MSGS_SENT)),
                "pkts_recv": _slot(self._node_unit(ni, OFF_MSGS_RECV)),
                "lat":       loc.get("lat"),
                "lon":       loc.get("lon"),
            }

        inbox_dev = Devices.get(UNIT_INBOX)
        send_dev  = Devices.get(UNIT_SEND)
        payload = {
            "inbox":        inbox_dev.ID if inbox_dev else None,
            "inbox_value":  inbox_dev.sValue if inbox_dev else None,
            "send_idx":     send_dev.ID if send_dev else None,
            "self":         self._self_name or None,
            "nodes":        nodes,
            "written_at":   int(time.time()),
        }

        plugin_dir    = os.path.dirname(os.path.abspath(__file__))
        domoticz_root = os.path.abspath(os.path.join(plugin_dir, "..", ".."))
        dest = os.path.join(domoticz_root, "www", "templates", "meshcore_devices.json")
        try:
            with open(dest, "w") as f:
                json.dump(payload, f)
        except Exception as exc:
            Domoticz.Debug(f"Could not write device map: {exc}")

    def _remove_custom_page(self):
        plugin_dir    = os.path.dirname(os.path.abspath(__file__))
        domoticz_root = os.path.abspath(os.path.join(plugin_dir, "..", ".."))
        fname = "meshcore.html"
        dest = os.path.join(domoticz_root, "www", "templates", fname)
        try:
            if os.path.isfile(dest):
                os.remove(dest)
        except Exception as exc:
            Domoticz.Error(f"Failed to remove {fname}: {exc}")
        Domoticz.Log("MeshCore dashboard removed.")

    # ── Heartbeat-driven poll worker ─────────────────────────────────────────

    def _heartbeat_worker(self):
        """Run a short-lived connect→poll→disconnect cycle in a background thread."""
        Domoticz.Log("Worker: started")
        loop = asyncio.new_event_loop()
        self._current_mc = None
        try:
            Domoticz.Log("Worker: entering poll cycle…")
            loop.run_until_complete(self._poll_cycle(loop))
            Domoticz.Log("Worker: poll cycle completed OK")
        except Exception as exc:
            Domoticz.Error(f"Worker: poll error: {exc}")
            self._consec_failures += 1
            delay = min(BACKOFF_BASE_S * (BACKOFF_FACTOR ** self._consec_failures), BACKOFF_MAX_S)
            if delay > 0:
                self._skip_until = time.monotonic() + delay
                Domoticz.Log(f"Worker: backing off {int(delay)}s (failure #{self._consec_failures})")
        finally:
            Domoticz.Log("Worker: entering finally block…")
            if self._current_mc is not None:
                Domoticz.Log("Worker: calling _safe_disconnect…")
                self._safe_disconnect(self._current_mc, loop)
                self._current_mc = None
            Domoticz.Log("Worker: closing event loop…")
            try:
                loop.close()
            except Exception:
                pass
            Domoticz.Log("Worker: releasing _conn_lock…")
            self._conn_lock.release()
            Domoticz.Log("Worker: done.")

    def _immediate_send_worker(self, text: str):
        """Short-lived connect → send → disconnect fired from onDeviceModified."""
        Domoticz.Log(f"SendWorker: waiting for _conn_lock…")
        if not self._conn_lock.acquire(timeout=30):
            Domoticz.Error("SendWorker: _conn_lock timeout after 30s")
            self._queue.put(("send_result", {"ok": False, "target": "?",
                                             "body": text, "result": "connection busy (timeout)"}))
            return

        Domoticz.Log(f"SendWorker: lock acquired, starting send cycle…")
        loop = asyncio.new_event_loop()
        self._current_mc = None
        try:
            loop.run_until_complete(self._send_cycle(text, loop))
            Domoticz.Log("SendWorker: send cycle completed OK")
        except Exception as exc:
            Domoticz.Error(f"SendWorker: error: {exc}")
            self._queue.put(("send_result", {"ok": False, "target": "?",
                                             "body": text, "result": str(exc)}))
        finally:
            Domoticz.Log("SendWorker: entering finally block…")
            if self._current_mc is not None:
                Domoticz.Log("SendWorker: calling _safe_disconnect…")
                self._safe_disconnect(self._current_mc, loop)
                self._current_mc = None
            Domoticz.Log("SendWorker: closing event loop…")
            try:
                loop.close()
            except Exception:
                pass
            Domoticz.Log("SendWorker: releasing _conn_lock…")
            self._conn_lock.release()
            Domoticz.Log("SendWorker: done.")

    async def _send_cycle(self, text: str, loop):
        """Connect, send one message, disconnect.  Stores mc on self._current_mc."""
        from meshcore.tcp_cx import TCPConnection

        Domoticz.Log(f"SendCycle: connecting to {self.host}:{self.port}…")
        connection = TCPConnection(self.host, self.port)
        mc = MeshCore(connection)
        self._current_mc = mc

        Domoticz.Log("SendCycle: calling mc.connect()…")
        res = await asyncio.wait_for(mc.connect(), timeout=CONNECT_TIMEOUT)
        if res is None:
            raise ConnectionError("Device did not respond to appstart.")
        Domoticz.Log("SendCycle: connected.")

        # We need contacts loaded for name → contact lookup
        if not mc.contacts:
            Domoticz.Log("SendCycle: fetching contacts…")
            await asyncio.wait_for(mc.commands.get_contacts(), timeout=COMMAND_TIMEOUT)
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        Domoticz.Log(f"SendCycle: sending message…")
        await self._send_message(mc, text)
        Domoticz.Log("SendCycle: message sent.")

    async def _poll_cycle(self, loop):
        """Connect, do all work.  Stores mc on self._current_mc for cleanup."""
        from meshcore.tcp_cx import TCPConnection

        Domoticz.Log(f"Poll: connecting to {self.host}:{self.port}…")

        connection = TCPConnection(self.host, self.port)
        mc = MeshCore(connection)
        self._current_mc = mc

        Domoticz.Log("Poll: calling mc.connect()…")
        res = await asyncio.wait_for(mc.connect(), timeout=CONNECT_TIMEOUT)
        if res is None:
            raise ConnectionError("Device did not respond to appstart.")
        Domoticz.Log(f"Poll: connected. self_info={mc.self_info}")
        if mc.self_info:
            name = mc.self_info.get("name", "")
            if name:
                self._self_name = name
            self._queue.put(("self_info", dict(mc.self_info)))

        # ── Fetch contacts ────────────────────────────────────────────────
        Domoticz.Log("Poll: fetching contacts…")
        for attempt in range(5):
            try:
                await asyncio.wait_for(mc.commands.get_contacts(), timeout=COMMAND_TIMEOUT)
            except asyncio.TimeoutError:
                Domoticz.Debug(f"get_contacts timed out (attempt {attempt + 1})")
            except Exception as exc:
                Domoticz.Debug(f"get_contacts error (attempt {attempt + 1}): {exc}")
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            if mc.contacts:
                break
            await asyncio.sleep(1)

        if mc.contacts:
            Domoticz.Log(f"Poll: got {len(mc.contacts)} contact(s)")
            contacts_snapshot = {k: dict(v) for k, v in mc.contacts.items()}
            self._queue.put(("contacts", contacts_snapshot))
        else:
            Domoticz.Error("Poll: no contacts returned from device.")

        # ── Fetch channel names (once) ────────────────────────────────────
        if not self._channels_fetched:
            Domoticz.Log("Poll: fetching channel names…")
            await self._fetch_channel_names(mc)
            self._channels_fetched = True

        # ── Collect incoming messages ─────────────────────────────────────
        Domoticz.Log("Poll: draining push events…")
        await self._drain_push_events(mc)

        # ── Poll self-node stats periodically ─────────────────────────────
        if self._hb_count % SELF_STATS_HEARTBEATS == 0:
            Domoticz.Log("Poll: polling self stats…")
            await self._poll_self_stats(mc)

        # Connection succeeded — reset backoff
        self._consec_failures = 0
        self._skip_until = 0.0

        Domoticz.Log("Poll: cycle complete.")

    async def _drain_push_events(self, mc):
        """Drain all pending messages from the device using get_msg().

        The device queues incoming messages; we pull them one by one until
        NO_MORE_MSGS is returned.
        """
        fetched = 0
        for _ in range(50):  # safety limit
            try:
                r = await asyncio.wait_for(mc.commands.get_msg(), timeout=5.0)
            except asyncio.TimeoutError:
                break
            except Exception as exc:
                Domoticz.Debug(f"get_msg error: {exc}")
                break
            if r is None or r.type == EventType.NO_MORE_MSGS:
                break
            if r.type in (EventType.CONTACT_MSG_RECV, EventType.CHANNEL_MSG_RECV):
                self._queue.put(("message", r.payload))
                fetched += 1
            elif r.type == EventType.ERROR:
                break
        if fetched:
            Domoticz.Log(f"Fetched {fetched} pending message(s) from device.")

    async def _poll_self_stats(self, mc):
        """Poll all available stats from the connected node itself."""
        Domoticz.Debug("Polling self-node stats…")

        stats = {}

        try:
            r = await asyncio.wait_for(mc.commands.get_stats_core(), timeout=5.0)
            if r and r.type == EventType.STATS_CORE:
                stats.update(r.payload)
                Domoticz.Debug(f"stats_core: {r.payload}")
        except Exception as exc:
            Domoticz.Debug(f"get_stats_core error: {exc}")

        try:
            r = await asyncio.wait_for(mc.commands.get_stats_radio(), timeout=5.0)
            if r and r.type == EventType.STATS_RADIO:
                stats.update(r.payload)
                Domoticz.Debug(f"stats_radio: {r.payload}")
        except Exception as exc:
            Domoticz.Debug(f"get_stats_radio error: {exc}")

        try:
            r = await asyncio.wait_for(mc.commands.get_stats_packets(), timeout=5.0)
            if r and r.type == EventType.STATS_PACKETS:
                stats.update(r.payload)
                Domoticz.Debug(f"stats_packets: {r.payload}")
        except Exception as exc:
            Domoticz.Debug(f"get_stats_packets error: {exc}")

        if stats:
            self._queue.put(("self_stats", stats))

    async def _fetch_channel_names(self, mc):
        """Query channel names for indices 0–7 and write meshcore_channels.json."""
        channel_names = {}
        for idx in range(8):
            try:
                res = await asyncio.wait_for(mc.commands.get_channel(idx), timeout=2.0)
                Domoticz.Debug(f"get_channel({idx}): type={res.type if res else None} payload={res.payload if res else None}")
                if res and res.type == EventType.CHANNEL_INFO:
                    name = res.payload.get("channel_name", "").strip("\x00").strip()
                    if name:
                        channel_names[str(idx)] = name
                elif res and res.type == EventType.ERROR:
                    # ERROR = no more channels, stop probing
                    break
            except asyncio.TimeoutError:
                # Unconfigured channels may simply not respond — skip, don't abort
                Domoticz.Debug(f"get_channel({idx}) timed out — skipping")
                continue
            except Exception as exc:
                Domoticz.Debug(f"get_channel({idx}) error: {exc} — skipping")
                continue
        Domoticz.Debug(f"Channel names fetched: {channel_names}")
        if channel_names:
            self._write_channel_names(channel_names)

    async def _send_message(self, mc, text: str):
        """Send a message from the Mesh Send device value.

        Syntax accepted:
          "hello world"          → direct message to the first tracked node
          "garden: hello"        → direct message to the node named 'garden'
          "#0: hello"            → broadcast on channel 0
          "#flood: hello"        → broadcast on channel 0 (alias)
        """
        target = None
        body   = text

        if ":" in text:
            prefix, rest = text.split(":", 1)
            prefix = prefix.strip()
            body   = rest.strip()
            if prefix.startswith("#"):
                chan_part = prefix[1:].lower()
                chan_idx  = 0 if chan_part in ("", "flood") else int(chan_part)
                try:
                    result = await asyncio.wait_for(
                        mc.commands.send_chan_msg(chan_idx, body), timeout=15.0
                    )
                    tx_busy = (
                        result is not None
                        and result.type == EventType.ERROR
                        and (result.payload or {}).get("reason") == "no_event_received"
                    )
                    ok = result is not None and result.type == EventType.OK
                    self._queue.put(("send_result", {"ok": ok, "target": f"#{chan_idx}", "body": body,
                                                    "result": "TX busy — try again" if tx_busy else str(result)}))
                except Exception as exc:
                    self._queue.put(("send_result", {"ok": False, "target": f"#{chan_idx}", "body": body, "result": str(exc)}))
                return
            else:
                target = prefix  # node name

        # Direct message to a node — success response is EventType.MSG_SENT
        if target is None:
            target = self._contact_names[0] if self._contact_names else ""
        if not target:
            self._queue.put(("send_result", {"ok": False, "target": target, "body": body, "result": "no target node"}))
            return

        contact = None
        for c in mc.contacts.values():
            if c.get("adv_name", "").strip() == target:
                contact = dict(c)
                break

        if contact is None:
            self._queue.put(("send_result", {"ok": False, "target": target, "body": body, "result": "contact not found"}))
            return

        try:
            result = await asyncio.wait_for(
                mc.commands.send_msg(contact, body), timeout=15.0
            )
            tx_busy = (
                result is not None
                and result.type == EventType.ERROR
                and (result.payload or {}).get("reason") == "no_event_received"
            )
            ok = result is not None and result.type == EventType.MSG_SENT
            self._queue.put(("send_result", {"ok": ok, "target": target, "body": body,
                                             "result": "TX busy — try again" if tx_busy else str(result)}))
        except Exception as exc:
            self._queue.put(("send_result", {"ok": False, "target": target, "body": body, "result": str(exc)}))

    # ── Queue dispatcher (runs on Domoticz main thread via onHeartbeat) ───────

    def _dispatch(self, item):
        kind = item[0]
        if kind == "message":
            Domoticz.Log(f"Message: {item[1]}")
            self._handle_message(item[1])
        elif kind == "contacts":
            self._handle_contacts(item[1])
        elif kind == "self_stats":
            self._handle_self_stats(item[1])
        elif kind == "self_info":
            name = item[1].get("name", "")
            Domoticz.Log(f"Self info: name={name}, freq={item[1].get('radio_freq')} MHz")
            if name and name != self._self_name:
                self._self_name = name
        elif kind == "send_result":
            d = item[1]
            if d["ok"]:
                Domoticz.Log(f"Message sent to '{d['target']}': {d['body']}")
                self._sent_count += 1
                if UNIT_MSGS_SENT_ in Devices:
                    Devices[UNIT_MSGS_SENT_].Update(nValue=0, sValue=str(self._sent_count))
            else:
                Domoticz.Error(f"Send failed to '{d['target']}': {d['result']}")

    # ── Data handlers ─────────────────────────────────────────────────────────

    def _handle_contacts(self, contacts: dict):
        now = time.time()

        # Rebuild prefix → friendly-name lookup
        self._prefix_to_name = {
            c.get("public_key", "")[:12]: c.get("adv_name", "").strip()
            for c in contacts.values()
        }

        # Register any new contacts (non-self) in discovery order
        for contact in contacts.values():
            name = contact.get("adv_name", "").strip()
            if name and name != self._self_name and name not in self._contact_names:
                self._contact_names.append(name)
                Domoticz.Log(f"New contact discovered: '{name}'")

        # Update self node status from self_info (always online when connected)
        if self._self_name:
            self._ensure_node_devices(self._self_name)
            idx = self._node_index(self._self_name)
            if idx >= 0:
                status_unit = self._node_unit(idx, OFF_STATUS)
                if status_unit in Devices:
                    Devices[status_unit].Update(nValue=1, sValue="On")

        # Update all remote contacts
        for contact in contacts.values():
            node_name = contact.get("adv_name", "").strip()
            if not node_name or node_name == self._self_name:
                continue

            last_advert = contact.get("last_advert", 0)
            if last_advert < 1_577_836_800:
                last_advert = 0
            last_activity = self._node_last_activity.get(node_name, 0)
            effective_ts  = max(last_advert, last_activity)
            advert_online = effective_ts > 0 and (now - effective_ts) < ONLINE_THRESHOLD_S

            path_len    = contact.get("out_path_len", -1)
            path_online = path_len >= 0
            online      = advert_online or path_online
            age_s       = int(now - effective_ts) if effective_ts > 0 else -1

            self._ensure_node_devices(node_name)
            idx = self._node_index(node_name)
            if idx < 0:
                continue

            status_unit = self._node_unit(idx, OFF_STATUS)
            if status_unit in Devices:
                Devices[status_unit].Update(
                    nValue=1 if online else 0,
                    sValue="On" if online else "Off"
                )

            hops_unit = self._node_unit(idx, OFF_HOPS)
            if hops_unit in Devices and path_len >= 0:
                Devices[hops_unit].Update(nValue=0, sValue=str(path_len))

            if effective_ts > 0:
                ls_unit = self._node_unit(idx, OFF_LASTSEEN)
                if ls_unit in Devices:
                    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(effective_ts))
                    Devices[ls_unit].Update(nValue=0, sValue=ts)

            la = self._node_last_activity.get(node_name, 0)

            # Store GPS location if the contact advertises valid coordinates
            adv_lat = contact.get("adv_lat", 0.0)
            adv_lon = contact.get("adv_lon", 0.0)
            if adv_lat and adv_lon and not (adv_lat == 0.0 and adv_lon == 0.0):
                self._node_locations[node_name] = {"lat": adv_lat, "lon": adv_lon}

            Domoticz.Log(
                f"Contact '{node_name}' type={contact.get('type',-1)}: "
                f"last_advert={int(now-last_advert)}s ago  "
                f"last_activity={int(now-la) if la else 'never'}  "
                f"path_len={path_len}  "
                f"online={online} (advert={advert_online} path={path_online})"
            )

        self._write_device_map()

    def _handle_message(self, msg: dict):
        """Handle an incoming message — update Inbox and per-node RSSI/SNR/LastSeen."""
        msg_type  = msg.get("type", "")
        text      = msg.get("text", "")

        # Resolve sender name
        prefix    = msg.get("pubkey_prefix", "")
        node_name = self._prefix_to_name.get(prefix, "").strip() if prefix else ""

        # For CHAN messages the sender name is embedded in the text as "Name: text"
        # and there is no pubkey — use text prefix up to the first ": " as hint
        if not node_name and msg_type in ("CHAN", "channel_message"):
            if ": " in text:
                node_name = text.split(": ", 1)[0].strip()
                text_body = text.split(": ", 1)[1].strip()
            else:
                text_body = text
        else:
            text_body = text

        display_name = node_name or prefix or "?"

        # Channel tag: C<idx> for channel messages, P for private
        channel_idx = msg.get("channel_idx")
        if msg_type in ("CHAN", "channel_message") and channel_idx is not None:
            chan_tag = f"C{channel_idx}"
        else:
            chan_tag = "P"

        # Update global inbox — format: [C0|sender] text  or  [P|sender] text
        if UNIT_INBOX in Devices:
            Devices[UNIT_INBOX].Update(nValue=0, sValue=f"[{chan_tag}|{display_name}] {text_body}")

        # Update per-node devices for any known contact
        if node_name:
            self._ensure_node_devices(node_name)
            idx = self._node_index(node_name)
            if idx >= 0:
                now_ts = int(time.time())
                # Record activity — used by _handle_contacts for online detection
                self._node_last_activity[node_name] = now_ts

                # A message means the node is clearly reachable → mark online
                status_unit = self._node_unit(idx, OFF_STATUS)
                if status_unit in Devices:
                    Devices[status_unit].Update(nValue=1, sValue="On")

                # Last Seen
                ls_unit = self._node_unit(idx, OFF_LASTSEEN)
                if ls_unit in Devices:
                    Devices[ls_unit].Update(nValue=0, sValue=time.strftime("%Y-%m-%d %H:%M:%S"))

                # SNR from message metadata; RSSI from status poll (req_status_sync)
                snr = msg.get("SNR") if msg.get("SNR") is not None else msg.get("snr")
                if snr is not None:
                    snr_unit = self._node_unit(idx, OFF_SNR)
                    if snr_unit in Devices:
                        Devices[snr_unit].Update(nValue=0, sValue=str(round(float(snr), 2)))

        self._write_device_map()

        # Increment message received counter
        self._recv_count += 1
        if UNIT_MSGS_RECV in Devices:
            Devices[UNIT_MSGS_RECV].Update(nValue=0, sValue=str(self._recv_count))

    def _handle_self_stats(self, stats: dict):
        """Update devices for the connected (self) node from polled stats."""
        if not self._self_name:
            return
        self._ensure_node_devices(self._self_name)
        idx = self._node_index(self._self_name)
        if idx < 0:
            return

        # Battery (millivolts) — from stats_core
        bat_mv = stats.get("battery_mv", 0)
        if bat_mv:
            pct   = _bat_pct(bat_mv)
            v     = round(bat_mv / 1000, 2)
            u_pct = self._node_unit(idx, OFF_BATT_PCT)
            u_v   = self._node_unit(idx, OFF_BATT_V)
            if u_pct in Devices:
                Devices[u_pct].Update(nValue=pct, sValue=str(pct))
            if u_v in Devices:
                Devices[u_v].Update(nValue=0, sValue=str(v))

        # Uptime (seconds → minutes)
        uptime_s = stats.get("uptime_secs", 0)
        if uptime_s:
            u = self._node_unit(idx, OFF_UPTIME)
            if u in Devices:
                Devices[u].Update(nValue=0, sValue=str(round(uptime_s / 60, 1)))

        # Radio stats
        noise = stats.get("noise_floor")
        if noise is not None:
            u = self._node_unit(idx, OFF_NOISE)
            if u in Devices:
                Devices[u].Update(nValue=0, sValue=str(noise))

        rssi = stats.get("last_rssi")
        if rssi is not None:
            u = self._node_unit(idx, OFF_RSSI)
            if u in Devices:
                Devices[u].Update(nValue=0, sValue=str(rssi))

        snr = stats.get("last_snr")
        if snr is not None:
            u = self._node_unit(idx, OFF_SNR)
            if u in Devices:
                Devices[u].Update(nValue=0, sValue=str(round(snr, 2)))

        # TX air seconds
        tx_air = stats.get("tx_air_secs")
        if tx_air is not None:
            u = self._node_unit(idx, OFF_AIRTIME)
            if u in Devices:
                Devices[u].Update(nValue=0, sValue=str(tx_air))

        # Packet counters
        pkt_sent = stats.get("sent")
        if pkt_sent is not None:
            u = self._node_unit(idx, OFF_MSGS_SENT)
            if u in Devices:
                Devices[u].Update(nValue=0, sValue=str(pkt_sent))

        pkt_recv = stats.get("recv")
        if pkt_recv is not None:
            u = self._node_unit(idx, OFF_MSGS_RECV)
            if u in Devices:
                Devices[u].Update(nValue=0, sValue=str(pkt_recv))

        # Last seen = now (we just got data from it)
        ls_unit = self._node_unit(idx, OFF_LASTSEEN)
        if ls_unit in Devices:
            Devices[ls_unit].Update(nValue=0, sValue=time.strftime("%Y-%m-%d %H:%M:%S"))

        Domoticz.Log(f"Self stats updated: bat={bat_mv}mV uptime={uptime_s}s rssi={rssi} snr={stats.get('last_snr')}")
        self._write_device_map()


# ── Domoticz plugin entry points ─────────────────────────────────────────────

_plugin = BasePlugin()

def onStart():            _plugin.onStart()
def onStop():             _plugin.onStop()
def onHeartbeat():        _plugin.onHeartbeat()
def onDeviceModified(u):  _plugin.onDeviceModified(u)
