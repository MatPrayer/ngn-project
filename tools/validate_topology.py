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
    """Run a command inside a Kathara machine and return decoded output.

    Args:
        machine (str): Name of the Kathara machine.
        command (str): Shell command to execute.
        lab (Lab): The deployed Kathara lab instance.

    Returns:
        tuple[str, str, int]: Decoded stdout, decoded stderr, and return code.
    """
    stdout, stderr, rc = Kathara.get_instance().exec(
        machine, ["sh", "-c", command], lab=lab, stream=False
    )

    def text(raw):
        """Decode raw bytes to string, tolerating decode errors.

        Args:
            raw (bytes or None): Raw byte string to decode.

        Returns:
            str: Decoded text, or an empty string if raw is None.
        """
        return raw.decode(errors="replace") if raw else ""

    return text(stdout), text(stderr), rc


def parse_iperf_mbps(output):
    """Extract receiver throughput in Mbps from iperf3 JSON output.

    Args:
        output (str): Raw iperf3 ``-J`` output.

    Returns:
        float or None: Throughput in Mbps, or None if parsing fails.
    """
    match = re.search(r"\{.*\}", output, re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        return round(data["end"]["sum_received"]["bits_per_second"] / 1e6, 2)
    except (json.JSONDecodeError, KeyError):
        return None


def wait_for(predicate, timeout, poll=0.5):
    """Poll a predicate until it returns truthy or timeout.

    Args:
        predicate (callable): Zero-argument function polled each cycle.
        timeout (float): Maximum seconds to wait.
        poll (float): Seconds between polls. Defaults to 0.5.

    Returns:
        The truthy return value of predicate, or None on timeout.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(poll)
    return None


# ---------------------------------------------------------------- check 1 & 2


def check_ports_and_tc(t, lab, results):
    """Verify OpenFlow port numbering and tc HTB shaping on every interface.

    Checks that each switch's OpenFlow port numbers match expectations and
    that ``tc`` HTB classes are present at the configured rates on both
    switch and host sides of every link.

    Args:
        t (Topology): The deployed topology object.
        lab (Lab): The deployed Kathara lab instance.
        results (dict): Accumulator dict for validation results.
    """
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

    Args:
        t (Topology): The deployed topology object.
        lab (Lab): The deployed Kathara lab instance.
        src_host (str): Source host name.
        dst_host (str): Destination host name.
        switch_path (list[str]): Ordered list of switch names forming the path.

    Returns:
        list[tuple[str, int, int]]: List of (switch, in_port, out_port) hops.
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
    """Build a canonical link identifier from two endpoint names.

    Args:
        a (str): First endpoint name.
        b (str): Second endpoint name.

    Returns:
        str: Sorted ``"a--b"`` string suitable for lookup in the topology.
    """
    lo, hi = sorted((a, b))
    return f"{lo}--{hi}"


def clear_flows(t, lab):
    """Delete all flow entries from every switch in the topology.

    Args:
        t (Topology): The deployed topology object.
        lab (Lab): The deployed Kathara lab instance.
    """
    for switch in t.switches:
        sh(switch, "ovs-ofctl -O OpenFlow13 del-flows br0", lab)


def measure_path(t, lab, src_host, dst_host, switch_path, label, results):
    """Install a static path, verify connectivity, and measure throughput.

    Installs bidirectional flow entries along the given switch path, runs
    a ping check, then iperf3 to measure effective throughput against the
    path bottleneck.

    Args:
        t (Topology): The deployed topology object.
        lab (Lab): The deployed Kathara lab instance.
        src_host (str): Source host name.
        dst_host (str): Destination host name.
        switch_path (list[str]): Ordered switch names forming the path.
        label (str): Human-readable label for this measurement (e.g. ``"widest"``).
        results (dict): Accumulator dict for validation results.
    """
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
    """Measure throughput along the shortest and widest paths to verify shaping.

    Runs iperf3 along the narrow direct path and the wide multi-hop path,
    confirming each comes out near its configured bottleneck.

    Args:
        t (Topology): The deployed topology object.
        lab (Lab): The deployed Kathara lab instance.
        results (dict): Accumulator dict for validation results.
    """
    print("[*] check 3: shaping binds along real paths")
    sh("h4", "iperf3 -s -D --logfile /tmp/iperf-server.log", lab)
    time.sleep(1)
    # The two paths the widest-vs-shortest experiment will contrast.
    measure_path(t, lab, "h1", "h4", ["s1", "s4"], "shortest_narrow", results)
    measure_path(t, lab, "h1", "h4", ["s1", "s2", "s3", "s4"], "widest", results)
    clear_flows(t, lab)


# -------------------------------------------------------------------- check 4


def check_flow_removed(t, lab, results):
    """Verify OFPT_FLOW_REMOVED delivery for idle and hard timeouts.

    Starts the flowrem_probe controller, waits for both a 3-second idle
    timeout and a 6-second hard timeout removal event, then confirms the
    correct reason codes were received.

    Args:
        t (Topology): The deployed topology object.
        lab (Lab): The deployed Kathara lab instance.
        results (dict): Accumulator dict for validation results.
    """
    print("[*] check 4: OFPT_FLOW_REMOVED delivery")
    if FLOWREM_LOG.exists():
        FLOWREM_LOG.unlink()

    controller = subprocess.Popen(
        [PYTHON, str(HERE / "flowrem_probe.py")],
        stdout=(HERE / "flowrem_probe.log").open("w"),
        stderr=subprocess.STDOUT,
    )
    try:

        def events():
            """Parse the flowrem event log into a list of dicts.

            Returns:
                list[dict]: Parsed event entries, or an empty list.
            """
            if not FLOWREM_LOG.exists():
                return []
            return [json.loads(line) for line in FLOWREM_LOG.read_text().splitlines() if line.strip()]

        connected = wait_for(
            lambda: any(e["kind"] == "switch_up" and e["dpid"] == 1 for e in events()), timeout=90
        )
        results["flowrem_switch_connected"] = bool(connected)
        if not connected:
            print("    s1 never connected to probe controller")
            return

        # idle=3s and hard=6s, so 12s covers both with margin.
        wait_for(lambda: len([e for e in events() if e["kind"] == "flow_removed"]) >= 2, timeout=25)
        time.sleep(1)

        removals = [e for e in events() if e["kind"] == "flow_removed"]
        reasons = {e["cookie"]: e["reason"] for e in removals}
        results["flow_removed_events"] = removals
        results["idle_timeout_delivered"] = reasons.get(0x1001) == "IDLE_TIMEOUT"
        results["hard_timeout_delivered"] = reasons.get(0x1002) == "HARD_TIMEOUT"
        results["of_errors"] = [e for e in events() if e["kind"] == "of_error"]
        for e in removals:
            print(f"    cookie=0x{e['cookie']:x} reason={e['reason']} after {e['duration_sec']}s")
    finally:
        controller.terminate()
        try:
            controller.wait(timeout=10)
        except subprocess.TimeoutExpired:
            controller.kill()
        # Leave the switches without a controller again.
        clear_flows(t, lab)


# ----------------------------------------------------------------------- main


def report(t, results):
    """Print a formatted summary of the topology validation results.

    Args:
        t (Topology): The topology object (used for switch/host/link counts).
        results (dict): Check name to result mapping produced by ``main``.
    """
    print("\n" + "=" * 66)
    print("TOPOLOGY VALIDATION")
    print("=" * 66)
    print(f"switches {len(t.switches)}  hosts {len(t.hosts)}  "
          f"core links {len(t.core_links())}  access links {len(t.hosts)}")
    print()

    def verdict(ok):
        """Format a boolean as PASS or FAIL.

        Args:
            ok (bool): Whether the check passed.

        Returns:
            str: ``"PASS"`` if ok, ``"FAIL"`` otherwise.
        """
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
    """Run the full topology validation suite end to end.

    Deploys the topology, waits for startup scripts, then runs port/tc
    checks, shaping measurements, and flow-removed validation.

    Returns:
        tuple[Topology, dict]: The topology object and validation results.
    """
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
        check_flow_removed(t, lab, results)
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
