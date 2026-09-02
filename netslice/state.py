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
        """First switch on the path (where the flow enters the core)."""
        return self.path[0] if self.path else None

    @property
    def egress(self) -> Optional[str]:
        """Last switch on the path (where the flow leaves the core)."""
        return self.path[-1] if self.path else None

    def holds_capacity(self) -> bool:
        """Check whether this flow currently occupies link capacity.

        Returns:
            bool: ``True`` if the flow is in the :attr:`ACTIVE` state.
        """
        return self.state is FlowState.ACTIVE

    def in_hold_down(self, now: Optional[float] = None) -> bool:
        """Check whether this flow is inside its hold-down immunity window.

        A recently rerouted flow is immune from preemption for
        :data:`HOLD_DOWN_SEC` seconds to prevent oscillation.

        Args:
            now: Current time (seconds). Defaults to :func:`time.time`.

        Returns:
            bool: ``True`` if the hold-down window has not yet expired.
        """
        return (now if now is not None else time.time()) < self.hold_down_until

    def to_dict(self) -> dict:
        """Serialize the flow entry to a JSON-safe dictionary.

        Returns:
            dict: All flow fields including ``flow_id``, ``src``, ``dst``,
            ``bandwidth_mbps``, ``priority``, ``path``, ``links``,
            ``state``, ``reroutes``, and ``age_sec``.
        """
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
    links are built at 100 Mbps, far above any core link, so they can never be
    the binding constraint on a path and are left out of the accounting
    entirely.
    """

    def __init__(self, topology: Topology, hold_down_sec: float = HOLD_DOWN_SEC):
        """Initialise the residual-capacity graph and flow table.

        Args:
            topology: Network topology defining switches, hosts, and links.
            hold_down_sec: Seconds a rerouted flow is immune from
                preemption. Defaults to :data:`HOLD_DOWN_SEC` (5.0).
        """
        self.topology = topology
        self.hold_down_sec = hold_down_sec

        self.capacity: Dict[str, float] = {
            link.id: float(link.capacity_mbps) for link in topology.core_links()
        }
        self.residual: Dict[str, float] = dict(self.capacity)
        self.link_flows: Dict[str, Set[str]] = {
            link_id: set() for link_id in self.capacity
        }
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
        """Mint a flow entry that has not been placed yet.

        The entry holds no capacity until :meth:`reserve` charges it to a path.

        Args:
            src: Source host name (e.g. ``"h1"``).
            dst: Destination host name (e.g. ``"h4"``).
            bandwidth_mbps: Requested bandwidth in Mbps.
            priority: Flow priority (higher = more important).
            idle_timeout: Seconds of inactivity before the switch expires
                the flow entry. Defaults to 30.
            hard_timeout: Maximum lifetime in seconds. 0 means no limit.
            ip_proto: IP protocol number. Defaults to TCP (6).
            policy: Routing policy used for admission and rerouting.
            tie_break: Victim-selection tie-break strategy.

        Returns:
            FlowEntry: A new flow in :attr:`FAILED` state (unplaced).
        """
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
        self,
        flow: FlowEntry,
        path: Sequence[str],
        links: Sequence[str],
        force: bool = False,
    ) -> None:
        """Charge *flow* to every link along *path*.

        With :meth:`release`, the only place residual capacity ever changes.

        Atomically checks all links before committing. If any link is short
        (and *force* is ``False``), raises :exc:`InsufficientCapacity` and
        changes nothing.

        Args:
            flow: The flow entry to place.
            path: Ordered sequence of switch names (ingress first).
            links: Core link IDs the reservation is charged to.
            force: If ``True``, bypass the capacity check and let residual
                go negative.

        Raises:
            InsufficientCapacity: If any link lacks the required bandwidth
                and *force* is ``False``.
            RuntimeError: If the flow is already placed.
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

    def release(
        self, flow_id: str, new_state: FlowState
    ) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
        """Give the flow's capacity back and move it out of ``ACTIVE``.

        Returns the former ``(path, links)`` so the caller can delete the
        right flow entries and (for make-before-break) know which switches
        the new path does *not* reuse.

        Args:
            flow_id: Identifier of the flow to release.
            new_state: State to transition the flow into (e.g.
                ``EXPIRED``, ``PREEMPTED``, ``REMOVED``).

        Returns:
            Tuple: ``(old_path, old_links)``, the switch sequence and
            core link IDs the flow was occupying.
        """
        flow = self.flows[flow_id]
        old_path, old_links = flow.path, flow.links

        if flow.holds_capacity():
            for link_id in flow.links:
                self.residual[link_id] += flow.bandwidth_mbps
                self.link_flows[link_id].discard(flow_id)

                self.residual[link_id] = min(
                    self.residual[link_id], self.capacity[link_id]
                )

        flow.state = new_state
        flow.path = ()
        flow.links = ()
        return old_path, old_links

    def forget(self, flow_id: str) -> None:
        """Drop a terminal flow from the table.

        Args:
            flow_id: Identifier of the flow to remove.

        Raises:
            RuntimeError: If the flow still holds capacity (is ACTIVE).
        """
        flow = self.flows.get(flow_id)
        if flow is None:
            return
        if flow.holds_capacity():
            raise RuntimeError(f"{flow_id} still holds capacity")
        del self.flows[flow_id]

    def mark_hold_down(self, flow_id: str, now: Optional[float] = None) -> None:
        """Start the hold-down immunity window for a rerouted flow.

        Args:
            flow_id: Identifier of the flow to protect.
            now: Current time (seconds). Defaults to :func:`time.time`.
        """
        now = now if now is not None else time.time()
        self.flows[flow_id].hold_down_until = now + self.hold_down_sec

    def available(self, link_id: str) -> float:
        """Residual capacity usable *right now*: zero for a down link.

        Args:
            link_id: Core link identifier (e.g. ``"s1--s2"``).

        Returns:
            float: Available bandwidth in Mbps.
        """
        if link_id in self.down_links:
            return 0.0
        return self.residual.get(link_id, 0.0)

    def flows_on(self, link_id: str) -> List[FlowEntry]:
        """Return the flows currently charged to a link.

        Args:
            link_id: Core link identifier.

        Returns:
            list[FlowEntry]: All flow entries whose reservation includes
            this link.
        """
        return [self.flows[f] for f in self.link_flows.get(link_id, ())]

    def preemptable_capacity(
        self, link_id: str, priority: int, now: Optional[float] = None
    ) -> float:
        """Compute the total capacity that could be freed on a link.

        Equals ``residual(l) + Σ bw(f)`` for flows *f* on *link_id* with
        ``priority(f) < priority`` and *f* not in hold-down.

        Args:
            link_id: Core link identifier.
            priority: Threshold priority, only flows strictly below this
                count as preemptable.
            now: Current time (seconds). Defaults to :func:`time.time`.

        Returns:
            float: Total reclaimable bandwidth in Mbps.
        """
        if link_id in self.down_links:
            return 0.0
        reclaimable = sum(
            f.bandwidth_mbps
            for f in self.flows_on(link_id)
            if f.priority < priority and not f.in_hold_down(now)
        )
        return self.residual.get(link_id, 0.0) + reclaimable

    def victims(
        self,
        link_id: str,
        bandwidth_mbps: float,
        priority: int,
        tie_break: str = "fewest",
        exclude: Optional[Set[str]] = None,
        now: Optional[float] = None,
    ) -> Optional[List[FlowEntry]]:
        """Select which flows to preempt on a link to cover a bandwidth deficit.

        Phase 2 of the preemption algorithm. Returns the list of
        flows whose eviction frees enough capacity, or ``None`` if the
        deficit cannot be covered.

        Two orderings:
            - ``"fewest"``: ascending priority, then descending bandwidth,
              minimise the number of interrupted flows.
            - ``"best_fit"``: ascending priority, then smallest single flow
              that covers the deficit, minimise wasted bandwidth.

        Args:
            link_id: Core link identifier.
            bandwidth_mbps: Required bandwidth on this link.
            priority: Threshold priority, only flows strictly below this
                are candidates.
            tie_break: Victim-selection strategy (``"fewest"`` or
                ``"best_fit"``).
            exclude: Flow IDs already chosen on earlier links (to avoid
                double-counting).
            now: Current time (seconds). Defaults to :func:`time.time`.

        Returns:
            list[FlowEntry] or None: The chosen victims, or ``None`` if the
            deficit is unsatisfiable.
        """
        exclude = exclude or set()
        deficit = bandwidth_mbps - self.available(link_id)
        if deficit <= EPS:
            return []

        candidates = [
            f
            for f in self.flows_on(link_id)
            if f.priority < priority
            and f.flow_id not in exclude
            and not f.in_hold_down(now)
        ]

        if tie_break == "best_fit":
            sufficient = [f for f in candidates if f.bandwidth_mbps + EPS >= deficit]
            if sufficient:
                best = min(sufficient, key=lambda f: (f.priority, f.bandwidth_mbps))
                return [best]
            candidates.sort(key=lambda f: (f.priority, f.bandwidth_mbps))
        else:
            candidates.sort(key=lambda f: (f.priority, -f.bandwidth_mbps))

        chosen: List[FlowEntry] = []
        freed = 0.0
        for flow in candidates:
            if freed + EPS >= deficit:
                break
            chosen.append(flow)
            freed += flow.bandwidth_mbps
        if freed + EPS < deficit:
            return None
        return chosen

    def set_link_down(self, link_id: str) -> List[FlowEntry]:
        """Mark a link unusable and return the flows crossing it, worst first.

        Flows are ordered by descending priority then descending bandwidth
        so important flows get first claim on the remaining capacity during
        rerouting.

        Args:
            link_id: Core link identifier.

        Returns:
            list[FlowEntry]: Affected flows, highest priority first.
        """
        self.down_links.add(link_id)
        affected = self.flows_on(link_id)
        affected.sort(key=lambda f: (-f.priority, -f.bandwidth_mbps))
        return affected

    def set_link_up(self, link_id: str) -> None:
        """Restore a link to usable status.

        Args:
            link_id: Core link identifier.
        """
        self.down_links.discard(link_id)

    def active_flows(self) -> List[FlowEntry]:
        """Return all flows that are currently holding capacity.

        Returns:
            list[FlowEntry]: Flows in the :attr:`ACTIVE` state.
        """
        return [f for f in self.flows.values() if f.holds_capacity()]

    def utilisation(self) -> Dict[str, dict]:
        """Report per-link capacity, residual, usage, and membership.

        Returns:
            dict: Mapping of link ID to a dict with keys
            ``capacity_mbps``, ``residual_mbps``, ``used_mbps``, ``down``,
            and ``flows`` (sorted flow IDs).
        """
        return {
            link_id: {
                "capacity_mbps": capacity,
                "residual_mbps": round(self.residual[link_id], 4),
                "used_mbps": round(capacity - self.residual[link_id], 4),
                "down": link_id in self.down_links,
                "flows": sorted(self.link_flows[link_id]),
            }
            for link_id, capacity in self.capacity.items()
        }

    def snapshot(self) -> dict:
        """Return a full serializable snapshot of flows and links.

        Returns:
            dict: ``{"flows": [...], "links": {...}}`` combining
            :meth:`active_flows`-style dictionaries and
            :meth:`utilisation`.
        """
        return {
            "flows": [f.to_dict() for f in self.flows.values()],
            "links": self.utilisation(),
        }
