"""topology.py - topology definition and Kathara lab construction.

This module is the single source of truth for the emulated network: the
controller loads the same `Topology` object the lab was built from, so port
numbers, link capacities and addresses never drift between the two.

Everything is built through the Kathara Python API, no static lab directories.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from Kathara.manager.Kathara import Kathara
from Kathara.model.Lab import Lab

LAB_NAME = "netslice"
IMAGE = "kathara/sdn"


OF_PORT = 6653
SUBNET = "10.0.0"
PREFIX_LEN = 24

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"



RUN_DIR = ROOT / ".run"
CONTROLLER_PID = RUN_DIR / "controller.pid"
CONTROLLER_LOG = RUN_DIR / "controller.log"


def _script(name: str) -> str:
    """Return the contents of a shell script under ``scripts/``.

    Args:
        name: Script file name (e.g. ``"switch_startup.sh"``).

    Returns:
        str: The scripts content, with a trailing newline.
    """
    return (SCRIPTS / name).read_text() + "\n"




ACCESS_CAPACITY_MBPS = 100.0



_CONTROLLER_HOST: Optional[str] = None
_CONTROLLER_HOST_RESOLVED = False


def controller_host() -> Optional[str]:
    """Return the address a switch container must use to reach the controller.

    The controller runs on the host, outside the lab. On plain Linux Docker the
    container's default gateway *is* the host, so the startup script can work
    it out by itself and this returns ``None``. On Docker Desktop (macOS and
    Windows) that gateway is docker0 inside the LinuxKit VM and never reaches
    the host, so an explicit address is needed.

    Docker's own ``host-gateway`` keyword resolves to the right address on
    every platform, but Kathara rewrites ``/etc/hosts`` in every lab machine
    and wipes the mapping Docker Desktop injects. The address is therefore
    resolved once here, in a throwaway container, and baked into the generated
    startup scripts as a literal.

    Returns:
        str | None: IPv4 address of the host as seen from a container, or
        ``None`` when the container default gateway can be used instead.
    """
    global _CONTROLLER_HOST, _CONTROLLER_HOST_RESOLVED
    if _CONTROLLER_HOST_RESOLVED:
        return _CONTROLLER_HOST

    _CONTROLLER_HOST_RESOLVED = True
    try:
        import docker

        client = docker.from_env()
        if "Docker Desktop" not in client.info().get("OperatingSystem", ""):
            return _CONTROLLER_HOST
        output = client.containers.run(
            IMAGE,
            ["sh", "-c", "getent ahostsv4 host.docker.internal | head -1"],
            extra_hosts={"host.docker.internal": "host-gateway"},
            remove=True,
        )
        address = output.decode().split()[0]
    except Exception:
        return _CONTROLLER_HOST

    octets = address.split(".")
    if len(octets) == 4 and all(o.isdigit() for o in octets):
        _CONTROLLER_HOST = address
    return _CONTROLLER_HOST


def controller_host_override() -> str:
    """Return the ``$GW`` pre-assignment for a switch startup script.

    ``scripts/switch_startup.sh`` falls back to the container default gateway
    when ``GW`` is unset, which is right on Linux. On Docker Desktop the
    address has to be supplied from outside, so emit an assignment ahead of
    the call.

    Returns:
        str: A ``GW=<address>`` line, or an empty string to let the script
        work the address out for itself.
    """
    host = controller_host()
    if not host:
        return ""
    return f"# Host as seen from a container (Docker Desktop); see D20.\nGW={host}\n"


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
        """Return the endpoint at the other end of the link.

        Args:
            node: One endpoint of the link.

        Returns:
            str: The other endpoint.

        Raises:
            KeyError: If *node* is not an endpoint of this link.
        """
        if node == self.a:
            return self.b
        if node == self.b:
            return self.a
        raise KeyError(f"{node} is not an endpoint of {self.id}")


@dataclass
class Topology:
    switches: List[str]
    hosts: Dict[str, str]
    links: List[Link]


    _ifaces: Dict[str, List[Tuple[int, Link]]] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        """Fill in derived interface-index tables after construction."""
        self._assign_interfaces()



    def host_index(self, host: str) -> int:
        """Return the deterministic index of a host.

        Hosts are ordered alphabetically and indexed from 1.

        Args:
            host: Host name (e.g. ``"h1"``).

        Returns:
            int: 1-based index used for IP/MAC derivation.
        """
        return sorted(self.hosts).index(host) + 1

    def host_ip(self, host: str) -> str:
        """Return the IPv4 address assigned to a host.

        Derived deterministically as ``SUBNET.<host_index>``.

        Args:
            host: Host name.

        Returns:
            str: e.g. ``"10.0.0.1"``.
        """
        return f"{SUBNET}.{self.host_index(host)}"

    def host_mac(self, host: str) -> str:
        """Return the deterministic MAC address of a host.

        Deterministic MACs let every host be given a static ARP table, so no
        ARP traffic ever reaches the controller.

        Args:
            host: Host name.

        Returns:
            str: MAC as ``"00:00:00:00:00:XX"``.
        """
        return f"00:00:00:00:00:{self.host_index(host):02x}"

    def dpid(self, switch: str) -> int:
        """Return the datapath ID of a switch.

        Datapath id = switch index (``s1`` → 1), keeping controller logs and
        ``ovs-ofctl`` output readable.

        Args:
            switch: Switch name (e.g. ``"s1"``).

        Returns:
            int: The switch's datapath ID.
        """
        return self.switches.index(switch) + 1

    def switch_by_dpid(self, dpid: int) -> str:
        """Return the switch name for a datapath ID.

        Args:
            dpid: Datapath ID.

        Returns:
            str: Switch name (e.g. ``"s3"``).

        Raises:
            IndexError: If *dpid* is out of range for this topology.
        """
        return self.switches[dpid - 1]



    def _assign_interfaces(self) -> None:
        """Assign each device's links to consecutive interface indexes.

        Kathara names interfaces ``eth<index>``. OpenFlow port numbers are
        pinned to ``index+1`` via ``ofport_request``.

        Returns:
            None. Populates the private ``_ifaces`` table.

        Raises:
            KeyError: If a link references an unknown device.
        """
        self._ifaces = {name: [] for name in self.switches}
        self._ifaces.update({name: [] for name in self.hosts})
        for link in self.links:
            for endpoint in (link.a, link.b):
                if endpoint not in self._ifaces:
                    raise KeyError(
                        f"link {link.id} references unknown device {endpoint}"
                    )
                self._ifaces[endpoint].append((len(self._ifaces[endpoint]), link))

    def interfaces(self, device: str) -> List[Tuple[int, Link]]:
        """Return the ``(iface_index, link)`` pairs for a device.

        Args:
            device: Switch or host name.

        Returns:
            list[tuple[int, Link]]: Interface index and Link for each
            connection of the device.
        """
        return self._ifaces[device]

    def ofport(self, switch: str, link: Link) -> int:
        """Return the OpenFlow port number of a link on a switch.

        Args:
            switch: Switch name.
            link: The link to look up.

        Returns:
            int: OpenFlow port number (interface index + 1).

        Raises:
            KeyError: If the switch has no interface on that link.
        """
        for index, candidate in self._ifaces[switch]:
            if candidate.id == link.id:
                return index + 1
        raise KeyError(f"{switch} has no interface on {link.id}")

    def port_to_link(self, switch: str) -> Dict[int, str]:
        """Map OpenFlow port numbers to link IDs on a switch.

        This is what lets the controller turn an ``OFPT_PORT_STATUS`` into
        the set of affected flows.

        Args:
            switch: Switch name.

        Returns:
            dict[int, str]: OpenFlow port → link ID.
        """
        return {index + 1: link.id for index, link in self._ifaces[switch]}

    def link_by_id(self, link_id: str) -> Link:
        """Find a link by its endpoint-order-independent ID.

        Args:
            link_id: Link identifier (e.g. ``"s1--s2"``).

        Returns:
            Link: The matching link.

        Raises:
            KeyError: If no link has that ID.
        """
        for link in self.links:
            if link.id == link_id:
                return link
        raise KeyError(link_id)

    def neighbours(self, switch: str) -> Iterable[Tuple[str, Link]]:
        """Yield neighbouring switches and the links to them.

        Access links are excluded, only switch-to-switch neighbours are
        yielded.

        Args:
            switch: Switch name.

        Yields:
            tuple[str, Link]: A neighbouring switch and the core link to it.
        """
        for _, link in self._ifaces[switch]:
            other = link.other(switch)
            if other in self.switches:
                yield other, link

    def core_links(self) -> List[Link]:
        """Return switch-to-switch links (the ones with reservable capacity).

        Returns:
            list[Link]: Links whose endpoints are both switches.
        """
        return [l for l in self.links if l.a in self.switches and l.b in self.switches]

    def access_link(self, host: str) -> Link:
        """Return the access link a host attaches to the network with.

        Args:
            host: Host name.

        Returns:
            Link: The host's access link (host to switch).
        """
        return self._ifaces[host][0][1]



    def _switch_startup(self, switch: str) -> str:
        """Render the startup script for a switch container.

        Loads the real shell logic from ``scripts/switch_startup.sh`` and
        appends the per-switch invocation with its interface specs, so the
        startup file is a single self-contained call.

        Args:
            switch: Switch name.

        Returns:
            str: Shell script as a string.
        """
        specs = " ".join(
            f"eth{index}:{index + 1}:{link.capacity_mbps:g}"
            for index, link in self.interfaces(switch)
        )
        return (
            _script("switch_startup.sh")
            + controller_host_override()
            + f"switch_startup {self.dpid(switch):016x} {specs}\n"
        )

    def _host_startup(self, host: str) -> str:
        """Render the startup script for a host container.

        Loads the real shell logic from ``scripts/host_startup.sh`` and
        appends the per-host invocation with its IP and static-ARP peers.

        Args:
            host: Host name.

        Returns:
            str: Shell script as a string.
        """
        peers = " ".join(
            f"{self.host_ip(peer)}:{self.host_mac(peer)}"
            for peer in sorted(self.hosts)
            if peer != host
        )
        return (
            _script("host_startup.sh")
            + f"host_startup {self.host_ip(host)}/{PREFIX_LEN} "
            f"{self.access_link(host).capacity_mbps:g} {peers}\n"
        )



    def build_lab(self) -> Lab:
        """Construct a Kathara Lab object representing this topology.

        Creates machines for each switch and host, connects them to links
        in interface order, and attaches the generated startup files.

        Returns:
            Lab: A Kathara lab, ready to deploy.
        """
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
            lab.create_file_from_string(
                self._switch_startup(switch), f"{switch}.startup"
            )
        for host in self.hosts:
            lab.create_file_from_string(self._host_startup(host), f"{host}.startup")

        return lab



    def to_dict(self) -> dict:
        """Serialize the topology to a JSON-safe dictionary.

        Returns:
            dict: Keys ``switches`` (name → dpid + ports), ``hosts``
            (name → switch, IP, MAC, ofport), and ``links`` (list of link
            dicts).
        """
        return {
            "switches": {
                s: {"dpid": self.dpid(s), "ports": self.port_to_link(s)}
                for s in self.switches
            },
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
        """Serialize the topology as a JSON string.

        Args:
            indent: JSON indentation. Defaults to 2.

        Returns:
            str: The topology as formatted JSON.
        """
        return json.dumps(self.to_dict(), indent=indent)


def default_topology() -> Topology:
    """Build the default six-switch ring with three chords and six hosts.

    Capacities are tuned so shortest and widest path disagree for several
    host pairs, giving the experiment something to show.

    Returns:
        Topology: The default topology.
    """
    links = [

        Link("s1", "s2", 10),
        Link("s2", "s3", 10),
        Link("s3", "s4", 10),
        Link("s4", "s5", 10),
        Link("s5", "s6", 10),
        Link("s6", "s1", 10),

        Link("s1", "s4", 4),
        Link("s2", "s5", 20),
        Link("s3", "s6", 6),
    ]
    switches = [f"s{i}" for i in range(1, 7)]
    hosts = {f"h{i}": f"s{i}" for i in range(1, 7)}
    links += [
        Link(host, switch, ACCESS_CAPACITY_MBPS) for host, switch in hosts.items()
    ]
    return Topology(switches=switches, hosts=hosts, links=links)





def deploy(topology: Optional[Topology] = None) -> Tuple[Topology, Lab]:
    """Build and deploy the Kathara lab for a topology.

    Args:
        topology: Topology to deploy. Defaults to the default topology.

    Returns:
        tuple[Topology, Lab]: The topology and the deployed Lab object.
    """
    topology = topology or default_topology()
    lab = topology.build_lab()
    Kathara.get_instance().deploy_lab(lab)
    return topology, lab


def undeploy() -> None:
    """Tear down the running Kathara lab."""
    Kathara.get_instance().undeploy_lab(lab_name=LAB_NAME)


def running_machines() -> List[str]:
    """Return the names of the lab's containers currently up.

    Args:
        None.

    Returns:
        list[str]: Device names (e.g. ``["h1", "s1"]``), or ``[]`` if the
        lab is not deployed or no runtime is available.
    """
    try:
        containers = Kathara.get_instance().get_machines_api_objects(lab_name=LAB_NAME)
    except Exception:
        return []

    devices = set(default_topology().switches) | set(default_topology().hosts)
    return sorted(
        {
            device
            for api in containers
            for device in devices
            if f"_{device}_" in api.name
        }
    )


def set_link(a: str, b: str, up: bool) -> Tuple[str, int]:
    """Bring one end of a core link administratively up or down.

    Args:
        a: Switch whose interface is touched, the one that notifies the
            controller.
        b: Switch at the other end.
        up: ``True`` to bring the link up, ``False`` to bring it down.

    Returns:
        tuple[str, int]: ``(switch, interface index)`` so the caller can
        report what was done.

    Raises:
        ValueError: If the link is an access link (host failures are out
            of scope).
        KeyError: If the link does not exist.
    """
    topology = default_topology()
    lo, hi = sorted((a, b))
    link = topology.link_by_id(f"{lo}--{hi}")
    if link.a in topology.hosts or link.b in topology.hosts:
        raise ValueError(f"{link.id} is an access link; host failures are out of scope")

    index = next(
        i for i, candidate in topology.interfaces(a) if candidate.id == link.id
    )
    state = "up" if up else "down"


    cmd = f"set -- eth{index} {state}\n" + (SCRIPTS / "link_state.sh").read_text()
    Kathara.get_instance().exec(
        a,
        ["sh", "-c", cmd],
        lab_name=LAB_NAME,
        stream=False,
    )
    return a, index


def _control_addr():
    """Return the controller's control-socket address.

    Imported lazily: ``controller`` imports this module, so a module-level
    import would be circular.

    Returns:
        tuple[str, int]: Host and port of the JSON control channel.
    """
    from netslice.controller import CONTROL_ADDR

    return CONTROL_ADDR


def controller_responding(timeout: float = 0.4) -> bool:
    """Report whether a controller is accepting control-channel connections.

    Args:
        timeout: Connection timeout in seconds. Defaults to 0.4.

    Returns:
        bool: True if something is listening on the control channel.
    """
    import socket

    try:
        with socket.create_connection(_control_addr(), timeout=timeout):
            return True
    except OSError:
        return False


def controller_pid() -> Optional[int]:
    """Return the PID of the controller this CLI started, if it is still alive.

    A stale PID file, the machine rebooted, or the process was killed by
    other means, is removed rather than reported.

    Returns:
        int | None: The live controller's PID, or None.
    """
    import os

    try:
        pid = int(CONTROLLER_PID.read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        CONTROLLER_PID.unlink(missing_ok=True)
        return None
    return pid


def start_controller(timeout: float = 25.0) -> Tuple[bool, str]:
    """Start the controller as a detached background process.

    Detached (its own session, output to a log) so the shell that launched it
    can exit, otherwise this could not be followed by a deploy in the same
    command.

    Args:
        timeout: Seconds to wait for the control channel to answer.

    Returns:
        tuple[bool, str]: Success flag and a message for the operator.
    """
    import subprocess
    import sys
    import time

    if controller_responding():
        pid = controller_pid()
        where = f" (pid {pid})" if pid else " (not started by this CLI)"
        return False, f"a controller is already running{where}"

    RUN_DIR.mkdir(exist_ok=True)
    log = CONTROLLER_LOG.open("ab")
    log.write(f"\n--- started {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n".encode())
    log.flush()
    process = subprocess.Popen(
        [sys.executable, "-m", "netslice.controller"],
        cwd=str(ROOT),
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    CONTROLLER_PID.write_text(f"{process.pid}\n")

    deadline = time.time() + timeout
    while time.time() < deadline:
        if process.poll() is not None:
            CONTROLLER_PID.unlink(missing_ok=True)
            return False, (
                f"controller exited immediately (code {process.returncode}); "
                f"see {CONTROLLER_LOG}"
            )
        if controller_responding():
            return True, f"controller running (pid {process.pid}), log: {CONTROLLER_LOG}"
        time.sleep(0.3)
    return False, f"controller did not answer within {timeout:g}s; see {CONTROLLER_LOG}"


def stop_controller(timeout: float = 10.0) -> Tuple[bool, str]:
    """Stop the controller started by :func:`start_controller`.

    Args:
        timeout: Seconds to wait for a clean exit before escalating to
            ``SIGKILL``.

    Returns:
        tuple[bool, str]: Success flag and a message for the operator.
    """
    import os
    import signal
    import time

    pid = controller_pid()
    if pid is None:
        if controller_responding():
            return False, (
                "a controller is running but was not started by this CLI; "
                "stop it with:  pkill -f 'netslice.controller'"
            )
        return False, "no controller running"

    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            CONTROLLER_PID.unlink(missing_ok=True)
            return True, f"controller stopped (pid {pid})"
        time.sleep(0.2)

    os.kill(pid, signal.SIGKILL)
    CONTROLLER_PID.unlink(missing_ok=True)
    return True, f"controller did not exit in {timeout:g}s; killed (pid {pid})"


def wait_for_switches(count: int, timeout: float = 45.0) -> int:
    """Wait until every switch has connected to the controller.

    Args:
        count: How many switches the topology has.
        timeout: Seconds to wait before giving up.

    Returns:
        int: How many switches were connected when this returned.
    """
    import time

    from netslice.client import send

    connected = 0
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            reply = send({"cmd": "state"}, addr=_control_addr(), timeout=5.0)
            connected = sum(
                1 for s in reply.get("switches", {}).values() if s.get("connected")
            )
        except (OSError, ValueError):
            connected = 0
        if connected >= count:
            return connected
        time.sleep(1.0)
    return connected


class _HelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Help formatter that leaves room for the longest subcommand name.

    argparse works out the help column from each action's invocation length
    plus the *current* indent, but subcommands are printed one level deeper
    than that. The longest name therefore never fits and wraps onto a line of
    its own, which breaks the eye's run down the list. Measuring subactions at
    the indent they are actually printed at fixes it.
    """

    def add_argument(self, action) -> None:
        """Measure an action, counting subcommands at their printed indent.

        Args:
            action: The argparse action being measured.
        """
        super().add_argument(action)
        subactions = list(self._iter_indented_subactions(action))
        if not subactions:
            return
        longest = max(len(self._format_action_invocation(a)) for a in subactions)
        self._action_max_length = max(
            self._action_max_length,
            longest + self._current_indent + self._indent_increment,
        )


