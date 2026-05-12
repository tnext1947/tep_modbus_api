import time
import random
import signal
import threading
import requests
import logging
import json
import os
from datetime import datetime
from collections import deque
from logging.handlers import RotatingFileHandler
from pyModbusTCP.client import ModbusClient

# ─────────────────────────────────────────────
#  Config
# ─────────────────────────────────────────────

CONFIG_FILE  = "device_ip.json"
API_URL      = "http://127.0.0.1:5000/station/status"
API_KEY      = os.environ.get("API_KEY", "nextroboticslab2024")
POLL_INTERVAL           = 1.0    # seconds between Modbus reads
HEARTBEAT_INTERVAL      = 1.0    # seconds between heartbeat POSTs
WATCHDOG_INTERVAL       = 10.0   # seconds between thread health checks
OFFLINE_BUFFER_SIZE     = 200    # max queued payloads per device
RECONNECT_INTERVAL      = 10.0   # seconds between reconnect attempts (fixed, no backoff ceiling)
MODBUS_PORT             = 8899
MODBUS_TIMEOUT          = 5.0
LOG_RETENTION_DAYS      = 7      # connect.log and operation.log kept for 7 days

# ─────────────────────────────────────────────
#  Logger
# ─────────────────────────────────────────────

import glob

def _purge_old_logs(log_path: str, days: int) -> None:
    """Delete log files older than `days` days (handles rotated .1/.2/etc files)."""
    cutoff = time.time() - days * 86400
    base = log_path
    for f in glob.glob(base + "*"):
        try:
            if os.path.getmtime(f) < cutoff:
                os.remove(f)
                print(f"[LOG-PURGE] Deleted old log: {f}")
        except OSError:
            pass


def setup_logger(name: str, logfile: str) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(logfile, maxBytes=5 * 1024 * 1024, backupCount=5)
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


logger = setup_logger("modbus_client", "client.log")

# ── connect.log — first connect and last connect per device ──────────────
# Format: "YYYY-MM-DD HH:MM:SS  CONNECT_FIRST / CONNECT_LAST  station=ID  ip=IP"
_connect_logger = logging.getLogger("connect")
_connect_logger.setLevel(logging.INFO)
_connect_logger.propagate = False
_connect_fh = RotatingFileHandler("connect.log", maxBytes=5 * 1024 * 1024, backupCount=14)
_connect_fh.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
_connect_logger.addHandler(_connect_fh)

# Per-device first-connect flag so we log CONNECT_FIRST only once per session
_first_connect_done: dict = {}
_connect_lock = threading.Lock()

# Purge logs older than 7 days on startup
_purge_old_logs("client.log", LOG_RETENTION_DAYS)
_purge_old_logs("connect.log", LOG_RETENTION_DAYS)

# ─────────────────────────────────────────────
#  Load & validate device config
# ─────────────────────────────────────────────

def load_config(path: str) -> dict:
    if not os.path.exists(path):
        logger.critical(f"Config file '{path}' not found. Exiting.")
        raise SystemExit(1)

    with open(path, "r") as f:
        raw = json.load(f)

    config = {}
    for k, v in raw.items():
        try:
            device_id = int(k)
        except ValueError:
            logger.warning(f"Skipping non-integer key: {k}")
            continue

        parts = str(v).split(".")
        if len(parts) != 4 or not all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
            logger.warning(f"Skipping invalid IP for device {device_id}: {v}")
            continue

        config[device_id] = str(v)

    if not config:
        logger.critical("No valid devices found in config. Exiting.")
        raise SystemExit(1)

    logger.info(f"Loaded {len(config)} device(s): {list(config.keys())}")
    return config

DEVICE_CONFIG = load_config(CONFIG_FILE)

# ─────────────────────────────────────────────
#  Shared state
# ─────────────────────────────────────────────

shutdown_event = threading.Event()

# Per-device offline buffer (ring buffer, drops oldest if full)
offline_buffers: dict[int, deque] = {
    id: deque(maxlen=OFFLINE_BUFFER_SIZE) for id in DEVICE_CONFIG
}

