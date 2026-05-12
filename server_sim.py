import os
import re
import json
import logging
import logging.handlers
import platform
import subprocess
import threading
import ipaddress
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, request, jsonify
from flask_cors import CORS
# ─────────────────────────────────────────────
#  Config
# ─────────────────────────────────────────────

API_KEY             = os.environ.get("API_KEY", "nextroboticslab2024")
STALE_THRESHOLD_SEC = int(os.environ.get("STALE_THRESHOLD", "5"))
RATE_LIMIT_PER_SEC  = int(os.environ.get("RATE_LIMIT", "5"))

# Scan config
DEVICES_FILE   = os.environ.get("DEVICES_FILE",  "devices.json")
DEVICE_IP_FILE = os.environ.get("DEVICE_IP_FILE", "device_ip.json")
DEFAULT_SUBNET = os.environ.get("DEFAULT_SUBNET", "192.168.20.0/24")
NMAP_TIMEOUT   = int(os.environ.get("NMAP_TIMEOUT", "60"))

# Auto-scan config
PERIODIC_SCAN_INTERVAL = int(os.environ.get("PERIODIC_SCAN_INTERVAL", "600"))  # 10 min
DISCONNECT_SCAN_DELAY  = int(os.environ.get("DISCONNECT_SCAN_DELAY",  "30"))   # wait 30s before rescan on disconnect

# State persistence — survives server restart
STATE_FILE     = os.environ.get("STATE_FILE", "state.json")
STATE_SIM_FILE = os.environ.get("STATE_SIM_FILE", "state_sim.json")

# ─────────────────────────────────────────────
#  Logger
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("flask_server")

# Dedicated operation logger → operation.log
_op_logger = logging.getLogger("operation")
_op_logger.setLevel(logging.INFO)
_op_logger.propagate = False
_op_fh = logging.handlers.RotatingFileHandler(
    "operation.log", maxBytes=10 * 1024 * 1024, backupCount=10
)
_op_fh.setFormatter(logging.Formatter(
    "%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
))
_op_logger.addHandler(_op_fh)

# ─────────────────────────────────────────────
#  State persistence helpers
# ─────────────────────────────────────────────

def _save_state() -> None:
    """
    Persist hold/alarm state of every device to STATE_FILE.
    Called inside db_lock — do NOT acquire the lock here.
    Only saves fields that need to survive a restart.
    """
    snapshot = {}
    for device_id, d in devices_db.items():
        snapshot[str(device_id)] = {
            "hold":    d.get("hold",    False),
            "alarm":   d.get("alarm",   False),
            "taskID":  d.get("taskID",  None),
            "robotID": d.get("robotID", None),
        }
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(snapshot, f, indent=2)
    except OSError as e:
        logger.error(f"[STATE] Failed to save state: {e}")


def _save_sim_state() -> None:
    """Persist simulation state to STATE_SIM_FILE."""
    with sim_lock:
        snapshot = {str(k): v for k, v in sim_db.items()}
    try:
        with open(STATE_SIM_FILE, "w") as f:
            json.dump(snapshot, f, indent=2)
    except OSError as e:
        logger.error(f"[SIM-STATE] Failed to save: {e}")


def _load_sim_state() -> None:
    """Load persisted simulation state."""
    if not os.path.exists(STATE_SIM_FILE):
        return
    try:
        with open(STATE_SIM_FILE) as f:
            snapshot = json.load(f)
        with sim_lock:
            for k, v in snapshot.items():
                sim_db[int(k)] = v
        logger.info(f"[SIM-STATE] Loaded {len(snapshot)} device(s)")
    except (OSError, json.JSONDecodeError, ValueError) as e:
        logger.warning(f"[SIM-STATE] Could not load: {e}")


def _load_state() -> None:
    """
    Load persisted state into devices_db on startup.
    Devices that were on hold when the server stopped will be restored.
    """
    if not os.path.exists(STATE_FILE):
        logger.info("[STATE] No saved state file found — starting fresh")
        return
    try:
        with open(STATE_FILE) as f:
            snapshot = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"[STATE] Could not load state: {e}")
        return

    restored_hold = []
    with db_lock:
        for k, s in snapshot.items():
            try:
                device_id = int(k)
            except ValueError:
                continue
            devices_db[device_id] = {
                "hold":      s.get("hold",    False),
                "alarm":     s.get("alarm",   False),
                "taskID":    s.get("taskID",  None),
                "robotID":   s.get("robotID", None),
                "status":    "BUSY" if s.get("hold") else "NORMAL",
                "timestamp": None,
                "input":     0,
            }
            if s.get("hold"):
                restored_hold.append(device_id)

    logger.info(f"[STATE] Loaded {len(snapshot)} device(s) from '{STATE_FILE}'")
    if restored_hold:
        logger.warning(f"[STATE] Hold state RESTORED for devices: {restored_hold}")
        for device_id in restored_hold:
            d = devices_db[device_id]
            _op_logger.info(
                f"HOLD_RESTORED  station={device_id}"
                f"  taskID={d['taskID']}  robotID={d['robotID']}"
            )

