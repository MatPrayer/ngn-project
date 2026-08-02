"""state.py - flows, residual capacity, and the link -> flows index.

Three structures that make up the minimal controller state:

    flows:      dict[flow_id, FlowEntry]
    link_flows: dict[link_id, set[flow_id]]
    residual:   dict[link_id, float]

They are kept in one object because they must never drift apart: every
allocation, release, expiry and preemption has to touch all three in the same
step. `reserve()` and `release()` are therefore the *only* methods that write
them, and everything else in the controller goes through those two.

This module knows nothing about OpenFlow. It is pure bookkeeping, so the
admission and routing logic on top of it can be unit tested with no lab
running.
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from netslice.topology import Topology



EPS = 1e-9




HOLD_DOWN_SEC = 5.0





TP_PORT_BASE = 5201

IPPROTO_TCP = 6
IPPROTO_UDP = 17


class FlowState(str, Enum):
    """Only ACTIVE flows hold capacity."""

    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    PREEMPTED = "PREEMPTED"
    FAILED = "FAILED"
    REMOVED = "REMOVED"


class InsufficientCapacity(Exception):
    """Raised by reserve() when a link on the path cannot take the flow.

    Callers are expected to have checked first; this is the guard that keeps a
    bug from silently overbooking a link.
    """


@dataclass
class FlowEntry:
    """One admitted request, plus where it currently sits in the network."""

    flow_id: str
    cookie: int
    src: str
    dst: str
    bandwidth_mbps: float
    priority: int
    tp_dst: int
    ip_proto: int = IPPROTO_TCP
    idle_timeout: int = 30
    hard_timeout: int = 0



    policy: str = "widest"
    tie_break: str = "fewest"

    path: Tuple[str, ...] = ()
    links: Tuple[str, ...] = ()
    state: FlowState = FlowState.ACTIVE

    created_at: float = field(default_factory=time.time)
    placed_at: float = 0.0
    hold_down_until: float = 0.0
    reroutes: int = 0

    @property
    def ingress(self) -> Optional[str]:
        return self.path[0] if self.path else None

    @property
    def egress(self) -> Optional[str]:
        return self.path[-1] if self.path else None

    def holds_capacity(self) -> bool:
        return self.state is FlowState.ACTIVE

    def in_hold_down(self, now: Optional[float] = None) -> bool:
        return (now if now is not None else time.time()) < self.hold_down_until

    def to_dict(self) -> dict:
        return {
            "flow_id": self.flow_id,
            "src": self.src,
            "dst": self.dst,
            "bandwidth_mbps": self.bandwidth_mbps,
            "priority": self.priority,
            "tp_dst": self.tp_dst,
            "proto": "tcp" if self.ip_proto == IPPROTO_TCP else "udp",
            "idle_timeout": self.idle_timeout,
            "hard_timeout": self.hard_timeout,
            "path": list(self.path),
            "links": list(self.links),
            "state": self.state.value,
            "reroutes": self.reroutes,
            "age_sec": round(time.time() - self.created_at, 1),
        }


class NetworkState:
    """Residual-capacity graph plus the flow table.

    Only *core* links (switch to switch) carry reservable capacity. Access
    links are 100 Mbps by construction, far above anything a core link can
    carry, so they can never be the binding constraint and are left out of the
    accounting entirely.
    """

    def __init__(self, topology: Topology, hold_down_sec: float = HOLD_DOWN_SEC):
        self.topology = topology
        self.hold_down_sec = hold_down_sec

        self.capacity: Dict[str, float] = {
            link.id: float(link.capacity_mbps) for link in topology.core_links()
        }
        self.residual: Dict[str, float] = dict(self.capacity)
        self.link_flows: Dict[str, Set[str]] = {link_id: set() for link_id in self.capacity}
        self.flows: Dict[str, FlowEntry] = {}




        self.down_links: Set[str] = set()

        self._counter = itertools.count(1)



    def new_flow(
        self,
        src: str,
        dst: str,
        bandwidth_mbps: float,
        priority: int,
        idle_timeout: int = 30,
        hard_timeout: int = 0,
        ip_proto: int = IPPROTO_TCP,
        policy: str = "widest",
        tie_break: str = "fewest",
    ) -> FlowEntry:
        """Mint an unplaced flow. It holds no capacity until reserve()."""
        number = next(self._counter)
        return FlowEntry(
            flow_id=f"f{number}",
            cookie=number,
            src=src,
            dst=dst,
            bandwidth_mbps=float(bandwidth_mbps),
            priority=int(priority),
            tp_dst=TP_PORT_BASE + number,
            ip_proto=ip_proto,
            idle_timeout=idle_timeout,
            hard_timeout=hard_timeout,
            policy=policy,
            tie_break=tie_break,
            state=FlowState.FAILED,
        )

    def reserve(
        self, flow: FlowEntry, path: Sequence[str], links: Sequence[str], force: bool = False
    ) -> None:
        """Charge `flow` to every link of `path`. The only place residual shrinks.

        Raises InsufficientCapacity and changes nothing if any link is short,
        so a failed reservation can never leave capacity half-consumed.

        `force` bypasses the check and lets residual go negative. That is the
        "without admission control" arm of the baseline experiment, where
        every request is accepted regardless of network state; the negative
        residual is exactly the overbooking the plots are meant to show.
        """
        if flow.holds_capacity() and flow.links:
            raise RuntimeError(f"{flow.flow_id} is already placed; release it first")

        short = [
            link_id
            for link_id in links
            if self.available(link_id) + EPS < flow.bandwidth_mbps
        ]
        if short and not force:
            raise InsufficientCapacity(
                f"{flow.flow_id} needs {flow.bandwidth_mbps} Mbps, short on {short}"
            )

        for link_id in links:
            self.residual[link_id] -= flow.bandwidth_mbps
            self.link_flows[link_id].add(flow.flow_id)

        flow.path = tuple(path)
        flow.links = tuple(links)
        flow.state = FlowState.ACTIVE
        flow.placed_at = time.time()
        self.flows[flow.flow_id] = flow

    def release(self, flow_id: str, new_state: FlowState) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
        """Give the flow's capacity back and move it out of ACTIVE.

        Returns the (path, links) it was occupying, which the caller needs in
        order to delete the right flow entries — and, for make-before-break,
        to know which switches the new path does *not* reuse.
        """
        flow = self.flows[flow_id]
        old_path, old_links = flow.path, flow.links

        if flow.holds_capacity():
            for link_id in flow.links:
                self.residual[link_id] += flow.bandwidth_mbps
                self.link_flows[link_id].discard(flow_id)


                self.residual[link_id] = min(self.residual[link_id], self.capacity[link_id])

        flow.state = new_state
        flow.path = ()
        flow.links = ()
        return old_path, old_links

    def forget(self, flow_id: str) -> None:
        """Drop a terminal flow from the table. Refuses to drop a live one."""
        flow = self.flows.get(flow_id)
        if flow is None:
            return
        if flow.holds_capacity():
            raise RuntimeError(f"{flow_id} still holds capacity")
        del self.flows[flow_id]

    def mark_hold_down(self, flow_id: str, now: Optional[float] = None) -> None:
        now = now if now is not None else time.time()
        self.flows[flow_id].hold_down_until = now + self.hold_down_sec

    # ------------------------------------------------------------- capacities

    def available(self, link_id: str) -> float:
        """Residual capacity usable *right now*: zero for a link that is down."""
        if link_id in self.down_links:
            return 0.0
        return self.residual.get(link_id, 0.0)

    def flows_on(self, link_id: str) -> List[FlowEntry]:
        return [self.flows[f] for f in self.link_flows.get(link_id, ())]
