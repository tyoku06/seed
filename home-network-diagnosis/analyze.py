#!/usr/bin/env python3
"""
Turn one or two diagnose.py JSON snapshots into a bottleneck report.

Usage:
    python3 analyze.py results/20260101T120000_before.json
    python3 analyze.py results/..._before.json results/..._after.json

Every line in the report is tagged:
    [実測]   -- a number read directly from the JSON, not a guess
    [推測]   -- a conclusion inferred from the numbers, could be wrong
    [未検証] -- this tool cannot determine it; needs manual/ISP check

This mirrors the judgment order requested in the original brief:
Wi-Fi -> connection type (IPv6/PPPoE) -> bufferbloat -> route hop 2,
then a recommendation list ordered by expected effect, with "no effect"
called out explicitly rather than left ambiguous.
"""

import argparse
import json
import sys


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def g(d, *keys, default=None):
    """Safe nested get."""
    for k in keys:
        if not isinstance(d, dict) or k not in d:
            return default
        d = d[k]
    return d


def analyze_one(data):
    facts, inferences, unverified = [], [], []
    recs = []  # list of (effect: 'high'|'medium'|'low'|'none', text, reason)

    # --- 1. Wi-Fi -----------------------------------------------------
    wifi = data.get("wifi", {})
    snr = wifi.get("snr_db")
    tx_rate = wifi.get("tx_rate_mbps")
    if wifi.get("available") and snr is not None:
        facts.append(
            f"Wi-Fi: signal={wifi.get('signal_dbm')}dBm noise={wifi.get('noise_dbm')}dBm "
            f"SNR={snr}dB link_rate={tx_rate}Mbps phy={wifi.get('phy_mode')} channel={wifi.get('channel')}"
        )
        if snr < 15:
            inferences.append(f"SNR={snr}dB is very low (rule-of-thumb: <15dB is unusable/marginal Wi-Fi). "
                               "The radio link itself is a plausible primary bottleneck.")
            recs.append(("high", "ルーターの位置変更・障害物の除去・5GHz/6GHzへの変更",
                         f"SNR={snr}dBと低く、電波品質そのものが律速している可能性が高い"))
        elif snr < 25:
            inferences.append(f"SNR={snr}dB is borderline (rule-of-thumb: 15-25dB = marginal, more retransmits under load).")
            recs.append(("medium", "ルーターの位置変更・チャンネル変更",
                         f"SNR={snr}dBは境界域。改善余地はあるが主因とは限らない"))
        else:
            facts.append(f"SNR={snr}dB is comfortably in the healthy range (rule-of-thumb: >25dB).")
    else:
        unverified.append(f"Wi-Fi radio stats unavailable ({wifi.get('error')}). "
                           "If on Ethernet this section is not applicable.")

    # --- 2. IPv6 / connection type -------------------------------------
    ipv6 = data.get("ipv6", {})
    has_v6 = bool(ipv6.get("global_address")) and bool(ipv6.get("has_default_route"))
    facts.append(f"IPv6: global_address={ipv6.get('global_address')} "
                 f"default_route={ipv6.get('has_default_route')}")
    if not has_v6:
        inferences.append(
            "No IPv6 global address / default route. In Japanese FTTH access this commonly (not "
            "always) correlates with a legacy PPPoE connection rather than IPoE (MAP-E/DS-Lite), "
            "and PPPoE sessions terminate at ISP-shared NTT-East/West network-termination equipment "
            "that can congest at peak hours. This is a correlation, not a direct measurement of "
            "connection type."
        )
        unverified.append(
            "Actual WAN connection type (PPPoE vs IPoE), ISP name, and contract plan -- check the "
            "router's WAN status page and your ISP contract, or ask the ISP directly. This tool "
            "cannot see that."
        )
        recs.append(("high", "ISPにIPoE(v6プラス/OCNバーチャルコネクト等)対応の有無を確認し、対応していれば切替",
                     "IPv6が未接続。IPoE化はPPPoEの輻輳ポイントを回避できる可能性が高い(ISPが対応している場合)"))
    else:
        facts.append("IPv6 is connected end-to-end, which usually (not conclusively) rules out "
                     "legacy-PPPoE-only congestion as the bottleneck.")

    # --- 3. Bufferbloat --------------------------------------------------
    bb = data.get("bufferbloat", {})
    if bb.get("available"):
        facts.append(f"Bufferbloat: down_RPM={bb.get('down_rpm')} up_RPM={bb.get('up_rpm')} "
                     f"idle_latency={bb.get('idle_latency_ms')}ms "
                     f"down_throughput={bb.get('download_mbps')}Mbps up_throughput={bb.get('upload_mbps')}Mbps")
        down_rpm = bb.get("down_rpm")
        if isinstance(down_rpm, (int, float)) and down_rpm < 300:
            inferences.append(
                f"down_RPM={down_rpm} is on the low side (Apple's networkQuality classifies low RPM "
                "as poor responsiveness under load). This causes bad *felt* latency -- video call lag, "
                "gaming stutter -- even when raw Mbps looks fine. Exact RPM cutoffs vary by Apple "
                "documentation version, so treat this as directional, not an exact threshold."
            )
            recs.append(("medium", "ルーターのSQM/スマートキューイング機能を有効化、または対応ルーターに交換",
                         f"down_RPM={down_rpm}と低くバッファブロートの疑い。回線速度自体は変わらないが体感遅延が改善する"))
    else:
        unverified.append(f"Bufferbloat (networkQuality) unavailable: {bb.get('error')}")

    # --- 4. Route hop 2 ---------------------------------------------------
    tr = data.get("traceroute", {})
    hops = tr.get("hops", [])
    hop2 = next((h for h in hops if h.get("hop") == 2), None)
    if hop2:
        facts.append(f"Traceroute hop2: ip={hop2.get('ip')} rtts_ms={hop2.get('rtts_ms')} "
                      f"timeout={hop2.get('timeout')}")
        if hop2.get("timeout") or not hop2.get("ip"):
            unverified.append(
                "Hop 2 did not respond (common when ICMP is filtered -- not necessarily a fault). "
                "Whether the expected ISP equipment (ONU/HGW/BRAS) is actually present at this hop "
                "cannot be confirmed automatically."
            )
        else:
            unverified.append(
                f"Hop2 IP {hop2.get('ip')} was reached, but whether this is your ISP's expected "
                "access equipment (vs. an unexpected extra hop indicating a misconfigured or "
                "congested shared segment) requires comparing against your ISP's documented network "
                "topology -- not something this tool can verify."
            )
    else:
        unverified.append("Traceroute returned fewer than 2 hops or failed; hop2 check not possible.")

    # --- 5. Throughput single vs parallel -------------------------------
    tp = data.get("throughput", {})
    single = g(tp, "single", "mbps")
    parallel = g(tp, "parallel", "mbps")
    if single is not None and parallel is not None:
        facts.append(f"Throughput: single-connection={single:.1f}Mbps, "
                     f"parallel(x{g(tp, 'parallel', 'workers')})={parallel:.1f}Mbps")
        if parallel > single * 1.5:
            inferences.append(
                "Parallel throughput is much higher than single-connection throughput. This pattern "
                "is consistent with a single-flow limiter (RTT/TCP window, or per-flow shaping) rather "
                "than a hard total-bandwidth ceiling."
            )
        elif parallel <= single * 1.2:
            inferences.append(
                "Parallel throughput is not meaningfully higher than single-connection throughput. "
                "This pattern is consistent with the aggregate link/plan bandwidth itself being the "
                "ceiling, not a single-flow limitation."
            )
    else:
        unverified.append("Throughput measurement incomplete "
                           f"(single error={g(tp,'single','error')}, parallel error={g(tp,'parallel','error')}).")

    # --- No-effect calls -------------------------------------------------
    if wifi.get("available") and snr is not None and snr >= 25 and single is not None and parallel is not None:
        recs.append(("none", "ルーターの置き場所変更・チャンネル変更",
                     f"SNR={snr}dBと電波品質は既に良好。ボトルネックはWi-Fiではないため、"
                     "置き場所やチャンネルを変えても速度・体感は改善しない"))

    recs.sort(key={"high": 0, "medium": 1, "low": 2, "none": 3}.get)
    return {"facts": facts, "inferences": inferences, "unverified": unverified, "recommendations": recs}


