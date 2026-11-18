"""
tools.py
--------
Deterministic 4-Layer Network Snapshot & Pre/Post Comparison Engine + AI Read-Only Tools.

Design Principle:
Python deterministically captures operational state and computes exact configuration/routing
deltas. Llama 3.2 audits those exact deltas against the Authorized MOP Scope and can invoke
targeted read-only tools if deeper inspection is needed.
"""

import difflib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from napalm import get_network_driver
from guardrails import run_readonly_cli
from inventory import IP_TO_DEVICE, ROUTERS, USE_TELNET

SNAPSHOT_DIR = Path("reports/snapshots")


def _clean_ios_config(raw_cfg: str) -> str:
    """Strip volatile IOS header lines (byte count, timestamps) so clean diffs stay silent."""
    ignore_prefixes = (
        "Building configuration...",
        "Current configuration :",
        "! Last configuration change",
        "! NVRAM config last updated",
    )
    lines = [
        line for line in raw_cfg.splitlines()
        if not line.strip().startswith(ignore_prefixes)
    ]
    return "\n".join(lines).strip()


def _parse_bgp_summary(raw_bgp: str) -> dict:
    """Extract {peer_ip: state_or_prefix_count} from 'show ip bgp summary'."""
    peers = {}
    for line in raw_bgp.splitlines():
        m = re.match(r"^\s*(\d+\.\d+\.\d+\.\d+)\s+4\s+\d+.*?\s(\S+)\s*$", line)
        if m:
            peers[m.group(1)] = m.group(2)
    return peers


def _parse_ospf_neighbors(raw_ospf: str) -> dict:
    """Extract {neighbor_id: state} from 'show ip ospf neighbor'."""
    nbrs = {}
    for line in raw_ospf.splitlines():
        m = re.match(r"^\s*(\d+\.\d+\.\d+\.\d+)\s+\d+\s+(\S+)", line)
        if m:
            nbrs[m.group(1)] = m.group(2)
    return nbrs


def _run_ping_probe(router: str, target_ip: str, source_ip: str, size: int, df_bit: bool) -> dict:
    """Run a single ICMP probe and return structured pass/fail telemetry."""
    cmd = f"ping {target_ip} source {source_ip} size {size}"
    if df_bit:
        cmd += " df-bit"
    cmd += " repeat 3"

    raw = run_readonly_cli(router, cmd, read_timeout=20)
    rate_match = re.search(r"Success rate is (\d+) percent", raw)
    pct = int(rate_match.group(1)) if rate_match else 0

    if pct == 100:
        detail = "100% Pass"
    elif "M" in raw:
        detail = f"{pct}% Fail (MTU Exceeded / DF-bit 'M')"
    else:
        detail = f"{pct}% Fail (Timeout / Dropped)"

    return {"command": cmd, "success_percent": pct, "detail": detail}


def _check_bgp_prefix_valid(router: str, prefix: str) -> dict:
    """Check whether a critical BGP prefix is valid/best (*>) and reachable."""
    raw = run_readonly_cli(router, f"show ip bgp {prefix}")
    valid_best = "*>" in raw
    inaccessible = "inaccessible" in raw.lower()
    not_in_table = "network not in table" in raw.lower()

    if not_in_table:
        status = "MISSING (Not in BGP table)"
    elif inaccessible or not valid_best:
        status = "INVALID (Next-hop inaccessible)"
    else:
        status = "Valid Best Path (*>)"

    return {"valid": valid_best and not inaccessible, "status": status, "raw": raw.strip()[:400]}


