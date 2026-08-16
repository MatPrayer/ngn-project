"""Demo 4 — TTL: reservations that clean up after themselves.

 A reservation nobody releases is a leak. OpenFlow entries
carry both timeout types and the project uses both: `idle_timeout` is adaptive
and resets while traffic flows, `hard_timeout` is an absolute upper bound.

The switch reports expiry with OFPT_FLOW_REMOVED — but only if
OFPFF_SEND_FLOW_REM was set at install time. Forget that flag and bandwidth
leaks forever, which is why it was validated before anything was built on it.

    python demo/04_ttl.py
"""

import time

from _common import (
    Demo,
    flow_by_id,
    iperf_background,
    require_ready,
    reset,
    residual,
)

IDLE = 8
HARD = 12


def secs(value):
    return "—" if value is None else f"{value}s"


def rate(value):
    return "—" if value is None else f"{value} Mbps"


def main():
    demo = Demo(
        "TTL — automatic release",
        "TTL and automatic release",
        "idle timeouts adapt to traffic, hard timeouts do not",
    )
    require_ready()
    reset()

    # ------------------------------------------------------------ idle, quiet
    demo.step(f"A flow with idle_timeout={IDLE}s, and no traffic on it")
    quiet = demo.allocate("h3", "h6", 3, idle_timeout=IDLE)
    flow_id = quiet["flow"]["flow_id"]
    link = quiet["flow"]["links"][0]
    demo.say(f"{link} residual: {residual(link)} Mbps")
    demo.pause()

    demo.step("Wait, and watch the capacity come back on its own")
    for _ in range(IDLE + 6):
        time.sleep(1)
        flow = flow_by_id(flow_id)
        if flow and flow["state"] != "ACTIVE":
            demo.live_done()
            demo.good(f"{flow_id} expired ({flow['state']}) — "
                      f"{link} residual back to {residual(link)} Mbps")
            break
        demo.live(f"{flow['state'] if flow else '?':<8} "
                  f"idle {secs(flow.get('idle_for_sec') if flow else None):<6} "
                  f"residual {residual(link)} Mbps")
    else:
        demo.live_done()
        demo.warn("did not expire — is something still sending on it?")
    demo.note("The switch fired OFPT_FLOW_REMOVED; the controller tore down the")
    demo.note("rest of the path and released every link in one step.")
    demo.pause()

    # ------------------------------------------------------------- idle, busy
    reset()
    demo.step(f"The same idle_timeout={IDLE}s, but with traffic running")
    busy = demo.allocate("h3", "h6", 3, idle_timeout=IDLE)
    busy_id = busy["flow"]["flow_id"]
    iperf_background("h3", "h6", busy["flow"]["tp_dst"], seconds=IDLE + 8)
    demo.note("The timer resets every time a packet matches, so an active flow")
    demo.note("keeps its reservation for as long as it is actually using it.")

    for _ in range(IDLE + 3):
        time.sleep(1)
        flow = flow_by_id(busy_id)
        if not flow:
            break
        demo.live(f"{flow['state']:<8} idle {secs(flow.get('idle_for_sec')):<6} "
                  f"throughput {rate(flow.get('throughput_mbps'))}")
    demo.live_done()
    flow = flow_by_id(busy_id)
    if flow and flow["state"] == "ACTIVE":
        demo.good(f"still ACTIVE after {IDLE}s — the traffic kept it alive")
    else:
        demo.warn("expired despite the traffic — check the iperf run started")
    demo.note("Note the idle counter never climbing past a second or two. The")
    demo.note("controller is never told the switch reset the timer, so it infers")
    demo.note("it by watching the packet counter move — which is also where the")
    demo.note("dashboard's throughput column comes from.")
    demo.pause()

    # ------------------------------------------------------------------- hard
    reset()
    demo.step(f"hard_timeout={HARD}s — an upper bound traffic cannot extend")
    hard = demo.allocate("h3", "h6", 3, idle_timeout=0, hard_timeout=HARD)
    hard_id = hard["flow"]["flow_id"]
    iperf_background("h3", "h6", hard["flow"]["tp_dst"], seconds=HARD + 10)
    demo.note("Same flow, same traffic, but this timer ignores it entirely.")

    for _ in range(HARD + 6):
        time.sleep(1)
        flow = flow_by_id(hard_id)
        if flow and flow["state"] != "ACTIVE":
            demo.live_done()
            demo.good(f"{hard_id} expired at the hard limit, mid-transfer")
            break
        if flow:
            demo.live(f"{flow['state']:<8} {secs(flow.get('remaining_hard_sec')):<6} left  "
                      f"throughput {rate(flow.get('throughput_mbps'))}")
    else:
        demo.live_done()
        demo.warn("did not expire in time")
    demo.note("Adaptive by default so useful flows are not cut off; a hard bound")
    demo.note("when a flow must not hold bandwidth indefinitely. Both are set per")
    demo.note("request, so the two can be shown side by side.")

    reset()
    demo.done("Next: demo/05_rerouting.py")


if __name__ == "__main__":
    main()
