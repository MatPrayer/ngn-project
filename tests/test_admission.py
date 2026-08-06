"""test_admission.py - admission control end to end, minus OpenFlow.

These are the decisions the demo is judged on, accept, reject, preempt, and
they are all reachable without a lab, so a regression shows up in a second
rather than after a two-minute deploy.
"""

import unittest

from netslice import admission, routing
from netslice.state import FlowState, NetworkState
from netslice.topology import default_topology


class AdmissionTestCase(unittest.TestCase):
    def setUp(self):
        self.topology = default_topology()
        self.adj = routing.adjacency(self.topology)
        self.state = NetworkState(self.topology)

    def evaluate(self, src="s1", dst="s4", bandwidth=4.0, priority=1, **kwargs):
        return admission.evaluate(self.state, self.adj, src, dst, bandwidth, priority, **kwargs)

    def admit(self, src="s1", dst="s4", bandwidth=4.0, priority=1, **kwargs):
        """Evaluate and apply, the way the controller does."""
        decision = self.evaluate(src, dst, bandwidth, priority, **kwargs)
        self.assertTrue(decision.accepted, decision.reason)
        for victim_id in decision.victims:
            self.state.release(victim_id, FlowState.PREEMPTED)
        flow = self.state.new_flow("h1", "h4", bandwidth, priority)
        self.state.reserve(flow, decision.path.switches, decision.path.links,
                           force=decision.forced)
        return flow, decision

    def fill(self, link_id, bandwidth, priority=1):
        """Park a flow on one link to eat capacity."""
        flow = self.state.new_flow("h1", "h2", bandwidth, priority)
        link = self.topology.link_by_id(link_id)
        self.state.reserve(flow, (link.a, link.b), (link_id,))
        return flow


class BasicAdmissionTest(AdmissionTestCase):
    def test_accepts_on_free_capacity_via_the_widest_path(self):
        decision = self.evaluate(bandwidth=4)
        self.assertTrue(decision.accepted)
        self.assertEqual(decision.path.switches, ("s1", "s2", "s3", "s4"))
        self.assertFalse(decision.preemption_used)

    def test_rejects_what_no_path_can_carry(self):
        decision = self.evaluate(bandwidth=25)
        self.assertFalse(decision.accepted)
        self.assertIn("25", decision.reason)

    def test_reservations_accumulate_and_eventually_exhaust_the_network(self):
        for _ in range(5):
            self.admit(bandwidth=4, priority=1, allow_preemption=False)
        decision = self.evaluate(bandwidth=4, allow_preemption=False)
        self.assertFalse(decision.accepted)

    def test_shortest_policy_picks_the_narrow_chord(self):
        decision = self.evaluate(bandwidth=4, policy="shortest")
        self.assertEqual(decision.path.switches, ("s1", "s4"))

    def test_shortest_policy_still_respects_capacity(self):
        """Capacity-aware even in the baseline policy, so the comparison is of metrics
        rather than comparing admission control against nothing."""
        self.fill("s1--s4", 4)
        decision = self.evaluate(bandwidth=4, policy="shortest")
        self.assertTrue(decision.accepted)
        self.assertNotEqual(decision.path.switches, ("s1", "s4"))

    def test_without_admission_control_everything_is_accepted(self):
        self.fill("s1--s4", 4)
        self.fill("s1--s2", 10)
        self.fill("s1--s6", 10)
        decision = self.evaluate(bandwidth=20, admission_control=False)
        self.assertTrue(decision.accepted)
        self.assertTrue(decision.forced)

    def test_a_down_link_is_never_chosen(self):
        self.state.set_link_down("s1--s2")
        decision = self.evaluate(bandwidth=6)
        self.assertTrue(decision.accepted)
        self.assertNotIn("s1--s2", decision.path.links)

    def test_partitioned_source_is_rejected(self):
        for link_id in ("s1--s2", "s1--s4", "s1--s6"):
            self.state.set_link_down(link_id)
        self.assertFalse(self.evaluate(bandwidth=1).accepted)


class PreemptionAdmissionTest(AdmissionTestCase):
    def saturate_s1_s4(self, priority=1):
        """Leave the s1-s4 chord full and every alternative too narrow, so the
        only way through is by preempting."""
        self.fill("s1--s4", 4, priority=priority)
        self.fill("s1--s2", 10, priority=priority)
        self.fill("s1--s6", 10, priority=priority)

    def test_high_priority_preempts_when_nothing_is_free(self):
        self.saturate_s1_s4(priority=1)
        self.assertFalse(self.evaluate(bandwidth=4, priority=5, allow_preemption=False).accepted)

        decision = self.evaluate(bandwidth=4, priority=5)
        self.assertTrue(decision.accepted)
        self.assertTrue(decision.preemption_used)
        self.assertEqual(len(decision.victims), 1)

    def test_equal_priority_cannot_preempt(self):
        self.saturate_s1_s4(priority=5)
        self.assertFalse(self.evaluate(bandwidth=4, priority=5).accepted)

    def test_lower_priority_cannot_preempt(self):
        self.saturate_s1_s4(priority=5)
        self.assertFalse(self.evaluate(bandwidth=4, priority=1).accepted)

    def test_victims_are_credited_across_shared_links(self):
        """One victim crossing two links of the chosen path must be counted
        once, not sacrificed twice."""
        victim = self.state.new_flow("h1", "h3", 8, 1)
        self.state.reserve(victim, ("s1", "s2", "s3"), ("s1--s2", "s2--s3"))
        self.fill("s1--s4", 4, priority=1)
        self.fill("s1--s6", 10, priority=1)

        decision = self.evaluate(src="s1", dst="s3", bandwidth=9, priority=5)
        self.assertTrue(decision.accepted, decision.reason)
        self.assertEqual(decision.victims, (victim.flow_id,))

    def test_hold_down_protects_a_recent_victim(self):
        self.saturate_s1_s4(priority=1)
        for flow in self.state.active_flows():
            self.state.mark_hold_down(flow.flow_id, now=1000.0)
        self.assertFalse(self.evaluate(bandwidth=4, priority=5, now=1001.0).accepted)
        self.assertTrue(self.evaluate(bandwidth=4, priority=5, now=1010.0).accepted)

    def test_applying_a_preemption_decision_keeps_the_books_straight(self):
        self.saturate_s1_s4(priority=1)
        flow, decision = self.admit(bandwidth=4, priority=5)
        self.assertTrue(decision.preemption_used)
        for link_id, capacity in self.state.capacity.items():
            charged = sum(
                self.state.flows[f].bandwidth_mbps for f in self.state.link_flows[link_id]
            )
            self.assertAlmostEqual(self.state.residual[link_id] + charged, capacity, places=6)
            self.assertGreaterEqual(self.state.residual[link_id], -1e-9)


if __name__ == "__main__":
    unittest.main()
