#!/usr/bin/env python3
"""
Home network latency diagnosis tool (macOS).

Measures Wi-Fi radio quality, IPv6 reachability, latency, route, DNS
response time, throughput (single + parallel), and bufferbloat (via
networkQuality), then saves everything as one labeled JSON snapshot so
runs can be compared before/after a change (new router placement, new
channel, ISP switch to IPoE, etc).

Usage:
    python3 diagnose.py --label before
    ... make a change (move the router, switch channel, etc) ...
    python3 diagnose.py --label after

Requires macOS (uses system_profiler, networkQuality, ifconfig,
netstat, route, traceroute -- all Apple-supplied). No third-party
Python packages are required.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import ipaddress
import json
import os
import platform
import random
import re
import socket
import statistics
import struct
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

DEFAULT_BIG_FILE_URL = "https://speed.cloudflare.com/__down?bytes={size}"
DEFAULT_FILE_SIZE_MB = 100
DEFAULT_PING_COUNT = 20
DEFAULT_DNS_DOMAIN = "www.apple.com"
DEFAULT_PARALLEL_WORKERS = 6
TARGETS = {"cloudflare": "1.1.1.1", "google": "8.8.8.8"}


def run(cmd, timeout=15):
    """Run a command, return (stdout, stderr, returncode). Never raises."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        return proc.stdout, proc.stderr, proc.returncode
    except FileNotFoundError:
        return "", f"command not found: {cmd[0]}", 127
    except subprocess.TimeoutExpired:
        return "", f"timed out after {timeout}s: {' '.join(cmd)}", 124
    except Exception as e:  # noqa: BLE001 -- this is a best-effort probe tool
        return "", str(e), 1


# ---------------------------------------------------------------------------
# 1. Wi-Fi
# ---------------------------------------------------------------------------

def _find_current_network(node):
    """Walk the system_profiler JSON tree for the entry marked as the
    currently-associated network."""
    if isinstance(node, dict):
        if node.get("spairport_current_network_information") is True:
            return node
        for v in node.values():
            found = _find_current_network(v)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_current_network(item)
            if found is not None:
                return found
    return None


def get_wifi_info():
    result = {
        "available": False,
        "ssid": None,
        "signal_dbm": None,
        "noise_dbm": None,
        "snr_db": None,
        "tx_rate_mbps": None,
        "phy_mode": None,
        "channel": None,
        "channel_width_mhz": None,
        "raw": None,
        "error": None,
    }
    if platform.system() != "Darwin":
        result["error"] = "not macOS; Wi-Fi radio stats unavailable"
        return result

    out, err, rc = run(["system_profiler", "SPAirPortDataType", "-json"], timeout=20)
    if rc != 0 or not out.strip():
        result["error"] = err or "system_profiler returned no data"
        return result

    try:
        data = json.loads(out)
    except json.JSONDecodeError as e:
        result["error"] = f"could not parse system_profiler JSON: {e}"
        return result

    net = _find_current_network(data)
    if net is None:
        result["error"] = "not currently associated to a Wi-Fi network"
        return result

    result["available"] = True
    result["ssid"] = net.get("_name")
    result["raw"] = net

    signal_noise = net.get("spairport_signal_noise", "")
    m = re.search(r"(-?\d+)\s*dBm\s*/\s*(-?\d+)\s*dBm", signal_noise)
    if m:
        signal, noise = int(m.group(1)), int(m.group(2))
        result["signal_dbm"] = signal
        result["noise_dbm"] = noise
        result["snr_db"] = signal - noise

    rate = net.get("spairport_network_rate")
    if rate is not None:
        result["tx_rate_mbps"] = rate

    result["phy_mode"] = net.get("spairport_network_phymode")

    channel_raw = net.get("spairport_network_channel", "")
    result["channel"] = channel_raw
    m = re.search(r"(\d+)\s*MHz", str(channel_raw))
    if m:
        result["channel_width_mhz"] = int(m.group(1))

    return result


# ---------------------------------------------------------------------------
# 2. IPv6
# ---------------------------------------------------------------------------

