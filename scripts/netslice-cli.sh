#!/usr/bin/env bash
# netslice-cli - `python -m netslice.*` cli wrapper.
#
# Usage examples:
#   netslice-cli deploy
#   netslice-cli add h1 h4 5 --priority 2
#   netslice-cli flows
#   netslice-cli serve
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
		echo "usage: netslice-cli <deploy|undeploy|status|json|ifmap|link-up|link-down|add|flows|links|remove|clear|serve|help>"
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
	serve)
		exec "$PY" -m netslice.controller
		;;
	*)
		echo "netslice: unknown command '$cmd'" >&2
		echo "run 'netslice-cli help'" >&2
		exit 1
		;;
esac
