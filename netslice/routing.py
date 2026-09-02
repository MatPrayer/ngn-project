"""routing.py - path selection, pure graph code.

No OpenFlow, no controller state. Widest path (Dijkstra max-min) picks the
path whose bottleneck link has the highest residual capacity, breaking ties on
lowest hop count. Shortest path is here for comparison experiments. Both take
the same arguments and return the same type, so the controller can switch
between them with a string.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from netslice.topology import Topology

EPS = 1e-9
INF = float("inf")

Adjacency = Dict[str, List[Tuple[str, str]]]
WidthFn = Callable[[str], float]


@dataclass(frozen=True)
class Path:
    """One route through the core, and the capacity it can carry.

    ``bottleneck`` is the narrowest residual capacity along the route, which
    is what decides whether a request fits: a path is only as wide as its
    tightest link.

    Attributes:
        switches: Switches traversed, source first.
        links: Link IDs between them; always one shorter than *switches*.
        bottleneck: Residual capacity of the narrowest link, in Mbps.
    """

    switches: Tuple[str, ...]
    links: Tuple[str, ...]
    bottleneck: float

    @property
    def hops(self) -> int:
        """Number of hops (links) in the path."""
        return len(self.links)

    def to_dict(self) -> dict:
        """Serialize the path to a JSON-safe dictionary.

        Returns:
            dict: Keys ``switches``, ``links``, ``bottleneck_mbps`` (None if
            infinite), and ``hops``.
        """
        return {
            "switches": list(self.switches),
            "links": list(self.links),
            "bottleneck_mbps": (
                None if self.bottleneck == INF else round(self.bottleneck, 4)
            ),
            "hops": self.hops,
        }

    def __str__(self) -> str:
        """Human-readable path string, e.g. ``"s1-s2-s3"``."""
        return "-".join(self.switches)


def adjacency(topology: Topology) -> Adjacency:
    """Build a switch-only adjacency list from a topology.

    Access links (host-to-switch) are excluded: a path is a sequence of
    switches, and the host hop is implied by the ingress/egress switch.

    Args:
        topology: The network topology.

    Returns:
        Adjacency: Mapping of switch name to list of ``(neighbour, link_id)``
        tuples for core links only.
    """
    adj: Adjacency = {switch: [] for switch in topology.switches}
    for link in topology.core_links():
        adj[link.a].append((link.b, link.id))
        adj[link.b].append((link.a, link.id))
    return adj


def _reconstruct(
    prev: Dict[str, Tuple[str, str]], src: str, dst: str, bottleneck: float
) -> Path:
    """Rebuild a Path from Dijkstra's predecessor table.

    Args:
        prev: Predecessor map. Each entry ``prev[node] = (predecessor, link_id)``
            traces one step back toward the source.
        src: Source switch name.
        dst: Destination switch name.
        bottleneck: Bottleneck capacity of the reconstructed path.

    Returns:
        Path: The reconstructed path from *src* to *dst*.
    """
    switches: List[str] = [dst]
    links: List[str] = []
    node = dst
    while node != src:
        node, link_id = prev[node]
        switches.append(node)
        links.append(link_id)
    switches.reverse()
    links.reverse()
    return Path(tuple(switches), tuple(links), bottleneck)


def widest_path(
    adj: Adjacency,
    src: str,
    dst: str,
    width: WidthFn,
    minimum: float = 0.0,
) -> Optional[Path]:
    """Find the maximum-bottleneck path from *src* to *dst*.

    Dijkstra variant where the label is ``(bottleneck, hops)``, wider
    wins, then fewer hops breaks the tie. ``minimum`` prunes links that
    cannot carry the request, so any returned path is guaranteed admissible.

    Args:
        adj: Switch-only adjacency list.
        src: Source switch name.
        dst: Destination switch name.
        width: Function mapping a link ID to its usable capacity (Mbps).
            May be ``residual()``, ``preemptable_capacity()``, or
            ``capacity`` depending on the phase.
        minimum: Minimum bandwidth a link must have to be considered.

    Returns:
        Path or None: The widest path, or ``None`` if no admissible path
        exists.
    """
    if src not in adj or dst not in adj:
        return None
    if src == dst:
        return Path((src,), (), INF)

    best: Dict[str, Tuple[float, int]] = {src: (INF, 0)}
    prev: Dict[str, Tuple[str, str]] = {}
    settled: set = set()
    heap: List[Tuple[float, int, str]] = [(-INF, 0, src)]

    while heap:
        neg_bottleneck, hops, node = heapq.heappop(heap)
        if node in settled:
            continue
        settled.add(node)
        bottleneck = -neg_bottleneck
        if node == dst:
            return _reconstruct(prev, src, dst, bottleneck)

        for neighbour, link_id in adj[node]:
            if neighbour in settled:
                continue
            link_width = width(link_id)
            if link_width <= EPS or link_width + EPS < minimum:
                continue
            candidate = (min(bottleneck, link_width), hops + 1)
            current = best.get(neighbour)

            if (
                current is None
                or (candidate[0] > current[0] + EPS)
                or (abs(candidate[0] - current[0]) <= EPS and candidate[1] < current[1])
            ):
                best[neighbour] = candidate
                prev[neighbour] = (node, link_id)
                heapq.heappush(heap, (-candidate[0], candidate[1], neighbour))

    return None


def shortest_path(
    adj: Adjacency,
    src: str,
    dst: str,
    width: WidthFn,
    minimum: float = 0.0,
) -> Optional[Path]:
    """Find the fewest-hop admissible path, tie-broken on widest bottleneck.

    The baseline policy for the comparison. Still capacity-aware,
    links below *minimum* are pruned, so the experiment isolates the effect
    of the metric, not of admission control being switched off.

    Args:
        adj: Switch-only adjacency list.
        src: Source switch name.
        dst: Destination switch name.
        width: Function mapping a link ID to its usable capacity (Mbps).
        minimum: Minimum bandwidth a link must have to be considered.

    Returns:
        Path or None: The shortest admissible path, or ``None`` if none
        exists.
    """
    if src not in adj or dst not in adj:
        return None
    if src == dst:
        return Path((src,), (), INF)

    best: Dict[str, Tuple[int, float]] = {src: (0, INF)}
    prev: Dict[str, Tuple[str, str]] = {}
    settled: set = set()
    heap: List[Tuple[int, float, str]] = [(0, -INF, src)]

    while heap:
        hops, neg_bottleneck, node = heapq.heappop(heap)
        if node in settled:
            continue
        settled.add(node)
        bottleneck = -neg_bottleneck
        if node == dst:
            return _reconstruct(prev, src, dst, bottleneck)

        for neighbour, link_id in adj[node]:
            if neighbour in settled:
                continue
            link_width = width(link_id)
            if link_width <= EPS or link_width + EPS < minimum:
                continue
            candidate = (hops + 1, min(bottleneck, link_width))
            current = best.get(neighbour)
            if (
                current is None
                or candidate[0] < current[0]
                or (candidate[0] == current[0] and candidate[1] > current[1] + EPS)
            ):
                best[neighbour] = candidate
                prev[neighbour] = (node, link_id)
                heapq.heappush(heap, (candidate[0], -candidate[1], neighbour))

    return None


POLICIES = {"widest": widest_path, "shortest": shortest_path}


def find_path(
    policy: str,
    adj: Adjacency,
    src: str,
    dst: str,
    width: WidthFn,
    minimum: float = 0.0,
) -> Optional[Path]:
    """Dispatch to the routing algorithm selected by *policy*.

    Args:
        policy: ``"widest"`` for max-bottleneck, ``"shortest"`` for
            fewest-hop.
        adj: Switch-only adjacency list.
        src: Source switch name.
        dst: Destination switch name.
        width: Function mapping a link ID to its usable capacity (Mbps).
        minimum: Minimum bandwidth a link must have to be considered.

    Returns:
        Path or None: The chosen path, or ``None`` if unreachable.

    Raises:
        ValueError: If *policy* is not a known policy name.
    """
    try:
        algorithm = POLICIES[policy]
    except KeyError:
        raise ValueError(
            f"unknown policy {policy!r}, expected one of {sorted(POLICIES)}"
        )
    return algorithm(adj, src, dst, width, minimum)
