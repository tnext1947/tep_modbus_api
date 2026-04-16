"""
Workflow:
  1. Read devices.json        { "1": "AA:BB:CC:DD:EE:FF", "2": "11:22:33:44:55:66" }
  2. Run nmap to populate ARP cache for the whole subnet
  3. Parse `arp -an` to build MAC → IP map
  4. Match each device's MAC to an IP
  5. Write device_ip.json     { "1": "192.168.1.101", "2": "192.168.1.102" }

Requirements:
    pip install python-nmap
    sudo apt install nmap       # Linux
Run:
    python find_ip.py                        # auto-detect gateway
    python find_ip.py --gateway 192.168.1.0  # specify subnet manually
    python find_ip.py --gateway 192.168.1.0 --timeout 30
"""

import argparse
import ipaddress
import json
import logging
import os
import platform
import re
import subprocess
import sys

# ─────────────────────────────────────────────
#  Config
# ─────────────────────────────────────────────

DEVICES_FILE    = "devices.json"      # input:  { "id": "MAC" }
DEVICE_IP_FILE  = "device_ip.json"    # output: { "id": "IP"  }
NMAP_TIMEOUT    = 60                  # seconds for nmap ping sweep

# ─────────────────────────────────────────────
#  Logger
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("find_ip")

# ─────────────────────────────────────────────
#  Load devices.json
# ─────────────────────────────────────────────

def load_devices(path: str) -> dict[int, str]:

    if not os.path.exists(path):
        logger.critical(f"'{path}' not found. Create it first.")
        sys.exit(1)

    with open(path) as f:
        raw = json.load(f)

    devices = {}
    for k, mac in raw.items():
        try:
            device_id = int(k)
        except ValueError:
            logger.warning(f"Skipping non-integer key: {k}")
            continue

        normalized = normalize_mac(mac)
        if normalized is None:
            logger.warning(f"Skipping invalid MAC for device {device_id}: {mac}")
            continue

        devices[device_id] = normalized

    if not devices:
        logger.critical("No valid devices in devices.json. Exiting.")
        sys.exit(1)

    logger.info(f"Loaded {len(devices)} device(s) from '{path}'")
    return devices

# ─────────────────────────────────────────────
#  MAC normalization
# ─────────────────────────────────────────────

def normalize_mac(mac: str) -> str | None:
    """
    Accept any common MAC format and return uppercase colon-separated.
    Accepts:  aa:bb:cc:dd:ee:ff  |  AA-BB-CC-DD-EE-FF  |  aabbccddeeff
    Returns:  AA:BB:CC:DD:EE:FF  or None if invalid.
    """
    cleaned = re.sub(r"[^0-9a-fA-F]", "", mac)
    if len(cleaned) != 12:
        return None
    return ":".join(cleaned[i:i+2].upper() for i in range(0, 12, 2))

# ─────────────────────────────────────────────
#  Auto-detect default gateway subnet
# ─────────────────────────────────────────────

def detect_gateway() -> str | None:
    system = platform.system()
    try:
        if system == "Windows":
            out = subprocess.check_output("ipconfig", text=True)
            # Find 'Default Gateway' line
            match = re.search(r"Default Gateway[^\d]+([\d.]+)", out)
        elif system == "Darwin":
            out = subprocess.check_output(["netstat", "-rn"], text=True)
            match = re.search(r"^default\s+([\d.]+)", out, re.MULTILINE)
        else:  # Linux
            out = subprocess.check_output(["ip", "route"], text=True)
            match = re.search(r"default via ([\d.]+)", out)

        if match:
            gw_ip = match.group(1)
            # Convert to /24 subnet
            network = ipaddress.ip_network(f"{gw_ip}/24", strict=False)
            subnet = str(network)
            logger.info(f"Auto-detected gateway subnet: {subnet}")
            return subnet

    except Exception as exc:
        logger.warning(f"Gateway auto-detection failed: {exc}")

    return None

# ─────────────────────────────────────────────
#  nmap ping sweep — populates ARP cache
# ─────────────────────────────────────────────

def run_nmap_sweep(subnet: str, timeout: int) -> None:
    logger.info(f"Running nmap ping sweep on {subnet} (timeout={timeout}s) ...")
    logger.info("This may take a few seconds — please wait.")

    cmd = ["nmap", "-sn", "--host-timeout", f"{timeout}s", subnet]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout + 10,
        )
        if result.returncode != 0:
            logger.warning(f"nmap returned exit code {result.returncode}")
            if result.stderr:
                logger.warning(f"nmap stderr: {result.stderr.strip()}")
        else:
            # Count hosts found
            found = len(re.findall(r"Nmap scan report for", result.stdout))
            logger.info(f"nmap complete — {found} host(s) responded")

    except FileNotFoundError:
        logger.error("nmap not found. Install it: sudo apt install nmap")
        sys.exit(1)
    except subprocess.TimeoutExpired:
        logger.warning("nmap timed out — ARP table may be incomplete")