# Per-device metrics (updated by each device thread, read by heartbeat thread)
metrics: dict[int, dict] = {
    id: {
        "start_time":    datetime.now().isoformat(),
        "last_seen":     None,
        "timeout_count": 0,
        "post_ok":       0,
        "post_fail":     0,
        "reconnects":    0,
    }
    for id in DEVICE_CONFIG
}

thread_registry: dict[int, threading.Thread] = {}

# ─────────────────────────────────────────────
#  HTTP helpers
# ─────────────────────────────────────────────

HEADERS = {
    "Content-Type": "application/json",
    "X-API-Key": API_KEY,
}

HEARTBEAT_URL = API_URL.replace("/station/status", "/heartbeat")

def _ensure_device_state(device_id: int) -> None:
    """
    Guarantee offline_buffers and metrics entries exist for device_id.
    Safe to call from any thread at any time — uses setdefault so it is
    a no-op if the entries already exist.
    This fixes KeyError when ip_watch_loop adds a new device to DEVICE_CONFIG
    after the module-level dict comprehensions have already run.
    """
    offline_buffers.setdefault(device_id, deque(maxlen=OFFLINE_BUFFER_SIZE))
    metrics.setdefault(device_id, {
        "start_time":    datetime.now().isoformat(),
        "last_seen":     None,
        "timeout_count": 0,
        "post_ok":       0,
        "post_fail":     0,
        "reconnects":    0,
    })


def post_with_buffer(device_id: int, payload: dict) -> dict | None:
    _ensure_device_state(device_id)   # ← guard: never KeyError even for late-added devices
    buf = offline_buffers[device_id]
    buf.append(payload)

    last_response = None
    while buf and not shutdown_event.is_set():
        item = buf[0]
        try:
            resp = requests.post(API_URL, json=item, headers=HEADERS, timeout=1)
            if resp.status_code == 201:
                buf.popleft()
                metrics[device_id]["post_ok"] += 1
                last_response = resp.json()
            else:
                logger.warning(f"[{device_id}] API returned {resp.status_code}")
                metrics[device_id]["post_fail"] += 1
                break
        except requests.exceptions.ConnectionError:
            metrics[device_id]["post_fail"] += 1
            break
        except Exception as exc:
            logger.debug(f"[{device_id}] POST error: {exc}")
            metrics[device_id]["post_fail"] += 1
            break

    buffered = len(buf)
    if buffered > 0:
        logger.warning(f"[{device_id}] {buffered} payload(s) buffered (API down)")

    return last_response

# ─────────────────────────────────────────────
#  Modbus connect — fixed 10s retry, no timeout
# ─────────────────────────────────────────────

def connect_with_backoff(client: ModbusClient, device_id: int) -> bool:
    """
    Retry Modbus connection every RECONNECT_INTERVAL seconds forever.
    No exponential backoff, no ceiling — keeps retrying every 10s until success or shutdown.
    Logs CONNECT_FIRST on the very first successful connection per session,
    and CONNECT_LAST on every subsequent reconnect.
    """
    _ensure_device_state(device_id)
    attempt = 0
    while not shutdown_event.is_set():
        if client.open():
            ip = client.host
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with _connect_lock:
                is_first = not _first_connect_done.get(device_id, False)
                if is_first:
                    _first_connect_done[device_id] = True

            if is_first:
                _connect_logger.info(
                    f"CONNECT_FIRST  station={device_id}  ip={ip}"
                )
                logger.info(f"[{device_id}] First connection established — IP: {ip}")
            else:
                metrics[device_id]["reconnects"] += 1
                _connect_logger.info(
                    f"CONNECT_LAST   station={device_id}  ip={ip}"
                    f"  attempt={attempt}"
                )
                logger.info(
                    f"[{device_id}] Reconnected after {attempt} attempt(s) — IP: {ip}"
                )
            return True

        attempt += 1
        logger.warning(
            f"[{device_id}] Connect failed (attempt #{attempt}). "
            f"Retrying in {RECONNECT_INTERVAL:.0f}s ..."
        )
        shutdown_event.wait(RECONNECT_INTERVAL)   # fixed 10s — never gives up
    return False

