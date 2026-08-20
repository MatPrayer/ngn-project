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
import urllib.error
import urllib.request
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


def events():
    """Parse the controller event log into a list of dictionaries.

    Returns:
        list[dict]: Parsed event entries, or an empty list if the log
            does not exist.
    """
    if not EVENT_LOG.exists():
        return []
    return [json.loads(line) for line in EVENT_LOG.read_text().splitlines() if line.strip()]


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
        return round(json.loads(match.group(0))["end"]["sum_received"]["bits_per_second"] / 1e6, 2)
    except (json.JSONDecodeError, KeyError):
        return None


def port_in_use(host, port):
    """Check whether a TCP port is currently accepting connections.

    Args:
        host (str): Hostname or IP address to probe.
        port (int): TCP port number.

    Returns:
        bool: True if the port is open and accepting connections.
    """
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

    Returns:
        bool: True if the ports are free and it is safe to proceed.
    """
    busy = [f"{host}:{port}"
            for host, port in (("127.0.0.1", 6653), ("127.0.0.1", 9000), ("127.0.0.1", 8080))
            if port_in_use(host, port)]
    if busy:
        print(f"[!] already in use: {', '.join(busy)}")
        print("[!] another controller is running. Stop it first:")
        print("      pkill -f 'netslice.controller'")
        return False
    return True


def check(results, name, ok, **detail):
    """Record a single validation result and print its verdict.

    Args:
        results (dict): Accumulator dict for all check results.
        name (str): Check identifier string.
        ok (bool): Whether the check passed.
        **detail: Arbitrary metadata attached to the result entry.

    Returns:
        bool: The ``ok`` value, for chaining.
    """
    results[name] = {"pass": bool(ok), **detail}
    print(f"    {name:<28} {'PASS' if ok else 'FAIL'}  {detail if detail else ''}")
    return ok


# --------------------------------------------------------------------- checks


def check_clean_state(results):
    """Every check below assumes an empty network, so assert it rather than
    assume it. A leftover reservation silently changes which path is widest.

    Args:
        results (dict): Accumulator dict for check results.

    Returns:
        bool: True if the controller has no pre-existing flows.
    """
    flows = send({"cmd": "flows"})["flows"]
    return check(results, "controller_state_clean", not flows,
                 pre_existing=[f["flow_id"] for f in flows])


def check_switches_connect(results):
    """Wait for all six switches to connect and record the result.

    Args:
        results (dict): Accumulator dict for check results.

    Returns:
        bool: True if all six switches connected within the timeout.
    """
    print("[*] check 1: switches connect")
    seen = wait_for(
        lambda: {e["dpid"] for e in events() if e["kind"] == "switch_up"} if
        len({e["dpid"] for e in events() if e["kind"] == "switch_up"}) == 6 else None,
        timeout=90,
    )
    check(results, "switches_connected", bool(seen), dpids=sorted(seen or []))
    return bool(seen)


def check_widest_admission(t, lab, results):
    """Verify widest-path admission picks the wide path and meter enforces 5 Mbps.

    Installs a 5 Mbps flow from h1 to h4, confirms it takes a 3-hop 10 Mbps
    path (not the 1-hop 4 Mbps chord), then runs iperf3 to verify metering.

    Args:
        t (Topology): The deployed topology object.
        lab (Lab): The deployed Kathara lab instance.
        results (dict): Accumulator dict for check results.

    Returns:
        dict or None: The flow record from the admission reply, or None if
            admission was rejected.
    """
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
    return flow


def http(path, method="GET", body=None):
    """Minimal HTTP client, stdlib only. Returns (status, parsed-or-text).

    Args:
        path (str): URL path relative to ``http://127.0.0.1:8080``.
        method (str): HTTP method. Defaults to ``"GET"``.
        body (dict, optional): Request body, JSON-encoded automatically.

    Returns:
        tuple[int, str | dict | list]: HTTP status code and the response
            body parsed as JSON, or raw text if not valid JSON.
    """
    request = urllib.request.Request(
        f"http://127.0.0.1:8080{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"} if body is not None else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read().decode()
            status = response.status
    except urllib.error.HTTPError as exc:
        raw, status = exc.read().decode(), exc.code
    except OSError as exc:
        return 0, str(exc)
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw


def check_dashboard(t, lab, results, flow):
    """The dashboard API, and the flow-counter poll that feeds it.

    The counters are the part worth testing: ``idle_timeout`` resets on traffic
    and the switch never says so, so the controller infers activity by watching
    the packet counter. If that poll is broken the UI shows a TTL that never
    counts down and a throughput permanently stuck at zero — and nothing else
    in the system would notice.

    Args:
        t (Topology): The deployed topology object.
        lab (Lab): The deployed Kathara lab instance.
        results (dict): Accumulator dict for check results.
        flow (dict): Flow record from a previous admission check.
    """
    print("[*] check 9: dashboard API")
    status, page = http("/")
    check(results, "dashboard_serves_page",
          status == 200 and isinstance(page, str) and "netslice" in page, status=status)

    status, payload = http("/api/topology")
    check(results, "dashboard_topology",
          status == 200 and len(payload.get("switches", {})) == 6
          and len(payload.get("links", [])) == 15, status=status)

    status, payload = http("/api/state")
    known = {f["flow_id"] for f in payload.get("flows", [])} if status == 200 else set()
    check(results, "dashboard_state", status == 200 and flow["flow_id"] in known,
          flows=sorted(known))

    # Traffic in the background, so the counters are moving while we sample.
    port = flow["tp_dst"]
    sh("h4", f"iperf3 -s -p {port} -D --logfile /tmp/iperf-dash.log", lab)
    time.sleep(1)
    sh("h1", f"nohup iperf3 -c {t.host_ip('h4')} -p {port} -t 20 >/tmp/dash-client.log 2>&1 &", lab)
    time.sleep(8)

    status, payload = http("/api/state")
    live = next((f for f in payload.get("flows", []) if f["flow_id"] == flow["flow_id"]), {})
    check(results, "dashboard_flow_throughput",
          isinstance(live.get("throughput_mbps"), (int, float)) and live["throughput_mbps"] > 1.0,
          throughput_mbps=live.get("throughput_mbps"), bytes=live.get("bytes"))
    check(results, "dashboard_remaining_ttl",
          live.get("remaining_idle_sec") is not None and live["remaining_idle_sec"] > 0,
          remaining_idle_sec=live.get("remaining_idle_sec"),
          idle_for_sec=live.get("idle_for_sec"))

    status, payload = http("/api/flows", "POST",
                           {"src": "h5", "dst": "h2", "bandwidth_mbps": 2, "priority": 1})
    created = payload.get("flow", {}).get("flow_id") if status == 200 else None
    check(results, "dashboard_add_flow", bool(payload.get("ok")) and bool(created),
          flow_id=created, reason=payload.get("reason"))
    if created:
        status, payload = http(f"/api/flows/{created}", "DELETE")
        check(results, "dashboard_remove_flow", bool(payload.get("ok")), status=status)

    status, payload = http("/api/flows", "POST", {"src": "h1", "nonsense": 1})
    check(results, "dashboard_rejects_bad_input",
          status == 400 and payload.get("ok") is False, status=status,
          reason=payload.get("reason"))

    status, payload = http("/api/events?since=0")
    check(results, "dashboard_events",
          status == 200 and len(payload.get("events", [])) > 0,
          count=len(payload.get("events", [])) if status == 200 else None)

    sh("h1", "pkill -f 'iperf3 -c' || true", lab)


def check_shortest_policy(results):
    """Verify shortest-path routing picks the direct 2-hop chord.

    Args:
        results (dict): Accumulator dict for check results.
    """
    print("[*] check 4: shortest-path policy")
    reply = send({"cmd": "add", "src": "h2", "dst": "h5", "bandwidth_mbps": 2,
                  "policy": "shortest", "idle_timeout": 120})
    ok = reply.get("ok") and reply["flow"]["path"] == ["s2", "s5"]
    check(results, "shortest_path_chosen", ok, path=reply.get("flow", {}).get("path"))
    if reply.get("ok"):
        send({"cmd": "remove", "flow_id": reply["flow"]["flow_id"]})


def check_rejection(results):
    """Verify that an impossible bandwidth request is refused with a reason.

    Args:
        results (dict): Accumulator dict for check results.
    """
    print("[*] check 5: impossible request refused")
    reply = send({"cmd": "add", "src": "h1", "dst": "h4", "bandwidth_mbps": 50})
    check(results, "oversized_request_refused",
          reply.get("ok") is False and bool(reply.get("reason")), reason=reply.get("reason"))


def check_ttl(results):
    """Verify that an idle flow expires and its capacity is released.

    Installs a flow with a 5-second idle timeout, waits for expiry via
    the event log, then confirms residual capacity increased.

    Args:
        results (dict): Accumulator dict for check results.
    """
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
    """Verify that a high-priority request preempts lower-priority flows.

    Fills the network with priority-1 traffic, confirms a new priority-1
    request is blocked, then checks a priority-5 request succeeds by
    preempting victims.

    Args:
        results (dict): Accumulator dict for check results.
    """
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


def check_link_failure(t, lab, results):
    """Verify that a downed link triggers rerouting of the affected flow.

    Brings down one end of a core link, waits for a reroute event, then
    confirms traffic still flows on the new path.

    Args:
        t (Topology): The deployed topology object.
        lab (Lab): The deployed Kathara lab instance.
        results (dict): Accumulator dict for check results.
    """
    print("[*] check 8: link failure rerouting")
    time.sleep(1)
    reply = send({"cmd": "add", "src": "h1", "dst": "h4", "bandwidth_mbps": 3,
                  "priority": 1, "idle_timeout": 300})
    if not reply.get("ok"):
        return check(results, "reroute_on_link_down", False, reason=reply.get("reason"))
    flow_id = reply["flow"]["flow_id"]
    original = reply["flow"]["path"]

    # Down the s2 end of s2--s3, which the widest path uses.
    index = next(i for i, link in t.interfaces("s2") if link.id == "s2--s3")
    started = time.monotonic()
    sh("s2", f"ip link set eth{index} down", lab)

    rerouted = wait_for(
        lambda: next((e for e in events()
                      if e["kind"] in ("rerouted", "reroute_failed") and e["flow_id"] == flow_id),
                     None),
        timeout=30,
    )
    latency = round((rerouted["mono"] - started) * 1000, 1) if rerouted else None
    check(results, "reroute_on_link_down",
          rerouted is not None and rerouted["kind"] == "rerouted",
          old_path=original, new_path=(rerouted or {}).get("new_path"),
          latency_ms=latency)

    # And the traffic actually follows the new path.
    port = reply["flow"]["tp_dst"]
    sh("h4", f"iperf3 -s -p {port} -D --logfile /tmp/iperf-{port}.log", lab)
    time.sleep(1)
    out, _, _ = sh("h1", f"iperf3 -c {t.host_ip('h4')} -p {port} -t 5 -J", lab)
    measured = parse_iperf_mbps(out)
    check(results, "traffic_survives_reroute", measured is not None and measured > 1.0,
          measured_mbps=measured)

    sh("s2", f"ip link set eth{index} up", lab)
    time.sleep(3)


# ----------------------------------------------------------------------- main


def main():
    """Run the full controller validation suite end to end.

    Deploys the topology, starts the controller, runs all checks in order,
    then tears everything down.

    Returns:
        tuple[Topology, dict]: The topology object and a dict mapping check
            names to their result entries.
    """
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

        flow = check_widest_admission(t, lab, results)
        if flow:
            check_dashboard(t, lab, results, flow)
        check_shortest_policy(results)
        check_rejection(results)
        check_ttl(results)
        check_preemption(results)
        check_link_failure(t, lab, results)
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
    """Print a formatted summary of all validation results.

    Args:
        results (dict): Check name to result mapping produced by ``main``.
    """
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