def get_default_gateway():
    out, _, rc = run(["route", "-n", "get", "default"])
    if rc == 0:
        m = re.search(r"gateway:\s*(\S+)", out)
        if m:
            return m.group(1)
    out, _, rc = run(["netstat", "-rn", "-f", "inet"])
    for line in out.splitlines():
        if line.startswith("default"):
            parts = line.split()
            if len(parts) >= 2:
                return parts[1]
    return None


def get_ipv6_info():
    result = {
        "global_address": None,
        "has_default_route": False,
        "default_route_raw": None,
        "all_addresses": [],
        "error": None,
    }

    out, err, rc = run(["ifconfig"])
    if rc != 0:
        result["error"] = err or "ifconfig failed"
        return result

    for line in out.splitlines():
        line = line.strip()
        if line.startswith("inet6"):
            parts = line.split()
            if len(parts) >= 2:
                addr = parts[1].split("%")[0]
                result["all_addresses"].append(addr)
                try:
                    ip = ipaddress.IPv6Address(addr)
                    if ip.is_global:
                        result["global_address"] = addr
                except ValueError:
                    pass

    out, err, rc = run(["netstat", "-rn", "-f", "inet6"])
    if rc == 0:
        for line in out.splitlines():
            if line.startswith("default"):
                result["has_default_route"] = True
                result["default_route_raw"] = line.strip()
                break
    else:
        result["error"] = err or result["error"]

    return result


# ---------------------------------------------------------------------------
# 3. Latency (ping)
# ---------------------------------------------------------------------------

def ping_host(host, count=DEFAULT_PING_COUNT):
    result = {
        "host": host,
        "transmitted": None,
        "received": None,
        "loss_pct": None,
        "min_ms": None,
        "avg_ms": None,
        "max_ms": None,
        "stddev_ms": None,
        "error": None,
    }
    if not host:
        result["error"] = "no host given"
        return result

    out, err, rc = run(["ping", "-c", str(count), host], timeout=count * 2 + 10)
    if not out:
        result["error"] = err or "no ping output"
        return result

    m = re.search(
        r"(\d+) packets transmitted, (\d+) (?:packets )?received,.*?([\d.]+)% packet loss",
        out,
    )
    if m:
        result["transmitted"] = int(m.group(1))
        result["received"] = int(m.group(2))
        result["loss_pct"] = float(m.group(3))

    m = re.search(
        r"= ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)\s*ms", out
    )
    if m:
        result["min_ms"] = float(m.group(1))
        result["avg_ms"] = float(m.group(2))
        result["max_ms"] = float(m.group(3))
        result["stddev_ms"] = float(m.group(4))
    elif result["received"] == 0:
        result["error"] = "100% packet loss"

    return result


# ---------------------------------------------------------------------------
# 4. Traceroute (first N hops)
# ---------------------------------------------------------------------------

def traceroute_first_hops(host, max_hops=4, probes_per_hop=3):
    result = {"host": host, "hops": [], "error": None}
    if not host:
        result["error"] = "no host given"
        return result

    out, err, rc = run(
        ["traceroute", "-m", str(max_hops), "-q", str(probes_per_hop), "-w", "1", host],
        timeout=max_hops * probes_per_hop * 2 + 10,
    )
    if not out:
        result["error"] = err or "no traceroute output"
        return result

    for line in out.splitlines():
        m = re.match(r"^\s*(\d+)\s+(.*)$", line)
        if not m:
            continue
        hop_num = int(m.group(1))
        rest = m.group(2)
        if "* * *" in rest or rest.strip() == "* * *":
            result["hops"].append({"hop": hop_num, "ip": None, "rtts_ms": [], "timeout": True})
            continue
        ip_m = re.search(r"\(([\d.]+)\)", rest)
        ip = ip_m.group(1) if ip_m else None
        rtts = [float(x) for x in re.findall(r"([\d.]+)\s*ms", rest)]
        result["hops"].append({"hop": hop_num, "ip": ip, "rtts_ms": rtts, "timeout": False})

    return result


# ---------------------------------------------------------------------------
# 5. DNS response time (hand-rolled UDP query -- no external deps)
# ---------------------------------------------------------------------------