# ─────────────────────────────────────────────
#  Main device thread
# ─────────────────────────────────────────────

def read_and_send(device_id: int, ip: str) -> None:
    _ensure_device_state(device_id)   # guarantee metrics/buffer exist before any access
    logger.info(f"[{device_id}] Thread started — IP: {ip}")

    client = ModbusClient(
        host=ip,
        port=MODBUS_PORT,
        unit_id=1,
        timeout=MODBUS_TIMEOUT,
        auto_open=False,
    )

    my_hold  = False
    my_task_id  = None
    my_robot_id = None
    is_alarm   = False
    last_coils = None

    last_input = 0
    input_status = 0
    last_change_time = time.time()
    DEBOUNCE_TIME = 0.5

    if not connect_with_backoff(client, device_id):
        # Only reaches here if shutdown_event is set
        logger.info(f"[{device_id}] Shutdown during initial connect. Thread exits.")
        return

    # Write green (NORMAL) coil immediately on startup
    if client.write_multiple_coils(0, [True, False, False, False]):
        last_coils = [True, False, False, False]
        logger.info(f"[{device_id}] Startup: Green coil set (NORMAL)")

    while not shutdown_event.is_set():
        try:
            # ── Ensure connection — retry every 10s forever ────
            if not client.is_open:
                logger.warning(f"[{device_id}] Connection lost. Reconnecting...")
                if not connect_with_backoff(client, device_id):
                    break   # only exits on shutdown

            # ── Read discrete inputs ───────────────────────────
            regs = client.read_discrete_inputs(0, 8)

            if regs is None:
                metrics[device_id]["timeout_count"] += 1
                logger.warning(f"[{device_id}] Read timeout (total: {metrics[device_id]['timeout_count']})")
                shutdown_event.wait(POLL_INTERVAL)
                continue

            raw_input = 1 if (regs[0] or regs[1]) else 0
            now = time.time()

            if raw_input != last_input:
                last_change_time = now
                last_input = raw_input

            if (now - last_change_time) > DEBOUNCE_TIME:
                input_status = raw_input

            # ── POST raw physical input to API ───────────────
            # Server computes status (NORMAL/FULL/BUSY/ALARM) itself
            payload = {
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "id":        device_id,
                "input":     input_status,   # 0 or 1 only
            }

            response = post_with_buffer(device_id, payload)
            is_sim   = False

            if response is not None:
                my_hold     = response.get("hold_mode",  False)
                is_alarm    = response.get("alarm_mode", False)
                my_task_id  = response.get("taskID",     None)
                my_robot_id = response.get("robotID",    None)
                state       = response.get("status",     "NORMAL")
                is_sim      = response.get("simulated",  False)
                metrics[device_id]["last_seen"] = payload["timestamp"]
            else:
                # No response — derive state locally as fallback
                if my_hold:
                    state = "BUSY"        # operation mode wins everything
                elif input_status == 1:
                    state = "FULL"
                elif is_alarm:
                    state = "ALARM"
                else:
                    state = "NORMAL"

            hold_info = f"taskID={my_task_id} robotID={my_robot_id}" if my_hold else "—"
            sim_tag   = "[SIM] " if is_sim else ""
            logger.info(f"[{device_id}] {sim_tag}State:{state}  Input:{input_status}  Hold:{my_hold}  {hold_info}  "
                        f"Buf:{len(offline_buffers[device_id])}")

            # ── Write coils based on state ─────────────────────
            # Coil 0 = Green  (NORMAL)
            # Coil 1 = Yellow (BUSY)
            # Coil 2 = Red    (FULL)
            # Coil 3 = Alarm  (ALARM)
            ## SIM
            # if is_sim:
            #     # In simulation mode, we skip writing to physical coils
            #     continue

            c1_green     = False
            c2_operation = False
            c3_red       = False
            c4_alarm     = False

            match state:
                case "BUSY":
                    c2_operation = True
                case "ALARM":
                    c4_alarm = True
                case "FULL":
                    c3_red = True
                case "NORMAL":
                    c1_green = True

            new_coils = [c1_green, c2_operation, c3_red, c4_alarm]

            if new_coils != last_coils:
                ok = client.write_multiple_coils(0, new_coils)
                if ok:
                    logger.info(f"[{device_id}] Coils updated: {last_coils} → {new_coils}")
                    last_coils = new_coils
                else:
                    logger.warning(f"[{device_id}] Coil write failed — will retry next cycle")
                    # Do NOT update last_coils on failure so we retry next cycle.

        except Exception as exc:
            logger.error(f"[{device_id}] Unhandled error: {exc}", exc_info=True)

        shutdown_event.wait(POLL_INTERVAL)

    client.close()
    logger.info(f"[{device_id}] Thread exiting cleanly.")

