"""test_state.py - reservation bookkeeping consistency tests.

The invariant every test here defends: the three structures are updated
together, or not at all.
"""

import unittest

from netslice.state import FlowState, InsufficientCapacity, NetworkState
from netslice.topology import default_topology


class StateTestCase(unittest.TestCase):
    def setUp(self):
        """Initialize a fresh NetworkState from the default topology."""
        self.state = NetworkState(default_topology())

    def place(self, src="h1", dst="h4", bandwidth=4.0, priority=1, links=("s1--s2",), path=("s1", "s2")):
        """Create a flow and reserve capacity on the given path and links.

        Args:
            src (str): Source host identifier.
            dst (str): Destination host identifier.
            bandwidth (float): Bandwidth to reserve in Mbps.
            priority (int): Flow priority (higher is more important).
            links (tuple[str]): Link IDs to reserve on.
            path (tuple[str]): Switch hop sequence for the flow.

        Returns:
            FlowState: The newly created flow object.
        """
        flow = self.state.new_flow(src, dst, bandwidth, priority)
        self.state.reserve(flow, path, links)
        return flow

    def assert_consistent(self):
        """Assert that residual plus charged capacity equals total for every link.

        Checks the fundamental invariant: residual[link] + sum of all flow
        bandwidths on that link == capacity[link].
        """
        for link_id, capacity in self.state.capacity.items():
            charged = sum(
                self.state.flows[f].bandwidth_mbps for f in self.state.link_flows[link_id]
            )
            self.assertAlmostEqual(self.state.residual[link_id] + charged, capacity, places=6)
            for flow_id in self.state.link_flows[link_id]:
                self.assertTrue(self.state.flows[flow_id].holds_capacity())


class ReserveReleaseTest(StateTestCase):
    def test_only_core_links_are_tracked(self):
        """Verify only 9 core links are tracked, not access links like h1--s1."""
        self.assertEqual(len(self.state.capacity), 9)
        self.assertNotIn("h1--s1", self.state.capacity)

    def test_reserve_charges_every_link(self):
        """Verify reserving a multi-link flow deducts bandwidth from each link."""
        self.place(bandwidth=3, links=("s1--s2", "s2--s3"), path=("s1", "s2", "s3"))
        self.assertAlmostEqual(self.state.residual["s1--s2"], 7)
        self.assertAlmostEqual(self.state.residual["s2--s3"], 7)
        self.assertAlmostEqual(self.state.residual["s3--s4"], 10)
        self.assert_consistent()

    def test_release_gives_capacity_back(self):
        """Verify releasing a flow restores all reserved capacity to links."""
        flow = self.place(bandwidth=3, links=("s1--s2",))
        path, links = self.state.release(flow.flow_id, FlowState.EXPIRED)
        self.assertEqual(links, ("s1--s2",))
        self.assertAlmostEqual(self.state.residual["s1--s2"], 10)
        self.assertEqual(self.state.link_flows["s1--s2"], set())
        self.assertIs(flow.state, FlowState.EXPIRED)
        self.assert_consistent()

    def test_double_release_is_harmless(self):
        """Verify releasing an already-released flow does not corrupt state."""
        flow = self.place(bandwidth=3, links=("s1--s2",))
        self.state.release(flow.flow_id, FlowState.EXPIRED)
        self.state.release(flow.flow_id, FlowState.REMOVED)
        self.assertAlmostEqual(self.state.residual["s1--s2"], 10)

    def test_overbooking_is_refused_atomically(self):
        """Verify that when a multi-link reservation fails, no partial charges remain."""
        self.place(bandwidth=8, links=("s1--s2", "s2--s3"), path=("s1", "s2", "s3"))
        flow = self.state.new_flow("h2", "h3", 5, 1)
        with self.assertRaises(InsufficientCapacity):
            self.state.reserve(flow, ("s1", "s2", "s3"), ("s1--s2", "s2--s3"))
        self.assertAlmostEqual(self.state.residual["s1--s2"], 2)
        self.assert_consistent()

    def test_force_allows_overbooking(self):
        """Verify force mode accepts overbooking and lets residual go negative."""
        self.place(bandwidth=8, links=("s1--s2",))
        flow = self.state.new_flow("h2", "h3", 5, 1)
        self.state.reserve(flow, ("s1", "s2"), ("s1--s2",), force=True)
        self.assertAlmostEqual(self.state.residual["s1--s2"], -3)

    def test_a_down_link_has_no_available_capacity(self):
        """Verify a link marked down reports zero available capacity."""
        self.state.set_link_down("s1--s2")
        self.assertEqual(self.state.available("s1--s2"), 0.0)
        self.assertAlmostEqual(self.state.residual["s1--s2"], 10)
        self.state.set_link_up("s1--s2")
        self.assertAlmostEqual(self.state.available("s1--s2"), 10)

    def test_link_down_returns_affected_flows_by_priority(self):
        """Verify set_link_down returns affected flows sorted by priority descending."""
        low = self.place(bandwidth=2, priority=1, links=("s1--s2",))
        high = self.place(src="h2", dst="h3", bandwidth=2, priority=9, links=("s1--s2",))
        self.place(src="h3", dst="h6", bandwidth=2, priority=5, links=("s3--s6",))
        affected = self.state.set_link_down("s1--s2")
        self.assertEqual([f.flow_id for f in affected], [high.flow_id, low.flow_id])

    def test_forget_refuses_a_live_flow(self):
        """Verify forget raises RuntimeError for active flows and removes expired ones."""
        flow = self.place()
        with self.assertRaises(RuntimeError):
            self.state.forget(flow.flow_id)
        self.state.release(flow.flow_id, FlowState.REMOVED)
        self.state.forget(flow.flow_id)
        self.assertNotIn(flow.flow_id, self.state.flows)

    def test_flow_ports_are_unique(self):
        """Verify each new flow is assigned a distinct destination port."""
        ports = {self.state.new_flow("h1", "h2", 1, 1).tp_dst for _ in range(20)}
        self.assertEqual(len(ports), 20)


