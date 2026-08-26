"""test_routing.py - widest-path and shortest-path tests.

    python -m unittest discover -s tests

No lab, no switches, no controller: routing.py is pure graph code, which is the
whole reason it lives in its own module.
"""

import unittest

from netslice import routing
from netslice.topology import default_topology


def line(*capacities):
    """Build a linear chain graph a-b-c-... with the given link capacities.

    Args:
        *capacities (float): Capacity values for each consecutive link.

    Returns:
        tuple: A 3-tuple of (adjacency dict, widths dict, node names list).
    """
    names = [chr(ord("a") + i) for i in range(len(capacities) + 1)]
    adj = {n: [] for n in names}
    widths = {}
    for i, capacity in enumerate(capacities):
        link_id = f"{names[i]}--{names[i + 1]}"
        adj[names[i]].append((names[i + 1], link_id))
        adj[names[i + 1]].append((names[i], link_id))
        widths[link_id] = capacity
    return adj, widths, names


class WidestPathTest(unittest.TestCase):
    def setUp(self):
        """Initialize topology, adjacency map, and core link capacities."""
        self.topology = default_topology()
        self.adj = routing.adjacency(self.topology)
        self.capacity = {l.id: l.capacity_mbps for l in self.topology.core_links()}

    def width(self, link_id):
        """Return the capacity of a link.

        Args:
            link_id (str): Link identifier.

        Returns:
            float: The link capacity in Mbps.
        """
        return self.capacity[link_id]

    def test_adjacency_excludes_access_links(self):
        """Verify adjacency contains only core switches, not hosts."""
        self.assertEqual(set(self.adj), set(self.topology.switches))
        for neighbours in self.adj.values():
            for name, _ in neighbours:
                self.assertIn(name, self.topology.switches)

    def test_headline_case_s1_to_s4(self):
        """Verify widest path from s1 to s4 takes the 3-hop ring over the narrow chord."""
        path = routing.widest_path(self.adj, "s1", "s4", self.width)
        self.assertEqual(path.switches, ("s1", "s2", "s3", "s4"))
        self.assertEqual(path.bottleneck, 10)
        self.assertEqual(path.hops, 3)

    def test_shortest_disagrees_with_widest(self):
        """Verify shortest and widest paths differ, proving the policy choice matters."""
        widest = routing.widest_path(self.adj, "s1", "s4", self.width)
        shortest = routing.shortest_path(self.adj, "s1", "s4", self.width)
        self.assertEqual(shortest.switches, ("s1", "s4"))
        self.assertEqual(shortest.bottleneck, 4)
        self.assertNotEqual(widest.switches, shortest.switches)
        self.assertGreater(widest.bottleneck, shortest.bottleneck)

    def test_minimum_prunes_infeasible_links(self):
        """Verify no path is returned when no path meets the minimum capacity requirement."""
        self.assertIsNone(routing.widest_path(self.adj, "s1", "s4", self.width, minimum=25))
        path = routing.widest_path(self.adj, "s2", "s5", self.width, minimum=20)
        self.assertEqual(path.switches, ("s2", "s5"))

    def test_zero_width_links_are_unusable(self):
        """Verify links with zero residual capacity are excluded from path computation."""
        exhausted = dict(self.capacity)
        for link_id in ("s1--s2", "s1--s4", "s1--s6"):
            exhausted[link_id] = 0.0
        self.assertIsNone(routing.widest_path(self.adj, "s1", "s4", exhausted.get))

    def test_source_equals_destination(self):
        """Verify a zero-hop path is returned when source equals destination."""
        path = routing.widest_path(self.adj, "s3", "s3", self.width)
        self.assertEqual(path.switches, ("s3",))
        self.assertEqual(path.links, ())

    def test_unknown_node(self):
        """Verify None is returned when the destination does not exist in the graph."""
        self.assertIsNone(routing.widest_path(self.adj, "s1", "s99", self.width))

    def test_tie_break_is_hop_count(self):
        """Verify equally wide paths are broken by hop count (fewer hops wins)."""
        adj = {
            "a": [("b", "ab"), ("c", "ac")],
            "b": [("a", "ab"), ("d", "bd")],
            "c": [("a", "ac"), ("e", "ce")],
            "e": [("c", "ce"), ("d", "ed")],
            "d": [("b", "bd"), ("e", "ed")],
        }
        widths = {"ab": 10, "bd": 10, "ac": 10, "ce": 10, "ed": 10}
        path = routing.widest_path(adj, "a", "d", widths.get)
        self.assertEqual(path.switches, ("a", "b", "d"))

    def test_widest_prefers_capacity_over_length(self):
        """Verify widest path chooses a longer route when the direct link is narrower."""
        adj, widths, _ = line(1, 1)
        adj["a"].append(("c", "ac"))
        adj["c"].append(("a", "ac"))
        widths["ac"] = 1
        self.assertEqual(routing.widest_path(adj, "a", "c", widths.get).hops, 1)
        widths["ac"] = 0.5
        self.assertEqual(routing.widest_path(adj, "a", "c", widths.get).hops, 2)

    def test_bottleneck_is_the_minimum_of_the_path(self):
        """Verify the bottleneck value equals the minimum link capacity along the path."""
        adj, widths, _ = line(10, 3, 7)
        path = routing.widest_path(adj, "a", "d", widths.get)
        self.assertEqual(path.bottleneck, 3)
        self.assertEqual(path.links, ("a--b", "b--c", "c--d"))

    def test_find_path_rejects_unknown_policy(self):
        """Verify find_path raises ValueError for an unrecognized routing policy."""
        with self.assertRaises(ValueError):
            routing.find_path("cheapest", self.adj, "s1", "s2", self.width)


if __name__ == "__main__":
    unittest.main()
