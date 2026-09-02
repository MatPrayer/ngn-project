"""Shared plumbing for the live-demo scripts.

The demo is a graded deliverable given in front of an audience, so these
scripts are built around three rules:

1. **Start from a known state.** Every script resets the controller and the
   links before it does anything. A demo that only works if the previous one
   was run, in order, without mistakes, will fail on the day.
2. **Say what is happening and why.** Each step prints the action, the result,
   and the explanation. The script should carry the explanation, so the
   presenter can talk over it rather than recite it.
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
    """Wrap *text* in an ANSI colour escape sequence.

    Args:
        code (str): ANSI SGR parameter (e.g. ``'32'`` for green).
        text (str): String to wrap.

    Returns:
        str: Coloured string when stdout is a TTY, plain string otherwise.
    """
    return f"\033[{code}m{text}\033[0m" if _COLOUR else text


def bold(t):
    """Wrap *t* in ANSI bold escapes (or return it unchanged if no TTY)."""
    return _c("1", t)


def dim(t):
    """Wrap *t* in ANSI dim escapes (or return it unchanged if no TTY)."""
    return _c("2", t)


def green(t):
    """Wrap *t* in ANSI green escapes (or return it unchanged if no TTY)."""
    return _c("32", t)


def yellow(t):
    """Wrap *t* in ANSI yellow escapes (or return it unchanged if no TTY)."""
    return _c("33", t)


def red(t):
    """Wrap *t* in ANSI red escapes (or return it unchanged if no TTY)."""
    return _c("31", t)


def blue(t):
    """Wrap *t* in ANSI blue escapes (or return it unchanged if no TTY)."""
    return _c("36", t)





def sh(machine: str, command: str):
    """Run a shell command inside a Kathara lab container.

    Args:
        machine (str): Name of the container (e.g. ``'h1'``, ``'s1'``).
        command (str): Shell command to execute.

    Returns:
        tuple: ``(stdout, stderr, return_code)``, each string decoded
        with ``errors='replace'``.
    """
    out, err, rc = Kathara.get_instance().exec(
        machine, ["sh", "-c", command], lab_name=topo.LAB_NAME, stream=False
    )
    decode = lambda raw: raw.decode(errors="replace") if raw else ""
    return decode(out), decode(err), rc


def iperf_server(host: str, port: int) -> None:
    """Start a detached iperf3 server inside *host*.

    Args:
        host (str): Container name to run the server in.
        port (int): TCP port to listen on.
    """
    sh(host, f"iperf3 -s -p {port} -D --logfile /tmp/iperf-{port}.log")


def iperf(src: str, dst: str, port: int, seconds: int = 6):
    """Measure one flow's achieved throughput in Mbps, or None if it failed.

    Starts an iperf3 server on *dst*, waits briefly, then runs a single
    iperf3 client from *src* and parses the JSON report.

    Args:
        src (str): Source container name.
        dst (str): Destination container name.
        port (int): TCP port for the iperf3 session.
        seconds (int): Duration of the iperf3 test in seconds.

    Returns:
        float or None: Throughput in Mbps, rounded to two decimals,
        or ``None`` on parse failure.
    """
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
    """Run several flows *at the same time* and return ``{port: Mbps}``.

    Measuring them one after another would be misleading: three flows sharing a
    4 Mbps link each measure the full rate if they take turns. Contention only
    shows up when they contend.

    Args:
        specs: Iterable of ``(src, dst, port)`` tuples describing each flow.
        seconds (int): Duration of each iperf3 test in seconds.

    Returns:
        dict: Mapping from port number to measured throughput in Mbps
        (``None`` for flows that failed).
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
    """Start a background iperf3 client and return immediately.

    Use this to show a flow surviving a link failure in real time rather
    than measuring throughput after the fact.

    Args:
        src (str): Source container name.
        dst (str): Destination container name.
        port (int): TCP port for the iperf3 session.
        seconds (int): How long the background test should run.
    """
    iperf_server(dst, port)
    time.sleep(0.6)
    sh(src, f"nohup iperf3 -c {TOPOLOGY.host_ip(dst)} -p {port} -t {seconds} "
            f"-i 1 --logfile /tmp/iperf-client-{port}.log >/dev/null 2>&1 &")