class PreemptionTest(StateTestCase):
    def test_preemptable_counts_only_lower_priorities(self):
        """Verify preemptable_capacity includes residual plus lower-priority flow bandwidth."""
        self.place(bandwidth=3, priority=1, links=("s1--s2",))
        self.place(src="h2", dst="h3", bandwidth=4, priority=5, links=("s1--s2",))
        self.assertAlmostEqual(self.state.preemptable_capacity("s1--s2", 3), 6)
        self.assertAlmostEqual(self.state.preemptable_capacity("s1--s2", 1), 3)
        self.assertAlmostEqual(self.state.preemptable_capacity("s1--s2", 9), 10)

    def test_hold_down_hides_a_recent_victim(self):
        """Verify a flow in hold-down is excluded from preemptable capacity until the timer expires."""
        flow = self.place(bandwidth=3, priority=1, links=("s1--s2",))
        self.state.mark_hold_down(flow.flow_id, now=1000.0)
        self.assertAlmostEqual(self.state.preemptable_capacity("s1--s2", 5, now=1001.0), 7)
        self.assertAlmostEqual(self.state.preemptable_capacity("s1--s2", 5, now=1010.0), 10)

    def test_fewest_prefers_low_priority_then_fat_flows(self):
        """Verify fewest tie-break selects the single largest victim that covers the deficit."""
        self.place(bandwidth=1, priority=1, links=("s1--s2",))
        fat = self.place(src="h2", dst="h3", bandwidth=4, priority=1, links=("s1--s2",))
        self.place(src="h3", dst="h4", bandwidth=4, priority=2, links=("s1--s2",))
        victims = self.state.victims("s1--s2", 4, priority=3, tie_break="fewest")
        self.assertEqual([v.flow_id for v in victims], [fat.flow_id])

    def test_best_fit_prefers_the_smallest_sufficient_victim(self):
        """Verify best_fit tie-break selects the smallest victim that covers the deficit."""
        thin = self.place(bandwidth=3, priority=1, links=("s1--s2",))
        self.place(src="h2", dst="h3", bandwidth=5, priority=1, links=("s1--s2",))
        best = self.state.victims("s1--s2", 5, priority=3, tie_break="best_fit")
        fewest = self.state.victims("s1--s2", 5, priority=3, tie_break="fewest")
        self.assertEqual([v.flow_id for v in best], [thin.flow_id])
        self.assertNotEqual([v.flow_id for v in fewest], [thin.flow_id])

    def test_no_victims_needed_when_capacity_is_free(self):
        """Verify an empty victim list is returned when enough residual capacity exists."""
        self.assertEqual(self.state.victims("s1--s2", 4, priority=3), [])

    def test_unsatisfiable_deficit_returns_none(self):
        """Verify None is returned when no combination of victims can cover the deficit."""
        self.place(bandwidth=9, priority=5, links=("s1--s2",))
        self.assertIsNone(self.state.victims("s1--s2", 8, priority=3))

    def test_exclude_prevents_double_counting_a_victim(self):
        """Verify the exclude set prevents a flow from being selected as a victim twice."""
        victim = self.place(bandwidth=8, priority=1, links=("s1--s2",))
        self.assertEqual(
            self.state.victims("s1--s2", 8, priority=5, exclude={victim.flow_id}), None
        )


if __name__ == "__main__":
    unittest.main()
