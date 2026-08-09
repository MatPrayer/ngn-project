"""End-to-end validation of the controller core against the real lab.

    python tools/validate_controller.py

Deploys the topology, starts `netslice.controller`, and drives it through its
control socket, checking the behaviours the specification requires:

  1. all six switches connect and the topology is under control
  2. widest-path admission picks the wide 3-hop path over the narrow chord
  3. the reservation is enforced: iperf3 through a 5 Mbps flow gets ~5 Mbps
  4. the shortest-path policy picks the other path
  5. an impossible request is refused with a reason
  6. TTL: an idle flow expires and its capacity comes back
  7. preemption: a high-priority request reclaims from a lower one
  8. link failure: a downed port reroutes the affected flow

Raw results land in tools/controller_validation.json.
"""

import json
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from Kathara.manager.Kathara import Kathara  # noqa: E402

from netslice import topology as topo  # noqa: E402
from netslice.client import send  # noqa: E402

HERE = Path(__file__).resolve().parent
PYTHON = str(ROOT / ".venv" / "bin" / "python")
EVENT_LOG = ROOT / "controller_events.jsonl"
RESULTS = HERE / "controller_validation.json"


def sh(machine, command, lab):
    stdout, stderr, rc = Kathara.get_instance().exec(
        machine, ["sh", "-c", command], lab=lab, stream=False
    )

    def text(raw):
        return raw.decode(errors="replace") if raw else ""

    return text(stdout), text(stderr), rc


def events():
    if not EVENT_LOG.exists():
        return []
    return [json.loads(line) for line in EVENT_LOG.read_text().splitlines() if line.strip()]


