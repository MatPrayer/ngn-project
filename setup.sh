#!/usr/bin/env bash
set -eu

ROOT=$(cd -P "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)
LAUNCHER="$ROOT/scripts/netslice-cli.sh"
VENV="$ROOT/.venv"
BIN=${NETSLICE_BIN:-"$HOME/bin"}
LINK="$BIN/netslice-cli"

find_python() {
	if [ -n "${NETSLICE_PYTHON:-}" ]; then
		[ -x "$NETSLICE_PYTHON" ] && echo "$NETSLICE_PYTHON" && return 0
		return 1
	fi

	# Try pyenv first
	if command -v pyenv >/dev/null 2>&1; then
		local pyenv_python
		pyenv_python="$(pyenv which python3.11 2>/dev/null)" && [ -x "$pyenv_python" ] && { echo "$pyenv_python"; return 0; }
	fi

	# Fall back to system python3.11
	command -v python3.11 >/dev/null 2>&1 && { echo "$(command -v python3.11)"; return 0; }

	return 1
}

PY=$(find_python) || { echo "error: no Python 3.11 found." >&2; echo "  Install pyenv (https://pyenv.sh)" >&2; echo "  Or install python@3.11 via a packege manager" >&2; echo "  Or set NETSLICE_PYTHON=/path/to/3.11/bin/python" >&2; exit 1; }
echo "using python: $PY ($("$PY" --version 2>&1))"

if [ ! -x "$VENV/bin/python" ]; then
	echo "creating venv at $VENV"
	"$PY" -m venv "$VENV"
fi

"$VENV/bin/python" -m pip install --quiet --upgrade pip
echo "installing requirements into $VENV"
"$VENV/bin/pip" install -r "$ROOT/requirements.txt"

if [ ! -x "$LAUNCHER" ]; then
	chmod +x "$LAUNCHER"
fi

mkdir -p "$BIN"
ln -sfn "$LAUNCHER" "$LINK"
echo "linked $LINK -> $LAUNCHER"
echo "venv python: $VENV/bin/python"

case ":$PATH:" in
	*":$BIN:"*) : ;;
	*) echo; echo "WARNING: $BIN is not on your PATH. Add:"; echo "  export PATH=\"$BIN:\$PATH\"";;
esac

echo
echo "done. Try:  $LINK help"