def _build_dns_query(domain):
    txid = random.randint(0, 0xFFFF)
    header = struct.pack(">HHHHHH", txid, 0x0100, 1, 0, 0, 0)
    question = b""
    for label in domain.strip(".").split("."):
        question += struct.pack("B", len(label)) + label.encode("ascii")
    question += b"\x00" + struct.pack(">HH", 1, 1)  # type A, class IN
    return header + question, txid


def dns_query_time(server, domain=DEFAULT_DNS_DOMAIN, timeout=2.0, attempts=5):
    result = {
        "server": server,
        "domain": domain,
        "attempts": attempts,
        "successes": 0,
        "min_ms": None,
        "avg_ms": None,
        "max_ms": None,
        "error": None,
    }
    if not server:
        result["error"] = "no DNS server given"
        return result

    times = []
    for _ in range(attempts):
        query, txid = _build_dns_query(domain)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        try:
            start = time.monotonic()
            sock.sendto(query, (server, 53))
            data, _ = sock.recvfrom(4096)
            elapsed = (time.monotonic() - start) * 1000
            resp_txid = struct.unpack(">H", data[:2])[0]
            if resp_txid == txid:
                times.append(elapsed)
        except (socket.timeout, OSError):
            pass
        finally:
            sock.close()

    if times:
        result["successes"] = len(times)
        result["min_ms"] = min(times)
        result["avg_ms"] = statistics.mean(times)
        result["max_ms"] = max(times)
    else:
        result["error"] = "no response to any query"

    return result


# ---------------------------------------------------------------------------
# 6. Throughput: single-connection and parallel download
# ---------------------------------------------------------------------------

def _download_range(url, start, end, timeout):
    req = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}"})
    total = 0
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            total += len(chunk)
    return total


def throughput_single(url, timeout=30):
    result = {"url": url, "bytes": 0, "seconds": None, "mbps": None, "error": None}
    try:
        start = time.monotonic()
        total = 0
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                total += len(chunk)
        elapsed = time.monotonic() - start
        result["bytes"] = total
        result["seconds"] = elapsed
        result["mbps"] = (total * 8 / elapsed / 1_000_000) if elapsed > 0 else None
    except Exception as e:  # noqa: BLE001
        result["error"] = str(e)
    return result


def throughput_parallel(url, total_bytes, workers=DEFAULT_PARALLEL_WORKERS, timeout=30):
    result = {
        "url": url,
        "workers": workers,
        "bytes": 0,
        "seconds": None,
        "mbps": None,
        "per_worker_bytes": [],
        "error": None,
    }
    try:
        chunk_size = total_bytes // workers
        ranges = []
        for i in range(workers):
            start = i * chunk_size
            end = (start + chunk_size - 1) if i < workers - 1 else (total_bytes - 1)
            ranges.append((start, end))

        start_time = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_download_range, url, s, e, timeout) for s, e in ranges]
            per_worker = [f.result() for f in futures]
        elapsed = time.monotonic() - start_time

        result["per_worker_bytes"] = per_worker
        result["bytes"] = sum(per_worker)
        result["seconds"] = elapsed
        result["mbps"] = (result["bytes"] * 8 / elapsed / 1_000_000) if elapsed > 0 else None
    except Exception as e:  # noqa: BLE001
        result["error"] = str(e)
    return result


# ---------------------------------------------------------------------------
# 7. Bufferbloat (networkQuality)
# ---------------------------------------------------------------------------

