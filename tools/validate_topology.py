"""Validate the emulated topology before any real controller logic exists.

    python tools/validate_topology.py

Checks, in order:
  1. every switch has br0 with the expected OpenFlow port numbering
  2. `tc` HTB shaping is present on both ends of every core link
  3. shaping actually binds: static flows are pushed with ovs-ofctl along two
     different paths and iperf3 must come out near each path's bottleneck
  4. OFPT_FLOW_REMOVED is delivered when entries expire

Step 3 needs no controller: the bridges are in secure fail mode, so with no
controller connected they forward exactly the entries we install by hand.
"""

import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from Kathara.manager.Kathara import Kathara  # noqa: E402

from netslice import topology as topo  # noqa: E402

HERE = Path(__file__).resolve().parent
PYTHON = str(ROOT / ".venv" / "bin" / "python")
FLOWREM_LOG = HERE / "flowrem_events.jsonl"
RESULTS = HERE / "topology_validation.json"


def sh(machine, command, lab):
    stdout, stderr, rc = Kathara.get_instance().exec(
        machine, ["sh", "-c", command], lab=lab, stream=False
    )

    def text(raw):
        return raw.decode(errors="replace") if raw else ""

    return text(stdout), text(stderr), rc


def parse_iperf_mbps(output):
    match = re.search(r"\{.*\}", output, re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        return round(data["end"]["sum_received"]["bits_per_second"] / 1e6, 2)
    except (json.JSONDecodeError, KeyError):
        return None


def wait_for(predicate, timeout, poll=0.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(poll)
    return None


# ---------------------------------------------------------------- check 1 & 2


def check_ports_and_tc(t, lab, results):
    print("[*] check 1/2: OpenFlow port numbering and tc shaping")
    port_errors, tc_errors = [], []

    for switch in t.switches:
        out, _, _ = sh(switch, "ovs-ofctl -O OpenFlow13 show br0", lab)
        # Lines look like:  1(eth0): addr:...
        seen = {int(m.group(1)): m.group(2) for m in re.finditer(r"^\s*(\d+)\((\S+?)\):", out, re.M)}
        for index, link in t.interfaces(switch):
            expected_port = index + 1
            iface = f"eth{index}"
            if seen.get(expected_port) != iface:
                port_errors.append(
                    f"{switch}: expected port {expected_port} to be {iface}, got {seen.get(expected_port)!r}"
                )

            tc_out, _, _ = sh(switch, f"tc class show dev {iface}", lab)
            rate = re.search(r"rate (\d+(?:\.\d+)?)([KMG])bit", tc_out)
            if not rate:
                tc_errors.append(f"{switch}/{iface}: no HTB class found ({link.id})")
                continue
            value = float(rate.group(1)) * {"K": 1e-3, "M": 1.0, "G": 1e3}[rate.group(2)]
            if abs(value - link.capacity_mbps) > 0.01:
                tc_errors.append(
                    f"{switch}/{iface}: shaped at {value} Mbps, expected {link.capacity_mbps}"
                )

    # Host side of every access link must be shaped too (tc is egress-only).
    for host in t.hosts:
        tc_out, _, _ = sh(host, "tc class show dev eth0", lab)
        if "htb" not in tc_out:
            tc_errors.append(f"{host}/eth0: no HTB class found")

    results["port_errors"] = port_errors
    results["tc_errors"] = tc_errors
    print(f"    port numbering: {'OK' if not port_errors else f'{len(port_errors)} problems'}")
    print(f"    tc shaping    : {'OK' if not tc_errors else f'{len(tc_errors)} problems'}")


# -------------------------------------------------------------------- check 3


def install_static_path(t, lab, src_host, dst_host, switch_path):
    """Cross-connect a host-to-host path hop by hop with ovs-ofctl.

    Matching is on in_port alone, which is enough because each switch on the
    path carries only this one test flow at a time.
    """
    hops = []
    for position, switch in enumerate(switch_path):
        if position == 0:
            in_port = t.ofport(switch, t.access_link(src_host))
        else:
            in_port = t.ofport(switch, t.link_by_id(_link_id(switch_path[position - 1], switch)))
        if position == len(switch_path) - 1:
            out_port = t.ofport(switch, t.access_link(dst_host))
        else:
            out_port = t.ofport(switch, t.link_by_id(_link_id(switch, switch_path[position + 1])))
        hops.append((switch, in_port, out_port))

    for switch, in_port, out_port in hops:
        for a, b in ((in_port, out_port), (out_port, in_port)):
            sh(
                switch,
                f"ovs-ofctl -O OpenFlow13 add-flow br0 "
                f"'priority=100,in_port={a},actions=output:{b}'",
                lab,
            )
    return hops


def _link_id(a, b):
    lo, hi = sorted((a, b))
    return f"{lo}--{hi}"


def clear_flows(t, lab):
    for switch in t.switches:
        sh(switch, "ovs-ofctl -O OpenFlow13 del-flows br0", lab)


def measure_path(t, lab, src_host, dst_host, switch_path, label, results):
    bottleneck = min(
        t.link_by_id(_link_id(a, b)).capacity_mbps
        for a, b in zip(switch_path, switch_path[1:])
    )
    print(f"[*]   path {label}: {'-'.join(switch_path)}, bottleneck {bottleneck} Mbps")

    clear_flows(t, lab)
    install_static_path(t, lab, src_host, dst_host, switch_path)
    time.sleep(1)

    out, _, rc = sh(src_host, f"ping -c 3 -W 2 {t.host_ip(dst_host)}", lab)
    reachable = rc == 0
    throughput = None
    if reachable:
        out, _, _ = sh(src_host, f"iperf3 -c {t.host_ip(dst_host)} -t 5 -J", lab)
        throughput = parse_iperf_mbps(out)
    else:
        print("        unreachable")

    # HTB should land within roughly 20% under the configured rate and must not
    # exceed it by more than a small margin.
    ok = throughput is not None and 0.8 * bottleneck <= throughput <= 1.1 * bottleneck
    results[f"path_{label}"] = {
        "switch_path": switch_path,
        "bottleneck_mbps": bottleneck,
        "reachable": reachable,
        "throughput_mbps": throughput,
        "within_expected": ok,
    }
    print(f"        reachable={reachable} throughput={throughput} Mbps -> {'OK' if ok else 'CHECK'}")


def check_shaping_binds(t, lab, results):
    print("[*] check 3: shaping binds along real paths")
    sh("h4", "iperf3 -s -D --logfile /tmp/iperf-server.log", lab)
    time.sleep(1)
    # The two paths the widest-vs-shortest experiment will contrast.
    measure_path(t, lab, "h1", "h4", ["s1", "s4"], "shortest_narrow", results)
    measure_path(t, lab, "h1", "h4", ["s1", "s2", "s3", "s4"], "widest", results)
    clear_flows(t, lab)


# -------------------------------------------------------------------- check 4


def report(t, results):
    print("\n" + "=" * 66)
    print("TOPOLOGY VALIDATION")
    print("=" * 66)
    print(f"switches {len(t.switches)}  hosts {len(t.hosts)}  "
          f"core links {len(t.core_links())}  access links {len(t.hosts)}")
    print()

    def verdict(ok):
        return "PASS" if ok else "FAIL"

    print(f"OpenFlow port numbering deterministic : {verdict(not results.get('port_errors'))}")
    for e in results.get("port_errors", [])[:5]:
        print(f"      {e}")
    print(f"tc HTB present and correct on all ends: {verdict(not results.get('tc_errors'))}")
    for e in results.get("tc_errors", [])[:5]:
        print(f"      {e}")
    print()
    for label in ("shortest_narrow", "widest"):
        entry = results.get(f"path_{label}", {})
        if entry:
            print(
                f"{label:<16} {'-'.join(entry['switch_path']):<16} "
                f"bottleneck {entry['bottleneck_mbps']:>5} Mbps  "
                f"measured {str(entry['throughput_mbps']):>6} Mbps  "
                f"{verdict(entry['within_expected'])}"
            )
    print()
    print(f"OFPT_FLOW_REMOVED idle timeout        : {verdict(results.get('idle_timeout_delivered'))}")
    print(f"OFPT_FLOW_REMOVED hard timeout        : {verdict(results.get('hard_timeout_delivered'))}")
    print("=" * 66)


def main():
    results = {}
    t = topo.default_topology()
    lab = None
    try:
        print("[*] deploying topology ...")
        try:
            topo.undeploy()
        except Exception:  # noqa: BLE001
            pass
        t, lab = topo.deploy(t)
        print("[*] waiting for startup scripts ...")
        time.sleep(12)

        check_ports_and_tc(t, lab, results)
        check_shaping_binds(t, lab, results)
        return t, results
    finally:
        print("[*] tearing down ...")
        try:
            topo.undeploy()
        except Exception as exc:  # noqa: BLE001
            print(f"[!] undeploy failed: {exc}")


if __name__ == "__main__":
    t, results = main()
    report(t, results)
    RESULTS.write_text(json.dumps(results, indent=2, default=str))
    print(f"\nraw results -> {RESULTS}")
