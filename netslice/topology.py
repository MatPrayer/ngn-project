"""topology.py - topology definition and Kathara lab construction.

This module is the single source of truth for the emulated network: the
controller loads the same `Topology` object the lab was built from, so port
numbers, link capacities and addresses never drift between the two.

Everything is built through the Kathara Python API, no static lab directories.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

from Kathara.manager.Kathara import Kathara
from Kathara.model.Lab import Lab

LAB_NAME = "netslice"
IMAGE = "kathara/sdn"
OF_PORT = 6653
SUBNET = "10.0.0"
PREFIX_LEN = 24

# Capacity given to host access links. Deliberately far above any switch-to-
# switch capacity so the core links are always the binding constraint and the
# access link never shows up as a bottleneck in the widest-path computation.
ACCESS_CAPACITY_MBPS = 100.0


@dataclass(frozen=True)
class Link:
    """One collision domain connecting exactly two devices.

    Capacity is enforced with `tc` on *both* endpoints, since tc shapes egress
    only.
    """

    a: str
    b: str
    capacity_mbps: float

    @property
    def id(self) -> str:
        """Endpoint-order-independent identifier, so `residual[link_id]` is the
        same entry no matter which direction a lookup comes from."""
        lo, hi = sorted((self.a, self.b))
        return f"{lo}--{hi}"

    @property
    def domain(self) -> str:
        """Kathara collision domain name. Must be short and alphanumeric."""
        lo, hi = sorted((self.a, self.b))
        return f"{lo}{hi}"

    def other(self, node: str) -> str:
        if node == self.a:
            return self.b
        if node == self.b:
            return self.a
        raise KeyError(f"{node} is not an endpoint of {self.id}")


@dataclass
class Topology:
    switches: List[str]
    hosts: Dict[str, str]  # host name -> switch it attaches to
    links: List[Link]

    # Filled in by _assign_interfaces(): device -> list of (iface_index, link)
    _ifaces: Dict[str, List[Tuple[int, Link]]] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self._assign_interfaces()

    # ------------------------------------------------------------- addressing

    def host_index(self, host: str) -> int:
        return sorted(self.hosts).index(host) + 1

    def host_ip(self, host: str) -> str:
        return f"{SUBNET}.{self.host_index(host)}"

    def host_mac(self, host: str) -> str:
        """Deterministic MACs let every host be given a static ARP table, so no
        ARP traffic ever reaches the controller and the data plane only has to
        deal with IP flows (see FINDINGS / design notes)."""
        return f"00:00:00:00:00:{self.host_index(host):02x}"

    def dpid(self, switch: str) -> int:
        """Datapath id = switch index, so `s1` is dpid 1. Keeps controller logs
        and `ovs-ofctl` output readable."""
        return self.switches.index(switch) + 1

    def switch_by_dpid(self, dpid: int) -> str:
        return self.switches[dpid - 1]

    # ------------------------------------------------------------- interfaces

    def _assign_interfaces(self) -> None:
        """Assign each device's links to consecutive interface indexes.

        Kathara names interfaces eth<index>. We additionally pin the OpenFlow
        port number to index+1 via `ofport_request`, rather than relying on the
        order OVS happens to add ports in.
        """
        self._ifaces = {name: [] for name in self.switches}
        self._ifaces.update({name: [] for name in self.hosts})
        for link in self.links:
            for endpoint in (link.a, link.b):
                if endpoint not in self._ifaces:
                    raise KeyError(f"link {link.id} references unknown device {endpoint}")
                self._ifaces[endpoint].append((len(self._ifaces[endpoint]), link))

    def interfaces(self, device: str) -> List[Tuple[int, Link]]:
        return self._ifaces[device]

    def ofport(self, switch: str, link: Link) -> int:
        for index, candidate in self._ifaces[switch]:
            if candidate.id == link.id:
                return index + 1
        raise KeyError(f"{switch} has no interface on {link.id}")

    def port_to_link(self, switch: str) -> Dict[int, str]:
        """OpenFlow port number -> link id. This is what lets the controller
        turn an OFPT_PORT_STATUS into the set of affected flows."""
        return {index + 1: link.id for index, link in self._ifaces[switch]}

    def link_by_id(self, link_id: str) -> Link:
        for link in self.links:
            if link.id == link_id:
                return link
        raise KeyError(link_id)

    def neighbours(self, switch: str) -> Iterable[Tuple[str, Link]]:
        for _, link in self._ifaces[switch]:
            other = link.other(switch)
            if other in self.switches:
                yield other, link

    def core_links(self) -> List[Link]:
        """Switch-to-switch links: the ones that carry reservable capacity."""
        return [l for l in self.links if l.a in self.switches and l.b in self.switches]

    def access_link(self, host: str) -> Link:
        return self._ifaces[host][0][1]

    # ------------------------------------------------------------ startup gen

def default_topology() -> Topology:
    """Six-switch ring with three chords, one host per switch.

    Capacities are chosen so that the shortest path and the widest path
    disagree for several host pairs -- otherwise the widest-path-vs-shortest-
    path experiment has nothing to show.

    Worked example, h1 (on s1) -> h4 (on s4):
      shortest : s1-s4 chord, 1 hop, bottleneck  4 Mbps
      widest   : s1-s2-s3-s4, 3 hops, bottleneck 10 Mbps

                     s1 ------10------ s2
                    /  \                | \
                   /    \4              |  \20
                 10      \              10   \
                 /        \             |     \
                s6         +--- s4 ---10+      s5
                 \        /      |             /
                  \      10      +-----10-----+
                   6    /
                    \  /
                     s3
    """
    links = [
        # Ring
        Link("s1", "s2", 10),
        Link("s2", "s3", 10),
        Link("s3", "s4", 10),
        Link("s4", "s5", 10),
        Link("s5", "s6", 10),
        Link("s6", "s1", 10),
        # Chords: one deliberately narrow, one wide, one middling.
        Link("s1", "s4", 4),
        Link("s2", "s5", 20),
        Link("s3", "s6", 6),
    ]
    switches = [f"s{i}" for i in range(1, 7)]
    hosts = {f"h{i}": f"s{i}" for i in range(1, 7)}
    links += [Link(host, switch, ACCESS_CAPACITY_MBPS) for host, switch in hosts.items()]
    return Topology(switches=switches, hosts=hosts, links=links)


# ------------------------------------------------------------------ lifecycle
