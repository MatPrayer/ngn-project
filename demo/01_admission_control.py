"""demo/01_admission_control.py - admission control preserves per-flow guarantees.

Offers the same request sequence twice, with admission control and without it.
With it, fewer flows are admitted and each gets what it asked for; without it,
all are admitted and none do.

Flows are measured *concurrently*: run one after another they each reach the
full rate, because they never actually contend, and the comparison shows
nothing.

    python demo/01_admission_control.py
"""

from _common import Demo, green, iperf_parallel, red, require_ready, reset


REQUEST_MBPS = 3
ATTEMPTS = 9


def measure(demo, flows, label):
    rates = iperf_parallel(
        [("h1", "h4", f["tp_dst"]) for f in flows],
        seconds=4 if demo.args.quick else 7,
    )
    met = 0
    for flow in flows:
        rate = rates[flow["tp_dst"]]
        ok = rate is not None and rate >= 0.85 * flow["bandwidth_mbps"]
        met += ok
        demo.say(f"{flow['flow_id']:<5} asked {flow['bandwidth_mbps']:>4.1f}   "
                 f"got {(green if ok else red)(f'{rate} Mbps')}")
    demo.say(f"{label}: {met}/{len(flows)} met their reservation")
    return met


def main():
    demo = Demo("Admission control", "admission control, and the baseline with none")
    require_ready()
    reset()

    demo.step(f"Offer {ATTEMPTS} requests of {REQUEST_MBPS} Mbps, h1 -> h4, "
              f"admission control ON")
    admitted = []
    for _ in range(ATTEMPTS):
        reply = demo.allocate("h1", "h4", REQUEST_MBPS, idle_timeout=300,
                              allow_preemption=False)
        if reply.get("ok"):
            admitted.append(reply["flow"])
    demo.say(f"{len(admitted)} admitted, {ATTEMPTS - len(admitted)} refused")
    demo.pause()

    demo.step("Capacity out of s1")
    demo.show_links(["s1--s2", "s1--s4", "s1--s6"])
    demo.pause()

    demo.step("Concurrent iperf3 over the admitted flows")
    met_with = measure(demo, admitted, "with admission control")
    demo.pause()

    reset()
    demo.step(f"Same {ATTEMPTS} requests, admission control OFF")
    forced = []
    for _ in range(ATTEMPTS):
        reply = demo.allocate("h1", "h4", REQUEST_MBPS, policy="shortest",
                              idle_timeout=300, admission_control=False)
        if reply.get("ok"):
            forced.append(reply["flow"])
    demo.say(f"{len(forced)} admitted, 0 refused")
    demo.pause()

    demo.step("Capacity out of s1")
    demo.show_links(["s1--s4"])
    demo.note("residual is negative: more reserved than the link can carry")
    demo.pause()

    demo.step("Concurrent iperf3 over the admitted flows")
    met_without = measure(demo, forced, "without admission control")
    demo.pause()

    demo.step("Result")
    demo.say(f"admission control ON    {len(admitted)}/{ATTEMPTS} admitted   "
             f"{green(f'{met_with} satisfied')}")
    demo.say(f"admission control OFF   {len(forced)}/{ATTEMPTS} admitted   "
             f"{red(f'{met_without} satisfied')}")

    reset()
    demo.done()


if __name__ == "__main__":
    main()
