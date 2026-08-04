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
    switches: Tuple[str, ...]
    links: Tuple[str, ...]
    bottleneck: float

    @property
    def hops(self) -> int:
        return len(self.links)

    def to_dict(self) -> dict:
        return {
            "switches": list(self.switches),
            "links": list(self.links),
            "bottleneck_mbps": None if self.bottleneck == INF else round(self.bottleneck, 4),
            "hops": self.hops,
        }

    def __str__(self) -> str:
        return "-".join(self.switches)


def adjacency(topology: Topology) -> Adjacency:
    """Switch-only adjacency. Access links are excluded: a path is a sequence
    of switches, and the host hop is implied by the ingress/egress switch."""
    adj: Adjacency = {switch: [] for switch in topology.switches}
    for link in topology.core_links():
        adj[link.a].append((link.b, link.id))
        adj[link.b].append((link.a, link.id))
    return adj


def _reconstruct(prev: Dict[str, Tuple[str, str]], src: str, dst: str, bottleneck: float) -> Path:
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
    """Maximum-bottleneck path from `src` to `dst`, or None if there is none.

    `minimum` prunes links that cannot carry the request at all, so the search
    only ever returns an admissible path: a result is a guarantee that every
    link on it has at least `minimum` available.

    Labels are (bottleneck, hops) compared as "higher bottleneck wins, then
    fewer hops" — the tie-break requires. The label is monotone along a
    path (bottleneck can only shrink, hops only grow), so the usual Dijkstra
    argument holds and the first time a node is settled it is settled optimally.
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

            if current is None or (candidate[0] > current[0] + EPS) or (
                abs(candidate[0] - current[0]) <= EPS and candidate[1] < current[1]
            ):
                best[neighbour] = candidate
                prev[neighbour] = (node, link_id)
                heapq.heappush(heap, (-candidate[0], candidate[1], neighbour))

    return None
