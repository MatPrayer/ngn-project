"""controller.py - SDN controller: OpenFlow plumbing around the core.

Run it from the repo root with the project venv, after the lab is up:

    .venv/bin/python -m netslice.controller

It listens for switches on tcp:6653 and for commands on tcp:127.0.0.1:9000
(one JSON object per line, see `netslice/client.py`).

Division of labour, deliberately strict:

* `state.py`     what is reserved where          (no OpenFlow)
* `routing.py`   which path                      (no OpenFlow, no state)
* `admission.py` whether to admit, and at whose expense (pure, no mutation)
* this module    turning a decision into flow mods, and turning switch events
                 back into decisions

Everything above this file can be unit tested without a lab; this file is the
only part that needs one.

The controller is **proactive**: hosts have static ARP tables and every switch
runs in secure fail mode with a drop table-miss, so nothing is ever punted to
the controller. Traffic moves only where a flow has been explicitly admitted,
which is what makes the admission-control guarantee real rather than advisory.
"""

from __future__ import annotations

import itertools
import json
import socket
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Deque, Dict, List, Optional, Sequence

from os_ken.base import app_manager
from os_ken.controller import ofp_event
from os_ken.controller.handler import (
    CONFIG_DISPATCHER,
    DEAD_DISPATCHER,
    MAIN_DISPATCHER,
    set_ev_cls,
)
from os_ken.lib import hub
from os_ken.ofproto import ofproto_v1_3

from netslice import admission, dashboard, routing
from netslice.state import (
    IPPROTO_TCP,
    IPPROTO_UDP,
    FlowEntry,
    FlowState,
    NetworkState,
)
from netslice.topology import OF_PORT, default_topology, set_link

ETH_TYPE_IP = 0x0800





FLOW_PRIORITY = 100
TABLE_MISS_PRIORITY = 0

CONTROL_ADDR = ("127.0.0.1", 9000)
DASHBOARD_ADDR = ("127.0.0.1", 8080)



EVENT_BUFFER = 500



STATS_INTERVAL = 2.0

ROOT = Path(__file__).resolve().parent.parent
EVENT_LOG = ROOT / "controller_events.jsonl"

DEFAULT_IDLE_TIMEOUT = 30
DEFAULT_HARD_TIMEOUT = 0
DEFAULT_PRIORITY = 1


def _spawn_daemon(target, *args) -> threading.Thread:
    """Start a background thread that will not outlive a dying process.

    ``hub.spawn`` returns a non-daemon thread that starts immediately, so
    any looping function would hang a failed controller at startup. This
    wrapper makes the thread a daemon instead.

    Args:
        target: Callable to run in the new thread.
        *args: Positional arguments passed to *target*.

    Returns:
        threading.Thread: The started daemon thread.
    """
    thread = threading.Thread(target=target, args=args, daemon=True)
    thread.start()
    return thread


