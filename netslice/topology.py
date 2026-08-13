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

    def _switch_startup(self, switch: str) -> str:
        lines = [
            "/usr/share/openvswitch/scripts/ovs-ctl start --system-id=random --no-mlockall",
            "",
            "GW=$(ip route | awk '/^default/ {print $3}')",
            "",
            "ovs-vsctl add-br br0",
            "ovs-vsctl set bridge br0 protocols=OpenFlow13",
            f"ovs-vsctl set bridge br0 other-config:datapath-id={self.dpid(switch):016x}",
            # Secure fail mode: with no controller the switch forwards nothing,
            # so a controller crash cannot silently turn the network into a hub.
            "ovs-vsctl set-fail-mode br0 secure",
            "",
        ]
        for index, link in self.interfaces(switch):
            iface = f"eth{index}"
            ofport = index + 1
            lines.append(f"ip link set {iface} up")
            lines.append(f"ovs-vsctl add-port br0 {iface} -- set Interface {iface} ofport_request={ofport}")
            lines.append(self._tc_command(iface, link.capacity_mbps))
            lines.append("")
        lines.append(f"ovs-vsctl set-controller br0 tcp:$GW:{OF_PORT}")
        return "\n".join(lines) + "\n"

    def _host_startup(self, host: str) -> str:
        ip = self.host_ip(host)
        lines = [
            "ip link set eth0 up",
            f"ip addr add {ip}/{PREFIX_LEN} dev eth0",
            self._tc_command("eth0", self.access_link(host).capacity_mbps),
            "",
            "# Static ARP for every peer: keeps ARP off the data plane entirely,",
            "# so the controller only ever handles IP flows.",
        ]
        for peer in sorted(self.hosts):
            if peer == host:
                continue
            lines.append(
                f"ip neigh replace {self.host_ip(peer)} lladdr {self.host_mac(peer)} "
                f"dev eth0 nud permanent"
            )
        return "\n".join(lines) + "\n"

    @staticmethod
    def _tc_command(iface: str, capacity_mbps: float) -> str:
        """HTB with equal rate and ceil, so the interface cannot borrow beyond
        its configured capacity. Applied per interface; because every link has
        the discipline on both ends, capacity is symmetric."""
        rate = f"{capacity_mbps:g}mbit"
        return (
            f"tc qdisc replace dev {iface} root handle 1: htb default 1 && "
            f"tc class replace dev {iface} parent 1: classid 1:1 htb "
            f"rate {rate} ceil {rate} burst 15k"
        )

    # ------------------------------------------------------------------- lab

    def build_lab(self) -> Lab:
        lab = Lab(LAB_NAME)

        for switch in self.switches:
            lab.get_or_new_machine(switch, image=IMAGE, bridged=True)
        for host in self.hosts:
            lab.get_or_new_machine(host, image=IMAGE)


        for device, entries in self._ifaces.items():
            for index, link in entries:
                mac = self.host_mac(device) if device in self.hosts else None
                lab.connect_machine_to_link(
                    device, link.domain, machine_iface_number=index, mac_address=mac
                )

        for switch in self.switches:
            lab.create_file_from_string(self._switch_startup(switch), f"{switch}.startup")
        for host in self.hosts:
            lab.create_file_from_string(self._host_startup(host), f"{host}.startup")

        return lab

    # ----------------------------------------------------------- serialisation

    def to_dict(self) -> dict:
        return {
            "switches": {s: {"dpid": self.dpid(s), "ports": self.port_to_link(s)} for s in self.switches},
            "hosts": {
                h: {
                    "switch": sw,
                    "ip": self.host_ip(h),
                    "mac": self.host_mac(h),
                    "ofport": self.ofport(sw, self.access_link(h)),
                }
                for h, sw in self.hosts.items()
            },
            "links": [
                {"id": l.id, "a": l.a, "b": l.b, "capacity_mbps": l.capacity_mbps}
                for l in self.links
            ],
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


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





def deploy(topology: Optional[Topology] = None) -> Tuple[Topology, Lab]:
    topology = topology or default_topology()
    lab = topology.build_lab()
    Kathara.get_instance().deploy_lab(lab)
    return topology, lab


def undeploy() -> None:
    Kathara.get_instance().undeploy_lab(lab_name=LAB_NAME)


def running_machines() -> List[str]:
    """Names of the lab's containers that are currently up, or [] if none are.

    Used by `status` and by the deploy guard: deploying on top of a running lab
    is not an error, but it is almost never what was meant.
    """
    try:
        containers = Kathara.get_instance().get_machines_api_objects(lab_name=LAB_NAME)
    except Exception:  # noqa: BLE001 - no lab, or no container runtime at all
        return []
    # Container names are kathara_<user>_<device>_<hash>; report the device.
    devices = set(default_topology().switches) | set(default_topology().hosts)
    return sorted(
        {device for api in containers for device in devices if f"_{device}_" in api.name}
    )


def set_link(a: str, b: str, up: bool) -> Tuple[str, int]:
    """Bring one end of a core link administratively up or down.

    Returns (switch, interface index) so the caller can say what it did.

    The end matters. Under the Docker manager a collision domain is a Linux
    bridge, not a veth pair, so downing an interface drops carrier only on that
    container's side — **only the switch named first will notify the
    controller** (`spike/FINDINGS.md` check A). One notification is enough: the
    controller knows the topology and treats it as the whole link being down.
    """
    topology = default_topology()
    lo, hi = sorted((a, b))
    link = topology.link_by_id(f"{lo}--{hi}")
    if link.a in topology.hosts or link.b in topology.hosts:
        raise ValueError(f"{link.id} is an access link; host failures are out of scope")

    index = next(i for i, candidate in topology.interfaces(a) if candidate.id == link.id)
    state = "up" if up else "down"
    Kathara.get_instance().exec(
        a, ["sh", "-c", f"ip link set eth{index} {state}"], lab_name=LAB_NAME, stream=False
    )
    return a, index


def main(argv=None) -> int:
    """Lab lifecycle and demo controls.

    The lab and the controller are separate lifetimes: the controller can run
    with no lab (it just has no switches), and the lab can run with no
    controller (secure fail mode means it forwards nothing). Both have to be up
    for anything to work, which is exactly the thing that is easy to forget.
    """
    import argparse

    parser = argparse.ArgumentParser(prog="netslice.topology", description=main.__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("deploy", help="start the containers")
    sub.add_parser("undeploy", help="remove them")
    sub.add_parser("status", help="what is running")
    sub.add_parser("json", help="print the topology and exit")
    sub.add_parser("links", help="per-switch interface map: which ethN is which link")

    for name, verb in (("link-down", "Fail"), ("link-up", "Restore")):
        p = sub.add_parser(name, help=f"{verb.lower()} a core link, for the rerouting demo")
        p.add_argument("a", help="switch whose interface is touched — the one that notifies")
        p.add_argument("b", help="switch at the other end")

    args = parser.parse_args(argv)
    topology = default_topology()

    if args.action == "json":
        print(topology.to_json())
        return 0

    if args.action == "links":
        print("OpenFlow port = interface index + 1\n")
        for switch in topology.switches:
            entries = "  ".join(
                f"eth{i}={link.id}" for i, link in topology.interfaces(switch)
            )
            print(f"  {switch}  {entries}")
        return 0

    if args.action in ("link-down", "link-up"):
        if not running_machines():
            print(f"lab '{LAB_NAME}' is not deployed")
            return 1
        try:
            switch, index = set_link(args.a, args.b, up=args.action == "link-up")
        except (KeyError, ValueError, StopIteration) as exc:
            print(f"cannot touch {args.a}--{args.b}: {exc}")
            return 1
        lo, hi = sorted((args.a, args.b))
        if args.action == "link-down":
            print(f"{lo}--{hi} down  ({switch} eth{index})")
            print(f"  only {switch} reports it — the far end never notices "
                  f"(collision domains are Linux bridges, not veth pairs)")
            print(f"  restore with:  python -m netslice.topology link-up {args.a} {args.b}")
        else:
            print(f"{lo}--{hi} up  ({switch} eth{index})")
            print("  FAILED flows are retried; flows already rerouted stay where they are")
        return 0

    if args.action == "status":
        machines = running_machines()
        if not machines:
            print(f"lab '{LAB_NAME}' is not deployed")
            print("  deploy it with:  python -m netslice.topology deploy")
            return 1
        print(f"lab '{LAB_NAME}': {len(machines)} containers up")
        print("  " + " ".join(machines))
        return 0

    if args.action == "undeploy":
        undeploy()
        print(f"lab '{LAB_NAME}' removed")
        return 0

    already = running_machines()
    if already:
        print(f"lab '{LAB_NAME}' is already deployed ({len(already)} containers).")
        print("  redeploy with:  python -m netslice.topology undeploy && "
              "python -m netslice.topology deploy")
        return 1

    deploy(topology)
    print(f"lab '{LAB_NAME}' deployed: {len(topology.switches)} switches, "
          f"{len(topology.hosts)} hosts")
    print("  switches take ~15 s to run their startup scripts and connect")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
