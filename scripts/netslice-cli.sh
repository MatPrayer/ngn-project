#!/usr/bin/env bash
# netslice-cli - `python -m netslice.*` cli wrapper.
#
# Usage examples:
#   netslice-cli deploy
#   netslice-cli add h1 h4 5 --priority 2
#   netslice-cli demo 2         run one demo
#   netslice-cli flows
#   netslice-cli start          controller in the background, then the lab
#   netslice-cli stop           undeploy the lab and stop the controller
#   netslice-cli serve          controller in the foreground, on this terminal
set -eu

SOURCE="${BASH_SOURCE[0]}"
while [ -L "$SOURCE" ]; do
	DIR=$(cd -P "$(dirname "$SOURCE")" >/dev/null 2>&1 && pwd)
	SOURCE=$(readlink "$SOURCE")
	[[ $SOURCE != /* ]] && SOURCE="$DIR/$SOURCE"
done
ROOT=$(cd -P "$(dirname "$SOURCE")/.." >/dev/null 2>&1 && pwd)

PY=${NETSLICE_PY:-"$ROOT/.venv/bin/python"}
[ -x "$PY" ] || { echo "netslice: no venv at $PY — run ./setup.sh first" >&2; exit 1; }

cmd=${1:-help}
shift || true

	case "$cmd" in
	help|-h|--help)
		cat <<'USAGE'
netslice-cli <command> [args]

  Network slicing controller for an emulated Kathara lab.
  Wraps `python -m netslice.topology` and `python -m netslice.client`.

lab
  start                 start the controller, deploy the lab, wait until ready
  stop                  undeploy the lab and stop the controller
  start-controller      start the controller in the background
  stop-controller       stop the background controller
  serve                 run the controller in the foreground, on this terminal
  deploy                create the lab containers
  undeploy              remove them
  status                which containers are up

flows
  add <src> <dst> <mbps>   request a reservation, e.g. add h1 h4 5 --priority 2
  flows                    what is allocated now
  links                    per-link capacity and residual
  state                    one snapshot: flows, links, switches, hosts
  remove <flow-id>         release one flow
  clear                    release everything

demos
  demo                  run all five, paced for a live audience
  demo <n|name> ...     run some, e.g. demo 3   or   demo ttl rerouting
  demo list             what is available
  demo --no-pause       straight through, no waiting for enter
  demo --quick          shorter iperf runs; faster, less accurate

failure
  link-down <a> <b>     break a core link, e.g. link-down s2 s3
  link-up <a> <b>       restore it

inspect
  json                  the topology as JSON (no lab needed)
  ifmap                 which ethN on which switch is which link

  Dashboard: http://127.0.0.1:8080 while the controller runs.
  Full options for any command:  netslice-cli <command> --help
USAGE
		;;
	deploy|undeploy|status)
		exec "$PY" -m netslice.topology "$cmd"
		;;
	json|ifmap)
		sub=${cmd/ifmap/links}
		exec "$PY" -m netslice.topology "$sub"
		;;
	link-up|link-down)
		exec "$PY" -m netslice.topology "$cmd" "$@"
		;;
	add|remove|flows|links|clear)
		exec "$PY" -m netslice.client "$cmd" "$@"
		;;
	demo)
		exec "$PY" "$ROOT/demo/run_all.py" "$@"
		;;
	serve)
		exec "$PY" -m netslice.controller
		;;
	*)
		echo "netslice: unknown command '$cmd'" >&2
		echo "run 'netslice-cli help'" >&2
		exit 1
		;;
esac
