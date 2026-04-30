# 🏭 Station Monitor — API Protocol Guide

> **Next Robotics Lab** · Industrial Modbus-TCP Station Management System
> Flask REST API · Python 3.8+ · Multi-Station · Real-time monitoring, control & simulation

---

## 📋 Table of Contents

- [Overview](#-overview)
- [Architecture](#-architecture)
- [Files Reference](#-files-reference)
- [Quick Start](#-quick-start)
- [Authentication](#-authentication)
- [Station Status Values](#-station-status-values)
- [Coil / LED Mapping](#-coil--led-mapping)
- [API Endpoints](#-api-endpoints)
  - [Station Status](#station-status)
  - [Hold (Standby)](#hold-standby)
  - [Alarm](#alarm)
  - [Heartbeat](#heartbeat)
  - [Health Check](#health-check)
  - [Simulation Mode](#simulation-mode)
  - [IP Scan](#ip-scan)
  - [Device IP](#device-ip)
- [Hold + FULL Wait Flow](#-hold--full-wait-flow)
- [Simulation Mode Guide](#-simulation-mode-guide)
- [IP Discovery](#-ip-discovery)
- [Auto-Scan Features](#-auto-scan-features)
- [Log Files](#-log-files)
- [Error Codes](#-error-codes)
- [Configuration Reference](#-configuration-reference)
- [Data Flow Diagram](#-data-flow-diagram)

---

## 🔍 Overview

The Station Monitor manages industrial stations on a factory floor. Each station communicates via **Modbus-TCP** through `client.py`, which polls sensor data every second and reports it to `server_sim.py`. The server manages state, hold/alarm logic, and simulation.

**Key features:**
- ✅ Real-time station status at 1-second poll rate
- ✅ Hold / Release with robot task coordination
- ✅ Hold allowed on FULL stations — robot waits until station clears automatically
- ✅ Per-device **Simulation Mode** — test without physical hardware
- ✅ Automatic IP discovery via nmap + ARP
- ✅ Fixed 10-second reconnect retry (no exponential backoff ceiling)
- ✅ State persistence across server restarts (`state.json`, `state_sim.json`)
- ✅ `connect.log` — first connect, reconnect, disconnect per device
- ✅ `operation.log` — hold start/end/restore events
- ✅ 7-day log retention with automatic cleanup

---

## 🏗 Architecture

| Component | File | Role |
|---|---|---|
| **API Server** | `server_sim.py` | Central state manager. Receives sensor data, handles hold/alarm/sim, serves dashboard |
| **Modbus Client** | `client.py` | Polls each station via Modbus-TCP every 1s, POSTs to server |
| **IP Discovery** | `ip.py` | CLI tool: scans subnet via nmap+ARP, writes `device_ip.json` |

---

## 📁 Files Reference

| File | Purpose |
|---|---|
| `devices.json` | **Input** — maps device ID → MAC address |
| `device_ip.json` | **Auto-managed** — maps device ID → IP (written by `ip.py` or `/scan`) |
| `state.json` | **Auto-managed** — persists hold/alarm state across server restarts |
| `state_sim.json` | **Auto-managed** — persists simulation state across server restarts |
| `operation.log` | Hold start/end/restore events (10 MB × 10 files, 7-day retention) |
| `connect.log` | CONNECT_FIRST / CONNECT_LAST / DISCONNECT events (5 MB × 14 files, 7-day retention) |
| `client.log` | Modbus reads, reconnect events (5 MB × 5 files, 7-day retention) |

### `devices.json` format

```json
{
  "10001": "D4-AD-20-CA-69-81",
  "20001": "D4-AD-20-CA-63-05",
  "30021": "D4-AD-20-E1-53-89",
  "30031": ""
}
```

> Devices with empty MAC (`""`) are known IDs with no physical hardware yet — can be used in simulation mode.

---

## ⚡ Quick Start

### 1. Install dependencies

```bash
pip install flask pyModbusTCP requests
sudo apt install nmap
```

### 2. Run IP discovery

```bash
sudo python3 ip.py --gateway 192.168.20.0/24
```

### 3. Start the server

```bash
python3 server_sim.py
```

### 4. Start the Modbus client

```bash
python3 client.py
```

---

## 🔑 Authentication

All endpoints **except** `GET /health` require:

```http
X-API-Key: nextroboticslab2024
```

```javascript
// Node-RED / JavaScript
msg.headers = { 'X-API-Key': 'nextroboticslab2024' };
```

---

## 🚦 Station Status Values

The server **derives** status — clients only send raw `input` (0 or 1).

| Status | Condition |
|---|---|
| `NORMAL` | No hold, no alarm, sensor = 0 |
| `FULL` | Sensor = 1 (station occupied), or hold=true with sensor=1 |
| `BUSY` | `hold=true` AND sensor = 0 — robot reserved, station is clear |
| `ALARM` | `alarm=true`, no hold, sensor = 0 |
| `OFFLINE` | No heartbeat received within `STALE_THRESHOLD` seconds (default 5s) |

> **Sim mode:** When `sim_active=true`, the virtual `sim_input` replaces the real sensor for status derivation. The `input` field in GET responses also reflects the virtual value while sim is active.

---

## 💡 Coil / LED Mapping

`client.py` writes Modbus coils based on the status returned by the server:

| Coil | Color | Active When |
|---|---|---|
| Coil 0 | 🟢 Green | `status = NORMAL` |
| Coil 1 | 🟡 Yellow | `status = BUSY` |
| Coil 2 | 🔴 Red | `status = FULL` |
| Coil 3 | 🔔 Buzzer/Alarm | `status = ALARM` |

Only one coil is active at a time.

> When a robot holds a FULL station while waiting, the LED stays 🔴 Red until the item is removed, then switches automatically to 🟡 Yellow (BUSY).

> In simulation mode, coil writes still happen based on the simulated status. To skip physical coil writes during simulation, uncomment the `is_sim: continue` block in `client.py`.

---

## 📡 API Endpoints

Base URL: `http://<server-ip>:5000`

---

### Station Status

#### `POST /station/status`
Called by `client.py` every 1 second. Sends raw physical sensor reading.

**Auth:** ✅ Required · **Rate limit:** 5 req/s per device

**Request:**
```json
{ "id": 10001, "input": 0, "timestamp": "2026-04-27 09:00:00" }
```

| Field | Type | Description |
|---|---|---|
| `id` | `integer` | Device ID |
| `input` | `0` or `1` | Physical sensor value only — server computes status |
| `timestamp` | `string` | `"YYYY-MM-DD HH:MM:SS"` |

**Response `201`:**
```json
{
  "message":    "OK",
  "status":     "NORMAL",
  "hold_mode":  false,
  "alarm_mode": false,
  "taskID":     null,
  "robotID":    null,
  "simulated":  false
}
```

> `simulated: true` tells `client.py` that sim mode is active for this device. Used to optionally skip physical coil writes.

---

#### `GET /station/status/all`
Returns state snapshot of all registered devices.

**Auth:** ✅ Required

**Response `200`:**
```json
{
  "10001": {
    "hold": false, "alarm": false,
    "status": "NORMAL",
    "input": 0,
    "timestamp": "2026-04-27 09:00:01",
    "taskID": null, "robotID": null,
    "sim_active": false, "sim_input": 0
  },
  "10008": {
    "hold": true, "alarm": false,
    "status": "BUSY",
    "input": 0,
    "taskID": "TASK-042", "robotID": "SMR-01",
    "sim_active": true, "sim_input": 0
  }
}
```

> When `sim_active=true`, the `input` field reflects `sim_input` (virtual sensor), not the real physical sensor.

---

#### `GET /station/status/<id>`
Returns state for a single device. Same structure as one entry from `/station/status/all`.

Returns `HTTP 404` + `ret_code 4020` if device not registered.

---

### Hold (Standby)

#### `POST /station/standby`

Reserve or release a station for robot operation.

**Auth:** ✅ Required

**Request — Set Hold:**
```json
{
  "id":      10005,
  "hold":    true,
  "robotID": "SMR-01",
  "taskID":  "TASK-042"
}
```

**Request — Release Hold:**
```json
{ "id": 10005, "hold": false, "robotID": "SMR-01" }
```

**Response `200` — station was clear (sensor=0):**
```json
{
  "message": "Device 10005 hold=True",
  "hold":    true,
  "status":  "BUSY",
  "taskID":  "TASK-042",
  "robotID": "SMR-01"
}
```

**Response `200` — station is occupied (sensor=1):**
```json
{
  "message": "Device 10005 hold=True",
  "hold":    true,
  "status":  "FULL",
  "taskID":  "TASK-042",
  "robotID": "SMR-01"
}
```

> When `status=FULL`, hold is registered and reserved. The robot polls `GET /station/status/<id>` until status becomes `BUSY`.

**Hold Business Rules:**

| Condition | HTTP | ret_code | Behaviour |
|---|---|---|---|
| Station FULL when hold is set | `200` | — | ✅ Allowed — hold registered, status=FULL, robot waits |
| Station NORMAL when hold is set | `200` | — | ✅ Allowed — status=BUSY immediately |
| Already held by another robot | `409` | `4030` | ❌ Rejected |
| Wrong robot tries to release | `403` | `4030` | ❌ Only holding robot can release |
| Device OFFLINE | `409` | `4022` | ❌ Rejected — device unreachable |
| Device not registered | `404` | `4020` | ❌ Device must POST `/station/status` first |

---

### Alarm

#### `POST /station/alarm`

**Auth:** ✅ Required

```json
{ "id": 10005, "alarm": true }
```

**Response `200`:** `{ "message": "Device 10005 alarm=True" }`

Returns `409` + `ret_code 4022` if device is OFFLINE.

---

### Heartbeat

#### `POST /heartbeat`
Called by `client.py` every 1 second per device.

```json
{
  "id": 10001,
  "start_time":    "2026-04-27T08:00:00",
  "last_seen":     "2026-04-27 09:00:01",
  "timeout_count": 0,
  "post_ok":       3600,
  "post_fail":     0,
  "reconnects":    0
}
```

#### `GET /heartbeat/all`
Latest heartbeat for every device.

#### `GET /heartbeat/<id>`
Latest heartbeat for one device. Returns `HTTP 404` + `ret_code 4021` if never received.

**Example response:**
```json
{
  "id": 10001,
  "start_time":    "2026-04-27T08:00:00",
  "last_seen":     "2026-04-27 09:00:01",
  "timeout_count": 0,
  "post_ok":       3600,
  "post_fail":     0,
  "reconnects":    1
}
```

---

### Health Check

#### `GET /health`
**No authentication required.**

```json
{
  "server_time":         "2026-04-27 09:00:05",
  "total_devices":       16,
  "disconnected_device": [],
  "buzzer_on":           [],
  "hold_devices": [
    { "id": 10005, "taskID": "TASK-042", "robotID": "SMR-01" }
  ]
}
```

---

### Simulation Mode

Per-device simulation — test hold/alarm/status flow without physical hardware. When a device is in sim mode:
- Real Modbus sensor value is ignored
- Virtual `sim_input` (0 or 1) is used instead for all status calculations
- The `input` field in GET responses reflects `sim_input`
- Device is never considered OFFLINE (bypasses heartbeat check)
- Devices are auto-registered in `devices_db` if they don't exist yet

---

#### `POST /simulation/mode`
Enable or disable simulation for one device.

**Auth:** ✅ Required

```json
{ "id": 30022, "active": true }
```

**Response `200`:**
```json
{ "id": 30022, "sim_active": true, "sim_input": 0, "status": "NORMAL" }
```

> Devices not in `devices.json` or never connected can still be simulated — they are auto-registered on first `POST /simulation/mode`.

---

#### `POST /simulation/input`
Set the virtual sensor value (0 = empty, 1 = full).

**Auth:** ✅ Required

```json
{ "id": 30022, "input": 1 }
```

**Response `200`:**
```json
{ "id": 30022, "sim_input": 1 }
```

> The status changes immediately on the server without waiting for the next `client.py` POST cycle.

---

**Complete simulation flow example:**

```bash
# 1. Enable sim for device 30022
POST /simulation/mode   { "id": 30022, "active": true }

# 2. Set sensor to FULL
POST /simulation/input  { "id": 30022, "input": 1 }
# → GET /station/status/30022 shows: { "status": "FULL", "input": 1, "sim_active": true }

# 3. Robot holds the station
POST /station/standby   { "id": 30022, "hold": true, "robotID": "SMR-01", "taskID": "T-001" }
# → { "status": "FULL", "hold": true }  ← robot waits

# 4. Simulate item removed
POST /simulation/input  { "id": 30022, "input": 0 }
# → { "status": "BUSY", "hold": true }  ← robot proceeds

# 5. Robot done
POST /station/standby   { "id": 30022, "hold": false, "robotID": "SMR-01" }
# → { "status": "NORMAL", "hold": false }

# 6. Disable sim
POST /simulation/mode   { "id": 30022, "active": false }
# → Real physical sensor resumes
```

---

### IP Scan

#### `POST /scan`
Trigger nmap + ARP sweep to rediscover device IPs.

**Auth:** ✅ Required · Returns `409` if scan already running.

```json
{ "subnet": "192.168.20.0/24", "timeout": 60 }
```

**Response `200`:**
```json
{
  "subnet":     "192.168.20.0/24",
  "total":      16,
  "found":      15,
  "missing":    [10007],
  "matched":    { "10001": "192.168.20.37", "10002": "192.168.20.48" },
  "scanned_at": "2026-04-27 09:01:00"
}
```

#### `GET /scan/result`
Last scan result. Returns `404` if no scan has run yet.

#### `GET /scan/status`
```json
{ "running": false }
```

---

### Device IP

#### `GET /device_ip/version`
Version number of `device_ip.json`. Clients poll this — when version changes they reload IPs.

```json
{ "version": 3 }
```

#### `GET /device_ip`
Current `device_ip.json` contents + version.

```json
{
  "version": 3,
  "device_ip": { "10001": "192.168.20.37", "10002": "192.168.20.48" }
}
```

---

## 🔄 Hold + FULL Wait Flow

```
Robot → POST /station/standby { hold: true, robotID: "SMR-01", taskID: "TASK-042" }
                   │
          sensor = 1 (FULL — item present)
                   │
                   ▼
     Response 200: { "status": "FULL", "hold": true,
                     "taskID": "TASK-042", "robotID": "SMR-01" }
                   │
                   ▼
     Robot polls GET /station/status/10005 every 1s
                   │
          sensor=1 → status: "FULL" → keep waiting ...
                   │
          Item removed from station
                   │
          sensor drops to 0 ← detected by client.py
                   │
                   ▼
     Next poll: { "status": "BUSY", "hold": true,
                  "taskID": "TASK-042", "robotID": "SMR-01" }
                   │
                   ▼
     Robot sees BUSY → approaches station ✅
                   │
                   ▼
     Robot done → POST /station/standby { hold: false, robotID: "SMR-01" }
                   │
                   ▼
     { "status": "NORMAL", "hold": false }
```

---

## Simulation Mode Guide

### When to use

- Testing robot integration without physical PLCs
- Verifying hold/alarm/wait logic end-to-end
- Testing new device IDs (`30031`, `30032`, etc.) not yet wired
- Demo and training

### Dashboard controls

| Button | Action |
|---|---|
| **SIM** | Toggle simulation ON/OFF for this device |
| **SENS:0 / SENS:1** | Toggle virtual sensor (enabled only when SIM is ON) |

When SIM is ON:
- Row background turns light blue
- `Input` column shows virtual value with `[S]` indicator
- Status changes immediately when SENS is toggled
- HOLD and ALARM buttons are enabled even when device is OFFLINE

### Persistence

`state_sim.json` saves all simulation states. After server restart, sim devices that were active are automatically restored.

### Coil write behavior

By default, `client.py` still writes physical coils based on simulated status. To prevent this during simulation, uncomment in `client.py`:

```python
## SIM — uncomment to skip coil writes during simulation
# if is_sim:
#     continue
```

---

## 🌐 IP Discovery

### Automatic (server-side)

The server runs scans automatically:

| Trigger | When |
|---|---|
| **Startup** | Once at server start (background, non-blocking) |
| **Periodic** | Every 10 minutes |
| **Disconnect** | When a device goes OFFLINE — rescans after 30s debounce |

### Manual — `ip.py`

```bash
# Auto-detect subnet
sudo python3 ip.py

# Specify subnet
sudo python3 ip.py --gateway 192.168.20.0/24

# Custom timeout
sudo python3 ip.py --gateway 192.168.20.0/24 --timeout 30
```

`ip.py` reads `devices.json`, runs `nmap -sn`, parses `arp -an` (with `/proc/net/arp` fallback on Linux for speed), and writes `device_ip.json`.

> Devices with empty MAC in `devices.json` are skipped by `ip.py` — they are simulation-only devices.

---

## 📝 Log Files

### `connect.log`

Records device connection lifecycle events.

```
2026-04-27 08:30:00 CONNECT_FIRST  station=10001  ip=192.168.20.37  date=2026-04-27 08:30:00
2026-04-27 22:15:41 DISCONNECT     station=10001  last_seen=2026-04-27 22:15:36  detected_at=2026-04-27 22:15:41
2026-04-28 08:45:03 CONNECT_LAST   station=10001  ip=192.168.20.37  offline_sec=37582  date=2026-04-28 08:45:03
```

| Event | When |
|---|---|
| `CONNECT_FIRST` | Device posts to `/station/status` for the first time ever |
| `DISCONNECT` | Device not seen for > `STALE_THRESHOLD` seconds |
| `CONNECT_LAST` | Device reconnects after being offline |

### `operation.log`

Records hold and alarm events.

```
2026-04-27 09:00:10 HOLD_START  station=10005  taskID=TASK-042  robotID=SMR-01  prev_status=NORMAL  initial_status=BUSY
2026-04-27 09:02:35 HOLD_END    station=10005  taskID=TASK-042  robotID=SMR-01  prev_status=BUSY
2026-04-27 09:05:00 HOLD_RESTORED  station=10003  taskID=TASK-011  robotID=SMR-02
2026-04-27 09:10:00 RECONNECT_RECOVERED  station=10008  taskID=TASK-099  robotID=SMR-01  offline_sec=47
```

### Log retention

All log files older than **7 days** are deleted on server and client startup. Configurable via `LOG_RETENTION_DAYS` environment variable.

---

## ❌ Error Codes

All errors use this envelope:
```json
{ "ret_code": 4011, "error": "Field 'id' must be an integer" }
```

### Auth & Transport

| ret_code | HTTP | Cause |
|---|---|---|
| `4001` | `401` | Wrong or missing `X-API-Key` |
| `4002` | `429` | Rate limit exceeded (5 req/s per device) |
| `4003` | `405` | Wrong HTTP method |
| `4004` | `404` | Unknown endpoint |

### Payload Validation

| ret_code | HTTP | Cause |
|---|---|---|
| `4010` | `400` | Empty body or invalid JSON |
| `4011` | `400` | `id` missing or not integer |
| `4012` | `400` | `status` not in valid set |
| `4013` | `400` | `hold` not bool, or `taskID`/`robotID` wrong type |
| `4014` | `400` | `alarm` not bool |
| `4015` | `400` | Heartbeat `id` missing or not integer |

### Device State

| ret_code | HTTP | Cause |
|---|---|---|
| `4020` | `404` | Device not registered |
| `4021` | `404` | No heartbeat received for device |
| `4022` | `409` | Device is OFFLINE — hold/alarm rejected |
| `4030` | `409/403` | `409` = held by another robot · `403` = wrong robot releasing |
| `4031` | — | Reserved (CANNOT_HOLD_FULL — currently unused) |

### Scan

| ret_code | HTTP | Cause |
|---|---|---|
| `4040` | `409` | Scan already running |
| `4041` | `404` | No scan result yet |
| `4042` | `400` | Invalid subnet string |

### Server

| ret_code | HTTP | Cause |
|---|---|---|
| `5000` | `500` | Unhandled server exception |

---

## ⚙️ Configuration Reference

### `server_sim.py`

| Variable | Default | Description |
|---|---|---|
| `API_KEY` | `nextroboticslab2024` | Shared API key |
| `STALE_THRESHOLD` | `5` | Seconds without POST before device is OFFLINE |
| `RATE_LIMIT` | `5` | Max requests/second per device |
| `DEVICES_FILE` | `devices.json` | Input MAC map |
| `DEVICE_IP_FILE` | `device_ip.json` | Output IP map |
| `DEFAULT_SUBNET` | `192.168.20.0/24` | Subnet for auto/manual scans |
| `NMAP_TIMEOUT` | `60` | nmap per-host timeout (seconds) |
| `PERIODIC_SCAN_INTERVAL` | `600` | Seconds between periodic scans |
| `DISCONNECT_SCAN_DELAY` | `30` | Seconds before rescan after disconnect |
| `STATE_FILE` | `state.json` | Hold/alarm persistence |
| `STATE_SIM_FILE` | `state_sim.json` | Simulation state persistence |
| `LOG_RETENTION_DAYS` | `7` | Days before log files are deleted |

### `client.py`

| Variable | Default | Description |
|---|---|---|
| `API_KEY` | `nextroboticslab2024` | Must match server |
| `API_URL` | `http://127.0.0.1:5000/station/status` | Server URL |
| `POLL_INTERVAL` | `1.0` | Seconds between Modbus reads |
| `HEARTBEAT_INTERVAL` | `1.0` | Seconds between heartbeat POSTs |
| `WATCHDOG_INTERVAL` | `10.0` | Seconds between watchdog checks |
| `RECONNECT_INTERVAL` | `10.0` | Fixed retry interval — never gives up |
| `OFFLINE_BUFFER_SIZE` | `200` | Max queued payloads per device |
| `MODBUS_PORT` | `8899` | TCP port on each station |
| `MODBUS_TIMEOUT` | `5.0` | Modbus read/write timeout (seconds) |
| `LOG_RETENTION_DAYS` | `7` | Days before log files are deleted |

---

## 🔁 Data Flow Diagram

```
devices.json  ──→  ip.py  ──→  device_ip.json
(id + MAC)    nmap + ARP       (id + IP)
                                    │
                                    ▼
┌─────────────────────────────────────────────────────┐
│                   server_sim.py                      │
│              Flask REST API :5000                    │
│                                                      │
│  Status logic:                                       │
│    sim_active + sim_input=1  →  FULL                 │
│    sim_active + sim_input=0  →  NORMAL/BUSY          │
│    hold=true  + sensor=1     →  FULL  (robot waits)  │
│    hold=true  + sensor=0     →  BUSY  (robot goes)   │
│    sensor=1   (no hold)      →  FULL                 │
│    alarm=true (no hold)      →  ALARM                │
│    no heartbeat > 5s         →  OFFLINE              │
│    else                      →  NORMAL               │
│                                                      │
│  Persistence:                                        │
│    state.json      ← hold/alarm on every change      │
│    state_sim.json  ← sim state on every change       │
│                                                      │
│  Auto-scan: startup / 10min / on-disconnect          │
└──────────────────┬──────────────────────────────────┘
                   │  HTTP POST every 1s
                   │  POST /heartbeat every 1s
                   ▼
┌─────────────────────────────────────────────────────┐
│                    client.py                         │
│  • One thread per device (from device_ip.json)       │
│  • Retry every 10s forever — no backoff ceiling      │
│  • Watchdog thread restarts dead threads             │
│  • Heartbeat thread (one POST per device per 1s)     │
│  • Offline buffer (200 payloads per device)          │
│  • connect.log: FIRST / LAST connection events       │
└──────────────────┬──────────────────────────────────┘
                   │  Modbus-TCP port 8899
                   ▼
┌─────────────────────────────────────────────────────┐
│           PLC / Modbus Device (per station)          │
│   Read:  Discrete Inputs 0–1 (sensor)                │
│   Write: Coils 0–3 (signal tower lights + buzzer)    │
└─────────────────────────────────────────────────────┘
```

---

## 🚀 Quick API Reference

```bash
BASE="http://192.168.10.211:5000"
KEY="nextroboticslab2024"
H='-H "X-API-Key: '$KEY'" -H "Content-Type: application/json"'

# Status
curl -H "X-API-Key: $KEY" $BASE/station/status/all
curl -H "X-API-Key: $KEY" $BASE/station/status/10001

# Hold / Release
curl -X POST $H $BASE/station/standby \
  -d '{"id":10005,"hold":true,"robotID":"SMR-01","taskID":"TASK-042"}'
curl -X POST $H $BASE/station/standby \
  -d '{"id":10005,"hold":false,"robotID":"SMR-01"}'

# Alarm
curl -X POST $H $BASE/station/alarm -d '{"id":10005,"alarm":true}'

# Simulation
curl -X POST $H $BASE/simulation/mode  -d '{"id":30022,"active":true}'
curl -X POST $H $BASE/simulation/input -d '{"id":30022,"input":1}'
curl -X POST $H $BASE/simulation/mode  -d '{"id":30022,"active":false}'

# Health (no auth)
curl $BASE/health

# Manual IP scan
curl -X POST $H $BASE/scan -d '{"subnet":"192.168.20.0/24","timeout":60}'
```

---

## 📝 License

Taweeporn Maneesin — Robotics Software Engineer
Next Robotics Lab — Internal Use

---

*Last updated: 2026-04-28*
Done
