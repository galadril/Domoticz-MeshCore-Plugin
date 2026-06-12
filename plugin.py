"""
<plugin key="MeshCore" name="MeshCore" author="galadril, GizMoCuz" version="1.0.3" wikilink="" externallink="https://github.com/galadril/Domoticz-MeshCore-Plugin">
    <description>
        MeshCore LoRa mesh integration for Domoticz.
        Requires: pip install -r requirements.txt
    </description>
    <params>
        <param field="Mode1" label="Transport" width="120px">
            <options>
                <option label="TCP"    value="TCP" default="true"/>
                <option label="Serial" value="Serial"/>
            </options>
        </param>
        <param field="Address"    label="MeshCore Host" width="200px" default="192.168.1.50" visible_when="Mode1=TCP"/>
        <param field="Port"       label="MeshCore Port" width="80px"  default="5000"         visible_when="Mode1=TCP"/>
        <param field="SerialPort" label="Serial Port"   width="200px"                        visible_when="Mode1=Serial"/>
        <param field="Mode2"      label="Baud Rate"     width="100px"                        visible_when="Mode1=Serial">
            <options>
                <option label="115200" value="115200" default="true"/>
                <option label="57600"  value="57600"/>
                <option label="38400"  value="38400"/>
                <option label="19200"  value="19200"/>
                <option label="9600"   value="9600"/>
            </options>
        </param>
        <param field="Mode4"    label="Install Custom Dashboard" width="150px">
            <options>
                <option label="Yes" value="true" default="true"/>
                <option label="No"  value="false"/>
            </options>
        </param>
        <param field="Mode3" label="Command Bridge Channel" width="150px"/>
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

import DomoticzEx as Domoticz
import asyncio
import calendar
import collections
import copy
import functools
import gc
import json
import math
import os
import queue
import re
import shutil
import sqlite3
import threading
import time
import traceback
import urllib.error
import urllib.request

try:
    from meshcore import MeshCore
    from meshcore.events import EventType
    MESHCORE_AVAILABLE = True
except ImportError:
    MESHCORE_AVAILABLE = False

# ── Device scheme (DomoticzEx) ────────────────────────────────────────────────
# DomoticzEx keys devices by a string DeviceID; each DeviceID carries a Units
# dict. There is no 255-unit-per-plugin cap, so the old slot-block math is gone.
#
#   DeviceID = MESH_DID ("mesh")  → global devices, Unit = UNIT_* below
#   DeviceID = "self"             → the connected node, Unit = OFF_* below
#   DeviceID = <pubkey[:12]>      → a remote contact,  Unit = OFF_* below
#
# Units must be >= 1 in DomoticzEx, so OFF_* are 1-based.
MESH_DID = "mesh"
SELF_DID = "self"

UNIT_INBOX      = 1
UNIT_SEND       = 2   # Deprecated send device (superseded by WebSocket channel); kept for stale-cleanup only
UNIT_MSGS_RECV  = 3   # Custom counter: messages received today
UNIT_MSGS_SENT_ = 4   # Custom counter: messages sent today
UNIT_DZV_IN     = 5   # Text: inbound command payload (JSON, seq-stamped) for dzVents bridge
UNIT_DZV_REPLY  = 6   # Text: outbound reply payload written by dzVents
UNIT_DZV_SEND   = 7   # Switch (Push On): trigger written by dzVents to dispatch reply

# MeshCore firmware exposes up to 40 channel slots. Domoticz devices are NOT
# created per slot — they live entirely in the dashboard JSON map.
MAX_CHANNEL_SLOTS = 40

# Radio-tuning bounds (used by !set_radio / !set_tx_power validation).
# ISM bands cover 100-2500 MHz; MeshCore typically runs 433/868/915 MHz.
# Wide bandwidth range allows narrow-band experimentation.
RADIO_FREQ_MIN_MHZ = 100.0
RADIO_FREQ_MAX_MHZ = 2500.0
RADIO_BW_MIN_KHZ   = 5.0
RADIO_BW_MAX_KHZ   = 500.0
RADIO_SF_MIN       = 7
RADIO_SF_MAX       = 12
RADIO_CR_MIN       = 5   # firmware encoding: 5..8 = 4/5..4/8
RADIO_CR_MAX       = 8
# Default upper bound on TX power if the device hasn't reported max_tx_power.
RADIO_TX_POWER_DEFAULT_MAX_DBM = 22

# Per-node metric units (1-based — DomoticzEx requires Unit >= 1)
OFF_STATUS    = 1   # Switch:      online / offline
OFF_BATT_PCT  = 2   # Percentage:  battery %
OFF_BATT_V    = 3   # Custom (V):  battery voltage
OFF_RSSI      = 4   # Custom (dBm): last RSSI
OFF_SNR       = 5   # Custom (dB):  last SNR
OFF_NOISE     = 6   # Custom (dBm): noise floor
OFF_LASTSEEN  = 7   # Text:        timestamp of last received message/advert
OFF_TEMP      = 8   # Temperature: °C
OFF_HUMID     = 9   # Humidity:    %
OFF_HOPS      = 10  # Custom:      path length (hops)
OFF_UPTIME    = 11  # Custom (min): node uptime
OFF_AIRTIME   = 12  # Custom (%):  TX airtime utilization
OFF_MSGS_SENT = 13  # Custom:      total messages sent
OFF_MSGS_RECV = 14  # Custom:      total messages received
OFF_MSGS      = 15  # Text:        per-contact DM conversation history

# Cayenne LPP sensor type codes (used in self_telemetry LPP list entries)
LPP_TEMPERATURE = 103
LPP_HUMIDITY    = 104
LPP_VOLTAGE     = 116   # channel 1 = battery

# Battery voltage range for % calculation (mV)
BAT_VMIN_MV = 3000
BAT_VMAX_MV = 4200

# Node is considered online if last_advert is newer than this (8 h)
ONLINE_THRESHOLD_S = 28800

# Connection timeout for the initial connect (seconds)
CONNECT_TIMEOUT    = 12
COMMAND_TIMEOUT    = 10

# Reconnect delay after a connection failure / drop (seconds)
RECONNECT_DELAY_S  = 30

# Periodic refresh intervals on the persistent connection
STATS_REFRESH_S    = 300   # self-node stats (battery, radio, packets)
CONTACTS_REFRESH_S = 300   # contact list refresh (catches new contacts + path changes)
MSG_DRAIN_S        = 10    # periodic get_msg() drain — safety net for firmware
                           # that doesn't emit MESSAGES_WAITING / unsolicited
                           # push, so the node's message queue never piles up

# Rolling RX log buffer size (per-event detail kept in memory for the dashboard)
RX_LOG_BUFFER      = 250
# How often we re-write meshcore_rx_log.json at most (seconds)
RX_LOG_WRITE_S     = 2.0

# Seconds after a DM send_msg before we give up waiting for an ACK and
# annotate the sent line with "(no ack)".  The firmware's suggested_timeout
# is typically 20–60 s; 90 s gives slow multi-hop paths a generous margin.
DM_ACK_TIMEOUT_S   = 90

# After the user changes a setting, ignore device-side self_info echoes of
# manual_add_contacts/telemetry/adv_loc_policy for this many seconds. Some
# firmware briefly returns the prior value while flushing to flash, which
# would otherwise undo the user's change on the very next poll.
# Note: this only guards self_info-sourced settings. The default flood scope
# comes from a separate get_default_flood_scope() round-trip and the device
# returns the just-written value reliably there, so no grace needed for it.
SETTINGS_GRACE_S = 45

# MeshCore firmware encodes "no path / direct or unknown" as path_len=255 (0xFF).
# This is a sentinel value, NOT a real hop count.  Exclude it everywhere we
# record or display hop counts so it never appears in hops_records or UI.
HOPS_SENTINEL = 255

# Set to True to append a timestamped trace of the message send/receive
# round-trip to meshcore_debug.log in the plugin directory. Best-effort,
# never raises into callers, size-capped. Off in production.
MSG_FLOW_DEBUG = False
_DBG_PATH = None
_DBG_MAX_BYTES = 2 * 1024 * 1024


def _dbg(msg: str) -> None:
    """Append a timestamped line to meshcore_debug.log. Never raises."""
    if not MSG_FLOW_DEBUG:
        return
    try:
        global _DBG_PATH
        if _DBG_PATH is None:
            _DBG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "meshcore_debug.log")
        try:
            if os.path.getsize(_DBG_PATH) > _DBG_MAX_BYTES:
                os.replace(_DBG_PATH, _DBG_PATH + ".1")
        except OSError:
            pass
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        with open(_DBG_PATH, "a", encoding="utf-8") as fh:
            fh.write("[" + ts + "] " + str(msg) + "\n")
    except Exception:
        pass


def _bat_pct(mv: int) -> int:
    return max(0, min(100, int((mv - BAT_VMIN_MV) / (BAT_VMAX_MV - BAT_VMIN_MV) * 100)))


class BasePlugin:

    def __init__(self):
        self._queue          = queue.Queue()  # worker → main thread
        self.transport       = "TCP"          # "TCP" or "Serial"
        self.host            = ""
        self.port            = 5000
        self.serial_port     = ""
        self.baud_rate       = 115200
        # Tracks last successful-connection state to emit transition log lines
        self._was_connected  = False
        self._contact_names  = []   # contact names discovered from mc.contacts (non-self)
        self.initialized     = False
        self._self_name      = ""   # name of the connected node
        # pubkey_prefix (12 hex chars) → adv_name, rebuilt from contacts
        self._prefix_to_name = {}
        # node_name → last Unix timestamp we saw ANY activity from it
        self._node_last_activity: dict = {}
        # node_name → {"lat": float, "lon": float} from contact adv_lat/adv_lon
        self._node_locations: dict = {}
        # node_name → contact type int (1=Client/Contact, 2=Repeater, 3=Room Server, 4=Sensor)
        self._node_types: dict = {}
        # node_name → last_advert unix timestamp (for client-side sorting)
        self._node_last_advert: dict = {}
        # node_name → contact public_key hex (needed for remove_contact)
        self._node_pubkey: dict = {}
        # node_name → out_path hex string (e.g. "22a83b") or "" for flood.
        # Populated from the contacts list; pruned on contact removal.
        self._node_out_path: dict = {}
        # node_name → out_path_hash_mode (int, +1 offset matching the
        # dashboard convention used elsewhere for device_info path_hash_mode).
        self._node_out_path_hash_mode: dict = {}
        # node_name → DomoticzEx DeviceID (12-hex pubkey prefix). Populated
        # from contact pubkeys and from incoming message pubkey prefixes so a
        # device can be created/updated before the contacts poll runs.
        self._node_did: dict = {}
        # Current value of the connected node's manual_add_contacts setting
        # (True = node ignores adverts from unknown contacts; False = auto-add)
        self._manual_add_contacts: bool = False
        # Other device settings mirrored from self_info — used by the dashboard
        self._telemetry_mode_base: int = 0
        self._telemetry_mode_loc:  int = 0
        self._telemetry_mode_env:  int = 0
        self._advert_loc_policy:   int = 0
        # Monotonic time (seconds) of the last user-driven setting change.
        # Within SETTINGS_GRACE_S after a change we trust the just-set value
        # over what self_info reports — some firmware returns the previous
        # value briefly while flushing to flash.
        self._settings_set_at: float = 0.0
        # Default flood scope tag (e.g. "#nl"); empty string = global flood.
        # Read via mc.commands.get_default_flood_scope() once per poll cycle.
        self._default_flood_scope: str = ""
        # Favorite contact names — persisted to <plugin_dir>/meshcore_favorites.json.
        # Toggled from the dashboard; favorites sort first within online/offline groups.
        self._favorites: set = set()
        # Device info (firmware version, build, model) from send_device_query().
        # Fetched on connect and refreshed periodically.
        self._device_info: dict = {}
        # Full SELF_INFO snapshot — radio params (freq, bw, sf, cr, tx_power,
        # max_tx_power, multi_acks), our pubkey + advertised lat/lon. Exposed
        # via the device map so the dashboard's self-node side panel can show
        # and edit them.
        self._self_info_full: dict = {}
        # Latest result of get_self_telemetry (board-level sensors if any).
        self._self_telemetry: dict = {}
        # Per-contact query results from remote sync calls (status, telemetry,
        # neighbours). Keyed by contact adv_name. Exposed via device map so
        # the dashboard's contact info panel can read them.
        self._contact_query_results: dict = {}
        # Bumped from fast (2s) to steady (10s) once the first contacts batch
        # has been dispatched, so we keep onHeartbeat responsive without
        # firing every 2 seconds forever.
        self._heartbeat_restored: bool = False
        # Message counters (reset when Domoticz restarts the plugin)
        self._recv_count = 0
        self._sent_count = 0
        # Channel names already fetched flag (only need once)
        self._channels_fetched = False
        # Channel index→name map (populated from device), e.g. {0: "General", 1: "MyRoom"}
        # Non-empty entries only — used for message routing.
        self._channel_names: dict = {}
        # Full 8-slot table, including empty slots (idx → name). Exposed via
        # the device map so the dashboard can render every slot with controls.
        self._channel_slots: dict = {}
        # chan_hash (2-hex string, e.g. "a3") → channel_name for every configured
        # channel whose CHANNEL_INFO has been fetched.  Lets the dashboard resolve
        # "Hashes heard on air" rows to a readable name even when the raw RX_LOG
        # frames never carried chan_name (which only happens when the library can
        # HMAC-verify the ciphertext, which requires the channel secret).
        self._chan_hash_to_name: dict = {}
        # Persistent-connection worker state
        self._worker_thread: threading.Thread | None = None
        self._worker_loop: asyncio.AbstractEventLoop | None = None
        self._mc = None                       # live MeshCore instance (worker-owned)
        self._stop_event = threading.Event()  # set on shutdown (cross-thread)
        self._stop_async: asyncio.Event | None = None  # created inside worker loop
        self._main_task: asyncio.Task | None = None     # _run() task, for hard cancel on stop
        # Serialise concurrent `!verb` sends and remote queries. The meshcore
        # library subscribes to EventType.OK/ERROR globally per send() call,
        # so two commands in flight at the same time can have their responses
        # cross-attributed (the second waiter gets the first reply). One lock
        # → one in-flight command keeps the dispatcher unambiguous.
        self._cmd_lock: asyncio.Lock | None = None
        # Flag to prevent new connections during shutdown
        self._stopping = False
        # WebSocket channel state (F1+).
        # _ws_ok: None=unknown (first push will detect), True=available, False=absent.
        # _sub_feeds: last requested feed from {t:'sub'}; guarded by _rx_log_lock
        # (written on the main thread, will be read from the worker thread).
        self._ws_ok: bool | None = None
        # All fields below are touched from BOTH the worker thread (push
        # event callbacks) AND the main thread (_handle_message via
        # _dispatch, plus _write_rx_log). Always take self._rx_log_lock
        # before reading or mutating any of them.
        # Rolling RX_LOG_DATA buffer.
        self._rx_log = collections.deque(maxlen=RX_LOG_BUFFER)
        self._rx_log_lock = threading.Lock()
        # _sub_feeds is initialised here (after the lock) and is always
        # accessed under self._rx_log_lock to keep the main/worker access safe.
        self._sub_feeds: str = "none"
        self._rx_log_dirty = False
        self._rx_log_last_write = 0.0
        # F3 — rx-log on-demand + deltas.
        # _rx_log_seq: monotonic counter incremented on every rxlog/rxlog_delta push.
        # _rx_log_total_appended: absolute count of every entry ever appended to
        #   _rx_log (never decremented, even when the deque evicts old entries).
        # _rx_log_pushed_total: value of _rx_log_total_appended at the time of
        #   the last window/delta push.  The entries still in the buffer that
        #   the client has not yet seen are:
        #     start = _rx_log_total_appended - len(_rx_log)   (oldest still buffered)
        #     new   = list(_rx_log)[_rx_log_pushed_total - start:]
        #   If _rx_log_pushed_total < start the client missed evicted entries →
        #   fall back to a full window push.
        # All three are guarded by _rx_log_lock.
        self._rx_log_seq: int = 0
        self._rx_log_total_appended: int = 0
        self._rx_log_pushed_total: int = 0
        # F7 — device-map delta.
        # _device_seq: monotonic counter incremented on every devices/devices_delta push.
        # _last_pushed_device_map: snapshot of the deviceMap sent in the last full or
        #   delta push (None = no baseline; next push will be a full 'devices' message).
        # Both are guarded by _rx_log_lock (same lock reused — no new lock needed).
        self._device_seq: int = 0
        self._last_pushed_device_map: dict | None = None
        # Per-feed WebSocket push dirty flags (separate from the file-write
        # dirty flags so the two throttling paths don't interfere).
        # Set wherever the respective data changes; cleared by _push_dirty_feeds.
        self._ws_devices_dirty  = False
        self._ws_stats_dirty    = False
        self._ws_heard_dirty    = False
        self._ws_channels_dirty = False
        # Per-feed wall-clock timestamp of the last WebSocket push (monotonic).
        self._devices_last_push  = 0.0
        self._stats_last_push    = 0.0
        self._heard_last_push    = 0.0
        self._channels_last_push = 0.0
        # Aggregated stats over the rx-log window:
        self._payload_type_counts: dict = collections.defaultdict(int)
        self._chan_hash_counts:    dict = collections.defaultdict(int)
        # pubkey_prefix → list of {t, snr, rssi, path_len, kind}
        self._signal_history:      dict = collections.defaultdict(list)
        # raw_hex → list of {t, path, snr} (duplicate flood detection)
        self._dup_floods:          dict = collections.defaultdict(list)
        # 24h × 1h heatmap: hour-of-day → count for past 24h (timestamps trimmed on read)
        self._packet_times = collections.deque(maxlen=2000)  # raw ts list, trimmed to last 24h
        # Recent incoming-message signatures for de-duplication. The same
        # message can arrive twice: as an unsolicited push AND via the
        # get_msg() drain that start_auto_message_fetching() performs (and
        # duplicate-flood copies repeat with the same sender_timestamp/text
        # on a different path). 300 entries ≈ plenty at mesh message rates.
        self._recent_msg_sigs = collections.deque(maxlen=300)
        # Lifetime statistics (persisted to meshcore_stats.json, flushed on
        # the rx-log cadence + on stop, reloaded on start). Sender class is
        # derived from the contact type: Repeater(2) / Room Server(3) /
        # everything else (incl. unknown) = client. Mutations are guarded by
        # self._rx_log_lock (same lock as the heard/rx-log writers).
        self._stats = {
            "adverts_total":  0,
            "messages_total": 0,
            "client_total":   0,
            "repeater_total": 0,
            "server_total":   0,
            "msg_by_sender":  {},   # sender name -> message count
            "adv_by_sender":  {},   # advert name -> advert count
            "msg_by_channel": {},   # resolved channel name -> message count (known channels only)
            "hops_records":   [],   # top-5 [{hops,name,date,channel}], best per name
            "today":          {"date": "", "messages": 0,
                               "client": 0, "repeater": 0, "server": 0},
        }
        self._stats_dirty = False
        # Persistent "heard nodes" — adverts from nodes NOT in our contacts.
        # full pubkey hex → {pubkey, name, type, lat, lon, snr, rssi,
        #   path_len, first_heard, last_heard}. Survives restarts via
        # meshcore_heard.json (flushed on the rx-log cadence + on stop).
        # Updated from RX_LOG ADVERT frames on the worker thread under
        # _rx_log_lock; _known_pubkeys is swapped wholesale by _handle_contacts
        # so the worker can cheaply skip nodes that are already contacts.
        self._heard_nodes: dict = {}
        self._heard_dirty = False
        self._known_pubkeys: set = set()
        # Pubkeys that the user has explicitly deleted from the heard list.
        # Full pubkey hex strings.  Persisted in meshcore_heard.json under
        # "purged": [...] so purged nodes stay dead across restarts.
        # Once a purged key reappears as a real contact (i.e. it shows up in
        # _known_pubkeys via _handle_contacts) it is removed from this set so
        # a subsequent removal can add it back to heard normally.
        self._heard_purged: set = set()
        # Latest received signal for nodes that ARE contacts, keyed by the
        # 12-hex pubkey prefix → {snr, rssi, path_len, t, source}.
        # Last-writer-wins across ADVERT (worker, _on_rx_log) and incoming
        # messages with a known pubkey (main, _handle_message). Lets contact
        # cards show hops/SNR/RSSI even without a Domoticz device, a recent
        # message, or a direct path. RSSI is only set when the frame actually
        # carried one (adverts do; message events usually don't).
        self._contact_sig: dict = {}
        # Per-contact clock-skew sample, keyed by pubkey[:12]. Captured ONLY
        # when we actually receive a contact's ADVERT over the air, so it is
        # a trustworthy paired measurement: {"node_ts": <node's advertised
        # RTC>, "our_ts": <our local receive time of THAT advert>}. The
        # dashboard flags a wrong RTC from this pair (same approach as heard
        # nodes) instead of the old, false-positive-prone comparison of the
        # stale contact-list last_advert against an unrelated last_seen.
        self._contact_clock: dict = {}
        # Pending DM delivery-ACK records.
        # Keyed by expected_ack hex code (8 hex chars from MSG_SENT payload).
        # Value: {"target": str, "body": str, "out_ts": float,
        #         "inbox_line": str, "dm_name": str|None}
        # Written by the worker thread (_send_message), read and cleared by
        # _on_ack (worker) and by onHeartbeat timeout sweep (main).  Both
        # paths hold _rx_log_lock for the mutation — same discipline as
        # _chan_hash_to_name.  Dict is bounded: entries are removed on match
        # or timeout; at most one entry per in-flight send (send commands are
        # serialised by _cmd_lock), so size ≤ 1 in normal operation.
        self._pending_acks: dict = {}
        # SQLite message store — long-lived connection, opened in onStart.
        # All access serialized via _msgdb_lock (separate from _rx_log_lock).
        self._msgdb: sqlite3.Connection | None = None
        self._msgdb_lock = threading.Lock()
        # Monotonic insert counter used for pruning; reset on each onStart.
        self._msgdb_insert_count: int = 0
        # dzVents command bridge state.
        # _dzv_enabled: derived in onStart — True iff a non-empty Command Bridge
        #   Channel (Mode3) is configured; no separate toggle.
        # _dzv_channel: channel name to listen on (from Mode3); empty = disabled.
        # _cmd_origins: rid -> {kind, chan, ts} for pending channel replies.
        # _dzv_req_id: monotonic counter for correlation ids.
        # _dzv_in_seq: monotonic write counter so UNIT_DZV_IN always changes.
        self._dzv_enabled: bool = False
        self._dzv_channel: str = ""
        self._cmd_origins: dict = {}
        self._dzv_req_id: int = 0
        self._dzv_in_seq: int = 0
        # Time-series analytics state.
        # Previous packet counter values for delta computation in _ts_packets_add.
        self._ts_prev_pkt_recv:     int | None = None
        self._ts_prev_pkt_sent:     int | None = None
        self._ts_prev_pkt_flood_rx: int | None = None
        self._ts_prev_pkt_flood_tx: int | None = None
        self._ts_prev_pkt_dir_rx:   int | None = None
        self._ts_prev_pkt_dir_tx:   int | None = None
        # Set of panel names whose cached query results are stale after a new insert.
        self._ts_dirty_panels: set = set()

    # ── dzVents command bridge helpers ────────────────────────────────────────

    def _dzv_next_id(self) -> int:
        """Return the next correlation id, wrapping at 1_000_000."""
        self._dzv_req_id = (self._dzv_req_id + 1) % 1_000_000
        return self._dzv_req_id

    def _dzv_prune_origins(self):
        """Remove stale entries from _cmd_origins (age >300s; cap at 200)."""
        cutoff = time.time() - 300
        stale = [k for k, v in self._cmd_origins.items() if v.get("ts", 0) < cutoff]
        for k in stale:
            del self._cmd_origins[k]
        if len(self._cmd_origins) > 200:
            # Evict the oldest entries by timestamp.
            by_age = sorted(self._cmd_origins.items(), key=lambda kv: kv[1].get("ts", 0))
            for k, _ in by_age[: len(self._cmd_origins) - 200]:
                del self._cmd_origins[k]

    def _dzv_channel_match(self, chan_tag: str) -> bool:
        """Return True iff the bridge is enabled, a channel is configured, and
        chan_tag matches the configured channel after normalisation.

        Normalisation: strip a single leading '#', then .strip().lower() both
        sides.  So '#Alerts', 'alerts', and 'Alerts' all match a stored 'alerts'.
        """
        if not self._dzv_enabled or not self._dzv_channel:
            return False
        def _norm(s: str) -> str:
            return s.lstrip("#").strip().lower()
        return _norm(chan_tag) == _norm(self._dzv_channel)

    # ── SQLite message store ──────────────────────────────────────────────────

    # Max rows to keep in messages table (newest wins on prune).
    _MSG_STORE_CAP = 20_000
    # Prune at most once every N inserts (cheap amortised cost).
    _MSG_STORE_PRUNE_EVERY = 200
    # Current schema version stored in the preferences table.
    MSG_DB_SCHEMA_VERSION = 4

    # ── Elevation cache ───────────────────────────────────────────────────────

    # LRU cap: keep at most this many rows in elevation_cache.
    _ELEV_PRUNE_CAP = 100_000
    # Prune elevation cache at most once every this many seconds (5 min).
    _ELEV_PRUNE_INTERVAL = 300

    def _msg_store_open(self, db_path: str):
        """Open (or create) the SQLite message store at *db_path*.

        Creates the schema if it does not exist.  Must only be called once
        from onStart on the main thread before the worker starts.
        """
        try:
            con = sqlite3.connect(db_path, check_same_thread=False)
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    chan      TEXT    NOT NULL,
                    sender    TEXT    NOT NULL,
                    epoch     TEXT    NOT NULL,
                    bad       INTEGER NOT NULL DEFAULT 0,
                    body      TEXT    NOT NULL,
                    hops      INTEGER,
                    snr       REAL,
                    rssi      INTEGER,
                    path      TEXT,
                    ack       INTEGER,
                    direction TEXT    NOT NULL DEFAULT 'in',
                    recv_ts   TEXT    NOT NULL,
                    peer_key  TEXT
                )
            """)
            con.execute("CREATE INDEX IF NOT EXISTS idx_messages_chan_id ON messages(chan, id)")
            con.execute("""
                CREATE TABLE IF NOT EXISTS preferences (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            con.commit()
            self._msgdb = con
            self._msgdb_insert_count = 0
            self._msg_store_migrate()
            # Create indexes after migration so they are always present regardless
            # of which path (fresh DB vs. future migration) created the table.
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_peerkey"
                " ON messages(peer_key, id)"
            )
            con.commit()
            Domoticz.Debug("Message store opened: " + db_path)
        except Exception as exc:
            Domoticz.Error(f"Message store open failed (non-fatal): {exc!r}")
            self._msgdb = None

    def _pref_get(self, key: str, default=None):
        """Return the preferences value for *key*, or *default* if absent/error."""
        if self._msgdb is None:
            return default
        try:
            with self._msgdb_lock:
                cur = self._msgdb.execute(
                    "SELECT value FROM preferences WHERE key=?", (key,)
                )
                row = cur.fetchone()
            return row[0] if row is not None else default
        except Exception as exc:
            Domoticz.Error(f"Message store pref_get failed (non-fatal): {exc!r}")
            return default

    def _pref_set(self, key: str, value: str):
        """Upsert *key*=*value* in the preferences table. Never raises."""
        if self._msgdb is None:
            return
        try:
            with self._msgdb_lock:
                self._msgdb.execute(
                    "INSERT INTO preferences(key,value) VALUES(?,?)"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )
                self._msgdb.commit()
        except Exception as exc:
            Domoticz.Error(f"Message store pref_set failed (non-fatal): {exc!r}")

    def _msg_store_migrate(self):
        """Apply any pending schema migrations and record the resulting version.

        Migration ladder — add one ``elif ver == N:`` block per future version:

            ver == 1  — baseline; all columns (including peer_key) already in the
                        CREATE TABLE statement in _msg_store_open; nothing to ALTER.

        The loop is always exercised: even for a fresh DB it runs once (0→1),
        confirming the ladder structure is live code, not a dead stub.
        """
        try:
            stored = self._pref_get("db_version")
            ver = int(stored) if stored is not None else 0
            from_ver = ver

            while ver < self.MSG_DB_SCHEMA_VERSION:
                ver += 1
                if ver == 1:
                    pass  # baseline — tables already created in _msg_store_open
                elif ver == 2:
                    # Add peer_key column — may be missing on DBs created before
                    # the column was introduced (ALTER TABLE is a no-op if it
                    # already exists, so fresh installs are safe too).
                    try:
                        self._msgdb.execute(
                            "ALTER TABLE messages ADD COLUMN peer_key TEXT"
                        )
                        self._msgdb.commit()
                    except Exception:
                        pass  # column already present — safe to ignore
                elif ver == 3:
                    # Elevation sample cache for the LoS tool. Keyed by quantised
                    # (lat, lon) grid (≈11 m resolution). last_used drives LRU eviction.
                    cur = self._msgdb.cursor()
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS elevation_cache (
                            lat_q     INTEGER NOT NULL,
                            lon_q     INTEGER NOT NULL,
                            elev_m    REAL    NOT NULL,
                            last_used INTEGER NOT NULL,
                            PRIMARY KEY (lat_q, lon_q)
                        )
                    """)
                    cur.execute(
                        "CREATE INDEX IF NOT EXISTS ix_elev_last_used"
                        " ON elevation_cache (last_used)"
                    )
                    self._msgdb.commit()
                elif ver == 4:
                    # Time-series tables for historical analytics panels.
                    cur = self._msgdb.cursor()
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS ts_radio (
                            ts        INTEGER NOT NULL,
                            node_key  TEXT    NOT NULL,
                            rssi      INTEGER,
                            snr       REAL,
                            noise     INTEGER,
                            path_len  INTEGER,
                            src       TEXT
                        )
                    """)
                    cur.execute(
                        "CREATE INDEX IF NOT EXISTS ix_ts_radio_ts"
                        " ON ts_radio (ts)"
                    )
                    cur.execute(
                        "CREATE INDEX IF NOT EXISTS ix_ts_radio_node_ts"
                        " ON ts_radio (node_key, ts)"
                    )
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS ts_packets_hourly (
                            hour_ts   INTEGER PRIMARY KEY,
                            rx_count  INTEGER NOT NULL DEFAULT 0,
                            tx_count  INTEGER NOT NULL DEFAULT 0,
                            flood_rx  INTEGER NOT NULL DEFAULT 0,
                            flood_tx  INTEGER NOT NULL DEFAULT 0,
                            direct_rx INTEGER NOT NULL DEFAULT 0,
                            direct_tx INTEGER NOT NULL DEFAULT 0
                        )
                    """)
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS ts_packets_min (
                            ts        INTEGER PRIMARY KEY,
                            rx_count  INTEGER NOT NULL DEFAULT 0,
                            tx_count  INTEGER NOT NULL DEFAULT 0
                        )
                    """)
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS ts_relay_keys (
                            hex_key    TEXT PRIMARY KEY,
                            name       TEXT,
                            last_seen  INTEGER NOT NULL,
                            count      INTEGER NOT NULL DEFAULT 0
                        )
                    """)
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS ts_hops (
                            ts    INTEGER NOT NULL,
                            hops  INTEGER NOT NULL
                        )
                    """)
                    cur.execute(
                        "CREATE INDEX IF NOT EXISTS ix_ts_hops_ts"
                        " ON ts_hops (ts)"
                    )
                    self._msgdb.commit()

            self._pref_set("db_version", str(self.MSG_DB_SCHEMA_VERSION))

            if from_ver < self.MSG_DB_SCHEMA_VERSION:
                Domoticz.Debug(
                    f"Message store migrated schema v{from_ver} -> v{self.MSG_DB_SCHEMA_VERSION}"
                )
            else:
                Domoticz.Debug(
                    f"Message store schema v{self.MSG_DB_SCHEMA_VERSION} (up to date)"
                )
        except Exception as exc:
            Domoticz.Error(f"Message store migration failed (non-fatal): {exc!r}")

    @staticmethod
    def _norm_peer_key(pk) -> "str | None":
        """Normalise a pubkey prefix to a stable 12-char lowercase hex string.

        Rules:
        - Strip any non-hex characters ([^0-9a-fA-F]).
        - Lowercase.
        - Truncate to the first 12 characters.
        - Return None if the result is empty.
        """
        if not pk:
            return None
        cleaned = "".join(c for c in str(pk).lower() if c in "0123456789abcdef")
        return cleaned[:12] or None

    # ── Elevation cache helpers ───────────────────────────────────────────────

    @staticmethod
    def _elev_quantise(lat: float, lon: float) -> "tuple[int, int]":
        """Quantise (lat, lon) to an integer grid of ~11 m resolution.

        Multiplying by 1e4 gives approximately 11 m per step at the equator,
        which is finer than the 30 m SRTM source resolution and therefore
        lossless for caching purposes.

        The rounding deliberately uses ``math.floor(x + 0.5)`` rather than
        Python's built-in ``round()`` so that the result matches JavaScript's
        ``Math.round`` semantics (half-away-from-+inf for positives,
        half-toward-zero for negatives).  The frontend pre-rounds every
        coordinate via ``Math.round(v * 1e4) / 1e4`` before sending; using
        Python banker's rounding here would produce a 1 ULP mismatch at
        boundary values (e.g. lat 52.00005) and cause needless cache misses.
        """
        return (math.floor(lat * 1e4 + 0.5), math.floor(lon * 1e4 + 0.5))

    def _elevation_lookup(self, points: "list[tuple[float, float]]") -> "list":
        """Return elevation in metres (float) for each (lat, lon) in *points*.

        Results are returned in input order.  Any point whose elevation could
        not be fetched from either upstream source is represented as None
        (rare — should log a warning).

        Cache strategy:
        - Quantise all points and batch-SELECT from elevation_cache.
        - Update last_used for hits.
        - Fetch misses from open-elevation (batch ≤100), fall back to
          opentopodata on HTTP error.
        - INSERT OR REPLACE fetched samples.

        This is a *blocking* function — it does synchronous HTTP.  Call it via
        ``loop.run_in_executor(None, self._elevation_lookup, points)`` from the
        worker loop.
        """
        if not points:
            return []

        db = self._msgdb
        quantised = [self._elev_quantise(lat, lon) for lat, lon in points]
        n = len(quantised)
        results = [None] * n

        # ── Cache lookup ─────────────────────────────────────────────────────
        # Map (lat_q, lon_q) → index list (multiple input points may map to
        # the same quantised bucket after rounding).
        from collections import defaultdict
        bucket_to_idxs: "dict[tuple, list[int]]" = defaultdict(list)
        for i, q in enumerate(quantised):
            bucket_to_idxs[q].append(i)

        cached_elev: "dict[tuple, float]" = {}
        if db is not None:
            unique_qs = list(bucket_to_idxs.keys())
            # SQLite 999-param limit: chunk into batches of ≤499 pairs (2 params each).
            _CHUNK = 499
            for chunk_start in range(0, len(unique_qs), _CHUNK):
                chunk = unique_qs[chunk_start: chunk_start + _CHUNK]
                if not chunk:
                    continue
                # Build: WHERE (lat_q=? AND lon_q=?) OR (lat_q=? AND lon_q=?) ...
                where_parts = " OR ".join(["(lat_q=? AND lon_q=?)"] * len(chunk))
                params = []
                for lat_q, lon_q in chunk:
                    params.extend([lat_q, lon_q])
                try:
                    with self._msgdb_lock:
                        rows = self._msgdb.execute(
                            f"SELECT lat_q, lon_q, elev_m FROM elevation_cache"
                            f" WHERE {where_parts}",
                            params,
                        ).fetchall()
                        if rows:
                            now_ts = int(time.time())
                            upd_params = []
                            for lat_q, lon_q, elev_m in rows:
                                cached_elev[(lat_q, lon_q)] = elev_m
                                upd_params.extend([lat_q, lon_q])
                            upd_where = " OR ".join(
                                ["(lat_q=? AND lon_q=?)"] * len(rows)
                            )
                            self._msgdb.execute(
                                f"UPDATE elevation_cache SET last_used=?"
                                f" WHERE {upd_where}",
                                [now_ts] + upd_params,
                            )
                            self._msgdb.commit()
                except Exception as exc:
                    Domoticz.Error(
                        f"Elevation cache lookup failed (non-fatal): {exc!r}"
                    )

        # Fill hits from cache
        for q, idxs in bucket_to_idxs.items():
            if q in cached_elev:
                for i in idxs:
                    results[i] = cached_elev[q]

        # ── Fetch misses from upstream ────────────────────────────────────────
        miss_qs = [q for q in bucket_to_idxs if q not in cached_elev]
        if miss_qs:
            fetched: "dict[tuple, float]" = {}
            _BATCH = 100

            def _fetch_open_elevation(batch_qs):
                """POST to open-elevation; returns {(lat_q,lon_q): elev_m} or raises."""
                locations = [
                    {"latitude": lq / 1e4, "longitude": oq / 1e4}
                    for lq, oq in batch_qs
                ]
                body = json.dumps({"locations": locations}).encode()
                req = urllib.request.Request(
                    "https://api.open-elevation.com/api/v1/lookup",
                    data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    if resp.status != 200:
                        raise urllib.error.HTTPError(
                            req.full_url, resp.status, "non-200", {}, None
                        )
                    data = json.loads(resp.read())
                out = {}
                for i, r in enumerate(data.get("results", [])):
                    out[batch_qs[i]] = float(r["elevation"])
                return out

            def _fetch_opentopodata(batch_qs):
                """GET opentopodata; returns {(lat_q,lon_q): elev_m} or raises."""
                loc_str = "|".join(
                    f"{lq / 1e4},{oq / 1e4}" for lq, oq in batch_qs
                )
                url = (
                    f"https://api.opentopodata.org/v1/srtm30m"
                    f"?locations={urllib.request.quote(loc_str, safe=',|.')}"
                )
                req = urllib.request.Request(url, method="GET")
                with urllib.request.urlopen(req, timeout=15) as resp:
                    if resp.status != 200:
                        raise urllib.error.HTTPError(
                            req.full_url, resp.status, "non-200", {}, None
                        )
                    data = json.loads(resp.read())
                out = {}
                for i, r in enumerate(data.get("results", [])):
                    elev = r.get("elevation")
                    if elev is not None:
                        out[batch_qs[i]] = float(elev)
                return out

            for batch_start in range(0, len(miss_qs), _BATCH):
                batch = miss_qs[batch_start: batch_start + _BATCH]
                try:
                    batch_result = _fetch_open_elevation(batch)
                except Exception as exc:
                    Domoticz.Debug(
                        f"Elevation: open-elevation failed ({exc!r}), trying opentopodata"
                    )
                    try:
                        batch_result = _fetch_opentopodata(batch)
                    except Exception as exc2:
                        Domoticz.Error(
                            f"Elevation: both upstream services failed for batch"
                            f" of {len(batch)} point(s):"
                            f" open-elevation: {exc!r}; opentopodata: {exc2!r}"
                        )
                        batch_result = {}
                fetched.update(batch_result)

            # Persist fetched samples
            if fetched and db is not None:
                now_ts = int(time.time())
                try:
                    with self._msgdb_lock:
                        self._msgdb.executemany(
                            "INSERT OR REPLACE INTO elevation_cache"
                            " (lat_q, lon_q, elev_m, last_used) VALUES (?,?,?,?)",
                            [
                                (lat_q, lon_q, elev_m, now_ts)
                                for (lat_q, lon_q), elev_m in fetched.items()
                            ],
                        )
                        self._msgdb.commit()
                except Exception as exc:
                    Domoticz.Error(
                        f"Elevation cache write failed (non-fatal): {exc!r}"
                    )

            # Fill misses
            for q, idxs in bucket_to_idxs.items():
                if q not in cached_elev and q in fetched:
                    for i in idxs:
                        results[i] = fetched[q]

        return results

    def _elev_prune(self):
        """LRU-evict elevation_cache rows beyond _ELEV_PRUNE_CAP.

        Keeps the _ELEV_PRUNE_CAP most-recently-used rows; deletes the rest.
        Never raises into callers.
        """
        if self._msgdb is None:
            return
        try:
            with self._msgdb_lock:
                self._msgdb.execute(
                    "DELETE FROM elevation_cache"
                    " WHERE rowid NOT IN ("
                    "   SELECT rowid FROM elevation_cache"
                    "   ORDER BY last_used DESC"
                    "   LIMIT ?"
                    ")",
                    (self._ELEV_PRUNE_CAP,),
                )
                self._msgdb.commit()
        except Exception as exc:
            Domoticz.Error(f"Elevation cache prune failed (non-fatal): {exc!r}")

    # ── Time-series analytics helpers ─────────────────────────────────────────

    def _ts_ingest(self, src: str, *, node_key: str = None,
                   rssi=None, snr=None, noise=None, path_len=None):
        """Insert one radio sample into ts_radio.

        Inserts immediately — SQLite WAL mode handles concurrent writes at this
        frequency without batching.  Marks all radio-related analytics panels
        dirty so cached query results are invalidated.

        Never raises into callers.
        """
        if self._msgdb is None:
            return
        try:
            ts = int(time.time())
            nk = node_key or "unknown"
            rssi_v  = int(rssi)    if rssi  is not None else None
            snr_v   = float(snr)   if snr   is not None else None
            noise_v = int(noise)   if noise is not None else None
            pl_v    = int(path_len) if path_len is not None else None
            with self._msgdb_lock:
                self._msgdb.execute(
                    "INSERT INTO ts_radio (ts, node_key, rssi, snr, noise, path_len, src)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (ts, nk, rssi_v, snr_v, noise_v, pl_v, src),
                )
                self._msgdb.commit()
            self._ts_dirty_panels.update({"rssi", "snr", "noise"})
        except Exception as exc:
            Domoticz.Debug(f"_ts_ingest failed (non-fatal): {exc!r}")

    def _ts_packets_add(self, now_ts: int, drx: int, dtx: int,
                        flood_drx: int = 0, flood_dtx: int = 0,
                        direct_drx: int = 0, direct_dtx: int = 0):
        """Upsert the current-minute row in ts_packets_min and the current-hour
        row in ts_packets_hourly in a single transaction.

        All delta arguments should be non-negative counters.  Wrap-around-safe
        deltas are computed by the caller (_handle_self_stats).
        Never raises into callers.
        """
        if self._msgdb is None:
            return
        try:
            min_ts  = now_ts - (now_ts % 60)
            hour_ts = now_ts - (now_ts % 3600)
            with self._msgdb_lock:
                self._msgdb.execute(
                    "INSERT INTO ts_packets_min (ts, rx_count, tx_count)"
                    " VALUES (?,?,?)"
                    " ON CONFLICT(ts) DO UPDATE SET"
                    "   rx_count = rx_count + excluded.rx_count,"
                    "   tx_count = tx_count + excluded.tx_count",
                    (min_ts, max(0, drx), max(0, dtx)),
                )
                self._msgdb.execute(
                    "INSERT INTO ts_packets_hourly"
                    " (hour_ts, rx_count, tx_count, flood_rx, flood_tx, direct_rx, direct_tx)"
                    " VALUES (?,?,?,?,?,?,?)"
                    " ON CONFLICT(hour_ts) DO UPDATE SET"
                    "   rx_count  = rx_count  + excluded.rx_count,"
                    "   tx_count  = tx_count  + excluded.tx_count,"
                    "   flood_rx  = flood_rx  + excluded.flood_rx,"
                    "   flood_tx  = flood_tx  + excluded.flood_tx,"
                    "   direct_rx = direct_rx + excluded.direct_rx,"
                    "   direct_tx = direct_tx + excluded.direct_tx",
                    (hour_ts,
                     max(0, drx), max(0, dtx),
                     max(0, flood_drx), max(0, flood_dtx),
                     max(0, direct_drx), max(0, direct_dtx)),
                )
                self._msgdb.commit()
            self._ts_dirty_panels.update({"packets", "hourly"})
        except Exception as exc:
            Domoticz.Debug(f"_ts_packets_add failed (non-fatal): {exc!r}")

    def _path_token_chars(self) -> int:
        """Hex-char width of one path-hash hop token (2/4/6).

        Derived from the node's reported device_info path_hash_mode (stored
        1-based: 1/2/3 = byte width); falls back to 1-byte when the node has
        not reported yet. Clamped to the firmware's valid 1–3 range.
        """
        try:
            mode = int((self._device_info or {}).get("path_hash_mode") or 1)
        except (TypeError, ValueError):
            mode = 1
        return 2 * min(3, max(1, mode))

    def _resolve_relay_key(self, hex_token: str) -> "str | None":
        """Return the contact name whose pubkey prefix starts with hex_token.

        hex_token is one path-hash hop token (2/4/6 hex chars depending on
        the mesh's path_hash_mode).  Returns None when no match is found or
        when two contacts share the same prefix (ambiguous).
        """
        want = hex_token.lower()
        match = None
        for prefix, name in list(self._prefix_to_name.items()):
            if prefix.startswith(want):
                if match is not None:
                    return None  # ambiguous — two contacts collide on this token
                match = name
        return match

    def _ts_relay_observed(self, hex_key: str, name: str = None):
        """Bump count and update last_seen for one hex relay token
        (2/4/6 chars = 1/2/3-byte path hash).

        Updates name only if the provided name is non-empty and different from
        the stored one.  Never raises into callers.
        """
        if self._msgdb is None:
            return
        if not hex_key or len(hex_key) not in (2, 4, 6):
            return
        try:
            ts = int(time.time())
            with self._msgdb_lock:
                self._msgdb.execute(
                    "INSERT INTO ts_relay_keys (hex_key, name, last_seen, count)"
                    " VALUES (?,?,?,1)"
                    " ON CONFLICT(hex_key) DO UPDATE SET"
                    "   last_seen = excluded.last_seen,"
                    "   count = count + 1,"
                    "   name = CASE"
                    "     WHEN COALESCE(excluded.name, '') != ''"
                    "      AND COALESCE(excluded.name, '') != COALESCE(ts_relay_keys.name, '')"
                    "     THEN excluded.name"
                    "     ELSE ts_relay_keys.name"
                    "   END",
                    (hex_key.lower(), name or None, ts),
                )
                self._msgdb.commit()
            self._ts_dirty_panels.add("relays")
        except Exception as exc:
            Domoticz.Debug(f"_ts_relay_observed failed (non-fatal): {exc!r}")

    def _ts_hops_record(self, ts: int, hops):
        """Append one row to ts_hops.

        Skips if hops is None, negative, or equals HOPS_SENTINEL.
        Never raises into callers.
        """
        if self._msgdb is None:
            return
        if hops is None:
            return
        try:
            hops_i = int(hops)
        except (TypeError, ValueError):
            return
        if hops_i < 0 or hops_i == HOPS_SENTINEL:
            return
        try:
            with self._msgdb_lock:
                self._msgdb.execute(
                    "INSERT INTO ts_hops (ts, hops) VALUES (?,?)",
                    (int(ts), hops_i),
                )
                self._msgdb.commit()
            self._ts_dirty_panels.add("hops")
        except Exception as exc:
            Domoticz.Debug(f"_ts_hops_record failed (non-fatal): {exc!r}")

    def _ts_prune(self):
        """Delete rows older than each table's retention window.

        Schedules:
          ts_radio:         14 days
          ts_packets_min:   48 hours
          ts_hops:          14 days
          ts_relay_keys:    30 days (only rows with count < 100)
          ts_packets_hourly: kept indefinitely (small table)
        Never raises into callers.
        """
        if self._msgdb is None:
            return
        try:
            now = int(time.time())
            cutoffs = {
                "ts_radio":      now - 14 * 86400,
                "ts_packets_min": now - 48 * 3600,
                "ts_hops":       now - 14 * 86400,
            }
            with self._msgdb_lock:
                for tbl, cutoff in cutoffs.items():
                    self._msgdb.execute(
                        f"DELETE FROM {tbl} WHERE ts < ?", (cutoff,)
                    )
                relay_cutoff = now - 30 * 86400
                self._msgdb.execute(
                    "DELETE FROM ts_relay_keys"
                    " WHERE last_seen < ? AND count < 100",
                    (relay_cutoff,),
                )
                self._msgdb.commit()
        except Exception as exc:
            Domoticz.Debug(f"_ts_prune failed (non-fatal): {exc!r}")

    # ── Analytics query helpers ───────────────────────────────────────────────

    # Default bucket sizes (seconds) keyed by range upper bound (seconds).
    # Chosen so charts have ~100–300 data points.
    _TS_BUCKET_TABLE = [
        (3  * 3600,  60),        # <= 3 h  -> 1 min buckets
        (6  * 3600,  120),       # <= 6 h  -> 2 min buckets
        (12 * 3600,  300),       # <= 12 h -> 5 min buckets
        (24 * 3600,  600),       # <= 24 h -> 10 min buckets
        (48 * 3600,  1200),      # <= 48 h -> 20 min buckets
        (7  * 86400, 3600),      # <= 7 d  -> 1 h buckets
    ]
    # Hard cap on the analytics range accepted from the dashboard.
    _TS_MAX_RANGE_S = 30 * 86400  # 30 days

    @staticmethod
    def _ts_default_bucket(range_s: int) -> int:
        """Return a sensible default bucket size for the given time range."""
        for upper, bucket in BasePlugin._TS_BUCKET_TABLE:
            if range_s <= upper:
                return bucket
        return 3600  # > 7 d: use 1 h

    def _q_rssi_snr(self, panel: str, t_from: int, t_to: int,
                    bucket_s: int, nodes_tuple: tuple) -> dict:
        """Return per-node time-series for the rssi or snr panel.

        Shape:
          {
            "series": [
              {"node": <key>, "data": [[<bucket_ts>, <avg_value>], ...]},
              ...
            ]
          }
        Only returns nodes whose series is non-empty within the range.
        Filtered to nodes_tuple when non-empty.
        """
        col = "rssi" if panel == "rssi" else "snr"
        if self._msgdb is None:
            return {"series": []}
        try:
            where_nodes = ""
            if nodes_tuple:
                placeholders = ",".join("?" * len(nodes_tuple))
                where_nodes = f" AND node_key IN ({placeholders})"
            sql = (
                f"SELECT node_key,"
                f" (ts / ? * ?) AS bucket,"
                f" AVG({col}) AS val"
                f" FROM ts_radio"
                f" WHERE ts >= ? AND ts < ? AND {col} IS NOT NULL"
                f"{where_nodes}"
                f" GROUP BY node_key, bucket"
                f" ORDER BY node_key, bucket"
            )
            params_ordered = [bucket_s, bucket_s, t_from, t_to] + (list(nodes_tuple) if nodes_tuple else [])
            with self._msgdb_lock:
                rows = self._msgdb.execute(sql, params_ordered).fetchall()
            by_node: dict = {}
            for node_key, bucket, val in rows:
                by_node.setdefault(node_key, []).append([bucket, round(val, 2) if val is not None else None])
            series = [{"node": k, "data": v} for k, v in sorted(by_node.items())]
            # Build a node_key → display_name map so the frontend can resolve
            # historic nodes that may no longer be in the active contact list.
            names: dict = {}
            if self._self_name:
                names["self"] = self._self_name
            for nk in by_node:
                if nk in ("self", "unknown"):
                    continue
                resolved = self._prefix_to_name.get(nk)
                if not resolved:
                    for pk, h in list(self._heard_nodes.items()):
                        if pk[:12] == nk and h.get("name"):
                            resolved = h["name"]
                            break
                if resolved:
                    names[nk] = resolved
            return {"series": series, "names": names}
        except Exception as exc:
            Domoticz.Debug(f"_q_rssi_snr({panel}) failed (non-fatal): {exc!r}")
            return {"series": [], "names": {}}

    def _q_noise(self, panel: str, t_from: int, t_to: int,
                 bucket_s: int, nodes_tuple: tuple) -> dict:
        """Return self-node noise-floor time-series.

        Shape:
          {
            "series": [
              {"node": "self", "data": [[<bucket_ts>, <avg_noise>], ...]},
            ]
          }
        """
        if self._msgdb is None:
            return {"series": []}
        try:
            sql = (
                "SELECT (ts / ? * ?) AS bucket, AVG(noise) AS val"
                " FROM ts_radio"
                " WHERE ts >= ? AND ts < ? AND noise IS NOT NULL"
                " GROUP BY bucket"
                " ORDER BY bucket"
            )
            with self._msgdb_lock:
                rows = self._msgdb.execute(sql, (bucket_s, bucket_s, t_from, t_to)).fetchall()
            data = [[bucket, round(val, 2) if val is not None else None] for bucket, val in rows]
            return {"series": [{"node": "self", "data": data}] if data else []}
        except Exception as exc:
            Domoticz.Debug(f"_q_noise failed (non-fatal): {exc!r}")
            return {"series": []}

    def _q_packets(self, panel: str, t_from: int, t_to: int,
                   bucket_s: int, nodes_tuple: tuple) -> dict:
        """Return rx+tx packet volume time-series (per-minute table, bucketed).

        Shape:
          {
            "series": [
              {"name": "rx", "data": [[<bucket_ts>, <sum>], ...]},
              {"name": "tx", "data": [[<bucket_ts>, <sum>], ...]},
            ]
          }
        """
        if self._msgdb is None:
            return {"series": []}
        try:
            sql = (
                "SELECT (ts / ? * ?) AS bucket,"
                " SUM(rx_count) AS rx, SUM(tx_count) AS tx"
                " FROM ts_packets_min"
                " WHERE ts >= ? AND ts < ?"
                " GROUP BY bucket"
                " ORDER BY bucket"
            )
            with self._msgdb_lock:
                rows = self._msgdb.execute(sql, (bucket_s, bucket_s, t_from, t_to)).fetchall()
            rx_data = [[b, r] for b, r, _ in rows]
            tx_data = [[b, t] for b, _, t in rows]
            return {"series": [{"name": "rx", "data": rx_data},
                                {"name": "tx", "data": tx_data}]}
        except Exception as exc:
            Domoticz.Debug(f"_q_packets failed (non-fatal): {exc!r}")
            return {"series": []}

    def _q_packets_hourly(self, panel: str, t_from: int, t_to: int,
                          bucket_s: int, nodes_tuple: tuple) -> dict:
        """Return all hourly packet counter rows in time order.

        Shape:
          {
            "rows": [
              {"ts": <hour_ts>, "rx": <int>, "tx": <int>,
               "flood_rx": <int>, "flood_tx": <int>,
               "direct_rx": <int>, "direct_tx": <int>},
              ...
            ]
          }
        """
        if self._msgdb is None:
            return {"rows": []}
        try:
            sql = (
                "SELECT hour_ts, rx_count, tx_count,"
                " flood_rx, flood_tx, direct_rx, direct_tx"
                " FROM ts_packets_hourly"
                " WHERE hour_ts >= ? AND hour_ts < ?"
                " ORDER BY hour_ts"
            )
            with self._msgdb_lock:
                rows = self._msgdb.execute(sql, (t_from, t_to)).fetchall()
            result = [
                {"ts": ts, "rx": rx, "tx": tx,
                 "flood_rx": frx, "flood_tx": ftx,
                 "direct_rx": drx, "direct_tx": dtx}
                for ts, rx, tx, frx, ftx, drx, dtx in rows
            ]
            return {"rows": result}
        except Exception as exc:
            Domoticz.Debug(f"_q_packets_hourly failed (non-fatal): {exc!r}")
            return {"rows": []}

    def _q_msg_per_channel(self, panel: str, t_from: int, t_to: int,
                           bucket_s: int, nodes_tuple: tuple) -> dict:
        """Return message count per channel from the messages table.

        Excludes chan='P' (private/DM messages) so only channel traffic is
        counted.  Uses epoch TEXT column with strftime comparison.

        Shape:
          {
            "rows": [{"chan": <str>, "count": <int>}, ...]
          }
        sorted by count descending.
        """
        if self._msgdb is None:
            return {"rows": []}
        try:
            t_from_str = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t_from))
            t_to_str   = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t_to))
            sql = (
                "SELECT chan, COUNT(*) AS cnt"
                " FROM messages"
                " WHERE chan != 'P'"
                " AND epoch >= ? AND epoch < ?"
                " GROUP BY chan"
                " ORDER BY cnt DESC"
            )
            with self._msgdb_lock:
                rows = self._msgdb.execute(sql, (t_from_str, t_to_str)).fetchall()
            return {"rows": [{"chan": chan, "count": cnt} for chan, cnt in rows]}
        except Exception as exc:
            Domoticz.Debug(f"_q_msg_per_channel failed (non-fatal): {exc!r}")
            return {"rows": []}

    def _q_hop_histogram(self, panel: str, t_from: int, t_to: int,
                         bucket_s: int, nodes_tuple: tuple) -> dict:
        """Return hop-count histogram within the time range.

        Shape:
          {
            "rows": [{"hops": <int>, "count": <int>}, ...]
          }
        sorted by hops ascending.
        """
        if self._msgdb is None:
            return {"rows": []}
        try:
            sql = (
                "SELECT hops, COUNT(*) AS cnt"
                " FROM ts_hops"
                " WHERE ts >= ? AND ts < ?"
                " GROUP BY hops"
                " ORDER BY hops"
            )
            with self._msgdb_lock:
                rows = self._msgdb.execute(sql, (t_from, t_to)).fetchall()
            return {"rows": [{"hops": h, "count": c} for h, c in rows]}
        except Exception as exc:
            Domoticz.Debug(f"_q_hop_histogram failed (non-fatal): {exc!r}")
            return {"rows": []}

    def _q_top_relays(self, panel: str, t_from: int, t_to: int,
                      bucket_s: int, nodes_tuple: tuple) -> dict:
        """Return relay-key tallies ordered by count descending.

        nodes_tuple[0] is treated as a limit (int) when provided; defaults to 20.
        Only keys matching the current path_hash_mode token width are returned,
        so rows tallied under a previous hash-size setting don't pollute the
        table after a mode switch (they stay in the DB and age out via the
        normal retention pass).

        Shape:
          {
            "rows": [{"hex": <str>, "name": <str|null>, "count": <int>,
                      "last_seen": <int>}, ...]
          }
        """
        if self._msgdb is None:
            return {"rows": []}
        try:
            limit = int(nodes_tuple[0]) if nodes_tuple else 20
            sql = (
                "SELECT hex_key, name, count, last_seen"
                " FROM ts_relay_keys"
                " WHERE length(hex_key) = ?"
                " ORDER BY count DESC"
                " LIMIT ?"
            )
            with self._msgdb_lock:
                rows = self._msgdb.execute(sql, (self._path_token_chars(), limit)).fetchall()
            return {"rows": [{"hex": h, "name": n, "count": c, "last_seen": ls}
                              for h, n, c, ls in rows]}
        except Exception as exc:
            Domoticz.Debug(f"_q_top_relays failed (non-fatal): {exc!r}")
            return {"rows": []}

    # Dispatch table for analytics panels.
    _ANALYTICS_PANELS = {
        "rssi":     "_q_rssi_snr",
        "snr":      "_q_rssi_snr",
        "noise":    "_q_noise",
        "packets":  "_q_packets",
        "channels": "_q_msg_per_channel",
        "hourly":   "_q_packets_hourly",
        "relays":   "_q_top_relays",
        "hops":     "_q_hop_histogram",
    }

    def _handle_analytics_query(self, panel: str, t_from, t_to,
                                 bucket_s=None,
                                 nodes: "list | None" = None) -> dict:
        """Dispatch an analytics query and return a result dict for the dashboard.

        Validation:
        - panel must be a known key in _ANALYTICS_PANELS;
        - t_from and t_to must be finite numerics;
        - t_to - t_from in (0, _TS_MAX_RANGE_S] (30 days);
        - bucket_s, if provided, must be a positive integer ≤ 90 days.

        Cache:
        - Results are NOT memoized across calls; each call re-queries SQLite.
          The cheap dirty-flag set (_ts_dirty_panels) is used by ingestion to
          signal "fresh data landed since the last query" so callers may
          decide to refetch — it is not an LRU cache. Add @functools.lru_cache
          here if profiling shows repeat-query overhead.

        Returns:
        - {ok: True, panel, t_from, t_to, bucket_s, ...payload}, or
        - {ok: False, error: <str>} on validation or query failure.
        """
        if panel not in self._ANALYTICS_PANELS:
            return {"ok": False, "error": "unknown panel"}
        if not isinstance(t_from, (int, float)) or not isinstance(t_to, (int, float)):
            return {"ok": False, "error": "timestamps must be numeric"}
        if math.isnan(t_from) or math.isnan(t_to) or math.isinf(t_from) or math.isinf(t_to):
            return {"ok": False, "error": "timestamps cannot be NaN or Infinity"}
        t_from = int(t_from); t_to = int(t_to)
        range_s = t_to - t_from
        if range_s <= 0 or range_s > self._TS_MAX_RANGE_S:
            return {"ok": False, "error": "range out of bounds (max 30 days)"}
        if bucket_s is not None:
            if not isinstance(bucket_s, int) or isinstance(bucket_s, bool) or bucket_s <= 0 or bucket_s > 86400 * 90:
                return {"ok": False, "error": "bucket_s must be a positive integer ≤ 90 days"}
        else:
            bucket_s = self._ts_default_bucket(range_s)
        nodes_tuple = tuple(nodes) if nodes else ()
        fn_name = self._ANALYTICS_PANELS[panel]
        fn = getattr(self, fn_name)
        try:
            payload = fn(panel, t_from, t_to, bucket_s, nodes_tuple)
        except Exception as exc:
            Domoticz.Debug(f"_handle_analytics_query({panel}) failed: {exc!r}")
            return {"ok": False, "error": str(exc)}
        self._ts_dirty_panels.discard(panel)
        result = {"ok": True, "panel": panel,
                  "t_from": t_from, "t_to": t_to, "bucket_s": bucket_s}
        result.update(payload)
        return result

    def _msg_store_add(self, chan: str, sender: str, body: str, epoch: int,
                       bad: bool = False, snr=None, hops=None, rssi=None,
                       path: str = None, ack=None,
                       direction: str = "in",
                       peer_key: str = None) -> "int | None":
        """Insert one message row and return its rowid (or None on error).

        Never raises — any sqlite error is caught, logged, and None is returned
        so the caller's live message path is unaffected.
        """
        if self._msgdb is None:
            return None
        # Timestamps stored as UTC datetime TEXT ('%Y-%m-%d %H:%M:%S') so the DB
        # is human-readable when opened directly.  Callers pass epoch as int seconds.
        try:
            epoch_text = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(int(epoch)))
        except (TypeError, ValueError, OSError):
            epoch_text = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())
        recv_ts_text = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())
        bad_int = 1 if bad else 0
        norm_pk = self._norm_peer_key(peer_key)
        try:
            with self._msgdb_lock:
                cur = self._msgdb.execute(
                    "INSERT INTO messages"
                    " (chan, sender, epoch, bad, body, hops, snr, rssi, path, ack, direction, recv_ts, peer_key)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (chan, sender, epoch_text, bad_int, body,
                     int(hops) if (isinstance(hops, int) and 0 <= hops < HOPS_SENTINEL) else None,
                     float(snr) if snr is not None else None,
                     int(rssi) if rssi is not None else None,
                     path or None, ack, direction, recv_ts_text, norm_pk),
                )
                rowid = cur.lastrowid
                self._msgdb.commit()
                self._msgdb_insert_count += 1
                # Prune periodically to cap table size. The DELETE keeps the
                # newest _MSG_STORE_CAP rows; running it under the same lock as
                # the insert means a reader never sees a half-pruned table.
                if self._msgdb_insert_count % self._MSG_STORE_PRUNE_EVERY == 0:
                    self._msgdb.execute(
                        "DELETE FROM messages WHERE id < "
                        "(SELECT MAX(id) - ? FROM messages)",
                        (self._MSG_STORE_CAP,),
                    )
                    self._msgdb.commit()
            return rowid
        except Exception as exc:
            Domoticz.Error(f"Message store insert failed (non-fatal): {exc!r}")
            return None

    def _msg_store_set_ack(self, rowid: int, delivered: bool):
        """Update the ack column for a row by id. Silently no-ops on error."""
        if self._msgdb is None or rowid is None:
            return
        try:
            with self._msgdb_lock:
                self._msgdb.execute(
                    "UPDATE messages SET ack=? WHERE id=?",
                    (1 if delivered else 0, rowid),
                )
                self._msgdb.commit()
        except Exception as exc:
            Domoticz.Error(f"Message store ack update failed (non-fatal): {exc!r}")

    def _msg_store_query(self, scope: str, before=None, limit: int = 50,
                         search: str = "") -> dict:
        """Execute a paginated inbox query and return an inbox_page payload dict.

        *scope* is 'all' (no channel filter) or a specific chan value.
        *before* is an exclusive upper bound on id (for pagination); None = newest.
        *limit* is clamped to 1..250; a value <= 0 means "all" (no row limit —
            returns every match for the scope/search, has_more always False).
        *search* is a case-insensitive substring matched against body and sender;
            '%' and '_' in the term are treated literally (escaped).
        """
        try:
            _lim_in = int(limit) if limit is not None else 50
        except (TypeError, ValueError):
            _lim_in = 50
        all_mode = _lim_in <= 0
        limit = 0 if all_mode else max(1, min(250, _lim_in))
        search_str = str(search).strip() if search else ""
        if search and not search_str:
            Domoticz.Debug("inbox_query: search term was whitespace-only - ignoring it")
        before_int = int(before) if before is not None else None

        rows = []
        has_more = False
        oldest_id = None
        error = None

        if self._msgdb is None:
            return {
                "scope": scope, "search": search_str,
                "rows": [], "has_more": False, "oldest_id": None,
                "error": "store not available",
            }

        try:
            # Escape LIKE special characters so user-provided '%' / '_' are literal.
            def _like_escape(term: str) -> str:
                return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

            params: list = []
            clauses: list = []

            if scope.startswith("@k:"):
                # Key-based DM-thread scope: stable identity via peer_key column.
                # Normalise the supplied key the same way as stored values.
                dm_key = self._norm_peer_key(scope[3:])
                clauses.append("chan='P' AND peer_key=?")
                params.append(dm_key)
            elif scope.startswith("@"):
                # Name-based DM-thread scope (back-compat fallback):
                # all private messages to/from this contact by sender name.
                # Outgoing DMs are stored with sender "> <name>" or legacy "▶<name>"
                # / "▶ <name>"; incoming DMs with sender "<name>".
                dm_name = scope[1:]
                clauses.append(
                    "chan='P' AND sender IN (?,?,?,?)"
                )
                params.extend([
                    dm_name,
                    f"> {dm_name}",
                    f"▶{dm_name}",
                    f"▶ {dm_name}",
                ])
            elif scope != "all":
                # Channel scope. A channel message is stored under the
                # resolved channel NAME (e.g. "#test") when the name was
                # known at receive time, or under the "C<idx>" fallback when
                # it was not. The frontend sends whichever it has, so match
                # BOTH forms — otherwise a chip filter shows nothing while
                # "All" shows the same messages.
                variants = {scope}
                m = re.match(r"^C(\d+)$", scope)
                if m:
                    nm = self._channel_names.get(int(m.group(1)))
                    if nm:
                        variants.add(nm)
                else:
                    for _idx, _nm in self._channel_names.items():
                        if _nm == scope:
                            variants.add(f"C{_idx}")
                vs = sorted(variants)
                clauses.append("chan IN (%s)" % ",".join("?" for _ in vs))
                params.extend(vs)
                _dbg(f"_msg_store_query channel scope={scope!r} -> variants={vs}")

            if search_str:
                esc = _like_escape(search_str)
                pat = f"%{esc}%"
                clauses.append("(body LIKE ? ESCAPE '\\' OR sender LIKE ? ESCAPE '\\')")
                params.extend([pat, pat])

            if before_int is not None:
                clauses.append("id<?")
                params.append(before_int)

            where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            if all_mode:
                # "all": no LIMIT — return every match; nothing left to page.
                sql = (
                    f"SELECT id,chan,sender,epoch,bad,body,hops,snr,rssi,path,ack,direction,peer_key"
                    f" FROM messages {where} ORDER BY id DESC"
                )
            else:
                # Fetch one extra row to detect whether an older page exists.
                params.append(limit + 1)
                sql = (
                    f"SELECT id,chan,sender,epoch,bad,body,hops,snr,rssi,path,ack,direction,peer_key"
                    f" FROM messages {where} ORDER BY id DESC LIMIT ?"
                )
            with self._msgdb_lock:
                cur = self._msgdb.execute(sql, params)
                fetched = cur.fetchall()

            has_more = (not all_mode) and len(fetched) > limit
            trimmed = fetched if all_mode else fetched[:limit]
            for row in trimmed:
                # epoch is stored as UTC TEXT; convert back to int Unix-seconds for
                # the wire contract (frontend's _fmtEpoch and topology logic expect int).
                raw_epoch = row[3]
                try:
                    epoch_int = int(calendar.timegm(time.strptime(raw_epoch, '%Y-%m-%d %H:%M:%S')))
                except (TypeError, ValueError):
                    # Malformed stored timestamp (shouldn't happen — the write
                    # path validates): fall back to "now" rather than 1970 so a
                    # stray row doesn't sort/display as ancient.
                    epoch_int = int(time.time())
                rows.append({
                    "id":       row[0],
                    "chan":     row[1],
                    "sender":   row[2],
                    "epoch":    epoch_int,
                    "bad":      bool(row[4]),
                    "body":     row[5],
                    "hops":     row[6],
                    "snr":      row[7],
                    "rssi":     row[8],
                    "path":     row[9],
                    "ack":      row[10],
                    "dir":      row[11],
                    "peer_key": row[12],
                })
            oldest_id = rows[-1]["id"] if rows else None

        except Exception as exc:
            Domoticz.Error(f"Message store query failed (non-fatal): {exc!r}")
            error = str(exc)

        result = {
            "scope":    scope,
            "search":   search_str,
            "rows":     rows,
            "has_more": has_more,
            "oldest_id": oldest_id,
        }
        if error is not None:
            result["error"] = error
        return result

    # Valid sort_by values for heard_query.
    _HEARD_SORT_KEYS = frozenset(("recent", "least_hops", "max_hops", "times_heard"))

    def _handle_heard_query(
        self,
        offset: int = 0,
        limit: int = 100,
        search: str = "",
        type_filter: "int | None" = None,
        sort_by: str = "recent",
    ) -> dict:
        """Return a paginated page of heard nodes.

        offset: number of items to skip (position cursor — works for any sort).
        sort_by: "recent" | "least_hops" | "max_hops" | "times_heard".
        Returns: {rows, has_more, offset, total}
        Filters out nodes that are already known contacts.
        """
        if sort_by not in self._HEARD_SORT_KEYS:
            sort_by = "recent"
        known_pks = set(self._node_pubkey.values())
        with self._rx_log_lock:
            items = [
                (pk, dict(h))
                for pk, h in self._heard_nodes.items()
                if pk not in known_pks
            ]
        # Sort according to requested order.
        if sort_by == "least_hops":
            items.sort(key=lambda kv: (
                kv[1].get("path_len") if isinstance(kv[1].get("path_len"), int)
                and kv[1].get("path_len") >= 0 else 9999
            ))
        elif sort_by == "max_hops":
            items.sort(key=lambda kv: (
                kv[1].get("path_len") if isinstance(kv[1].get("path_len"), int)
                and kv[1].get("path_len") >= 0 else -1
            ), reverse=True)
        elif sort_by == "times_heard":
            items.sort(key=lambda kv: kv[1].get("count") or 0, reverse=True)
        else:  # "recent"
            items.sort(key=lambda kv: kv[1].get("last_heard") or 0, reverse=True)
        # Apply filters before counting total.
        if type_filter is not None:
            items = [(pk, h) for pk, h in items if h.get("type") == type_filter]
        if search:
            sl = search.lower()
            items = [(pk, h) for pk, h in items if sl in (h.get("name") or "").lower()]
        total = len(items)
        # Offset-based cursor (works for any sort order).
        start = max(0, offset)
        page = items[start:start + limit]
        rows = [{"pubkey": pk, **h} for pk, h in page]
        return {"rows": rows, "has_more": (start + limit) < total,
                "offset": start, "total": total}

    def _force_close_serial(self, mc):
        """Synchronously close the underlying pyserial port.

        Used only on shutdown so Windows releases the COM handle immediately
        instead of waiting for connection_lost to fire on a loop we're about
        to tear down.
        """
        try:
            connection = mc.connection_manager.connection
            transport = getattr(connection, "transport", None)
            if transport is None:
                return
            raw_serial = getattr(transport, "serial", None)
            try:
                transport.close()
            except Exception:
                pass
            if raw_serial is not None:
                try:
                    raw_serial.close()
                except Exception:
                    pass
        except Exception:
            pass

    def _device_id_for(self, node_name: str):
        """Resolve a node name to its DomoticzEx DeviceID.

        Returns "self" for the connected node, the 12-hex pubkey prefix for a
        remote contact, or None when the pubkey isn't known yet (the device is
        created on the next contacts poll once the pubkey arrives).
        """
        if node_name and node_name == self._self_name:
            return SELF_DID
        did = self._node_did.get(node_name)
        return did or None

    def _dev(self, device_id, unit):
        """Safe accessor — returns the DomoticzEx Unit object or None."""
        if not device_id or device_id not in Devices:
            return None
        dev = Devices[device_id]
        if unit not in dev.Units:
            return None
        return dev.Units[unit]

    def _set(self, device_id, unit, nValue=None, sValue=None):
        """Update a DomoticzEx unit. Unlike the classic framework, the Ex
        Update() does not take nValue/sValue kwargs — they're set as
        attributes on the Unit object, then Update() is called.

        Sets _ws_devices_dirty when the value actually changes so that
        outgoing-message echoes (and any other _set-driven writes) trigger a
        devices_delta push without waiting for the 90 s fallback."""
        d = self._dev(device_id, unit)
        if d is None:
            return
        changed = False
        if nValue is not None:
            new_n = int(nValue)
            if new_n != d.nValue:
                d.nValue = new_n
                changed = True
        if sValue is not None:
            new_s = str(sValue)
            if new_s != d.sValue:
                d.sValue = new_s
                changed = True
        d.Update()
        if changed:
            self._ws_devices_dirty = True

    def _all_node_names(self):
        """Self node (if known) + all discovered contacts."""
        names = []
        if self._self_name:
            names.append(self._self_name)
        names.extend(self._contact_names)
        return names

    async def _disconnect_mc(self, mc):
        """Graceful disconnect of the persistent connection."""
        if mc is None:
            return
        try:
            await asyncio.wait_for(mc.disconnect(), timeout=5)
        except Exception as exc:
            Domoticz.Debug(f"disconnect error: {exc}")
        if self.transport == "Serial":
            # Force-close the raw pyserial Serial so Windows releases the COM
            # handle right away instead of waiting for connection_lost.
            self._force_close_serial(mc)
            try:
                await asyncio.sleep(0.1)
            except Exception:
                pass

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def onStart(self):
        if not MESHCORE_AVAILABLE:
            Domoticz.Error("meshcore package not installed. Run: pip install meshcore")
            return

        Domoticz.Debugging(int(Parameters["Mode6"] or 0))
        self.transport = (Parameters.get("Mode1") or "TCP").strip() or "TCP"
        self.host      = Parameters.get("Address", "").strip()
        self.port      = int((Parameters.get("Port") or "5000").strip() or 5000)
        self.serial_port = Parameters.get("SerialPort", "").strip()
        try:
            self.baud_rate = int((Parameters.get("Mode2") or "115200").strip() or 115200)
        except ValueError:
            self.baud_rate = 115200

        if self.transport == "Serial" and not self.serial_port:
            Domoticz.Error("Serial transport selected but no serial port chosen.")
            return

        self._dzv_channel = (Parameters.get("Mode3") or "").strip()
        self._dzv_enabled = bool(self._dzv_channel)
        self._create_base_devices()
        # One-time cleanup: delete the legacy "Mesh Send" device (UNIT_SEND=2) if it
        # still exists from an older install. Commands are now sent via the WebSocket
        # channel; the text device is no longer created or needed.
        try:
            if MESH_DID in Devices and UNIT_SEND in Devices[MESH_DID].Units:
                Devices[MESH_DID].Units[UNIT_SEND].Delete()
                Domoticz.Log("Removed legacy 'Mesh Send' device (unit 2); commands are now sent via the WebSocket channel.")
        except Exception as _exc:
            Domoticz.Debug(f"Stale UNIT_SEND cleanup failed (non-fatal): {_exc}")
        # Pre-existing per-contact Messages devices (OFF_MSGS=15) are deliberately
        # left in place. DM history is now served from the SQLite store and these
        # devices are no longer created or written, but they are NOT auto-deleted —
        # the user removes them manually if/when they decide they're no longer needed.
        self._load_manual_locations()
        self._load_favorites()
        self._migrate_state_files()
        self._load_heard()
        self._load_rx_log()
        self._load_stats()
        self._load_channels()

        # Open SQLite message store (non-fatal if it fails).
        try:
            _db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "meshcore_messages.db")
            self._msg_store_open(_db_path)
        except Exception as _dbexc:
            Domoticz.Error(f"Message store init failed (non-fatal): {_dbexc!r}")

        if Parameters.get("Mode4", "true") == "true":
            self._install_custom_page()

        self.initialized = True
        # Heartbeat is now purely for draining the worker→main queue; the
        # actual MeshCore session is a persistent connection in a worker
        # thread. Use a fast tick at startup so the first contacts/self_info
        # batch is dispatched promptly, then `_dispatch` bumps it back to a
        # steady-state cadence once the first contacts arrive.
        Domoticz.Heartbeat(2)
        self._heartbeat_restored = False
        if self.transport == "Serial":
            Domoticz.Status(f"MeshCore plugin started - Serial {self.serial_port} @ {self.baud_rate}")
        else:
            Domoticz.Status(f"MeshCore plugin started - TCP {self.host}:{self.port}")

        # Proactively broadcast a snapshot so a dashboard left open across a
        # plugin-only restart (e.g. hardware disable/enable, or an auto-
        # reconnect) refreshes itself. The Domoticz WebSocket stays open in
        # that case, so the page never re-sends `hello` and would otherwise
        # show stale state until a manual reload. Broadcasting to the
        # plugin:<key> topic reaches any still-subscribed page; if none is
        # open the frame is simply dropped.
        self._broadcast_snapshot("startup")

        self._stop_event.clear()
        t = threading.Thread(target=self._worker_main, daemon=True, name="MeshCoreWorker")
        self._worker_thread = t
        t.start()

    def onStop(self):
        self._stopping = True
        self.initialized = False
        self._stop_event.set()

        loop = self._worker_loop
        stop_async = self._stop_async
        if loop is not None and loop.is_running() and stop_async is not None:
            # Wake the worker loop via its asyncio.Event so the serve loop
            # exits at its next iteration, runs its finally block (which
            # disconnects mc and drains serial_asyncio_fast's executor tasks)
            # and lets the thread shut down cleanly.
            try:
                loop.call_soon_threadsafe(stop_async.set)
            except Exception as exc:
                Domoticz.Debug(f"onStop: stop dispatch failed: {exc}")
            # Hard-cancel the _run() task too. Setting the event only breaks
            # the serve loop at its next _wait_or_stop; if we're blocked
            # inside an in-flight mc.* await (slow/hung network read, message
            # drain, periodic refresh) the loop never gets there and the
            # thread join below times out ("threads still running"). A
            # threadsafe Task.cancel() raises CancelledError straight into
            # that await so the worker unwinds well within the join window.
            mt = self._main_task
            if mt is not None:
                try:
                    loop.call_soon_threadsafe(mt.cancel)
                except Exception as exc:
                    Domoticz.Debug(f"onStop: task cancel dispatch failed: {exc}")

        # Wait for the worker thread to exit
        t = self._worker_thread
        if t is not None and t.is_alive():
            t.join(timeout=8)
            if t.is_alive():
                Domoticz.Log("onStop: worker thread did not stop within 8s.")

        # Force-close the serial port so Windows releases the COM handle even
        # if the cancelled tasks never reached the graceful disconnect path.
        if self.transport == "Serial" and self._mc is not None:
            self._force_close_serial(self._mc)
        self._mc = None
        self._worker_thread = None
        self._stop_async = None
        self._main_task = None
        # Drop references that could keep an IOCP / proactor pinned alive and
        # force a collection so the Windows COM port handle is released by
        # the asyncio internals.
        gc.collect()
        # Brief grace window — the foreign (Dummy-N) IOCP thread serving the
        # asyncio proactor on Windows can take a moment to die after the
        # loop's underlying handle is released. Give it 500 ms; not strictly
        # required but avoids Domoticz spamming "1 Python thread still
        # running" for ten seconds in the common case.
        for _ in range(10):
            alive_now = [t for t in threading.enumerate()
                         if t is not threading.main_thread()
                         and t.name != "MeshCoreWorker"]
            if not alive_now:
                break
            time.sleep(0.05)

        alive = [t for t in threading.enumerate() if t is not threading.main_thread()]
        Domoticz.Debug(f"onStop: {len(alive)} non-main thread(s) alive: " +
                       ", ".join(f"{t.name}(daemon={t.daemon})" for t in alive))

        self._remove_custom_page()

        # Close the message store connection (do NOT delete the .db file).
        if self._msgdb is not None:
            try:
                with self._msgdb_lock:
                    self._msgdb.close()
            except Exception as _mexc:
                Domoticz.Debug(f"Message store close: {_mexc!r}")
            self._msgdb = None

        Domoticz.Status("MeshCore plugin stopped.")

        # Workaround: Domoticz polls threading.enumerate() after onStop and
        # logs "Plugin has N Python threads still running" if anything besides
        # MainThread is reported. Our Domoticz plugin-host C thread (the one
        # currently running onStop) shows up as `Dummy-N` because it was
        # created in C and Python only registered it on first call-in. After
        # onStop returns this thread does exit in C, but Python's
        # threading._active dict never gets the entry pruned — so enumerate()
        # keeps reporting it for ~10s until Domoticz gives up. We pre-empt
        # that by removing the entry ourselves right before returning. It's
        # the very last thing we touch on this thread.
        try:
            my_tid = threading.get_ident()
            me = threading._active.get(my_tid)
            if me is None:
                Domoticz.Debug("DummyThread cleanup: no entry for current thread (already gone).")
            elif type(me).__name__ == "_DummyThread":
                del threading._active[my_tid]
                Domoticz.Debug("DummyThread cleanup: removed entry to silence Domoticz still-running warning.")
            else:
                # A future CPython rename of _DummyThread would land us here.
                # Log so the workaround silently stopping working is visible.
                Domoticz.Debug(
                    f"DummyThread cleanup: current thread is {type(me).__name__!r}, not _DummyThread — "
                    f"workaround did not run. Domoticz may report 'thread still running' until it gives up."
                )
        except Exception as exc:
            Domoticz.Debug(f"DummyThread cleanup: unexpected error: {exc}")

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

        # Expire pending DM ACK records that have exceeded the timeout and
        # annotate their sent lines with "(no ack)".
        self._sweep_pending_acks()

    # ── WebSocket channel (F1+) ───────────────────────────────────────────────

    def _broadcast_snapshot(self, reason: str):
        """Push an unsolicited snapshot (+ deferred heard) to the topic.

        Mirrors the `hello` handler but without a cmd_result/correlation id —
        the frontend's snapshot handler resets its delta baselines and
        re-renders on any snapshot it receives, so an open dashboard recovers
        automatically. Never raises into the caller.
        """
        try:
            snap = self._build_snapshot_payload()
            self._push("snapshot", snap)
            try:
                self._push("heard", {"heard": self._build_heard_slim_payload()})
            except Exception as _hexc:
                Domoticz.Debug(f"_broadcast_snapshot({reason}): deferred heard push failed: {_hexc}")
            _dbg(f"_broadcast_snapshot({reason}): snapshot pushed")
            Domoticz.Debug(f"_broadcast_snapshot({reason}): snapshot pushed")
        except Exception as exc:
            Domoticz.Debug(f"_broadcast_snapshot({reason}): snapshot build/push failed: {exc!r}")

    def _push(self, t: str, payload: dict):
        """Send a JSON frame to the dashboard via the plugin WebSocket channel.

        Performs a one-time feature-detect on first call: if Domoticz does not
        expose WebSocketSend (old build), logs once and sets _ws_ok=False so
        callers can skip further pushes gracefully.

        Never raises into callers — any exception from WebSocketSend is caught
        and logged so a bad send cannot stall the heartbeat queue drain.
        """
        if self._ws_ok is None:
            self._ws_ok = hasattr(Domoticz, "WebSocketSend")
            if not self._ws_ok:
                Domoticz.Log(
                    "MeshCore: Domoticz.WebSocketSend not available — "
                    "upgrade to build 17956+ for dashboard WebSocket support."
                )
        if not self._ws_ok:
            return
        msg = {"t": t}
        msg.update(payload)
        try:
            # Serialize ourselves and send a string rather than handing the
            # dict to Domoticz. Domoticz's built-in Python->JSON conversion
            # emits non-string dict keys unquoted (invalid JSON) and renders
            # None as the literal string "None"; json.dumps produces correct
            # JSON (quoted keys, null) and bypasses that converter entirely.
            if t in ("cmd_result", "inbox_page", "snapshot"):
                _dbg(f"_push frame t={t!r} id={payload.get('id')!r}")
            Domoticz.WebSocketSend(json.dumps(msg, ensure_ascii=False, default=str))
        except Exception as exc:
            _dbg(f"_push: WebSocketSend raised {exc!r} (t={t!r})")
            Domoticz.Debug(f"_push: WebSocketSend raised {exc!r}; ignoring.")

    def onWebSocketMessage(self, Data):
        # Domoticz calls this with a single positional string argument.
        # Confirmed in PluginMessages.h (onWebSocketMessageCallback::ProcessLocked)
        # which builds params via Py_BuildValue("(s)", m_Data.c_str()), and in
        # plugins/examples/WebSocketChannelTest/plugin.py lines 83 & 158.
        """Receive a message from the dashboard over the plugin WebSocket channel."""
        try:
            payload = json.loads(Data) if isinstance(Data, str) else Data
        except Exception as exc:
            Domoticz.Debug(f"onWebSocketMessage: JSON parse error: {exc}")
            return
        if not isinstance(payload, dict):
            Domoticz.Debug(f"onWebSocketMessage: unexpected payload type {type(payload)}")
            return

        t = payload.get("t")
        # Correlation id: clients send an opaque integer/string id with every
        # {t:'cmd'} frame; we echo it back in the cmd_result so the browser can
        # resolve the exact promise that triggered the command rather than
        # relying on FIFO shift().
        req_id = payload.get("id")
        if t in ("cmd", "inbox_query", "heard_query", "hello", "resync"):
            _dbg(f"onWebSocketMessage IN: t={t!r} id={req_id!r} "
                 f"cmd={payload.get('cmd')!r} scope={payload.get('scope')!r}")
        Domoticz.Debug(f"onWebSocketMessage: t={t!r} id={req_id!r}")

        if t == "hello":
            # Build the snapshot before sending the ack so a failure is
            # surfaced as an explicit ok=False frame rather than swallowed
            # silently (the client would then wait forever for a snapshot
            # that never arrives and show a "not ready" state with no hint).
            try:
                snap = self._build_snapshot_payload()
                self._push("cmd_result", {"ok": True, "target": "hello", "result": "connected", "id": req_id})
                self._push("snapshot", snap)
                # Deferred follow-up: heard can be large and is not needed for
                # first paint (only shown when the heard panel opens), so it is
                # sent as a separate frame right after the lean snapshot rather
                # than inflating it. The frontend t:'heard' handler applies it.
                try:
                    self._push("heard", {"heard": self._build_heard_slim_payload()})
                except Exception as _hexc:
                    Domoticz.Debug(f"hello: deferred heard push failed: {_hexc}")
            except Exception as exc:
                Domoticz.Error(f"hello: snapshot build failed: {exc!r}")
                self._push("cmd_result", {
                    "ok": False, "target": "hello",
                    "result": f"snapshot build failed: {exc}", "id": req_id,
                })

        elif t == "cmd":
            cmd = payload.get("cmd", "").strip()
            # Guard against whitespace-only input so split()[0] can never IndexError.
            target = cmd.split()[0] if cmd else "unknown"
            if not cmd:
                self._push("cmd_result", {"ok": False, "target": "unknown", "result": "empty cmd", "id": req_id})
                return

            # Elevation proxy: local DB + HTTP, no MC connection required.
            if cmd == "analytics":
                _panel   = str(payload.get("panel", "")).strip()
                _t_from  = payload.get("from")
                _t_to    = payload.get("to")
                _bucket  = payload.get("bucket")
                _nodes   = payload.get("nodes")
                try:
                    _t_from = int(_t_from)
                    _t_to   = int(_t_to)
                except (TypeError, ValueError):
                    self._push("cmd_result", {
                        "ok": False, "id": req_id,
                        "error": "from and to must be integer timestamps",
                    })
                    return
                if _bucket is not None:
                    try:
                        _bucket = int(_bucket)
                    except (TypeError, ValueError):
                        self._push("cmd_result", {"ok": False, "id": req_id, "error": "bucket must be a positive integer or null"})
                        return
                if _nodes is not None:
                    if not isinstance(_nodes, list):
                        self._push("cmd_result", {"ok": False, "id": req_id, "error": "nodes must be a list of strings or null"})
                        return
                    for _n in _nodes:
                        if not isinstance(_n, str):
                            self._push("cmd_result", {"ok": False, "id": req_id, "error": "nodes must be a list of strings or null"})
                            return
                result = self._handle_analytics_query(
                    _panel, _t_from, _t_to,
                    bucket_s=_bucket, nodes=_nodes,
                )
                result["id"] = req_id
                self._push("cmd_result", result)
                return

            if cmd == "elevation":
                raw_pts = payload.get("points", [])
                if not isinstance(raw_pts, list) or len(raw_pts) > 4096:
                    self._push("cmd_result", {
                        "ok": False, "id": req_id,
                        "error": "too many points",
                    })
                    return
                loop = self._worker_loop
                if loop is None:
                    self._push("cmd_result", {
                        "ok": False, "id": req_id,
                        "error": "worker not running",
                    })
                    return
                if not raw_pts:
                    self._push("cmd_result", {"ok": True, "id": req_id, "elevations": []})
                    return
                points = []
                try:
                    for p in raw_pts:
                        if not isinstance(p, (list, tuple)) or len(p) < 2:
                            raise ValueError("point must be [lat, lon]")
                        lat, lon = float(p[0]), float(p[1])
                        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
                            raise ValueError("point out of bounds")
                        points.append((lat, lon))
                except (ValueError, TypeError) as exc:
                    self._push("cmd_result", {
                        "ok": False, "id": req_id,
                        "error": f"invalid point: {exc}",
                    })
                    return

                async def _run_elevation(pts, rid):
                    try:
                        elevs = await asyncio.get_event_loop().run_in_executor(
                            None, self._elevation_lookup, pts
                        )
                        self._push("cmd_result", {
                            "ok": True, "id": rid,
                            "elevations": elevs,
                        })
                    except Exception as exc:
                        Domoticz.Error(f"elevation cmd failed: {exc!r}")
                        self._push("cmd_result", {
                            "ok": False, "id": rid,
                            "error": str(exc),
                        })

                try:
                    asyncio.run_coroutine_threadsafe(_run_elevation(points, req_id), loop)
                except Exception as exc:
                    self._push("cmd_result", {
                        "ok": False, "id": req_id,
                        "error": str(exc),
                    })
                return

            if self._handle_local_only_command(cmd):
                self._push("cmd_result", {"ok": True, "target": target, "result": "applied", "id": req_id})
                return
            loop = self._worker_loop
            if loop is None:
                self._push("cmd_result", {
                    "ok": False, "target": target,
                    "result": "not connected - auto-reconnect in progress",
                    "id": req_id,
                })
                return
            _dbg(f"cmd dispatched to worker: cmd={cmd!r} id={req_id!r}")
            Domoticz.Debug(f"WebSocket cmd: {cmd}")
            try:
                asyncio.run_coroutine_threadsafe(
                    self._send_message_for_text(cmd, req_id), loop
                )
            except Exception as exc:
                self._push("cmd_result", {
                    "ok": False, "target": target, "result": str(exc), "id": req_id,
                })

        elif t == "sub":
            feed = payload.get("feed", "none")
            with self._rx_log_lock:
                self._sub_feeds = str(feed)
            Domoticz.Debug(f"onWebSocketMessage: sub feed={feed!r}")
            # F3: on rxlog subscription, immediately push a full window so
            # the frontend has a baseline before the next periodic delta.
            if feed == "rxlog":
                try:
                    self._push_rx_log_window()
                except Exception as exc:
                    Domoticz.Debug(f"sub rxlog: _push_rx_log_window failed: {exc}")

        elif t == "resync":
            # F7 gap-recovery: the frontend detected a seq gap in devices_delta.
            # Clear the device-map baseline so the next _push_dirty_feeds sends
            # a full t:'devices' message instead of a delta.  Mirrors how
            # t:'sub' feed:'rxlog' forces a fresh rxlog window.
            feed = payload.get("feed", "")
            if feed == "devices":
                with self._rx_log_lock:
                    self._last_pushed_device_map = None
                self._ws_devices_dirty = True
                Domoticz.Debug("onWebSocketMessage: resync devices - baseline cleared")

        elif t == "inbox_query":
            # Paginated inbox query.  Run the DB lookup, build the page, push back.
            try:
                scope  = str(payload.get("scope", "all"))
                before = payload.get("before")
                raw_lim = payload.get("limit", 50)
                search = str(payload.get("search") or "")
                _dbg(f"inbox_query: scope={scope!r} before={before!r} "
                     f"limit={raw_lim!r} search={search!r} id={req_id!r}")
                page = self._msg_store_query(scope, before=before, limit=raw_lim, search=search)
                _dbg(f"inbox_page: scope={scope!r} rows={len(page.get('rows',[]))} "
                     f"has_more={page.get('has_more')} oldest_id={page.get('oldest_id')} "
                     f"id={req_id!r}")
                page["id"] = req_id
                self._push("inbox_page", page)
            except Exception as exc:
                _dbg(f"inbox_query handler EXC: {exc!r}")
                Domoticz.Error(f"inbox_query handler failed: {exc!r}")
                self._push("inbox_page", {
                    "id":       req_id,
                    "scope":    payload.get("scope", "all"),
                    "search":   str(payload.get("search") or ""),
                    "rows":     [],
                    "has_more": False,
                    "oldest_id": None,
                    "error":    str(exc),
                })

        elif t == "heard_query":
            # Paginated heard-nodes query.  Queries in-memory store, pushes page.
            try:
                _raw_lim = payload.get("limit", 100)
                _limit   = min(int(_raw_lim) if isinstance(_raw_lim, (int, float)) else 100, 500)
                _raw_off = payload.get("offset", 0)
                _offset  = int(_raw_off) if isinstance(_raw_off, (int, float)) else 0
                _search  = str(payload.get("search") or "")
                _tf_raw  = payload.get("type_filter")
                _type_f  = int(_tf_raw) if _tf_raw is not None else None
                _sort    = str(payload.get("sort_by") or "recent")
                page = self._handle_heard_query(
                    offset=_offset, limit=_limit,
                    search=_search, type_filter=_type_f, sort_by=_sort,
                )
                page["id"] = req_id
                self._push("heard_page", page)
            except Exception as exc:
                Domoticz.Error(f"heard_query handler failed: {exc!r}")
                self._push("heard_page", {
                    "id": req_id, "rows": [], "has_more": False,
                    "offset": 0, "total": 0, "error": str(exc),
                })

        else:
            Domoticz.Debug(f"onWebSocketMessage: unknown t={t!r}")

    def _handle_local_only_command(self, text: str) -> bool:
        """Handle commands that don't require an MC connection. Returns True if
        consumed (caller should not spawn a send worker).

        Note: we intentionally don't enqueue a "send_result" here — the
        dashboard already performs an optimistic update of its local
        _deviceMap.favorites array on click, so it doesn't need confirmation
        roundtripping through the queue.
        """
        if text.startswith("!forget_heard "):
            return self._handle_forget_heard(text[len("!forget_heard "):])
        if text.startswith("!purge_heard "):
            return self._handle_purge_heard(text[len("!purge_heard "):])
        if text.startswith("!purge_heard_count "):
            return self._handle_purge_heard_count(text[len("!purge_heard_count "):])
        if not text.startswith("!favorite "):
            return False
        try:
            _, action, name = text.split(None, 2)
        except ValueError:
            Domoticz.Error("!favorite syntax: !favorite add|remove <name>")
            return True
        action = action.lower()
        if action == "add":
            self._favorites.add(name)
            # Create the contact's devices now instead of waiting for the
            # next contacts poll (favourited contacts get real devices).
            self._ensure_node_devices(name)
        elif action == "remove":
            self._favorites.discard(name)
            # Remove the contact's now-unneeded Domoticz devices so the DB
            # doesn't accumulate them. The contact still shows on the
            # dashboard via the JSON map.
            self._delete_node_devices(name)
        else:
            Domoticz.Error(f"!favorite unknown action: {action}")
            return True
        self._save_favorites()
        self._write_device_map()
        Domoticz.Debug(f"Favorite {action}: {name}")
        return True

    def _demote_contact_to_heard(self, name: str):
        """Demote a just-removed contact into the heard-nodes store so its
        last-known metadata is not lost.  Call this BEFORE clearing the
        per-contact dicts so data is still available.

        Merges with an existing heard entry if one was present (preserves
        first_heard / count).  Removes the pubkey from _known_pubkeys so the
        worker's ADVERT gate lets future broadcasts through (for the heard
        store, not as a contact).  Sets _heard_dirty / _ws_heard_dirty.

        Caller must NOT hold _rx_log_lock."""
        pk = self._node_pubkey.get(name, "")
        if not pk:
            return
        h = {
            "pubkey":      pk,
            "name":        name,
            "type":        int(self._node_types.get(name, 1)),
            "first_heard": int(self._node_last_advert.get(name, 0) or time.time()),
            "last_heard":  int(self._node_last_advert.get(name, 0) or time.time()),
            "count":       0,
            "lat":         (self._node_locations.get(name) or {}).get("lat") or 0.0,
            "lon":         (self._node_locations.get(name) or {}).get("lon") or 0.0,
        }
        sig = self._contact_sig.get(pk[:12]) or {}
        h["snr"]      = sig.get("snr")
        h["rssi"]     = sig.get("rssi")
        h["path_len"] = sig.get("path_len", -1)
        with self._rx_log_lock:
            existing = self._heard_nodes.get(pk)
            if existing:
                # Preserve accumulator fields from the existing entry.
                # All other fields from the freshest contact state win.
                for k, v in h.items():
                    if k in ("first_heard", "count"):
                        continue  # never regress these accumulators
                    if v not in (None, "", 0, 0.0):
                        existing[k] = v
            else:
                self._heard_nodes[pk] = h
            self._known_pubkeys.discard(pk)
        self._heard_dirty = True
        self._ws_heard_dirty = True
        self._write_heard()
        Domoticz.Log(f"Contact '{name}' removed and demoted to heard nodes")

    def _handle_purge_heard(self, arg: str) -> bool:
        """Bulk-delete heard nodes whose last_heard age is older than the
        supplied threshold in seconds.  Matches the per-node !forget_heard
        semantics: removed pubkeys go into _heard_purged so a queued advert
        from one of them does not resurrect the entry. Returns True (always
        consumed). Nodes with no last_heard at all are also pruned — they
        carry no useful timestamp and would never satisfy any age filter."""
        try:
            older_than_s = int(arg.strip())
        except (ValueError, TypeError):
            Domoticz.Log(f"!purge_heard: invalid seconds argument {arg!r}")
            return True
        if older_than_s <= 0:
            Domoticz.Log("!purge_heard: seconds must be positive")
            return True
        cutoff = int(time.time()) - older_than_s
        removed = 0
        with self._rx_log_lock:
            to_remove = [
                pk for pk, h in self._heard_nodes.items()
                if not h.get("last_heard") or int(h.get("last_heard") or 0) < cutoff
            ]
            for pk in to_remove:
                self._heard_nodes.pop(pk, None)
                self._heard_purged.add(pk)
                removed += 1
            if removed:
                self._heard_dirty = True
                self._ws_heard_dirty = True
        if removed:
            self._write_heard()
            Domoticz.Log(f"!purge_heard: removed {removed} heard node(s) older than {older_than_s}s")
        else:
            Domoticz.Log(f"!purge_heard: no heard nodes older than {older_than_s}s")
        return True

    def _handle_purge_heard_count(self, arg: str) -> bool:
        """Bulk-delete heard nodes whose advert count is strictly less than
        the supplied threshold (so threshold=2 removes 0× and 1× entries).
        Same purge semantics as !purge_heard: removed pubkeys go into
        _heard_purged so a queued advert does not resurrect them.
        Returns True (always consumed)."""
        try:
            threshold = int(arg.strip())
        except (ValueError, TypeError):
            Domoticz.Log(f"!purge_heard_count: invalid threshold {arg!r}")
            return True
        if threshold <= 0:
            Domoticz.Log("!purge_heard_count: threshold must be positive")
            return True
        removed = 0
        with self._rx_log_lock:
            to_remove = [
                pk for pk, h in self._heard_nodes.items()
                if int(h.get("count") or 0) < threshold
            ]
            for pk in to_remove:
                self._heard_nodes.pop(pk, None)
                self._heard_purged.add(pk)
                removed += 1
            if removed:
                self._heard_dirty = True
                self._ws_heard_dirty = True
        if removed:
            self._write_heard()
            Domoticz.Log(f"!purge_heard_count: removed {removed} heard node(s) with count < {threshold}")
        else:
            Domoticz.Log(f"!purge_heard_count: no heard nodes with count < {threshold}")
        return True

    def _handle_forget_heard(self, pubkey_or_prefix: str) -> bool:
        """Permanently delete a heard node and add it to the purged set so
        re-broadcasts from that node do not resurrect it.  Accepts a 12-hex
        prefix or a full pubkey.  Returns True (always consumed)."""
        prefix = pubkey_or_prefix.strip().lower()
        if not prefix:
            Domoticz.Log("!forget_heard: no pubkey specified")
            return True
        with self._rx_log_lock:
            matched_pk = next(
                (k for k in self._heard_nodes if k.lower().startswith(prefix)), None
            )
            if matched_pk:
                self._heard_nodes.pop(matched_pk, None)
                self._heard_purged.add(matched_pk)
                self._heard_dirty = True
                self._ws_heard_dirty = True
                Domoticz.Log(f"Heard node {matched_pk[:12]} deleted and purged")
            else:
                Domoticz.Log(f"!forget_heard: no heard node matched prefix {prefix!r}")
        if matched_pk:
            self._write_heard()
        return True

    def _delete_node_devices(self, node_name: str):
        """Delete all Domoticz units for a (non-self) contact's DeviceID."""
        did = self._device_id_for(node_name)
        if not did or did == SELF_DID or did not in Devices:
            return
        try:
            for unit in list(Devices[did].Units):
                Devices[did].Units[unit].Delete()
            Domoticz.Log(f"Removed devices for node '{node_name}' (DeviceID={did})")
        except Exception as exc:
            Domoticz.Debug(f"_delete_node_devices({node_name}) error: {exc}")

    # ── Device creation ───────────────────────────────────────────────────────

    def _create_base_devices(self):
        def _have(unit):
            return MESH_DID in Devices and unit in Devices[MESH_DID].Units
        if not _have(UNIT_INBOX):
            Domoticz.Unit(Name="Mesh Inbox", DeviceID=MESH_DID, Unit=UNIT_INBOX,
                          TypeName="Text").Create()
        # UNIT_SEND (unit 2) is intentionally NOT created — commands are sent via the
        # WebSocket channel. Any existing UNIT_SEND device from older installs is
        # deleted in onStart (stale-device cleanup).
        if not _have(UNIT_MSGS_RECV):
            Domoticz.Unit(Name="Mesh Msgs Received", DeviceID=MESH_DID, Unit=UNIT_MSGS_RECV,
                          TypeName="Custom", Options={"Custom": "1;msgs"}).Create()
        if not _have(UNIT_MSGS_SENT_):
            Domoticz.Unit(Name="Mesh Msgs Sent", DeviceID=MESH_DID, Unit=UNIT_MSGS_SENT_,
                          TypeName="Custom", Options={"Custom": "1;msgs"}).Create()
        if self._dzv_enabled:
            if not _have(UNIT_DZV_IN):
                Domoticz.Unit(Name="MeshCore Command In", DeviceID=MESH_DID, Unit=UNIT_DZV_IN,
                              TypeName="Text").Create()
            if not _have(UNIT_DZV_REPLY):
                Domoticz.Unit(Name="MeshCore Reply", DeviceID=MESH_DID, Unit=UNIT_DZV_REPLY,
                              TypeName="Text").Create()
            if not _have(UNIT_DZV_SEND):
                Domoticz.Unit(Name="MeshCore Send", DeviceID=MESH_DID, Unit=UNIT_DZV_SEND,
                              TypeName="Switch", Switchtype=9).Create()

    def _favorites_path(self) -> str:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "meshcore_favorites.json")

    def _load_favorites(self):
        p = self._favorites_path()
        if not os.path.isfile(p):
            return
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                self._favorites = {str(x) for x in data}
                Domoticz.Log(f"Loaded {len(self._favorites)} favorite contact(s).")
        except Exception as exc:
            Domoticz.Error(f"Could not load meshcore_favorites.json: {exc}")

    def _save_favorites(self):
        p = self._favorites_path()
        try:
            with open(p, "w", encoding="utf-8") as f:
                json.dump(sorted(self._favorites), f)
        except Exception as exc:
            Domoticz.Error(f"Could not write meshcore_favorites.json: {exc}")

    def _migrate_state_files(self, _plugin_dir: str = None, _old_dir: str = None):
        """One-time migration: move state JSON files from the old www/templates
        location to the plugin directory.

        Prior to F4 the five state files were written into www/templates so the
        dashboard could fetch them.  They now live in the plugin directory and
        are never HTTP-served.  When upgrading, the new location will be empty
        on the first start, so we copy from the old location and then remove
        the old copy to keep things tidy.  The operation is idempotent: if the
        new file already exists (or the old file doesn't) it is a no-op.

        The ``_plugin_dir`` and ``_old_dir`` parameters exist solely to allow
        the unit tests to inject a temporary directory without relying on
        module-level ``__file__`` patching (which is unreliable under CPython
        3.13's specializing adaptive interpreter).  Production code never
        passes them.
        """
        plugin_dir    = _plugin_dir or os.path.dirname(os.path.abspath(__file__))
        domoticz_root = os.path.abspath(os.path.join(plugin_dir, "..", ".."))
        old_dir       = _old_dir or os.path.join(domoticz_root, "www", "templates")
        for fname in (
            "meshcore_devices.json",
            "meshcore_rx_log.json",
            "meshcore_heard.json",
            "meshcore_stats.json",
            "meshcore_channels.json",
        ):
            new_path = os.path.join(plugin_dir, fname)
            old_path = os.path.join(old_dir, fname)
            if os.path.isfile(new_path):
                # New location already populated — nothing to do.
                # Also remove the stale old copy so www/templates stays clean.
                try:
                    os.remove(old_path)
                    Domoticz.Debug(f"Migration: removed stale {fname} from templates dir")
                except FileNotFoundError:
                    pass
                except Exception as exc:
                    Domoticz.Debug(f"Migration: could not remove old {fname}: {exc}")
                continue
            if not os.path.isfile(old_path):
                continue
            try:
                shutil.copy2(old_path, new_path)
                os.remove(old_path)
                Domoticz.Log(f"Migration: moved {fname} from templates to plugin dir")
            except Exception as exc:
                Domoticz.Log(f"Migration: could not move {fname} (non-fatal, history may reset): {exc}")

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
        did = self._device_id_for(node_name)
        if did is None:
            # No pubkey yet — the contacts poll will create the device once
            # the pubkey is known. Self always resolves to SELF_DID.
            return
        is_self = (did == SELF_DID)
        # Only the self node and favourited contacts get real Domoticz
        # devices. The mesh can carry 300+ contacts (repeaters / room
        # servers / sensors) you never interact with — creating ~4 devices
        # each would bloat the DB and Domoticz UI. Non-favourite contacts
        # still appear fully on the dashboard via the WebSocket devices push
        # (last_seen comes from _node_last_activity, plus type/last_advert/
        # pubkey/query are all in the map). Favourite a contact and its
        # devices are created on the next contacts poll.
        if not is_self and node_name not in self._favorites:
            return
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
        existing_units = (Devices[did].Units if did in Devices else {})
        for offset, name, typename, opts in specs:
            if offset not in existing_units:
                Domoticz.Unit(Name=name, DeviceID=did, Unit=offset,
                              TypeName=typename, Options=opts).Create()
                created = True
        if created:
            Domoticz.Log(f"Created devices for node '{node_name}' (DeviceID={did})")
            self._write_device_map()

    # ── Custom dashboard page ──────────────────────────────────────────────────

    def _install_custom_page(self):
        plugin_dir = os.path.dirname(os.path.abspath(__file__))
        template = os.path.join(plugin_dir, "meshcore.html")
        if not os.path.isfile(template):
            Domoticz.Error("meshcore.html template not found - dashboard not installed.")
            return

        domoticz_root = os.path.abspath(os.path.join(plugin_dir, "..", ".."))
        dest_dir      = os.path.join(domoticz_root, "www", "templates")
        dest          = os.path.join(dest_dir, "meshcore.html")

        try:
            os.makedirs(dest_dir, exist_ok=True)
            shutil.copy2(template, dest)
            Domoticz.Log(f"MeshCore dashboard installed: {dest}")
        except Exception as exc:
            Domoticz.Error(f"Failed to install dashboard: {exc}")

        # Bundle Leaflet locally so the topology / map panel works even when
        # the browser's tracking-prevention blocks unpkg.com (Edge/Firefox).
        leaflet_src = os.path.join(plugin_dir, "assets", "leaflet")
        leaflet_dst = os.path.join(dest_dir, "leaflet")
        if os.path.isdir(leaflet_src):
            try:
                os.makedirs(leaflet_dst, exist_ok=True)
                for fname in ("leaflet.js", "leaflet.css", "leaflet-heat.js"):
                    s = os.path.join(leaflet_src, fname)
                    if os.path.isfile(s):
                        shutil.copy2(s, os.path.join(leaflet_dst, fname))
                Domoticz.Debug(f"Leaflet installed: {leaflet_dst}")
            except Exception as exc:
                Domoticz.Error(f"Failed to install Leaflet: {exc}")

    def _channels_path(self) -> str:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "meshcore_channels.json")

    def _load_channels(self):
        """Restore channel index→name map on startup so ``_channel_names``
        (and therefore the snapshot's ``channels`` field) is populated before
        the first connection-time channel fetch."""
        path = self._channels_path()
        if not os.path.isfile(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return
            # JSON keys are strings; restore as int-keyed dict to match the
            # in-memory convention.  Skip empty-string entries (empty slots).
            loaded = {int(k): v for k, v in data.items()
                      if v and isinstance(v, str)}
            if loaded:
                self._channel_names = loaded
                parts = [f"#{k}={v}" for k, v in sorted(loaded.items())]
                Domoticz.Log(f"Restored channel names: {', '.join(parts)}")
        except Exception as exc:
            Domoticz.Error(f"Could not load meshcore_channels.json: {exc}")

    def _write_channel_names(self, channel_names: dict):
        """Persist channel index→name map to meshcore_channels.json (plugin dir)
        and mark the WebSocket channel feed dirty."""
        dest = self._channels_path()
        try:
            with open(dest, "w") as f:
                json.dump(channel_names, f)
        except Exception as exc:
            Domoticz.Debug(f"Could not write channel names: {exc}")
        self._ws_channels_dirty = True
        self._ws_devices_dirty = True

    def _build_device_map_payload(self) -> dict:
        """Build and return the device-map dict (persisted to plugin dir and
        pushed as ``t:'devices'`` over WebSocket).
        Callers own the returned dict; it is safe to mutate or serialize.
        """
        def _slot(did, unit):
            """Return {idx, value, online} for a device unit, or None if not created yet."""
            d = self._dev(did, unit)
            if not d:
                return None
            return {
                "idx":    d.ID,
                "value":  d.sValue if d.sValue else None,
                "online": d.nValue == 1,
            }

        nodes = {}
        for node_name in self._all_node_names():
            did = self._device_id_for(node_name)
            loc = self._node_locations.get(node_name, {})
            pk_full = self._node_pubkey.get(node_name, "")
            # Build the last_seen slot. _node_last_activity is the source of
            # truth — it's always updated regardless of whether the Domoticz
            # device exists yet (pubkey-less contacts have no DeviceID until
            # the contacts poll). We surface the matching device idx for the
            # click-through to the device log when it has been created.
            ls_slot_dev = _slot(did, OFF_LASTSEEN)
            last_activity = self._node_last_activity.get(node_name, 0)
            if last_activity:
                ls_slot = {
                    "idx": ls_slot_dev.get("idx") if ls_slot_dev else None,
                    "value": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_activity)),
                    "online": False,
                }
            else:
                ls_slot = ls_slot_dev
            nodes[node_name] = {
                "status":    _slot(did, OFF_STATUS),
                "battery":   _slot(did, OFF_BATT_PCT),
                "battery_v": _slot(did, OFF_BATT_V),
                "rssi":      _slot(did, OFF_RSSI),
                "snr":       _slot(did, OFF_SNR),
                "noise":     _slot(did, OFF_NOISE),
                "last_seen": ls_slot,
                "hops":      _slot(did, OFF_HOPS),
                "uptime":    _slot(did, OFF_UPTIME),
                "airtime":   _slot(did, OFF_AIRTIME),
                "pkts_sent": _slot(did, OFF_MSGS_SENT),
                "pkts_recv": _slot(did, OFF_MSGS_RECV),
                "lat":       loc.get("lat"),
                "lon":       loc.get("lon"),
                # Contact metadata — used by the dashboard for type chip and sorting.
                # type: 1=Contact, 2=Repeater, 3=Room Server, 4=Sensor (0 for self)
                "type":          self._node_types.get(node_name, 0),
                "last_advert":   self._node_last_advert.get(node_name, 0),
                # Full pubkey (hex) and first-12-char prefix used by the firmware
                # to identify message senders. The sparkline / signal-history
                # lookup is keyed by the prefix.
                "pubkey":        pk_full,
                "pubkey_prefix": pk_full[:12] if pk_full else "",
                # Trustworthy clock-skew sample (node advert RTC vs our
                # receive time of that advert), or None if we've not received
                # an over-the-air advert from this contact this session.
                "clock":         (dict(self._contact_clock.get(pk_full[:12]))
                                  if pk_full and self._contact_clock.get(pk_full[:12])
                                  else None),
                # Per-contact query results (status / telemetry / neighbours)
                # from req_* sync calls. None if never queried.
                "query":         self._contact_query_results.get(node_name, {}),
                # Outbound path for the topology polyline renderer.
                # out_path: hex string of repeater hash bytes (e.g. "22a83b"),
                # empty string when the path is direct / flood, or None when unknown.
                # out_path_hash_mode: integer +1 offset (1=1-byte, 2=2-byte, 3=3-byte).
                "out_path":           self._node_out_path.get(node_name, ""),
                "out_path_hash_mode": self._node_out_path_hash_mode.get(node_name, 0),
            }

            # Fall back to the latest received signal (advert OR message) for
            # contacts that have no Domoticz device (non-favourites) or no
            # message/direct-path data, so their cards aren't bare. Device
            # slots (favourites with live data) win over this fallback.
            # _contact_sig is mutated by the worker thread under _rx_log_lock;
            # take a shallow snapshot of the single entry to avoid tearing.
            if pk_full:
                with self._rx_log_lock:
                    adv = dict(self._contact_sig.get(pk_full[:12]) or {}) or None
            else:
                adv = None
            # Ignore stale fallback signal (older than the node-online window)
            # so cards don't show indefinitely-old SNR/RSSI for gone nodes.
            if adv and (time.time() - adv.get("t", 0)) > 28800:
                adv = None
            if adv and did != SELF_DID:
                e = nodes[node_name]
                if e["snr"] is None and adv.get("snr") is not None:
                    e["snr"] = {"value": f"{adv['snr']} dB"}
                if e["rssi"] is None and adv.get("rssi") is not None:
                    e["rssi"] = {"value": f"{adv['rssi']} dBm"}
                _pl = adv.get("path_len", -1)
                if e["hops"] is None and isinstance(_pl, int) and _pl >= 0:
                    e["hops"] = {"value": str(_pl)}

        inbox_dev = self._dev(MESH_DID, UNIT_INBOX)
        payload = {
            "inbox":        inbox_dev.ID if inbox_dev else None,
            "inbox_value":  inbox_dev.sValue if inbox_dev else None,
            "self":         self._self_name or None,
            "nodes":        nodes,
            # True = device blocks auto-add of new contacts from adverts
            "manual_add_contacts": bool(self._manual_add_contacts),
            "telemetry_mode_base": int(self._telemetry_mode_base),
            "telemetry_mode_loc":  int(self._telemetry_mode_loc),
            "telemetry_mode_env":  int(self._telemetry_mode_env),
            "adv_loc_policy":      int(self._advert_loc_policy),
            "default_flood_scope": self._default_flood_scope or "",
            "favorites":    sorted(self._favorites),
            "device_info":  self._device_info or {},
            # Full self_info snapshot — radio params, our coordinates, pubkey.
            # Dashboard self-node side panel reads from here.
            "self_info":    {
                "name":           self._self_info_full.get("name", self._self_name),
                "public_key":     self._self_info_full.get("public_key", ""),
                "adv_lat":        self._self_info_full.get("adv_lat", 0.0),
                "adv_lon":        self._self_info_full.get("adv_lon", 0.0),
                "adv_type":       self._self_info_full.get("adv_type", 0),
                "radio_freq":     self._self_info_full.get("radio_freq", 0),
                "radio_bw":       self._self_info_full.get("radio_bw", 0),
                "radio_sf":       self._self_info_full.get("radio_sf", 0),
                "radio_cr":       self._self_info_full.get("radio_cr", 0),
                "tx_power":       self._self_info_full.get("tx_power", 0),
                "max_tx_power":   self._self_info_full.get("max_tx_power", 0),
                "multi_acks":     self._self_info_full.get("multi_acks", 0),
            },
            "self_telemetry": self._self_telemetry or {},
            # Every channel slot the device has, so the dashboard can render
            # an Add/Remove button per slot. Non-empty slots have their
            # `name`; empty slots have name = "".
            "channel_slots": [
                {"idx": i, "name": self._channel_slots.get(i, "")} for i in range(MAX_CHANNEL_SLOTS)
            ],
            "written_at":   int(time.time()),
        }
        return payload

    def _write_device_map(self):
        """Persist the device map to meshcore_devices.json (plugin dir) and mark the
        WebSocket feed dirty so the next flush pushes it to the dashboard."""
        payload = self._build_device_map_payload()
        dest = os.path.join(os.path.dirname(os.path.abspath(__file__)), "meshcore_devices.json")
        try:
            with open(dest, "w") as f:
                json.dump(payload, f)
        except Exception as exc:
            Domoticz.Debug(f"Could not write device map: {exc}")
        self._ws_devices_dirty = True

    def _write_rx_log(self):
        """Atomically write the rolling RX_LOG_DATA buffer + aggregates as JSON.

        The dashboard fetches this every few seconds to render the analyzer
        detail panel, RF firehose, signal sparklines, packet-rate heatmap,
        duplicate-flood and channel-discovery views.
        """
        dest = self._rx_log_path()

        now = time.time()
        with self._rx_log_lock:
            entries = list(self._rx_log)
            payload_type_counts = dict(self._payload_type_counts)
            chan_hash_counts    = dict(self._chan_hash_counts)
            signal_history      = {k: list(v) for k, v in self._signal_history.items()}
            dup_floods          = {k: list(v) for k, v in self._dup_floods.items() if len(v) > 1}
            # Trim packet_times to last 24h for the heatmap, then bucket by hour-of-day
            cutoff = now - 86400
            while self._packet_times and self._packet_times[0] < cutoff:
                self._packet_times.popleft()
            heatmap = [0] * 24
            for t in self._packet_times:
                heatmap[time.localtime(t).tm_hour] += 1
            # Persist the (already 24h-trimmed) timestamps so the heatmap can
            # be restored on the next plugin start instead of resetting.
            packet_times = [int(t) for t in self._packet_times]
            known_channels_snap = dict(self._channel_names)
            chan_hash_names_snap = dict(self._chan_hash_to_name)

        # Build a JSON-safe view of each entry (already normalized in _on_rx_log)
        out_entries = []
        for e in entries:
            row = {}
            for k, v in e.items():
                # Already-normalized values are JSON-safe; defend against stray bytes
                if isinstance(v, (bytes, bytearray)):
                    row[k] = v.hex()
                elif isinstance(v, (str, int, float, bool)) or v is None:
                    row[k] = v
                else:
                    row[k] = str(v)
            out_entries.append(row)

        payload = {
            "written_at": int(now),
            "entries":    out_entries,
            "stats": {
                "payload_type_counts": payload_type_counts,
                "chan_hash_counts":    chan_hash_counts,
                "signal_history":      signal_history,
                "dup_floods":          dup_floods,
                "heatmap_24h":         heatmap,
            },
            "packet_times":    packet_times,
            "known_channels":  known_channels_snap,
            "chan_hash_names":  chan_hash_names_snap,
        }

        try:
            tmp = dest + ".tmp"
            with open(tmp, "w") as f:
                json.dump(payload, f)
            os.replace(tmp, dest)
        except Exception as exc:
            Domoticz.Debug(f"Could not write rx log: {exc}")

    def _rx_log_path(self) -> str:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "meshcore_rx_log.json")

    # ── F3: rx-log on-demand push helpers ────────────────────────────────────

    def _build_rx_log_payload(self, entries: list) -> dict:
        """Serialize a list of rx-log entries into the JSON-safe shape used by
        the file writer and the WebSocket push.

        Caller must already hold ``_rx_log_lock`` (or pass a snapshot copy).
        """
        out = []
        for e in entries:
            row = {}
            for k, v in e.items():
                if isinstance(v, (bytes, bytearray)):
                    row[k] = v.hex()
                elif isinstance(v, (str, int, float, bool)) or v is None:
                    row[k] = v
                else:
                    row[k] = str(v)
            out.append(row)
        return out

    def _build_rx_log_stats(self, now: float) -> dict:
        """Build the aggregated stats sub-object (same shape as the ``stats``
        key in the rx-log payload).  Trims ``_packet_times`` to last 24 h
        as a side-effect.

        Must be called with ``_rx_log_lock`` held.
        """
        cutoff = now - 86400
        while self._packet_times and self._packet_times[0] < cutoff:
            self._packet_times.popleft()
        heatmap = [0] * 24
        for t in self._packet_times:
            heatmap[time.localtime(t).tm_hour] += 1
        return {
            "payload_type_counts": dict(self._payload_type_counts),
            "chan_hash_counts":    dict(self._chan_hash_counts),
            "signal_history":     {k: list(v) for k, v in self._signal_history.items()},
            "dup_floods":         {k: list(v) for k, v in self._dup_floods.items() if len(v) > 1},
            "heatmap_24h":        heatmap,
        }

    def _push_rx_log_window(self):
        """Push a full rx-log window to the subscribed frontend client.

        Increments ``_rx_log_seq`` and records ``_rx_log_pushed_total`` so
        subsequent ``_push_rx_log_delta`` calls can compute exactly which
        entries are new using the absolute append counter.

        Must be called WITHOUT holding ``_rx_log_lock``; acquires it internally.
        """
        now = time.time()
        with self._rx_log_lock:
            entries_snap  = list(self._rx_log)
            stats_snap    = self._build_rx_log_stats(now)
            self._rx_log_seq += 1
            seq = self._rx_log_seq
            self._rx_log_pushed_total = self._rx_log_total_appended
            chan_hash_snap = dict(self._chan_hash_to_name)
        self._push("rxlog", {
            "entries":         self._build_rx_log_payload(entries_snap),
            "stats":           stats_snap,
            "seq":             seq,
            "chan_hash_names":  chan_hash_snap,
        })
        Domoticz.Debug(f"_push_rx_log_window: seq={seq} entries={len(entries_snap)}")

    def _push_rx_log_delta(self):
        """Push only the entries appended since the last window/delta push.

        Uses the monotonic absolute counter ``_rx_log_total_appended`` rather
        than the deque length, so the full-buffer steady state (len stays at
        RX_LOG_BUFFER=250 while old entries are evicted) never silently drops
        new arrivals.

        Fallback: if ``_rx_log_pushed_total`` is older than the oldest entry
        still in the buffer (i.e. the client missed evicted entries), a full
        ``_push_rx_log_window`` is issued instead.

        Must be called WITHOUT holding ``_rx_log_lock``.
        Called from the existing rx-log write cadence when a subscriber is
        active.
        """
        now = time.time()

        # All decision values, the slice of new entries, the stats side-effect,
        # the seq increment, and the pushed_total update are derived under a
        # SINGLE lock acquisition so that no interleaved append can cause the
        # slice offset (pushed_total - start) to become stale before it is used.
        eviction_gap = False
        payload = None
        with self._rx_log_lock:
            sub          = self._sub_feeds
            if sub != "rxlog":
                return

            current_total = self._rx_log_total_appended
            pushed_total  = self._rx_log_pushed_total
            buf_len       = len(self._rx_log)
            # Oldest absolute index still present in the deque.
            start         = current_total - buf_len

            if pushed_total < start:
                # The client missed entries that were evicted from the deque —
                # signal the eviction-gap fallback; release the lock first so
                # _push_rx_log_window can acquire it.
                eviction_gap = True
            else:
                # Slice off only the entries the client hasn't seen yet.
                # pushed_total - start is the number of already-sent entries
                # that are still in the buffer; everything after that is new.
                new_entries = list(self._rx_log)[pushed_total - start:]
                if new_entries:
                    stats_snap = self._build_rx_log_stats(now)
                    self._rx_log_seq += 1
                    seq = self._rx_log_seq
                    self._rx_log_pushed_total = current_total
                    chan_hash_snap = dict(self._chan_hash_to_name)
                    payload = (new_entries, stats_snap, seq, chan_hash_snap)

        # Lock released — safe to call _push / _push_rx_log_window.
        if eviction_gap:
            Domoticz.Debug("_push_rx_log_delta: eviction gap, pushing full window")
            self._push_rx_log_window()
            return

        if payload is None:
            return

        new_entries, stats_snap, seq, chan_hash_snap = payload
        self._push("rxlog_delta", {
            "entries":         self._build_rx_log_payload(new_entries),
            "stats":           stats_snap,
            "seq":             seq,
            "chan_hash_names":  chan_hash_snap,
        })
        Domoticz.Debug(f"_push_rx_log_delta: seq={seq} new={len(new_entries)}")

    def _load_rx_log(self):
        """Restore the packet-time history, chan_hash_names and the heard
        channel-hash counters on startup.

        Packet timestamps power the packets/hour heatmap; chan_hash_names
        avoid a cold-start gap where configured channel hashes show as
        "unknown" until _fetch_channel_names completes its first round-trip;
        chan_hash_counts keep the "Hashes heard on air" table in the channels
        panel populated across a restart instead of resetting to empty.
        The rolling frame buffer / sparklines rebuild from live RX events.
        """
        # Only seed an empty buffer. On a warm disable→enable (Domoticz still
        # running) _packet_times may already hold live samples; appending the
        # persisted ones again would double-count the heatmap.
        if self._packet_times:
            return
        path = self._rx_log_path()
        if not os.path.isfile(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return
            pts = data.get("packet_times")
            if isinstance(pts, list):
                cutoff = time.time() - 86400
                pts = sorted(int(t) for t in pts if isinstance(t, (int, float)))
                pts = [t for t in pts if t >= cutoff]
                # Respect the deque's maxlen — keep the most recent samples.
                self._packet_times.extend(pts)
                Domoticz.Log(f"Restored {len(self._packet_times)} packet timestamp(s) for the heatmap")
            chn = data.get("chan_hash_names")
            if isinstance(chn, dict):
                restored = {str(k).lower(): str(v) for k, v in chn.items() if k and v}
                if restored:
                    with self._rx_log_lock:
                        self._chan_hash_to_name = restored
                    Domoticz.Log(f"Restored {len(restored)} chan_hash->name mapping(s) from rx-log")
            stats = data.get("stats")
            counts = stats.get("chan_hash_counts") if isinstance(stats, dict) else None
            if isinstance(counts, dict):
                restored_counts: dict = {}
                for k, v in counts.items():
                    if k and isinstance(v, (int, float)) and int(v) > 0:
                        key = str(k).lower()
                        restored_counts[key] = restored_counts.get(key, 0) + int(v)
                if restored_counts:
                    # Only seed when empty — a warm disable→enable keeps the
                    # live counters and re-adding the persisted ones on top
                    # would double-count.
                    with self._rx_log_lock:
                        if not self._chan_hash_counts:
                            self._chan_hash_counts.update(restored_counts)
                            Domoticz.Log(f"Restored {len(restored_counts)} heard channel-hash counter(s) from rx-log")
        except Exception as exc:
            Domoticz.Error(f"Could not load packet times from meshcore_rx_log.json: {exc}")

    def _stats_path(self) -> str:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "meshcore_stats.json")

    def _load_stats(self):
        """Restore lifetime statistics on startup. Today's counters reset
        when the stored day != the current local day."""
        path = self._stats_path()
        if not os.path.isfile(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return
            s = self._stats
            for k in ("adverts_total", "messages_total", "client_total",
                      "repeater_total", "server_total"):
                if isinstance(data.get(k), int):
                    s[k] = data[k]
            for k in ("msg_by_sender", "adv_by_sender", "msg_by_channel"):
                if isinstance(data.get(k), dict):
                    s[k] = {str(n): int(c) for n, c in data[k].items()
                            if isinstance(c, (int, float))}
            recs = data.get("hops_records")
            if isinstance(recs, list):
                clean = [
                    {"hops": int(r.get("hops", -1)), "name": str(r.get("name", "")),
                     "date": str(r.get("date", "")), "channel": str(r.get("channel", ""))}
                    for r in recs
                    if isinstance(r, dict) and isinstance(r.get("hops"), int)
                    and 0 <= int(r.get("hops", -1)) < HOPS_SENTINEL
                ]
                clean.sort(key=lambda r: r["hops"], reverse=True)
                s["hops_records"] = clean[:5]
            else:
                # Migrate the old single hops_record dict → list.
                hr = data.get("hops_record")
                if isinstance(hr, dict) and isinstance(hr.get("hops"), int) and hr.get("name"):
                    s["hops_records"] = [{
                        "hops": hr.get("hops", -1), "name": hr.get("name", ""),
                        "date": hr.get("date", ""), "channel": hr.get("channel", ""),
                    }]
            today = time.strftime("%Y-%m-%d")
            td = data.get("today")
            if isinstance(td, dict) and td.get("date") == today:
                s["today"] = {
                    "date": today,
                    "messages": int(td.get("messages", 0)),
                    "client":   int(td.get("client", 0)),
                    "repeater": int(td.get("repeater", 0)),
                    "server":   int(td.get("server", 0)),
                }
            Domoticz.Log(
                f"Restored stats: {s['messages_total']} msgs / "
                f"{s['adverts_total']} adverts lifetime")
        except Exception as exc:
            Domoticz.Error(f"Could not load meshcore_stats.json: {exc}")

    def _build_stats_payload(self) -> dict:
        """Build and return the stats dict (persisted to plugin dir and pushed
        as ``t:'stats'`` over WebSocket). Caller must NOT hold _rx_log_lock
        when calling this — the method acquires it internally."""
        with self._rx_log_lock:
            payload = dict(self._stats)
            payload["msg_by_sender"]  = dict(self._stats["msg_by_sender"])
            payload["adv_by_sender"]  = dict(self._stats["adv_by_sender"])
            payload["msg_by_channel"] = dict(self._stats["msg_by_channel"])
            payload["hops_records"]   = [dict(r) for r in self._stats["hops_records"]]
            payload["today"]          = dict(self._stats["today"])
            payload["written_at"]    = int(time.time())
        return payload

    def _write_stats(self):
        """Atomically persist lifetime statistics to the plugin directory."""
        dest = self._stats_path()
        payload = self._build_stats_payload()
        try:
            tmp = dest + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            os.replace(tmp, dest)
        except Exception as exc:
            Domoticz.Debug(f"Could not write stats: {exc}")

    def _classify_sender(self, name: str) -> str:
        """client / repeater / server from the contact's MeshCore type.
        Unknown or any other type counts as client (per design)."""
        t = self._node_types.get(name, 0)
        if t == 2:
            return "repeater"
        if t == 3:
            return "server"
        return "client"

    def _pretty_chan(self, tag: str) -> str:
        """Friendly channel label for the hops record. 'P' → Direct (DM);
        'C<idx>' → the channel's name if known (e.g. C0 → Public), else
        '#<idx>'; anything else (already a name like '#test') as-is."""
        if not tag:
            return "?"
        if tag == "P":
            return "Direct (DM)"
        if len(tag) > 1 and tag[0] == "C" and tag[1:].isdigit():
            idx = int(tag[1:])
            return self._channel_names.get(idx) or f"#{idx}"
        return tag

    def _bump_msg_stats(self, sender: str, hops, channel: str):
        cls = self._classify_sender(sender)
        today = time.strftime("%Y-%m-%d")
        when  = time.strftime("%Y-%m-%d %H:%M:%S")
        with self._rx_log_lock:
            s = self._stats
            if s["today"].get("date") != today:
                s["today"] = {"date": today, "messages": 0,
                              "client": 0, "repeater": 0, "server": 0}
            s["messages_total"] += 1
            s["today"]["messages"] += 1
            s[f"{cls}_total"] += 1
            s["today"][cls] += 1
            if sender:
                s["msg_by_sender"][sender] = s["msg_by_sender"].get(sender, 0) + 1
            # Count channel messages by resolved channel name (known channels only).
            # channel is the already-resolved chan_tag from _handle_message:
            #   "P"        → private DM — not a channel
            #   a name     → resolved from _channel_names (known channel)
            #   "C<idx>"   → unresolved index fallback (unknown channel, skip)
            if channel and channel != "P":
                known_names = set(self._channel_names.values())
                if channel in known_names:
                    s["msg_by_channel"][channel] = s["msg_by_channel"].get(channel, 0) + 1
            if isinstance(hops, int) and 0 <= hops < HOPS_SENTINEL and sender:
                chan = self._pretty_chan(channel)
                recs = s["hops_records"]
                ex = next((r for r in recs if r["name"] == sender), None)
                if ex is None:
                    recs.append({"hops": hops, "name": sender,
                                 "date": when, "channel": chan})
                elif hops > ex["hops"]:
                    ex.update(hops=hops, date=when, channel=chan)
                recs.sort(key=lambda r: r["hops"], reverse=True)
                del recs[5:]
            self._stats_dirty = True
            self._ws_stats_dirty = True

    def _heard_path(self) -> str:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "meshcore_heard.json")

    def _load_heard(self):
        """Load the persisted heard-nodes store on startup."""
        path = self._heard_path()
        if not os.path.isfile(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            nodes = data.get("nodes") if isinstance(data, dict) else None
            if isinstance(nodes, dict):
                self._heard_nodes = nodes
                Domoticz.Log(f"Loaded {len(nodes)} heard node(s) from meshcore_heard.json")
            purged = data.get("purged") if isinstance(data, dict) else None
            if isinstance(purged, list):
                self._heard_purged = set(purged)
                if purged:
                    Domoticz.Log(f"Loaded {len(purged)} purged heard pubkey(s)")
        except Exception as exc:
            Domoticz.Error(f"Could not load meshcore_heard.json: {exc}")

    def _build_heard_payload(self) -> dict:
        """Build and return the full heard-nodes dict for file writes.

        Caller must NOT hold _rx_log_lock when calling this.
        """
        with self._rx_log_lock:
            nodes = {k: dict(v) for k, v in self._heard_nodes.items()}
            purged = sorted(self._heard_purged)
        return {"written_at": int(time.time()), "nodes": nodes, "purged": purged}

    def _build_heard_slim_payload(self) -> dict:
        """Slim heard payload for WS push — strips redundant/rarely-needed fields.

        Strips ``pubkey`` (the key is already the pubkey), ``first_heard``, and
        ``node_ts`` from each node value to reduce payload size.  Adds ``total``
        count.  Does NOT include the full purged list.
        Caller must NOT hold _rx_log_lock when calling this.
        """
        _SLIM_SKIP = frozenset(("pubkey", "first_heard", "node_ts"))
        with self._rx_log_lock:
            nodes = {
                pk: {k: v for k, v in h.items() if k not in _SLIM_SKIP}
                for pk, h in self._heard_nodes.items()
            }
            purged = sorted(self._heard_purged)
        return {
            "written_at": int(time.time()),
            "total": len(nodes),
            "nodes": nodes,
            "purged": purged,
        }

    def _write_heard(self):
        """Atomically persist the heard-nodes store to the plugin directory."""
        dest = self._heard_path()
        payload = self._build_heard_payload()
        try:
            tmp = dest + ".tmp"
            with open(tmp, "w") as f:
                json.dump(payload, f)
            os.replace(tmp, dest)
        except Exception as exc:
            Domoticz.Debug(f"Could not write heard nodes: {exc}")

    # ── WebSocket state-push (F2) ─────────────────────────────────────────────

    def _build_snapshot_payload(self) -> dict:
        """Build the lean snapshot payload for the ``t:'snapshot'`` message.

        Only the state needed for the dashboard's first paint is included:
        deviceMap (self + contacts + inbox), stats and channels.  ``heard``
        is deliberately excluded — it can be hundreds of KB (hundreds of
        nodes) and is only shown when the heard panel is opened, so it would
        otherwise block first paint.  It is delivered as a deferred ``heard``
        follow-up frame immediately after the snapshot (see the hello
        handler).

        Also establishes the F7 device-map delta baseline so the first
        ``devices_delta`` after the snapshot has a valid reference to diff
        against.  ``deviceSeq`` carries the CURRENT ``_device_seq`` as the
        baseline marker and intentionally does NOT increment it (unlike the
        rxlog window which does); the first subsequent ``devices_delta`` is
        ``seq == deviceSeq + 1``.  All payloads are built BEFORE the lock
        block so that a failure in a later build cannot leave the baseline
        set without a matching payload having been returned.
        """
        device_map = self._build_device_map_payload()
        stats = self._build_stats_payload()
        channels = {str(k): v for k, v in self._channel_names.items()}
        with self._rx_log_lock:
            self._last_pushed_device_map = copy.deepcopy(device_map)
            seq = self._device_seq
        return {
            "deviceMap": device_map,
            "deviceSeq": seq,
            "stats":     stats,
            "channels":  channels,
        }

    def _push_devices_feed(self):
        """Push the current device map — full ``t:'devices'`` or incremental
        ``t:'devices_delta'`` — mirroring the rxlog full-window / delta pattern.

        Full push path (``_last_pushed_device_map`` is None or diff is the whole
        map): sends ``t:'devices'`` with the complete ``deviceMap``, stores the
        new map as the baseline, and increments ``_device_seq``.

        Delta path: diffs the new map against the stored baseline, emits
        ``t:'devices_delta'`` with only the changed/added node objects, a list
        of removed node names, any changed top-level scalar fields, and the new
        ``seq``.  Then updates the stored baseline.

        Must be called WITHOUT holding ``_rx_log_lock``; acquires it internally
        (mirrors ``_push_rx_log_window`` / ``_push_rx_log_delta``).
        """
        new_map = self._build_device_map_payload()

        with self._rx_log_lock:
            baseline = self._last_pushed_device_map

            if baseline is None:
                # No baseline yet — send a full push and establish it.
                self._device_seq += 1
                seq = self._device_seq
                self._last_pushed_device_map = copy.deepcopy(new_map)
                send_full = True
                delta_payload = None
            else:
                # Compute the diff.
                old_nodes = baseline.get("nodes") or {}
                if not isinstance(old_nodes, dict): old_nodes = {}
                new_nodes = new_map.get("nodes") or {}
                if not isinstance(new_nodes, dict): new_nodes = {}

                changed_nodes = {
                    name: node
                    for name, node in new_nodes.items()
                    if node != old_nodes.get(name)
                }
                removed_nodes = [
                    name for name in old_nodes if name not in new_nodes
                ]
                # Diff scalar (non-'nodes') top-level keys generically.
                changed_scalars = {}
                for key, val in new_map.items():
                    if key == "nodes":
                        continue
                    if val != baseline.get(key):
                        changed_scalars[key] = val
                # Also capture keys present in baseline but absent in new_map.
                for key in baseline:
                    if key == "nodes":
                        continue
                    if key not in new_map:
                        changed_scalars[key] = None

                # Heuristic: if the diff is effectively the entire map, fall
                # back to a full push to avoid the overhead of a delta that
                # carries everything anyway.
                total_nodes = max(len(old_nodes), len(new_nodes), 1)
                is_full_replacement = (
                    len(changed_nodes) + len(removed_nodes) >= total_nodes
                    and len(changed_scalars) >= max(len(new_map) - 1, 1)
                )

                if is_full_replacement:
                    self._device_seq += 1
                    seq = self._device_seq
                    self._last_pushed_device_map = copy.deepcopy(new_map)
                    send_full = True
                    delta_payload = None
                elif changed_nodes or removed_nodes or changed_scalars:
                    self._device_seq += 1
                    seq = self._device_seq
                    self._last_pushed_device_map = copy.deepcopy(new_map)
                    send_full = False
                    delta_payload = {
                        "changed": changed_nodes,
                        "removed": removed_nodes,
                        "scalars": changed_scalars,
                        "seq":     seq,
                    }
                else:
                    # Nothing changed — skip the push entirely.
                    send_full = False
                    delta_payload = None

        # Lock released — safe to call _push (mirrors _push_rx_log_delta pattern).
        if send_full:
            Domoticz.Debug(f"_push_devices_feed: full seq={seq} nodes={len((new_map.get('nodes') or {}))}")
            self._push("devices", {"deviceMap": new_map, "seq": seq})
        elif delta_payload is not None:
            Domoticz.Debug(
                f"_push_devices_feed: delta seq={delta_payload['seq']} "
                f"changed={len(delta_payload['changed'])} "
                f"removed={len(delta_payload['removed'])} "
                f"scalars={list(delta_payload['scalars'].keys())}"
            )
            self._push("devices_delta", delta_payload)

    def _push_dirty_feeds(self):
        """Flush any dirty feed over WebSocket, coalesced to ≤1 push/sec/feed.

        Uses four independent ``_ws_*_dirty`` flags that are separate from the
        file-write dirty flags (``_stats_dirty``, ``_heard_dirty``) so the two
        throttling paths do not interfere with each other.

        Each flag is written from BOTH the worker thread (e.g. ``_on_rx_log``,
        ``_handle_contacts``) AND the main/onHeartbeat thread (e.g.
        ``_write_device_map``, ``_write_channel_names``).  All four flags share
        the same benign race: the read here on the worker loop and the write on
        the main thread are unsynchronised, which can produce at most one extra
        push containing a slightly stale build.  That is acceptable — the next
        dirty event will re-push the up-to-date state.

        Called from the worker loop every iteration (≈1 s cadence).
        """
        now = time.monotonic()
        _PUSH_MIN_INTERVAL = 1.0

        if self._ws_devices_dirty and (now - self._devices_last_push) >= _PUSH_MIN_INTERVAL:
            self._ws_devices_dirty = False
            self._devices_last_push = now
            self._push_devices_feed()


        if self._ws_stats_dirty and (now - self._stats_last_push) >= _PUSH_MIN_INTERVAL:
            self._ws_stats_dirty = False
            self._stats_last_push = now
            self._push("stats", {"stats": self._build_stats_payload()})

        if self._ws_heard_dirty and (now - self._heard_last_push) >= _PUSH_MIN_INTERVAL:
            self._ws_heard_dirty = False
            self._heard_last_push = now
            self._push("heard", {"heard": self._build_heard_slim_payload()})

        if self._ws_channels_dirty and (now - self._channels_last_push) >= _PUSH_MIN_INTERVAL:
            self._ws_channels_dirty = False
            self._channels_last_push = now
            self._push("channels", {"channels": {str(k): v for k, v in self._channel_names.items()}})

    def _remove_custom_page(self):
        plugin_dir    = os.path.dirname(os.path.abspath(__file__))
        domoticz_root = os.path.abspath(os.path.join(plugin_dir, "..", ".."))
        tpl_dir = os.path.join(domoticz_root, "www", "templates")
        fname = "meshcore.html"
        dest = os.path.join(tpl_dir, fname)
        try:
            if os.path.isfile(dest):
                os.remove(dest)
        except Exception as exc:
            Domoticz.Error(f"Failed to remove {fname}: {exc}")
        leaflet_dst = os.path.join(tpl_dir, "leaflet")
        if os.path.isdir(leaflet_dst):
            try:
                shutil.rmtree(leaflet_dst, ignore_errors=True)
            except Exception as exc:
                Domoticz.Debug(f"Failed to remove leaflet dir: {exc}")
        Domoticz.Log("MeshCore dashboard removed.")

    # ── Persistent-connection worker ──────────────────────────────────────────

    def _worker_main(self):
        """Worker thread entry point. Owns an asyncio loop and runs _run() until
        the plugin is stopped. The loop is exposed via self._worker_loop so the
        main thread can schedule sends with asyncio.run_coroutine_threadsafe."""
        Domoticz.Debug("Worker: started")
        loop = asyncio.new_event_loop()
        self._worker_loop = loop
        asyncio.set_event_loop(loop)
        self._stop_async = asyncio.Event()
        self._cmd_lock   = asyncio.Lock()
        self._remote_query_tasks: set = set()
        try:
            self._main_task = loop.create_task(self._run())
            loop.run_until_complete(self._main_task)
        except asyncio.CancelledError:
            Domoticz.Debug("Worker: cancelled (shutdown).")
        except Exception as exc:
            Domoticz.Error(f"Worker: fatal error: {exc}")
            Domoticz.Debug(traceback.format_exc())
        finally:
            Domoticz.Debug("Worker: entering shutdown sequence")
            try:
                for task in asyncio.all_tasks(loop):
                    task.cancel()
                loop.run_until_complete(asyncio.sleep(0))
            except Exception as exc:
                Domoticz.Debug(f"Worker: cancel-tasks step error: {exc}")
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception as exc:
                Domoticz.Debug(f"Worker: shutdown_asyncgens error: {exc}")
            try:
                if hasattr(loop, "shutdown_default_executor"):
                    loop.run_until_complete(loop.shutdown_default_executor())
                else:
                    ex = getattr(loop, "_default_executor", None)
                    if ex is not None:
                        ex.shutdown(wait=True)
            except Exception as exc:
                Domoticz.Debug(f"Worker: executor shutdown error: {exc}")
            # On Windows the ProactorEventLoop owns an IOCP that has its own
            # native completion thread (registered as Dummy-N once it calls
            # back into Python). loop.close() is supposed to release it but
            # in practice the thread can linger; closing the proactor first
            # gives the IOCP a chance to flush before the loop tears down.
            try:
                proactor = getattr(loop, "_proactor", None)
                if proactor is not None:
                    proactor.close()
            except Exception as exc:
                Domoticz.Debug(f"Worker: proactor close error: {exc}")
            try:
                loop.close()
            except Exception as exc:
                Domoticz.Debug(f"Worker: loop close error: {exc}")
            self._worker_loop = None
            Domoticz.Debug("Worker: shutdown sequence complete.")

    async def _wait_or_stop(self, secs: float) -> bool:
        """Sleep up to `secs` seconds, returning True if a stop was signalled.

        Uses an asyncio.Event so onStop can wake us instantly via
        call_soon_threadsafe(self._stop_async.set) — no cancellation needed.
        """
        if self._stop_async is None:
            await asyncio.sleep(secs)
            return self._stop_event.is_set()
        try:
            await asyncio.wait_for(self._stop_async.wait(), timeout=secs)
            return True
        except asyncio.TimeoutError:
            return False

    async def _run(self):
        """Outer loop: connect, serve until disconnect, wait, reconnect."""
        while not self._stop_event.is_set():
            try:
                await self._connect_and_serve()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                Domoticz.Debug(f"Worker: serve error: {exc}")
                Domoticz.Debug(traceback.format_exc())

            if self._stop_event.is_set():
                break

            # Wait RECONNECT_DELAY_S before reconnecting (early-exit on stop).
            if await self._wait_or_stop(RECONNECT_DELAY_S):
                return

    async def _connect_and_serve(self):
        """One connection lifecycle: connect, subscribe, serve, disconnect."""
        endpoint = (f"serial {self.serial_port} @ {self.baud_rate}"
                    if self.transport == "Serial"
                    else f"{self.host}:{self.port}")

        try:
            if self.transport == "Serial":
                mc = await asyncio.wait_for(
                    MeshCore.create_serial(self.serial_port, baudrate=self.baud_rate),
                    timeout=CONNECT_TIMEOUT,
                )
            else:
                mc = await asyncio.wait_for(
                    MeshCore.create_tcp(self.host, self.port),
                    timeout=CONNECT_TIMEOUT,
                )
        except Exception as exc:
            if self._was_connected:
                Domoticz.Error(f"Lost connection to MeshCore ({endpoint}): {exc}. Reconnecting in {RECONNECT_DELAY_S}s.")
                self._was_connected = False
            else:
                Domoticz.Error(f"Could not connect to MeshCore ({endpoint}): {exc}. Retrying in {RECONNECT_DELAY_S}s.")
            return

        if mc is None or not mc.is_connected:
            Domoticz.Error(f"MeshCore ({endpoint}): create returned but not connected. Retrying in {RECONNECT_DELAY_S}s.")
            try:
                await self._disconnect_mc(mc)
            except Exception:
                pass
            return

        self._mc = mc
        if not self._was_connected:
            Domoticz.Status(f"Connected to MeshCore ({endpoint}).")
        self._was_connected = True

        # Subscribe to push events. Callbacks run on this loop; they queue
        # work for the main thread via self._queue (no Domoticz API calls!).
        try:
            mc.subscribe(EventType.CONTACT_MSG_RECV, self._on_contact_msg)
            mc.subscribe(EventType.CHANNEL_MSG_RECV, self._on_channel_msg)
            mc.subscribe(EventType.ADVERTISEMENT,    self._on_advertisement)
            mc.subscribe(EventType.RX_LOG_DATA,      self._on_rx_log)
            mc.subscribe(EventType.ACK,              self._on_ack)
        except Exception as exc:
            Domoticz.Error(f"Worker: subscribe failed: {exc}")

        # ── Initial fetches ───────────────────────────────────────────────
        # Order matters: contacts and channel names must populate
        # self._prefix_to_name / self._channel_names BEFORE any queued
        # message is drained, otherwise _handle_message has no resolution
        # table and stores the raw 12-hex pubkey prefix as the sender. The
        # queue is processed strictly in order on the main thread, so as
        # long as we *enqueue* the contacts payload before the message
        # drain enqueues the messages, resolution wins. That's why the
        # firmware drain + start_auto_message_fetching now run LAST in
        # this block.
        if mc.self_info:
            name = mc.self_info.get("name", "")
            if name:
                self._self_name = name
            self._queue.put(("self_info", dict(mc.self_info)))

        # Device info (fw / build / model) is a single fast query and is shown
        # prominently on the dashboard, so fetch it FIRST — before the slower
        # contacts list and the slot-by-slot channel-name scan — instead of
        # making the user wait ~10 s for it.
        try:
            await self._refresh_device_info(mc)
        except Exception as exc:
            Domoticz.Debug(f"Initial device_info error: {exc}")

        try:
            await self._refresh_contacts(mc, verbose=True)
        except Exception as exc:
            Domoticz.Debug(f"Initial get_contacts error: {exc}")

        if not self._channels_fetched:
            try:
                await self._fetch_channel_names(mc)
                self._channels_fetched = True
            except Exception as exc:
                Domoticz.Debug(f"Initial channel fetch error: {exc}")

        try:
            await self._refresh_flood_scope(mc)
        except Exception as exc:
            Domoticz.Debug(f"Initial flood scope error: {exc}")

        try:
            await self._poll_self_stats(mc)
        except Exception as exc:
            Domoticz.Debug(f"Initial self_stats error: {exc}")

        # Catch up on anything the firmware queued while we were
        # disconnected. Runs AFTER contacts/channels so that
        # _handle_message can resolve pubkey prefixes to contact names —
        # otherwise a DM that arrived while the plugin was offline is
        # stored with the raw 12-hex prefix as its sender and a reply
        # can't address the contact by name.
        try:
            n_missed = await self._drain_push_events(mc)
            if n_missed:
                Domoticz.Log(f"Reconnect catch-up: received {n_missed} message(s) "
                             f"that were queued while disconnected.")
            else:
                Domoticz.Log("Reconnect catch-up: no messages were missed.")
        except Exception as exc:
            Domoticz.Debug(f"Worker: connect-time drain error: {exc}")

        # Keep draining on every MESSAGES_WAITING signal for the life of
        # this connection. start_auto_message_fetching() also does one
        # immediate get_msg() (harmless — the drain above already emptied
        # the queue, and the _handle_message signature de-dup collapses
        # any redelivery, so this no longer causes duplicate inbox
        # entries). Re-armed on every reconnect (bound to this mc);
        # mc.disconnect() tears it down. Started LAST so its built-in
        # immediate get_msg() also runs against a populated contacts map.
        try:
            await mc.start_auto_message_fetching()
        except Exception as exc:
            Domoticz.Error(f"Worker: start_auto_message_fetching failed: {exc}")

        # ── Serve loop ────────────────────────────────────────────────────
        # Wrapped in try/finally so the disconnect always runs — including
        # when the stop event triggers an early return. This is what lets
        # serial_asyncio_fast's executor tasks (open/close) drain so the
        # default thread pool can shut down cleanly on plugin stop.
        try:
            last_stats      = time.monotonic()
            last_contacts   = time.monotonic()
            last_msg_drain  = time.monotonic()   # connect-time drain just ran
            last_rx_write   = 0.0
            last_elev_prune = time.monotonic()
            last_ts_prune   = time.monotonic()

            while not self._stop_event.is_set():
                stopped = await self._wait_or_stop(1.0)
                if stopped:
                    break
                if not mc.is_connected:
                    Domoticz.Error("Worker: connection lost, will reconnect.")
                    break

                now = time.monotonic()

                # Periodic refreshes also acquire the command lock so they
                # don't interleave with user-initiated sends (which would
                # trip the meshcore library's event-subscription race).
                async def _locked(coro):
                    if self._cmd_lock is None:
                        await coro
                        return
                    async with self._cmd_lock:
                        await coro

                if now - last_stats >= STATS_REFRESH_S:
                    last_stats = now
                    try:
                        await _locked(self._refresh_flood_scope(mc))
                    except Exception as exc:
                        Domoticz.Debug(f"Periodic flood_scope error: {exc}")
                    try:
                        await _locked(self._refresh_device_info(mc))
                    except Exception as exc:
                        Domoticz.Debug(f"Periodic device_info error: {exc}")
                    try:
                        await _locked(self._poll_self_stats(mc))
                    except Exception as exc:
                        Domoticz.Debug(f"Periodic self_stats error: {exc}")

                if now - last_contacts >= CONTACTS_REFRESH_S:
                    last_contacts = now
                    try:
                        await _locked(self._refresh_contacts(mc))
                    except Exception as exc:
                        Domoticz.Debug(f"Periodic contacts error: {exc}")

                # Safety-net message drain. start_auto_message_fetching()
                # only pulls on MESSAGES_WAITING / unsolicited push; some
                # firmware emits neither, so without this the node's queue
                # grows unbounded and the plugin silently misses everything
                # until reconnect. Polling get_msg() guarantees delivery.
                if now - last_msg_drain >= MSG_DRAIN_S:
                    last_msg_drain = now
                    try:
                        await _locked(self._drain_push_events(mc))
                    except Exception as exc:
                        Domoticz.Debug(f"Periodic msg drain error: {exc}")

                if self._rx_log_dirty and (now - last_rx_write) >= RX_LOG_WRITE_S:
                    last_rx_write = now
                    self._rx_log_dirty = False
                    # Write directly from the worker thread. _write_rx_log is
                    # pure file I/O + dict copies under self._rx_log_lock; it
                    # doesn't touch the Domoticz API, so it's safe off the
                    # main thread and lets the dashboard see fresh data
                    # without waiting for the next heartbeat tick.
                    self._write_rx_log()
                    # F3: push delta/window over WebSocket if subscriber wants rxlog.
                    with self._rx_log_lock:
                        _want_rxlog = self._sub_feeds == "rxlog"
                    if _want_rxlog:
                        try:
                            self._push_rx_log_delta()
                        except Exception as exc:
                            Domoticz.Debug(f"_push_rx_log_delta error: {exc}")
                    if self._heard_dirty:
                        self._heard_dirty = False
                        self._write_heard()
                    if self._stats_dirty:
                        self._stats_dirty = False
                        self._write_stats()

                # Push any dirty feeds to connected WebSocket clients (F2).
                # Runs every loop iteration; _push_dirty_feeds coalesces to
                # ≤1 push/sec/feed using per-feed last-push timestamps.
                self._push_dirty_feeds()

                # LRU eviction for the elevation cache (every 5 min).
                if now - last_elev_prune >= self._ELEV_PRUNE_INTERVAL:
                    last_elev_prune = now
                    try:
                        await asyncio.get_event_loop().run_in_executor(
                            None, self._elev_prune
                        )
                    except Exception as exc:
                        Domoticz.Debug(f"Periodic elev_prune error: {exc}")
                # Prune old time-series rows (every 5 min, same cadence).
                if now - last_ts_prune >= self._ELEV_PRUNE_INTERVAL:
                    last_ts_prune = now
                    try:
                        await asyncio.get_event_loop().run_in_executor(
                            None, self._ts_prune
                        )
                    except Exception as exc:
                        Domoticz.Debug(f"Periodic ts_prune error: {exc}")
        finally:
            try:
                self._write_heard()
            except Exception:
                pass
            try:
                self._write_stats()
            except Exception:
                pass
            try:
                await self._disconnect_mc(mc)
            except Exception:
                pass
            self._mc = None
            if not self._stop_event.is_set():
                self._was_connected = False
                # Propagate connection loss to the Domoticz STATUS device so
                # the dashboard's connected-node badge goes offline.  Must not
                # call Domoticz API here (worker thread) — queue for main thread.
                self._queue.put(("self_offline", {}))

    # ── Push-event callbacks (run on worker loop) ────────────────────────────

    def _on_contact_msg(self, ev):
        self._queue.put(("message", dict(ev.payload or {})))

    def _on_channel_msg(self, ev):
        self._queue.put(("message", dict(ev.payload or {})))

    def _on_advertisement(self, ev):
        self._queue.put(("advert", dict(ev.payload or {})))

    def _on_ack(self, ev):
        """Acknowledgement from a remote node for one of our outbound TEXT_MSGs.

        The ACK event carries a "code" attribute (8 hex chars) that exactly
        matches the expected_ack returned by send_msg's MSG_SENT event.
        We use this code as the correlation key to annotate the corresponding
        outgoing DM line in the inbox with a delivery marker.

        Correlation strategy: exact match on expected_ack hex code.
        Limitation: if the expected_ack bytes object returned by send_msg was
        not captured (older firmware, or a send that failed before MSG_SENT),
        no pending record exists and the ACK is simply recorded in the rx-log
        without annotation.  We never annotate a line we can't reliably match.
        """
        p = dict(ev.payload or {})
        t = time.time()
        ack_code = p.get("code", "")   # 8-char hex string from the ACK frame
        matched_rec = None
        deferred = False
        with self._rx_log_lock:
            rec = self._pending_acks.get(ack_code) if ack_code else None
            if rec is not None:
                if rec.get("inbox_line") or rec.get("msg_rowid") is not None:
                    # send_result already reconciled this record (we know the
                    # stored row / inbox line) — safe to consume and annotate.
                    matched_rec = self._pending_acks.pop(ack_code)
                else:
                    # The ACK beat the send_result drain (fast multi-hop reply,
                    # e.g. a repeater answering in ~1 s while send_result is
                    # still queued for the next heartbeat). Don't discard the
                    # correlation: flag it delivered and keep it so the
                    # back-fill path emits the ack_result the moment it learns
                    # the row id — otherwise a delivered message never shows
                    # the "delivered" tick.
                    rec["delivered"] = True
                    rec["acked_at"] = t
                    deferred = True
        _dbg(f"_on_ack: code={ack_code!r} matched={matched_rec is not None} "
             f"deferred={deferred} pending_codes={list(self._pending_acks.keys())}")
        # Visible info-level log so users can see ACK delivery without having
        # to enable debug. Three outcomes:
        #   - matched: ACK consumed and annotated on the inbox row
        #   - deferred: matched but send_result hadn't reconciled yet — the
        #     back-fill path will emit the ack_result once the row id lands
        #   - unmatched: ACK arrived but no expected_ack was captured for it
        #     (older firmware path, or ACK for someone else's send)
        if matched_rec:
            Domoticz.Log(
                f"ACK delivered (code={ack_code}) -> "
                f"target={matched_rec.get('target') or '?'}"
            )
            self._queue.put(("ack_result", {
                "ack_code":   ack_code,
                "delivered":  True,
                "target":     matched_rec.get("target", ""),
                "body":       matched_rec.get("body", ""),
                "out_ts":     matched_rec.get("out_ts", 0),
                "inbox_line": matched_rec.get("inbox_line"),
                "dm_name":    matched_rec.get("dm_name"),
                "msg_rowid":  matched_rec.get("msg_rowid"),
            }))
        elif deferred:
            Domoticz.Log(
                f"ACK delivered (code={ack_code}) -> deferred annotation, "
                f"waiting for send_result to reconcile the inbox row"
            )
        else:
            Domoticz.Log(
                f"ACK received (code={ack_code or '?'}) but no matching "
                f"outgoing message — recipient may have ACKed a send from "
                f"another client, or the expected_ack was not captured."
            )
        synth = {
            "payload_typename": "ACK",
            "payload_type":     -1,
            "route_typename":   "",
            "snr":              p.get("SNR"),
            "rssi":             p.get("rssi"),
            "recv_time":        int(t),
            "message":          "(acknowledgement)",
            "raw_hex":          "",
            "_t":               t,
        }
        # Preserve any library-supplied identifying field (pkt_hash, code, etc.)
        for k in ("pkt_hash", "code", "ack_code", "request_hash"):
            if k in p:
                synth[k] = p[k]
        with self._rx_log_lock:
            self._rx_log.append(synth)
            self._rx_log_total_appended += 1
            self._payload_type_counts["ACK"] = self._payload_type_counts.get("ACK", 0) + 1
            self._packet_times.append(t)
        self._rx_log_dirty = True
        Domoticz.Debug(f"ACK received: {p}")

    def _on_rx_log(self, ev):
        """Record an RX_LOG_DATA event in the rolling buffer + aggregates."""
        p = dict(ev.payload or {})
        # Normalize: timestamps, bytes → hex, enum → name
        t = time.time()
        p["_t"] = t
        # Convert bytes-like fields to hex strings for JSON serialization
        for k, v in list(p.items()):
            if isinstance(v, (bytes, bytearray)):
                p[k] = v.hex()
            elif hasattr(v, "name") and hasattr(v, "value"):  # IntEnum
                p[k] = v.name
        with self._rx_log_lock:
            self._rx_log.append(p)
            self._rx_log_total_appended += 1
            # Aggregates
            pt = p.get("payload_typename") or str(p.get("payload_type", ""))
            if pt:
                self._payload_type_counts[pt] = self._payload_type_counts.get(pt, 0) + 1
            ch = (p.get("chan_hash") or "").lower() or None
            if ch:
                p["chan_hash"] = ch
                self._chan_hash_counts[ch] = self._chan_hash_counts.get(ch, 0) + 1
            self._packet_times.append(t)
            # Per-contact signal history. We key by the first 12 hex chars of
            # the originating contact's public key (a.k.a. pubkey_prefix), so
            # dashboard cards can pull SNR/RSSI history by looking the contact
            # up in the device map. ADVERT carries adv_key directly. For other
            # payload types we can't recover the originating contact from the
            # RX frame alone (the destination is hashed), so they don't show
            # up here — incoming messages will add their own samples via the
            # main-thread _handle_message hook.
            snr  = p.get("snr")
            rssi = p.get("rssi")
            pp   = p.get("path") or ""
            adv_key = p.get("adv_key")
            if p.get("payload_typename") == "ADVERT":
                # Tally every advert we hear here — this RX_LOG frame is the
                # canonical source (the heard-nodes store is built from it
                # too). The higher-level ADVERTISEMENT push is a decoded
                # duplicate of the same on-air frame and some firmware never
                # emits it, so counting it there missed everything. We hold
                # _rx_log_lock already, so increment inline.
                self._stats["adverts_total"] += 1
                _an = (p.get("adv_name") or "").strip()
                if _an:
                    self._stats["adv_by_sender"][_an] = \
                        self._stats["adv_by_sender"].get(_an, 0) + 1
                self._stats_dirty = True
                self._ws_stats_dirty = True
            if p.get("payload_typename") == "ADVERT" and adv_key and (snr is not None or rssi is not None):
                prefix = adv_key[:12]
                hist = self._signal_history.setdefault(prefix, [])
                hist.append({"t": t, "snr": snr, "rssi": rssi, "path_len": p.get("path_len", -1), "kind": "ADVERT"})
                if len(hist) > 60:
                    del hist[: len(hist) - 60]
                # If this advert is from a node that IS a contact, remember
                # its latest signal so the contact card can show hops/SNR/
                # RSSI even without a device, a message, or a direct path.
                if adv_key in self._known_pubkeys:
                    self._contact_sig[adv_key[:12]] = {
                        "snr": snr, "rssi": rssi,
                        "path_len": p.get("path_len", -1),
                        "t": t, "source": "advert",
                    }
                    # Trustworthy clock-skew sample: the node's advertised RTC
                    # vs OUR receive time of this very advert (captured
                    # together — the only valid comparison).
                    _ats = p.get("adv_timestamp")
                    if _ats:
                        self._contact_clock[adv_key[:12]] = {
                            "node_ts": int(_ats), "our_ts": int(t),
                        }
            # Persistent heard-nodes store: ADVERTs from nodes that are NOT
            # already contacts. If it's a contact, the contacts poll tracks
            # it — skip. If already heard, just refresh last_heard + signal.
            if (p.get("payload_typename") == "ADVERT" and adv_key
                    and adv_key not in self._known_pubkeys
                    and adv_key not in self._heard_purged):
                h = self._heard_nodes.get(adv_key)
                if h is None:
                    h = {"pubkey": adv_key, "first_heard": t, "count": 0}
                    self._heard_nodes[adv_key] = h
                h["name"]      = p.get("adv_name") or h.get("name", "")
                h["type"]      = p.get("adv_type", h.get("type", 0))
                lat, lon = p.get("adv_lat"), p.get("adv_lon")
                if lat or lon:
                    h["lat"], h["lon"] = lat, lon
                h["snr"]       = snr
                h["rssi"]      = rssi
                h["path_len"]  = p.get("path_len", -1)
                h["count"]     = (h.get("count") or 0) + 1
                # Feed advert hops into the lifetime hops records too — a
                # far node is usually learned via its advert, not a chat
                # message. Inline (we already hold _rx_log_lock; calling
                # _bump_msg_stats would re-enter the non-reentrant lock).
                _pl = p.get("path_len", -1)
                if isinstance(_pl, int) and 0 <= _pl < HOPS_SENTINEL:
                    _nm = h.get("name") or adv_key[:12]
                    _recs = self._stats["hops_records"]
                    _ex = next((r for r in _recs if r["name"] == _nm), None)
                    _when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))
                    if _ex is None:
                        _recs.append({"hops": _pl, "name": _nm,
                                      "date": _when, "channel": "Advert"})
                    elif _pl > _ex["hops"]:
                        _ex.update(hops=_pl, date=_when, channel="Advert")
                    _recs.sort(key=lambda r: r["hops"], reverse=True)
                    del _recs[5:]
                    self._stats_dirty = True
                    self._ws_stats_dirty = True
                h["last_heard"] = t          # OUR local receive time
                # The node's own advertised clock — used by the dashboard to
                # flag a wrong RTC (compared against last_heard).
                if p.get("adv_timestamp"):
                    h["node_ts"] = p.get("adv_timestamp")
                self._heard_dirty = True
                self._ws_heard_dirty = True
            # Duplicate-flood detection: keep last few timestamps per raw_hex
            raw = p.get("raw_hex")
            if raw and p.get("route_typename") in ("TC_FLOOD", "FLOOD"):
                dl = self._dup_floods.setdefault(raw, [])
                dl.append({"t": t, "path": pp, "snr": snr})
                if len(dl) > 8:
                    del dl[: len(dl) - 8]
        # Time-series ingestion for analytics panels.
        # Use adv_key for ADVERT frames; leave node_key=None for other types
        # (we can't reliably identify the originating contact from the RX frame).
        ts_src    = p.get("payload_typename", "rx").lower()
        ts_nk     = None
        ts_snr    = p.get("snr")
        ts_rssi   = p.get("rssi")
        ts_pl     = p.get("path_len")
        if p.get("payload_typename") == "ADVERT":
            ak = p.get("adv_key")
            if ak:
                ts_nk = ak[:12]
            ts_src = "adv"
        if ts_snr is not None or ts_rssi is not None:
            self._ts_ingest(ts_src, node_key=ts_nk,
                            rssi=ts_rssi, snr=ts_snr, path_len=ts_pl)
        # Hop histogram from RX_LOG frames with a valid path_len.
        if isinstance(ts_pl, int) and 0 <= ts_pl < HOPS_SENTINEL:
            self._ts_hops_record(int(t), ts_pl)
        # Relay-key tally: walk the path string one hop token at a time
        # (token width follows the node's path_hash_mode: 2/4/6 hex chars).
        if pp:
            clean_path = re.sub(r"[^0-9a-fA-F]", "", pp).lower()
            tok = self._path_token_chars()
            for i in range(0, len(clean_path) - tok + 1, tok):
                rb = clean_path[i:i + tok]
                self._ts_relay_observed(rb, name=self._resolve_relay_key(rb))
        self._rx_log_dirty = True

    async def _refresh_contacts(self, mc, verbose=False):
        """Issue get_contacts and post the snapshot to the main thread."""
        log = Domoticz.Log if verbose else Domoticz.Debug
        log("Fetching contact list from node...")
        t0 = time.monotonic()
        for attempt in range(3):
            try:
                await asyncio.wait_for(mc.commands.get_contacts(), timeout=COMMAND_TIMEOUT)
            except asyncio.TimeoutError:
                Domoticz.Log(f"get_contacts timed out (attempt {attempt + 1}), retrying…")
            except Exception as exc:
                Domoticz.Debug(f"get_contacts error (attempt {attempt + 1}): {exc}")
            await asyncio.sleep(0)
            if mc.contacts:
                break
            await asyncio.sleep(1)
        elapsed = time.monotonic() - t0
        if mc.contacts:
            log(f"Contact list received: {len(mc.contacts)} contact(s) in {elapsed:.1f}s")
            self._queue.put(("contacts", {k: dict(v) for k, v in mc.contacts.items()}))
        else:
            Domoticz.Log(f"Contact list empty after {elapsed:.1f}s (node may have no contacts yet)")

    async def _refresh_flood_scope(self, mc):
        r = await asyncio.wait_for(mc.commands.get_default_flood_scope(), timeout=5.0)
        if r and r.type == EventType.DEFAULT_FLOOD_SCOPE:
            payload = r.payload or {}
            scope_name = payload.get("scope_name", "") or ""
            scope_key  = payload.get("scope_key", "")  or ""
            # The firmware doesn't reliably zero-pad the 31-byte scope_name
            # buffer when overwriting, so leftover bytes from a previous scope
            # bleed back through the meshcore library's NUL-stripping decode.
            # scope_key is the source of truth: it's the sha256-derived
            # routing key. If it's all zeros, the scope is actually empty
            # (global flood). Otherwise trim scope_name to the first run of
            # valid scope characters.
            if scope_key and set(scope_key) <= {"0"}:
                cleaned = ""
            else:
                m = re.match(r"^([#A-Za-z0-9_\-]+)", scope_name)
                cleaned = m.group(1) if m else ""
            if cleaned != scope_name:
                Domoticz.Debug(
                    f"Flood scope sanitised: raw_name={scope_name!r} "
                    f"key_zero={scope_key and set(scope_key) <= {'0'}} cleaned={cleaned!r}"
                )
            self._queue.put(("flood_scope", cleaned))

    async def _refresh_device_info(self, mc):
        r = await asyncio.wait_for(mc.commands.send_device_query(), timeout=5.0)
        if r and r.type == EventType.DEVICE_INFO:
            self._queue.put(("device_info", dict(r.payload or {})))

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
            Domoticz.Log(f"Fetched {fetched} pending message(s) from device - added to inbox.")
        return fetched

    async def _poll_self_stats(self, mc):
        """Poll all available stats from the connected node itself."""
        Domoticz.Debug("Polling self-node stats...")

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
        """Query all channel slots and record every one (including empty).

        The device map exposes the full slot table so the dashboard can render
        every slot with an Add/Remove control. The legacy meshcore_channels.json
        only contains non-empty entries for backwards compatibility with the
        existing channel resolver.
        """
        channel_names = {}        # non-empty only — used by message routing
        all_slots: dict = {}      # idx → name ("" if empty) for all slots
        chan_hash_to_name: dict = {}  # 2-hex chan_hash → channel_name (non-empty slots)
        # Stop probing after this many consecutive empty / error slots so a
        # 40-slot scan doesn't burn dozens of round-trips on sparse devices.
        consecutive_empty = 0
        EMPTY_RUN_STOP = 4
        for idx in range(MAX_CHANNEL_SLOTS):
            try:
                res = await asyncio.wait_for(mc.commands.get_channel(idx), timeout=5.0)
                Domoticz.Debug(f"get_channel({idx}): type={res.type if res else None} payload={res.payload if res else None}")
                if res and res.type == EventType.CHANNEL_INFO:
                    # Trust the index the firmware reports in the response, NOT
                    # the loop variable. Under a slow link a late CHANNEL_INFO
                    # can be matched to a later get_channel() wait, so keying
                    # off `idx` shifts/drops channels. channel_idx is
                    # authoritative (reader.py decodes it from the response).
                    rep_idx = res.payload.get("channel_idx", idx)
                    name = res.payload.get("channel_name", "").strip("\x00").strip()
                    all_slots[rep_idx] = name
                    if name:
                        channel_names[str(rep_idx)] = name
                        consecutive_empty = 0
                        # Map the on-air 1-byte hash to the channel name so the
                        # dashboard can resolve "Hashes heard on air" rows for
                        # configured channels.
                        ch_hash = (res.payload.get("channel_hash") or "").lower()
                        if ch_hash:
                            chan_hash_to_name[ch_hash] = name
                    else:
                        consecutive_empty += 1
                elif res and res.type == EventType.ERROR:
                    # ERROR usually means firmware reports no such slot —
                    # record the remaining slots as empty and stop probing.
                    for j in range(idx, MAX_CHANNEL_SLOTS):
                        all_slots.setdefault(j, "")
                    break
                else:
                    consecutive_empty += 1
                if consecutive_empty >= EMPTY_RUN_STOP:
                    for j in range(idx + 1, MAX_CHANNEL_SLOTS):
                        all_slots.setdefault(j, "")
                    break
            except asyncio.TimeoutError:
                Domoticz.Debug(f"get_channel({idx}) timed out - assume empty")
                all_slots[idx] = ""
                continue
            except Exception as exc:
                Domoticz.Debug(f"get_channel({idx}) error: {exc} - assume empty")
                all_slots[idx] = ""
                continue
        # Ensure full coverage
        for j in range(MAX_CHANNEL_SLOTS):
            all_slots.setdefault(j, "")
        with self._rx_log_lock:
            self._channel_slots = all_slots
            self._channel_names = {int(k): v for k, v in channel_names.items()}
            self._chan_hash_to_name = chan_hash_to_name
        if channel_names:
            parts = [f"#{k} = {v}" for k, v in sorted(channel_names.items())]
            Domoticz.Log(f"MeshCore channels: {', '.join(parts)}")
        else:
            Domoticz.Debug("No channel names found on device.")
        self._write_channel_names(channel_names)

    async def _run_remote_query(self, verb: str, name: str, coro, kind: str):
        """Run a req_*_sync call as a detached task and queue the result.

        Detaches the slow remote round-trip from the main send pipeline so an
        unresponsive contact can't stall other sends or periodic pollers.
        Tasks are bounded to 30s. Held under the global command lock to avoid
        the meshcore library's per-call event subscription race.
        """
        lock = self._cmd_lock
        try:
            if lock is not None:
                async with lock:
                    r = await asyncio.wait_for(coro, timeout=30.0)
            else:
                r = await asyncio.wait_for(coro, timeout=30.0)
            ok = r is not None and getattr(r, "type", None) != EventType.ERROR
            if ok:
                # The sync helpers may return either an Event (has .payload)
                # or a raw list/dict (telemetry/neighbours). Don't touch
                # r.payload unless r actually has it.
                if hasattr(r, "payload"):
                    p = r.payload
                    payload = dict(p) if isinstance(p, dict) else (p if p is not None else None)
                elif isinstance(r, (list, dict)):
                    payload = r
                else:
                    payload = None
                self._queue.put((kind, {"name": name, "data": payload}))
            self._queue.put(("send_result", {
                "ok": ok, "target": verb, "body": name,
                "result": "received" if ok else "no response from remote",
            }))
        except asyncio.TimeoutError:
            self._queue.put(("send_result", {
                "ok": False, "target": verb, "body": name,
                "result": "timeout - remote did not respond within 30s",
            }))
        except Exception as exc:
            self._queue.put(("send_result", {
                "ok": False, "target": verb, "body": name, "result": str(exc),
            }))

    async def _send_message_for_text(self, text: str, req_id=None):
        """Wrapper invoked via run_coroutine_threadsafe by onWebSocketMessage.

        Reads the live mc instance inside the worker loop and short-circuits
        with a friendly result if we are mid-reconnect — the alternative is
        a stale mc reference that would error confusingly inside the library.

        Holds the global command lock so concurrent rapid-fire sends (e.g.
        applying a preset that issues 4 verbs back-to-back) don't trip the
        meshcore library's per-call event-subscription race condition.

        req_id is the correlation id from the inbound {t:'cmd',id:...} frame;
        it is threaded through to every send_result put so _dispatch can echo
        it back in the cmd_result WebSocket reply.
        """
        mc = self._mc
        if mc is None or not getattr(mc, "is_connected", False):
            self._queue.put(("send_result", {
                "ok": False, "target": "?", "body": text,
                "result": "not connected - auto-reconnect in progress",
                "id": req_id,
            }))
            return
        lock = self._cmd_lock
        if lock is None:
            await self._send_message(mc, text, req_id)
            return
        async with lock:
            await self._send_message(mc, text, req_id)

    async def _send_message(self, mc, text: str, req_id=None):
        """Send a message dispatched via onWebSocketMessage.

        Syntax accepted:
          "hello world"          → direct message to the first tracked node
          "garden: hello"        → direct message to the node named 'garden'
          "#0: hello"            → broadcast on channel index 0
          "#General: hello"      → broadcast on the channel named 'General'
          "#flood: hello"        → broadcast on channel 0 (alias)
          "!remove <name>"       → remove the named contact from the device
        """
        # Note: "!favorite ..." is intentionally NOT handled here — it is consumed
        # locally by _handle_local_only_command() without opening an MC session,
        # since the operation only touches plugin state.

        # ── Remote-contact verbs ─────────────────────────────────────────
        # Helper: resolve a contact dict by adv_name from mc.contacts.
        def _resolve_contact(name):
            matches = [c for c in mc.contacts.values()
                       if c.get("adv_name", "").strip() == name]
            if len(matches) > 1:
                Domoticz.Debug(
                    f"_resolve_contact({name!r}): {len(matches)} contacts share this "
                    f"adv_name — first match wins; pubkey={matches[0].get('public_key','?')[:12]}"
                )
            return dict(matches[0]) if matches else None

        # !reset_path <contact name>
        if text.startswith("!reset_path "):
            name = text[len("!reset_path "):].strip()
            contact = _resolve_contact(name)
            if contact is None:
                self._queue.put(("send_result", {
                    "ok": False, "target": "!reset_path", "body": name,
                    "result": f"contact '{name}' not found",
                    "id": req_id,
                }))
                return
            try:
                # reset_path takes a pubkey-like key
                pk = bytes.fromhex(contact.get("public_key", ""))
                r = await asyncio.wait_for(mc.commands.reset_path(pk), timeout=8.0)
                ok = r is not None and r.type == EventType.OK
                if ok:
                    Domoticz.Status(f"Path reset for '{name}'")
                self._queue.put(("send_result", {
                    "ok": ok, "target": "!reset_path", "body": name,
                    "result": "applied" if ok else str(r),
                
                    "id": req_id,
                }))
            except Exception as exc:
                self._queue.put(("send_result", {
                    "ok": False, "target": "!reset_path", "body": name, "result": str(exc),
                
                    "id": req_id,
                }))
            return

        # !req_status / !req_telemetry / !req_neighbours <contact name>
        # Remote queries can block for tens of seconds while waiting for the
        # remote node to respond. We run them as detached asyncio tasks so a
        # slow / offline contact doesn't pin the worker loop and stall other
        # sends or the periodic pollers.
        for verb, fn_name, kind, ok_event_only in (
            ("!req_status ",     "req_status_sync",     "contact_status",     False),
            ("!req_telemetry ",  "req_telemetry_sync",  "contact_telemetry",  False),
            ("!req_neighbours ", "req_neighbours_sync", "contact_neighbours", False),
        ):
            if text.startswith(verb):
                name = text[len(verb):].strip()
                contact = _resolve_contact(name)
                if contact is None:
                    self._queue.put(("send_result", {
                        "ok": False, "target": verb.strip(), "body": name,
                        "result": f"contact '{name}' not found",
                        "id": req_id,
                    }))
                    return
                # Hold a strong reference so the GC doesn't finalise a still-
                # pending task with "Task was destroyed but it is pending".
                _task = asyncio.create_task(self._run_remote_query(
                    verb.strip(), name, getattr(mc.commands, fn_name)(contact), kind
                ))
                self._remote_query_tasks.add(_task)
                _task.add_done_callback(self._remote_query_tasks.discard)
                # Immediate optimistic ack so the UI doesn't sit on a spinner
                self._queue.put(("send_result", {
                    "ok": True, "target": verb.strip(), "body": name,
                    "result": "querying (up to 30s)",
                
                    "id": req_id,
                }))
                return

        # ── Self-node verbs ──────────────────────────────────────────────
        # !send_advert [direct|flood]   default flood
        if text.startswith("!send_advert"):
            arg = text[len("!send_advert"):].strip().lower()
            flood = arg != "direct"   # anything other than "direct" → flood
            try:
                r = await asyncio.wait_for(mc.commands.send_advert(flood=flood), timeout=8.0)
                ok = r is not None and r.type == EventType.OK
                if ok:
                    Domoticz.Log(f"Advertisement sent ({'flood' if flood else 'direct'}).")
                self._queue.put(("send_result", {
                    "ok": ok, "target": "!send_advert",
                    "body": "flood" if flood else "direct",
                    "result": "applied" if ok else str(r),
                
                    "id": req_id,
                }))
            except Exception as exc:
                self._queue.put(("send_result", {
                    "ok": False, "target": "!send_advert", "body": arg, "result": str(exc),
                
                    "id": req_id,
                }))
            return

        # !set_radio <freq_MHz> <bw_kHz> <sf 7-12> <cr 5-8>
        if text.startswith("!set_radio"):
            parts = text.split()
            if len(parts) != 5:
                self._queue.put(("send_result", {
                    "ok": False, "target": "!set_radio", "body": text,
                    "result": f"syntax: !set_radio <freq MHz> <bw kHz> <sf {RADIO_SF_MIN}-{RADIO_SF_MAX}> <cr {RADIO_CR_MIN}-{RADIO_CR_MAX}>",
                    "id": req_id,
                }))
                return
            try:
                freq, bw, sf, cr = float(parts[1]), float(parts[2]), int(parts[3]), int(parts[4])
            except ValueError as exc:
                self._queue.put(("send_result", {
                    "ok": False, "target": "!set_radio", "body": text, "result": f"parse error: {exc}",
                    "id": req_id,
                }))
                return
            # Sanity bounds — see module-level RADIO_* constants.
            if not (RADIO_FREQ_MIN_MHZ <= freq <= RADIO_FREQ_MAX_MHZ):
                self._queue.put(("send_result", {
                    "ok": False, "target": "!set_radio", "body": text,
                    "result": f"freq must be {RADIO_FREQ_MIN_MHZ:.0f}-{RADIO_FREQ_MAX_MHZ:.0f} MHz",
                    "id": req_id,
                }))
                return
            if not (RADIO_BW_MIN_KHZ <= bw <= RADIO_BW_MAX_KHZ):
                self._queue.put(("send_result", {
                    "ok": False, "target": "!set_radio", "body": text,
                    "result": f"bw must be {RADIO_BW_MIN_KHZ:.0f}-{RADIO_BW_MAX_KHZ:.0f} kHz",
                    "id": req_id,
                }))
                return
            if not (RADIO_SF_MIN <= sf <= RADIO_SF_MAX):
                self._queue.put(("send_result", {
                    "ok": False, "target": "!set_radio", "body": text,
                    "result": f"sf must be {RADIO_SF_MIN}-{RADIO_SF_MAX}",
                    "id": req_id,
                }))
                return
            if not (RADIO_CR_MIN <= cr <= RADIO_CR_MAX):
                self._queue.put(("send_result", {
                    "ok": False, "target": "!set_radio", "body": text,
                    "result": f"cr must be {RADIO_CR_MIN}-{RADIO_CR_MAX} (=4/5..4/8)",
                    "id": req_id,
                }))
                return
            try:
                r = await asyncio.wait_for(mc.commands.set_radio(freq, bw, sf, cr), timeout=8.0)
                ok = r is not None and r.type == EventType.OK
                if ok:
                    Domoticz.Log(f"Radio set: freq={freq} MHz bw={bw} kHz sf={sf} cr={cr}")
                    # Optimistically update local snapshot — firmware will re-emit
                    # SELF_INFO on next connect / advert anyway. Match the wire
                    # types firmware uses so equality diffs don't false-positive
                    # (freq/bw are reported as ints when integer-valued).
                    self._self_info_full["radio_freq"] = int(freq) if freq == int(freq) else freq
                    self._self_info_full["radio_bw"]   = int(bw)   if bw   == int(bw)   else bw
                    self._self_info_full["radio_sf"]   = sf
                    self._self_info_full["radio_cr"]   = cr
                    self._write_device_map()
                self._queue.put(("send_result", {
                    "ok": ok, "target": "!set_radio",
                    "body": f"{freq}/{bw}/sf{sf}/cr{cr}",
                    "result": "applied" if ok else str(r),
                
                    "id": req_id,
                }))
            except Exception as exc:
                self._queue.put(("send_result", {
                    "ok": False, "target": "!set_radio", "body": text, "result": str(exc),
                
                    "id": req_id,
                }))
            return

        # !set_tx_power <dBm>
        if text.startswith("!set_tx_power"):
            parts = text.split()
            if len(parts) != 2:
                self._queue.put(("send_result", {
                    "ok": False, "target": "!set_tx_power", "body": text,
                    "result": "syntax: !set_tx_power <dBm>",
                    "id": req_id,
                }))
                return
            try:
                p = int(parts[1])
            except ValueError as exc:
                self._queue.put(("send_result", {
                    "ok": False, "target": "!set_tx_power", "body": text, "result": str(exc),
                
                    "id": req_id,
                }))
                return
            # Clamp against the firmware-reported maximum to avoid bricking
            # the radio at hardware-illegal levels. Fall back to a generous
            # 22 dBm ceiling when max isn't known yet.
            max_tx = int(self._self_info_full.get("max_tx_power",
                                                   RADIO_TX_POWER_DEFAULT_MAX_DBM)
                         or RADIO_TX_POWER_DEFAULT_MAX_DBM)
            if not (0 <= p <= max_tx):
                self._queue.put(("send_result", {
                    "ok": False, "target": "!set_tx_power", "body": str(p),
                    "result": f"tx_power must be 0-{max_tx} dBm",
                
                    "id": req_id,
                }))
                return
            try:
                r = await asyncio.wait_for(mc.commands.set_tx_power(p), timeout=8.0)
                ok = r is not None and r.type == EventType.OK
                if ok:
                    Domoticz.Log(f"TX power set to {p} dBm")
                    self._self_info_full["tx_power"] = p
                    self._write_device_map()
                self._queue.put(("send_result", {
                    "ok": ok, "target": "!set_tx_power", "body": str(p),
                    "result": "applied" if ok else str(r),
                
                    "id": req_id,
                }))
            except Exception as exc:
                self._queue.put(("send_result", {
                    "ok": False, "target": "!set_tx_power", "body": text, "result": str(exc),
                
                    "id": req_id,
                }))
            return

        # !set_name <new name>
        if text.startswith("!set_name "):
            new_name = text[len("!set_name "):].strip()
            if not new_name:
                self._queue.put(("send_result", {
                    "ok": False, "target": "!set_name", "body": text,
                    "result": "name must not be empty",
                    "id": req_id,
                }))
                return
            # Defend in depth: HTML caps at 32 but the /json.htm endpoint
            # bypasses that. Reject overlong / non-printable names.
            if len(new_name) > 32:
                self._queue.put(("send_result", {
                    "ok": False, "target": "!set_name", "body": new_name,
                    "result": "name must be ≤ 32 characters",
                    "id": req_id,
                }))
                return
            if not all(c.isprintable() for c in new_name):
                self._queue.put(("send_result", {
                    "ok": False, "target": "!set_name", "body": new_name,
                    "result": "name contains non-printable characters",
                    "id": req_id,
                }))
                return
            try:
                r = await asyncio.wait_for(mc.commands.set_name(new_name), timeout=8.0)
                ok = r is not None and r.type == EventType.OK
                if ok:
                    Domoticz.Log(f"Device name set to: {new_name}")
                    self._self_name = new_name
                    self._self_info_full["name"] = new_name
                    self._write_device_map()
                self._queue.put(("send_result", {
                    "ok": ok, "target": "!set_name", "body": new_name,
                    "result": "applied" if ok else str(r),
                
                    "id": req_id,
                }))
            except Exception as exc:
                self._queue.put(("send_result", {
                    "ok": False, "target": "!set_name", "body": new_name, "result": str(exc),
                
                    "id": req_id,
                }))
            return

        # !set_coords <lat> <lon>
        if text.startswith("!set_coords"):
            parts = text.split()
            if len(parts) != 3:
                self._queue.put(("send_result", {
                    "ok": False, "target": "!set_coords", "body": text,
                    "result": "syntax: !set_coords <lat> <lon>",
                    "id": req_id,
                }))
                return
            try:
                lat, lon = float(parts[1]), float(parts[2])
            except ValueError as exc:
                self._queue.put(("send_result", {
                    "ok": False, "target": "!set_coords", "body": text, "result": str(exc),
                
                    "id": req_id,
                }))
                return
            if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
                self._queue.put(("send_result", {
                    "ok": False, "target": "!set_coords", "body": f"{lat},{lon}",
                    "result": "lat must be -90..90 and lon must be -180..180",
                
                    "id": req_id,
                }))
                return
            try:
                r = await asyncio.wait_for(mc.commands.set_coords(lat, lon), timeout=8.0)
                ok = r is not None and r.type == EventType.OK
                if ok:
                    Domoticz.Log(f"Coords set: lat={lat} lon={lon}")
                    self._self_info_full["adv_lat"] = lat
                    self._self_info_full["adv_lon"] = lon
                    self._write_device_map()
                self._queue.put(("send_result", {
                    "ok": ok, "target": "!set_coords", "body": f"{lat},{lon}",
                    "result": "applied" if ok else str(r),
                
                    "id": req_id,
                }))
            except Exception as exc:
                self._queue.put(("send_result", {
                    "ok": False, "target": "!set_coords", "body": text, "result": str(exc),
                
                    "id": req_id,
                }))
            return

        # !set_path_hash_mode <1|2|3>
        #   1 = 1-byte hashes (max ~64 hops)
        #   2 = 2-byte hashes (max ~32 hops)
        #   3 = 3-byte hashes (max ~21 hops, default on SF8)
        if text.startswith("!set_path_hash_mode"):
            parts = text.split()
            if len(parts) != 2:
                self._queue.put(("send_result", {
                    "ok": False, "target": "!set_path_hash_mode", "body": text,
                    "result": "syntax: !set_path_hash_mode <1|2|3>",
                    "id": req_id,
                }))
                return
            try:
                mode = int(parts[1])
                if mode < 1 or mode > 3:
                    raise ValueError("mode must be 1, 2 or 3")
            except ValueError as exc:
                self._queue.put(("send_result", {
                    "ok": False, "target": "!set_path_hash_mode", "body": text, "result": str(exc),
                
                    "id": req_id,
                }))
                return
            # Wire format: firmware uses 0/1/2 to mean 1/2/3-byte hashes
            # (off-by-one vs. the human-friendly value the UI uses). Translate
            # at the boundary so the rest of the code can stay in 1/2/3.
            wire_mode = mode - 1
            try:
                r = await asyncio.wait_for(mc.commands.set_path_hash_mode(wire_mode), timeout=8.0)
                ok = r is not None and r.type == EventType.OK
                if ok:
                    Domoticz.Log(f"Path hash mode set to {mode}-byte (wire={wire_mode})")
                    # Store the human-friendly value so the dashboard dropdown
                    # selects the same option the user picked.
                    self._device_info["path_hash_mode"] = mode
                    self._write_device_map()
                self._queue.put(("send_result", {
                    "ok": ok, "target": "!set_path_hash_mode", "body": str(mode),
                    "result": "applied" if ok else str(r),
                
                    "id": req_id,
                }))
            except Exception as exc:
                self._queue.put(("send_result", {
                    "ok": False, "target": "!set_path_hash_mode", "body": text, "result": str(exc),
                
                    "id": req_id,
                }))
            return

        # !reboot
        if text.startswith("!reboot"):
            try:
                # reboot doesn't wait for a response — the device acks then
                # disconnects roughly 1s later. We only swallow disconnect-
                # class exceptions (TimeoutError, ConnectionError) as expected;
                # other errors (e.g. permission denied, bad state) are reported.
                await asyncio.wait_for(mc.commands.reboot(), timeout=5.0)
                Domoticz.Log("Reboot command sent.")
                self._queue.put(("send_result", {
                    "ok": True, "target": "!reboot", "body": "", "result": "sent",
                
                    "id": req_id,
                }))
            except (asyncio.TimeoutError, ConnectionError, OSError) as exc:
                # Disconnection during reboot is expected — the device just
                # reset and our serial/TCP link dropped. Treat as success.
                Domoticz.Log(f"Reboot sent (device disconnected as expected: {exc})")
                self._queue.put(("send_result", {
                    "ok": True, "target": "!reboot", "body": "", "result": "sent",
                
                    "id": req_id,
                }))
            except Exception as exc:
                Domoticz.Error(f"Reboot failed: {exc}")
                self._queue.put(("send_result", {
                    "ok": False, "target": "!reboot", "body": "", "result": str(exc),
                
                    "id": req_id,
                }))
            return

        # !get_telemetry — local sensors
        if text.startswith("!get_telemetry"):
            try:
                r = await asyncio.wait_for(mc.commands.get_self_telemetry(), timeout=8.0)
                payload = dict(r.payload or {}) if r else {}
                ok = r is not None
                if ok:
                    self._queue.put(("self_telemetry", payload))
                self._queue.put(("send_result", {
                    "ok": ok, "target": "!get_telemetry", "body": "",
                    "result": "received" if ok else "no response",
                
                    "id": req_id,
                }))
            except Exception as exc:
                self._queue.put(("send_result", {
                    "ok": False, "target": "!get_telemetry", "body": "", "result": str(exc),
                
                    "id": req_id,
                }))
            return

        # ── Special: set / clear a channel slot ─────────────────────────────
        # Syntax:
        #   !set_channel <slot> <name>            — name starting with '#'
        #                                            auto-derives the secret
        #   !set_channel <slot> <name> <secret>   — explicit 32-hex-char secret
        #   !clear_channel <slot>                 — wipe slot to empty
        if text.startswith("!set_channel"):
            parts = text.split(None, 3)
            if len(parts) < 3:
                self._queue.put(("send_result", {
                    "ok": False, "target": "!set_channel", "body": text,
                    "result": f"syntax: !set_channel <slot 0-{MAX_CHANNEL_SLOTS-1}> <name> [secret_hex]",
                    "id": req_id,
                }))
                return
            try:
                slot = int(parts[1])
                if slot < 0 or slot >= MAX_CHANNEL_SLOTS:
                    raise ValueError(f"slot must be 0..{MAX_CHANNEL_SLOTS-1}")
            except ValueError as exc:
                self._queue.put(("send_result", {
                    "ok": False, "target": "!set_channel", "body": text,
                    "result": f"bad slot: {exc}",
                    "id": req_id,
                }))
                return
            name = parts[2]
            # Names beginning with '!' would re-enter the local command parser
            # on the next plugin restart if anything echoed them back, and
            # they're not valid MeshCore channel names anyway.
            if name.startswith("!"):
                self._queue.put(("send_result", {
                    "ok": False, "target": "!set_channel", "body": text,
                    "result": "channel name must not start with '!'",
                    "id": req_id,
                }))
                return
            secret = None
            if len(parts) >= 4:
                try:
                    secret = bytes.fromhex(parts[3])
                except ValueError as exc:
                    self._queue.put(("send_result", {
                        "ok": False, "target": "!set_channel", "body": text,
                        "result": f"bad secret hex: {exc}",
                        "id": req_id,
                    }))
                    return
                if len(secret) != 16:
                    self._queue.put(("send_result", {
                        "ok": False, "target": "!set_channel", "body": text,
                        "result": "secret must be exactly 16 bytes (32 hex chars)",
                        "id": req_id,
                    }))
                    return
            try:
                result = await asyncio.wait_for(
                    mc.commands.set_channel(slot, name, secret), timeout=10.0
                )
                ok = result is not None and result.type == EventType.OK
                if ok:
                    # Refresh slot table immediately so the dashboard sees it
                    await self._fetch_channel_names(mc)
                    self._write_device_map()
                    Domoticz.Log(f"Channel slot {slot} set to '{name}'")
                self._queue.put(("send_result", {
                    "ok": ok, "target": "!set_channel",
                    "body": f"slot={slot} name={name}",
                    "result": "applied" if ok else str(result),
                
                    "id": req_id,
                }))
            except Exception as exc:
                self._queue.put(("send_result", {
                    "ok": False, "target": "!set_channel",
                    "body": f"slot={slot} name={name}", "result": str(exc),
                
                    "id": req_id,
                }))
            return

        if text.startswith("!clear_channel"):
            parts = text.split(None, 1)
            if len(parts) < 2:
                self._queue.put(("send_result", {
                    "ok": False, "target": "!clear_channel", "body": text,
                    "result": f"syntax: !clear_channel <slot 0-{MAX_CHANNEL_SLOTS-1}>",
                    "id": req_id,
                }))
                return
            try:
                slot = int(parts[1])
                if slot < 0 or slot >= MAX_CHANNEL_SLOTS:
                    raise ValueError(f"slot must be 0..{MAX_CHANNEL_SLOTS-1}")
            except ValueError as exc:
                self._queue.put(("send_result", {
                    "ok": False, "target": "!clear_channel", "body": text,
                    "result": f"bad slot: {exc}",
                    "id": req_id,
                }))
                return
            try:
                # Clear by writing an empty name + zero secret. The library
                # accepts any 16-byte secret; with empty name the firmware
                # treats the slot as free.
                result = await asyncio.wait_for(
                    mc.commands.set_channel(slot, "", b"\x00" * 16), timeout=10.0
                )
                ok = result is not None and result.type == EventType.OK
                if ok:
                    await self._fetch_channel_names(mc)
                    self._write_device_map()
                    Domoticz.Log(f"Channel slot {slot} cleared.")
                self._queue.put(("send_result", {
                    "ok": ok, "target": "!clear_channel",
                    "body": f"slot={slot}",
                    "result": "cleared" if ok else str(result),
                
                    "id": req_id,
                }))
            except Exception as exc:
                self._queue.put(("send_result", {
                    "ok": False, "target": "!clear_channel",
                    "body": f"slot={slot}", "result": str(exc),
                
                    "id": req_id,
                }))
            return

        # ── Special: set default flood scope ───────────────────────────────
        # Syntax: "!flood_scope <name>"  (empty name = reset to global flood)
        if text.startswith("!flood_scope"):
            arg = text[len("!flood_scope"):].strip()
            # Library treats "", "0", "None", "*" as reset. We pass the empty
            # string for reset (NOT None) because meshcore.commands.messaging
            # has a bug where set_default_flood_scope(None) calls len(scope)
            # on the still-None argument. Empty string takes the elif branch
            # and resets correctly.
            scope_to_set = arg or ""
            # Mirror the library's disable sentinels so our UI/state stays in
            # sync. "0", "*", "None" all mean "clear scope" — collapse to "".
            if scope_to_set in ("0", "*", "None"):
                scope_to_set = ""
            try:
                # When clearing, also clear the runtime scope first. The firmware
                # refuses set_default_flood_scope("") while a runtime scope is
                # active (returns ERR_CODE_ILLEGAL_ARG). Clearing the runtime
                # scope first removes that block.
                if not scope_to_set:
                    try:
                        await asyncio.wait_for(
                            mc.commands.set_flood_scope(""), timeout=10.0
                        )
                    except Exception as e:
                        Domoticz.Debug(f"set_flood_scope clear pre-step failed: {e}")
                    # Try each disable sentinel in turn: "0", "", "*".
                    result = None
                    for sentinel in ("0", "", "*"):
                        try:
                            result = await asyncio.wait_for(
                                mc.commands.set_default_flood_scope(sentinel),
                                timeout=10.0,
                            )
                        except Exception as e:
                            Domoticz.Debug(f"set_default_flood_scope({sentinel!r}) raised: {e}")
                            continue
                        if result is not None and result.type == EventType.OK:
                            Domoticz.Debug(f"Empty-scope clear accepted with sentinel {sentinel!r}")
                            break
                else:
                    result = await asyncio.wait_for(
                        mc.commands.set_default_flood_scope(scope_to_set), timeout=10.0
                    )
                ok = result is not None and result.type == EventType.OK
                if ok:
                    # Normalize the stored value to what the device would echo back
                    if not scope_to_set:
                        self._default_flood_scope = ""
                    else:
                        s = scope_to_set
                        if not s.startswith("#"):
                            s = "#" + s
                        self._default_flood_scope = s
                    Domoticz.Log(f"Default flood scope set to {self._default_flood_scope or '(none)'}")
                    self._write_device_map()
                # Detect the firmware's "ILLEGAL_ARG on empty-scope set" quirk
                # and surface a user-facing hint about !reboot. The firmware
                # refuses to wipe an active scope_key in-place; it only clears
                # on power-on. Reproduces consistently on fw 27 (Apr 2026).
                err_payload = (result.payload if result is not None else None) or {}
                is_empty_scope_quirk = (
                    not ok and not scope_to_set
                    and isinstance(err_payload, dict)
                    and err_payload.get("code_string") == "ERR_CODE_ILLEGAL_ARG"
                )
                if is_empty_scope_quirk:
                    result_str = ("firmware refused empty-scope set while a scope is active. "
                                  "Workaround: clear scope via the MeshCore phone app and save, "
                                  "or send !reboot - the scope clears on startup.")
                else:
                    result_str = "applied" if ok else str(result)
                self._queue.put(("send_result", {
                    "ok": ok, "target": "!flood_scope",
                    "body": arg or "(reset)",
                    "result": result_str,
                
                    "id": req_id,
                }))
            except Exception as exc:
                self._queue.put(("send_result", {
                    "ok": False, "target": "!flood_scope",
                    "body": arg or "(reset)", "result": str(exc),
                
                    "id": req_id,
                }))
            return

        # ── Special: set device parameter ──────────────────────────────────
        # Syntax: "!set <key> <int-value>"
        # Supported keys: telemetry_base, telemetry_loc, telemetry_env, adv_loc_policy
        if text.startswith("!set "):
            try:
                _, key, val = text.split(None, 2)
                ival = int(val)
            except ValueError:
                self._queue.put(("send_result", {"ok": False, "target": "!set", "body": text, "result": "syntax: !set <key> <int>",
                    "id": req_id,
                }))
                return
            cmd_map = {
                "telemetry_base":  mc.commands.set_telemetry_mode_base,
                "telemetry_loc":   mc.commands.set_telemetry_mode_loc,
                "telemetry_env":   mc.commands.set_telemetry_mode_env,
                "adv_loc_policy":  mc.commands.set_advert_loc_policy,
            }
            fn = cmd_map.get(key)
            if fn is None:
                self._queue.put(("send_result", {"ok": False, "target": "!set", "body": text, "result": f"unknown key '{key}'",
                    "id": req_id,
                }))
                return
            try:
                result = await asyncio.wait_for(fn(ival), timeout=10.0)
                ok = result is not None and result.type == EventType.OK
                if ok:
                    if key == "telemetry_base":     self._telemetry_mode_base = ival
                    elif key == "telemetry_loc":    self._telemetry_mode_loc = ival
                    elif key == "telemetry_env":    self._telemetry_mode_env = ival
                    elif key == "adv_loc_policy":   self._advert_loc_policy = ival
                    self._settings_set_at = time.monotonic()
                    Domoticz.Log(f"Device {key} = {ival}")
                self._queue.put(("send_result", {
                    "ok": ok, "target": "!set", "body": f"{key}={ival}",
                    "result": "applied" if ok else str(result),
                
                    "id": req_id,
                }))
            except Exception as exc:
                self._queue.put(("send_result", {"ok": False, "target": "!set", "body": f"{key}={ival}", "result": str(exc),
                    "id": req_id,
                }))
            return

        # ── Special: toggle manual_add_contacts on the connected node ──────
        if text.startswith("!manual_add"):
            arg = text[len("!manual_add"):].strip().lower()
            enable = arg in ("on", "1", "true", "yes")
            try:
                result = await asyncio.wait_for(
                    mc.commands.set_manual_add_contacts(enable), timeout=10.0
                )
                ok = result is not None and result.type == EventType.OK
                if ok:
                    self._manual_add_contacts = enable
                    self._settings_set_at = time.monotonic()
                    Domoticz.Log(f"manual_add_contacts set to {enable} on device.")
                self._queue.put(("send_result", {
                    "ok": ok,
                    "target": "!manual_add",
                    "body": "on" if enable else "off",
                    "result": "applied" if ok else str(result),
                
                    "id": req_id,
                }))
            except Exception as exc:
                self._queue.put(("send_result", {
                    "ok": False, "target": "!manual_add",
                    "body": "on" if enable else "off", "result": str(exc),
                
                    "id": req_id,
                }))
            return

        # ── Special: remove contact ────────────────────────────────────────
        if text.startswith("!remove "):
            name = text[len("!remove "):].strip()
            if not name:
                self._queue.put(("send_result", {"ok": False, "target": "!remove", "body": text, "result": "no contact name",
                    "id": req_id,
                }))
                return
            contact = None
            for c in mc.contacts.values():
                if c.get("adv_name", "").strip() == name:
                    contact = dict(c)
                    break
            if contact is None:
                self._queue.put(("send_result", {"ok": False, "target": "!remove", "body": text, "result": f"contact '{name}' not found",
                    "id": req_id,
                }))
                return
            try:
                result = await asyncio.wait_for(mc.commands.remove_contact(contact), timeout=10.0)
                ok = result is not None and result.type == EventType.OK
                self._queue.put(("send_result", {
                    "ok": ok,
                    "target": "!remove",
                    "body": name,
                    "result": "removed" if ok else str(result),
                
                    "id": req_id,
                }))
                if ok:
                    # Demote the contact into the heard store BEFORE clearing
                    # local state so all metadata is still available.
                    self._demote_contact_to_heard(name)
                    # Drop from local tracking so it disappears from the dashboard immediately
                    if name in self._contact_names:
                        self._contact_names.remove(name)
                    self._node_types.pop(name, None)
                    self._node_last_advert.pop(name, None)
                    self._node_pubkey.pop(name, None)
                    self._node_did.pop(name, None)
                    self._contact_query_results.pop(name, None)
                    self._node_last_activity.pop(name, None)
                    self._node_locations.pop(name, None)
                    self._node_out_path.pop(name, None)
                    self._node_out_path_hash_mode.pop(name, None)
                    self._ws_devices_dirty = True
                    if name in self._favorites:
                        self._favorites.discard(name)
                        self._save_favorites()
            except Exception as exc:
                self._queue.put(("send_result", {"ok": False, "target": "!remove", "body": name, "result": str(exc),
                    "id": req_id,
                }))
            return

        # ── Special: add a heard node to contacts ──────────────────────────
        # Syntax: "!heard_add <full_pubkey_hex>"
        if text.startswith("!heard_add "):
            pk = text[len("!heard_add "):].strip()
            h = self._heard_nodes.get(pk)
            if h is None:
                self._queue.put(("send_result", {
                    "ok": False, "target": "!heard_add", "body": pk[:12],
                    "result": "heard node not found (rescan?)",
                    "id": req_id,
                }))
                return
            contact = {
                "public_key":         pk,
                "type":               int(h.get("type") or 1),
                "flags":              0,
                "out_path":           "",
                "out_path_len":       -1,     # flood — no direct path known
                "out_path_hash_mode": 0,
                "adv_name":           h.get("name") or pk[:12],
                "last_advert":        int(h.get("last_heard") or time.time()),
                "adv_lat":            float(h.get("lat") or 0.0),
                "adv_lon":            float(h.get("lon") or 0.0),
            }
            try:
                result = await asyncio.wait_for(
                    mc.commands.add_contact(contact), timeout=10.0)
                ok = result is not None and result.type == EventType.OK
                if ok:
                    # _heard_nodes is also touched by _on_rx_log under this
                    # lock. Hold it only for the mutation — _write_heard()
                    # below re-acquires it (non-reentrant Lock).
                    with self._rx_log_lock:
                        self._heard_nodes.pop(pk, None)
                    self._known_pubkeys = self._known_pubkeys | {pk}
                    self._heard_dirty = True
                    self._ws_heard_dirty = True
                    self._write_heard()
                    Domoticz.Log(f"Added heard node '{contact['adv_name']}' to contacts.")
                    # Refresh contacts so the new device/dashboard entry appears
                    await asyncio.wait_for(self._refresh_contacts(mc, verbose=True), timeout=COMMAND_TIMEOUT)
                self._queue.put(("send_result", {
                    "ok": ok, "target": "!heard_add",
                    "body": contact["adv_name"],
                    "result": "added" if ok else str(result),
                    "id": req_id,
                }))
            except Exception as exc:
                self._queue.put(("send_result", {
                    "ok": False, "target": "!heard_add",
                    "body": pk[:12], "result": str(exc),
                    "id": req_id,
                }))
            return

        # ── Special: delete a heard node from the heard list ───────────────
        if text.startswith("!heard_delete "):
            pk = text[len("!heard_delete "):].strip()
            with self._rx_log_lock:
                existed = self._heard_nodes.pop(pk, None) is not None
            if existed:
                self._heard_dirty = True
                self._ws_heard_dirty = True
                self._write_heard()
            self._queue.put(("send_result", {
                "ok": existed, "target": "!heard_delete", "body": pk[:12],
                "result": "deleted" if existed else "not in heard list",
                "id": req_id,
            }))
            return

        # ── Special: prune heard nodes by age or frequency ─────────────────
        if text.startswith("!heard_prune "):
            criteria = text[len("!heard_prune "):].strip()
            _valid = {"week", "month", "year", "once"}
            if criteria not in _valid:
                self._queue.put(("send_result", {
                    "ok": False, "target": "!heard_prune", "body": criteria,
                    "result": f"unknown criteria '{criteria}'; use: week, month, year, once",
                    "id": req_id,
                }))
                return
            now = time.time()
            _age_thresholds = {"week": 7 * 86400, "month": 30 * 86400, "year": 365 * 86400}
            to_remove = []
            with self._rx_log_lock:
                for pk, h in list(self._heard_nodes.items()):
                    if criteria == "once":
                        if (h.get("count") or 1) <= 1:
                            to_remove.append(pk)
                    else:
                        lh = h.get("last_heard")
                        if lh is None:
                            # No last_heard → skip (keep) — safer than pruning
                            continue
                        if (now - lh) > _age_thresholds[criteria]:
                            to_remove.append(pk)
                for pk in to_remove:
                    self._heard_nodes.pop(pk, None)
            n = len(to_remove)
            if n > 0:
                self._heard_dirty = True
                self._ws_heard_dirty = True
                self._write_heard()
            self._queue.put(("send_result", {
                "ok": True, "target": "!heard_prune", "body": str(n),
                "result": f"pruned {n} node(s)",
                "id": req_id,
            }))
            return

        # ── Special: wipe all lifetime statistics ──────────────────────────
        if text.strip() == "!reset_stats":
            with self._rx_log_lock:
                self._stats = {
                    "adverts_total":  0, "messages_total": 0,
                    "client_total":   0, "repeater_total": 0, "server_total": 0,
                    "msg_by_sender":  {}, "adv_by_sender":  {},
                    "msg_by_channel": {},
                    "hops_records":   [],
                    "today": {"date": "", "messages": 0,
                              "client": 0, "repeater": 0, "server": 0},
                }
                self._stats_dirty = False
            self._ws_stats_dirty = True
            self._write_stats()
            Domoticz.Log("Lifetime statistics reset by dashboard.")
            self._queue.put(("send_result", {
                "ok": True, "target": "!reset_stats", "body": "",
                "result": "statistics cleared",
                "id": req_id,
            }))
            return

        target = None
        body   = text

        if ":" in text:
            prefix, rest = text.split(":", 1)
            prefix = prefix.strip()
            body   = rest.strip()
            if prefix.startswith("#"):
                chan_part = prefix[1:].strip()
                if chan_part.lower() in ("", "flood"):
                    chan_idx = 0
                elif chan_part.isdigit():
                    chan_idx = int(chan_part)
                else:
                    # Resolve channel name → index (case- and #-insensitive).
                    # Stored names include the leading "#" (e.g. "#test");
                    # the target may arrive as "#test" or "##test" depending
                    # on the caller, so normalise both sides.
                    want = chan_part.lstrip("#").lower()
                    chan_idx = None
                    for idx, name in self._channel_names.items():
                        if name.lstrip("#").lower() == want:
                            chan_idx = idx
                            break
                    if chan_idx is None:
                        self._queue.put(("send_result", {"ok": False, "target": prefix,
                                                         "body": body, "result": f"Unknown channel name '{chan_part}'. Known: {self._channel_names}",
                            "id": req_id,
                        }))
                        return
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
                                                    "result": "TX busy - try again" if tx_busy else str(result),
                        "id": req_id,
                    }))
                except Exception as exc:
                    self._queue.put(("send_result", {"ok": False, "target": f"#{chan_idx}", "body": body, "result": str(exc),
                        "id": req_id,
                    }))
                return
            else:
                target = prefix  # node name

        # Direct message to a node — success response is EventType.MSG_SENT
        if target is None:
            target = self._contact_names[0] if self._contact_names else ""
        if not target:
            self._queue.put(("send_result", {"ok": False, "target": target, "body": body, "result": "no target node",
                "id": req_id,
            }))
            return

        contact = None
        for c in mc.contacts.values():
            if c.get("adv_name", "").strip() == target:
                contact = dict(c)
                break

        if contact is None:
            self._queue.put(("send_result", {"ok": False, "target": target, "body": body, "result": "contact not found",
                "id": req_id,
            }))
            return

        # Use plain send_msg: returns as soon as the local node has accepted
        # the packet for TX. We register a pending-ACK record keyed by the
        # expected_ack code returned by MSG_SENT so _on_ack can correlate
        # exactly without timing guesswork (the code is a 4-byte hash of the
        # message content that the firmware embeds in the ACK packet).
        try:
            _dbg(f"worker send_msg -> target={target!r} body={body[:60]!r}")
            result = await asyncio.wait_for(
                mc.commands.send_msg(contact, body), timeout=15.0
            )
            _dbg(f"worker send_msg result: type={getattr(result,'type',None)!r} "
                 f"target={target!r}")
            tx_busy = (
                result is not None
                and result.type == EventType.ERROR
                and (result.payload or {}).get("reason") == "no_event_received"
            )
            ok = result is not None and result.type == EventType.MSG_SENT
            # Register pending ACK record so _on_ack can annotate the sent line.
            # Only do this on a clean MSG_SENT — if the TX failed there will be
            # no ACK to wait for.
            if ok:
                exp_ack = (result.payload or {}).get("expected_ack")
                if isinstance(exp_ack, (bytes, bytearray)) and len(exp_ack) == 4:
                    ack_code = exp_ack.hex()
                    out_ts = int(time.time())
                    with self._rx_log_lock:
                        self._pending_acks[ack_code] = {
                            "target":   target,
                            "body":     body,
                            "out_ts":   out_ts,
                        }
                    _dbg(f"worker registered pending_ack code={ack_code} "
                         f"target={target!r}")
                else:
                    _dbg(f"worker MSG_SENT but no usable expected_ack "
                         f"(exp_ack={exp_ack!r}) target={target!r}")
            self._queue.put(("send_result", {"ok": ok, "target": target, "body": body,
                                             "result": "TX busy - try again" if tx_busy else str(result),
                "id": req_id,
            }))
        except Exception as exc:
            self._queue.put(("send_result", {"ok": False, "target": target, "body": body, "result": str(exc),
                "id": req_id,
            }))

    # ── Queue dispatcher (runs on Domoticz main thread via onHeartbeat) ───────

    def _dispatch(self, item):
        kind = item[0]
        if kind == "message":
            Domoticz.Debug(f"Message: {item[1]}")
            self._handle_message(item[1])
        elif kind == "advert":
            # Ambient advertisement — a hint that a node is alive even before
            # its first message. Lifetime advert stats are tallied from the
            # RX_LOG ADVERT frame (the canonical source) — not counted here.
            #
            # If the advert is from a node we already track as a contact,
            # bump its activity NOW so "Last Heard" reflects over-the-air
            # adverts immediately instead of waiting for the next contacts
            # poll. (Non-contact adverts are handled by the heard-node store.)
            ap = item[1] or {}
            adv_name = (ap.get("adv_name") or ap.get("name") or "").strip()
            if adv_name and adv_name in self._node_pubkey:
                now_ts = int(time.time())
                self._node_last_activity[adv_name] = now_ts
                self._node_last_advert[adv_name]   = now_ts
                did = self._device_id_for(adv_name)
                if did is not None:
                    self._set(did, OFF_STATUS, 1, "On")
                self._write_device_map()
            # Ingest into analytics store when the advert carries signal data.
            _adv_pk = ap.get("adv_key") or ap.get("pubkey") or ""
            _adv_snr  = ap.get("snr") or ap.get("SNR")
            _adv_rssi = ap.get("rssi")
            if _adv_snr is not None or _adv_rssi is not None:
                self._ts_ingest("adv",
                                node_key=_adv_pk[:12] if _adv_pk else None,
                                rssi=_adv_rssi, snr=_adv_snr)
        elif kind == "contacts":
            self._handle_contacts(item[1])
            # First contacts batch processed — bump heartbeat back to a
            # steady 10s cadence. Push events still arrive instantly via
            # the worker thread; the heartbeat is only there to drain the
            # cross-thread queue. 10s feels live without thrashing.
            if not self._heartbeat_restored:
                Domoticz.Heartbeat(10)
                self._heartbeat_restored = True
        elif kind == "self_stats":
            self._handle_self_stats(item[1])
        elif kind == "self_telemetry":
            # Latest local-sensor reading from get_self_telemetry. Stored in
            # the device map so the self-node side panel can render it.
            self._self_telemetry = item[1] or {}
            self._write_device_map()
        elif kind == "contact_status":
            d = item[1] or {}
            name = d.get("name")
            if name:
                self._contact_query_results.setdefault(name, {})["status"] = {
                    "t": int(time.time()), "data": d.get("data") or {}
                }
                self._write_device_map()
        elif kind == "contact_telemetry":
            d = item[1] or {}
            name = d.get("name")
            if name:
                self._contact_query_results.setdefault(name, {})["telemetry"] = {
                    "t": int(time.time()), "data": d.get("data") or {}
                }
                self._write_device_map()
        elif kind == "contact_neighbours":
            d = item[1] or {}
            name = d.get("name")
            if name:
                # Neighbours payload can be a list — preserve as-is, JSON serializer
                # will handle it.
                data = d.get("data")
                self._contact_query_results.setdefault(name, {})["neighbours"] = {
                    "t": int(time.time()), "data": data
                }
                self._write_device_map()
        elif kind == "flood_scope":
            scope = (item[1] or "").strip()
            if scope != self._default_flood_scope:
                self._default_flood_scope = scope
                Domoticz.Debug(f"Default flood scope: {scope or '(none)'}")
                self._write_device_map()
        elif kind == "device_info":
            info = dict(item[1] or {})
            # Firmware reports path_hash_mode in wire format (0/1/2 = 1/2/3
            # byte). The rest of our code and the dashboard work in the
            # human-friendly 1/2/3 form, so translate at the boundary.
            if "path_hash_mode" in info and isinstance(info["path_hash_mode"], int):
                info["path_hash_mode"] = info["path_hash_mode"] + 1
            if info != self._device_info:
                self._device_info = info
                fw = info.get("fw ver", "?")
                build = info.get("fw_build", "")
                model = info.get("model", "")
                Domoticz.Log(f"Device info: fw={fw} build={build!r} model={model!r}")
                self._write_device_map()
        elif kind == "self_info":
            name = item[1].get("name", "")
            Domoticz.Debug(f"Self info: name={name}, freq={item[1].get('radio_freq')} MHz")
            if name and name != self._self_name:
                self._self_name = name
            # Keep a full snapshot for the dashboard's self-node side panel.
            self._self_info_full = dict(item[1])
            self._write_device_map()
            # Track device-side settings so the dashboard can show / toggle them.
            # Skip while in the user-set grace window — some firmware returns
            # the previous value briefly while persisting to flash.
            in_grace = (time.monotonic() - self._settings_set_at) < SETTINGS_GRACE_S
            if not in_grace:
                mac = item[1].get("manual_add_contacts")
                if mac is not None:
                    self._manual_add_contacts = bool(mac)
                for k_attr, k_info in (
                    ("_telemetry_mode_base", "telemetry_mode_base"),
                    ("_telemetry_mode_loc",  "telemetry_mode_loc"),
                    ("_telemetry_mode_env",  "telemetry_mode_env"),
                    ("_advert_loc_policy",   "adv_loc_policy"),
                ):
                    v = item[1].get(k_info)
                    if v is not None:
                        setattr(self, k_attr, int(v))
            else:
                Domoticz.Debug(f"In settings grace window ({int(SETTINGS_GRACE_S - (time.monotonic() - self._settings_set_at))}s left); skipping self_info settings update.")
        elif kind == "send_result":
            d = item[1]
            # Internal control commands (!remove, !manual_add, !flood_scope,
            # !favorite, !set, ...) shouldn't appear in the inbox or count as
            # a sent mesh message.
            is_internal = isinstance(d.get("target"), str) and d["target"].startswith("!")
            _dbg(f"send_result: ok={d.get('ok')} target={d.get('target')!r} "
                 f"body={d.get('body','')!r} result={d.get('result','')!r} "
                 f"id={d.get('id')!r} internal={is_internal}")
            if d["ok"]:
                if is_internal:
                    # The verb handler already logged its own success message;
                    # don't duplicate. Debug-level keeps it greppable.
                    Domoticz.Debug(f"Internal command ok: {d['target']} {d.get('body','')}")
                    # Skip inbox / counter updates for internal commands.
                else:
                    Domoticz.Status(f"Message sent to '{d['target']}': {d['body']}")
                    self._sent_count += 1
                    self._set(MESH_DID, UNIT_MSGS_SENT_, 0, str(self._sent_count))
                    # Show sent message in the inbox so the user gets confirmation.
                    # Use the same [ChannelName|sender] / [P|sender] format as incoming msgs
                    # with a leading "> " on the sender to mark it as outgoing. Stick to
                    # ASCII so Windows/cp1252 stored text isn't mangled.
                    tgt = d["target"]
                    me = self._self_name or "Me"
                    out_ts = int(time.time())   # our own message, our clock
                    if tgt.startswith("#"):
                        chan_idx_str = tgt[1:]
                        chan_idx_int = int(chan_idx_str) if chan_idx_str.isdigit() else None
                        chan_tag = self._channel_names.get(chan_idx_int, f"C{chan_idx_str}") if chan_idx_int is not None else f"C{chan_idx_str}"
                        self._set(MESH_DID, UNIT_INBOX, 0,
                                  self._inbox_line(chan_tag, f"> {me}", d['body'], out_ts))
                        # Persist outgoing channel message to SQLite store.
                        # direction="out" carries the in/out distinction; sender is the actual author.
                        self._msg_store_add(
                            chan=chan_tag, sender=me, body=d['body'],
                            epoch=out_ts, direction="out", peer_key=None,
                        )
                    else:
                        sent_line = self._inbox_line("P", f"> {tgt}", d['body'], out_ts)
                        self._set(MESH_DID, UNIT_INBOX, 0, sent_line)
                        self._log_contact_dm(tgt, sent_line)
                        # Persist outgoing DM to SQLite store; capture rowid for ACK back-fill.
                        # Resolve the peer's pubkey for the stable conversation key.
                        # direction="out" carries the in/out distinction; sender is the actual author (self).
                        _tgt_did = self._device_id_for(tgt)
                        _tgt_pk = (
                            _tgt_did
                            if (_tgt_did and _tgt_did not in ("self", None) and len(_tgt_did) == 12)
                            else self._node_did.get(tgt)
                            or self._node_pubkey.get(tgt, "")
                        )
                        _out_rowid = self._msg_store_add(
                            chan="P", sender=me, body=d['body'],
                            epoch=out_ts, direction="out",
                            peer_key=self._norm_peer_key(_tgt_pk),
                        )
                        _dbg(f"send_result DM stored: tgt={tgt!r} "
                             f"resolved_pk={self._norm_peer_key(_tgt_pk)!r} "
                             f"rowid={_out_rowid} line={sent_line!r}")
                        # Back-fill the inbox_line, dm_name, and msg_rowid into the
                        # pending_ack record so _on_ack / timeout sweep know exactly
                        # which stored line (and DB row) to rewrite.  Match by
                        # (target, body) — the worker wrote the record just before
                        # queuing send_result, so at most one entry will match.
                        # Hold the lock only for the dict lookup.
                        _early_ack = None
                        with self._rx_log_lock:
                            for _code, _rec in self._pending_acks.items():
                                if _rec.get("target") == tgt and _rec.get("body") == d['body']:
                                    _rec["inbox_line"] = sent_line
                                    _rec["dm_name"]    = tgt
                                    _rec["msg_rowid"]  = _out_rowid
                                    _dbg(f"send_result: back-filled pending_ack "
                                         f"for tgt={tgt!r} rowid={_out_rowid}")
                                    if _rec.get("delivered"):
                                        # The ACK already arrived before this
                                        # reconcile (fast reply). Consume the
                                        # record now and emit the delivered
                                        # result so the tick shows immediately
                                        # instead of waiting for a timeout.
                                        _early_ack = self._pending_acks.pop(_code)
                                    break
                        if _early_ack is not None:
                            _dbg(f"send_result: emitting deferred ACK for tgt={tgt!r} "
                                 f"rowid={_out_rowid}")
                            self._queue.put(("ack_result", {
                                "ack_code":   None,
                                "delivered":  True,
                                "target":     _early_ack.get("target", ""),
                                "body":       _early_ack.get("body", ""),
                                "out_ts":     _early_ack.get("out_ts", 0),
                                "inbox_line": _early_ack.get("inbox_line"),
                                "dm_name":    _early_ack.get("dm_name"),
                                "msg_rowid":  _early_ack.get("msg_rowid"),
                            }))
            else:
                Domoticz.Error(f"Send failed to '{d['target']}': {d['result']}")
            # Push result to any connected WebSocket client.  Wrapped so a
            # WebSocketSend exception cannot stall the heartbeat queue drain
            # (_push already swallows exceptions internally, but being explicit
            # here makes the intent clear and guards against future refactors).
            try:
                _dbg(f"push cmd_result: ok={d['ok']} target={d.get('target','')!r} "
                     f"id={d.get('id')!r}")
                self._push("cmd_result", {
                    "ok":     d["ok"],
                    "target": d.get("target", ""),
                    "result": d.get("result", ""),
                    "id":     d.get("id"),
                })
            except Exception as _exc:
                Domoticz.Debug(f"_dispatch send_result: _push raised {_exc!r}; ignoring.")
        elif kind == "self_offline":
            # Worker signalled that the node connection was lost (not a clean
            # plugin stop).  Flip the self STATUS device to Off so the dashboard
            # badge reflects the real state.
            if self._self_name:
                self._set(SELF_DID, OFF_STATUS, 0, "Off")
                self._ws_devices_dirty = True
            # Reset packet-delta baselines so the first sample after reconnect
            # does not produce a spurious large delta.
            self._ts_prev_pkt_recv     = None
            self._ts_prev_pkt_sent     = None
            self._ts_prev_pkt_flood_rx = None
            self._ts_prev_pkt_flood_tx = None
            self._ts_prev_pkt_dir_rx   = None
            self._ts_prev_pkt_dir_tx   = None
        elif kind == "ack_result":
            self._handle_ack_result(item[1])

    # ── Data handlers ─────────────────────────────────────────────────────────

    def _handle_contacts(self, contacts: dict):
        now = time.time()

        # Rebuild prefix → friendly-name lookup
        self._prefix_to_name = {
            c.get("public_key", "")[:12]: c.get("adv_name", "").strip()
            for c in contacts.values()
        }

        # Refresh the worker-readable set of known contact pubkeys and prune
        # any heard-node entry that is now a real contact.  Both mutations are
        # combined under a single _rx_log_lock acquisition so the worker never
        # observes the new _known_pubkeys without the corresponding _heard_nodes
        # pruning (or vice-versa).  _heard_nodes is also mutated by _on_rx_log
        # on the worker thread, so the lock is mandatory anyway.
        new_known = {
            c.get("public_key", "") for c in contacts.values() if c.get("public_key")
        }
        with self._rx_log_lock:
            self._known_pubkeys = new_known
            if self._heard_nodes:
                for pk in [k for k in self._heard_nodes if k in self._known_pubkeys]:
                    self._heard_nodes.pop(pk, None)
                    self._heard_dirty = True
                    self._ws_heard_dirty = True
            # If a previously-purged node has been re-added as a real contact,
            # lift the purge so a future removal can demote it back to heard.
            purge_lift = self._heard_purged & new_known
            if purge_lift:
                self._heard_purged -= purge_lift

        # Register any new contacts (non-self) in discovery order
        for contact in contacts.values():
            name = contact.get("adv_name", "").strip()
            if name and name != self._self_name and name not in self._contact_names:
                self._contact_names.append(name)
                Domoticz.Log(f"New contact discovered: '{name}'")

        # Update self node status from self_info (always online when connected)
        if self._self_name:
            self._ensure_node_devices(self._self_name)
            self._set(SELF_DID, OFF_STATUS, 1, "On")

        # Update all remote contacts
        for contact in contacts.values():
            node_name = contact.get("adv_name", "").strip()
            if not node_name or node_name == self._self_name:
                continue

            last_advert = contact.get("last_advert", 0)
            if last_advert < 1_577_836_800:
                last_advert = 0
            # "Last Seen" must reflect OUR local clock, never the node's
            # advertised timestamp (some nodes have a bad RTC and advertise a
            # future time). When the node's advertised time changes vs what we
            # last stored, a fresh advert arrived ~now, so record local now.
            # The raw node-reported time is kept separately (_node_last_advert)
            # so the dashboard can flag a wrong clock without corrupting our
            # reliable "last seen".
            prev_advert = self._node_last_advert.get(node_name, 0)
            if last_advert and last_advert != prev_advert:
                # "Last Seen" must always be OUR local receive time of a real
                # reception event — never the node's advertised clock (some
                # nodes have a wrong/stale RTC; that belongs only to the
                # separate skew check via _node_last_advert).
                #
                # prev_advert == 0 is the FIRST sighting this session (e.g.
                # just after a restart). The contact-list snapshot is not a
                # fresh advert that arrived "now", and the node's own advert
                # time is untrustworthy, so we cannot honestly say when we
                # last heard it: leave Last Seen unset until a real advert
                # change or an incoming message gives us a local timestamp.
                #
                # prev_advert != 0 means the node's advert time CHANGED
                # between two of our polls → a fresh advert genuinely arrived
                # ~now, so record our local clock. Monotonic: never regress.
                if prev_advert != 0:
                    self._node_last_activity[node_name] = max(
                        self._node_last_activity.get(node_name, 0), now)
            last_activity = self._node_last_activity.get(node_name, 0)
            advert_online = last_activity > 0 and (now - last_activity) < ONLINE_THRESHOLD_S

            path_len    = contact.get("out_path_len", -1)
            path_online = path_len >= 0
            online      = advert_online or path_online
            age_s       = int(now - last_activity) if last_activity > 0 else -1

            # Store pubkey / DeviceID BEFORE ensuring devices — the DeviceID
            # is derived from the pubkey, so it must be known first.
            pk = contact.get("public_key", "")
            if pk:
                self._node_pubkey[node_name] = pk
                self._node_did[node_name] = pk[:12]

            # Store outbound path for topology routing.
            # out_path is a hex string of 1-byte (or N-byte) hash tokens.
            # out_path_hash_mode from the library is 0-based; we store it as
            # +1 to match the dashboard convention used for device_info.
            raw_path = contact.get("out_path", "") or ""
            self._node_out_path[node_name] = str(raw_path)
            raw_mode = contact.get("out_path_hash_mode", 0) or 0
            self._node_out_path_hash_mode[node_name] = int(raw_mode) + 1

            self._ensure_node_devices(node_name)
            did = self._device_id_for(node_name)
            if did is None:
                continue

            self._set(did, OFF_STATUS,
                      1 if online else 0,
                      "On" if online else "Off")

            if 0 <= path_len < HOPS_SENTINEL:
                self._set(did, OFF_HOPS, 0, str(path_len))

            if last_activity > 0:
                # We have a real local-clock reception time — show it plainly.
                ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_activity))
                self._set(did, OFF_LASTSEEN, 0, ts)
            elif last_advert > 0:
                # No local reception yet (first sighting after a restart), but
                # the contact list carries the node's OWN advertised time.
                # Surface it in parentheses so it's unambiguous this is the
                # node's (possibly wrong) clock, not when WE heard it.
                node_ts = time.strftime("%Y-%m-%d %H:%M:%S",
                                        time.localtime(last_advert))
                self._set(did, OFF_LASTSEEN, 0, f"({node_ts})")

            la = self._node_last_activity.get(node_name, 0)

            # Store contact metadata for the dashboard map
            self._node_types[node_name] = int(contact.get("type", 0))
            if last_advert > 0:
                self._node_last_advert[node_name] = last_advert

            # Store GPS location if the contact advertises valid coordinates
            adv_lat = contact.get("adv_lat", 0.0)
            adv_lon = contact.get("adv_lon", 0.0)
            if adv_lat and adv_lon and not (adv_lat == 0.0 and adv_lon == 0.0):
                self._node_locations[node_name] = {"lat": adv_lat, "lon": adv_lon}

            Domoticz.Debug(
                f"Contact '{node_name}' type={contact.get('type',-1)}: "
                f"last_advert={int(now-last_advert)}s ago  "
                f"last_activity={int(now-la) if la else 'never'}  "
                f"path_len={path_len}  "
                f"online={online} (advert={advert_online} path={path_online})"
            )

        self._write_device_map()

    @staticmethod
    def _inbox_line(chan_tag, sender, body, ts, bad=False,
                    snr=None, hops=None, rssi=None, path=None, ack=None):
        """Build the inbox / conversation wire string with an embedded send
        time so the dashboard can show the real time a message was sent (not
        the time Domoticz happened to log it — which for messages drained on
        reconnect is the catch-up time, not the original time).

        Format:
          [chan|sender|<epoch>]              <epoch> = trusted send time
          [chan|sender|<epoch>|x]            <epoch> = OUR receive time,
                                             substituted because the node's
                                             reported time was missing /
                                             implausible (bad RTC).
          [chan|sender|<epoch>{|x}{|~h<hops>}{|~s<snr>}{|~r<rssi>}{|~p<path>}{|~a<0|1>}] body
                                             optional per-message signal,
                                             appended AFTER the epoch/x so the
                                             existing epoch parsing is
                                             unaffected and older lines (no
                                             tokens) still parse unchanged.
          ~a0 = no ACK received within timeout  (no delivery confirmation)
          ~a1 = ACK received (message delivered)
          ~a is only emitted on outgoing DM lines after ACK resolution.
          Back-compat: lines without ~a parse identically to before.
        """
        meta = f"{chan_tag}|{sender}|{int(ts)}"
        if bad:
            meta += "|x"
        if isinstance(hops, int) and hops >= 0:
            meta += f"|~h{hops}"
        if snr is not None:
            try:
                meta += f"|~s{round(float(snr), 2)}"
            except (TypeError, ValueError):
                pass
        if rssi is not None:
            try:
                meta += f"|~r{int(rssi)}"
            except (TypeError, ValueError):
                pass
        # Per-hop path = concatenated hex of each repeater's pubkey-hash
        # prefix the packet carried. Only meaningful when present (mostly
        # channel msgs back-filled from a matched RX_LOG frame). Strip any
        # separators the library may add; keep hex only so the token can't
        # contain '|' / ']' and break parsing.
        if path:
            ph = re.sub(r"[^0-9a-fA-F]", "", str(path))
            if ph:
                meta += f"|~p{ph.lower()}"
        # Delivery ACK annotation: ~a1 = delivered, ~a0 = no ack.
        # Only set on outgoing DM lines after ACK resolution; never on
        # incoming messages or channel sends.
        if ack is True:
            meta += "|~a1"
        elif ack is False:
            meta += "|~a0"
        return f"[{meta}] {body}"

    def _log_contact_dm(self, node_name: str, line: str):
        """No-op: per-contact Messages devices are retired.

        DM history is now served exclusively from the SQLite message store
        via the inbox_query / @<name> scope mechanism.  The method signature
        is kept so call sites do not need to be touched.
        """

    @staticmethod
    def _annotate_sent_line(line: str, delivered: bool) -> str:
        """Return a copy of an outgoing inbox wire line with the ~a token set.

        If the line already carries a ~a token it is replaced.  If the line
        has no meta block (legacy / malformed) it is returned unchanged so we
        never corrupt an existing entry.
        """
        if not line:
            return line
        m = re.match(r"^(\[[^\]]+\])(.*)", line, re.DOTALL)
        if not m:
            return line
        meta_block = m.group(1)   # "[chan|sender|ts|...]"
        body_part  = m.group(2)   # " body text"
        inner = meta_block[1:-1]  # strip [ ]
        # Remove any existing ~a token
        parts = [seg for seg in inner.split("|") if not re.match(r"^~a[01]$", seg)]
        # Append the new ~a token at the end
        parts.append("~a1" if delivered else "~a0")
        return f"[{'|'.join(parts)}]{body_part}"

    def _handle_ack_result(self, d: dict):
        """Rewrite the outgoing DM inbox/DM-device line with the ACK annotation.

        Runs on the Domoticz main thread (via _dispatch), so Domoticz API
        calls (_set) are safe here.
        """
        inbox_line = d.get("inbox_line")
        dm_name    = d.get("dm_name")
        delivered  = bool(d.get("delivered"))
        _dbg(f"_handle_ack_result: delivered={delivered} target={d.get('target')!r} "
             f"dm_name={dm_name!r} rowid={d.get('msg_rowid')} "
             f"has_inbox_line={bool(inbox_line)}")
        if not inbox_line:
            return
        annotated = self._annotate_sent_line(inbox_line, delivered)
        if annotated == inbox_line:
            return   # no change (shouldn't happen, but be safe)
        # Rewrite global inbox
        self._set(MESH_DID, UNIT_INBOX, 0, annotated)
        # Rewrite per-contact DM device if the contact is a favourite
        if dm_name:
            self._log_contact_dm(dm_name, annotated)
        # Update ACK status in the SQLite message store.
        msg_rowid = d.get("msg_rowid")
        if msg_rowid is not None:
            self._msg_store_set_ack(msg_rowid, delivered)
        status = "delivered" if delivered else "no ack"
        Domoticz.Debug(f"ACK annotation applied: {status} target={d.get('target')!r}")

    def _sweep_pending_acks(self):
        """Expire pending-ACK records that have exceeded DM_ACK_TIMEOUT_S.

        Called from onHeartbeat (main thread).  Timed-out records result in
        a ~a0 (no ack) annotation on the outgoing line.
        """
        now = time.time()
        expired = []
        with self._rx_log_lock:
            for code, rec in list(self._pending_acks.items()):
                if now - rec.get("out_ts", now) >= DM_ACK_TIMEOUT_S:
                    expired.append((code, dict(rec)))
            for code, _ in expired:
                del self._pending_acks[code]
        for _code, rec in expired:
            if rec.get("inbox_line"):
                # A record can carry delivered=True if the ACK arrived before
                # send_result reconciled it but, for some reason, the
                # back-fill path never emitted it (defensive — normally the
                # back-fill consumes it immediately). Honour that here so a
                # delivered message is never wrongly annotated "(no ack)".
                _delivered = bool(rec.get("delivered"))
                Domoticz.Debug(
                    f"DM ACK {'delivered (deferred)' if _delivered else 'timeout: no ack'} "
                    f"for target={rec.get('target')!r} "
                    f"body={rec.get('body','')[:40]!r}"
                )
                self._queue.put(("ack_result", {
                    "delivered":  _delivered,
                    "target":     rec.get("target", ""),
                    "body":       rec.get("body", ""),
                    "out_ts":     rec.get("out_ts", 0),
                    "inbox_line": rec.get("inbox_line"),
                    "dm_name":    rec.get("dm_name"),
                    "msg_rowid":  rec.get("msg_rowid"),
                }))

    def _handle_message(self, msg: dict):
        """Handle an incoming message — update Inbox and per-node RSSI/SNR/LastSeen."""
        msg_type  = msg.get("type", "")
        text      = msg.get("text", "")

        # De-duplicate. The sender stamps each message with sender_timestamp;
        # (sender, channel, timestamp, text) uniquely identifies it, so a
        # message redelivered via the get_msg() drain or repeated by a
        # duplicate flood (different path, same content/timestamp) is dropped.
        # Runs on the single main _dispatch thread, so no lock needed.
        sig = (
            msg_type,
            msg.get("pubkey_prefix", ""),
            msg.get("channel_idx"),
            msg.get("sender_timestamp"),
            text,
        )
        if sig in self._recent_msg_sigs:
            Domoticz.Debug(f"Duplicate message dropped: {sig!r}")
            return
        self._recent_msg_sigs.append(sig)

        # Resolve sender name
        prefix    = msg.get("pubkey_prefix", "")
        node_name = self._prefix_to_name.get(prefix, "").strip() if prefix else ""
        if prefix and not node_name:
            Domoticz.Debug(
                f"Incoming message: pubkey_prefix={prefix!r} did not match any known "
                f"contact (have {len(self._prefix_to_name)} prefixes). per-node "
                f"updates will be skipped."
            )

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

        # Channel tag: resolve index to name when available, fall back to C<idx>
        channel_idx = msg.get("channel_idx")
        if msg_type in ("CHAN", "channel_message") and channel_idx is not None:
            chan_tag = self._channel_names.get(channel_idx, f"C{channel_idx}")
        else:
            chan_tag = "P"

        # Decide the send time to embed. The sender stamps each message with
        # its own RTC (sender_timestamp). Trust it only if it's plausible:
        # not before 2020-01-01 and not more than 1h in the future relative
        # to our clock. Otherwise the node's RTC is wrong — fall back to our
        # system time and flag it so the dashboard can show that.
        now_i = int(time.time())
        st = msg.get("sender_timestamp") or 0
        if st and 1_577_836_800 <= st <= now_i + 3600:
            msg_ts, ts_bad = int(st), False
        else:
            msg_ts, ts_bad = now_i, True

        # Lifetime statistics (leaderboard + client/repeater/server split).
        _hops = msg.get("path_len")
        self._bump_msg_stats(node_name or display_name,
                             _hops if isinstance(_hops, int) else -1,
                             chan_tag)

        # Per-message signal, embedded in the wire line so the dashboard can
        # show the exact signal for THIS message (channel msgs have no pubkey
        # and no RX_LOG text match, so this is the only reliable source).
        _msg_snr = msg.get("SNR") if msg.get("SNR") is not None else msg.get("snr")
        _msg_rssi = msg.get("rssi")
        # Per-hop path hashes — present when the library back-filled it from
        # a matched RX_LOG frame (mostly channel msgs). Lets the dashboard
        # show / resolve the actual repeater chain.
        _msg_path = msg.get("path")

        # Update global inbox — [chan|sender|<epoch>[|x][|~h..|~s..|~r..|~p..]] text
        self._set(MESH_DID, UNIT_INBOX, 0,
                  self._inbox_line(chan_tag, display_name, text_body, msg_ts,
                                   ts_bad, snr=_msg_snr, hops=_hops,
                                   rssi=_msg_rssi, path=_msg_path))
        _dbg(f"incoming msg: chan={chan_tag!r} sender={display_name!r} "
             f"node={node_name!r} prefix={prefix!r} "
             f"peer_key={self._norm_peer_key(prefix)!r} ts={msg_ts} "
             f"body={text_body[:60]!r}")
        # Persist to SQLite message store (non-fatal).
        # Store hops only when it's a real count: exclude sentinel (255) and negatives.
        self._msg_store_add(
            chan=chan_tag, sender=display_name, body=text_body,
            epoch=msg_ts, bad=ts_bad, snr=_msg_snr,
            # path_len == HOPS_SENTINEL (255) means the firmware reported no
            # path info; a message that reached us directly is 0 hops, which
            # is more meaningful than "unknown". Real counts pass through;
            # anything else invalid -> None.
            hops=(0 if (isinstance(_hops, int) and _hops == HOPS_SENTINEL)
                  else _hops if (isinstance(_hops, int) and 0 <= _hops < HOPS_SENTINEL)
                  else None),
            rssi=_msg_rssi, path=_msg_path, ack=None, direction="in",
            peer_key=self._norm_peer_key(prefix),
        )
        # Ingest signal data for analytics (only when we have a reliable sender id).
        if prefix and (_msg_snr is not None or _msg_rssi is not None):
            _ts_pl = _hops if (isinstance(_hops, int) and 0 <= _hops < HOPS_SENTINEL) else None
            self._ts_ingest("msg", node_key=prefix[:12],
                            snr=_msg_snr, rssi=_msg_rssi, path_len=_ts_pl)
        # Record hop count.
        if isinstance(_hops, int) and 0 <= _hops < HOPS_SENTINEL:
            self._ts_hops_record(now_i, _hops)
        # Relay-key tally from the embedded per-message path (token width
        # follows the node's path_hash_mode: 2/4/6 hex chars).
        if _msg_path:
            _clean = re.sub(r"[^0-9a-fA-F]", "", str(_msg_path)).lower()
            _tok = self._path_token_chars()
            for _i in range(0, len(_clean) - _tok + 1, _tok):
                _rb = _clean[_i:_i + _tok]
                self._ts_relay_observed(_rb, name=self._resolve_relay_key(_rb))

        # Persist private (DM) messages to the sender's per-contact Messages
        # device so a favourite's conversation history is never lost.
        if chan_tag == "P" and node_name:
            self._log_contact_dm(node_name,
                self._inbox_line("P", display_name, text_body, msg_ts,
                                 ts_bad, snr=_msg_snr, hops=_hops,
                                 rssi=_msg_rssi, path=_msg_path))

        # dzVents command bridge: expose "!" commands received on the configured channel.
        if (self._dzv_enabled
                and self._dzv_channel
                and self._dzv_channel_match(chan_tag)
                and text_body.strip().startswith("!")):
            self._dzv_prune_origins()
            rid = self._dzv_next_id()
            pk_prefix = msg.get("pubkey_prefix", "")[:12]
            self._cmd_origins[rid] = {
                "kind": "chan",
                "chan": self._dzv_channel,
                "ts": time.time(),
            }
            self._dzv_in_seq += 1
            _snr_val = msg.get("SNR") if msg.get("SNR") is not None else msg.get("snr")
            payload = json.dumps({
                "id": rid,
                "seq": self._dzv_in_seq,
                "cmd": text_body.strip(),
                "sender": node_name or display_name,
                "pubkey": pk_prefix,
                "channel": self._dzv_channel,
                "snr": _snr_val,
                "ts": int(time.time()),
            })
            self._set(MESH_DID, UNIT_DZV_IN, 0, payload)
            Domoticz.Debug(f"dzVents bridge: rid={rid} cmd={text_body.strip()[:60]!r} sender={(node_name or display_name)!r} chan={self._dzv_channel!r}")

        # Update per-node devices for any known contact
        if node_name:
            # CONTACT_MSG_RECV carries the sender's pubkey prefix — register
            # it as the DeviceID so the device can be created on first message
            # even before the contacts poll runs.
            pk_prefix = msg.get("pubkey_prefix", "")
            if pk_prefix and node_name not in self._node_did:
                self._node_did[node_name] = pk_prefix[:12]

            # Latest received signal from this message — only when we have a
            # reliable identity (a pubkey prefix; channel msgs without one are
            # skipped to avoid mis-attribution). Covers non-favourite contacts
            # too (this is outside the device-only block below). RSSI is left
            # to adverts unless the message event actually carried one.
            if pk_prefix:
                _msnr = msg.get("SNR")
                if _msnr is None:
                    _msnr = msg.get("snr")
                with self._rx_log_lock:
                    self._contact_sig[pk_prefix[:12]] = {
                        "snr": float(_msnr) if _msnr is not None else None,
                        "rssi": msg.get("rssi"),
                        "path_len": msg.get("path_len", -1),
                        "t": time.time(), "source": "msg",
                    }

            self._ensure_node_devices(node_name)
            did = self._device_id_for(node_name)
            if did is not None:
                now_ts = int(time.time())
                # Record activity — used by _handle_contacts for online detection
                self._node_last_activity[node_name] = now_ts
                # Also advance last_advert so the dashboard's "Last Heard"
                # reflects any heard activity (advert OR message), not just
                # the periodic advert. This dict is what the device-map
                # snapshot publishes as `last_advert` (see _build_device_map),
                # and it is persisted via meshcore_devices.json so the value
                # survives plugin restarts.
                if now_ts > self._node_last_advert.get(node_name, 0):
                    self._node_last_advert[node_name] = now_ts

                # A message means the node is clearly reachable → mark online
                self._set(did, OFF_STATUS, 1, "On")

                # Last Seen
                self._set(did, OFF_LASTSEEN, 0, time.strftime("%Y-%m-%d %H:%M:%S"))

                # SNR from message metadata; RSSI from status poll (req_status_sync)
                snr = msg.get("SNR") if msg.get("SNR") is not None else msg.get("snr")
                if snr is not None:
                    self._set(did, OFF_SNR, 0, str(round(float(snr), 2)))
                if pk_prefix and snr is not None:
                    with self._rx_log_lock:
                        hist = self._signal_history.setdefault(pk_prefix, [])
                        hist.append({"t": time.time(), "snr": float(snr), "rssi": None,
                                     "path_len": msg.get("path_len", -1), "kind": "MSG"})
                        if len(hist) > 60:
                            del hist[: len(hist) - 60]
                    self._rx_log_dirty = True

        self._write_device_map()

        # Increment message received counter
        self._recv_count += 1
        self._set(MESH_DID, UNIT_MSGS_RECV, 0, str(self._recv_count))

    def _handle_self_stats(self, stats: dict):
        """Update devices for the connected (self) node from polled stats."""
        if not self._self_name:
            return
        self._ensure_node_devices(self._self_name)
        did = SELF_DID
        if did not in Devices:
            return

        def _upd(unit, nValue, sValue):
            self._set(did, unit, nValue, sValue)

        # Battery (millivolts) — from stats_core
        bat_mv = stats.get("battery_mv", 0)
        if bat_mv:
            pct = _bat_pct(bat_mv)
            v   = round(bat_mv / 1000, 2)
            _upd(OFF_BATT_PCT, pct, str(pct))
            _upd(OFF_BATT_V, 0, str(v))

        # Uptime (seconds → minutes)
        uptime_s = stats.get("uptime_secs", 0)
        if uptime_s:
            _upd(OFF_UPTIME, 0, str(round(uptime_s / 60, 1)))

        # Radio stats
        noise = stats.get("noise_floor")
        if noise is not None:
            _upd(OFF_NOISE, 0, str(noise))

        rssi = stats.get("last_rssi")
        if rssi is not None:
            _upd(OFF_RSSI, 0, str(rssi))

        snr = stats.get("last_snr")
        if snr is not None:
            _upd(OFF_SNR, 0, str(round(snr, 2)))

        # TX air seconds
        tx_air = stats.get("tx_air_secs")
        if tx_air is not None:
            _upd(OFF_AIRTIME, 0, str(tx_air))

        # Packet counters
        pkt_sent = stats.get("sent")
        if pkt_sent is not None:
            _upd(OFF_MSGS_SENT, 0, str(pkt_sent))

        pkt_recv = stats.get("recv")
        if pkt_recv is not None:
            _upd(OFF_MSGS_RECV, 0, str(pkt_recv))

        # Last seen = now (we just got data from it)
        _upd(OFF_LASTSEEN, 0, time.strftime("%Y-%m-%d %H:%M:%S"))

        # Analytics ingestion: radio sample for self node.
        _s_rssi  = stats.get("last_rssi")
        _s_snr   = stats.get("last_snr")
        _s_noise = stats.get("noise_floor")
        if _s_rssi is not None or _s_snr is not None:
            self._ts_ingest("rx", node_key="self",
                            rssi=_s_rssi, snr=_s_snr, noise=_s_noise)

        # Analytics ingestion: packet counter deltas for self node.
        _now_ts  = int(time.time())
        _pkt_rx  = stats.get("recv")
        _pkt_tx  = stats.get("sent")
        _pkt_frx = stats.get("flood_rx")
        _pkt_ftx = stats.get("flood_tx")
        _pkt_drx = stats.get("direct_rx")
        _pkt_dtx = stats.get("direct_tx")
        if _pkt_rx is not None and _pkt_tx is not None:
            def _wrap_delta(cur, prev):
                if prev is None:
                    return 0
                d = cur - prev
                return d if d >= 0 else cur  # wrap-around: treat as absolute gain

            drx = _wrap_delta(_pkt_rx, self._ts_prev_pkt_recv)
            dtx = _wrap_delta(_pkt_tx, self._ts_prev_pkt_sent)
            dfrx = _wrap_delta(_pkt_frx, self._ts_prev_pkt_flood_rx) if _pkt_frx is not None else 0
            dftx = _wrap_delta(_pkt_ftx, self._ts_prev_pkt_flood_tx) if _pkt_ftx is not None else 0
            ddrx = _wrap_delta(_pkt_drx, self._ts_prev_pkt_dir_rx)  if _pkt_drx is not None else 0
            ddtx = _wrap_delta(_pkt_dtx, self._ts_prev_pkt_dir_tx)  if _pkt_dtx is not None else 0
            if drx or dtx:
                self._ts_packets_add(_now_ts, drx, dtx, dfrx, dftx, ddrx, ddtx)
            self._ts_prev_pkt_recv     = _pkt_rx
            self._ts_prev_pkt_sent     = _pkt_tx
            self._ts_prev_pkt_flood_rx = _pkt_frx
            self._ts_prev_pkt_flood_tx = _pkt_ftx
            self._ts_prev_pkt_dir_rx   = _pkt_drx
            self._ts_prev_pkt_dir_tx   = _pkt_dtx

        Domoticz.Debug(f"Self stats updated: bat={bat_mv}mV uptime={uptime_s}s rssi={rssi} snr={stats.get('last_snr')}")
        self._write_device_map()

    def onCommand(self, DeviceID, Unit, Command, Level, Color):
        """dzVents command bridge: triggered when dzVents turns on UNIT_DZV_SEND."""
        try:
            if not self._dzv_enabled:
                return
            if DeviceID != MESH_DID or Unit != UNIT_DZV_SEND:
                return
            if str(Command).strip() != "On":
                return

            # Read the reply payload from UNIT_DZV_REPLY.
            try:
                reply_svalue = Devices[MESH_DID].Units[UNIT_DZV_REPLY].sValue
            except (KeyError, AttributeError):
                Domoticz.Error("dzVents bridge: UNIT_DZV_REPLY device not found")
                self._set(MESH_DID, UNIT_DZV_SEND, 0, "Off")
                return

            try:
                reply = json.loads(reply_svalue)
            except (ValueError, TypeError) as exc:
                Domoticz.Error(f"dzVents bridge: UNIT_DZV_REPLY JSON parse error: {exc!r} value={reply_svalue!r}")
                self._set(MESH_DID, UNIT_DZV_SEND, 0, "Off")
                return

            body = reply.get("text", "").strip()
            if not body:
                Domoticz.Error("dzVents bridge: reply JSON has no 'text' field")
                self._set(MESH_DID, UNIT_DZV_SEND, 0, "Off")
                return

            # Resolve the send target.
            sendstr = None
            if "to" in reply:
                # Explicit override: dzVents specified the target directly.
                to = str(reply["to"]).strip()
                if not to:
                    Domoticz.Error("dzVents bridge: reply 'to' override is empty")
                    self._set(MESH_DID, UNIT_DZV_SEND, 0, "Off")
                    return
                sendstr = f"{to}: {body}"
            else:
                rid = reply.get("id")
                if rid is None:
                    Domoticz.Error("dzVents bridge: reply JSON has no 'id' and no 'to'")
                    self._set(MESH_DID, UNIT_DZV_SEND, 0, "Off")
                    return
                origin = self._cmd_origins.get(rid)
                if origin is None:
                    Domoticz.Error(f"dzVents bridge: unknown or expired origin id={rid!r}")
                    self._set(MESH_DID, UNIT_DZV_SEND, 0, "Off")
                    return
                kind = origin.get("kind")
                if kind == "P":
                    name = origin.get("name", "")
                    if not name:
                        Domoticz.Error(f"dzVents bridge: origin id={rid!r} has no contact name")
                        self._set(MESH_DID, UNIT_DZV_SEND, 0, "Off")
                        return
                    sendstr = f"{name}: {body}"
                elif kind == "chan":
                    chan = origin.get("chan", "")
                    sendstr = f"#{chan}: {body}"
                else:
                    Domoticz.Error(f"dzVents bridge: unrecognised origin kind={kind!r} for id={rid!r}")
                    self._set(MESH_DID, UNIT_DZV_SEND, 0, "Off")
                    return
                self._cmd_origins.pop(rid, None)

            loop = self._worker_loop
            if loop is None:
                Domoticz.Error("dzVents bridge: worker not running, cannot send reply")
                self._set(MESH_DID, UNIT_DZV_SEND, 0, "Off")
                return

            Domoticz.Debug(f"dzVents bridge: dispatching reply send={sendstr!r}")
            try:
                asyncio.run_coroutine_threadsafe(
                    self._send_message_for_text(sendstr, None), loop
                )
            except Exception as exc:
                Domoticz.Error(f"dzVents bridge: dispatch failed: {exc!r}")

            self._set(MESH_DID, UNIT_DZV_SEND, 0, "Off")
        except Exception as exc:
            Domoticz.Error(f"dzVents bridge onCommand error: {exc!r}")


# ── Domoticz plugin entry points ─────────────────────────────────────────────

_plugin = BasePlugin()

def onStart():                                                   _plugin.onStart()
def onStop():                                                    _plugin.onStop()
def onHeartbeat():                                               _plugin.onHeartbeat()
def onWebSocketMessage(Data):                                    _plugin.onWebSocketMessage(Data)
def onCommand(DeviceID, Unit, Command, Level, Color):            _plugin.onCommand(DeviceID, Unit, Command, Level, Color)
