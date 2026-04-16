# 🏭 Station Monitor — API Protocol Guide

> **Next Robotics Lab** · Industrial Modbus-TCP Station Management System  
> Flask REST API · Python 3.10+ · 16 Stations · Real-time monitoring & control

---

## 📋 Table of Contents

- [Overview](#-overview)
- [Architecture](#-architecture)
- [Quick Start](#-quick-start)
- [Authentication](#-authentication)
- [Station Status Values](#-station-status-values)
- [API Endpoints](#-api-endpoints)
  - [Station — Report Status](#1-post-stationstatus)
  - [Station — Get All Status](#2-get-stationstatusall)
  - [Station — Get Single Status](#3-get-stationstatusid)
  - [Station — Set Hold (Standby)](#4-post-stationstandby)
  - [Station — Set Alarm](#5-post-stationalarm)
  - [Heartbeat — Report](#6-post-heartbeat)
  - [Heartbeat — Get All](#7-get-heartbeatall)
  - [Heartbeat — Get Single](#8-get-heartbeatid)
  - [Health Check](#9-get-health)
  - [Scan — Manual Trigger](#10-post-scan)
  - [Scan — Get Last Result](#11-get-scanresult)
  - [Scan — Get Scan Status](#12-get-scanstatus)
  - [Device IP — Get Version](#13-get-device_ipversion)
  - [Device IP — Get Map](#14-get-device_ip)
- [IP Discovery — Auto Scan Features](#-ip-discovery--auto-scan-features)
- [Error Codes](#-error-codes)
- [Configuration Reference](#-configuration-reference)
- [File Reference](#-file-reference)
- [Coil / LED Mapping](#-coil--led-mapping)
- [Data Flow Diagram](#-data-flow-diagram)

---

## 🔍 Overview

The Station Monitor API manages up to **16 industrial stations** on a factory floor. Each station communicates via **Modbus-TCP** through `cilent.py`, which polls sensor data and reports it to `server.py` every second.

```
Station Device (Modbus-TCP)
        │  192.168.20.x:8899
        ▼
  cilent.py  ──POST /station/status──▶  server.py :5000
                                               │
  Dashboard / Robot  ◀──GET /station/status/all, /health──┘
```

**Key features:**
- ✅ Real-time station status at 1-second poll rate
- ✅ Hold / Release operation mode per station (robot task coordination)
- ✅ Alarm (buzzer) control per station
- ✅ Automatic IP discovery with nmap + ARP scan
- ✅ State persistence across server restarts
- ✅ Heartbeat monitoring per device
- ✅ Rate limiting (5 req/s per device)

---

## 🏗 Architecture

| Component | File | Role |
|---|---|---|
| **API Server** | `server.py` | Central state manager. Receives sensor data, handles hold/alarm commands, serves dashboard queries |
| **Modbus Client** | `cilent.py` | Runs on gateway machine. Polls each station via Modbus-TCP, POSTs to server every 1s |
| **IP Discovery** | `ip.py` | One-shot CLI tool. Scans subnet via nmap+ARP, writes `device_ip.json` |

---

## ⚡ Quick Start

### 1. Install dependencies

```bash
pip install flask pyModbusTCP requests
sudo apt install nmap        # Linux only
```

### 2. Prepare `devices.json`

```json
{
  "10001": "D4-AD-20-CA-69-81",
  "10002": "D4-AD-20-CA-69-5D"
}
```

### 3. Run IP discovery (first time or after network changes)

```bash
# Auto-detect gateway and scan
sudo python ip.py

# Or specify subnet manually
sudo python ip.py --gateway 192.168.20.0/24
```

### 4. Start the server

```bash
python server.py
# Server starts on http://0.0.0.0:5000
# Auto-scan runs on startup, every 10 min, and on disconnect events
```

### 5. Start the Modbus client

```bash
python cilent.py
# Reads device_ip.json, connects to all stations, begins polling
```

---

## 🔑 Authentication

All endpoints **except** `/health` require the API key in the request header.

| Header | Value |
|---|---|
| `X-API-Key` | `nextroboticslab2024` |

**Example:**

```http
GET /station/status/all HTTP/1.1
Host: 192.168.10.211:5000
X-API-Key: nextroboticslab2024
```

```python
# Python requests
headers = {
    "X-API-Key": "nextroboticslab2024",
    "Content-Type": "application/json"
}
```

> ⚠️ The API key can be overridden via the `API_KEY` environment variable.

---

## 🚦 Station Status Values

The server **derives** the status — clients only send the raw sensor input (`0` or `1`).

| Status | Priority | LED Coil | Condition |
|---|:---:|---|---|
| `BUSY` | **1** (highest) | Coil 1 — 🟡 Yellow | `hold = true` — robot operating at station |
| `FULL` | **2** | Coil 2 — 🔴 Red | `input = 1` — physical sensor HIGH |
| `ALARM` | **3** | Coil 3 — 🔔 Buzzer | `alarm = true`, no hold, sensor clear |
| `NORMAL` | **4** (lowest) | Coil 0 — 🟢 Green | All flags false, sensor clear |

> **BUSY always wins.** Once hold is set, the station shows BUSY regardless of sensor or alarm state.

---

## 📡 API Endpoints

Base URL: `http://<server-ip>:5000`

---

### 1. `POST /station/status`

**Called by `cilent.py` every 1 second.** Reports raw physical sensor reading. Server derives and returns computed status.

**Auth required:** ✅ Yes  
**Rate limit:** 5 requests/second per device

#### Request Body

| Field | Type | Required | Description |
|---|---|:---:|---|
| `id` | `integer` | ✅ | Device ID (e.g. `10001`) — must be integer, not string |
| `input` | `0` or `1` | ✅ | Physical sensor reading. `1` = station occupied/full |
| `timestamp` | `string` | ❌ | `"YYYY-MM-DD HH:MM:SS"` — used for disconnect detection |

```json
{
  "id": 10001,
  "input": 0,
  "timestamp": "2026-04-03 15:30:00"
}
```

#### Response — `HTTP 201`

| Field | Type | Description |
|---|---|---|
| `message` | `string` | `"OK"` |
| `status` | `string` | Server-derived status: `NORMAL` / `FULL` / `BUSY` / `ALARM` |
| `hold_mode` | `boolean` | `true` if station is currently held |
| `alarm_mode` | `boolean` | `true` if alarm is active |
| `taskID` | `string\|null` | Current task ID if hold active |
| `robotID` | `string\|null` | Current robot ID if hold active |

```json
{
  "message": "OK",
  "status": "NORMAL",
  "hold_mode": false,
  "alarm_mode": false,
  "taskID": null,
  "robotID": null
}
```

---

### 2. `GET /station/status/all`

Returns the full current state snapshot for **all registered devices**.

**Auth required:** ✅ Yes

#### Response — `HTTP 200`

```json
{
  "10001": {
    "hold": false,
    "alarm": false,
    "status": "NORMAL",
    "input": 0,
    "timestamp": "2026-04-03 15:30:01",
    "taskID": null,
    "robotID": null
  },
  "10003": {
    "hold": true,
    "alarm": false,
    "status": "BUSY",
    "input": 0,
    "timestamp": "2026-04-03 15:30:01",
    "taskID": "TASK-042",
    "robotID": "SMR-01"
  }
}
```

> Keys are device IDs as **integers**. Devices not yet registered (no POST received) will not appear.

---

### 3. `GET /station/status/<id>`

Returns state for a **single device**.

**Auth required:** ✅ Yes

```http
GET /station/status/10001
X-API-Key: nextroboticslab2024
```

#### Response — `HTTP 200`

Same structure as a single entry from `/station/status/all`.

Returns `HTTP 404` + `ret_code 4020` if device is not registered.

---

### 4. `POST /station/standby`

**Set or release operation hold on a station.**  
Hold forces status to `BUSY` regardless of sensor state.  
⚠️ Only the **same robot** that set the hold can release it.

**Auth required:** ✅ Yes

#### Request Body — Set Hold

| Field | Type | Required | Description |
|---|---|:---:|---|
| `id` | `integer` | ✅ | Device ID |
| `hold` | `boolean` | ✅ | `true` = activate hold, `false` = release hold |
| `robotID` | `string` | ✅ | Robot identifier. **Must match when releasing** |
| `taskID` | `string` | ✅ when `hold=true` | Task identifier |

```json
{
  "id": 10005,
  "hold": true,
  "robotID": "SMR-01",
  "taskID": "TASK-042"
}
```

#### Request Body — Release Hold

```json
{
  "id": 10005,
  "hold": false,
  "robotID": "SMR-01"
}
```

#### Response — `HTTP 200`

| Field | Type | Description |
|---|---|---|
| `message` | `string` | Confirmation message |
| `hold` | `boolean` | Current hold state |
| `status` | `string` | Current station status |
| `taskID` | `string\|null` | Active task ID |
| `robotID` | `string\|null` | Active robot ID |

```json
{
  "message": "Device 10005 hold=True",
  "hold": true,
  "status": "BUSY",
  "taskID": "TASK-042",
  "robotID": "SMR-01"
}
```

#### Hold Business Rules

| Condition | HTTP | ret_code | Behavior |
|---|:---:|:---:|---|
| Station already held by **another** robot | `409` | `4030` | Rejected. Response includes `held_by: {robotID, taskID}` |
| Station sensor = `1` (FULL) on new hold attempt | `409` | `4031` | Rejected. Station must be cleared first |
| Wrong robot tries to release | `403` | `4030` | Only the holding robot can release |
| Device not registered | `404` | `4020` | Device has not posted to `/station/status` yet |

---

### 5. `POST /station/alarm`

**Activates or deactivates the buzzer/alarm for a station.**

**Auth required:** ✅ Yes

#### Request Body

| Field | Type | Required | Description |
|---|---|:---:|---|
| `id` | `integer` | ✅ | Device ID |
| `alarm` | `boolean` | ✅ | `true` = alarm ON, `false` = alarm OFF |

```json
{ "id": 10005, "alarm": true }
```

#### Response — `HTTP 200`

```json
{ "message": "Device 10005 alarm=True" }
```

> When `alarm=true` and no hold is active, station status becomes `ALARM`.  
> If hold is active (`BUSY`), alarm is stored but status remains `BUSY`.

---

### 6. `POST /heartbeat`

**Called by `cilent.py` every 1 second** to report per-device health metrics.

**Auth required:** ✅ Yes

#### Request Body

| Field | Type | Description |
|---|---|---|
| `id` | `integer` | Device ID |
| `start_time` | `string` | ISO datetime when client thread started |
| `last_seen` | `string\|null` | Last successful Modbus read timestamp |
| `timeout_count` | `integer` | Total Modbus read timeouts |
| `post_ok` | `integer` | Successful API POSTs |
| `post_fail` | `integer` | Failed API POSTs |
| `reconnects` | `integer` | Modbus reconnect attempts |

```json
{
  "id": 10001,
  "start_time": "2026-04-03T09:00:00",
  "last_seen": "2026-04-03 15:30:01",
  "timeout_count": 2,
  "post_ok": 4520,
  "post_fail": 3,
  "reconnects": 1
}
```

#### Response — `HTTP 200`

```json
{ "message": "OK" }
```

---

### 7. `GET /heartbeat/all`

Returns the latest heartbeat snapshot for **every device**.

**Auth required:** ✅ Yes

```http
GET /heartbeat/all
X-API-Key: nextroboticslab2024
```

#### Response — `HTTP 200`

```json
{
  "10001": {
    "id": 10001,
    "start_time": "2026-04-03T09:00:00",
    "last_seen": "2026-04-03 15:30:01",
    "timeout_count": 0,
    "post_ok": 4520,
    "post_fail": 0,
    "reconnects": 0
  }
}
```

---

### 8. `GET /heartbeat/<id>`

Returns heartbeat for a **single device**.

**Auth required:** ✅ Yes

```http
GET /heartbeat/10001
X-API-Key: nextroboticslab2024
```

Returns `HTTP 404` + `ret_code 4021` if no heartbeat received yet for that device.

---

### 9. `GET /health`

System-wide health snapshot. **No authentication required.**

```http
GET /health
```

#### Response — `HTTP 200`

| Field | Type | Description |
|---|---|---|
| `server_time` | `string` | Current server datetime |
| `total_devices` | `integer` | Number of registered devices |
| `disconnected_device` | `array[int]` | IDs whose last timestamp is older than `STALE_THRESHOLD` (default 10s) |
| `buzzer_on` | `array[int]` | IDs with `alarm=true` |
| `hold_devices` | `array[object]` | Devices with active hold: `{id, taskID, robotID}` |

```json
{
  "server_time": "2026-04-03 15:30:05",
  "total_devices": 16,
  "disconnected_device": [],
  "buzzer_on": [],
  "hold_devices": [
    { "id": 10005, "taskID": "TASK-042", "robotID": "SMR-01" }
  ]
}
```

---

### 10. `POST /scan`

**Manually trigger** an nmap + ARP sweep to rediscover device IPs. Updates `device_ip.json` and increments the version counter.

**Auth required:** ✅ Yes

> ⚠️ Server must run with `sudo` on Linux for nmap to populate the ARP table.  
> Returns `HTTP 409` if a scan is already in progress.

#### Request Body (all optional)

| Field | Type | Default | Description |
|---|---|---|---|
| `subnet` | `string` | `192.168.20.0/24` | CIDR subnet to scan |
| `timeout` | `integer` | `60` | nmap per-host timeout in seconds |

```json
{ "subnet": "192.168.20.0/24", "timeout": 60 }
```

#### Response — `HTTP 200`

| Field | Type | Description |
|---|---|---|
| `subnet` | `string` | Subnet that was scanned |
| `total` | `integer` | Total devices in `devices.json` |
| `found` | `integer` | Devices successfully matched to IPs |
| `missing` | `array[int]` | Device IDs not found in ARP table |
| `matched` | `object` | `{ "10001": "192.168.20.37", ... }` |
| `scanned_at` | `string` | Timestamp of scan completion |

```json
{
  "subnet": "192.168.20.0/24",
  "total": 16,
  "found": 16,
  "missing": [],
  "matched": {
    "10001": "192.168.20.37",
    "10002": "192.168.20.48"
  },
  "scanned_at": "2026-04-03 15:31:19"
}
```

---

### 11. `GET /scan/result`

Returns the **most recent scan result** without running a new scan.

**Auth required:** ✅ Yes

Returns `HTTP 404` + `ret_code 4041` if no scan has been run since server start.

---

### 12. `GET /scan/status`

Check whether a scan is **currently running**.

**Auth required:** ✅ Yes

#### Response — `HTTP 200`

```json
{ "running": false }
```

---

### 13. `GET /device_ip/version`

Returns the **current version number** of `device_ip.json`.  
Clients poll this endpoint — when the version changes, they call `/device_ip` to reload IPs.

**Auth required:** ✅ Yes

#### Response — `HTTP 200`

```json
{ "version": 3 }
```

---

### 14. `GET /device_ip`

Returns the **full contents of `device_ip.json`** along with the current version. Allows clients to reload their IP maps without filesystem access.

**Auth required:** ✅ Yes

#### Response — `HTTP 200`

```json
{
  "version": 3,
  "device_ip": {
    "10001": "192.168.20.37",
    "10002": "192.168.20.48",
    "10003": "192.168.20.36"
  }
}
```

---

## 🌐 IP Discovery — Auto Scan Features

The server automatically manages device IP discovery. There are **4 scan modes**:

| Mode | When | Trigger | Env Variable |
|---|---|---|---|
| **Startup Scan** | Server boot | Background thread at startup | — |
| **Periodic Scan** | Every 10 minutes | Background timer loop | `PERIODIC_SCAN_INTERVAL` (default: `600`s) |
| **Disconnect-Triggered Scan** | Device goes offline | New stale devices detected → rescan after delay | `DISCONNECT_SCAN_DELAY` (default: `30`s) |
| **Manual Scan** | On demand | `POST /scan` API call | `DEFAULT_SUBNET`, `NMAP_TIMEOUT` |

### How Disconnect-Triggered Scan Works

```
Device goes offline (no POST for > STALE_THRESHOLD seconds)
        │
        ▼
Disconnect monitor detects newly stale device(s)
        │
        ▼
Schedule rescan in DISCONNECT_SCAN_DELAY seconds (default 30s)
(multiple disconnects within the window are debounced into one scan)
        │
        ▼
nmap + ARP sweep → update device_ip.json → bump version
        │
        ▼
Clients polling GET /device_ip/version detect the change
        │
        ▼
Clients call GET /device_ip → reload IP map → reconnect
```

### Manual IP Discovery with `ip.py`

For first-time setup or when the server is not running:

```bash
# Auto-detect subnet from default gateway
sudo python ip.py

# Specify subnet manually
sudo python ip.py --gateway 192.168.20.0/24

# With custom timeout
sudo python ip.py --gateway 192.168.20.0/24 --timeout 30

# Use custom input/output files
sudo python ip.py --devices custom_devices.json --output custom_ip.json
```

#### `ip.py` Arguments

| Argument | Default | Description |
|---|---|---|
| `--gateway` | auto-detect | Subnet to scan (CIDR or host address) |
| `--timeout` | `60` | nmap per-host timeout (seconds) |
| `--devices` | `devices.json` | Input file with MAC addresses |
| `--output` | `device_ip.json` | Output file for discovered IPs |

---

## ❌ Error Codes

All error responses use this envelope:

```json
{ "ret_code": 4011, "error": "Field 'id' must be an integer" }
```

### Authentication & Transport

| ret_code | HTTP | Name | Cause & Fix |
|---|:---:|---|---|
| `4001` | `401` | `UNAUTHORIZED` | Wrong or missing `X-API-Key` header |
| `4002` | `429` | `RATE_LIMITED` | Device exceeded 5 requests/second. Reduce poll rate or raise `RATE_LIMIT` |
| `4003` | `405` | `METHOD_NOT_ALLOWED` | Wrong HTTP verb (e.g. GET on a POST-only endpoint) |
| `4004` | `404` | `ENDPOINT_NOT_FOUND` | URL path does not exist |

### Payload Validation

| ret_code | HTTP | Name | Cause & Fix |
|---|:---:|---|---|
| `4010` | `400` | `EMPTY_BODY` | Request body missing or not valid JSON. Add `Content-Type: application/json` |
| `4011` | `400` | `INVALID_ID` | `id` is missing or not an integer. Use `"id": 10001` not `"id": "10001"` |
| `4012` | `400` | `INVALID_STATUS` | Status not in `NORMAL/FULL/BUSY/ALARM` |
| `4013` | `400` | `INVALID_HOLD` | `hold` not boolean, or `taskID`/`robotID` wrong type |
| `4014` | `400` | `INVALID_ALARM` | `alarm` field not boolean |
| `4015` | `400` | `INVALID_HEARTBEAT_ID` | `id` missing or not integer in heartbeat payload |

### Device State

| ret_code | HTTP | Name | Cause & Fix |
|---|:---:|---|---|
| `4020` | `404` | `DEVICE_NOT_FOUND` | Device ID not registered. Device must POST `/station/status` at least once |
| `4021` | `404` | `DEVICE_NOT_IN_HB` | No heartbeat received yet for that device |
| `4030` | `409/403` | `ALREADY_HELD` | `409` = station held by another robot. `403` = wrong robot trying to release |
| `4031` | `409` | `CANNOT_HOLD_FULL` | Station sensor=1 when hold attempted. Wait for station to clear |

### Scan

| ret_code | HTTP | Name | Cause & Fix |
|---|:---:|---|---|
| `4040` | `409` | `SCAN_IN_PROGRESS` | Another scan is already running. Wait and retry |
| `4041` | `404` | `SCAN_NO_RESULT` | `GET /scan/result` called before any scan has run |
| `4042` | `400` | `INVALID_SUBNET` | Subnet string is not valid CIDR notation |

### Server

| ret_code | HTTP | Name | Cause & Fix |
|---|:---:|---|---|
| `5000` | `500` | `INTERNAL_ERROR` | Unhandled server exception. Check server logs |

---

## ⚙️ Configuration Reference

All settings can be overridden with **environment variables**.

### `server.py`

| Variable | Default | Description |
|---|---|---|
| `API_KEY` | `nextroboticslab2024` | Shared API key for all authenticated requests |
| `STALE_THRESHOLD` | `10` | Seconds without a POST before device is marked disconnected |
| `RATE_LIMIT` | `5` | Max requests/second per device on `/station/status` |
| `DEVICES_FILE` | `devices.json` | Input file: `{ "id": "MAC" }` |
| `DEVICE_IP_FILE` | `device_ip.json` | IP map file written by scan |
| `DEFAULT_SUBNET` | `192.168.20.0/24` | Subnet for auto and manual scans |
| `NMAP_TIMEOUT` | `60` | nmap per-host timeout (seconds) |
| `PERIODIC_SCAN_INTERVAL` | `600` | Seconds between automatic periodic scans |
| `DISCONNECT_SCAN_DELAY` | `30` | Seconds to wait before rescan after detecting disconnect |
| `STATE_FILE` | `state.json` | File for persisting hold/alarm state across restarts |

### `cilent.py`

| Variable | Default | Description |
|---|---|---|
| `API_KEY` | `nextroboticslab2024` | Must match server `API_KEY`. Set via env var |
| `API_URL` | `http://127.0.0.1:5000/station/status` | Full URL to POST sensor data. Change if server is remote |
| `POLL_INTERVAL` | `1.0` | Seconds between Modbus reads per device |
| `HEARTBEAT_INTERVAL` | `1.0` | Seconds between heartbeat POSTs |
| `WATCHDOG_INTERVAL` | `10.0` | Seconds between watchdog thread checks |
| `OFFLINE_BUFFER_SIZE` | `200` | Max payloads queued per device when server is unreachable |
| `MAX_RECONNECT_DELAY` | `120` | Exponential backoff ceiling for Modbus reconnect (seconds) |
| `MODBUS_PORT` | `8899` | TCP port on each station device |
| `MODBUS_TIMEOUT` | `5.0` | Modbus read/write timeout per device (seconds) |

---

## 📁 File Reference

| File | Purpose |
|---|---|
| `devices.json` | **Input** — maps device ID → MAC address. Edit manually before first run |
| `device_ip.json` | **Auto-managed** — maps device ID → IP. Written by `ip.py` and `/scan` endpoint |
| `state.json` | **Auto-managed** — persists hold/alarm state across server restarts |
| `operation.log` | Hold start/end/restore events. Rotates at 10 MB × 10 files |
| `client.log` | Modbus reads, API responses, reconnect events. Rotates at 5 MB × 5 files |

### `devices.json` format

```json
{
  "10001": "D4-AD-20-CA-69-81",
  "10002": "D4-AD-20-CA-69-5D"
}
```

> MAC addresses can use colons (`:`), dashes (`-`), or no separator — all are normalized automatically.

### `device_ip.json` format

```json
{
  "10001": "192.168.20.37",
  "10002": "192.168.20.48"
}
```

---

## 💡 Coil / LED Mapping

`cilent.py` writes Modbus coils based on the server-returned status:

| Coil Index | Color | Active When |
|:---:|---|---|
| `0` | 🟢 Green | `status = NORMAL` |
| `1` | 🟡 Yellow | `status = BUSY` |
| `2` | 🔴 Red | `status = FULL` |
| `3` | 🔔 Alarm (buzzer) | `status = ALARM` |

Only one coil is active at a time.

---

## 🔁 Data Flow Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                     Station Device (x16)                    │
│              Modbus-TCP  192.168.20.x:8899                  │
└──────────────────────────┬──────────────────────────────────┘
                           │  read_discrete_inputs(0, 8)
                           │  write_multiple_coils(0, [G,Y,R,A])
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                        cilent.py                            │
│  • One thread per device                                    │
│  • Watchdog thread (restarts dead threads)                  │
│  • Heartbeat thread (POST /heartbeat every 1s)              │
│  • Offline buffer (200 payloads per device)                 │
└──────────┬──────────────────────────────────────────────────┘
           │  POST /station/status  (every 1s)
           │  POST /heartbeat       (every 1s)
           ▼
┌─────────────────────────────────────────────────────────────┐
│                        server.py                            │
│                                                             │
│  Status priority: BUSY > FULL > ALARM > NORMAL              │
│  State saved to state.json on every hold/alarm change       │
│                                                             │
│  Auto-scan threads:                                         │
│    • Startup scan                                           │
│    • Periodic scan (every 10 min)                           │
│    • Disconnect-triggered scan (after 30s debounce)         │
└──────────┬──────────────────────────────────────────────────┘
           │
    ┌──────┴──────────────────────┐
    │                             │
    ▼                             ▼
Dashboard / Node-RED          Robot / AMR
GET /station/status/all       POST /station/standby
GET /health                   POST /station/alarm
GET /heartbeat/all            GET /station/status/<id>
```

---

## 🚀 Complete Request Examples

### Check all stations

```bash
curl -H "X-API-Key: nextroboticslab2024" \
     http://192.168.10.211:5000/station/status/all
```

### Set a station on hold

```bash
curl -X POST \
     -H "X-API-Key: nextroboticslab2024" \
     -H "Content-Type: application/json" \
     -d '{"id": 10005, "hold": true, "robotID": "SMR-01", "taskID": "TASK-042"}' \
     http://192.168.10.211:5000/station/standby
```

### Release hold

```bash
curl -X POST \
     -H "X-API-Key: nextroboticslab2024" \
     -H "Content-Type: application/json" \
     -d '{"id": 10005, "hold": false, "robotID": "SMR-01"}' \
     http://192.168.10.211:5000/station/standby
```

### Turn alarm on

```bash
curl -X POST \
     -H "X-API-Key: nextroboticslab2024" \
     -H "Content-Type: application/json" \
     -d '{"id": 10005, "alarm": true}' \
     http://192.168.10.211:5000/station/alarm
```

### Trigger manual IP scan

```bash
curl -X POST \
     -H "X-API-Key: nextroboticslab2024" \
     -H "Content-Type: application/json" \
     -d '{"subnet": "192.168.20.0/24", "timeout": 60}' \
     http://192.168.10.211:5000/scan
```

### Check health (no auth needed)

```bash
curl http://192.168.10.211:5000/health
```

---

## 📝 License

Next Robotics Lab — Internal Use

---

*Last updated: 2026-04-16*