# ─────────────────────────────────────────────
#  Flask app
# ─────────────────────────────────────────────

app = Flask(__name__)
CORS(app)

# Structure per device:
# { "hold": bool, "alarm": bool, "status": str, "timestamp": str,
#   "taskID": str|None, "robotID": str|None }
devices_db: dict[int, dict] = {}
db_lock = threading.RLock()

# Per-device heartbeat store
# Structure: { device_id: { ...metrics..., "received_at": str } }
heartbeat_db: dict[int, dict] = {}
hb_lock = threading.RLock()

# Per-device simulation store
# { device_id: { "active": bool, "input": int } }
sim_db: dict[int, dict] = {}
sim_lock = threading.RLock()

# Per-device rate limiting
_rate_buckets: dict[int, list] = {}
_rate_lock = threading.Lock()

# IP scan state
_last_scan_result = None   # type: dict | None
_scan_lock = threading.Lock()

# device_ip.json version — incremented every time a new scan updates the file.
# Clients poll GET /device_ip/version and reload when the version changes.
_device_ip_version = 0
_device_ip_lock = threading.Lock()

# Disconnect-triggered scan: track which devices were stale last health check
# so we only trigger a rescan once per disconnect event (not every second).
_prev_disconnected = set()   # type: set
_disconnect_rescan_timer = None  # type: threading.Timer | None
_disconnect_rescan_lock = threading.Lock()

# ─────────────────────────────────────────────
#  Decorators
# ─────────────────────────────────────────────

