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

    # --------------------------------------------------------- public API

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