def capture_full_snapshot(label: str) -> dict:
    """
    Capture a comprehensive 4-layer baseline across R1, R2, and R3:
    1. Configuration Layer (cleaned running-config)
    2. Control Plane (OSPF adjacencies, BGP peers, critical route validity)
    3. Interface Layer (R2 Fa2/0 MTU & flags via NAPALM/IOS)
    4. Data Plane (100B reachability, 1500B DF-bit MTU, return-path ping)
    """
    snapshot = {
        "label": label,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "routers": {},
        "routes": {},
        "data_plane": {},
    }

    for r_name in ["R1", "R2", "R3"]:
        raw_cfg = run_readonly_cli(r_name, "show running-config")
        raw_bgp = run_readonly_cli(r_name, "show ip bgp summary")
        raw_ospf = run_readonly_cli(r_name, "show ip ospf neighbor") if r_name in ["R2", "R3"] else ""

        snapshot["routers"][r_name] = {
            "config": _clean_ios_config(raw_cfg),
            "bgp_peers": _parse_bgp_summary(raw_bgp),
            "ospf_neighbors": _parse_ospf_neighbors(raw_ospf) if raw_ospf else {},
        }

    # Verify critical end-to-end BGP prefixes
    snapshot["routes"]["R1_to_DC_172_16_30_0"] = _check_bgp_prefix_valid("R1", "172.16.30.0")
    snapshot["routes"]["R3_to_HQ_192_168_10_0"] = _check_bgp_prefix_valid("R3", "192.168.10.0")

    # Inspect core transit interface flags on R2 Fa2/0
    r2_fa2_cfg = run_readonly_cli("R2", "show running-config interface FastEthernet2/0")
    mtu_match = re.search(r"ip mtu (\d+)", r2_fa2_cfg, re.IGNORECASE)
    snapshot["r2_fa2_0_mtu"] = int(mtu_match.group(1)) if mtu_match else 1500
    snapshot["r2_fa2_0_unreachables"] = "no ip unreachables" not in r2_fa2_cfg.lower()

    # Data-plane synthetic probes
    snapshot["data_plane"]["HQ_to_DC_100B"] = _run_ping_probe("R1", "172.16.30.1", "192.168.10.1", 100, False)
    snapshot["data_plane"]["HQ_to_DC_1500B_DF"] = _run_ping_probe("R1", "172.16.30.1", "192.168.10.1", 1500, True)
    snapshot["data_plane"]["DC_to_HQ_100B"] = _run_ping_probe("R3", "192.168.10.1", "172.16.30.1", 100, False)

    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    with open(SNAPSHOT_DIR / f"{label}.json", "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2)

    return snapshot


def load_snapshot(label: str) -> dict | None:
    path = SNAPSHOT_DIR / f"{label}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def compute_pre_post_comparison(pre: dict, post: dict) -> dict:
    """
    Compare PRE and POST snapshots deterministically.
    Returns a UI-ready matrix table, unified config diffs, and regression flags.
    """
    matrix = []
    config_diffs = {}
    regressions = []
    changed_routers = []

    # 1. Configuration Unified Diffs
    for r in ["R1", "R2", "R3"]:
        pre_lines = pre["routers"][r]["config"].splitlines(keepends=True)
        post_lines = post["routers"][r]["config"].splitlines(keepends=True)
        diff = list(difflib.unified_diff(pre_lines, post_lines, fromfile=f"{r} (PRE)", tofile=f"{r} (POST)", n=2))
        if diff:
            changed_routers.append(r)
            config_diffs[r] = "".join(diff)

    # 2. OSPF Adjacency Comparisons
    for r in ["R2", "R3"]:
        pre_ospf = pre["routers"][r]["ospf_neighbors"]
        post_ospf = post["routers"][r]["ospf_neighbors"]
        all_nbrs = sorted(set(pre_ospf) | set(post_ospf))
        for nbr in all_nbrs:
            v_pre = pre_ospf.get(nbr, "MISSING")
            v_post = post_ospf.get(nbr, "MISSING")
            if "FULL" in v_pre and "FULL" not in v_post:
                status = "REGRESSION"
                regressions.append(f"{r} OSPF neighbor {nbr} degraded ({v_pre} -> {v_post})")
            else:
                status = "PASS"
            matrix.append({"category": "OSPF", "check": f"{r} Neighbor {nbr}", "pre": v_pre, "post": v_post, "status": status})

    # 3. BGP Peering Comparisons
    for r in ["R1", "R2", "R3"]:
        pre_bgp = pre["routers"][r]["bgp_peers"]
        post_bgp = post["routers"][r]["bgp_peers"]
        for peer in sorted(set(pre_bgp) | set(post_bgp)):
            v_pre = pre_bgp.get(peer, "MISSING")
            v_post = post_bgp.get(peer, "MISSING")
            if v_pre.isdigit() and (not v_post.isdigit() or int(v_post) < int(v_pre)):
                status = "REGRESSION"
                regressions.append(f"{r} BGP peer {peer} prefixes dropped ({v_pre} -> {v_post})")
            elif v_pre != v_post and v_post.isdigit() and v_pre.isdigit() and int(v_post) > int(v_pre):
                status = "IMPROVED"
            else:
                status = "PASS"
            matrix.append({"category": "BGP Peer", "check": f"{r} Peer {peer} (Pfx/State)", "pre": v_pre, "post": v_post, "status": status})

    # 4. Critical Route Validity
    for key, label in [
        ("R1_to_DC_172_16_30_0", "R1 BGP Route -> 172.16.30.0/24"),
        ("R3_to_HQ_192_168_10_0", "R3 BGP Route -> 192.168.10.0/24"),
    ]:
        pre_r = pre["routes"][key]
        post_r = post["routes"][key]
        if pre_r["valid"] and not post_r["valid"]:
            status = "REGRESSION"
            regressions.append(f"{label} failed: {post_r['status']}")
        elif not pre_r["valid"] and post_r["valid"]:
            status = "IMPROVED"
        else:
            status = "PASS"
        matrix.append({"category": "Routing", "check": label, "pre": pre_r["status"], "post": post_r["status"], "status": status})

    # 5. Core Transit MTU Policy
    pre_mtu = f"MTU={pre['r2_fa2_0_mtu']}, Unreachables={pre['r2_fa2_0_unreachables']}"
    post_mtu = f"MTU={post['r2_fa2_0_mtu']}, Unreachables={post['r2_fa2_0_unreachables']}"
    if post["r2_fa2_0_mtu"] < 1500 or not post["r2_fa2_0_unreachables"]:
        mtu_status = "REGRESSION"
        regressions.append(f"R2 Fa2/0 MTU/ICMP policy degraded ({post_mtu})")
    elif pre_mtu != post_mtu:
        mtu_status = "CHANGED"
    else:
        mtu_status = "PASS"
    matrix.append({"category": "Interface", "check": "R2 Fa2/0 MTU & ICMP Policy", "pre": pre_mtu, "post": post_mtu, "status": mtu_status})

    # 6. End-to-End Data Plane Matrix
    for key, label in [
        ("HQ_to_DC_100B", "Ping HQ -> DC (100B)"),
        ("HQ_to_DC_1500B_DF", "Ping HQ -> DC (1500B DF-bit)"),
        ("DC_to_HQ_100B", "Ping DC -> HQ (100B)"),
    ]:
        p_pre = pre["data_plane"][key]
        p_post = post["data_plane"][key]
        if p_pre["success_percent"] == 100 and p_post["success_percent"] < 100:
            status = "REGRESSION"
            regressions.append(f"Data plane failure on {label}: {p_post['detail']}")
        elif p_pre["success_percent"] < 100 and p_post["success_percent"] == 100:
            status = "IMPROVED"
        elif p_post["success_percent"] < 100:
            status = "REGRESSION"
            regressions.append(f"Data plane unhealthy on {label}: {p_post['detail']}")
        else:
            status = "PASS"
        matrix.append({"category": "Data Plane", "check": label, "pre": p_pre["detail"], "post": p_post["detail"], "status": status})

    return {
        "changed_routers": changed_routers,
        "config_diffs": config_diffs,
        "matrix": matrix,
        "regressions": regressions,
        "has_regression": len(regressions) > 0,
    }


# --- Read-Only Tools Exposed to Llama 3.2 During Post-Audit ---

def inspect_interface_config(router: str, interface: str) -> str:
    """
    AI Read-Only Tool: Inspect running-config and NAPALM/IOS status of a specific interface.

    Args:
        router: Router name ('R1', 'R2', or 'R3').
        interface: Interface name (e.g. 'FastEthernet2/0', 'Loopback2').
    """
    show_run = run_readonly_cli(router, f"show running-config interface {interface}")
    return json.dumps({"router": router.upper(), "interface": interface, "running_config": show_run.strip()}, indent=2)


def check_bgp_table_prefix(router: str, prefix: str) -> str:
    """
    AI Read-Only Tool: Query BGP table entry on a router for a specific prefix.

    Args:
        router: Router name ('R1', 'R2', or 'R3').
        prefix: Network prefix (e.g. '172.16.40.0' or '192.168.10.0').
    """
    res = _check_bgp_prefix_valid(router.upper(), prefix)
    return json.dumps({"router": router.upper(), "prefix": prefix, **res}, indent=2)


def get_napalm_bgp_summary(router: str) -> str:
    """
    AI Read-Only Tool: Fetch structured BGP neighbor state using NAPALM.

    Args:
        router: Router name ('R1', 'R2', or 'R3').
    """
    router_key = router.upper()
    try:
        target = ROUTERS[router_key]
        driver = get_network_driver(target["driver"])
        device = driver(
            hostname=target["hostname"],
            username=target["username"],
            password=target["password"],
            optional_args={
                "port": target["port"],
                "secret": target["secret"],
                "transport": "telnet" if USE_TELNET else "ssh",
                "global_delay_factor": 1.5,
            },
        )
        device.open()
        bgp = device.get_bgp_neighbors()
        device.close()
        return json.dumps({"router": router_key, "bgp_neighbors": bgp}, indent=2)
    except Exception as exc:
        raw = run_readonly_cli(router_key, "show ip bgp summary")
        return json.dumps({"router": router_key, "fallback_summary": raw.strip(), "note": str(exc)}, indent=2)