def kill_iperf() -> None:
    """Kill every iperf3 process in all host containers."""
    for host in TOPOLOGY.hosts:
        sh(host, "pkill iperf3 || true")





def request(**kwargs) -> dict:
    """Send an ``add`` command to the controller.

    Args:
        **kwargs: Arbitrary keyword arguments forwarded as fields in the
        JSON command payload (e.g. ``src``, ``dst``, ``bandwidth_mbps``).

    Returns:
        dict: The controller's JSON reply.
    """
    return send({"cmd": "add", **kwargs})


def flows() -> list:
    """Return the current list of flows from the controller.

    Returns:
        list: Flow dictionaries (each with ``flow_id``, ``path``,
        ``bandwidth_mbps``, ``state``, etc.).
    """
    return send({"cmd": "flows"})["flows"]


def links() -> dict:
    """Return link status information from the controller.

    Returns:
        dict: Mapping from link ID to a dict containing ``used_mbps``,
        ``capacity_mbps``, ``residual_mbps``, and ``down``.
    """
    return send({"cmd": "links"})["links"]


def residual(link_id: str) -> float:
    """Return the residual (unused) capacity of a single link.

    Args:
        link_id (str): Link identifier (e.g. ``'s1--s2'``).

    Returns:
        float: Residual bandwidth in Mbps.
    """
    return links()[link_id]["residual_mbps"]


def flow_by_id(flow_id: str):
    """Look up a single flow by its ID.

    Args:
        flow_id (str): Flow identifier (e.g. ``'f3'``).

    Returns:
        dict or None: The matching flow dictionary, or ``None`` if not found.
    """
    return next((f for f in flows() if f["flow_id"] == flow_id), None)


def path_of(flow_id: str) -> str:
    """Return the dash-joined path of a flow, or an em-dash if unavailable.

    Args:
        flow_id (str): Flow identifier.

    Returns:
        str: Path string (e.g. ``'s1-s2-s3'``) or ``'-'``.
    """
    flow = flow_by_id(flow_id)
    return "-".join(flow["path"]) if flow and flow["path"] else "-"





