# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the Project

### Local (no Docker)
```bash
pip install -r requirements.txt
python server_sim.py          # Flask API on :5000
python cilent.py              # Modbus client (reads device_ip.json for IPs)
```

### Docker
```bash
# Build image
docker build -t lane-infra:latest .

# Export for transfer to another machine
docker save lane-infra:latest -o lane-infra.tar

# Load on target machine and run
docker load -i lane-infra.tar
docker compose up -d

# Logs (both server and client output here)
docker compose logs -f
docker compose down && docker compose up -d   # restart after config changes
```

> On Windows/WSL: `network_mode: host` does not expose ports externally. Use the `ports` mapping in `docker-compose.yml` and open port 5000 in Windows Firewall (`New-NetFirewallRule -DisplayName "Allow Port 5000" -Direction Inbound -Protocol TCP -LocalPort 5000 -Action Allow`).

## Architecture

Two processes run inside a single Docker container, started by `start.sh`:

```
start.sh
  ├── server_sim.py  (background)   Flask REST API on :5000
  └── cilent.py      (PID 1)        Modbus TCP client
```

**`server_sim.py`** is the central state machine. It owns all business logic:
- Receives raw `input` (0 or 1) from `cilent.py` via `POST /station/status`
- Derives station status (`NORMAL / FULL / BUSY / ALARM / OFFLINE`) using `_derive_device_status()`
- Manages hold/alarm state with `db_lock` (RLock) protecting `devices_db`
- Stores simulation state separately in `sim_db` with `sim_lock`
- Persists hold/alarm to `state.json` and sim state to `state_sim.json` on every change

**`cilent.py`** is a pure I/O worker. It does not contain business logic:
- Spawns one thread per device from `device_ip.json`
- Reads Modbus discrete inputs 0–1 (physical sensor) from each device on port 8899
- POSTs raw `input` to server and receives back the derived `status`
- Writes coils 0–3 to the physical device based on returned status (Green/Yellow/Red/Alarm)
- Has a watchdog thread that restarts dead device threads every 10s
- Has an offline ring buffer (200 payloads/device) that drains when the API comes back

## Key Design Decisions

**Status is always derived server-side.** `cilent.py` never computes status — it only sends raw `input` (0 or 1). `_derive_device_status()` in `server_sim.py` is the single source of truth. `GET /station/status/all` and `GET /station/status/<id>` call `_derive_device_status()` live on every request — never return a cached value.

**Static IPs.** Auto-scan (nmap + ARP) is disabled. `device_ip.json` is the source of IPs and is edited directly. `devices_ip_real.json` is the master copy — copy it to `device_ip.json` to apply. Use `POST /scan` only for manual rediscovery when a device IP changes.

**Simulation bypasses OFFLINE.** When `sim_db[device_id]["active"] == True`, `_is_device_offline()` returns `False` unconditionally and `sim_db["input"]` replaces the real sensor. Simulated devices are auto-registered in `devices_db` on first `POST /simulation/mode` — no real client required.

**Hold ownership.** Only the robot that set `hold=true` (matched by `robotID`) can release it. A hold on a FULL station is legal — status stays `FULL` until the sensor drops to 0, then becomes `BUSY`.

**In-memory stores.** `devices_db`, `heartbeat_db`, `sim_db` are plain dicts in memory. Only `hold`, `alarm`, `taskID`, `robotID` survive restart (via `state.json`). Timestamps and counters reset on restart.

## Environment Variables

All config is via environment variables with defaults in the source:

| Variable | Default | File |
|---|---|---|
| `API_KEY` | `nextroboticslab2024` | both |
| `STALE_THRESHOLD` | `5` | server |
| `RATE_LIMIT` | `5` | server |
| `DEFAULT_SUBNET` | `192.168.20.0/24` | server |
| `DEVICE_IP_FILE` | `device_ip.json` | server |
| `STATE_FILE` | `state.json` | server |
| `STATE_SIM_FILE` | `state_sim.json` | server |

## Volume Mounts (docker-compose.yml)

These host files are mounted into `/app/` in the container. They must exist as files (not directories) before `docker compose up`:

```
devices.json       device_ip.json     state.json     state_sim.json
client.log         connect.log        operation.log
```

Create empty placeholders if missing: `touch client.log connect.log operation.log && echo '{}' > state.json && echo '{}' > state_sim.json`

## API Authentication

All endpoints except `GET /health` require the header:
```
X-API-Key: nextroboticslab2024
```