# ─────────────────────────────────────────────
#  Heartbeat thread — one POST per device
# ─────────────────────────────────────────────

def heartbeat_loop() -> None:
    """
    Every HEARTBEAT_INTERVAL seconds, POST a separate heartbeat for each device
    to /heartbeat. The server stores them individually so you can query:
      GET /heartbeat/<id>   — single device
      GET /heartbeat/all    — all devices
    """
    while not shutdown_event.is_set():
        for device_id, m in metrics.items():
            payload = {
                "id": device_id,
                **{k: v for k, v in m.items()},  # start_time, last_seen, counters…
            }
            try:
                requests.post(HEARTBEAT_URL, json=payload, headers=HEADERS, timeout=2)
            except Exception:
                pass  # Heartbeat is best-effort — never block the main loop

        shutdown_event.wait(HEARTBEAT_INTERVAL)

# ─────────────────────────────────────────────
#  Watchdog thread
# ─────────────────────────────────────────────

def watchdog_loop() -> None:
    """Restart any device thread that has died unexpectedly."""
    while not shutdown_event.is_set():
        for device_id, ip in DEVICE_CONFIG.items():
            t = thread_registry.get(device_id)
            if t is not None and not t.is_alive():
                logger.warning(f"[WATCHDOG] Thread for device {device_id} is dead — restarting")
                _start_device_thread(device_id, ip)
        shutdown_event.wait(WATCHDOG_INTERVAL)

# ─────────────────────────────────────────────
#  Thread management
# ─────────────────────────────────────────────

def _start_device_thread(device_id: int, ip: str) -> None:
    t = threading.Thread(
        target=read_and_send,
        args=(device_id, ip),
        daemon=True,
        name=f"dev-{device_id}",
    )
    t.start()
    thread_registry[device_id] = t

# ─────────────────────────────────────────────
#  Graceful shutdown
# ─────────────────────────────────────────────

def handle_shutdown(signum, frame) -> None:
    logger.info(f"Received signal {signum}. Shutting down...")
    shutdown_event.set()

signal.signal(signal.SIGTERM, handle_shutdown)
signal.signal(signal.SIGINT,  handle_shutdown)

# ─────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────

def main() -> None:
    logger.info(f"Starting industrial Modbus client — {len(DEVICE_CONFIG)} device(s)")

    for device_id, ip in DEVICE_CONFIG.items():
        _start_device_thread(device_id, ip)

    threading.Thread(target=watchdog_loop, daemon=True, name="watchdog").start()
    threading.Thread(target=heartbeat_loop, daemon=True, name="heartbeat").start()

    try:
        while not shutdown_event.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        shutdown_event.set()

    logger.info("Waiting for device threads to stop...")
    for device_id, t in thread_registry.items():
        t.join(timeout=5)
        if t.is_alive():
            logger.warning(f"[{device_id}] Thread did not stop in time.")

    logger.info("Shutdown complete.")


if __name__ == "__main__":
    main()