#!/bin/sh
# Bring one end of a core link administratively up or down (rerouting demo).
# Usage: link_state.sh <iface> <up|down>
set -eu
ip link set "$1" "$2"
