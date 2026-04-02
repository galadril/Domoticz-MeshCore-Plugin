# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Domoticz plugin that integrates MeshCore LoRa mesh nodes as automation devices. MeshCore is treated as a telemetry/event backend — the plugin exposes only useful automation data, not the full MeshCore contact/channel structure.

## Development

No build system or test framework is configured yet. The plugin runs inside the Domoticz Python plugin runtime — it cannot be executed standalone.

To test manually: copy `plugin.py` and `meshcore.html` into a Domoticz plugin folder (e.g. `domoticz/plugins/MeshCore/`) and restart Domoticz. Logs appear in the Domoticz log viewer.

The `Domoticz` module (imported in `plugin.py`) is provided by the Domoticz runtime and is not installable via pip. When writing logic outside of Domoticz lifecycle hooks, keep it in plain Python so it can be tested independently.

## Architecture

### Plugin lifecycle (Domoticz pattern)

Domoticz calls module-level functions (`onStart`, `onStop`, `onHeartbeat`). These delegate to a singleton `BasePlugin` instance. Never block the main thread — use a worker thread for I/O.

### Implementation (meshcore Python package over TCP)

```
Domoticz Plugin
  ├── Worker thread (asyncio event loop)
  │     ├── MeshCore.create_tcp(host, port)         — connects via meshcore package
  │     ├── mc.commands.get_contacts()              — refresh contact list every 30s
  │     ├── mc.commands.get_msg()                   — drain message queue every 30s
  │     ├── mc.commands.get_stats_core()            — self node: battery, uptime every 5 min
  │     ├── mc.commands.get_stats_radio()           — self node: RSSI, SNR, noise every 5 min
  │     ├── mc.commands.get_stats_packets()         — self node: pkt counters every 5 min
  │     ├── EventType.CONTACT_MSG_RECV push         — live messages from known contacts
  │     ├── EventType.CHANNEL_MSG_RECV push         — live channel messages
  │     ├── EventType.ADVERTISEMENT push            — node broadcast adverts
  │     └── Puts results on a queue
  └── onHeartbeat()
        └── Drains queue → updates Domoticz devices
```

### What data is actually available (verified against live hardware)

| Source | Data |
|---|---|
| Self node (`get_stats_core`) | `battery_mv`, `uptime_secs`, `errors`, `queue_len` |
| Self node (`get_stats_radio`) | `noise_floor`, `last_rssi`, `last_snr`, `tx_air_secs`, `rx_air_secs` |
| Self node (`get_stats_packets`) | `recv`, `sent`, `flood_tx`, `direct_tx`, `flood_rx`, `direct_rx` |
| Contacts list | `adv_name`, `last_advert`, `out_path_len`, `adv_lat`, `adv_lon`, `type`, `public_key` |
| Incoming messages (get_msg / push) | `text`, `SNR`, `path_len`, `sender_timestamp`, `type` (CHAN/PRIV) |
| Advertisement push | `adv_name`, `adv_lat`, `adv_lon`, `adv_timestamp` |

> **Note:** `send_statusreq()` (old text API) does NOT return data from remote nodes. Use `req_status_sync()` (binary API) instead — it sends a binary status request and waits for `STATUS_RESPONSE` containing `bat`, `last_rssi`, `last_snr`, `noise_floor`, `uptime`, etc. Remote nodes may still not respond if offline or firmware-limited.

### Dependencies

- `pip install meshcore` — Python package for MeshCore TCP communication

### Domoticz devices

| Unit range | Device | Type |
|---|---|---|
| 1 | Mesh Inbox | Text |
| 2 | Mesh Send | Text |
| 3 | Mesh Msgs Received | Custom (msgs) |
| 4 | Mesh Msgs Sent | Custom (msgs) |
| 10 + (node_idx × 20) + offset | Per-node devices (see below) | various |

Node index 0 = self (connected) node. Index 1..N = remote contacts (auto-discovered from mc.contacts).

#### Self node devices (index 0, units 10–29)

| Offset | Device | Type |
|---|---|---|
| 0 | Status | Switch (always On when connected) |
| 1 | Battery % | Percentage |
| 2 | Battery V | Custom (V) |
| 3 | RSSI | Custom (dBm) |
| 4 | SNR | Custom (dB) |
| 5 | Last Seen | Text |
| 6 | Noise Floor | Custom (dBm) |
| 10 | Uptime | Custom (min) |
| 11 | Airtime TX | Custom (s) |
| 12 | Pkts Sent | Custom (pkts) |
| 13 | Pkts Recv | Custom (pkts) |

#### Remote node devices (index 1..N, units 30+)

Only data that is reliably available without over-the-air requests:

| Offset | Device | Type |
|---|---|---|
| 0 | Status | Switch (On/Off based on last_advert age < 8h) |
| 4 | SNR | Custom (dB) — from incoming messages |
| 6 | Last Seen | Text |
| 9 | Hops | Custom (hops) — from contact out_path_len |

### Poll intervals

| What | Interval | Constant |
|---|---|---|
| Message drain + contacts refresh | 30 s | `MSG_POLL_INTERVAL` |
| Self-node stats (core + radio + packets) | 300 s | `SELF_STATS_INTERVAL` |

### Config (Domoticz hardware params)

| Param field | Content |
|---|---|
| Address | MeshCore TCP host |
| Port | TCP port (default 5000) |
| Mode4 | Install custom dashboard page (`"true"` / `"false"`) |
| Mode6 | Debug level (0 / 62 / -1) |

### Custom dashboard page

`meshcore.html` is a self-contained HTML+JS dashboard (external dependency: Leaflet.js CDN for the node map). On `onStart`, if Mode4 is `"true"`, the plugin copies `meshcore.html` verbatim to `<domoticz_root>/www/templates/meshcore.html`. The page is removed on `onStop`.

Dashboard features:
- Node cards with live telemetry, battery bars, and signal quality bars (SNR)
- Human-readable uptime formatting (e.g. "2d 5h 12m" instead of raw minutes)
- Collapsible node map (Leaflet.js + dark CARTO tiles) showing nodes with GPS coordinates
- Manual location overrides via `meshcore_locations.json` in the plugin directory
- Message inbox with filters, search, compose bar, emoji picker, and @mention highlighting
- The map only appears when at least one node has coordinates (from `adv_lat`/`adv_lon` or manual overrides)
- The map is collapsed by default to keep the chat visible; click the header to expand

The page derives all node names dynamically from the Domoticz JSON API (`/json.htm?type=command&param=getdevices&order=Name`) — no template injection needed:
- **Self node**: identified by a device whose name ends in `" Uptime"` (unique to the connected node)
- **Remote nodes**: identified by devices whose name ends in `" Hops"` (unique to tracked nodes)

The page fetches live device data and auto-refreshes every 10 seconds. Each metric links to `/index.html#/Devices/{idx}/Log`.

The self node card shows: Battery, Voltage, RSSI, SNR, Noise Floor, Uptime, TX Air, Pkts Sent, Pkts Recv, Last Seen.
Remote node cards show: Battery, Voltage, RSSI, SNR, Hops, Last Seen.

Send functionality has been intentionally removed.