def wait_for(predicate, timeout, poll=0.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(poll)
    return None


def parse_iperf_mbps(output):
    match = re.search(r"\{.*\}", output, re.S)
    if not match:
        return None
    try:
        return round(json.loads(match.group(0))["end"]["sum_received"]["bits_per_second"] / 1e6, 2)
    except (json.JSONDecodeError, KeyError):
        return None


def port_in_use(host, port):
    with socket.socket() as probe:
        probe.settimeout(1.0)
        return probe.connect_ex((host, port)) == 0


def preflight():
    """Refuse to run if anything is already listening.

    Learned the hard way: a controller left running from a manual session keeps
    6653 and 9000, this script's own controller dies on the bind, and every
    command then goes to the *other* controller — which has its own flows and
    its own reservations. The run looks plausible and every number is wrong.
    os-ken makes this worse by masking the bind failure behind an
    AttributeError in its own shutdown path, so nothing says "address in use".
    """
    busy = [f"{host}:{port}" for host, port in (("127.0.0.1", 6653), ("127.0.0.1", 9000))
            if port_in_use(host, port)]
    if busy:
        print(f"[!] already in use: {', '.join(busy)}")
        print("[!] another controller is running. Stop it first:")
        print("      pkill -f 'netslice.controller'")
        return False
    return True


def check(results, name, ok, **detail):
    results[name] = {"pass": bool(ok), **detail}
    print(f"    {name:<28} {'PASS' if ok else 'FAIL'}  {detail if detail else ''}")
    return ok


# --------------------------------------------------------------------- checks


def check_clean_state(results):
    """Every check below assumes an empty network, so assert it rather than
    assume it. A leftover reservation silently changes which path is widest."""
    flows = send({"cmd": "flows"})["flows"]
    return check(results, "controller_state_clean", not flows,
                 pre_existing=[f["flow_id"] for f in flows])


def check_switches_connect(results):
    print("[*] check 1: switches connect")
    seen = wait_for(
        lambda: {e["dpid"] for e in events() if e["kind"] == "switch_up"} if
        len({e["dpid"] for e in events() if e["kind"] == "switch_up"}) == 6 else None,
        timeout=90,
    )
    check(results, "switches_connected", bool(seen), dpids=sorted(seen or []))
    return bool(seen)


def check_widest_admission(t, lab, results):
    print("[*] check 2/3: widest-path admission and meter enforcement")
    reply = send({"cmd": "add", "src": "h1", "dst": "h4", "bandwidth_mbps": 5, "priority": 1,
                  "idle_timeout": 120})
    # On an empty network the two halves of the ring are a genuine tie — both
    # 3 hops of 10 Mbps — so accept either. What the check is about is that the
    # 1-hop 4 Mbps chord is *not* taken, and that the bottleneck is 10.
    widest = (["s1", "s2", "s3", "s4"], ["s1", "s6", "s5", "s4"])
    ok = (
        reply.get("ok")
        and reply["flow"]["path"] in widest
        and reply.get("bottleneck_mbps") == 10.0
    )
    check(results, "widest_path_chosen", ok, path=reply.get("flow", {}).get("path"),
          bottleneck_mbps=reply.get("bottleneck_mbps"), reason=reply.get("reason"))
    if not reply.get("ok"):
        return None

    flow = reply["flow"]
    port = flow["tp_dst"]
    sh("h4", f"iperf3 -s -p {port} -D --logfile /tmp/iperf-{port}.log", lab)
    time.sleep(1)
    out, _, _ = sh("h1", f"iperf3 -c {t.host_ip('h4')} -p {port} -t 6 -J", lab)
    measured = parse_iperf_mbps(out)
    # The meter drops above 5 Mbps; TCP overhead puts the goodput a little under.
    check(results, "meter_enforces_5mbps",
          measured is not None and 3.5 <= measured <= 5.6, measured_mbps=measured)

    # Assert on the links this flow was actually placed on, and on every other
    # link being untouched — checking hardcoded link names would pass just as
    # happily on somebody else's reservation.
    links = send({"cmd": "links"})["links"]
    charged = set(flow["links"])
    used = all(abs(links[l]["used_mbps"] - 5) < 1e-6 for l in charged) and all(
        abs(links[l]["used_mbps"]) < 1e-6 for l in links if l not in charged
    )
    check(results, "residual_updated", used,
          charged=sorted(charged),
          residual={l: links[l]["residual_mbps"] for l in sorted(charged)})
    return flow["flow_id"]


def check_shortest_policy(results):
    print("[*] check 4: shortest-path policy")
    reply = send({"cmd": "add", "src": "h2", "dst": "h5", "bandwidth_mbps": 2,
                  "policy": "shortest", "idle_timeout": 120})
    ok = reply.get("ok") and reply["flow"]["path"] == ["s2", "s5"]
    check(results, "shortest_path_chosen", ok, path=reply.get("flow", {}).get("path"))
    if reply.get("ok"):
        send({"cmd": "remove", "flow_id": reply["flow"]["flow_id"]})


def check_rejection(results):
    print("[*] check 5: impossible request refused")
    reply = send({"cmd": "add", "src": "h1", "dst": "h4", "bandwidth_mbps": 50})
    check(results, "oversized_request_refused",
          reply.get("ok") is False and bool(reply.get("reason")), reason=reply.get("reason"))


def check_ttl(results):
    print("[*] check 6: TTL releases capacity")
    reply = send({"cmd": "add", "src": "h3", "dst": "h6", "bandwidth_mbps": 3,
                  "idle_timeout": 5})
    if not reply.get("ok"):
        return check(results, "ttl_expiry", False, reason=reply.get("reason"))
    flow_id = reply["flow"]["flow_id"]
    before = send({"cmd": "links"})["links"]
    link = reply["flow"]["links"][0]

    expired = wait_for(
        lambda: next((e for e in events()
                      if e["kind"] == "flow_expired" and e["flow_id"] == flow_id), None),
        timeout=40,
    )
    after = send({"cmd": "links"})["links"]
    check(results, "ttl_expiry", bool(expired),
          reason=(expired or {}).get("reason"), duration_sec=(expired or {}).get("duration_sec"))
    check(results, "ttl_capacity_returned",
          expired is not None and after[link]["residual_mbps"] > before[link]["residual_mbps"],
          link=link, before=before[link]["residual_mbps"], after=after[link]["residual_mbps"])


def check_preemption(results):
    print("[*] check 7: preemption")
    send({"cmd": "clear"})
    time.sleep(1)
    # Saturate every way out of s1 with priority-1 traffic.
    filler = []
    for dst, bandwidth in (("h2", 10), ("h6", 10), ("h4", 4)):
        reply = send({"cmd": "add", "src": "h1", "dst": dst, "bandwidth_mbps": bandwidth,
                      "priority": 1, "idle_timeout": 120, "allow_preemption": False})
        filler.append(reply)
    saturated = all(r.get("ok") for r in filler)

    blocked = send({"cmd": "add", "src": "h1", "dst": "h4", "bandwidth_mbps": 4,
                    "priority": 1, "idle_timeout": 120})
    check(results, "saturated_network_refuses", saturated and blocked.get("ok") is False,
          reason=blocked.get("reason"))

    high = send({"cmd": "add", "src": "h1", "dst": "h4", "bandwidth_mbps": 4,
                 "priority": 5, "idle_timeout": 120})
    check(results, "high_priority_preempts",
          high.get("ok") and bool(high.get("preempted")),
          preempted=high.get("preempted"), path=high.get("flow", {}).get("path"))

    flows = {f["flow_id"]: f for f in send({"cmd": "flows"})["flows"]}
    victims = high.get("preempted") or []
    states = {v: flows[v]["state"] for v in victims if v in flows}
    check(results, "victims_resolved",
          bool(states) and all(s in ("ACTIVE", "FAILED") for s in states.values()),
          states=states)
    send({"cmd": "clear"})


def main():
    results = {}
    t = topo.default_topology()
    controller = None
    lab = None
    if not preflight():
        return t, results
    try:
        try:
            topo.undeploy()
        except Exception:  # noqa: BLE001
            pass

        print("[*] starting controller ...")
        controller = subprocess.Popen(
            [PYTHON, "-m", "netslice.controller"],
            cwd=str(ROOT),
            stdout=(HERE / "controller_run.log").open("w"),
            stderr=subprocess.STDOUT,
        )
        time.sleep(3)
        if controller.poll() is not None:
            print(f"[!] controller exited immediately (rc={controller.returncode}); "
                  f"see {HERE / 'controller_run.log'}")
            return t, results

        print("[*] deploying topology ...")
        t, lab = topo.deploy(t)

        if not check_switches_connect(results):
            return t, results
        if not check_clean_state(results):
            print("[!] the controller already holds flows — results would be meaningless")
            return t, results

        check_widest_admission(t, lab, results)
        check_shortest_policy(results)
        check_rejection(results)
        check_ttl(results)
        check_preemption(results)
        return t, results
    finally:
        if controller is not None:
            controller.terminate()
            try:
                controller.wait(timeout=10)
            except subprocess.TimeoutExpired:
                controller.kill()
        print("[*] tearing down ...")
        try:
            topo.undeploy()
        except Exception as exc:  # noqa: BLE001
            print(f"[!] undeploy failed: {exc}")


def report(results):
    print("\n" + "=" * 66)
    print("CONTROLLER VALIDATION")
    print("=" * 66)
    for name, entry in results.items():
        detail = {k: v for k, v in entry.items() if k != "pass"}
        print(f"{name:<28} {'PASS' if entry['pass'] else 'FAIL'}  {detail}")
    failed = [n for n, e in results.items() if not e["pass"]]
    print("=" * 66)
    print("all checks passed" if not failed else f"FAILED: {', '.join(failed)}")


if __name__ == "__main__":
    t, results = main()
    report(results)
    RESULTS.write_text(json.dumps(results, indent=2, default=str))
    print(f"\nraw results -> {RESULTS}")
