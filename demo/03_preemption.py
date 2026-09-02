"""demo/03_preemption.py - priority-based preemption.

Saturate every path out of s1 with priority-1 traffic, then offer the same
request at priority 1 (refused) and at priority 5 (admitted, with a named
victim).

Victims are rerouted through the failure-recovery routine but without
preemption rights of their own, so a preemption cannot cascade.

    python demo/03_preemption.py
"""

from _common import Demo, require_ready, reset


OUT_OF_S1 = ["s1--s2", "s1--s4", "s1--s6"]


def main():
    """Demonstrate priority-based preemption.

    Fills every link out of s1 with low-priority traffic, then shows
    that a high-priority request reclaims capacity by preempting a victim.
    The victim is rerouted without preemption rights of its own, so
    preemption cannot cascade.
    """
    demo = Demo("Preemption")
    require_ready()
    reset()

    demo.step("Saturate every path out of s1 with priority-1 flows")
    for dst, bandwidth in (("h2", 10), ("h6", 10), ("h4", 4)):
        demo.allocate("h1", dst, bandwidth, priority=1, idle_timeout=300,
                      allow_preemption=False)
    demo.show_links(OUT_OF_S1)
    demo.pause()

    demo.step("Request 4 Mbps at priority 1, preemption allowed")
    demo.allocate("h1", "h4", 4, priority=1, idle_timeout=300)
    demo.note("nothing of lower priority to reclaim")
    demo.pause()

    demo.step("Same request at priority 5")
    high = demo.allocate("h1", "h4", 4, priority=5, idle_timeout=300)
    demo.pause()

    demo.step("Flow table after preemption")
    demo.show_flows(only_active=False)
    if high.get("preempted"):
        demo.note(f"{', '.join(high['preempted'])} released, rerouted without "
                  f"preemption rights, no path with room -> FAILED")
    demo.pause()

    demo.step("Capacity out of s1")
    demo.show_links(OUT_OF_S1)
    demo.note("reserved + residual still equals capacity on every link")

    reset()
    demo.done()


if __name__ == "__main__":
    main()
