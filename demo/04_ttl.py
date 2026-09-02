"""demo/04_ttl.py - TTL-based automatic flow release.

Three cases: an idle flow expiring and releasing its capacity, the same flow
kept alive by traffic, and a hard timeout firing mid-transfer regardless.

`idle_timeout` resets whenever traffic matches and the switch never says so, so
the idle counter shown here is derived from the switch's own packet counters,
the same source the dashboard's TTL column uses.

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
    """Format a seconds value for display, or a dash when ``None``.

    Args:
        value (int, float, or None): Seconds to format.

    Returns:
        str: Formatted string (e.g. ``'8s'``) or ``'-'``.
    """
    return "-" if value is None else f"{value}s"


def rate(value):
    """Format a throughput value for display, or a dash when ``None``.

    Args:
        value (int, float, or None): Throughput in Mbps.

    Returns:
        str: Formatted string (e.g. ``'3.5 Mbps'``) or ``'-'``.
    """
    return "-" if value is None else f"{value} Mbps"


def main():
    """Demonstrate TTL-based automatic flow release.

    Three cases: an idle flow expiring and releasing its capacity, the
    same flow kept alive by active traffic, and a hard timeout firing
    mid-transfer regardless of activity.
    """
    demo = Demo("TTL, automatic release")
    require_ready()
    reset()


    demo.step(f"idle_timeout={IDLE}s, no traffic on the flow")
    quiet = demo.allocate("h3", "h6", 3, idle_timeout=IDLE)
    flow_id = quiet["flow"]["flow_id"]
    link = quiet["flow"]["links"][0]
    demo.say(f"{link} residual {residual(link)} Mbps")

    for _ in range(IDLE + 6):
        time.sleep(1)
        flow = flow_by_id(flow_id)
        if flow and flow["state"] != "ACTIVE":
            demo.live_done()
            demo.good(f"{flow_id} {flow['state']}, {link} residual "
                      f"{residual(link)} Mbps")
            break
        demo.live(f"idle {secs(flow.get('idle_for_sec') if flow else None):<6} "
                  f"residual {residual(link)} Mbps")
    else:
        demo.live_done()
        demo.warn("did not expire")
    demo.pause()


    reset()
    demo.step(f"idle_timeout={IDLE}s, traffic running")
    busy = demo.allocate("h3", "h6", 3, idle_timeout=IDLE)
    busy_id = busy["flow"]["flow_id"]
    iperf_background("h3", "h6", busy["flow"]["tp_dst"], seconds=IDLE + 8)

    for _ in range(IDLE + 3):
        time.sleep(1)
        flow = flow_by_id(busy_id)
        if not flow:
            break
        demo.live(f"idle {secs(flow.get('idle_for_sec')):<6} "
                  f"throughput {rate(flow.get('throughput_mbps'))}")
    demo.live_done()
    flow = flow_by_id(busy_id)
    if flow and flow["state"] == "ACTIVE":
        demo.good(f"still ACTIVE after {IDLE}s, idle never exceeded ~2s")
    else:
        demo.warn("expired despite the traffic")
    demo.pause()


    reset()
    demo.step(f"hard_timeout={HARD}s, traffic running")
    hard = demo.allocate("h3", "h6", 3, idle_timeout=0, hard_timeout=HARD)
    hard_id = hard["flow"]["flow_id"]
    iperf_background("h3", "h6", hard["flow"]["tp_dst"], seconds=HARD + 10)

    for _ in range(HARD + 6):
        time.sleep(1)
        flow = flow_by_id(hard_id)
        if flow and flow["state"] != "ACTIVE":
            demo.live_done()
            demo.good(f"{hard_id} expired mid-transfer")
            break
        if flow:
            demo.live(f"{secs(flow.get('remaining_hard_sec')):<6} left   "
                      f"throughput {rate(flow.get('throughput_mbps'))}")
    else:
        demo.live_done()
        demo.warn("did not expire")

    reset()
    demo.done()


if __name__ == "__main__":
    main()
