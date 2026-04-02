"""
<plugin key="MeshCore" name="MeshCore" author="galadril" version="0.0.1" wikilink="" externallink="https://github.com/galadril/Domoticz-MeshCore-Plugin">
    <description>
        MeshCore LoRa mesh integration for Domoticz.
        Requires: pip install meshcore
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

# Poll intervals (seconds)
MSG_POLL_INTERVAL      = 30    # how often to drain message queue and refresh contacts
SELF_STATS_INTERVAL    = 300   # 5 min — poll connected node stats
CONNECTION_STALE_S     = 600   # 10 min — force reconnect if no push events received
LIVENESS_INTERVAL      = 300   # 5 min — send a full contacts refresh to verify the link
SESSION_MAX_S          = 1800  # 30 min — proactive reconnect to prevent ESP32 TCP stack exhaustion


def _bat_pct(mv: int) -> int:
    return max(0, min(100, int((mv - BAT_VMIN_MV) / (BAT_VMAX_MV - BAT_VMIN_MV) * 100)))


class BasePlugin:

    def __init__(self):
        self._queue          = queue.Queue()
        self._worker         = None
        self._loop           = None
        self._main_task      = None
        self._stop           = threading.Event()
        self.host            = ""
        self.port            = 5000
        self._contact_names  = []   # contact names discovered from mc.contacts (non-self)
        self.initialized     = False
        self._mc             = None
        self._self_name      = ""   # name of the connected node
        # pubkey_prefix (12 hex chars) → adv_name, rebuilt from contacts
        self._prefix_to_name = {}
        # node_name → last Unix timestamp we saw ANY activity from it
        # (message, advertisement push, status response); used as fallback
        # when last_advert is 0 or missing (common for repeater-type nodes)
        self._node_last_activity: dict = {}
        # node_name → {"lat": float, "lon": float} from contact adv_lat/adv_lon
        self._node_locations: dict = {}
        # Last sValue we already dispatched — prevents re-sending on every heartbeat
        self._last_sent_text = ""
        self._last_push_event = 0.0  # monotonic timestamp of last push event received
        # Message counters (reset when Domoticz restarts the plugin)
        self._recv_count = 0
        self._sent_count = 0

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

    def _force_close_connection(self, mc):
        """Forcefully close the raw TCP transport/socket of a MeshCore instance.

        This ensures the OS-level socket is torn down immediately so the
        remote device releases the connection and will accept a new one.
        """
        if mc is None:
            return
        try:
            transport = mc.connection_manager.connection.transport
            if transport:
                transport.close()
        except Exception:
            pass
        # Also close the underlying socket directly in case transport.close()
        # didn't fully release it (e.g. half-open / stuck state).
        try:
            transport = mc.connection_manager.connection.transport
            if transport:
                sock = transport.get_extra_info("socket")
                if sock:
                    import socket as _socket
                    try:
                        sock.shutdown(_socket.SHUT_RDWR)
                    except OSError:
                        pass
                    sock.close()
        except Exception:
            pass

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

        self._stop.clear()
        self._worker = threading.Thread(target=self._worker_loop, daemon=True, name="MeshCoreWorker")
        self._worker.start()

        self.initialized = True
        Domoticz.Heartbeat(10)
        Domoticz.Log(f"MeshCore plugin started – {self.host}:{self.port}")

    def onStop(self):
        self._stop.set()

        # Close the raw TCP socket so any blocking network await unblocks immediately.
        mc = self._mc
        self._force_close_connection(mc)
        self._mc = None

        loop = self._loop
        if loop and not loop.is_closed():
            def _force_stop():
                for t in asyncio.all_tasks(loop):
                    if not t.done():
                        t.cancel()
                loop.stop()
            loop.call_soon_threadsafe(_force_stop)

        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=5)

        self._remove_custom_page()
        Domoticz.Log("MeshCore plugin stopped.")

    def onHeartbeat(self):
        if not self.initialized:
            return
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            self._dispatch(item)

    def onDeviceModified(self, unit: int):
        if unit != UNIT_SEND:
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
        mc = self._mc
        if mc is None:
            Domoticz.Error("Cannot send — not connected to MeshCore.")
            return
        # Schedule the send on the worker event loop (non-blocking from main thread)
        asyncio.run_coroutine_threadsafe(self._send_message(mc, text), self._loop)

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

    # ── Async worker ──────────────────────────────────────────────────────────

    def _apply_keepalive(self, mc):
        """Enable TCP keepalive on the underlying socket to prevent NAT table expiry."""
        import socket as _socket
        try:
            transport = mc.connection_manager.connection.transport
            if not transport:
                return
            sock = transport.get_extra_info("socket")
            if not sock:
                return
            sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_KEEPALIVE, 1)
            # On Linux/Windows: first keepalive probe after 60 s idle,
            # then every 10 s, fail after 3 missed probes.
            if hasattr(_socket, "TCP_KEEPIDLE"):
                sock.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_KEEPIDLE, 60)
            if hasattr(_socket, "TCP_KEEPINTVL"):
                sock.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_KEEPINTVL, 10)
            if hasattr(_socket, "TCP_KEEPCNT"):
                sock.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_KEEPCNT, 3)
            Domoticz.Log("TCP keepalive enabled on MeshCore socket.")
        except Exception as exc:
            Domoticz.Debug(f"Could not set TCP keepalive: {exc}")

    def _worker_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._main_task = self._loop.create_task(self._async_worker())
            self._loop.run_forever()
        except Exception as exc:
            Domoticz.Error(f"Worker loop error: {exc}")
        finally:
            try:
                self._loop.close()
            except Exception:
                pass

    async def _stop_aware_sleep(self, seconds: float):
        deadline = time.monotonic() + seconds
        while not self._stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                await asyncio.sleep(min(remaining, 1.0))
            except asyncio.CancelledError:
                raise

    async def _async_worker(self):
        try:
            while not self._stop.is_set():
                mc = None
                try:
                    Domoticz.Log(f"Connecting to {self.host}:{self.port}…")
                    mc = await asyncio.wait_for(
                        MeshCore.create_tcp(self.host, self.port), timeout=15.0
                    )
                    if mc is None:
                        Domoticz.Error("create_tcp returned None — device did not respond to appstart.")
                        self._mc = None
                    else:
                        self._mc = mc
                        self._apply_keepalive(mc)
                        await asyncio.sleep(0)
                        await asyncio.sleep(0)
                        Domoticz.Log(f"Connected. self_info={mc.self_info}")
                        if mc.self_info:
                            self._self_name = mc.self_info.get("name", "")
                            self._queue.put(("self_info", dict(mc.self_info)))
                        await self._run_session(mc)
                except asyncio.CancelledError:
                    return
                except asyncio.TimeoutError:
                    Domoticz.Error(f"Connection to {self.host}:{self.port} timed out after 15s.")
                except Exception as exc:
                    Domoticz.Error(f"MeshCore worker error: {exc}\n{traceback.format_exc()}")
                finally:
                    # Forcefully tear down the old connection before reconnecting.
                    # This ensures the OS socket is released and the remote device
                    # will accept a new connection on the next attempt.
                    if mc is not None:
                        self._force_close_connection(mc)
                    self._mc = None

                if self._stop.is_set():
                    return
                Domoticz.Log("Reconnecting in 10s…")
                try:
                    await self._stop_aware_sleep(10)
                except asyncio.CancelledError:
                    return
        finally:
            self._mc = None

    async def _run_session(self, mc):
        try:
            Domoticz.Log("Session started — fetching contacts…")
            for attempt in range(15):
                if self._stop.is_set():
                    return
                try:
                    await asyncio.wait_for(mc.commands.get_contacts(), timeout=10.0)
                    Domoticz.Log("get_contacts succeeded.")
                except asyncio.TimeoutError:
                    Domoticz.Log("get_contacts timed out.")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    Domoticz.Error(f"get_contacts error: {exc}")
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                if mc.contacts:
                    names = [c.get("adv_name", "?") for c in mc.contacts.values()]
                    Domoticz.Log(f"Contacts loaded ({len(mc.contacts)}): {names}")
                    break
                Domoticz.Log(f"mc.contacts still empty after attempt {attempt + 1}, retrying…")
                await asyncio.sleep(1)

            if not mc.contacts:
                Domoticz.Error("No contacts after 15 attempts — check device and node names.")
                return

            # Log contact details
            for key, c in mc.contacts.items():
                age = int(time.time()) - c.get("last_advert", 0)
                Domoticz.Log(
                    f"  Contact: '{c.get('adv_name')}' type={c.get('type')} "
                    f"path_len={c.get('out_path_len')} advert={age}s ago"
                )

            # Subscribe to incoming messages (push events) FIRST so we don't
            # miss anything while doing channel fetch / login below.
            # NOTE: These callbacks run on the async worker thread.
            # Do NOT call Domoticz.Log/Debug here — those are not thread-safe and
            # will leave Python in a bad error state, causing Domoticz to crash via
            # HasNodeFailed() → PyErr_Occurred() on the web-server thread.
            # All logging must be deferred to the main thread via the queue.
            def _on_contact_msg(event):
                try:
                    self._last_push_event = time.monotonic()
                    self._queue.put(("message", event.payload))
                except Exception:
                    pass

            def _on_channel_msg(event):
                try:
                    self._last_push_event = time.monotonic()
                    self._queue.put(("message", event.payload))
                except Exception:
                    pass

            def _on_advertisement(event):
                try:
                    self._last_push_event = time.monotonic()
                    self._queue.put(("advertisement", event.payload))
                except Exception:
                    pass

            def _on_new_contact(event):
                try:
                    self._last_push_event = time.monotonic()
                    self._queue.put(("new_contact", event.payload))
                except Exception:
                    pass

            def _on_status_response(event):
                # Handles STATUS_RESPONSE push events (spontaneous or from req_status_sync).
                # Take a local snapshot of _prefix_to_name to avoid a race with the
                # main thread updating it in _handle_contacts.
                try:
                    self._last_push_event = time.monotonic()
                    prefix_map = dict(self._prefix_to_name)
                    prefix = event.payload.get("pubkey_pre", "")
                    name   = prefix_map.get(prefix, "")
                    if name:
                        self._queue.put(("contact_status", {"name": name, "data": event.payload}))
                except Exception:
                    pass

            mc.subscribe(EventType.CONTACT_MSG_RECV,  _on_contact_msg)
            mc.subscribe(EventType.CHANNEL_MSG_RECV,  _on_channel_msg)
            mc.subscribe(EventType.ADVERTISEMENT,     _on_advertisement)
            mc.subscribe(EventType.NEW_CONTACT,       _on_new_contact)
            mc.subscribe(EventType.STATUS_RESPONSE,   _on_status_response)

            # Fetch channel names (local command, no over-the-air wait)
            await self._fetch_channel_names(mc)

            t_self_stats         = -(SELF_STATS_INTERVAL + 1)   # force immediate poll
            t_liveness           = time.monotonic()
            t_session_start      = time.monotonic()
            consecutive_failures = 0
            self._last_push_event = time.monotonic()  # reset on fresh session

            while not self._stop.is_set():
                now = time.monotonic()

                # ── Proactive reconnect: ESP32 TCP stacks can't hold a connection
                #    open indefinitely; cycling every 30 min keeps the device stable.
                if now - t_session_start >= SESSION_MAX_S:
                    Domoticz.Log(
                        f"Session reached {SESSION_MAX_S}s — proactive reconnect to keep device healthy."
                    )
                    return

                # ── Staleness check: if no push events for CONNECTION_STALE_S
                #    and at least one contact is supposedly online, the link is
                #    likely dead even though the socket hasn't errored out.
                since_last_push = now - self._last_push_event
                if since_last_push >= CONNECTION_STALE_S:
                    Domoticz.Error(
                        f"No push events received for {int(since_last_push)}s "
                        f"— connection appears stale, reconnecting…"
                    )
                    return

                # ── Refresh contacts list (incremental)
                try:
                    await asyncio.wait_for(
                        mc.commands.get_contacts(lastmod=mc._lastmod), timeout=10.0
                    )
                    await asyncio.sleep(0)
                    await asyncio.sleep(0)
                    consecutive_failures = 0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    consecutive_failures += 1
                    Domoticz.Error(f"contacts refresh error ({consecutive_failures}/3): {exc}")
                    if consecutive_failures >= 3:
                        Domoticz.Error("Connection appears dead — reconnecting…")
                        return

                # ── Periodic liveness probe: do a full (non-incremental) contacts
                #    fetch to force a real round-trip over the wire.
                if now - t_liveness >= LIVENESS_INTERVAL:
                    t_liveness = now
                    try:
                        await asyncio.wait_for(
                            mc.commands.get_contacts(), timeout=10.0
                        )
                        await asyncio.sleep(0)
                        await asyncio.sleep(0)
                        Domoticz.Debug("Liveness probe OK.")
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        Domoticz.Error(f"Liveness probe failed: {exc} — reconnecting…")
                        return

                # Push contacts snapshot to main thread
                try:
                    contacts_snapshot = {k: dict(v) for k, v in mc.contacts.items()}
                except (TypeError, ValueError):
                    contacts_snapshot = {}
                if contacts_snapshot:
                    self._queue.put(("contacts", contacts_snapshot))

                # Poll self-node stats every SELF_STATS_INTERVAL
                if now - t_self_stats >= SELF_STATS_INTERVAL:
                    t_self_stats = now
                    await self._poll_self_stats(mc)

                try:
                    await self._stop_aware_sleep(MSG_POLL_INTERVAL)
                except asyncio.CancelledError:
                    return
        finally:
            try:
                await asyncio.wait_for(mc.disconnect(), timeout=3.0)
            except Exception:
                pass
            # Forcefully tear down the TCP socket regardless of whether
            # disconnect() succeeded — this prevents half-open sockets
            # from blocking future reconnection attempts.
            self._force_close_connection(mc)

    async def _poll_self_stats(self, mc):
        """Poll all available stats from the connected node itself."""
        Domoticz.Log("Polling self-node stats…")

        stats = {}

        try:
            r = await asyncio.wait_for(mc.commands.get_stats_core(), timeout=5.0)
            if r and r.type == EventType.STATS_CORE:
                stats.update(r.payload)
                Domoticz.Log(f"stats_core: {r.payload}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            Domoticz.Debug(f"get_stats_core error: {exc}")

        try:
            r = await asyncio.wait_for(mc.commands.get_stats_radio(), timeout=5.0)
            if r and r.type == EventType.STATS_RADIO:
                stats.update(r.payload)
                Domoticz.Log(f"stats_radio: {r.payload}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            Domoticz.Debug(f"get_stats_radio error: {exc}")

        try:
            r = await asyncio.wait_for(mc.commands.get_stats_packets(), timeout=5.0)
            if r and r.type == EventType.STATS_PACKETS:
                stats.update(r.payload)
                Domoticz.Log(f"stats_packets: {r.payload}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            Domoticz.Debug(f"get_stats_packets error: {exc}")

        if stats:
            self._queue.put(("self_stats", stats))

    async def _fetch_channel_names(self, mc):
        """Query channel names for indices 0–7 and write meshcore_channels.json."""
        channel_names = {}
        for idx in range(8):
            try:
                res = await asyncio.wait_for(mc.commands.get_channel(idx), timeout=5.0)
                Domoticz.Log(f"get_channel({idx}): type={res.type if res else None} payload={res.payload if res else None}")
                if res and res.type == EventType.CHANNEL_INFO:
                    name = res.payload.get("channel_name", "").strip("\x00").strip()
                    if name:
                        channel_names[str(idx)] = name
                elif res and res.type == EventType.ERROR:
                    Domoticz.Log(f"get_channel({idx}) error: {res.payload}")
                    break
            except asyncio.TimeoutError:
                Domoticz.Log(f"get_channel({idx}) timed out")
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                Domoticz.Log(f"get_channel({idx}) exception: {exc}")
                break
        Domoticz.Log(f"Channel names fetched: {channel_names}")
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
        elif kind == "contact_status":
            self._handle_contact_status(item[1])
        elif kind == "advertisement":
            Domoticz.Debug(f"ADVERTISEMENT: {item[1]}")
            self._handle_advertisement(item[1])
        elif kind == "new_contact":
            Domoticz.Debug(f"NEW_CONTACT: {item[1]}")
            self._handle_advertisement(item[1])
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

    def _handle_advertisement(self, payload: dict):
        """Handle ADVERTISEMENT / NEW_CONTACT push — update last-seen for any contact.

        Push ADVERTISEMENT events only carry public_key (no adv_name), so we
        look the name up via _prefix_to_name.  NEW_CONTACT events carry the
        full contact dict and do have adv_name.
        """
        adv_name = payload.get("adv_name", "").strip()
        if not adv_name:
            pk = payload.get("public_key", "")
            adv_name = self._prefix_to_name.get(pk[:12], "")
        if not adv_name or adv_name == self._self_name:
            return

        adv_ts = payload.get("adv_timestamp", 0) or payload.get("last_advert", 0)
        if adv_ts < 1_577_836_800:
            adv_ts = 0
        if not adv_ts:
            adv_ts = int(time.time())

        # Only update stored contacts — don't auto-register new ones from advertisements
        if adv_name not in self._contact_names:
            return

        self._ensure_node_devices(adv_name)
        idx = self._node_index(adv_name)
        if idx < 0:
            return

        self._node_last_activity[adv_name] = adv_ts
        online = (int(time.time()) - adv_ts) < ONLINE_THRESHOLD_S

        # Store GPS location from advertisement if available
        adv_lat = payload.get("adv_lat", 0.0)
        adv_lon = payload.get("adv_lon", 0.0)
        if adv_lat and adv_lon and not (adv_lat == 0.0 and adv_lon == 0.0):
            self._node_locations[adv_name] = {"lat": adv_lat, "lon": adv_lon}

        status_unit = self._node_unit(idx, OFF_STATUS)
        if status_unit in Devices:
            Devices[status_unit].Update(
                nValue=1 if online else 0,
                sValue="On" if online else "Off"
            )

        ls_unit = self._node_unit(idx, OFF_LASTSEEN)
        if ls_unit in Devices:
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(adv_ts))
            Devices[ls_unit].Update(nValue=0, sValue=ts)

        Domoticz.Log(f"Advertisement from '{adv_name}': online={online}")

    def _handle_contact_status(self, payload: dict):
        """Update remote node devices from a STATUS_RESPONSE (binary status poll or push)."""
        node_name = payload.get("name", "")
        data      = payload.get("data", {})
        if not node_name or not data:
            return

        self._ensure_node_devices(node_name)
        idx = self._node_index(node_name)
        if idx < 0:
            return

        def _upd(offset, value, fmt=str):
            u = self._node_unit(idx, offset)
            if u in Devices:
                Devices[u].Update(nValue=0, sValue=fmt(value))

        # Battery (mV)
        bat_mv = data.get("bat", 0)
        if bat_mv:
            pct = _bat_pct(bat_mv)
            u_pct = self._node_unit(idx, OFF_BATT_PCT)
            u_v   = self._node_unit(idx, OFF_BATT_V)
            if u_pct in Devices:
                Devices[u_pct].Update(nValue=pct, sValue=str(pct))
            if u_v in Devices:
                Devices[u_v].Update(nValue=0, sValue=str(round(bat_mv / 1000, 2)))

        # RSSI / SNR
        rssi = data.get("last_rssi")
        snr  = data.get("last_snr")
        if rssi is not None: _upd(OFF_RSSI, rssi)
        if snr  is not None: _upd(OFF_SNR,  round(float(snr), 2))

        # Rich stats (only present when node supports binary protocol)
        noise = data.get("noise_floor")
        if noise is not None: _upd(OFF_NOISE, noise)

        uptime = data.get("uptime")
        if uptime: _upd(OFF_UPTIME, round(uptime / 60, 1))

        airtime = data.get("airtime")
        if airtime: _upd(OFF_AIRTIME, airtime)

        nb_sent = data.get("nb_sent")
        nb_recv = data.get("nb_recv")
        if nb_sent is not None: _upd(OFF_MSGS_SENT, nb_sent)
        if nb_recv is not None: _upd(OFF_MSGS_RECV, nb_recv)

        # A response means the node is reachable → record activity and mark online
        self._node_last_activity[node_name] = int(time.time())
        u_status = self._node_unit(idx, OFF_STATUS)
        if u_status in Devices:
            Devices[u_status].Update(nValue=1, sValue="On")

        # Last Seen = now
        u_ls = self._node_unit(idx, OFF_LASTSEEN)
        if u_ls in Devices:
            Devices[u_ls].Update(nValue=0, sValue=time.strftime("%Y-%m-%d %H:%M:%S"))

        Domoticz.Log(f"Contact status updated for '{node_name}': "
                     f"bat={bat_mv}mV rssi={rssi} snr={snr} "
                     f"noise={noise} uptime={uptime}s")
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
