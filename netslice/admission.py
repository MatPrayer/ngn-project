"""admission.py - decide whether a request can be admitted, and how.

This module answers one question, *given the current state, where does this
flow go and what has to be sacrificed for it*, and answers it without touching
anything. It returns a `Decision`; applying it (reserving capacity, pushing
flow mods) is the controller's job. Keeping the decision separate from its
application is what makes the policy testable with no lab and no switches, and
it is also what makes the experiments a matter of flipping arguments rather
than of running different code.

Two knobs come straight from the evaluation plan:

* `admission_control=False` accepts every request regardless of network state
  (the "without" arm).
* `policy="shortest"` swaps the path metric (widest vs shortest).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from netslice import routing
from netslice.routing import Adjacency, Path
from netslice.state import EPS, FlowEntry, NetworkState


@dataclass(frozen=True)
class Decision:
    accepted: bool
    path: Optional[Path] = None
    victims: Tuple[str, ...] = ()
    preemption_used: bool = False
    forced: bool = False
    reason: str = ""

    def to_dict(self) -> dict:
        """Serialize the decision to a JSON-safe dictionary.

        Returns:
            dict: A dictionary with keys ``accepted``, ``path`` (or None),
            ``victims``, ``preemption_used``, ``forced``, and ``reason``.
        """
        return {
            "accepted": self.accepted,
            "path": self.path.to_dict() if self.path else None,
            "victims": list(self.victims),
            "preemption_used": self.preemption_used,
            "forced": self.forced,
            "reason": self.reason,
        }


def evaluate(
    state: NetworkState,
    adj: Adjacency,
    src_switch: str,
    dst_switch: str,
    bandwidth_mbps: float,
    priority: int,
    policy: str = "widest",
    allow_preemption: bool = True,
    tie_break: str = "fewest",
    admission_control: bool = True,
    now: Optional[float] = None,
) -> Decision:
    """Plan the placement of one flow request without mutating state.

    Searches for a path using the chosen policy, then (if preemption is
    allowed) tries to free enough capacity by evicting lower-priority flows.

    Args:
        state: Current network state (flow table and residual capacities).
            Not modified by this function.
        adj: Switch-only adjacency list built by :func:`netslice.routing.adjacency`.
        src_switch: Ingress switch name (e.g. ``"s1"``).
        dst_switch: Egress switch name (e.g. ``"s4"``).
        bandwidth_mbps: Requested bandwidth in Mbps.
        priority: Flow priority (higher = more important).
        policy: Routing policy, ``"widest"`` (max-bottleneck) or
            ``"shortest"`` (fewest-hop).
        allow_preemption: If ``True``, the search considers evicting
            lower-priority flows when free capacity is insufficient.
        tie_break: Victim-selection tie-break, ``"fewest"`` (minimise
            interrupted flows) or ``"best_fit"`` (minimise wasted
            bandwidth).
        admission_control: If ``False``, every request is accepted on
            nominal capacity regardless of reservations ( baseline).
        now: Current monotonic time (seconds). Used to check hold-down
            windows. Defaults to :func:`time.time`.

    Returns:
        Decision: Accepted or rejected, with the chosen path, any victims
        to preempt, and a human-readable reason string.
    """

    if src_switch == dst_switch:


        return Decision(True, Path((src_switch,), (), float("inf")), reason="same switch")

    if not admission_control:


        path = routing.find_path(
            policy, adj, src_switch, dst_switch,
            width=lambda link_id: 0.0 if link_id in state.down_links else state.capacity[link_id],
        )
        if path is None:
            return Decision(False, reason="no path (topology partitioned)")
        return Decision(True, path, forced=True, reason="admission control disabled")


    path = routing.find_path(
        policy, adj, src_switch, dst_switch,
        width=state.available,
        minimum=bandwidth_mbps,
    )
    if path is not None:
        return Decision(True, path, reason="admitted on free capacity")

    if not allow_preemption:
        return Decision(
            False,
            reason=f"no path with {bandwidth_mbps} Mbps of residual capacity",
        )


    path = routing.find_path(
        policy, adj, src_switch, dst_switch,
        width=lambda link_id: state.preemptable_capacity(link_id, priority, now),
        minimum=bandwidth_mbps,
    )
    if path is None:
        return Decision(
            False,
            reason=(
                f"no path with {bandwidth_mbps} Mbps even after preempting "
                f"flows below priority {priority}"
            ),
        )


    chosen: Dict[str, FlowEntry] = {}
    for link_id in path.links:



        already_freed = sum(
            v.bandwidth_mbps for v in chosen.values() if link_id in v.links
        )
        picks = state.victims(
            link_id,
            bandwidth_mbps - already_freed,
            priority,
            tie_break=tie_break,
            exclude=set(chosen),
            now=now,
        )
        if picks is None:



            return Decision(
                False,
                reason=f"victim selection failed on {link_id}",
            )
        for victim in picks:
            chosen[victim.flow_id] = victim

    return Decision(
        True,
        path,
        victims=tuple(sorted(chosen)),
        preemption_used=bool(chosen),
        reason=(
            f"admitted by preempting {len(chosen)} flow(s)"
            if chosen
            else "admitted on free capacity"
        ),
    )
