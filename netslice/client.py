"""client.py - command-line client for the controller's control channel.

The dashboard will eventually sit here. Until it does this is how the demo
and the experiment scripts drive the controller:

    python -m netslice.client add h1 h4 5 --priority 2
    python -m netslice.client add h2 h5 8 --policy shortest --no-preemption
    python -m netslice.client flows
    python -m netslice.client links
    python -m netslice.client remove f3
    python -m netslice.client clear

Every command prints the controller's raw JSON reply, so scripts can pipe it
straight into `jq` or `json.load`.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
from typing import Optional

DEFAULT_ADDR = ("127.0.0.1", 9000)


def send(request: dict, addr=DEFAULT_ADDR, timeout: float = 30.0) -> dict:
    """Send one JSON command to the controller and return its reply.

    Args:
        request: The command dict (must include ``"cmd"``).
        addr: ``(host, port)`` of the control channel. Defaults to
            ``DEFAULT_ADDR``.
        timeout: Socket timeout in seconds. Defaults to 30.0.

    Returns:
        dict: The controller's JSON reply.

    Raises:
        ConnectionError: If the controller closes the connection without
            replying.
        OSError: If the controller cannot be reached.
    """
    with socket.create_connection(addr, timeout=timeout) as sock:
        stream = sock.makefile("rwb")
        stream.write((json.dumps(request) + "\n").encode())
        stream.flush()
        line = stream.readline()
    if not line:
        raise ConnectionError("controller closed the connection without replying")
    return json.loads(line.decode())


def _summarise(reply: dict) -> Optional[str]:
    """Build a one-line human summary next to the JSON, for the live demo.

    Args:
        reply: The controller's reply dict.

    Returns:
        str or None: A short summary line, or ``None`` if the reply does not
        lend itself to one.
    """
    if not reply.get("ok"):
        return f"REJECTED: {reply.get('reason', 'unknown reason')}"
    flow = reply.get("flow")
    if flow:
        line = (
            f"{flow['flow_id']}: {flow['src']} -> {flow['dst']} "
            f"{flow['bandwidth_mbps']} Mbps prio {flow['priority']} "
            f"via {'-'.join(flow['path'])} (port {flow['tp_dst']})"
        )
        if reply.get("preempted"):
            line += f"  preempted {', '.join(reply['preempted'])}"
        return line
    return None


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser.

    Returns:
        argparse.ArgumentParser: The configured parser with ``add``,
        ``remove``, ``clear``, ``flows``, ``links``, and ``state``
        subcommands.
    """
    parser = argparse.ArgumentParser(
        prog="netslice.client",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Ask the controller to allocate flows, and inspect what it has "
            "allocated.\n"
            "Talks to the JSON control channel; the controller must be running."
        ),
        epilog=(
            "examples:\n"
            "  netslice-cli add h1 h4 5 --priority 2           reserve 5 Mbps, high priority\n"
            "  netslice-cli add h2 h5 8 --policy shortest      route by hop count instead\n"
            "  netslice-cli add h3 h6 9 --no-admission-control admit blindly\n"
            "  netslice-cli flows                              what is allocated now\n"
            "  netslice-cli remove f3                          release one flow\n"
            "  netslice-cli clear                              release everything\n"
            "\n"
            "Every command prints the controller's raw JSON reply, so it pipes\n"
            "straight into jq. A one-line human summary goes to stderr.\n"
            "\n"
            "Exit codes: 0 accepted, 1 the controller refused, 2 unreachable."
        ),
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_ADDR[0],
        help="control channel host (default: %(default)s)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_ADDR[1],
        help="control channel port (default: %(default)s)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True, metavar="<command>")

    add = sub.add_parser(
        "add",
        help="request a flow allocation",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description="Ask for bandwidth between two hosts. The controller picks the "
        "path, installs the rules, and reports the iperf3 pair to run.",
    )
    add.add_argument("src", help="source host, e.g. h1")
    add.add_argument("dst", help="destination host, e.g. h4")
    add.add_argument("bandwidth_mbps", type=float, help="bandwidth to reserve, in Mbps")
    add.add_argument(
        "--priority",
        type=int,
        default=1,
        help="higher wins; may preempt lower-priority flows",
    )
    add.add_argument(
        "--idle-timeout",
        type=int,
        default=30,
        help="seconds of silence before the flow expires (0 = never)",
    )
    add.add_argument(
        "--hard-timeout",
        type=int,
        default=0,
        help="seconds before the flow expires regardless of traffic " "(0 = never)",
    )
    add.add_argument(
        "--proto",
        choices=["tcp", "udp"],
        default="tcp",
        help="transport protocol to match on",
    )
    add.add_argument(
        "--policy",
        choices=["widest", "shortest"],
        default="widest",
        help="widest = most spare capacity, shortest = fewest hops",
    )
    add.add_argument(
        "--tie-break",
        choices=["fewest", "best_fit"],
        default="fewest",
        help="between equally wide paths: fewest hops, or tightest fit",
    )
    add.add_argument(
        "--no-preemption",
        action="store_true",
        help="fail rather than displace lower-priority flows",
    )
    add.add_argument(
        "--no-admission-control",
        action="store_true",
        help="accept regardless of network state, overbooking linksd",
    )

    remove = sub.add_parser(
        "remove",
        help="tear a flow down",
        description="Release one flow and free its capacity.",
    )
    remove.add_argument("flow_id", help="flow to release, e.g. f3")

    sub.add_parser(
        "clear",
        help="tear every flow down",
        description="Release every flow the controller is holding.",
    )
    sub.add_parser(
        "flows",
        help="list flows",
        description="Every flow: path, state, bandwidth, TTL, throughput.",
    )
    sub.add_parser(
        "links",
        help="per-link capacity and residual",
        description="Capacity, reserved and residual Mbps for every link.",
    )
    sub.add_parser(
        "state",
        help="full snapshot: flows, links, switches",
        description="One snapshot of everything: flows, links, switches, hosts.",
    )
    return parser


def main(argv=None) -> int:
    """CLI entry point for the client.

    Parses arguments, sends the request, and prints the raw JSON reply plus
    an optional human summary.

    Args:
        argv: Optional argument list. Defaults to ``sys.argv[1:]``.

    Returns:
        int: 0 on success, 1 if the controller refused the request, 2 if
        the controller is unreachable.
    """
    args = build_parser().parse_args(argv)
    request = {"cmd": args.cmd}

    if args.cmd == "add":
        request.update(
            src=args.src,
            dst=args.dst,
            bandwidth_mbps=args.bandwidth_mbps,
            priority=args.priority,
            idle_timeout=args.idle_timeout,
            hard_timeout=args.hard_timeout,
            proto=args.proto,
            policy=args.policy,
            tie_break=args.tie_break,
            allow_preemption=not args.no_preemption,
            admission_control=not args.no_admission_control,
        )
    elif args.cmd == "remove":
        request["flow_id"] = args.flow_id

    try:
        reply = send(request, (args.host, args.port))
    except OSError as exc:
        print(
            f"cannot reach controller on {args.host}:{args.port}: {exc}",
            file=sys.stderr,
        )
        return 2

    print(json.dumps(reply, indent=2))
    summary = _summarise(reply)
    if summary:
        print(f"\n{summary}", file=sys.stderr)
    return 0 if reply.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
