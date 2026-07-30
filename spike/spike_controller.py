"""Throwaway os-ken app that validates the two environment assumptions the
project design depends on, in order of development priority:

  A. OFPT_PORT_STATUS actually fires on BOTH ends of a Kathara veth pair when
     one end is administratively downed. Link-failure rerouting depends on it.
  B. OVS in this kernel accepts OF1.3 meters and enforces the configured rate.
     Bandwidth enforcement (open point 1) depends on it.

Everything here is deliberately hard-coded for the line topology
h1 -- s1 -- s2 -- h2; it is a probe, not a component of the final controller.
"""

import json
import time
from pathlib import Path

from os_ken.base import app_manager
from os_ken.controller import ofp_event
from os_ken.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from os_ken.ofproto import ofproto_v1_3

EVENT_LOG = Path(__file__).with_name("spike_events.jsonl")

# Rate the test meter drops above, in kbps. iperf3 is driven well over this.
METER_RATE_KBPS = 5000
METER_ID = 1


class Spike(app_manager.OSKenApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.datapaths = {}
        EVENT_LOG.write_text("")

    def record(self, kind, **fields):
        """Append one event with a wall-clock and a monotonic timestamp.

        Monotonic is what the latency measurement uses; wall-clock is only
        there to make the log readable next to other tools' output.
        """
        entry = {"kind": kind, "t": time.time(), "mono": time.monotonic(), **fields}
        with EVENT_LOG.open("a") as fh:
            fh.write(json.dumps(entry) + "\n")
        self.logger.info("EVENT %s %s", kind, fields)

    # ---------------------------------------------------------------- setup

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        dp = ev.msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser
        self.datapaths[dp.id] = dp
        self.record("switch_up", dpid=dp.id)

        # Table-miss -> controller. Fail-mode is secure, so without this the
        # switch silently drops everything it has no rule for.
        self.add_flow(dp, 0, parser.OFPMatch(), [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)])

        # Check A relies on port state, so ask for the port descriptions.
        dp.send_msg(parser.OFPPortDescStatsRequest(dp, 0))
        # Check B: does this datapath advertise meter support at all?
        dp.send_msg(parser.OFPMeterFeaturesStatsRequest(dp, 0))

    @set_ev_cls(ofp_event.EventOFPPortDescStatsReply, MAIN_DISPATCHER)
    def port_desc_handler(self, ev):
        dp = ev.msg.datapath
        ports = {
            p.port_no: {"name": p.name.decode(errors="replace"), "state": p.state}
            for p in ev.msg.body
            if p.port_no <= ev.msg.datapath.ofproto.OFPP_MAX
        }
        self.record("port_desc", dpid=dp.id, ports=ports)
        self.install_line_forwarding(dp, sorted(ports))

    @set_ev_cls(ofp_event.EventOFPMeterFeaturesStatsReply, MAIN_DISPATCHER)
    def meter_features_handler(self, ev):
        dp = ev.msg.datapath
        body = ev.msg.body[0] if ev.msg.body else None
        self.record(
            "meter_features",
            dpid=dp.id,
            max_meter=getattr(body, "max_meter", None),
            band_types=getattr(body, "band_types", None),
            capabilities=getattr(body, "capabilities", None),
            max_bands=getattr(body, "max_bands", None),
        )
        # Only s1 (dpid 1) shapes; it is the ingress switch for h1 -> h2.
        if dp.id == 1 and getattr(body, "max_meter", 0):
            self.install_meter(dp)

    # ------------------------------------------------------------ forwarding

    def install_line_forwarding(self, dp, ports):
        """Cross-connect the two data ports. The topology is a line, so no
        learning switch is needed and the result is deterministic."""
        if len(ports) != 2:
            self.record("unexpected_port_count", dpid=dp.id, ports=ports)
            return
        parser = dp.ofproto_parser
        a, b = ports
        for in_port, out_port in ((a, b), (b, a)):
            self.add_flow(
                dp,
                10,
                parser.OFPMatch(in_port=in_port),
                [parser.OFPActionOutput(out_port)],
            )
        self.record("forwarding_installed", dpid=dp.id, ports=ports)

    def add_flow(self, dp, priority, match, actions, meter_id=None):
        parser = dp.ofproto_parser
        inst = []
        if meter_id is not None:
            inst.append(parser.OFPInstructionMeter(meter_id, dp.ofproto.OFPIT_METER))
        inst.append(parser.OFPInstructionActions(dp.ofproto.OFPIT_APPLY_ACTIONS, actions))
        dp.send_msg(parser.OFPFlowMod(datapath=dp, priority=priority, match=match, instructions=inst))

    # ---------------------------------------------------------------- meters

    def install_meter(self, dp):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        band = parser.OFPMeterBandDrop(rate=METER_RATE_KBPS, burst_size=0)
        dp.send_msg(
            parser.OFPMeterMod(
                datapath=dp,
                command=ofp.OFPMC_ADD,
                flags=ofp.OFPMF_KBPS,
                meter_id=METER_ID,
                bands=[band],
            )
        )
        # Higher priority than the plain cross-connect, matching only the
        # h1 -> h2 direction, so the reverse path stays unshaped.
        match = parser.OFPMatch(
            in_port=1, eth_type=0x0800, ipv4_src="10.0.0.1", ipv4_dst="10.0.0.2"
        )
        self.add_flow(dp, 100, match, [parser.OFPActionOutput(2)], meter_id=METER_ID)
        self.record("meter_installed", dpid=dp.id, rate_kbps=METER_RATE_KBPS)
        dp.send_msg(parser.OFPBarrierRequest(dp))

    @set_ev_cls(ofp_event.EventOFPErrorMsg, [CONFIG_DISPATCHER, MAIN_DISPATCHER])
    def error_handler(self, ev):
        msg = ev.msg
        self.record("of_error", dpid=msg.datapath.id, type=msg.type, code=msg.code)

    # ----------------------------------------------------------- check A

    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def port_status_handler(self, ev):
        msg = ev.msg
        ofp = msg.datapath.ofproto
        reason = {
            ofp.OFPPR_ADD: "ADD",
            ofp.OFPPR_DELETE: "DELETE",
            ofp.OFPPR_MODIFY: "MODIFY",
        }.get(msg.reason, str(msg.reason))
        self.record(
            "port_status",
            dpid=msg.datapath.id,
            port=msg.desc.port_no,
            name=msg.desc.name.decode(errors="replace"),
            reason=reason,
            link_down=bool(msg.desc.state & ofp.OFPPS_LINK_DOWN),
            state=msg.desc.state,
        )


def main():
    from os_ken import cfg, log

    log.early_init_log(20)
    cfg.CONF(args=[], project="os_ken")
    log.init_log()
    app_manager.AppManager.run_apps([__name__])


if __name__ == "__main__":
    main()
