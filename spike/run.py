"""Drive the validation spike end to end and print a verdict for each check.

    python spike/run.py

Starts the probe controller, deploys the lab, runs check B (meters) and then
check A (port status), tears everything down and reports.
"""

import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import spike_lab  # noqa: E402
from Kathara.manager.Kathara import Kathara  # noqa: E402

HERE = Path(__file__).resolve().parent
EVENT_LOG = HERE / "spike_events.jsonl"
CONTROLLER_LOG = HERE / "spike_controller.log"
PYTHON = str(HERE.parent / ".venv" / "bin" / "python")


def events():
    if not EVENT_LOG.exists():
        return []
    out = []
    for line in EVENT_LOG.read_text().splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def sh(machine, command, lab, timeout=60):
    """Run a command inside a Kathara machine, return (stdout, rc)."""
    stdout, stderr, rc = Kathara.get_instance().exec(
        machine, ["sh", "-c", command], lab=lab, stream=False
    )

    def text(raw):
        return raw.decode(errors="replace") if raw else ""

    return text(stdout), text(stderr), rc


def wait_for(predicate, timeout, poll=0.3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(poll)
    return None


def main():
    results = {}
    controller = subprocess.Popen(
        [PYTHON, str(HERE / "spike_controller.py")],
        stdout=CONTROLLER_LOG.open("w"),
        stderr=subprocess.STDOUT,
    )
    lab = None
    try:
        time.sleep(2)
        print("[*] deploying lab ...")
        # A previous crashed run can leave containers behind, and deploy_lab
        # refuses to start when a device of the same name already exists.
        try:
            spike_lab.undeploy()
        except Exception:  # noqa: BLE001
            pass
        lab = spike_lab.deploy()

        print("[*] waiting for both switches to connect ...")
        connected = wait_for(
            lambda: {e["dpid"] for e in events() if e["kind"] == "switch_up"} >= {1, 2},
            timeout=90,
        )
        results["switches_connected"] = bool(connected)
        if not connected:
            print("[!] switches never connected - aborting")
            return results

        # Give the controller time to push forwarding + meter rules.
        wait_for(
            lambda: {e["dpid"] for e in events() if e["kind"] == "forwarding_installed"} >= {1, 2},
            timeout=30,
        )
        time.sleep(2)

        # ---------------------------------------------------------- check B
        print("[*] check B: OF1.3 meters")
        feats = [e for e in events() if e["kind"] == "meter_features"]
        results["meter_features"] = feats
        results["meters_advertised"] = any(f.get("max_meter") for f in feats)

        out, _, _ = sh("s1", "ovs-ofctl -O OpenFlow13 dump-meters br0", lab)
        results["dump_meters"] = out.strip()
        results["meter_present_in_ovs"] = "meter=1" in out

        print("[*]   connectivity check h1 -> h2")
        out, _, rc = sh("h1", "ping -c 3 -W 2 10.0.0.2", lab)
        results["ping_ok"] = rc == 0
        if rc != 0:
            print("[!]   no connectivity, iperf3 measurement will be skipped")
        else:
            sh("h2", "iperf3 -s -D --logfile /tmp/iperf-server.log", lab)
            time.sleep(1)
            print("[*]   iperf3 with meter (expect ~%d Mbps)" % (spike_controller_rate() / 1000))
            out, _, _ = sh("h1", "iperf3 -c 10.0.0.2 -t 5 -J", lab, timeout=90)
            results["iperf_metered_mbps"] = parse_iperf_mbps(out)

            print("[*]   removing metered flow, re-measuring unshaped")
            sh("s1", 'ovs-ofctl -O OpenFlow13 del-flows br0 "ip,nw_src=10.0.0.1,nw_dst=10.0.0.2"', lab)
            time.sleep(1)
            out, _, _ = sh("h1", "iperf3 -c 10.0.0.2 -t 5 -J", lab, timeout=90)
            results["iperf_unshaped_mbps"] = parse_iperf_mbps(out)

        # ---------------------------------------------------------- check A
        print("[*] check A: OFPT_PORT_STATUS on link down")
        before = len(events())
        t0 = time.monotonic()
        sh("s1", "ip link set eth1 down", lab)

        wait_for(lambda: len(events()) > before, timeout=15)
        time.sleep(3)  # let the far end report too, if it ever does

        notifications = [
            e for e in events()[before:] if e["kind"] == "port_status" and e.get("link_down")
        ]
        results["port_status_events"] = notifications
        results["local_end_notified"] = any(e["dpid"] == 1 for e in notifications)
        results["remote_end_notified"] = any(e["dpid"] == 2 for e in notifications)
        if notifications:
            results["detection_latency_ms"] = round(
                (min(e["mono"] for e in notifications) - t0) * 1000, 2
            )
        return results
    finally:
        print("[*] tearing down ...")
        # Unconditional: deploy_lab can fail partway and still leave devices up.
        try:
            spike_lab.undeploy()
        except Exception as exc:  # noqa: BLE001
            print(f"[!] undeploy failed: {exc}")
        controller.terminate()
        try:
            controller.wait(timeout=10)
        except subprocess.TimeoutExpired:
            controller.kill()


def spike_controller_rate():
    import spike_controller

    return spike_controller.METER_RATE_KBPS


def parse_iperf_mbps(output):
    """Pull receiver throughput out of iperf3 JSON, tolerating junk around it."""
    match = re.search(r"\{.*\}", output, re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        return round(data["end"]["sum_received"]["bits_per_second"] / 1e6, 2)
    except (json.JSONDecodeError, KeyError):
        return None


def report(r):
    print("\n" + "=" * 62)
    print("SPIKE RESULTS")
    print("=" * 62)

    def verdict(ok):
        return "PASS" if ok else "FAIL"

    print(f"switches connected to controller : {verdict(r.get('switches_connected'))}")
    print(f"connectivity h1 -> h2            : {verdict(r.get('ping_ok'))}")
    print()
    print("-- check B: bandwidth enforcement via OF1.3 meters --")
    print(f"meters advertised by datapath    : {verdict(r.get('meters_advertised'))}")
    print(f"meter present in OVS             : {verdict(r.get('meter_present_in_ovs'))}")
    metered = r.get("iperf_metered_mbps")
    unshaped = r.get("iperf_unshaped_mbps")
    print(f"iperf3 with 5 Mbps meter         : {metered} Mbps")
    print(f"iperf3 without meter             : {unshaped} Mbps")
    if metered and unshaped:
        enforced = metered < 8 and unshaped > metered * 1.5
        print(f"rate actually enforced           : {verdict(enforced)}")
    print()
    print("-- check A: link-failure detection --")
    print(f"local end  (s1) notified         : {verdict(r.get('local_end_notified'))}")
    print(f"remote end (s2) notified         : {verdict(r.get('remote_end_notified'))}")
    if "detection_latency_ms" in r:
        print(f"detection latency                : {r['detection_latency_ms']} ms")
    for e in r.get("port_status_events", []):
        print(f"    dpid={e['dpid']} port={e['port']} {e['name']} reason={e['reason']}")
    print("=" * 62)


if __name__ == "__main__":
    res = main()
    report(res)
    (HERE / "spike_results.json").write_text(json.dumps(res, indent=2, default=str))
    print(f"\nraw results -> {HERE / 'spike_results.json'}")