def main(argv=None) -> int:
    """Lab lifecycle and demo controls (CLI entry point).

    Handles ``start``, ``start-controller``, ``stop-controller``, ``deploy``,
    ``undeploy``, ``status``, ``json``, ``links``, ``link-down``, and
    ``link-up`` subcommands.

    Args:
        argv: Optional command-line arguments. Defaults to ``sys.argv[1:]``.

    Returns:
        int: Process exit code (0 for success, 1 for errors).
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="netslice.topology",
        formatter_class=_HelpFormatter,
        description=(
            "Bring the emulated network up and down, and break links to "
            "demonstrate rerouting.\n"
            "The lab is 6 switches in a ring with 3 chords, one host per switch."
        ),
        epilog=(
            "typical session:\n"
            "  netslice-cli start            controller + lab, waits until ready\n"
            "  netslice-cli add h1 h4 5      reserve some bandwidth\n"
            "  netslice-cli link-down s2 s3  break a link and watch it reroute\n"
            "  netslice-cli link-up s2 s3    put it back\n"
            "  netslice-cli undeploy         tear the lab down\n"
            "  netslice-cli stop-controller  stop the controller\n"
            "\n"
            "The controller also serves the dashboard on http://127.0.0.1:8080.\n"
            "Started in the background, it logs to .run/controller.log."
        ),
    )
    sub = parser.add_subparsers(dest="action", required=True, metavar="<command>")
    sub.add_parser("start", help="start the controller, then deploy the lab and wait",
                   description="Start the controller in the background, deploy the lab, "
                               "and wait until every switch has connected. Safe to "
                               "re-run: whatever is already up is left alone.")
    sub.add_parser("start-controller", help="start the controller in the background",
                   description="Start the controller detached, logging to "
                               ".run/controller.log. Does not touch the lab.")
    sub.add_parser("stop-controller", help="stop the background controller",
                   description="Stop the controller started by start/start-controller. "
                               "One started any other way is reported, not killed.")
    sub.add_parser("stop", help="undeploy the lab and stop the controller",
                   description="The reverse of start: remove the lab containers, then "
                                "stop the controller. Safe to re-run, anything already "
                               "down is reported and skipped.")
    sub.add_parser("deploy", help="start the containers",
                   description="Create and start the lab containers. The controller "
                               "should already be running, or the switches waste a "
                               "retry interval finding it.")
    sub.add_parser("undeploy", help="remove the containers",
                   description="Stop and remove every lab container.")
    sub.add_parser("status", help="what is running",
                   description="List the lab containers that are currently up.")
    sub.add_parser("json", help="print the topology and exit",
                   description="Dump the topology as JSON: switches, hosts, links, "
                               "capacities, addresses. Needs no lab.")
    sub.add_parser("links", help="per-switch interface map: which ethN is which link",
                   description="Which interface on which switch carries which link, and "
                               "the OpenFlow port number for each. Needs no lab.")

    for name, verb in (("link-down", "break"), ("link-up", "restore")):
        p = sub.add_parser(
            name,
            help=f"{verb} a core link, for the rerouting demo",
            description=(
                f"{verb.capitalize()} one core link. Only the first switch sees the "
                "change, a collision domain is a Linux bridge, not a veth pair, and "
                "one port-down notification is treated as the whole link being down."
            ),
        )
        p.add_argument(
            "a", help="switch whose interface is touched, the one that notifies"
        )
        p.add_argument("b", help="switch at the other end")

    args = parser.parse_args(argv)
    topology = default_topology()

    if args.action == "start-controller":
        ok, message = start_controller()
        print(message)
        return 0 if ok else 1

    if args.action == "stop-controller":
        ok, message = stop_controller()
        print(message)
        return 0 if ok else 1

    if args.action == "stop":


        if running_machines():
            undeploy()
            print(f"lab '{LAB_NAME}' removed")
        else:
            print(f"lab '{LAB_NAME}' was not deployed")
        ok, message = stop_controller()
        print(message)



        return 0 if ok or not controller_responding() else 1

    if args.action == "start":



        if controller_responding():
            print("controller already running")
        else:
            ok, message = start_controller()
            print(message)
            if not ok:
                return 1
        if running_machines():
            print(f"lab '{LAB_NAME}' is already deployed; leaving it alone")
            return 0
        deploy(topology)
        print(
            f"lab '{LAB_NAME}' deployed: {len(topology.switches)} switches, "
            f"{len(topology.hosts)} hosts"
        )
        connected = wait_for_switches(len(topology.switches))
        if connected < len(topology.switches):
            print(f"  only {connected}/{len(topology.switches)} switches connected")
            print(f"  check the controller log:  {CONTROLLER_LOG}")
            return 1
        print(f"  {connected}/{len(topology.switches)} switches connected")
        print("  dashboard: http://127.0.0.1:8080")
        return 0

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
            print(
                f"  only {switch} reports it, the far end never notices "
                f"(collision domains are Linux bridges, not veth pairs)"
            )
            print(
                f"  restore with:  python -m netslice.topology link-up {args.a} {args.b}"
            )
        else:
            print(f"{lo}--{hi} up  ({switch} eth{index})")
            print(
                "  FAILED flows are retried; flows already rerouted stay where they are"
            )
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
        print(
            "  redeploy with:  python -m netslice.topology undeploy && "
            "python -m netslice.topology deploy"
        )
        return 1

    deploy(topology)
    print(
        f"lab '{LAB_NAME}' deployed: {len(topology.switches)} switches, "
        f"{len(topology.hosts)} hosts"
    )
    print("  switches take ~15 s to run their startup scripts and connect")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