# ─────────────────────────────────────────────
#  Parse ARP table  →  { MAC: IP }
# ─────────────────────────────────────────────

def parse_arp_table() -> dict[str, str]:
    """
    Run `arp -a` and parse the output into { normalized_mac: ip }.
    Works on Linux, macOS, and Windows.
    """
    logger.info("Reading ARP table ...")
    try:
        out = subprocess.check_output(["arp", "-an"], text=True, timeout=10)
    except FileNotFoundError:
        logger.error("`arp` command not found.")
        sys.exit(1)
    except subprocess.TimeoutExpired:
        logger.error("`arp -a` timed out.")
        sys.exit(1)

    mac_to_ip: dict[str, str] = {}

    ip_mac_pattern = re.compile(
        r"(\d{1,3}(?:\.\d{1,3}){3})"                      # IP address
        r".+?"                                              # anything between
        r"([0-9a-fA-F]{1,2}[:\-][0-9a-fA-F]{1,2}"        # MAC start
        r"(?:[:\-][0-9a-fA-F]{1,2}){4})"                  # MAC rest
    )

    for line in out.splitlines():
        match = ip_mac_pattern.search(line)
        if not match:
            continue
        ip  = match.group(1)
        mac = normalize_mac(match.group(2))
        if mac:
            mac_to_ip[mac] = ip

    logger.info(f"ARP table: {len(mac_to_ip)} entry/entries found")
    return mac_to_ip

# ─────────────────────────────────────────────
#  Match MACs → IPs
# ─────────────────────────────────────────────

def match_devices(devices: dict[int, str], arp_table: dict[str, str]) -> dict[int, str]:

    result: dict[int, str] = {}

    for device_id, mac in devices.items():
        ip = arp_table.get(mac)
        if ip:
            logger.info(f"  Device {device_id}: {mac} → {ip} ✓")
            result[device_id] = ip
        else:
            logger.warning(f"  Device {device_id}: {mac} → NOT FOUND in ARP table")

    return result

# ─────────────────────────────────────────────
#  Write device_ip.json
# ─────────────────────────────────────────────

def write_device_ip(matched: dict[int, str], path: str) -> None:
    output = {str(k): v for k, v in sorted(matched.items())}
    with open(path, "w") as f:
        json.dump(output, f, indent=2)
    logger.info(f"Written {len(output)} device(s) to '{path}'")

# ─────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Discover device IPs by MAC address and write device_ip.json"
    )
    parser.add_argument(
        "--gateway",
        default=None,
        help="Subnet to scan, e.g. 192.168.1.0/24 (auto-detected if omitted)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=NMAP_TIMEOUT,
        help=f"nmap timeout in seconds (default: {NMAP_TIMEOUT})",
    )
    parser.add_argument(
        "--devices",
        default=DEVICES_FILE,
        help=f"Input file (default: {DEVICES_FILE})",
    )
    parser.add_argument(
        "--output",
        default=DEVICE_IP_FILE,
        help=f"Output file (default: {DEVICE_IP_FILE})",
    )
    args = parser.parse_args()

    devices = load_devices(args.devices)
    logger.info("Devices to find:")
    for did, mac in devices.items():
        logger.info(f"  ID {did}: {mac}")

    subnet = args.gateway
    if subnet:
        if "/" not in subnet:
            subnet = str(ipaddress.ip_network(f"{subnet}/24", strict=False))
        logger.info(f"Using subnet: {subnet}")
    else:
        subnet = detect_gateway()
        if not subnet:
            logger.critical(
                "Could not auto-detect gateway. "
                "Run with --gateway 192.168.x.0 to specify manually."
            )
            sys.exit(1)

    run_nmap_sweep(subnet, args.timeout)

    arp_table = parse_arp_table()
    if not arp_table:
        logger.error("ARP table is empty. nmap may need sudo on Linux.")
        logger.error("Try:  sudo python find_ip.py")
        sys.exit(1)

    logger.info("Matching MACs to IPs:")
    matched = match_devices(devices, arp_table)

    if not matched:
        logger.error("No devices matched. Check your MACs in devices.json.")
        sys.exit(1)

    write_device_ip(matched, args.output)

    # ── Summary ────────────────────────────────
    total    = len(devices)
    found    = len(matched)
    missing  = total - found
    logger.info("─" * 40)
    logger.info(f"Done. {found}/{total} device(s) resolved.")
    if missing:
        missing_ids = [did for did in devices if did not in matched]
        logger.warning(f"{missing} device(s) not found: {missing_ids}")
        logger.warning("Check that those devices are powered on and on the same network.")
    else:
        logger.info("All devices found successfully.")


if __name__ == "__main__":
    main()