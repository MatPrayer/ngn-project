"""Demo 3 — preemption: making room for a flow that matters.

 Saturate every way out of s1 with low-priority traffic, then
show the same request refused at priority 1 and admitted at priority 5, with a
named victim. The victim is rerouted through the same code path as failure
recovery, but without preemption rights of its own — which is what stops a
cascade.

    python demo/03_preemption.py
"""

from _common import Demo, require_ready, reset


OUT_OF_S1 = ["s1--s2", "s1--s4", "s1--s6"]


def main():
    demo = Demo(
        "Preemption",
        "preemption",
        "a high-priority request reclaims capacity from less important flows",
    )
    require_ready()
    reset()

    demo.step("Fill every path out of s1 with priority-1 traffic")
    for dst, bandwidth in (("h2", 10), ("h6", 10), ("h4", 4)):
        demo.allocate("h1", dst, bandwidth, priority=1, idle_timeout=300,
                      allow_preemption=False)
    demo.show_links(OUT_OF_S1)
    demo.note("Nothing leaves s1 without going through one of these three.")
    demo.pause()

    demo.step("A priority-1 request now has nowhere to go")
    demo.allocate("h1", "h4", 4, priority=1, idle_timeout=300)
    demo.note("Even with preemption allowed: there is nothing of *lower*")
    demo.note("priority to reclaim, so the answer is still no.")
    demo.pause()

    demo.step("The same request at priority 5")
    high = demo.allocate("h1", "h4", 4, priority=5, idle_timeout=300)
    demo.note("Phase 1 re-ran widest-path over preemptable(l) = residual plus")
    demo.note("everything of lower priority on the link. Phase 2 then chose the")
    demo.note("victims: lowest priority first, fattest first among equals, so")
    demo.note("the fewest flows are interrupted.")
    demo.pause()

    demo.step("What happened to the victim")
    demo.show_flows(only_active=False)
    victims = high.get("preempted", [])
    if victims:
        demo.note(f"{', '.join(victims)} was released and immediately offered a new")
        demo.note("path — through the same routine that handles link failure, but")
        demo.note("with no preemption rights, so it cannot displace anyone in turn.")
        demo.note("Here the network is full by construction, so it ends FAILED and")
        demo.note("is reported rather than silently dropped.")
    demo.pause()

    demo.step("Capacity after the reshuffle")
    demo.show_links(OUT_OF_S1)
    demo.note("The books still balance: every Mbps is either reserved by an")
    demo.note("ACTIVE flow or residual. Preemption releases and reserves in one")
    demo.note("step, so the two can never drift apart.")
    demo.pause()

    demo.step("The other victim-selection policy")
    demo.note("`--tie-break best_fit` picks the smallest flow that covers the")
    demo.note("deficit instead — minimising wasted bandwidth rather than the")
    demo.note("number of interruptions. Both are implemented; compares them.")

    reset()
    demo.done("Next: demo/04_ttl.py")


if __name__ == "__main__":
    main()
