"""Demo 5 — surviving a link failure.

 Break a link a live flow is using and watch it move. The new
path is installed before the old one is removed, so traffic dips rather than
stops.

One thing to say out loud while this runs: only the switch whose interface was
downed notifies the controller. The specification assumed both ends would,
because it assumed Kathará links are veth pairs — under the Docker manager a
collision domain is a Linux bridge, so the far end never loses carrier. We
validated that early, and the controller treats one notification as the whole
link being down.

    python demo/05_rerouting.py
"""

import time

# _common must come first: it is what puts the repo root on sys.path, so it
# re-exports `topo` rather than every script importing netslice directly.
from _common import (
    Demo,
    flow_by_id,
    iperf_background,
    require_ready,
    reset,
    topo,
)

BREAK = ("s2", "s3")


def main():
    demo = Demo(
        "Link failure and rerouting",
        "link failure and rerouting",
        "detect, reroute, and keep the traffic flowing",
    )
    require_ready()
    reset()

    demo.step("Put a flow on the widest path")
    flow = demo.allocate("h1", "h4", 3, idle_timeout=300)
    flow_id = flow["flow"]["flow_id"]
    original = list(flow["flow"]["path"])
    demo.note(f"{'-'.join(original)} — it crosses {BREAK[0]}–{BREAK[1]}")
    demo.pause()

    demo.step("Start traffic and leave it running")
    iperf_background("h1", "h4", flow["flow"]["tp_dst"], seconds=25)
    time.sleep(3)
    demo.say("iperf3 running for 25s; the failure happens mid-transfer")
    demo.pause("press enter to break the link")

    demo.step(f"Break {BREAK[0]}–{BREAK[1]}")
    switch, index = topo.set_link(*BREAK, up=False)
    demo.say(f"ip link set eth{index} down   (inside {switch})")
    demo.note(f"Only {switch} will report this. The far end keeps carrier, because")
    demo.note("a collision domain is a Linux bridge, not a veth pair.")

    moved = None
    for _ in range(20):
        time.sleep(0.5)
        current = flow_by_id(flow_id)
        if current and current["path"] and current["path"] != original:
            moved = current
            break
    if moved:
        demo.good(f"{flow_id}: {'-'.join(original)}  ->  {'-'.join(moved['path'])}")
        demo.say(f"reroutes: {moved['reroutes']}   state: {moved['state']}")
    else:
        demo.bad("the flow did not move — check the controller log")
    demo.pause()

    demo.step("How fast, and did the traffic survive?")
    _report_latency(demo)
    time.sleep(2)
    current = flow_by_id(flow_id)
    if current and current.get("throughput_mbps"):
        demo.good(f"still carrying {current['throughput_mbps']} Mbps on the new path")
    demo.note("Make-before-break: the new entries went in first, and only then")
    demo.note("were the abandoned switches cleaned up. A flow's match is identical")
    demo.note("on every switch of its path, so reinstalling on a shared switch")
    demo.note("replaces the entry in place — no gap in forwarding.")
    demo.pause()

    demo.step("Restore the link")
    topo.set_link(*BREAK, up=True)
    time.sleep(2)
    current = flow_by_id(flow_id)
    demo.say(f"{flow_id} is on {'-'.join(current['path']) if current else '—'}")
    demo.note("It stays where it is. Only FAILED flows are retried on restoration:")
    demo.note("moving healthy flows back would be churn for no gain.")
    demo.pause()

    demo.step("When there is nowhere to go")
    reset()
    demo.say("filling the alternatives first, then breaking the link again")
    demo.allocate("h1", "h2", 10, idle_timeout=300, allow_preemption=False)
    demo.allocate("h1", "h6", 10, idle_timeout=300, allow_preemption=False)
    stuck = demo.allocate("h1", "h4", 4, idle_timeout=300, allow_preemption=False)
    if stuck.get("ok"):
        topo.set_link("s1", "s4", up=False)
        time.sleep(3)
        current = flow_by_id(stuck["flow"]["flow_id"])
        if current and current["state"] == "FAILED":
            demo.bad(f"{current['flow_id']} FAILED — no alternative path had room")
            demo.note("Reported rather than silently dropped, and retried when a")
            demo.note("link comes back:")
            topo.set_link("s1", "s4", up=True)
            time.sleep(3)
            recovered = flow_by_id(stuck["flow"]["flow_id"])
            if recovered and recovered["state"] == "ACTIVE":
                demo.good(f"link restored -> {recovered['flow_id']} is back on "
                          f"{'-'.join(recovered['path'])}")

    reset()
    demo.done("That is all five. Run demo/run_all.py for the whole sequence.")


def _report_latency(demo):
    """Pull the measured detection-to-installation time out of the event log."""
    import json
    from pathlib import Path

    log = Path(__file__).resolve().parent.parent / "controller_events.jsonl"
    if not log.exists():
        return
    events = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    downs = [e for e in events if e["kind"] == "link_down"]
    reroutes = [e for e in events if e["kind"] == "rerouted"]
    if downs and reroutes and reroutes[-1]["mono"] >= downs[-1]["mono"]:
        gap = (reroutes[-1]["mono"] - downs[-1]["mono"]) * 1000
        demo.say(f"controller: port-down to new path installed in {gap:.1f} ms")
        demo.note("plus roughly 37 ms for the switch to notice the carrier drop,")
        demo.note("measured separately in the spike.")


if __name__ == "__main__":
    main()