def get_bufferbloat():
    result = {
        "available": False,
        "down_rpm": None,
        "up_rpm": None,
        "idle_latency_ms": None,
        "download_mbps": None,
        "upload_mbps": None,
        "raw": None,
        "error": None,
    }
    if platform.system() != "Darwin":
        result["error"] = "not macOS; networkQuality unavailable"
        return result

    out, err, rc = run(["networkQuality", "-s"], timeout=60)
    if rc == 127:
        result["error"] = "networkQuality not found (requires macOS 12+)"
        return result

    parsed = None
    if out.strip():
        try:
            parsed = json.loads(out)
        except json.JSONDecodeError:
            parsed = None

    if parsed is not None:
        result["available"] = True
        result["raw"] = parsed
        for key in ("dl_responsiveness", "download_responsiveness", "responsiveness_download"):
            if key in parsed:
                result["down_rpm"] = parsed[key]
                break
        for key in ("ul_responsiveness", "upload_responsiveness", "responsiveness_upload"):
            if key in parsed:
                result["up_rpm"] = parsed[key]
                break
        for key in ("base_rtt", "idle_latency", "start_delay"):
            if key in parsed:
                result["idle_latency_ms"] = parsed[key]
                break
        for key in ("download_throughput", "dl_throughput"):
            if key in parsed:
                result["download_mbps"] = parsed[key] / 1_000_000 if parsed[key] else None
                break
        for key in ("upload_throughput", "ul_throughput"):
            if key in parsed:
                result["upload_mbps"] = parsed[key] / 1_000_000 if parsed[key] else None
                break
        return result

    # Fall back to plain-text output (older macOS, or -s unsupported).
    out, err, rc = run(["networkQuality", "-v"], timeout=60)
    if rc == 127:
        result["error"] = "networkQuality not found (requires macOS 12+)"
        return result
    if not out.strip():
        result["error"] = err or "networkQuality returned no output"
        return result

    result["available"] = True
    result["raw"] = out

    m = re.search(r"Downlink[^\n]*?Responsiveness:\s*([\d,]+)\s*RPM", out, re.IGNORECASE)
    if m:
        result["down_rpm"] = int(m.group(1).replace(",", ""))
    m = re.search(r"Uplink[^\n]*?Responsiveness:\s*([\d,]+)\s*RPM", out, re.IGNORECASE)
    if m:
        result["up_rpm"] = int(m.group(1).replace(",", ""))
    m = re.search(r"Idle Latency:\s*([\d.]+)\s*milliseconds", out, re.IGNORECASE)
    if m:
        result["idle_latency_ms"] = float(m.group(1))
    m = re.search(r"Downlink capacity:\s*([\d.]+)\s*Mbps", out, re.IGNORECASE)
    if m:
        result["download_mbps"] = float(m.group(1))
    m = re.search(r"Uplink capacity:\s*([\d.]+)\s*Mbps", out, re.IGNORECASE)
    if m:
        result["upload_mbps"] = float(m.group(1))

    return result


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def collect(args):
    print("[1/7] Wi-Fi radio stats...", file=sys.stderr)
    wifi = get_wifi_info()

    print("[2/7] IPv6...", file=sys.stderr)
    ipv6 = get_ipv6_info()

    gateway = get_default_gateway()
    print(f"[3/7] Latency (gateway={gateway}, 1.1.1.1, 8.8.8.8)...", file=sys.stderr)
    latency = {
        "gateway": ping_host(gateway, args.ping_count) if gateway else {"error": "no default gateway found"},
        "cloudflare_1.1.1.1": ping_host(TARGETS["cloudflare"], args.ping_count),
        "google_8.8.8.8": ping_host(TARGETS["google"], args.ping_count),
    }

    print("[4/7] Traceroute (first 4 hops to 1.1.1.1)...", file=sys.stderr)
    traceroute = traceroute_first_hops(TARGETS["cloudflare"], max_hops=4)

    print("[5/7] DNS response time (router / 1.1.1.1 / 8.8.8.8)...", file=sys.stderr)
    dns = {
        "router": dns_query_time(gateway, args.dns_domain) if gateway else {"error": "no default gateway found"},
        "cloudflare_1.1.1.1": dns_query_time(TARGETS["cloudflare"], args.dns_domain),
        "google_8.8.8.8": dns_query_time(TARGETS["google"], args.dns_domain),
    }

    print("[6/7] Throughput (single-connection, then parallel)...", file=sys.stderr)
    size_bytes = args.file_size_mb * 1024 * 1024
    url = args.big_file_url.format(size=size_bytes)
    throughput = {
        "single": throughput_single(url),
        "parallel": throughput_parallel(url, size_bytes, workers=args.workers),
    }

    print("[7/7] Bufferbloat (networkQuality)...", file=sys.stderr)
    bufferbloat = get_bufferbloat()

    return {
        "label": args.label,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "hostname": socket.gethostname(),
        },
        "default_gateway": gateway,
        "wifi": wifi,
        "ipv6": ipv6,
        "latency": latency,
        "traceroute": traceroute,
        "dns": dns,
        "throughput": throughput,
        "bufferbloat": bufferbloat,
    }


