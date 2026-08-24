#!/bin/sh
# Bring up one host container: IP + tc shaping + static ARP for every peer.
# Usage: host_startup <ip/prefix> <mbps> <peer-ip:peer-mac> [...]
host_startup() {
	IP=${1:?ip/prefix}; MBPS=${2:?mbps}
	shift 2

	ip link set eth0 up
	ip addr add "$IP" dev eth0
	tc qdisc replace dev eth0 root handle 1: htb default 1
	tc class replace dev eth0 parent 1: classid 1:1 htb rate "${MBPS}mbit" ceil "${MBPS}mbit" burst 15k

	# Static ARP for every peer: keeps ARP off the data plane entirely, so the
	# controller only ever handles IP flows.
	for spec in "$@"; do
		peer_ip=${spec%%:*}
		peer_mac=${spec#*:}
		ip neigh replace "$peer_ip" lladdr "$peer_mac" dev eth0 nud permanent
	done
}