def print_report(label, report):
    print(f"\n{'#'*70}\n# {label}\n{'#'*70}")
    print("\n[実測] tool-verified facts")
    for f in report["facts"]:
        print(f"  - {f}")
    print("\n[推測] inferences drawn from the numbers above (could be wrong)")
    for f in report["inferences"]:
        print(f"  - {f}")
    print("\n[未検証] this tool cannot determine these -- check manually")
    for f in report["unverified"]:
        print(f"  - {f}")
    print("\n対策 (効果が大きい順):")
    for effect, text, reason in report["recommendations"]:
        tag = {"high": "効果:大", "medium": "効果:中", "low": "効果:小", "none": "効果なし"}[effect]
        print(f"  [{tag}] {text}\n         理由: {reason}")


def diff_summary(before, after):
    print(f"\n{'#'*70}\n# before -> after diff\n{'#'*70}")

    def num(d, *keys):
        v = g(d, *keys)
        return v if isinstance(v, (int, float)) else None

    rows = [
        ("Wi-Fi SNR (dB)", ("wifi", "snr_db")),
        ("gateway latency avg (ms)", ("latency", "gateway", "avg_ms")),
        ("1.1.1.1 latency avg (ms)", ("latency", "cloudflare_1.1.1.1", "avg_ms")),
        ("throughput single (Mbps)", ("throughput", "single", "mbps")),
        ("throughput parallel (Mbps)", ("throughput", "parallel", "mbps")),
        ("bufferbloat down RPM", ("bufferbloat", "down_rpm")),
        ("bufferbloat idle latency (ms)", ("bufferbloat", "idle_latency_ms")),
    ]
    for label, keys in rows:
        b, a = num(before, *keys), num(after, *keys)
        if b is None or a is None:
            print(f"  {label}: before={b} after={a} (incomplete, skipping delta)")
            continue
        delta = a - b
        pct = (delta / b * 100) if b else float("inf")
        print(f"  {label}: before={b:.2f} after={a:.2f} delta={delta:+.2f} ({pct:+.1f}%)")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("results", nargs="+", help="one or two diagnose.py JSON output files")
    args = parser.parse_args()

    if len(args.results) not in (1, 2):
        print("pass exactly one or two JSON files", file=sys.stderr)
        sys.exit(1)

    datasets = [load(p) for p in args.results]
    for path, data in zip(args.results, datasets):
        report = analyze_one(data)
        print_report(f"{path}  (label={data.get('label')}, ts={data.get('timestamp')})", report)

    if len(datasets) == 2:
        diff_summary(datasets[0], datasets[1])


if __name__ == "__main__":
    main()
