"""Shared plumbing for the live-demo scripts.

The demo is a graded deliverable given in front of an audience, so these
scripts are built around three rules:

1. **Start from a known state.** Every script resets the controller and the
   links before it does anything. A demo that only works if the previous one
   was run, in order, without mistakes, will fail on the day.
2. **Say what is happening and why.** Each step prints the action, the result,
   and one line on what it is demonstrating. The script should carry
   the explanation, so the presenter can talk over it rather than recite it.
3. **Leave nothing behind.** Links come back up, flows are released, iperf
   servers are killed, including when a script is interrupted half way.

Pass `--no-pause` to run one unattended (useful for a rehearsal, or for
checking everything still works before walking into the room).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from Kathara.manager.Kathara import Kathara

from netslice import topology as topo
from netslice.client import send

TOPOLOGY = topo.default_topology()

_COLOUR = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOUR else text


def bold(t): return _c("1", t)
def dim(t): return _c("2", t)
def green(t): return _c("32", t)
def yellow(t): return _c("33", t)
def red(t): return _c("31", t)
def blue(t): return _c("36", t)


# --------------------------------------------------------------- lab helpers


def sh(machine: str, command: str):
    """Run a shell command inside a lab container."""
    out, err, rc = Kathara.get_instance().exec(
        machine, ["sh", "-c", command], lab_name=topo.LAB_NAME, stream=False
    )
    decode = lambda raw: raw.decode(errors="replace") if raw else ""
    return decode(out), decode(err), rc


def iperf_server(host: str, port: int) -> None:
    sh(host, f"iperf3 -s -p {port} -D --logfile /tmp/iperf-{port}.log")


def iperf(src: str, dst: str, port: int, seconds: int = 6):
    """Measure one flow's achieved throughput in Mbps, or None if it failed."""
    iperf_server(dst, port)
    time.sleep(0.6)
    out, _, _ = sh(src, f"iperf3 -c {TOPOLOGY.host_ip(dst)} -p {port} -t {seconds} -J")
    match = re.search(r"\{.*\}", out, re.S)
    if not match:
        return None
    try:
        return round(json.loads(match.group(0))["end"]["sum_received"]["bits_per_second"] / 1e6, 2)
    except (json.JSONDecodeError, KeyError):
        return None


def iperf_parallel(specs, seconds: int = 6) -> dict:
    """Run several flows *at the same time* and return {port: Mbps}.

    Measuring them one after another would be misleading: three flows sharing a
    4 Mbps link each measure the full rate if they take turns. Contention only
    shows up when they contend.
    """
    for _, dst, port in specs:
        iperf_server(dst, port)
    time.sleep(0.8)
    for src, dst, port in specs:
        sh(src, f"rm -f /tmp/r-{port}.json; "
                f"nohup iperf3 -c {TOPOLOGY.host_ip(dst)} -p {port} -t {seconds} "
                f"-J --logfile /tmp/r-{port}.json >/dev/null 2>&1 &")
    time.sleep(seconds + 3)

    results = {}
    for src, _, port in specs:
        out, _, _ = sh(src, f"cat /tmp/r-{port}.json 2>/dev/null")
        match = re.search(r"\{.*\}", out, re.S)
        try:
            results[port] = round(
                json.loads(match.group(0))["end"]["sum_received"]["bits_per_second"] / 1e6, 2
            ) if match else None
        except (json.JSONDecodeError, KeyError, AttributeError):
            results[port] = None
    return results


def iperf_background(src: str, dst: str, port: int, seconds: int) -> None:
    """Start traffic and return immediately — for showing a flow survive a
    link failure rather than measuring it afterwards."""
    iperf_server(dst, port)
    time.sleep(0.6)
    sh(src, f"nohup iperf3 -c {TOPOLOGY.host_ip(dst)} -p {port} -t {seconds} "
            f"-i 1 --logfile /tmp/iperf-client-{port}.log >/dev/null 2>&1 &")


def kill_iperf() -> None:
    for host in TOPOLOGY.hosts:
        sh(host, "pkill iperf3 || true")





def request(**kwargs) -> dict:
    return send({"cmd": "add", **kwargs})


def flows() -> list:
    return send({"cmd": "flows"})["flows"]


def links() -> dict:
    return send({"cmd": "links"})["links"]


def residual(link_id: str) -> float:
    return links()[link_id]["residual_mbps"]


def flow_by_id(flow_id: str):
    return next((f for f in flows() if f["flow_id"] == flow_id), None)


def path_of(flow_id: str) -> str:
    flow = flow_by_id(flow_id)
    return "-".join(flow["path"]) if flow and flow["path"] else "—"


# ------------------------------------------------------------------ the demo


def reset() -> None:
    """Put the network back to a known state: no flows, every link up."""
    try:
        send({"cmd": "clear"})
    except OSError:
        pass
    for link in TOPOLOGY.core_links():
        try:
            topo.set_link(link.a, link.b, up=True)
        except Exception:
            pass
    kill_iperf()


def require_ready() -> None:
    """Fail early and legibly rather than half way through a demo."""
    machines = topo.running_machines()
    if not machines:
        raise SystemExit(
            red("  the lab is not deployed\n") +
            "    python -m netslice.topology deploy"
        )
    try:
        state = send({"cmd": "state"})
    except OSError:
        raise SystemExit(
            red("  the controller is not running\n") +
            "    python -m netslice.controller"
        )
    offline = [s for s, info in state["switches"].items() if not info["connected"]]
    if offline:
        raise SystemExit(
            red(f"  {len(offline)} switch(es) not connected: {', '.join(sorted(offline))}\n") +
            "    give them a few seconds after deploying, then try again"
        )