class Demo:
    """A numbered, narrated sequence of steps."""

    def __init__(self, title: str, description: str = ""):
        """Initialise a numbered, narrated demo sequence.

        Args:
            title (str): Short title printed at the top of the demo.
            description (str): Optional one-line description shown with the title.
        """
        parser = argparse.ArgumentParser(description=f"{title}, {description}")
        parser.add_argument("--no-pause", action="store_true",
                            help="run straight through, for a rehearsal")
        parser.add_argument("--quick", action="store_true",
                            help="shorter iperf runs; less accurate, faster")
        self.args = parser.parse_args()
        self.paused = not self.args.no_pause and sys.stdin.isatty()
        self.n = 0
        self.title = title
        self._last_live = 0.0

        print()
        print(f"  {bold(title)}")
        print(dim("  " + "─" * 66))



    def step(self, text: str) -> None:
        """Print a bold numbered step header.

        Args:
            text (str): Short description of the step.
        """
        self.n += 1
        print()
        print(f"  {blue(f'[{self.n}]')} {bold(text)}")

    def say(self, text: str) -> None:
        """Print an indented narration line.

        Args:
            text (str): Text to print.
        """
        print(f"      {text}")

    def note(self, text: str) -> None:
        """Print a short, dimmed observation about the previous output.

        One short observation. The explanation is the presenter's job, the
        script prints what happened, not an essay about why it matters.

        Args:
            text (str): Observation text.
        """
        print(f"      {dim(text)}")

    def good(self, text: str) -> None:
        """Print a green success line.

        Args:
            text (str): Success message.
        """
        print(f"      {green(text)}")

    def bad(self, text: str) -> None:
        """Print a red failure line.

        Args:
            text (str): Failure message.
        """
        print(f"      {red(text)}")

    def warn(self, text: str) -> None:
        """Print a yellow warning line.

        Args:
            text (str): Warning message.
        """
        print(f"      {yellow(text)}")

    def live(self, text: str) -> None:
        """Show a self-overwriting status line during a countdown.

        On a terminal that is a carriage return. When the output is being
        captured, a rehearsal piped to a file, or CI, ``\\r`` would run every
        update together on one unreadable line, so print sparingly instead.

        Args:
            text (str): Status text to display (padded to 68 chars on a TTY).
        """
        if _COLOUR:
            print(f"      {text:<68}", end="\r", flush=True)
        else:
            now = time.monotonic()
            if now - self._last_live >= 1.8:
                self._last_live = now
                print(f"      {text}")

    def live_done(self) -> None:
        """Clear the self-overwriting live status line."""
        if _COLOUR:
            print(" " * 76, end="\r")

    def pause(self, prompt: str = "press enter") -> None:
        """Pause for user input, or sleep briefly if running non-interactively.

        Args:
            prompt (str): Text shown before the pause.
        """
        if self.paused:
            try:
                input(dim(f"\n      ── {prompt} ──"))
            except (EOFError, KeyboardInterrupt):
                raise SystemExit("\ninterrupted")
        else:
            time.sleep(0.7)



    def allocate(self, src, dst, bandwidth, **kwargs) -> dict:
        """Request a new flow from the controller and print the result.

        Args:
            src (str): Source host name.
            dst (str): Destination host name.
            bandwidth (int or float): Requested bandwidth in Mbps.
            **kwargs: Additional fields forwarded to ``request()``
            (e.g. ``priority``, ``idle_timeout``).

        Returns:
            dict: The controller's reply (includes ``ok``, ``flow``,
            ``preempted``, ``reason`` as applicable).
        """
        reply = request(src=src, dst=dst, bandwidth_mbps=bandwidth, **kwargs)
        priority = kwargs.get("priority", 1)
        if reply.get("ok"):
            flow = reply["flow"]
            line = (f"ADMITTED  {flow['flow_id']}  {src}->{dst}  {bandwidth} Mbps  "
                    f"prio {priority}  via {'-'.join(flow['path'])}")
            self.good(line)
            if reply.get("preempted"):
                self.warn(f"          preempted {', '.join(reply['preempted'])}")
        else:
            self.bad(f"REJECTED  {src}->{dst}  {bandwidth} Mbps  prio {priority}")
            self.say(dim(f"          {reply.get('reason')}"))
        return reply

    def show_links(self, link_ids=None) -> None:
        """Print a bar-chart of link utilisation for each link.

        Args:
            link_ids (iterable or None): Specific link IDs to display.
            When ``None``, all links are shown.
        """
        data = links()
        for link_id in sorted(link_ids or data):
            entry = data[link_id]
            used, capacity = entry["used_mbps"], entry["capacity_mbps"]
            bar_width = 22
            filled = 0 if capacity <= 0 else min(bar_width, round(bar_width * used / capacity))
            bar = "█" * filled + dim("·" * (bar_width - filled))
            flag = red("  DOWN") if entry["down"] else ""
            self.say(f"{link_id:<9} {bar} {used:>5.1f}/{capacity:<5.1f} Mbps"
                     f"  residual {entry['residual_mbps']:>5.1f}{flag}")

    def show_flows(self, only_active: bool = True) -> None:
        """Print a table of flows with state, path, and bandwidth.

        Args:
            only_active (bool): When ``True``, only ACTIVE flows are
            shown. When ``False``, all flows are shown.
        """
        rows = [f for f in flows() if not only_active or f["state"] == "ACTIVE"]
        if not rows:
            self.say(dim("(no flows)"))
            return
        for f in rows:
            state = {"ACTIVE": green, "FAILED": red}.get(f["state"], yellow)(f["state"])
            self.say(f"{f['flow_id']:<4} {f['src']}->{f['dst']:<3} "
                     f"{f['bandwidth_mbps']:>5.1f} Mbps  prio {f['priority']}  "
                     f"{'-'.join(f['path']) or '-':<14} {state}")

    def done(self, message: str = "") -> None:
        """Print a closing separator and optional final message.

        Args:
            message (str): Optional message to display below the separator.
        """
        print()
        print(dim("  " + "─" * 66))
        if message:
            print(f"  {bold(message)}")
        print()





def reset() -> None:
    """Put the network back to a known state: no flows, every link up.

    Clears all flows via the controller, brings every core link back up,
    and kills stray iperf3 processes. Safe to call even if the lab is
    partially broken, errors are silently ignored.
    """
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
    """Fail early and legibly rather than half way through a demo.

    Raises:
        SystemExit: With a coloured error message if the lab is not
        deployed, the controller is not running, or switches are offline.
    """
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