class NetSliceController(app_manager.OSKenApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        """Initialise the controller: state, sockets, and background threads.

        Sets up the topology, network state, routing adjacency, OpenFlow
        datapath tracking, the shared lock, event logging, flow counters,
        and the control/dashboard/stats daemon threads.

        Args:
            *args: Passed through to ``OSKenApp.__init__``.
            **kwargs: Passed through to ``OSKenApp.__init__``.
        """
        super().__init__(*args, **kwargs)
        self.topology = default_topology()
        self.adj = routing.adjacency(self.topology)
        self.state = NetworkState(self.topology)

        self.datapaths: Dict[int, object] = {}
        self._by_cookie: Dict[int, str] = {}






        self.lock = hub.Semaphore()









        self.event_log: Deque[dict] = deque(maxlen=EVENT_BUFFER)
        self._event_seq = itertools.count(1)


        self.flow_stats: Dict[int, dict] = {}

        EVENT_LOG.write_text("")
        self.control_server = _spawn_daemon(self._serve_control)
        self.stats_poller = _spawn_daemon(self._poll_flow_stats)
        self.dashboard_server = _spawn_daemon(dashboard.serve, self, DASHBOARD_ADDR)



    def record(self, kind: str, **fields) -> dict:
        """Append one event to the in-memory buffer and the on-disk log.

        The dashboard polls the in-memory tail; the report plots read the
        file.

        Args:
            kind: Event category (e.g. ``"flow_admitted"``,
                ``"link_down"``).
            **fields: Arbitrary key/value fields describing the event.

        Returns:
            dict: The constructed event entry including ``seq``, ``kind``,
            ``t``, and ``mono`` fields.
        """
        entry = {
            "seq": next(self._event_seq),
            "kind": kind,
            "t": time.time(),
            "mono": time.monotonic(),
            **fields,
        }
        with EVENT_LOG.open("a") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
        self.event_log.append(entry)
        self.logger.info("EVENT %s %s", kind, fields)
        return entry

    def events_since(self, seq: int = 0, limit: int = 200) -> List[dict]:
        """Return buffered events with sequence number greater than *seq*.

        Args:
            seq: Return events strictly after this sequence number.
            limit: Maximum number of events to return.

        Returns:
            list[dict]: The most recent matching events.
        """


        return [e for e in list(self.event_log) if e["seq"] > seq][-limit:]



    def _poll_flow_stats(self) -> None:
        """Ask each ingress switch for flow counters every STATS_INTERVAL.

        This is what makes the dashboard's remaining-TTL honest: ``idle_timeout``
        resets whenever traffic matches, so watching the packet counter stop
        moving is the only way to know how long a flow has been quiet. The poll
        also gives live per-flow throughput.

        Args:
            None.

        Returns:
            None. Runs forever in a daemon thread.
        """
        while True:
            hub.sleep(STATS_INTERVAL)
            try:
                ingresses = {f.ingress for f in self.state.active_flows() if f.ingress}
                for switch in ingresses:
                    dp = self._datapath(switch)
                    if dp is None:
                        continue
                    parser = dp.ofproto_parser
                    dp.send_msg(parser.OFPFlowStatsRequest(dp))
            except Exception:
                self.logger.exception("flow stats poll failed")

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def flow_stats_reply_handler(self, ev):
        """Handle an OFPFlowStatsReply and refresh per-flow counters.

        Only the ingress switch's forward entry is counted, so ACKs and
        downstream copies are not double-counted. Handles counter resets
        from reinstalling entries after a reroute.

        Args:
            ev: os-ken event carrying the flow stats reply.
        """
        switch = self._switch_name(ev.msg.datapath.id)
        now = time.monotonic()

        for entry in ev.msg.body:
            flow_id = self._by_cookie.get(entry.cookie)
            if flow_id is None:
                continue
            flow = self.state.flows.get(flow_id)


            if flow is None or switch != flow.ingress:
                continue
            if entry.match.get("ipv4_src") != self.topology.host_ip(flow.src):
                continue

            previous = self.flow_stats.get(entry.cookie)
            moved = previous is None or entry.packet_count != previous["packets"]
            elapsed = now - previous["at"] if previous else 0.0





            reset_counters = previous is not None and entry.byte_count < previous["bytes"]
            if reset_counters:
                throughput = previous["throughput_mbps"]
            elif previous and elapsed > 0:
                throughput = round((entry.byte_count - previous["bytes"]) * 8 / elapsed / 1e6, 3)
            else:
                throughput = 0.0
            self.flow_stats[entry.cookie] = {
                "packets": entry.packet_count,
                "bytes": entry.byte_count,
                "duration_sec": entry.duration_sec,
                "at": now,
                "last_active": now if moved else (previous["last_active"] if previous else now),
                "throughput_mbps": max(throughput, 0.0),
            }

    def _flow_view(self, flow: FlowEntry) -> dict:
        """Build the dashboard's view of a flow: entry plus live counters.

        Args:
            flow: The flow entry to render.

        Returns:
            dict: The flow's ``to_dict()`` payload augmented with
            ``throughput_mbps``, ``bytes``, ``idle_for_sec``,
            ``remaining_idle_sec``, and ``remaining_hard_sec`` (any of
            which may be None).
        """
        payload = flow.to_dict()
        stats = self.flow_stats.get(flow.cookie)
        idle_for = round(time.monotonic() - stats["last_active"], 1) if stats else None

        payload.update(
            throughput_mbps=stats["throughput_mbps"] if stats else None,
            bytes=stats["bytes"] if stats else None,
            idle_for_sec=idle_for,


            remaining_idle_sec=(
                max(0.0, round(flow.idle_timeout - idle_for, 1))
                if flow.idle_timeout and idle_for is not None
                else None
            ),
            remaining_hard_sec=(
                max(0.0, round(flow.hard_timeout - stats["duration_sec"], 1))
                if flow.hard_timeout and stats
                else None
            ),
        )
        return payload



    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        """Register a newly connected switch and rebuild its installed flows.

        Wipes the datapath and reinstalls the table-miss plus any active
        flows that traverse this switch, so a reconnecting switch never
        holds entries nothing accounts for.

        Args:
            ev: os-ken switch-features event.
        """
        dp = ev.msg.datapath
        self.datapaths[dp.id] = dp
        switch = self._switch_name(dp.id)
        self.record("switch_up", dpid=dp.id, switch=switch)





        self._wipe(dp)
        self._install_table_miss(dp)
        with self.lock:
            for flow in self.state.active_flows():
                if switch in flow.path:
                    self._install_flow(flow, only=[switch])

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def state_change_handler(self, ev):
        """Track switch connection state and record disconnections.

        Args:
            ev: os-ken state-change event.
        """
        dp = ev.datapath
        if ev.state == DEAD_DISPATCHER and dp.id in self.datapaths:
            del self.datapaths[dp.id]
            self.record("switch_down", dpid=dp.id, switch=self._switch_name(dp.id))

    @set_ev_cls(ofp_event.EventOFPErrorMsg, [CONFIG_DISPATCHER, MAIN_DISPATCHER])
    def error_handler(self, ev):
        """Record an OpenFlow error message from a switch.

        Args:
            ev: os-ken error event.
        """
        msg = ev.msg
        self.record("of_error", dpid=msg.datapath.id, type=msg.type, code=msg.code)

    def _wipe(self, dp) -> None:
        """Delete every flow and meter from a datapath.

        Args:
            dp: The OpenFlow datapath object.
        """
        ofp, parser = dp.ofproto, dp.ofproto_parser
        dp.send_msg(
            parser.OFPFlowMod(
                datapath=dp,
                command=ofp.OFPFC_DELETE,
                table_id=ofp.OFPTT_ALL,
                out_port=ofp.OFPP_ANY,
                out_group=ofp.OFPG_ANY,
                match=parser.OFPMatch(),
            )
        )
        dp.send_msg(
            parser.OFPMeterMod(
                datapath=dp, command=ofp.OFPMC_DELETE, meter_id=ofp.OFPM_ALL
            )
        )

    def _install_table_miss(self, dp) -> None:
        """Install a priority-0 drop entry that counts unmatched traffic.

        Redundant against secure fail mode, but the entry's packet counter is
        exactly "traffic offered by hosts that was never admitted".

        Args:
            dp: The OpenFlow datapath object.
        """
        parser = dp.ofproto_parser
        dp.send_msg(
            parser.OFPFlowMod(
                datapath=dp,
                priority=TABLE_MISS_PRIORITY,
                match=parser.OFPMatch(),
                instructions=[],
            )
        )



    def request_flow(
        self,
        src: str,
        dst: str,
        bandwidth_mbps: float,
        priority: int = DEFAULT_PRIORITY,
        idle_timeout: int = DEFAULT_IDLE_TIMEOUT,
        hard_timeout: int = DEFAULT_HARD_TIMEOUT,
        proto: str = "tcp",
        policy: str = "widest",
        allow_preemption: bool = True,
        tie_break: str = "fewest",
        admission_control: bool = True,
    ) -> dict:
        """Admit (or refuse) one flow request end-to-end.

        Validates the request, runs admission, releases any preempted
        victims, reserves capacity, installs OpenFlow entries and meters,
        and reroutes victims. This is what the dashboard and control socket
        call.

        Args:
            src: Source host name.
            dst: Destination host name.
            bandwidth_mbps: Requested bandwidth in Mbps.
            priority: Flow priority (higher = more important).
            idle_timeout: Seconds of inactivity before expiry.
            hard_timeout: Maximum lifetime in seconds (0 = none).
            proto: ``"tcp"`` or ``"udp"``.
            policy: ``"widest"`` or ``"shortest"``.
            allow_preemption: Whether lower-priority flows may be evicted.
            tie_break: Victim-selection tie-break strategy.
            admission_control: Whether admission control is enforced (vs
                the baseline).

        Returns:
            dict: Controller reply, ``{"ok": True, "flow": ..., ...}`` on
            success, or ``{"ok": False, "reason": ...}`` on refusal.
        """
        hosts = self.topology.hosts
        if src not in hosts or dst not in hosts:
            return {"ok": False, "reason": f"unknown host: {src if src not in hosts else dst}"}
        if src == dst:
            return {"ok": False, "reason": "source and destination are the same host"}
        if bandwidth_mbps <= 0:
            return {"ok": False, "reason": "bandwidth must be positive"}

        src_switch, dst_switch = hosts[src], hosts[dst]

        with self.lock:
            decision = admission.evaluate(
                self.state,
                self.adj,
                src_switch,
                dst_switch,
                bandwidth_mbps,
                priority,
                policy=policy,
                allow_preemption=allow_preemption,
                tie_break=tie_break,
                admission_control=admission_control,
            )
            if not decision.accepted:
                self.record(
                    "request_rejected",
                    src=src, dst=dst, bandwidth_mbps=bandwidth_mbps,
                    priority=priority, reason=decision.reason,
                )
                return {"ok": False, "reason": decision.reason}

            missing = [s for s in decision.path.switches if self._datapath(s) is None]
            if missing:
                self.record("request_rejected", src=src, dst=dst, reason=f"switches offline: {missing}")
                return {"ok": False, "reason": f"switches not connected: {', '.join(missing)}"}

            flow = self.state.new_flow(
                src, dst, bandwidth_mbps, priority,
                idle_timeout=idle_timeout,
                hard_timeout=hard_timeout,
                ip_proto=IPPROTO_UDP if proto.lower() == "udp" else IPPROTO_TCP,
                policy=policy,
                tie_break=tie_break,
            )



            victims = []
            for flow_id in decision.victims:
                victim = self.state.flows[flow_id]
                old_path, _ = self.state.release(flow_id, FlowState.PREEMPTED)
                victims.append((victim, old_path))
                self.record(
                    "preempted",
                    flow_id=flow_id, by=flow.flow_id,
                    priority=victim.priority, bandwidth_mbps=victim.bandwidth_mbps,
                    path=list(old_path),
                )

            self.state.reserve(
                flow, decision.path.switches, decision.path.links, force=decision.forced
            )
            self._by_cookie[flow.cookie] = flow.flow_id
            self._install_flow(flow)

            self.record(
                "flow_admitted",
                flow_id=flow.flow_id, src=src, dst=dst,
                bandwidth_mbps=bandwidth_mbps, priority=priority,
                tp_dst=flow.tp_dst, path=list(flow.path),
                bottleneck_mbps=decision.path.to_dict()["bottleneck_mbps"],
                policy=policy, preemption_used=decision.preemption_used,
                forced=decision.forced,
            )



            for victim, old_path in victims:
                self._replace_path(
                    victim, old_path,
                    allow_preemption=False,
                    cause=f"preempted by {flow.flow_id}",
                )

            return {
                "ok": True,
                "flow": flow.to_dict(),
                "bottleneck_mbps": decision.path.to_dict()["bottleneck_mbps"],
                "preempted": list(decision.victims),
                "reason": decision.reason,
                "iperf": self._iperf_hint(flow),
            }

    def remove_flow(self, flow_id: str) -> dict:
        """Tear down one flow by ID.

        Args:
            flow_id: Flow identifier (e.g. ``"f3"``).

        Returns:
            dict: ``{"ok": True, "flow_id": ...}`` on success, or
            ``{"ok": False, "reason": ...}`` if the flow is unknown.
        """
        with self.lock:
            flow = self.state.flows.get(flow_id)
            if flow is None:
                return {"ok": False, "reason": f"unknown flow {flow_id}"}
            path, _ = self.state.release(flow_id, FlowState.REMOVED)
            self._delete_flow(flow, path or ())
            self._by_cookie.pop(flow.cookie, None)
            self.record("flow_removed", flow_id=flow_id, path=list(path))
            return {"ok": True, "flow_id": flow_id}

    def clear_flows(self) -> dict:
        """Tear down every flow and clear the flow table.

        Returns:
            dict: ``{"ok": True}``.
        """
        for flow_id in list(self.state.flows):
            self.remove_flow(flow_id)
        with self.lock:
            self.state.flows.clear()
            self._by_cookie.clear()
        return {"ok": True}

    def list_flows(self) -> dict:
        """List all known flows with live counters.

        Returns:
            dict: ``{"ok": True, "flows": [...]}``.
        """
        with self.lock:
            return {"ok": True, "flows": [self._flow_view(f) for f in self.state.flows.values()]}

    def link_status(self) -> dict:
        """Report per-link capacity, residual, and membership.

        Returns:
            dict: ``{"ok": True, "links": {...}}``, see
            :meth:`netslice.state.NetworkState.utilisation`.
        """
        with self.lock:
            return {"ok": True, "links": self.state.utilisation()}

    def set_link_state(self, link_id: str, up: bool) -> dict:
        """Bring one end of a core link administratively up or down.

        Deliberately takes no lock: it touches no ``NetworkState``, and the
        Kathara exec underneath is slow enough that holding the lock across it
        would stall every OpenFlow handler and dashboard poll. The controller
        learns the outcome the same way it learns about a real failure, from
        the ``OFPT_PORT_STATUS`` the switch sends back (D14).

        Args:
            link_id: Link identifier, as in ``"s2--s3"``.
            up: ``True`` to restore the link, ``False`` to break it.

        Returns:
            dict: ``{"ok": True, "link": ..., "up": ..., "switch": ...,
            "iface": ...}``, or ``{"ok": False, "reason": ...}`` if the link
            is unknown or is an access link.
        """
        try:
            link = self.topology.link_by_id(link_id)
        except KeyError:
            return {"ok": False, "reason": f"unknown link {link_id!r}"}
        try:
            switch, index = set_link(link.a, link.b, up=up)
        except ValueError as exc:
            return {"ok": False, "reason": str(exc)}
        except Exception as exc:
            self.logger.exception("set_link %s up=%s failed", link_id, up)
            return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
        return {
            "ok": True,
            "link": link.id,
            "up": up,
            "switch": switch,
            "iface": f"eth{index}",
        }

    def snapshot(self) -> dict:
        """Everything the dashboard needs for one repaint, in one call.

        Locked like the write paths because os-ken runs on native threads; a
        dashboard poll can land in the middle of a PORT_STATUS handler
        rerouting flows.

        Returns:
            dict: ``{"ok": True, "flows": [...], "links": {...},
            "switches": {...}, "hosts": {...}}``.
        """
        with self.lock:
            return {
                "ok": True,
                "flows": [self._flow_view(f) for f in self.state.flows.values()],
                "links": self.state.utilisation(),
                "switches": {
                    s: {"dpid": self.topology.dpid(s), "connected": self._datapath(s) is not None}
                    for s in self.topology.switches
                },
                "hosts": {
                    h: {"switch": sw, "ip": self.topology.host_ip(h)}
                    for h, sw in self.topology.hosts.items()
                },
            }

    def topology_view(self) -> dict:
        """Return the static topology graph.

        Fixed for the controller's lifetime, so the dashboard fetches it once
        and only polls state after that.

        Returns:
            dict: The topology as a dict with ``"ok"`` set to ``True``.
        """
        payload = self.topology.to_dict()
        payload["ok"] = True
        return payload

    def _iperf_hint(self, flow: FlowEntry) -> dict:
        """Return exact iperf3 commands for a flow, so the demo never guesses.

        Args:
            flow: The flow to generate commands for.

        Returns:
            dict: ``{"server": ..., "client": ..., "server_on": ...,
            "client_on": ...}`` with the shell commands and which host each
            runs on.
        """
        udp = flow.ip_proto == IPPROTO_UDP
        return {
            "server": f"iperf3 -s -p {flow.tp_dst}",
            "client": (
                f"iperf3 -c {self.topology.host_ip(flow.dst)} -p {flow.tp_dst} "
                f"{'-u -b ' + str(flow.bandwidth_mbps) + 'M ' if udp else ''}-t 10"
            ),
            "server_on": flow.dst,
            "client_on": flow.src,
        }



    def _switch_name(self, dpid: int) -> Optional[str]:
        """Map a datapath ID to a switch name.

        Args:
            dpid: Datapath ID.

        Returns:
            str or None: Switch name, or ``None`` if the dpid is unknown.
        """
        try:
            return self.topology.switch_by_dpid(dpid)
        except IndexError:
            return None

    def _datapath(self, switch: str):
        """Return the OpenFlow datapath object for a switch, if connected.

        Args:
            switch: Switch name.

        Returns:
            datapath or None: The connected datapath, or ``None``.
        """
        return self.datapaths.get(self.topology.dpid(switch))

    def _link_between(self, a: str, b: str):
        """Find the core link between two switches.

        Args:
            a: First switch.
            b: Second switch.

        Returns:
            Link: The link object connecting the two.
        """
        lo, hi = sorted((a, b))
        return self.topology.link_by_id(f"{lo}--{hi}")

    def _matches(self, dp, flow: FlowEntry):
        """Build the forward and reverse OpenFlow matches for a flow.

        Both are 5-tuple matches identical on every switch of the path, only
        the output action differs, which lets a reroute overwrite an entry in
        place and lets a delete by cookie remove exactly this flow.

        Args:
            dp: The OpenFlow datapath object.
            flow: The flow entry.

        Returns:
            tuple[OFPMatch, OFPMatch]: ``(forward, reverse)`` matches.
        """
        parser = dp.ofproto_parser
        src_ip = self.topology.host_ip(flow.src)
        dst_ip = self.topology.host_ip(flow.dst)
        port_field = "tcp" if flow.ip_proto == IPPROTO_TCP else "udp"

        forward = parser.OFPMatch(
            eth_type=ETH_TYPE_IP,
            ipv4_src=src_ip,
            ipv4_dst=dst_ip,
            ip_proto=flow.ip_proto,
            **{f"{port_field}_dst": flow.tp_dst},
        )



        reverse = parser.OFPMatch(
            eth_type=ETH_TYPE_IP,
            ipv4_src=dst_ip,
            ipv4_dst=src_ip,
            ip_proto=flow.ip_proto,
            **{f"{port_field}_src": flow.tp_dst},
        )
        return forward, reverse

    def _ports_at(self, flow: FlowEntry, index: int):
        """Return the ingress/egress OpenFlow ports of a flow at one hop.

        Args:
            flow: The flow entry.
            index: Hop index along ``flow.path``.

        Returns:
            tuple[int, int]: ``(port the flow enters by, port it leaves by)``
            at that hop.
        """
        switch = flow.path[index]
        if index == 0:
            in_link = self.topology.access_link(flow.src)
        else:
            in_link = self._link_between(flow.path[index - 1], switch)
        if index == len(flow.path) - 1:
            out_link = self.topology.access_link(flow.dst)
        else:
            out_link = self._link_between(switch, flow.path[index + 1])
        return self.topology.ofport(switch, in_link), self.topology.ofport(switch, out_link)

    def _install_flow(self, flow: FlowEntry, only: Optional[Sequence[str]] = None) -> None:
        """Push a flow's OpenFlow entries, plus the ingress meter.

        OFPFC_ADD with the same match and priority replaces an existing entry,
        so rerouting updates shared switches in place with no forwarding gap
        (make-before-break).

        Args:
            flow: The flow entry to install.
            only: Optional subset of switches to update. Defaults to all
                switches on the flow's path.
        """
        wanted = set(only) if only is not None else set(flow.path)
        if flow.ingress in wanted:
            self._install_meter(flow)

        for index, switch in enumerate(flow.path):
            if switch not in wanted:
                continue
            dp = self._datapath(switch)
            if dp is None:
                self.record("install_skipped", flow_id=flow.flow_id, switch=switch,
                            reason="datapath not connected")
                continue
            ofp, parser = dp.ofproto, dp.ofproto_parser
            in_port, out_port = self._ports_at(flow, index)
            forward, reverse = self._matches(dp, flow)

            for match, out, metered in (
                (forward, out_port, switch == flow.ingress),
                (reverse, in_port, False),
            ):
                instructions = []
                if metered:
                    instructions.append(parser.OFPInstructionMeter(flow.cookie, ofp.OFPIT_METER))
                instructions.append(
                    parser.OFPInstructionActions(
                        ofp.OFPIT_APPLY_ACTIONS, [parser.OFPActionOutput(out)]
                    )
                )
                dp.send_msg(
                    parser.OFPFlowMod(
                        datapath=dp,
                        cookie=flow.cookie,
                        priority=FLOW_PRIORITY,
                        match=match,
                        idle_timeout=flow.idle_timeout,
                        hard_timeout=flow.hard_timeout,


                        flags=ofp.OFPFF_SEND_FLOW_REM,
                        instructions=instructions,
                    )
                )
            dp.send_msg(parser.OFPBarrierRequest(dp))

    def _install_meter(self, flow: FlowEntry) -> None:
        """Install one drop meter at the ingress switch at the reserved rate.

        Makes the reservation binding: excess traffic over the reserved rate
        is dropped so a flow cannot steal capacity promised to another.

        Args:
            flow: The flow to meter.
        """
        dp = self._datapath(flow.ingress)
        if dp is None:
            return
        ofp, parser = dp.ofproto, dp.ofproto_parser
        band = parser.OFPMeterBandDrop(rate=int(flow.bandwidth_mbps * 1000), burst_size=0)



        dp.send_msg(parser.OFPMeterMod(datapath=dp, command=ofp.OFPMC_DELETE, meter_id=flow.cookie))
        dp.send_msg(
            parser.OFPMeterMod(
                datapath=dp,
                command=ofp.OFPMC_ADD,
                flags=ofp.OFPMF_KBPS,
                meter_id=flow.cookie,
                bands=[band],
            )
        )

    def _delete_flow(self, flow: FlowEntry, switches: Sequence[str], drop_meter: bool = True) -> None:
        """Remove a flow's entries from the given switches, by cookie.

        Deleting by cookie handles both directions in one message and cannot
        touch another flow, since cookies are unique per flow.

        Args:
            flow: The flow to remove.
            switches: The switches to remove the flow from.
            drop_meter: Whether to also delete the ingress meter. Defaults
                to ``True``.
        """
        for switch in switches:
            dp = self._datapath(switch)
            if dp is None:
                continue
            ofp, parser = dp.ofproto, dp.ofproto_parser
            dp.send_msg(
                parser.OFPFlowMod(
                    datapath=dp,
                    cookie=flow.cookie,
                    cookie_mask=0xFFFFFFFFFFFFFFFF,
                    command=ofp.OFPFC_DELETE,
                    table_id=ofp.OFPTT_ALL,
                    out_port=ofp.OFPP_ANY,
                    out_group=ofp.OFPG_ANY,
                    match=parser.OFPMatch(),
                )
            )
        if drop_meter and flow.ingress in switches:
            dp = self._datapath(flow.ingress)
            if dp is not None:
                ofp, parser = dp.ofproto, dp.ofproto_parser
                dp.send_msg(
                    parser.OFPMeterMod(
                        datapath=dp, command=ofp.OFPMC_DELETE, meter_id=flow.cookie
                    )
                )



    def _replace_path(
        self,
        flow: FlowEntry,
        old_path: Sequence[str],
        allow_preemption: bool,
        cause: str,
    ) -> bool:
        """Find *flow* a new path and move it there (make-before-break).

        The caller must have already released the flow's capacity. On success
        the flow is placed, hits hold-down, and returns ``True``. On failure
        the flow is left ``FAILED`` with its entries removed, to be retried
        when a link comes back up.

        Args:
            flow: The flow to reroute.
            old_path: The switches it was occupying before.
            allow_preemption: Whether this reroute may preempt lower-priority
                flows. Victims it takes are themselves rerouted without
                rights, so a cascade stops after one level.
            cause: Human-readable reason for the reroute (e.g.
                ``"link s1--s2 down"``).

        Returns:
            bool: ``True`` if the flow is ACTIVE again, ``False`` if it is
            left FAILED.
        """
        hosts = self.topology.hosts
        decision = admission.evaluate(
            self.state,
            self.adj,
            hosts[flow.src],
            hosts[flow.dst],
            flow.bandwidth_mbps,
            flow.priority,
            policy=flow.policy,
            tie_break=flow.tie_break,
            allow_preemption=allow_preemption,
        )

        offline = (
            [s for s in decision.path.switches if self._datapath(s) is None]
            if decision.accepted
            else []
        )
        if not decision.accepted or offline:
            reason = (
                f"switches not connected: {', '.join(offline)}"
                if offline
                else decision.reason
            )
            flow.state = FlowState.FAILED
            self._delete_flow(flow, old_path)
            self.record(
                "reroute_failed",
                flow_id=flow.flow_id, cause=cause, reason=reason,
                old_path=list(old_path),
            )
            return False




        nested = []
        for flow_id in decision.victims:
            victim_path, _ = self.state.release(flow_id, FlowState.PREEMPTED)
            nested.append((self.state.flows[flow_id], victim_path))
            self.record("preempted", flow_id=flow_id, by=flow.flow_id,
                        path=list(victim_path))

        self.state.reserve(flow, decision.path.switches, decision.path.links)
        flow.reroutes += 1
        self._install_flow(flow)
        stale = [s for s in old_path if s not in set(flow.path)]
        self._delete_flow(flow, stale, drop_meter=False)
        self.state.mark_hold_down(flow.flow_id)
        self.record(
            "rerouted",
            flow_id=flow.flow_id, cause=cause,
            old_path=list(old_path), new_path=list(flow.path),
            preempted=list(decision.victims),
        )

        for victim, victim_path in nested:
            self._replace_path(
                victim, victim_path,
                allow_preemption=False,
                cause=f"preempted by {flow.flow_id} during reroute",
            )
        return True



    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def port_status_handler(self, ev):
        """Handle a port status change from a switch.

        Maps the port number to a link; if it is a core link that went down
        (or came up), triggers the appropriate handler. Access-link events
        are ignored.

        Args:
            ev: os-ken port-status event.
        """
        msg = ev.msg
        ofp = msg.datapath.ofproto
        switch = self._switch_name(msg.datapath.id)
        if switch is None or msg.reason != ofp.OFPPR_MODIFY:
            return

        link_id = self.topology.port_to_link(switch).get(msg.desc.port_no)
        if link_id is None:
            return
        link = self.topology.link_by_id(link_id)
        if link.a in self.topology.hosts or link.b in self.topology.hosts:

            return

        down = bool(msg.desc.state & ofp.OFPPS_LINK_DOWN)
        self.record(
            "port_status", switch=switch, port=msg.desc.port_no,
            link=link_id, link_down=down,
        )
        if down:
            self._handle_link_down(link_id)
        else:
            self._handle_link_up(link_id)

    def _handle_link_down(self, link_id: str) -> None:
        """Handle one port-down notification, treating the whole link as gone.

        Reroutes every affected flow in descending priority order.

        Args:
            link_id: Core link identifier.
        """
        with self.lock:
            if link_id in self.state.down_links:
                return
            affected = self.state.set_link_down(link_id)
            self.record("link_down", link=link_id,
                        affected=[f.flow_id for f in affected])



            for flow in affected:
                old_path, _ = self.state.release(flow.flow_id, FlowState.PREEMPTED)
                self._replace_path(flow, old_path, allow_preemption=True,
                                   cause=f"link {link_id} down")

    def _handle_link_up(self, link_id: str) -> None:
        """Handle a link restoration by retrying FAILED flows only.

        No automatic return to original paths, churn for no gain. Only
        FAILED flows are retried, since for them the alternative is no
        service at all.

        Args:
            link_id: Core link identifier.
        """
        with self.lock:
            if link_id not in self.state.down_links:
                return
            self.state.set_link_up(link_id)
            failed = [f for f in self.state.flows.values() if f.state is FlowState.FAILED]
            failed.sort(key=lambda f: (-f.priority, -f.bandwidth_mbps))
            self.record("link_up", link=link_id, retrying=[f.flow_id for f in failed])
            for flow in failed:
                self._replace_path(flow, (), allow_preemption=False,
                                   cause=f"link {link_id} restored")



    @set_ev_cls(ofp_event.EventOFPFlowRemoved, MAIN_DISPATCHER)
    def flow_removed_handler(self, ev):
        """Handle a switch-expired flow: release its reservation.

        Only the ingress switch's entry is authoritative; expiries from
        other switches on the path are noise once the flow is already torn
        down. Deletes are our own teardown and ignored.

        Args:
            ev: os-ken flow-removed event.
        """
        msg = ev.msg
        ofp = msg.datapath.ofproto
        if msg.reason not in (ofp.OFPRR_IDLE_TIMEOUT, ofp.OFPRR_HARD_TIMEOUT):

            return

        reason = "IDLE_TIMEOUT" if msg.reason == ofp.OFPRR_IDLE_TIMEOUT else "HARD_TIMEOUT"
        switch = self._switch_name(msg.datapath.id)
        flow_id = self._by_cookie.get(msg.cookie)
        if flow_id is None:
            return

        with self.lock:
            flow = self.state.flows.get(flow_id)
            if flow is None or not flow.holds_capacity():
                return



            if switch != flow.ingress:
                self.record("expiry_ignored", flow_id=flow_id, switch=switch, reason=reason)
                return

            path, _ = self.state.release(flow_id, FlowState.EXPIRED)
            self._delete_flow(flow, path)
            self._by_cookie.pop(flow.cookie, None)
            self.record(
                "flow_expired",
                flow_id=flow_id, reason=reason, path=list(path),
                duration_sec=msg.duration_sec,
                bandwidth_mbps=flow.bandwidth_mbps,
            )



    def _serve_control(self) -> None:
        """Serve the line-oriented JSON control channel.

        Deliberately not HTTP: the dashboard has its own API, but the demo,
        the experiment scripts, and ``netslice.client`` all drive the
        controller over this socket.

        Args:
            None.

        Returns:
            None. Runs forever in a daemon thread.
        """
        server = hub.StreamServer(CONTROL_ADDR, self._handle_control)
        self.logger.info("control channel on %s:%d", *CONTROL_ADDR)
        server.serve_forever()

    def _handle_control(self, sock, addr) -> None:
        """Handle one client connection on the control socket.

        Reads JSON lines, dispatches each to :meth:`_dispatch`, and writes
        the JSON reply. A failing command never kills the channel.

        Args:
            sock: The connected socket.
            addr: The client address.
        """
        stream = sock.makefile("rwb")
        try:
            for raw in stream:
                line = raw.decode().strip()
                if not line:
                    continue
                try:
                    reply = self._dispatch(json.loads(line))
                except Exception as exc:
                    self.logger.exception("control command failed")
                    reply = {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
                stream.write((json.dumps(reply, default=str) + "\n").encode())
                stream.flush()
        finally:
            stream.close()
            sock.close()

    def _dispatch(self, request: dict) -> dict:
        """Route a control-socket request to the matching handler.

        Args:
            request: The parsed JSON request dict with a ``"cmd"`` key.

        Returns:
            dict: The handler's reply dict.

        Note:
            Mutates *request* in place by popping the ``"cmd"`` key.
        """
        command = request.pop("cmd", None)
        if command == "add":
            return self.request_flow(**request)
        if command == "remove":
            return self.remove_flow(request["flow_id"])
        if command == "clear":
            return self.clear_flows()
        if command == "flows":
            return self.list_flows()
        if command == "links":
            return self.link_status()
        if command == "state":
            return self.snapshot()
        if command == "topology":
            return self.topology_view()
        if command == "events":
            return {"ok": True, "events": self.events_since(int(request.get("since", 0)))}
        return {"ok": False, "reason": f"unknown command {command!r}"}


PORTS = (
    ("127.0.0.1", OF_PORT, "OpenFlow (switches connect here)"),
    (CONTROL_ADDR[0], CONTROL_ADDR[1], "control socket (netslice.client)"),
    (DASHBOARD_ADDR[0], DASHBOARD_ADDR[1], "dashboard"),
)


def _ports_in_use():
    """Check which of the controller's ports are already bound.

    Returns:
        list[str]: Human-readable lines describing each busy port.
    """
    busy = []
    for host, port, what in PORTS:
        with socket.socket() as probe:
            probe.settimeout(0.5)
            if probe.connect_ex((host, port)) == 0:
                busy.append(f"  {host}:{port}  {what}")
    return busy


def main() -> int:
    """CLI entry point that starts the os-ken application.

    Checks that no port is already bound first (os-ken's own error on a
    failed bind is unhelpful), initialises logging, and runs the app.

    Returns:
        int: Process exit code (0 on clean stop, 1 on error).
    """
    from os_ken import cfg, log

    busy = _ports_in_use()
    if busy:
        print("cannot start: something is already listening on\n" + "\n".join(busy),
              file=sys.stderr)
        print("\nanother controller is probably running. Stop it with:\n"
              "  pkill -f 'netslice.controller'", file=sys.stderr)
        return 1

    log.early_init_log(20)
    cfg.CONF(args=[], project="os_ken")
    log.init_log()
    try:
        app_manager.AppManager.run_apps([__name__])
    except AttributeError as exc:
        if "'HubThread' object has no attribute 'kill'" not in str(exc):
            raise
        print("\ncontroller stopped. The AttributeError above comes from "
              "os-ken's own shutdown path, not from netslice, if this was "
              "not intentional, the real cause is logged before it.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
