"""demo/05_rerouting.py - link failure and rerouting.

A live flow crossing a link that is then broken: the new path is installed
before the old one is removed, so traffic dips rather than stops. Ends with a
flow that has nowhere to go, which goes FAILED and is retried on restoration.

Only the switch whose interface was downed notifies the controller. The
specification assumed both ends would, because it assumed Kathará links are
veth pairs; under the Docker manager a collision domain is a Linux bridge, so
the far end never loses carrier. Worth saying out loud during the demo.

    python demo/05_rerouting.py
"""

import time


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
    """Demonstrate link-failure rerouting and recovery.

    Breaks a link that a live flow is using, watches it move to an
    alternative path without traffic stopping, then restores the link
    (the flow stays on its new path). A final case shows a flow that
    has no alternative going FAILED and recovering when the link is
    restored.
    """
    demo = Demo("Link failure and rerouting")
    require_ready()
    reset()

    demo.step("Flow on the widest path, traffic running")
    flow = demo.allocate("h1", "h4", 3, idle_timeout=300)
    flow_id = flow["flow"]["flow_id"]
    original = list(flow["flow"]["path"])
    iperf_background("h1", "h4", flow["flow"]["tp_dst"], seconds=25)
    time.sleep(3)
    demo.pause(f"press enter to break {BREAK[0]}-{BREAK[1]}")

    demo.step(f"Break {BREAK[0]}-{BREAK[1]}")
    switch, index = topo.set_link(*BREAK, up=False)
    demo.say(f"ip link set eth{index} down   (inside {switch})")
    demo.note(f"only {switch} reports it; the far end keeps carrier")

    moved = None
    for _ in range(20):
        time.sleep(0.5)
        current = flow_by_id(flow_id)
        if current and current["path"] and current["path"] != original:
            moved = current
            break
    if moved:
        demo.good(f"{flow_id}  {'-'.join(original)} -> {'-'.join(moved['path'])}"
                  f"   reroutes={moved['reroutes']}  {moved['state']}")
    else:
        demo.bad("flow did not move")
    _report_latency(demo)
    time.sleep(3)
    current = flow_by_id(flow_id)
    demo.say(f"throughput on the new path: "
             f"{current.get('throughput_mbps') if current else '-'} Mbps")
    demo.pause()

    demo.step("Restore the link")
    topo.set_link(*BREAK, up=True)
    time.sleep(2)
    current = flow_by_id(flow_id)
    demo.say(f"{flow_id} on {'-'.join(current['path']) if current else '-'}")
    demo.note("healthy flows are not moved back ")
    demo.pause()

    demo.step("Flow with no alternative path")
    reset()
    demo.allocate("h1", "h2", 10, idle_timeout=300, allow_preemption=False)
    demo.allocate("h1", "h6", 10, idle_timeout=300, allow_preemption=False)
    stuck = demo.allocate("h1", "h4", 4, idle_timeout=300, allow_preemption=False)
    if stuck.get("ok"):
        topo.set_link("s1", "s4", up=False)
        time.sleep(3)
        current = flow_by_id(stuck["flow"]["flow_id"])
        if current and current["state"] == "FAILED":
            demo.bad(f"{current['flow_id']} FAILED, no path with room")
            topo.set_link("s1", "s4", up=True)
            time.sleep(3)
            recovered = flow_by_id(stuck["flow"]["flow_id"])
            if recovered and recovered["state"] == "ACTIVE":
                demo.good(f"link restored -> {recovered['flow_id']} ACTIVE on "
                          f"{'-'.join(recovered['path'])}")

    reset()
    demo.done()


def _report_latency(demo):
    """Print the detection-to-installation latency from the controller event log.

    Reads ``controller_events.jsonl`` and computes the gap between the
    last ``link_down`` and the last ``rerouted`` event.

    Args:
        demo: A ``Demo`` instance used for narration output.
    """
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
        demo.say(f"port-down to new path installed: {gap:.1f} ms "
                 f"(+ ~37 ms for the switch to notice)")


if __name__ == "__main__":
    main()
