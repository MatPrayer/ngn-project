"""Minimal Kathara lab used to validate environment assumptions.

Topology:

    h1 --(A)-- s1 --(B)-- s2 --(C)-- h2

s1 and s2 are Open vSwitch instances in `bridged` mode, so they get an extra
NIC on the Docker bridge through which they reach the controller running on
the host. With two collision domains each, that NIC is eth2 (Kathara assigns
the bridged interface the index after the last collision domain).
"""

from Kathara.manager.Kathara import Kathara
from Kathara.model.Lab import Lab

LAB_NAME = "sdn-spike"
IMAGE = "kathara/sdn"

# Controller port on the host, reached via the Docker bridge gateway.
OF_PORT = 6653

HOST_STARTUP = """\
ip addr add {ip}/24 dev eth0
ip link set eth0 up
"""

# The bridged NIC (eth2) is deliberately left out of br0: it is the control
# channel. Only the collision-domain NICs become OpenFlow ports.
SWITCH_STARTUP = """\
/usr/share/openvswitch/scripts/ovs-ctl start --system-id=random --no-mlockall

GW=$(ip route | awk '/^default/ {{print $3}}')

ovs-vsctl add-br br0
ovs-vsctl set bridge br0 protocols=OpenFlow13
ovs-vsctl set bridge br0 other-config:datapath-id={dpid}
ovs-vsctl set-fail-mode br0 secure

for i in {ports}; do
    ip link set $i up
    ovs-vsctl add-port br0 $i
done

ovs-vsctl set-controller br0 tcp:$GW:{of_port}
"""


def build_lab() -> Lab:
    lab = Lab(LAB_NAME)

    for name, dpid in (("s1", "0000000000000001"), ("s2", "0000000000000002")):
        lab.get_or_new_machine(name, image=IMAGE, bridged=True)

    for name, ip in (("h1", "10.0.0.1"), ("h2", "10.0.0.2")):
        lab.get_or_new_machine(name, image=IMAGE)

    # h1 -A- s1 -B- s2 -C- h2
    lab.connect_machine_to_link("h1", "A")
    lab.connect_machine_to_link("s1", "A")  # s1 eth0
    lab.connect_machine_to_link("s1", "B")  # s1 eth1
    lab.connect_machine_to_link("s2", "B")  # s2 eth0
    lab.connect_machine_to_link("s2", "C")  # s2 eth1
    lab.connect_machine_to_link("h2", "C")

    lab.create_file_from_string(HOST_STARTUP.format(ip="10.0.0.1"), "h1.startup")
    lab.create_file_from_string(HOST_STARTUP.format(ip="10.0.0.2"), "h2.startup")

    for name, dpid in (("s1", "0000000000000001"), ("s2", "0000000000000002")):
        lab.create_file_from_string(
            SWITCH_STARTUP.format(dpid=dpid, ports="eth0 eth1", of_port=OF_PORT),
            f"{name}.startup",
        )

    return lab


def deploy() -> Lab:
    lab = build_lab()
    Kathara.get_instance().deploy_lab(lab)
    return lab


def undeploy() -> None:
    Kathara.get_instance().undeploy_lab(lab_name=LAB_NAME)


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "clean":
        undeploy()
        print("lab undeployed")
    else:
        deploy()
        print("lab deployed")
