"""Run every demo in order.

    python demo/run_all.py              # paced, for the live presentation
    python demo/run_all.py --no-pause   # straight through, for a rehearsal

Checks the lab and the controller once up front, then hands over to each script
in turn. Any script can also be run on its own — they each reset the network
first, so the order is a narrative convenience rather than a dependency.
"""

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from _common import bold, dim, red, require_ready, reset

SCRIPTS = [
    ("01_admission_control.py", "accept what fits, refuse what does not"),
    ("02_widest_path.py", "fewest hops is not the same as most capacity"),
    ("03_preemption.py", "make room for a flow that matters"),
    ("04_ttl.py", "reservations that clean up after themselves"),
    ("05_rerouting.py", "survive a link failure"),
]


def main() -> int:
    """Run every demo script in order, stopping on the first failure.

    Checks the lab and controller up front, then invokes each numbered
    demo script as a subprocess. Passes ``--no-pause`` and/or ``--quick``
    through when given on the command line.

    Returns:
        int: Zero on success, or the failing script's exit code.
    """
    passthrough = [a for a in sys.argv[1:] if a in ("--no-pause", "--quick")]

    print()
    print(bold("  netslice — network slicing in SDN"))
    print(dim("  five demos, in the order the report presents them"))
    for name, blurb in SCRIPTS:
        print(dim(f"    {name:<26} {blurb}"))

    require_ready()
    reset()

    for name, _ in SCRIPTS:
        result = subprocess.run([sys.executable, str(HERE / name), *passthrough])
        if result.returncode != 0:
            print(red(f"\n  {name} exited with {result.returncode}; stopping here"))
            reset()
            return result.returncode

    reset()
    print(bold("  all five complete, network back to a clean state"))
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
