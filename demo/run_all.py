"""run_all.py - run the demos, all of them or a chosen few.

    python demo/run_all.py              # all five, paced for a live audience
    python demo/run_all.py --no-pause   # straight through, for a rehearsal
    python demo/run_all.py 3            # just the preemption one
    python demo/run_all.py rerouting    # by name instead of number
    python demo/run_all.py 1 2          # a couple, in the order given
    python demo/run_all.py list         # what is available

Checks the lab and the controller once up front, then hands over to each script
in turn, waiting for enter before each one so the presenter controls when the
next demo starts. Any script can also be run on its own, they each reset the
network first, so the order is a narrative convenience rather than a dependency.
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


def resolve(selectors) -> list:
    """Map command-line selectors onto demo scripts.

    A selector is a position (``3``), or any distinctive part of the script's
    name (``ttl``, ``rerouting``, ``04``). Order follows the command line, so
    demos can be replayed in whatever order a question demands.

    Args:
        selectors: Raw selector strings. Empty means every demo, in order.

    Returns:
        list[tuple[str, str]]: The chosen ``(script, blurb)`` pairs.

    Raises:
        KeyError: If a selector matches no demo, or is ambiguous.
    """
    if not selectors:
        return list(SCRIPTS)

    chosen = []
    for selector in selectors:
        if selector.isdigit() and 1 <= int(selector) <= len(SCRIPTS):
            chosen.append(SCRIPTS[int(selector) - 1])
            continue
        matches = [s for s in SCRIPTS if selector.lower() in s[0].lower()]
        if not matches:
            raise KeyError(f"no demo matches {selector!r}")
        if len(matches) > 1:
            names = ", ".join(name for name, _ in matches)
            raise KeyError(f"{selector!r} matches several demos: {names}")
        chosen.append(matches[0])
    return chosen


def listing() -> str:
    """Return the numbered list of demos, one per line.

    Returns:
        str: Lines of ``"  N  script  blurb"``.
    """
    return "\n".join(
        dim(f"    {i}  {name:<26} {blurb}")
        for i, (name, blurb) in enumerate(SCRIPTS, 1)
    )


def wait_for_enter(name: str, blurb: str) -> bool:
    """Wait for the presenter before starting the next demo.

    Args:
        name: Script file name about to run.
        blurb: One-line description of what it shows.

    Returns:
        bool: True to go ahead, False if the user interrupted instead.
    """
    try:
        input(dim(f"\n  ── next: {name}, {blurb}, press enter ──"))
        return True
    except (EOFError, KeyboardInterrupt):
        return False


def main() -> int:
    """Run every demo script in order, stopping on the first failure.

    Checks the lab and controller up front, then invokes each numbered
    demo script as a subprocess, waiting for enter before each. Passes
    ``--no-pause`` and/or ``--quick`` through when given on the command line;
    ``--no-pause`` also skips the wait between demos.

    Returns:
        int: Zero on success, the failing script's exit code, or 130 if the
        presenter interrupted at a prompt.
    """
    arguments = sys.argv[1:]
    if any(a in ("-h", "--help") for a in arguments):
        print(__doc__.strip())
        print("\ndemos:")
        print(listing())
        return 0

    passthrough = [a for a in arguments if a in ("--no-pause", "--quick")]
    selectors = [a for a in arguments if not a.startswith("-") and a != "list"]

    if "list" in arguments:
        print(listing())
        return 0

    unknown = [a for a in arguments if a.startswith("-") and a not in passthrough]
    if unknown:
        print(red(f"  unknown option(s): {' '.join(unknown)}"))
        print(__doc__.strip())
        return 2

    try:
        scripts = resolve(selectors)
    except KeyError as exc:
        print(red(f"  {exc.args[0]}"))
        print("\ndemos:")
        print(listing())
        return 2

    paused = "--no-pause" not in passthrough and sys.stdin.isatty()

    print()
    print(bold("  netslice, network slicing in SDN"))
    if len(scripts) == len(SCRIPTS):
        print(dim("  five demos, in the order the report presents them"))
    else:
        print(dim(f"  {len(scripts)} of {len(SCRIPTS)} demos"))
    for name, blurb in scripts:
        print(dim(f"    {name:<26} {blurb}"))

    require_ready()
    reset()

    for name, blurb in scripts:
        if paused and not wait_for_enter(name, blurb):
            print(red("\n  interrupted; putting the network back"))
            reset()
            return 130

        sys.stdout.flush()
        result = subprocess.run([sys.executable, str(HERE / name), *passthrough])
        if result.returncode != 0:
            print(red(f"\n  {name} exited with {result.returncode}; stopping here"))
            reset()
            return result.returncode

    reset()
    done = (
        "all five"
        if len(scripts) == len(SCRIPTS)
        else f"{len(scripts)} of {len(SCRIPTS)}"
    )
    print(bold(f"  {done} complete, network back to a clean state"))
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
