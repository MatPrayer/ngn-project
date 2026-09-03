#!/bin/sh
# Bring up one switch container: OVS + tc shaping + point at the controller.
# Usage: switch_startup <dpid> <iface:ofport:mbps> [...]
switch_startup() {
	DPID=${1:?datapath id}
	shift

	/usr/share/openvswitch/scripts/ovs-ctl start --system-id=random --no-mlockall

	# On Linux the container's default gateway is the host running the
	# controller. On Docker Desktop it is not reachable, so the caller
	# pre-sets GW to an address that is.
	GW=${GW:-$(ip route | awk '/^default/ {print $3}')}

	ovs-vsctl add-br br0
	ovs-vsctl set bridge br0 protocols=OpenFlow13
	ovs-vsctl set bridge br0 other-config:datapath-id="$DPID"
	# Secure fail mode: with no controller the switch forwards nothing, so a
	# controller crash cannot silently turn the network into a hub.
	ovs-vsctl set-fail-mode br0 secure

	for spec in "$@"; do
		iface=${spec%%:*}
		rest=${spec#*:}
		ofport=${rest%%:*}
		mbps=${rest#*:}
		ip link set "$iface" up
		ovs-vsctl add-port br0 "$iface" -- set Interface "$iface" ofport_request="$ofport"
		tc qdisc replace dev "$iface" root handle 1: htb default 1
		tc class replace dev "$iface" parent 1: classid 1:1 htb rate "${mbps}mbit" ceil "${mbps}mbit" burst 15k
	done

	ovs-vsctl set-controller br0 "tcp:$GW:6653"
}