def print_summary(data):
    print("\n" + "=" * 60)
    print(f"label: {data['label']}  |  timestamp: {data['timestamp']}")
    print("=" * 60)

    wifi = data["wifi"]
    if wifi.get("available"):
        print(f"[Wi-Fi] SSID={wifi.get('ssid')} SNR={wifi.get('snr_db')}dB "
              f"(signal={wifi.get('signal_dbm')}dBm / noise={wifi.get('noise_dbm')}dBm) "
              f"rate={wifi.get('tx_rate_mbps')}Mbps phy={wifi.get('phy_mode')} "
              f"channel={wifi.get('channel')}")
    else:
        print(f"[Wi-Fi] unavailable: {wifi.get('error')}")

    ipv6 = data["ipv6"]
    print(f"[IPv6] global_address={ipv6.get('global_address')} "
          f"default_route={ipv6.get('has_default_route')}")

    print("[Latency]")
    for name, r in data["latency"].items():
        if r.get("error"):
            print(f"  {name}: ERROR {r['error']}")
        else:
            print(f"  {name}: avg={r.get('avg_ms')}ms max={r.get('max_ms')}ms loss={r.get('loss_pct')}%")

    print("[Traceroute to 1.1.1.1] (first 4 hops)")
    for hop in data["traceroute"].get("hops", []):
        if hop.get("timeout"):
            print(f"  hop {hop['hop']}: * * *")
        else:
            print(f"  hop {hop['hop']}: {hop.get('ip')} rtts={hop.get('rtts_ms')}")

    print("[DNS]")
    for name, r in data["dns"].items():
        if r.get("error") and not r.get("avg_ms"):
            print(f"  {name}: ERROR {r['error']}")
        else:
            print(f"  {name}: avg={r.get('avg_ms')}ms ({r.get('successes')}/{r.get('attempts')} ok)")

    tp = data["throughput"]
    single, parallel = tp["single"], tp["parallel"]
    print(f"[Throughput] single={single.get('mbps')}Mbps "
          f"parallel(x{parallel.get('workers')})={parallel.get('mbps')}Mbps")
    if single.get("error"):
        print(f"  single error: {single['error']}")
    if parallel.get("error"):
        print(f"  parallel error: {parallel['error']}")

    bb = data["bufferbloat"]
    if bb.get("available"):
        print(f"[Bufferbloat] down_RPM={bb.get('down_rpm')} up_RPM={bb.get('up_rpm')} "
              f"idle_latency={bb.get('idle_latency_ms')}ms")
    else:
        print(f"[Bufferbloat] unavailable: {bb.get('error')}")

    print("=" * 60)
    print("Raw JSON saved for later before/after comparison; see --out-dir.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--label", required=True, help="run label, e.g. 'before' / 'after-channel-change'")
    parser.add_argument("--out-dir", default=os.path.join(os.path.dirname(__file__), "results"))
    parser.add_argument("--ping-count", type=int, default=DEFAULT_PING_COUNT)
    parser.add_argument("--dns-domain", default=DEFAULT_DNS_DOMAIN)
    parser.add_argument("--big-file-url", default=DEFAULT_BIG_FILE_URL,
                         help="URL template for throughput test; '{size}' is replaced with byte count")
    parser.add_argument("--file-size-mb", type=int, default=DEFAULT_FILE_SIZE_MB)
    parser.add_argument("--workers", type=int, default=DEFAULT_PARALLEL_WORKERS)
    args = parser.parse_args()

    if platform.system() != "Darwin":
        print(
            "warning: this tool is designed for macOS (Wi-Fi stats via system_profiler, "
            "bufferbloat via networkQuality). Those two sections will report 'unavailable' "
            "on this platform; everything else still runs.",
            file=sys.stderr,
        )

    data = collect(args)

    os.makedirs(args.out_dir, exist_ok=True)
    safe_label = re.sub(r"[^a-zA-Z0-9_.-]+", "_", args.label)
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    out_path = os.path.join(args.out_dir, f"{ts}_{safe_label}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print_summary(data)
    print(f"\nsaved: {out_path}")


if __name__ == "__main__":
    main()