def require_api_key(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if request.headers.get("X-API-Key", "") != API_KEY:
            logger.warning(f"Unauthorized request from {request.remote_addr}")
            return _err(RC_UNAUTHORIZED, "Unauthorized", 401)
        return f(*args, **kwargs)
    return wrapper


def rate_limit(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        import time
        data = request.get_json(silent=True) or {}
        device_id = data.get("id")
        if device_id is not None:
            now = time.monotonic()
            with _rate_lock:
                bucket = _rate_buckets.setdefault(device_id, [])
                bucket[:] = [t for t in bucket if now - t < 1.0]
                if len(bucket) >= RATE_LIMIT_PER_SEC:
                    return _err(RC_RATE_LIMITED, "Rate limit exceeded", 429)
                bucket.append(now)
        return f(*args, **kwargs)
    return wrapper

# ─────────────────────────────────────────────
#  Validation helpers
# ─────────────────────────────────────────────

VALID_STATES = {"NORMAL", "FULL", "BUSY", "ALARM"}

# ─────────────────────────────────────────────
#  Error codes  (4000-series)
# ─────────────────────────────────────────────
# Auth / transport
RC_UNAUTHORIZED          = 4001  # wrong or missing X-API-Key
RC_RATE_LIMITED          = 4002  # too many requests from this device
RC_METHOD_NOT_ALLOWED    = 4003  # wrong HTTP verb
RC_ENDPOINT_NOT_FOUND    = 4004  # unknown route
# Payload validation
RC_EMPTY_BODY            = 4010  # body missing or not JSON
RC_INVALID_ID            = 4011  # id not an integer
RC_INVALID_STATUS        = 4012  # status not in VALID_STATES
RC_INVALID_HOLD          = 4013  # hold not bool, or taskID/robotID wrong type
RC_INVALID_ALARM         = 4014  # alarm not bool
RC_INVALID_HEARTBEAT_ID  = 4015  # heartbeat id missing or not integer
# Device state
RC_DEVICE_NOT_FOUND      = 4020  # device_id not registered
RC_DEVICE_NOT_IN_HB      = 4021  # no heartbeat recorded for device
RC_DEVICE_OFFLINE        = 4022  # device is OFFLINE (heartbeat last_seen > STALE_THRESHOLD_SEC)
RC_ALREADY_HELD          = 4030  # station already held by another robot
RC_CANNOT_HOLD_FULL      = 4031  # station physically FULL, cannot hold
# Scan
RC_SCAN_IN_PROGRESS      = 4040  # concurrent /scan call
RC_SCAN_NO_RESULT        = 4041  # GET /scan/result before any scan ran
RC_INVALID_SUBNET        = 4042  # subnet string invalid
# Server
RC_INTERNAL_ERROR        = 5000  # unhandled exception


def _err(ret_code: int, message: str, http_status: int, **extra):
    """Uniform error envelope: {ret_code, error, ...extra}."""
    from flask import jsonify as _j
    body = {"ret_code": ret_code, "error": message}
    body.update(extra)
    return _j(body), http_status


def _is_device_offline(device_id: int) -> bool:
    """
    Return True if the device has no heartbeat, or its last_seen is older
    than STALE_THRESHOLD_SEC seconds.
    If simulation is active, the device is NEVER considered offline for API commands.
    """
    with sim_lock:
        if sim_db.get(device_id, {}).get("active"):
            return False

    with hb_lock:
        hb = heartbeat_db.get(device_id)
    if hb is None:
        return True   # never received a heartbeat — treat as offline
    last_seen = hb.get("last_seen")
    if not last_seen:
        return True
    try:
        last_dt = datetime.strptime(last_seen, "%Y-%m-%d %H:%M:%S")
        return (datetime.now() - last_dt).total_seconds() > STALE_THRESHOLD_SEC
    except ValueError:
        return True

def validate_status_payload(data: dict) -> str | None:
    if not data:
        return "Empty body"
    if "id" not in data or not isinstance(data["id"], int):
        return "Field 'id' must be an integer"
    if "input" not in data or data["input"] not in (0, 1):
        return "Field 'input' must be 0 or 1"
    return None

def validate_standby_payload(data: dict) -> str | None:
    if not data:
        return "Empty body"
    if "id" not in data or not isinstance(data["id"], int):
        return "Field 'id' must be an integer"
    if "hold" not in data or not isinstance(data["hold"], bool):
        return "Field 'hold' must be true or false"
    if "robotID" not in data or not isinstance(data["robotID"], str):
        return "Field 'robotID' must be a string"
    if data["hold"]:
        if "taskID" not in data or not isinstance(data["taskID"], str):
            return "Field 'taskID' must be a string when hold=true"
    return None

def validate_alarm_payload(data: dict) -> str | None:
    if not data:
        return "Empty body"
    if "id" not in data or not isinstance(data["id"], int):
        return "Field 'id' must be an integer"
    if "alarm" not in data or not isinstance(data["alarm"], bool):
        return "Field 'alarm' must be true or false"
    return None

def _derive_device_status(device_id: int) -> str:
    """
    Centralized status logic.
    Priority: SIM Mode > Real Physical State.
    If Simulated: Returns "SIM-NORMAL", "SIM-FULL", "SIM-BUSY", or "SIM-ALARM".
    If Not Simulated: Returns "NORMAL", "FULL", "BUSY", "ALARM", or "OFFLINE".
    """
    with sim_lock:
        s = sim_db.get(device_id, {"active": False, "input": 0})
        is_sim = s["active"]
        virtual_in = s["input"]

    with db_lock:
        d = devices_db.get(device_id)
        if not d:
            return "OFFLINE"

        hold  = d.get("hold",  False)
        alarm = d.get("alarm", False)

        # Use virtual input if sim is active, else use physical input
        effective_in = virtual_in if is_sim else d.get("input", 0)

        # ── Core Logic ──
        if hold and effective_in == 1:
            base_status = "FULL"
        elif hold and effective_in == 0:
            base_status = "BUSY"
        elif effective_in == 1:
            base_status = "FULL"
        elif alarm:
            base_status = "ALARM"
        else:
            base_status = "NORMAL"


        ## SIM-STATE TURN ON-OFF
        # if is_sim:
        #     return f"SIM-{base_status}"

        # If not sim, check if actually offline
        if _is_device_offline(device_id):
            return "OFFLINE"

        return base_status


# ─────────────────────────────────────────────
#  Routes — Simulation (Control from Dashboard)
# ─────────────────────────────────────────────

@app.route("/simulation/mode", methods=["POST"])
@require_api_key
def set_sim_mode():
    """Toggle simulation mode for a specific device."""
    data = request.get_json(silent=True) or {}
    device_id = data.get("id")
    active    = data.get("active", False)
    if not isinstance(device_id, int):
        return _err(RC_INVALID_ID, "id must be integer", 400)

    with sim_lock:
        s = sim_db.setdefault(device_id, {"active": False, "input": 0})
        s["active"] = active
        _save_sim_state()

    # Force immediate status update
    with db_lock:
        if device_id in devices_db:
            devices_db[device_id]["status"] = _derive_device_status(device_id)

    logger.info(f"[{device_id}] Simulation mode set to {active}")
    return jsonify({"id": device_id, "sim_active": active}), 200


@app.route("/simulation/input", methods=["POST"])
@require_api_key
def set_sim_input():
    """Set virtual input for a specific device."""
    data = request.get_json(silent=True) or {}
    device_id = data.get("id")
    val       = data.get("input", 0)
    if not isinstance(device_id, int):
        return _err(RC_INVALID_ID, "id must be integer", 400)

    with sim_lock:
        s = sim_db.setdefault(device_id, {"active": False, "input": 0})
        s["input"] = 1 if val else 0
        _save_sim_state()

    # Force immediate status update
    with db_lock:
        if device_id in devices_db:
            devices_db[device_id]["status"] = _derive_device_status(device_id)

    logger.info(f"[{device_id}] Simulated input set to {s['input']}")
    return jsonify({"id": device_id, "sim_input": s["input"]}), 200


# ─────────────────────────────────────────────
#  Routes — status (called by client every second)
# ─────────────────────────────────────────────

@app.route("/station/status", methods=["POST"])
@require_api_key
@rate_limit
def update_status():
    data = request.get_json(silent=True)
    err = validate_status_payload(data)
    if err:
        rc = RC_EMPTY_BODY if err == "Empty body" else (RC_INVALID_ID if "id" in err else RC_INVALID_STATUS)
        return _err(rc, err, 400)

    device_id   = data["id"]
    raw_input   = data["input"]   # 0 or 1 — physical sensor only

    # ── Simulation Logic ─────────────────────────────
    is_sim = False
    with sim_lock:
        s = sim_db.get(device_id)
        if s and s.get("active"):
            is_sim = True
            raw_input = s.get("input", 0)

    with db_lock:
        if device_id not in devices_db:
            devices_db[device_id] = {
                "hold":    False,
                "alarm":   False,
                "taskID":  None,
                "robotID": None,
            }
            logger.info(f"New device registered: {device_id} from {request.remote_addr}")
        else:
            # ── Reconnect detection ──────────────────────────
            # If timestamp was stale (device was offline) and now posting again,
            # log recovery and restore BUSY status if hold is still active.
            prev_ts = devices_db[device_id].get("timestamp")
            if prev_ts:
                try:
                    last = datetime.strptime(prev_ts, "%Y-%m-%d %H:%M:%S")
                    gap  = (datetime.now() - last).total_seconds()
                    if gap > STALE_THRESHOLD_SEC:
                        hold    = devices_db[device_id].get("hold", False)
                        task_id = devices_db[device_id].get("taskID")
                        robot_id= devices_db[device_id].get("robotID")
                        if hold:
                            logger.warning(
                                f"[{device_id}] RECONNECTED after {gap:.0f}s — "
                                f"hold state ACTIVE (taskID={task_id} robotID={robot_id})"
                            )
                            _op_logger.info(
                                f"RECONNECT_RECOVERED  station={device_id}"
                                f"  taskID={task_id}  robotID={robot_id}"
                                f"  offline_sec={gap:.0f}"
                            )
                        else:
                            logger.info(f"[{device_id}] Reconnected after {gap:.0f}s offline")
                except ValueError:
                    pass

        devices_db[device_id]["timestamp"] = data.get("timestamp")
        devices_db[device_id]["input"]     = raw_input

        # ── Server derives status ──
        status = _derive_device_status(device_id)
        devices_db[device_id]["status"] = status

        response_hold    = devices_db[device_id].get("hold",  False)
        response_alarm   = devices_db[device_id].get("alarm", False)
        response_taskID  = devices_db[device_id].get("taskID",  None)
        response_robotID = devices_db[device_id].get("robotID", None)

    return jsonify({
        "message":    "OK",
        "status":     status,
        "hold_mode":  response_hold,
        "alarm_mode": response_alarm,
        "taskID":     response_taskID,
        "robotID":    response_robotID,
        "simulated":  is_sim,  # Tell client to skip physical Modbus writes
    }), 201


@app.route("/station/status/all", methods=["GET"])
@require_api_key
def get_all():
    with db_lock:
        snapshot = {k: dict(v) for k, v in devices_db.items()}

    for device_id, d in snapshot.items():
        # Always derive status fresh — never trust the cached value
        d["status"] = _derive_device_status(device_id)

        with sim_lock:
            s = sim_db.get(device_id, {"active": False, "input": 0})
            d["sim_active"] = s["active"]
            d["sim_input"]  = s["input"]
            if s["active"]:
                d["input"] = s["input"]

    return jsonify(snapshot), 200


@app.route("/station/status/<int:device_id>", methods=["GET"])
@require_api_key
def get_one(device_id):
    with db_lock:
        device = devices_db.get(device_id)
    if device:
        result = dict(device)
        # Always derive status fresh — never trust the cached value
        result["status"] = _derive_device_status(device_id)

        with sim_lock:
            s = sim_db.get(device_id, {"active": False, "input": 0})
            result["sim_active"] = s["active"]
            result["sim_input"]  = s["input"]
            if s["active"]:
                result["input"] = s["input"]

        return jsonify(result), 200
    return _err(RC_DEVICE_NOT_FOUND, "Device not found", 404)

# ─────────────────────────────────────────────
#  Routes — control
# ─────────────────────────────────────────────

@app.route("/station/standby", methods=["POST"])
@require_api_key
def set_standby():
    data = request.get_json(silent=True)
    err = validate_standby_payload(data)
    if err:
        rc = RC_EMPTY_BODY if err == "Empty body" else (RC_INVALID_ID if "'id'" in err else RC_INVALID_HOLD)
        return _err(rc, err, 400)

    device_id = data["id"]
    hold      = data["hold"]
    task_id   = data.get("taskID")
    robot_id  = data["robotID"]   # always present now (required in validation)

    # ── OFFLINE guard — reject any hold/release on offline device ──
    # Check before acquiring db_lock to avoid deadlock with hb_lock.
    if _is_device_offline(device_id):
        logger.warning(f"[{device_id}] Hold request rejected — device is OFFLINE")
        return _err(
            RC_DEVICE_OFFLINE,
            f"Device {device_id} is OFFLINE — cannot set hold while device is unreachable",
            409,
        )

    with db_lock:
        if device_id not in devices_db:
            return _err(RC_DEVICE_NOT_FOUND, "Device not found", 404)

        current_hold   = devices_db[device_id].get("hold",    False)
        current_task   = devices_db[device_id].get("taskID")
        current_robot  = devices_db[device_id].get("robotID")
        current_status = devices_db[device_id].get("status", "NORMAL")

        # ── Already held by a different robot ─────────────────
        if hold and current_hold:
            logger.warning(f"[{device_id}] Hold rejected — already held by "
                           f"robotID={current_robot} taskID={current_task}")
            return _err(RC_ALREADY_HELD,
                        f"Device {device_id} is holding by robotID:{current_robot} taskID:{current_task} — please try again",
                        409,
                        held_by={"robotID": current_robot, "taskID": current_task})

        # ── FULL station cannot enter operation mode ───────────
        # Robot must wait until input=0 (station cleared) before holding.
        # Note: once hold is active, BUSY overrides FULL — this check
        # only blocks the *initial* hold request while sensor is HIGH.
        if hold and not current_hold:
            current_input = devices_db[device_id].get("input", 0)
            # Status: FULL if sensor still HIGH, BUSY if already clear
            initial_status = "FULL" if current_input == 1 else "BUSY"
            devices_db[device_id]["hold"]    = True
            devices_db[device_id]["taskID"]  = task_id
            devices_db[device_id]["robotID"] = robot_id
            devices_db[device_id]["status"]  = initial_status
            logger.info(f"[{device_id}] Hold ON  — taskID={task_id} robotID={robot_id} "
                        f"initial_status={initial_status}")
            _op_logger.info(
                f"HOLD_START  station={device_id}  taskID={task_id}"
                f"  robotID={robot_id}  prev_status={current_status}"
                f"  initial_status={initial_status}"
            )
            _save_state()

        # ── Release hold — only the same robot can release ────
        elif not hold and current_hold:
            if robot_id != current_robot:
                logger.warning(f"[{device_id}] Release rejected — "
                               f"robotID={robot_id} tried to release but held by robotID={current_robot}")
                return _err(RC_ALREADY_HELD,
                            f"Device {device_id} is held by robotID:{current_robot} — only that robot can release",
                            403,
                            held_by={"robotID": current_robot, "taskID": current_task})

            devices_db[device_id]["hold"]    = False
            devices_db[device_id]["taskID"]  = None
            devices_db[device_id]["robotID"] = None
            logger.info(f"[{device_id}] Hold OFF — taskID={current_task} robotID={current_robot} cleared")
            _op_logger.info(
                f"HOLD_END    station={device_id}  taskID={current_task}"
                f"  robotID={current_robot}  prev_status={current_status}"
            )
            _save_state()

        # Recalculate status immediately
        devices_db[device_id]["status"] = _derive_device_status(device_id)

        # Read results AFTER branches have mutated the record
        result_hold     = devices_db[device_id].get("hold",    False)
        result_task_id  = devices_db[device_id].get("taskID",  None)
        result_robot_id = devices_db[device_id].get("robotID", None)
        result_status   = devices_db[device_id].get("status",  "NORMAL")

    return jsonify({
        "message": f"Device {device_id} hold={result_hold}",
        "hold":    result_hold,
        "status":  result_status,
        "taskID":  result_task_id,
        "robotID": result_robot_id,
    }), 200


@app.route("/station/alarm", methods=["POST"])
@require_api_key
def set_alarm():
    data = request.get_json(silent=True)
    err = validate_alarm_payload(data)
    if err:
        rc = RC_EMPTY_BODY if err == "Empty body" else (RC_INVALID_ID if "'id'" in err else RC_INVALID_ALARM)
        return _err(rc, err, 400)

    device_id = data["id"]
    mode      = data["alarm"]

    # ── OFFLINE guard — reject alarm command on offline device ──
    if _is_device_offline(device_id):
        logger.warning(f"[{device_id}] Alarm request rejected — device is OFFLINE")
        return _err(
            RC_DEVICE_OFFLINE,
            f"Device {device_id} is OFFLINE — cannot set alarm while device is unreachable",
            409,
        )

    with db_lock:
        if device_id not in devices_db:
            return _err(RC_DEVICE_NOT_FOUND, "Device not found", 404)
        prev = devices_db[device_id].get("alarm", False)
        devices_db[device_id]["alarm"] = mode

    if prev != mode:
        logger.info(f"[{device_id}] Alarm set to {mode}")
        with db_lock:
            devices_db[device_id]["status"] = _derive_device_status(device_id)
            _save_state()

    return jsonify({"message": f"Device {device_id} alarm={mode}"}), 200

# ─────────────────────────────────────────────
#  Routes — heartbeat POST (from client.py)
# ─────────────────────────────────────────────

@app.route("/heartbeat", methods=["POST"])
@require_api_key
def heartbeat_post():
    """
    Receive a heartbeat from one device.

    Expected body:
    {
        "id":            1,
        "start_time":    "2024-01-01T12:00:00",
        "last_seen":     "2024-01-01 12:00:05",
        "error_count":   0,
        "timeout_count": 0,
        "post_ok":       120,
        "post_fail":     0,
        "reconnects":    0
    }
    """
    data = request.get_json(silent=True) or {}
    device_id = data.get("id")

    if device_id is None or not isinstance(device_id, int):
        return _err(RC_INVALID_HEARTBEAT_ID, "Field 'id' must be an integer", 400)

    # Drop error_count — not exposed in heartbeat
    stored = {k: v for k, v in data.items() if k != "error_count"}

    with hb_lock:
        heartbeat_db[device_id] = stored

    logger.debug(f"[{device_id}] Heartbeat received")
    return jsonify({"message": "OK"}), 200

# ─────────────────────────────────────────────
#  Routes — heartbeat GET
# ─────────────────────────────────────────────

@app.route("/heartbeat/all", methods=["GET"])
@require_api_key
def heartbeat_get_all():
    """Return the latest heartbeat data for every device."""
    _STRIP = {"received_at", "error_count"}
    with hb_lock:
        snapshot = {
            k: {fk: fv for fk, fv in v.items() if fk not in _STRIP}
            for k, v in heartbeat_db.items()
        }
    return jsonify(snapshot), 200


@app.route("/heartbeat/<int:device_id>", methods=["GET"])
@require_api_key
def heartbeat_get_one(device_id):
    """Return the latest heartbeat data for a single device."""
    _STRIP = {"received_at", "error_count"}
    with hb_lock:
        data = heartbeat_db.get(device_id)
    if data:
        return jsonify({k: v for k, v in data.items() if k not in _STRIP}), 200
    return _err(RC_DEVICE_NOT_IN_HB, f"No heartbeat received for device {device_id}", 404)

# ─────────────────────────────────────────────
#  Routes — health check (no auth required)
# ─────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    now    = datetime.now()
    cutoff = now - timedelta(seconds=STALE_THRESHOLD_SEC)
    disconnected, buzzer_on, hold_list = [], [], []

    with db_lock:
        total = len(devices_db)
        for device_id, d in devices_db.items():
            ts = d.get("timestamp")
            if ts:
                try:
                    if datetime.strptime(ts, "%Y-%m-%d %H:%M:%S") < cutoff:
                        disconnected.append(device_id)
                except ValueError:
                    pass
            if d.get("alarm"):
                buzzer_on.append(device_id)
            if d.get("hold"):
                hold_list.append({
                    "id":      device_id,
                    "taskID":  d.get("taskID"),
                    "robotID": d.get("robotID"),
                })

    return jsonify({
        "server_time":        now.strftime("%Y-%m-%d %H:%M:%S"),
        "total_devices":      total,
        "disconnected_device": disconnected,
        "buzzer_on":          buzzer_on,
        "hold_devices":       hold_list,
    }), 200

# ─────────────────────────────────────────────
#  IP scan helpers
# ─────────────────────────────────────────────

def _normalize_mac(mac: str) -> str | None:
    cleaned = re.sub(r"[^0-9a-fA-F]", "", mac)
    if len(cleaned) != 12:
        return None
    return ":".join(cleaned[i:i+2].upper() for i in range(0, 12, 2))


def _load_devices() -> dict[int, str]:
    if not os.path.exists(DEVICES_FILE):
        raise FileNotFoundError(f"'{DEVICES_FILE}' not found on server")
    with open(DEVICES_FILE) as f:
        raw = json.load(f)
    devices = {}
    for k, mac in raw.items():
        try:
            device_id = int(k)
        except ValueError:
            continue
        normalized = _normalize_mac(mac)
        if normalized:
            devices[device_id] = normalized
    if not devices:
        raise ValueError(f"No valid devices found in '{DEVICES_FILE}'")
    return devices


def _run_nmap(subnet: str, timeout: int) -> None:
    logger.info(f"[SCAN] nmap sweep on {subnet} ...")
    cmd = ["nmap", "-sn", "--host-timeout", f"{timeout}s", subnet]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 10)
        found = len(re.findall(r"Nmap scan report for", result.stdout))
        logger.info(f"[SCAN] nmap done — {found} host(s) responded")
    except FileNotFoundError:
        raise RuntimeError("nmap not found on server. Install: sudo apt install nmap")
    except subprocess.TimeoutExpired:
        logger.warning("[SCAN] nmap timed out — ARP table may be incomplete")


def _parse_arp() -> dict[str, str]:
    out = subprocess.check_output(["arp", "-an"], text=True, timeout=10)
    pattern = re.compile(
        r"(\d{1,3}(?:\.\d{1,3}){3}).+?"
        r"([0-9a-fA-F]{1,2}[:\-][0-9a-fA-F]{1,2}(?:[:\-][0-9a-fA-F]{1,2}){4})"
    )
    mac_to_ip = {}
    for line in out.splitlines():
        m = pattern.search(line)
        if not m:
            continue
        mac = _normalize_mac(m.group(2))
        if mac:
            mac_to_ip[mac] = m.group(1)
    logger.info(f"[SCAN] ARP table — {len(mac_to_ip)} entries")
    return mac_to_ip


def _run_scan(subnet: str, timeout: int) -> dict:
    devices = _load_devices()
    _run_nmap(subnet, timeout)
    arp = _parse_arp()
    if not arp:
        raise RuntimeError(
            "ARP table is empty. Run server with sudo, and ensure it is "
            "on the same subnet as the devices (192.168.20.x)."
        )
    matched, missing = {}, []
    for device_id, mac in devices.items():
        ip = arp.get(mac)
        if ip:
            matched[device_id] = ip
            logger.info(f"[SCAN]   {device_id}: {mac} -> {ip} OK")
        else:
            missing.append(device_id)
            logger.warning(f"[SCAN]   {device_id}: {mac} -> NOT FOUND")

    output = {str(k): v for k, v in sorted(matched.items())}
    with open(DEVICE_IP_FILE, "w") as f:
        json.dump(output, f, indent=2)
    logger.info(f"[SCAN] Written {len(output)} device(s) to '{DEVICE_IP_FILE}'")

    # Bump version so clients know to reload device_ip.json
    global _device_ip_version
    with _device_ip_lock:
        _device_ip_version += 1
        new_version = _device_ip_version
    logger.info(f"[SCAN] device_ip version is now {new_version}")

    return {
        "subnet":  subnet,
        "total":   len(devices),
        "found":   len(matched),
        "missing": missing,
        "matched": output,
        "scanned_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

# ─────────────────────────────────────────────
#  Auto-scan background threads
# ─────────────────────────────────────────────

def _background_scan(reason):
    # type: (str) -> None
    """
    Run a full nmap+ARP scan in a background thread.
    reason: human-readable label for logs ("startup" / "periodic" / "disconnect").
    Only one scan runs at a time (_scan_lock ensures this).
    """
    global _last_scan_result

    if not _scan_lock.acquire(blocking=False):
        logger.info(f"[AUTO-SCAN] Skipped ({reason}) — scan already in progress")
        return
    try:
        logger.info(f"[AUTO-SCAN] Starting — reason={reason}")
        result = _run_scan(DEFAULT_SUBNET, NMAP_TIMEOUT)
        _last_scan_result = result
        logger.info(
            f"[AUTO-SCAN] Done ({reason}) — "
            f"{result['found']}/{result['total']} device(s) resolved"
        )
        if result["missing"]:
            logger.warning(f"[AUTO-SCAN] Still missing: {result['missing']}")
    except Exception as exc:
        logger.error(f"[AUTO-SCAN] Failed ({reason}): {exc}", exc_info=True)
    finally:
        _scan_lock.release()


def _periodic_scan_loop():
    # type: () -> None
    """
    Background thread: run a full scan every PERIODIC_SCAN_INTERVAL seconds.
    First iteration waits the full interval (startup scan is separate).
    """
    import time
    time.sleep(PERIODIC_SCAN_INTERVAL)
    while True:
        _background_scan("periodic")
        time.sleep(PERIODIC_SCAN_INTERVAL)


def _check_disconnects_and_trigger_scan():
    # type: () -> None
    """
    Called by the /station/status route (server-side, inside update_status)
    AND by a dedicated background checker thread every STALE_THRESHOLD_SEC seconds.

    Logic:
      - Compare current stale devices against previously known stale devices.
      - If NEW devices became stale (newly disconnected), schedule a rescan
        after DISCONNECT_SCAN_DELAY seconds (debounce: cancel any pending timer first).
      - This ensures we only trigger once per disconnect event, not every second.
    """
    global _prev_disconnected, _disconnect_rescan_timer

    now    = datetime.now()
    cutoff = now - timedelta(seconds=STALE_THRESHOLD_SEC)
    currently_disconnected = set()

    with db_lock:
        for device_id, d in devices_db.items():
            # Update status using centralized logic (handles OFFLINE vs SIM)
            d["status"] = _derive_device_status(device_id)

            if d["status"] == "OFFLINE":
                currently_disconnected.add(device_id)

    newly_disconnected = currently_disconnected - _prev_disconnected
    _prev_disconnected = currently_disconnected

    if newly_disconnected:
        logger.warning(
            f"[DISCONNECT] New disconnection(s) detected: {sorted(newly_disconnected)} "
            f"— scheduling rescan in {DISCONNECT_SCAN_DELAY}s"
        )
        with _disconnect_rescan_lock:
            # Cancel previous pending timer if it hasn't fired yet
            if _disconnect_rescan_timer is not None and _disconnect_rescan_timer.is_alive():
                _disconnect_rescan_timer.cancel()
                logger.debug("[DISCONNECT] Previous rescan timer cancelled (new one started)")
            _disconnect_rescan_timer = threading.Timer(
                DISCONNECT_SCAN_DELAY,
                _background_scan,
                args=("disconnect",),
            )
            _disconnect_rescan_timer.daemon = True
            _disconnect_rescan_timer.start()


def _disconnect_monitor_loop():
    # type: () -> None
    """
    Background thread: check for disconnected devices every STALE_THRESHOLD_SEC seconds.
    This catches cases where no new status POSTs arrive (i.e. the client itself is down)
    so we can't rely on update_status() to trigger the check.
    """
    import time
    while True:
        time.sleep(STALE_THRESHOLD_SEC)
        try:
            _check_disconnects_and_trigger_scan()
        except Exception as exc:
            logger.error(f"[DISCONNECT-MONITOR] Error: {exc}", exc_info=True)


# ─────────────────────────────────────────────
#  Routes — device_ip (client polls this to detect IP changes)
# ─────────────────────────────────────────────

@app.route("/device_ip/version", methods=["GET"])
@require_api_key
def get_device_ip_version():
    """
    Returns the current version number of device_ip.json.
    Clients poll this endpoint; when the version changes they call GET /device_ip
    to fetch the new IP map and reload their connections.
    """
    with _device_ip_lock:
        v = _device_ip_version
    return jsonify({"version": v}), 200


@app.route("/device_ip", methods=["GET"])
@require_api_key
def get_device_ip():
    """
    Returns the current contents of device_ip.json so clients can reload
    without needing filesystem access.
    """
    if not os.path.exists(DEVICE_IP_FILE):
        return _err(RC_SCAN_NO_RESULT, "device_ip.json not found — run a scan first", 404)
    with _device_ip_lock:
        v = _device_ip_version
    try:
        with open(DEVICE_IP_FILE) as f:
            data = json.load(f)
        return jsonify({"version": v, "device_ip": data}), 200
    except (OSError, json.JSONDecodeError) as exc:
        return _err(RC_INTERNAL_ERROR, f"Could not read device_ip.json: {exc}", 500)


# ─────────────────────────────────────────────
#  Routes — scan (manual trigger stays, now also has status endpoint)
# ─────────────────────────────────────────────

@app.route("/scan", methods=["POST"])
@require_api_key
def trigger_scan():
    """
    Trigger an nmap + ARP scan to rediscover device IPs and update device_ip.json.

    Body (all optional):
      { "subnet": "192.168.20.0/24", "timeout": 60 }

    Returns 409 if a scan is already running.

    NOTE: The server process must be on the 192.168.20.x subnet and may need
          to run with sudo for nmap to populate the ARP table on Linux.
    """
    global _last_scan_result

    if not _scan_lock.acquire(blocking=False):
        return _err(RC_SCAN_IN_PROGRESS, "Scan already in progress", 409)

    try:
        body    = request.get_json(silent=True) or {}
        subnet  = body.get("subnet", DEFAULT_SUBNET)
        timeout = int(body.get("timeout", NMAP_TIMEOUT))

        try:
            if "/" not in subnet:
                subnet = str(ipaddress.ip_network(f"{subnet}/24", strict=False))
            else:
                ipaddress.ip_network(subnet, strict=False)
        except ValueError as e:
            return _err(RC_INVALID_SUBNET, f"Invalid subnet: {e}", 400)

        logger.info(f"[SCAN] Triggered via API — subnet={subnet}")
        result = _run_scan(subnet, timeout)
        _last_scan_result = result
        return jsonify(result), 200

    except Exception as exc:
        logger.error(f"[SCAN] Failed: {exc}", exc_info=True)
        return _err(RC_INTERNAL_ERROR, str(exc), 500)
    finally:
        _scan_lock.release()


@app.route("/scan/result", methods=["GET"])
@require_api_key
def scan_result():
    """Return the most recent scan result."""
    if _last_scan_result is None:
        return _err(RC_SCAN_NO_RESULT, "No scan has been run yet", 404)
    return jsonify(_last_scan_result), 200

@app.route("/scan/status", methods=["GET"])
@require_api_key
def scan_status():
    """Return whether a scan is currently running."""
    running = not _scan_lock.acquire(blocking=False)
    if not running:
        _scan_lock.release()
    return jsonify({"running": running}), 200


# ─────────────────────────────────────────────
#  Error handlers
# ─────────────────────────────────────────────

@app.errorhandler(404)
def not_found(e):
    return _err(RC_ENDPOINT_NOT_FOUND, "Endpoint not found", 404)

@app.errorhandler(405)
def method_not_allowed(e):
    return _err(RC_METHOD_NOT_ALLOWED, "Method not allowed", 405)

@app.errorhandler(500)
def internal_error(e):
    logger.error(f"Internal server error: {e}", exc_info=True)
    return _err(RC_INTERNAL_ERROR, "Internal server error", 500)

# ─────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────

if __name__ == "__main__":
    _load_state()
    _load_sim_state()

    # Auto-scan disabled — static IPs are used from device_ip.json.
    # Use POST /scan to trigger a manual rescan if needed.

    logger.info("[SCAN] Auto-scan disabled — using static IPs from device_ip.json")
    logger.info("Starting server on http://0.0.0.0:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)