"""Demo 1 — admission control: the baseline the whole project rests on.

The required comparison from The same sequence of requests is
offered twice: once with admission control, once with every request accepted
regardless of network state. With it, fewer flows are admitted and each gets
what it asked for. Without it, all of them are admitted and none of them do.

The measurement matters as much as the policy: the flows are run *concurrently*,
because contention only shows up when flows actually contend.

    python demo/01_admission_control.py
"""

from _common import Demo, green, iperf_parallel, red, require_ready, reset


REQUEST_MBPS = 3
ATTEMPTS = 9


def main():
    demo = Demo(
        "Admission control",
        "admission control, and the baseline with none",
        "accept what fits, refuse what does not, and say why",
    )
    require_ready()
    reset()

    # ------------------------------------------------------------------ with
    demo.step("An empty network: everything out of s1 is free")
    demo.show_links(["s1--s2", "s1--s4", "s1--s6"])
    demo.note("h1 sits behind s1, so every flow it sends crosses one of these.")
    demo.pause()

    demo.step(f"Offer {ATTEMPTS} requests of {REQUEST_MBPS} Mbps each, h1 -> h4")
    admitted = []
    rejected = 0
    for _ in range(ATTEMPTS):
        reply = demo.allocate("h1", "h4", REQUEST_MBPS, idle_timeout=300,
                              allow_preemption=False)
        if reply.get("ok"):
            admitted.append(reply["flow"])
        else:
            rejected += 1
    demo.note(f"{len(admitted)} admitted, {rejected} refused — each refusal names")
    demo.note("the reason. The network said no *before* programming anything,")
    demo.note("rather than accepting and letting the flows fight it out.")
    demo.pause()

    demo.step("Where the capacity went")
    demo.show_links(["s1--s2", "s1--s4", "s1--s6"])
    demo.note("Widest path spread the flows across different routes, filling the")
    demo.note("wide ones before falling back to the narrow 4 Mbps chord.")
    demo.pause()

    demo.step("Do the admitted flows get what they asked for? (all at once)")
    controlled = iperf_parallel(
        [("h1", "h4", f["tp_dst"]) for f in admitted],
        seconds=4 if demo.args.quick else 7,
    )
    met = 0
    for flow in admitted:
        rate = controlled[flow["tp_dst"]]
        ok = rate is not None and rate >= 0.85 * flow["bandwidth_mbps"]
        met += ok
        demo.say(f"{flow['flow_id']}  asked {flow['bandwidth_mbps']:>4.1f}  "
                 f"got {(green if ok else red)(f'{rate} Mbps')}")
    demo.good(f"{met}/{len(admitted)} flows met their requested bandwidth")
    demo.note("A meter on each ingress switch caps the flow at its reservation,")
    demo.note("so nobody can take more than they were promised either.")
    demo.pause("press enter to run the same sequence without admission control")

    # --------------------------------------------------------------- without
    reset()
    demo.step("Now accept every request regardless of network state")
    demo.note("The naive baseline — one flag on the request, same code path.")
    forced = []
    for _ in range(ATTEMPTS):
        reply = demo.allocate("h1", "h4", REQUEST_MBPS, policy="shortest",
                              idle_timeout=300, admission_control=False)
        if reply.get("ok"):
            forced.append(reply["flow"])
    demo.good(f"{len(forced)}/{ATTEMPTS} admitted — nothing is ever refused")
    demo.pause()

    demo.step("What that does to the links")
    demo.show_links(["s1--s4"])
    demo.warn("residual is negative: more is reserved than the link can carry")
    demo.pause()

    demo.step("And what each flow actually gets (all at once)")
    uncontrolled = iperf_parallel(
        [("h1", "h4", f["tp_dst"]) for f in forced],
        seconds=4 if demo.args.quick else 7,
    )
    met_without = 0
    for flow in forced:
        rate = uncontrolled[flow["tp_dst"]]
        ok = rate is not None and rate >= 0.85 * flow["bandwidth_mbps"]
        met_without += ok
        demo.say(f"{flow['flow_id']}  asked {flow['bandwidth_mbps']:>4.1f}  "
                 f"got {(green if ok else red)(f'{rate} Mbps')}")
    demo.pause()

    demo.step("Side by side")
    demo.say(f"with admission control     {len(admitted)}/{ATTEMPTS} admitted, "
             f"{green(f'{met} satisfied')}")
    demo.say(f"without admission control  {len(forced)}/{ATTEMPTS} admitted, "
             f"{red(f'{met_without} satisfied')}")
    demo.note("Admitting more is not the same as delivering more. That gap is")
    demo.note("what asks the report to quantify.")

    reset()
    demo.done("Next: demo/02_widest_path.py")


if __name__ == "__main__":
    main()
