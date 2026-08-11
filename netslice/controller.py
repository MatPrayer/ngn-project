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
from netslice.topology import OF_PORT, default_topology

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
    """`hub.spawn`, but the thread cannot outlive a dying process.

    Under HUB_TYPE=native `hub.spawn` returns a `threading.Thread` with the
    default `daemon=False`, and it starts the thread before handing it back, so
    there is no chance to change that. Three such threads, all looping forever,
    mean that when the controller fails at startup the traceback is printed and
    the process then just *sits there* — still holding its ports, so the next
    attempt fails for a different reason than the first one did.
    """
    thread = threading.Thread(target=target, args=args, daemon=True)
    thread.start()
    return thread


class NetSliceController(app_manager.OSKenApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
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

    # ------------------------------------------------------------ event log

    def record(self, kind: str, **fields) -> dict:
        """Append one event. The dashboard and the report plots read this file."""
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
        # list() first: the deque is appended to from OpenFlow handler threads,
        # and iterating it directly can raise mid-poll.
        return [e for e in list(self.event_log) if e["seq"] > seq][-limit:]

    # ----------------------------------------------------------- flow counters

    def _poll_flow_stats(self) -> None:
        """Ask each ingress switch for its flow counters, every STATS_INTERVAL.

        This is what makes the dashboard's "remaining TTL" honest. `idle_timeout`
        resets whenever traffic matches, and the switch never tells the
        controller that it did — so the only way to know how long a flow has
        been quiet is to watch its packet counter stop moving. The same poll
        gives live per-flow throughput, which the dashboard plots and the
        evaluation wants anyway.
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
            throughput = (
                round((entry.byte_count - previous["bytes"]) * 8 / elapsed / 1e6, 3)
                if previous and elapsed > 0
                else 0.0
            )
            self.flow_stats[entry.cookie] = {
                "packets": entry.packet_count,
                "bytes": entry.byte_count,
                "duration_sec": entry.duration_sec,
                "at": now,
                "last_active": now if moved else (previous["last_active"] if previous else now),
                "throughput_mbps": max(throughput, 0.0),
            }

    def _flow_view(self, flow: FlowEntry) -> dict:
        """A flow as the dashboard wants it: the entry plus live counters."""
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
        dp = ev.datapath
        if ev.state == DEAD_DISPATCHER and dp.id in self.datapaths:
            del self.datapaths[dp.id]
            self.record("switch_down", dpid=dp.id, switch=self._switch_name(dp.id))

    @set_ev_cls(ofp_event.EventOFPErrorMsg, [CONFIG_DISPATCHER, MAIN_DISPATCHER])
    def error_handler(self, ev):
        msg = ev.msg
        self.record("of_error", dpid=msg.datapath.id, type=msg.type, code=msg.code)

    def _wipe(self, dp) -> None:
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
        """Priority-0 drop.

        Redundant against secure fail mode, which already drops unmatched
        traffic, but an explicit entry carries a packet counter — and that
        counter is precisely "traffic offered by hosts that was never
        admitted", which the evaluation wants to report.
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
        """Admit (or refuse) one flow request. This is what the dashboard calls."""
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
        for flow_id in list(self.state.flows):
            self.remove_flow(flow_id)
        with self.lock:
            self.state.flows.clear()
            self._by_cookie.clear()
        return {"ok": True}

    def list_flows(self) -> dict:
        with self.lock:
            return {"ok": True, "flows": [self._flow_view(f) for f in self.state.flows.values()]}

    def link_status(self) -> dict:
        with self.lock:
            return {"ok": True, "links": self.state.utilisation()}

    def snapshot(self) -> dict:
        """Everything the dashboard needs for one repaint, in one call.

        Locked like the write paths: os-ken runs on native threads, so a
        dashboard poll really can land in the middle of a PORT_STATUS handler
        rerouting flows — and iterating the flow table while it is being
        rewritten raises rather than returning something merely stale.
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
        """The graph itself. Fixed for the lifetime of the controller, so the
        dashboard fetches it once and only polls the state after that."""
        payload = self.topology.to_dict()
        payload["ok"] = True
        return payload

    def _iperf_hint(self, flow: FlowEntry) -> dict:
        """Exact commands for the demo, so the per-flow port is never guessed."""
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

    # --------------------------------------------------------- flow plumbing

    def _switch_name(self, dpid: int) -> Optional[str]:
        try:
            return self.topology.switch_by_dpid(dpid)
        except IndexError:
            return None

    def _datapath(self, switch: str):
        return self.datapaths.get(self.topology.dpid(switch))

    def _link_between(self, a: str, b: str):
        lo, hi = sorted((a, b))
        return self.topology.link_by_id(f"{lo}--{hi}")

    def _matches(self, dp, flow: FlowEntry):
        """The forward and reverse matches for a flow.

        Both are 5-tuple matches and are *identical on every switch of
        the path* — only the output action differs. That is what lets a reroute
        overwrite an entry in place on a shared switch, and what lets a delete
        by cookie remove exactly this flow and nothing else.
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
        """(port the flow enters by, port it leaves by) at hop `index`."""
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
        """Push this flow's entries, plus the ingress meter.

        On a switch that already carries the flow, OFPFC_ADD with the same
        match and priority *replaces* the existing entry rather than adding a
        second one. Rerouting therefore updates shared switches in place with
        no gap in forwarding, which is what make-before-break means: new
        entries go in first, stale ones are deleted afterwards.
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
        """One drop meter at the ingress switch, at the reserved rate.

        This is what makes the reservation binding rather than advisory: a flow
        that sends beyond what it asked for has the excess dropped, so it
        cannot eat the capacity admission control promised to somebody else
        The spike measured 5.56 Mbps through a 5000 kbps meter, so one
        drop band is enough — which is all OVS offers here anyway.
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
        """Remove this flow's entries from `switches`, by cookie.

        Deleting by cookie takes both directions in one message and cannot
        touch another flow, since cookies are unique per flow.
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
        """Find `flow` a new path and move it there. Caller must have released it.

        Returns True if the flow is ACTIVE again. On failure the flow is left
        FAILED with its entries removed, and it will be retried when a link
        comes back up.
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
        """One port-down notification means the whole link is gone.

        Under the Docker manager a Kathara collision domain is a Linux bridge,
        not a veth pair, so only the end that was actually downed ever reports
        (spike/FINDINGS.md check A). Waiting for the second notification would
        mean waiting forever; the controller knows the topology, so one report
        is enough to identify the link unambiguously.
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
        """No automatic return to original paths — churn for no gain.

        Only FAILED flows are retried, since for them the alternative is no
        service at all.
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

    # ---------------------------------------------------------- control socket

    def _serve_control(self) -> None:
        """Line-oriented JSON control channel.

        Deliberately not HTTP: the dashboard is a separate component and will
        put its own API in front of this, but the demo, the experiment scripts
        and `netslice/client.py` all need to drive the controller today.
        """
        server = hub.StreamServer(CONTROL_ADDR, self._handle_control)
        self.logger.info("control channel on %s:%d", *CONTROL_ADDR)
        server.serve_forever()

    def _handle_control(self, sock, addr) -> None:
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


def main():
    from os_ken import cfg, log

    log.early_init_log(20)
    cfg.CONF(args=[], project="os_ken")
    log.init_log()
    app_manager.AppManager.run_apps([__name__])


if __name__ == "__main__":
    main()
