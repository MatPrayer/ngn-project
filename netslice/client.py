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
    with socket.create_connection(addr, timeout=timeout) as sock:
        stream = sock.makefile("rwb")
        stream.write((json.dumps(request) + "\n").encode())
        stream.flush()
        line = stream.readline()
    if not line:
        raise ConnectionError("controller closed the connection without replying")
    return json.loads(line.decode())


def _summarise(reply: dict) -> Optional[str]:
    """A one-line human summary next to the JSON, for the live demo."""
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
    parser = argparse.ArgumentParser(prog="netslice.client", description=__doc__)
    parser.add_argument("--host", default=DEFAULT_ADDR[0])
    parser.add_argument("--port", type=int, default=DEFAULT_ADDR[1])
    sub = parser.add_subparsers(dest="cmd", required=True)

    add = sub.add_parser("add", help="request a flow allocation")
    add.add_argument("src")
    add.add_argument("dst")
    add.add_argument("bandwidth_mbps", type=float)
    add.add_argument("--priority", type=int, default=1, help="higher = more important")
    add.add_argument("--idle-timeout", type=int, default=30)
    add.add_argument("--hard-timeout", type=int, default=0)
    add.add_argument("--proto", choices=["tcp", "udp"], default="tcp")
    add.add_argument("--policy", choices=["widest", "shortest"], default="widest")
    add.add_argument("--tie-break", choices=["fewest", "best_fit"], default="fewest")
    add.add_argument("--no-preemption", action="store_true")
    add.add_argument(
        "--no-admission-control",
        action="store_true",
        help="accept regardless of network state ( baseline)",
    )

    remove = sub.add_parser("remove", help="tear a flow down")
    remove.add_argument("flow_id")

    sub.add_parser("clear", help="tear every flow down")
    sub.add_parser("flows", help="list flows")
    sub.add_parser("links", help="per-link capacity and residual")
    sub.add_parser("state", help="full snapshot: flows, links, switches")
    return parser


def main(argv=None) -> int:
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
        print(f"cannot reach controller on {args.host}:{args.port}: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(reply, indent=2))
    summary = _summarise(reply)
    if summary:
        print(f"\n{summary}", file=sys.stderr)
    return 0 if reply.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
