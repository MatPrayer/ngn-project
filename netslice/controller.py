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

import json
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence

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

from netslice import admission, routing
from netslice.state import (
    IPPROTO_TCP,
    IPPROTO_UDP,
    FlowEntry,
    FlowState,
    NetworkState,
)
from netslice.topology import default_topology

ETH_TYPE_IP = 0x0800

# OpenFlow priority of an admitted flow entry. Unrelated to the flow's own
# priority class, which is a controller-side concept used for preemption and
# never reaches the switch: all admitted entries match a distinct 5-tuple, so
# they cannot shadow one another and all sit at the same OF priority.
FLOW_PRIORITY = 100
TABLE_MISS_PRIORITY = 0

CONTROL_ADDR = ("127.0.0.1", 9000)

ROOT = Path(__file__).resolve().parent.parent
EVENT_LOG = ROOT / "controller_events.jsonl"

DEFAULT_IDLE_TIMEOUT = 30
DEFAULT_HARD_TIMEOUT = 0
DEFAULT_PRIORITY = 1


class NetSliceController(app_manager.OSKenApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.topology = default_topology()
        self.adj = routing.adjacency(self.topology)
        self.state = NetworkState(self.topology)

        self.datapaths: Dict[int, object] = {}
        self._by_cookie: Dict[int, str] = {}

        # Requests arrive on a green thread of their own, switch events on the
        # os-ken hub. Both mutate state, so both take this first.
        self.lock = hub.Semaphore()

        EVENT_LOG.write_text("")
        self.control_server = hub.spawn(self._serve_control)

    # ------------------------------------------------------------ event log

    def record(self, kind: str, **fields) -> dict:
        """Append one event. The dashboard and the report plots read this file."""
        entry = {"kind": kind, "t": time.time(), "mono": time.monotonic(), **fields}
        with EVENT_LOG.open("a") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
        self.logger.info("EVENT %s %s", kind, fields)
        return entry

    # ------------------------------------------------------- switch lifecycle

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
        return {"ok": True, "flows": [f.to_dict() for f in self.state.flows.values()]}

    def link_status(self) -> dict:
        return {"ok": True, "links": self.state.utilisation()}

    def snapshot(self) -> dict:
        payload = self.state.snapshot()
        payload["switches"] = {
            s: {"dpid": self.topology.dpid(s), "connected": self._datapath(s) is not None}
            for s in self.topology.switches
        }
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

    # ------------------------------------------------------------- rerouting

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
        return {"ok": False, "reason": f"unknown command {command!r}"}


def main():
    from os_ken import cfg, log

    log.early_init_log(20)
    cfg.CONF(args=[], project="os_ken")
    log.init_log()
    app_manager.AppManager.run_apps([__name__])


if __name__ == "__main__":
    main()
