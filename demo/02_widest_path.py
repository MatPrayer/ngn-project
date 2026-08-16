"""Demo 2 — widest path vs shortest path.

 The topology is built so the two metrics disagree:
h1 to h4 is one hop across a 4 Mbps chord, or three hops across 10 Mbps ring
links.

Note what is *not* being compared. Both policies here are capacity-aware —
shortest path prunes links that cannot carry the request, exactly as widest
does — so this contrasts two routing metrics, not admission control against
nothing. On a single request that both can satisfy the difference is which path
is chosen; the effect on how much the network can carry only appears over a
sequence.

    python demo/02_widest_path.py
"""

from _common import Demo, require_ready, reset, links


# Pairs whose chord and ring routes disagree: h1->h4 (4 vs 10 Mbps) and
# h3->h6 (6 vs 10). h2->h5 is included as a control — its chord is 20 Mbps, so
# both policies choose it and it should behave identically under each.
SEQUENCE = [("h1", "h4"), ("h3", "h6"), ("h2", "h5")] * 4
REQUEST_MBPS = 2


def main():
    demo = Demo(
        "Widest path vs shortest path",
        "widest path against shortest path",
        "fewest hops is not the same as most capacity",
    )
    require_ready()
    reset()

    demo.step("The disagreement, on one request")
    for policy in ("shortest", "widest"):
        reset()
        reply = demo.allocate("h1", "h4", REQUEST_MBPS, policy=policy,
                              idle_timeout=300, allow_preemption=False)
        demo.say(f"    {policy:<9} -> {'-'.join(reply['flow']['path']):<14} "
                 f"bottleneck {reply['bottleneck_mbps']} Mbps")
    demo.note("Same request, same network, different route. Shortest takes the")
    demo.note("1-hop chord; widest goes the long way round for more headroom.")
    demo.note("Measured without meters in tools/validate_topology.py, those two")
    demo.note("paths carry 3.83 and 9.56 Mbps respectively — a 2.5x difference")
    demo.note("produced purely by path choice.")
    demo.pause()

    demo.step(f"Now offer the same {len(SEQUENCE)} requests under each policy")
    demo.note(f"{REQUEST_MBPS} Mbps each, across three host pairs, identical order.")
    results = {}
    for policy in ("shortest", "widest"):
        reset()
        admitted = 0
        for src, dst in SEQUENCE:
            reply = demo.allocate(src, dst, REQUEST_MBPS, policy=policy,
                                  idle_timeout=300, allow_preemption=False)
            admitted += bool(reply.get("ok"))
        results[policy] = (admitted, _utilisation())
        demo.say(dim_rule())
    demo.pause()

    demo.step("Accepted requests")
    for policy, (admitted, _) in results.items():
        demo.say(f"{policy:<9} {admitted}/{len(SEQUENCE)} admitted")
    demo.note("The same. Worth saying out loud rather than glossing over: on this")
    demo.note("topology, both policies admit the same number of requests, and they")
    demo.note("keep doing so as the load rises (checked up to 30 requests).")
    demo.note("Widest path is not a way to squeeze in more flows here.")
    demo.pause()

    demo.step("Where the difference actually shows: headroom")
    for policy, (_, util) in results.items():
        used = list(util.values())
        saturated = sum(1 for u in used if u > 0.99)
        demo.say(f"{policy:<9} peak link {max(used) * 100:>3.0f}% utilised, "
                 f"{saturated} link(s) completely full")
    demo.note("Shortest path packs the chords until one is at 100%; widest path")
    demo.note("leaves every link with room. Same flows carried, very different")
    demo.note("exposure to the next request — or to a link failure, which has")
    demo.note("somewhere to reroute to only if somewhere has spare capacity.")
    demo.note("That is the honest finding for this topology: the metric")
    demo.note("changes *where* traffic goes and how much slack is left, not how")
    demo.note("much the network can ultimately carry.")

    reset()
    demo.done("Next: demo/03_preemption.py")


def _utilisation():
    return {
        link_id: (entry["used_mbps"] / entry["capacity_mbps"]) if entry["capacity_mbps"] else 0.0
        for link_id, entry in links().items()
    }


def dim_rule():
    from _common import dim
    return dim("  " + "·" * 50)


if __name__ == "__main__":
    main()
