"""demo/02_widest_path.py - widest path vs shortest path.

h1 to h4 is one hop across a 4 Mbps chord, or three hops across 10 Mbps ring
links, so the two metrics disagree.

Both policies here are capacity-aware, shortest prunes links that cannot carry
the request, exactly as widest does, so this compares two routing metrics, not
admission control against nothing.

Measured finding: on this topology the two admit the *same* number of requests
(checked at 12, 18, 24 and 30). What differs is utilisation.

    python demo/02_widest_path.py
"""

from _common import Demo, links, request, require_ready, reset


SEQUENCE = [("h1", "h4"), ("h3", "h6"), ("h2", "h5")] * 4
REQUEST_MBPS = 2


def main():
    demo = Demo("Widest path vs shortest path", "widest path against shortest path")
    require_ready()
    reset()

    demo.step("Same request, each policy: which path is chosen")
    for policy in ("shortest", "widest"):
        reset()
        reply = request(src="h1", dst="h4", bandwidth_mbps=REQUEST_MBPS,
                        policy=policy, idle_timeout=300, allow_preemption=False)
        demo.say(f"{policy:<9} {'-'.join(reply['flow']['path']):<14} "
                 f"bottleneck {reply['bottleneck_mbps']} Mbps")
    demo.note("unmetered, those paths carry 3.83 and 9.56 Mbps "
              "(tools/validate_topology.py)")
    demo.pause()

    # The per-request outcomes are not the point of this step — the totals are.
    # Twenty-four ADMITTED lines would bury them.
    demo.step(f"Same {len(SEQUENCE)} requests of {REQUEST_MBPS} Mbps under each policy")
    results = {}
    for policy in ("shortest", "widest"):
        reset()
        admitted = 0
        for src, dst in SEQUENCE:
            reply = request(src=src, dst=dst, bandwidth_mbps=REQUEST_MBPS,
                            policy=policy, idle_timeout=300, allow_preemption=False)
            admitted += bool(reply.get("ok"))
        results[policy] = (admitted, _utilisation())
        demo.say(f"{policy:<9} done")
    demo.pause()

    demo.step("Requests admitted")
    for policy, (admitted, _) in results.items():
        demo.say(f"{policy:<9} {admitted}/{len(SEQUENCE)}")
    demo.note("identical, and stays identical up to 30 requests")
    demo.pause()

    demo.step("Link utilisation")
    for policy, (_, util) in results.items():
        used = list(util.values())
        full = sum(1 for u in used if u > 0.99)
        demo.say(f"{policy:<9} peak {max(used) * 100:>3.0f}%   {full} link(s) at 100%")
    demo.note("same flows carried, different headroom left for the next request")

    reset()
    demo.done()


def _utilisation():
    return {
        link_id: (entry["used_mbps"] / entry["capacity_mbps"]) if entry["capacity_mbps"] else 0.0
        for link_id, entry in links().items()
    }


if __name__ == "__main__":
    main()
