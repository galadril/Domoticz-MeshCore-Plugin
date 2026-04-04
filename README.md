

# 📡 Domoticz-MeshCore-Plugin

![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)  
🔗 _MeshCore LoRa Mesh integration for Domoticz Home Automation_

This plugin connects your **[MeshCore LoRa mesh nodes](https://meshcore.co.uk/)** to [Domoticz](https://www.domoticz.com/), exposing node telemetry, message inbox, and send controls as native Domoticz devices — ready for automations, dashboards and scripting.

> 📻 Track battery, signal quality and uptime of your connected node · 📨 Send & receive LoRa messages · 🖥️ Custom real-time dashboard included!

----------

## ✨ Features

- **Auto-discovery** – All contacts advertised by the mesh are automatically discovered and tracked
- **Self-node Telemetry** – Battery, voltage, RSSI, SNR, noise floor, uptime, airtime and packet counters for the connected node
- **Remote Node Status** – Online/offline detection, SNR, hops and last-seen timestamp for every discovered contact
- **Online / Offline Detection** – Automatic status based on last advertisement age (8 h threshold) or path availability
- **Message Inbox** – Every received LoRa message appears in Domoticz with sender, channel and timestamp
- **Send Messages** – Send direct or channel messages straight from Domoticz or the dashboard
- **Reply Support** – Reply to any message directly from the dashboard inbox
- **Custom Dashboard** – Built-in real-time dark-mode dashboard with node cards, message history, filters and compose bar
- **Node Map** – Interactive dark-themed map showing nodes with GPS coordinates (auto-hidden when no location data available)
- **Signal Quality Bars** – Visual SNR indicator with color-coded bars (excellent/good/fair/poor)
- **Uptime Formatting** – Human-readable uptime display (e.g. "2d 5h 12m" instead of raw minutes)
- **Message Counters** – Track total messages received and sent as Domoticz devices for automations
- **Emoji Picker** – Full WhatsApp-style emoji picker in the compose bar
- **@Mention Highlighting** – `@name` mentions are highlighted in the message history
- **Channel Support** – Send to named channels with automatic channel name resolution
- **Push Events** – Incoming messages, advertisements and status responses are received in real time
- **TCP Keep-alive** – Automatic TCP keep-alive to prevent NAT table expiry
- **Stale Connection Detection** – Automatic reconnect when no push events are received for 10 minutes
- **Auto-reconnect** – Worker thread reconnects automatically after connection loss

----------

## 📊 Screenshot 
![screenshot](https://github.com/galadril/Domoticz-MeshCore-Plugin/blob/main/docs/images/screenshot.png?raw=true "Screenshot")


## ⚙️ Installation

> ✅ Domoticz with Python plugin support & Python 3.6+ required

### Prerequisites

- Domoticz installed and running
- A [MeshCore](https://meshcore.co.uk/) node reachable over TCP (companion app or radio bridge)
- Python package:

```sh
pip install -r requirements.txt
```

### Setup

```sh
cd ~/domoticz/plugins
git clone https://github.com/galadril/Domoticz-MeshCore-Plugin.git MeshCore
sudo service domoticz.sh restart
```

Then go to **Setup → Hardware** and add a new hardware entry of type **MeshCore**.

----------

## 🛠 Configuration

| Field | Description |
|---|---|
| **MeshCore Host** | IP address of the MeshCore TCP endpoint |
| **MeshCore Port** | TCP port (default `5000`) |
| **Install Custom Dashboard** | Yes / No — installs `meshcore.html` into Domoticz templates (default Yes) |
| **Debug Level** | None / Basic / All |

> All contacts discovered by the mesh are tracked automatically — there is no manual node list to configure.

----------

## 🧾 Devices Created

Devices are created automatically on first data received for each node.

### Global devices

| Device | Type | Description |
|---|---|---|
| **Mesh Inbox** | Text | Last received message — format `[C0\|sender] text` or `[P\|sender] text` |
| **Mesh Send** | Text | Write here to send a message (see below) |
| **Mesh Msgs Received** | Custom (msgs) | Running counter of messages received since plugin start |
| **Mesh Msgs Sent** | Custom (msgs) | Running counter of messages successfully sent since plugin start |

### Self (connected) node — units 10–29

| Device | Type |
|---|---|
| Status | Switch (always On when connected) |
| Battery | Percentage |
| Battery V | Custom (V) |
| RSSI | Custom (dBm) |
| SNR | Custom (dB) |
| Noise Floor | Custom (dBm) |
| Last Seen | Text |
| Uptime | Custom (min) |
| Airtime TX | Custom (s) |
| Pkts Sent | Custom (pkts) |
| Pkts Recv | Custom (pkts) |

### Remote (discovered) nodes — units 30+

Each discovered contact gets a minimal device set:

| Device | Type |
|---|---|
| Status | Switch (online / offline) |
| SNR | Custom (dB) |
| Last Seen | Text |
| Hops | Custom (hops) — path length to reach the node |

> Additional devices (battery, RSSI, noise floor, uptime, airtime, packet counters) are created for remote nodes when a STATUS_RESPONSE push event is received — requires supported firmware on the remote node.

----------

## 📨 Sending Messages

Write to the **Mesh Send** device via the Domoticz API, a script, or the custom dashboard.

| Syntax | Result |
|---|---|
| `hello world` | Direct message to the first discovered contact |
| `garden: hello` | Direct message to the node named `garden` |
| `#0: hello` | Broadcast on channel 0 (flood) |
| `#flood: hello` | Broadcast on channel 0 (alias) |

----------

## 📜 dzVents Scripting Example

A ready-to-use **dzVents script** is included that sends periodic home-status reports to a MeshCore channel — perfect for keeping an eye on your house via LoRa.

The script sends **readable themed messages** (Climate, Weather, Energy) spaced 45 seconds apart, plus **instant alerts** on presence changes.

**Example output on the mesh:**
```
Climate: Indoor 20.3C, 52% | Thermostat 19.5C
Weather: 14.8C, 65%
Energy: Solar 1240W | Delivery 380W | Gas today 0.42 m3
```

➡️ **[Download the demo script](docs/meshcore_status_report.lua)**

Copy it to `~/domoticz/scripts/dzVents/generated_scripts/`, edit the `CONFIGURATION` section at the top to match your device names and channel index, and enable it.

----------

## 📊 Custom Dashboard

Enable **Install Custom Dashboard** in the plugin settings, then navigate to:

```
Setup → More Options → Custom Pages → meshcore
```

> 🌐 **Live demo:** [galadril.github.io/Domoticz-MeshCore-Plugin](https://galadril.github.io/Domoticz-MeshCore-Plugin/#)

### Dashboard features

- **Node cards** — online/offline badge, battery bar, signal quality bars (SNR), hops, last seen — every value links to its Domoticz device log
- **Signal quality bars** — color-coded visual SNR indicator (green = excellent, yellow = fair, red = poor)
- **Uptime formatting** — human-readable display like "2d 5h 12m" instead of raw minutes
- **Node map** — collapsible interactive Leaflet.js map with dark CARTO tiles showing all nodes that report GPS coordinates — auto-hidden when no location data is available, supports manual coordinate overrides
- **Manual node locations** — place a `meshcore_locations.json` in the plugin folder to pin nodes without GPS on the map (see below)
- **Message inbox** — full scrollable history with timestamps, channel tags and sender names
- **Channel & search filters** — filter messages by channel or search by sender / text
- **Compose bar** — select a channel or direct target, type and send
- **Reply** — hover any message and click ↩ Reply to pre-fill the compose bar with the right target and channel
- **@mention highlighting** — `@name` tokens are highlighted in green in message text
- **Emoji picker** — full categorised emoji picker (700+ emoji, WhatsApp-style) with search
- **Auto-refresh** — live updates every 10 seconds

----------

## 🗺️ Manual Node Locations

If some of your nodes don’t broadcast GPS coordinates, you can manually pin them on the dashboard map.

Create a file called `meshcore_locations.json` in the plugin directory (`domoticz/plugins/MeshCore/`):

```json
{
    "Garden": {"lat": 52.3690, "lon": 4.9075},
    "Garage": {"lat": 52.3665, "lon": 4.9010}
}
```

- Node names must match the contact names exactly (case-sensitive)
- Live GPS data from nodes automatically overrides manual coordinates
- The file is loaded on plugin start and copied to the dashboard
- The map section only appears when at least one node has coordinates (from GPS or manual)

----------

## 🔄 Poll Intervals

| What | Interval |
|---|---|
| Contacts refresh (incremental) | every 30 s |
| Liveness probe (full contacts refresh) | every 5 min |
| Self-node stats (battery, RSSI, uptime, counters) | every 5 min |
| Stale connection detection | 10 min without push events triggers reconnect |

----------

## 🔁 Updating

```sh
cd ~/domoticz/plugins/MeshCore
git pull
sudo service domoticz.sh restart
```

----------

## 🧩 Troubleshooting

Enable **Basic** or **All** debug logging in plugin settings for verbose logs in the Domoticz log viewer.

| Problem | Solution |
|---|---|
| `meshcore package not installed` | Run `pip install meshcore` on the Domoticz machine, then restart |
| Connection errors | Verify host/port are reachable; check the MeshCore companion app is running |
| Node not appearing | The plugin auto-discovers all contacts — make sure the node is advertising on the mesh; check the log for discovered contact names |
| Battery / stats missing for remote nodes | Requires firmware support for STATUS_RESPONSE push events |
| Connection drops silently | The plugin detects stale connections after 10 min and reconnects; enable debug logging to diagnose |

----------

## 🕘 Changelog

| Version | Notes |
|---|---|
| 0.0.1 | Initial release — telemetry, inbox, send, custom dashboard |

----------

## 💬 Support

For bugs or feature requests please use [GitHub Issues](https://github.com/galadril/Domoticz-MeshCore-Plugin/issues).

----------

## ☕ Donate

If this plugin saves you time, consider buying me a coffee (or 🍺 beer)!

[![Donate](https://img.shields.io/badge/paypal-donate-yellow.svg?logo=paypal)](https://www.paypal.me/markheinis)

----------

## 📄 License

This project is licensed under the **MIT License**.  
See the [LICENSE](LICENSE) file for details.
