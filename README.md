# Station Monitor — API Protocol Guide

> **Next Robotics Lab** · Industrial Modbus-TCP Station Management System  
> Flask REST API · Python 3.10 · Docker · Multi-Station · Real-time monitoring, control & simulation

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Files Reference](#files-reference)
- [Docker Deployment](#docker-deployment)
- [Authentication](#authentication)
- [Station Status Values](#station-status-values)
- [Coil / LED Mapping](#coil--led-mapping)
- [API Endpoints](#api-endpoints)
  - [Station Status](#station-status)
  - [Hold (Standby)](#hold-standby)
  - [Alarm](#alarm)
  - [Heartbeat](#heartbeat)
  - [Health Check](#health-check)
  - [Simulation Mode](#simulation-mode)
  - [IP Scan](#ip-scan)
  - [Device IP](#device-ip)
- [Hold + FULL Wait Flow](#hold--full-wait-flow)
- [Simulation Mode Guide](#simulation-mode-guide)
- [Log Files](#log-files)
- [Error Codes](#error-codes)
- [Configuration Reference](#configuration-reference)
- [Data Flow Diagram](#data-flow-diagram)

---

## Overview

The Station Monitor manages industrial stations on a factory floor. Each station communicates via **Modbus-TCP** through `cilent.py`, which polls sensor data every second and reports to `server_sim.py`. The server manages state, hold/alarm logic, and simulation. Both run together inside a single Docker container.

**Key features:**
- Real-time station status at 1-second poll rate
- Hold / Release with robot task coordination
- Hold allowed on FULL stations — robot waits until station clears automatically
- Per-device Simulation Mode — test without physical hardware, auto-registers devices
- Static IP support via `device_ip.json` — no dynamic scan required
- Manual IP rediscovery via `POST /scan` when needed
- Fixed 10-second reconnect retry (no exponential backoff ceiling)
- Offline buffer — 200 payloads per device queued when API is unreachable
- State persistence across restarts (`state.json`, `state_sim.json`)
- `connect.log` — first connect / reconnect per device
- `operation.log` — hold start / end / restore events
- 7-day log retention with automatic cleanup

---

## Architecture

| Component | File | Role |
|---|---|---|
| **API Server** | `server_sim.py` | Central state manager — receives sensor data, handles hold/alarm/sim, serves REST API |
| **Modbus Client** | `cilent.py` | Polls each station via Modbus-TCP every 1s, writes coils, POSTs to server |

Both run inside the same Docker container. `cilent.py` talks to `server_sim.py` via `127.0.0.1:5000` internally.

---

## Files Reference

| File | Purpose |
|---|---|
| `devices.json` | Maps device ID → MAC address (used for manual `/scan`) |
| `device_ip.json` | Maps device ID → IP — **edit this directly for static IPs** |
| `devices_ip_real.json` | Source of truth for static IPs — copy to `device_ip.json` to apply |
| `state.json` | Auto-managed — persists hold/alarm state across restarts |
| `state_sim.json` | Auto-managed — persists simulation state across restarts |
| `operation.log` | Hold start/end/restore events (10 MB × 10 files) |
| `connect.log` | CONNECT_FIRST / CONNECT_LAST events (5 MB × 14 files) |
| `client.log` | Modbus reads, reconnect events (5 MB × 5 files) |

### `device_ip.json` format

```json
{
  "10001": "192.168.207.227",
  "10002": "192.168.207.228",
  "20001": "192.168.207.243",
  "30021": "192.168.207.254"
}
```

> Static IPs are used by default. Auto-scan is disabled. Use `POST /scan` if you need to rediscover IPs after a device change.

### `devices.json` format

```json
{
  "10001": "D4-AD-20-CA-69-81",
  "20001": "D4-AD-20-CA-63-05",
  "30031": ""
}
```

> Devices with empty MAC (`""`) are simulation-only — they can be used via `POST /simulation/mode` without physical hardware.

---

## Docker Deployment

### Files to copy to the server machine

```
lane-infra.tar
docker-compose.yml
devices.json
device_ip.json
state.json          ← empty {}
state_sim.json      ← empty {}
client.log          ← empty file
connect.log         ← empty file
operation.log       ← empty file
```

> All log and state files must exist before `docker compose up` or Docker will fail to mount them.

### First-time setup

```bash
# 1. Create empty files if they don't exist
touch client.log connect.log operation.log
echo '{}' > state.json
echo '{}' > state_sim.json

# 2. Load the image
docker load -i lane-infra.tar

# 3. Start the container
docker compose up -d

# 4. Check logs (both server and client output here)
docker compose logs -f
```

### Running on Windows (WSL)

Docker on WSL does not expose ports via the machine's LAN IP by default. After starting the container, open **PowerShell as Administrator** and run once:

```powershell
New-NetFirewallRule -DisplayName "Allow Port 5000" -Direction Inbound -Protocol TCP -LocalPort 5000 -Action Allow
```

The server will then be reachable at `http://192.168.207.9:5000`.

### Updating the server

When you have a new `lane-infra.tar`:

```bash
docker load -i lane-infra.tar
docker compose down && docker compose up -d
```

### Useful commands

```bash
docker compose logs -f        # live logs
docker compose down           # stop
docker compose restart        # restart without rebuilding
```

### docker-compose.yml

```yaml
version: '3.8'

services:
  lane-server:
    image: lane-infra:latest
    container_name: lane-infra
    ports:
      - "0.0.0.0:5000:5000"
    volumes:
      - ./devices.json:/app/devices.json
      - ./device_ip.json:/app/device_ip.json
      - ./state.json:/app/state.json
      - ./state_sim.json:/app/state_sim.json
      - ./client.log:/app/client.log
      - ./connect.log:/app/connect.log
      - ./operation.log:/app/operation.log
    environment:
      - API_KEY=nextroboticslab2024
      - DEFAULT_SUBNET=192.168.207.0/24
    restart: always
```

---

## Authentication

All endpoints **except** `GET /health` require:

```http
X-API-Key: nextroboticslab2024
```

```javascript
// Node-RED / JavaScript
msg.headers = { 'X-API-Key': 'nextroboticslab2024' };
```

---

## Station Status Values

The server **derives** status — clients only send raw `input` (0 or 1).

| Status | Condition |
|---|---|
| `NORMAL` | No hold, no alarm, sensor = 0 |
| `FULL` | Sensor = 1 (station occupied), or hold=true with sensor=1 |
| `BUSY` | `hold=true` AND sensor = 0 — robot reserved, station clear |
| `ALARM` | `alarm=true`, no hold, sensor = 0 |
| `OFFLINE` | No heartbeat received within `STALE_THRESHOLD` seconds (default 5s) |

> **Sim mode:** When `sim_active=true`, the virtual `sim_input` replaces the real sensor for status derivation. The device is never considered OFFLINE while sim is active.

---

## Coil / LED Mapping

`cilent.py` writes Modbus coils based on the status returned by the server:

| Coil | Color | Active When |
|---|---|---|
| Coil 0 | Green | `status = NORMAL` |
| Coil 1 | Yellow | `status = BUSY` |
| Coil 2 | Red | `status = FULL` |
| Coil 3 | Buzzer/Alarm | `status = ALARM` |

Only one coil is active at a time.

---

## API Endpoints

Base URL: `http://<server-ip>:5000`

---

### Station Status

#### `POST /station/status`
Called by `cilent.py` every 1 second. Sends raw physical sensor reading.

**Auth:** Required · **Rate limit:** 5 req/s per device

**Request:**
```json
{ "id": 10001, "input": 0, "timestamp": "2026-05-12 09:00:00" }
```

| Field | Type | Description |
|---|---|---|
| `id` | `integer` | Device ID |
| `input` | `0` or `1` | Physical sensor value — server computes status |
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

> `simulated: true` tells `cilent.py` that sim mode is active for this device.

---

#### `GET /station/status/all`
Returns live status of all registered devices. Status is always derived fresh — never cached.

**Auth:** Required

**Response `200`:**
```json
{
  "10001": {
    "hold": false, "alarm": false,
    "status": "NORMAL",
    "input": 0,
    "timestamp": "2026-05-12 09:00:01",
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

> When `sim_active=true`, the `input` field reflects `sim_input`, not the real physical sensor.

---

#### `GET /station/status/<id>`
Returns live status for a single device. Same structure as one entry from `/station/status/all`.

Returns `HTTP 404` + `ret_code 4020` if device not registered.

---

### Hold (Standby)

#### `POST /station/standby`
Reserve or release a station for robot operation.

**Auth:** Required

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

**Response `200` — station clear (sensor=0):**
```json
{
  "message": "Device 10005 hold=True",
  "hold":    true,
  "status":  "BUSY",
  "taskID":  "TASK-042",
  "robotID": "SMR-01"
}
```

**Response `200` — station occupied (sensor=1):**
```json
{
  "message": "Device 10005 hold=True",
  "hold":    true,
  "status":  "FULL",
  "taskID":  "TASK-042",
  "robotID": "SMR-01"
}
```

> When `status=FULL`, hold is registered. The robot polls `GET /station/status/<id>` until status becomes `BUSY`.

**Hold Business Rules:**

| Condition | HTTP | ret_code | Behaviour |
|---|---|---|---|
| Station FULL when hold set | `200` | — | Allowed — hold registered, status=FULL, robot waits |
| Station NORMAL when hold set | `200` | — | Allowed — status=BUSY immediately |
| Already held by another robot | `409` | `4030` | Rejected |
| Wrong robot tries to release | `403` | `4030` | Only holding robot can release |
| Device OFFLINE | `409` | `4022` | Rejected |
| Device not registered | `404` | `4020` | Device must POST `/station/status` first |

---

### Alarm

#### `POST /station/alarm`

**Auth:** Required

```json
{ "id": 10005, "alarm": true }
```

**Response `200`:** `{ "message": "Device 10005 alarm=True" }`

Returns `409` + `ret_code 4022` if device is OFFLINE.

---

### Heartbeat

#### `POST /heartbeat`
Called by `cilent.py` every 1 second per device.

```json
{
  "id":            10001,
  "start_time":    "2026-05-12T08:00:00",
  "last_seen":     "2026-05-12 09:00:01",
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

---

### Health Check

#### `GET /health`
**No authentication required.**

```json
{
  "server_time":         "2026-05-12 09:00:05",
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

Per-device simulation — test hold/alarm/status flow without physical hardware.

When a device is in sim mode:
- Real Modbus sensor value is ignored
- Virtual `sim_input` (0 or 1) is used for all status calculations
- The `input` field in GET responses reflects `sim_input`
- Device is never considered OFFLINE (bypasses heartbeat check)
- Device is **auto-registered** in the database if it doesn't exist yet — no real client required

---

#### `POST /simulation/mode`
Enable or disable simulation for one device.

**Auth:** Required

```json
{ "id": 30022, "active": true }
```

**Response `200`:**
```json
{ "id": 30022, "sim_active": true }
```

> Devices not connected to real hardware are auto-registered on first `POST /simulation/mode`.

---

#### `POST /simulation/input`
Set the virtual sensor value (0 = empty, 1 = occupied).

**Auth:** Required

```json
{ "id": 30022, "input": 1 }
```

**Response `200`:**
```json
{ "id": 30022, "sim_input": 1 }
```

---

**Complete simulation flow:**

```bash
# 1. Enable sim for device 30022
POST /simulation/mode   { "id": 30022, "active": true }

# 2. Set sensor to occupied
POST /simulation/input  { "id": 30022, "input": 1 }
# → GET /station/status/30022 → { "status": "FULL", "sim_active": true }

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
```

---

### IP Scan

Auto-scan is **disabled** — static IPs from `device_ip.json` are used. Use `POST /scan` only when a device's IP changes.

#### `POST /scan`
Trigger nmap + ARP sweep to rediscover device IPs and rewrite `device_ip.json`.

**Auth:** Required · Returns `409` if scan already running.

```json
{ "subnet": "192.168.207.0/24", "timeout": 60 }
```

**Response `200`:**
```json
{
  "subnet":     "192.168.207.0/24",
  "total":      28,
  "found":      27,
  "missing":    [30021],
  "matched":    { "10001": "192.168.207.227", "10002": "192.168.207.228" },
  "scanned_at": "2026-05-12 09:01:00"
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
Version number of `device_ip.json`. Increments when scan updates the file.

```json
{ "version": 1 }
```

#### `GET /device_ip`
Current `device_ip.json` contents + version.

```json
{
  "version": 1,
  "device_ip": { "10001": "192.168.207.227", "10002": "192.168.207.228" }
}
```

---

## Hold + FULL Wait Flow

```
Robot → POST /station/standby { hold: true, robotID: "SMR-01", taskID: "TASK-042" }
                   │
          sensor = 1 (FULL — item present)
                   │
                   ▼
     Response: { "status": "FULL", "hold": true }
                   │
     Robot polls GET /station/status/10005 every 1s
                   │
          sensor=1 → status: "FULL" → keep waiting ...
                   │
          Item removed from station
                   │
          sensor drops to 0 — detected by cilent.py
                   │
                   ▼
     Next poll: { "status": "BUSY", "hold": true }
                   │
     Robot sees BUSY → approaches station
                   │
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
- Testing new device IDs not yet wired (`30031`, `30032`, etc.)
- Demo and training

### Persistence

`state_sim.json` saves all simulation states. After server restart, active sim devices are automatically restored.

### Coil write behavior in sim mode

By default, `cilent.py` still writes physical coils based on simulated status. To skip physical writes during simulation, uncomment in `cilent.py`:

```python
## SIM — uncomment to skip coil writes during simulation
# if is_sim:
#     continue
```

---

## Log Files

### `connect.log`

```
2026-05-12 08:30:00 CONNECT_FIRST  station=10001  ip=192.168.207.227
2026-05-12 22:15:41 CONNECT_LAST   station=10001  ip=192.168.207.227  attempt=2
```

| Event | When |
|---|---|
| `CONNECT_FIRST` | First ever successful Modbus connection per session |
| `CONNECT_LAST` | Reconnected after being offline |

### `operation.log`

```
2026-05-12 09:00:10 HOLD_START        station=10005  taskID=TASK-042  robotID=SMR-01  prev_status=NORMAL  initial_status=BUSY
2026-05-12 09:02:35 HOLD_END          station=10005  taskID=TASK-042  robotID=SMR-01  prev_status=BUSY
2026-05-12 09:05:00 HOLD_RESTORED     station=10003  taskID=TASK-011  robotID=SMR-02
2026-05-12 09:10:00 RECONNECT_RECOVERED  station=10008  taskID=TASK-099  robotID=SMR-01  offline_sec=47
```

### Log retention

All log files older than **7 days** are deleted on startup. Configurable via `LOG_RETENTION_DAYS` in `cilent.py`.

---

## Error Codes

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

## Configuration Reference

### `server_sim.py`

| Variable | Default | Description |
|---|---|---|
| `API_KEY` | `nextroboticslab2024` | Shared API key |
| `STALE_THRESHOLD` | `5` | Seconds without POST before device is OFFLINE |
| `RATE_LIMIT` | `5` | Max requests/second per device |
| `DEVICES_FILE` | `devices.json` | MAC map (for manual scan) |
| `DEVICE_IP_FILE` | `device_ip.json` | Static IP map |
| `DEFAULT_SUBNET` | `192.168.207.0/24` | Subnet for manual scan |
| `NMAP_TIMEOUT` | `60` | nmap per-host timeout (seconds) |
| `STATE_FILE` | `state.json` | Hold/alarm persistence |
| `STATE_SIM_FILE` | `state_sim.json` | Simulation state persistence |

### `cilent.py`

| Variable | Default | Description |
|---|---|---|
| `API_KEY` | `nextroboticslab2024` | Must match server |
| `API_URL` | `http://127.0.0.1:5000/station/status` | Server URL (internal) |
| `POLL_INTERVAL` | `1.0` | Seconds between Modbus reads |
| `HEARTBEAT_INTERVAL` | `1.0` | Seconds between heartbeat POSTs |
| `WATCHDOG_INTERVAL` | `10.0` | Seconds between watchdog checks |
| `RECONNECT_INTERVAL` | `10.0` | Fixed retry interval — never gives up |
| `OFFLINE_BUFFER_SIZE` | `200` | Max queued payloads per device |
| `MODBUS_PORT` | `8899` | TCP port on each station |
| `MODBUS_TIMEOUT` | `5.0` | Modbus read/write timeout (seconds) |
| `LOG_RETENTION_DAYS` | `7` | Days before log files are deleted |

---

## Data Flow Diagram

```
devices_ip_real.json  ──copy──►  device_ip.json  (static IPs, edit directly)
                                       │
                                       ▼
┌──────────────────────────────────────────────────────┐
│                Docker Container :5000                 │
│                                                      │
│  ┌─────────────────────────────────────────────────┐ │
│  │              server_sim.py                       │ │
│  │         Flask REST API (port 5000)               │ │
│  │                                                  │ │
│  │  Status logic:                                   │ │
│  │    sim_active + sim_input=1  →  FULL             │ │
│  │    sim_active + sim_input=0  →  NORMAL/BUSY      │ │
│  │    hold=true  + sensor=1     →  FULL (wait)      │ │
│  │    hold=true  + sensor=0     →  BUSY  (go)       │ │
│  │    sensor=1   (no hold)      →  FULL             │ │
│  │    alarm=true (no hold)      →  ALARM            │ │
│  │    no heartbeat > 5s         →  OFFLINE          │ │
│  │    else                      →  NORMAL           │ │
│  └───────────────┬─────────────────────────────────┘ │
│                  │  HTTP POST every 1s (127.0.0.1)    │
│                  ▼                                    │
│  ┌─────────────────────────────────────────────────┐ │
│  │              cilent.py                           │ │
│  │  One thread per device from device_ip.json       │ │
│  │  Watchdog restarts dead threads                  │ │
│  │  Heartbeat thread — one POST per device per 1s   │ │
│  │  Offline buffer — 200 payloads per device        │ │
│  └───────────────┬─────────────────────────────────┘ │
└──────────────────┼───────────────────────────────────┘
                   │  Modbus-TCP port 8899
                   ▼
     PLC / Modbus Device (per station)
     Read:  Discrete Inputs 0-1 (sensor)
     Write: Coils 0-3 (signal tower + buzzer)
```

---

## Quick API Reference

```bash
BASE="http://192.168.207.9:5000"
KEY="nextroboticslab2024"

# Health (no auth)
curl $BASE/health

# Status
curl -H "X-API-Key: $KEY" $BASE/station/status/all
curl -H "X-API-Key: $KEY" $BASE/station/status/10001

# Hold / Release
curl -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  $BASE/station/standby \
  -d '{"id":10005,"hold":true,"robotID":"SMR-01","taskID":"TASK-042"}'

curl -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  $BASE/station/standby \
  -d '{"id":10005,"hold":false,"robotID":"SMR-01"}'

# Alarm
curl -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  $BASE/station/alarm -d '{"id":10005,"alarm":true}'

# Simulation
curl -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  $BASE/simulation/mode  -d '{"id":30022,"active":true}'
curl -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  $BASE/simulation/input -d '{"id":30022,"input":1}'
curl -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  $BASE/simulation/mode  -d '{"id":30022,"active":false}'

# Manual IP scan
curl -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  $BASE/scan -d '{"subnet":"192.168.207.0/24","timeout":60}'
```

---

*Next Robotics Lab — Internal Use*  
*Last updated: 2026-05-12*
