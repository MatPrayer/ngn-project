"""Probe controller that validates OFPT_FLOW_REMOVED delivery.

 makes the entire TTL mechanism depend on the switch sending
OFPT_FLOW_REMOVED when a flow entry expires, which only happens if
OFPFF_SEND_FLOW_REM is set at installation time. If those notifications never
arrive, capacity is never released and the TTL extension cannot work.

On each switch connect this installs two entries on s1:
  * idle_timeout=3, no traffic  -> must expire with reason IDLE_TIMEOUT
  * hard_timeout=6              -> must expire with reason HARD_TIMEOUT
and logs every removal it receives.
"""

import json
import time
from pathlib import Path

from os_ken.base import app_manager
from os_ken.controller import ofp_event
from os_ken.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from os_ken.ofproto import ofproto_v1_3

EVENT_LOG = Path(__file__).with_name("flowrem_events.jsonl")

IDLE_COOKIE = 0x1001
HARD_COOKIE = 0x1002


class FlowRemProbe(app_manager.OSKenApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        """Initialize the flow-removal probe app.

        Clears the event log on startup.
        """
        super().__init__(*args, **kwargs)
        EVENT_LOG.write_text("")

    def record(self, kind, **fields):
        """Append one event to the JSONL log with timestamps.

        Args:
            kind (str): Event type identifier.
            **fields: Arbitrary key-value pairs included in the event.
        """
        entry = {"kind": kind, "t": time.time(), "mono": time.monotonic(), **fields}
        with EVENT_LOG.open("a") as fh:
            fh.write(json.dumps(entry) + "\n")
        self.logger.info("EVENT %s %s", kind, fields)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        """Install two flow entries on s1 to test idle and hard timeout removal.

        Both entries use ``OFPFF_SEND_FLOW_REM`` so the switch sends
        ``OFPT_FLOW_REMOVED`` on expiry. Only dpid 1 is used.

        Args:
            ev (EventOFPSwitchFeatures): Switch features event.
        """
        dp = ev.msg.datapath
        self.record("switch_up", dpid=dp.id)
        if dp.id != 1:
            return
        parser = dp.ofproto_parser
        ofp = dp.ofproto

        for cookie, idle, hard, tag in (
            (IDLE_COOKIE, 3, 0, "idle"),
            (HARD_COOKIE, 0, 6, "hard"),
        ):
            # Match something that will never be hit, so the idle entry really
            # does sit unused and expire on schedule.
            match = parser.OFPMatch(eth_type=0x0800, ipv4_dst=f"192.0.2.{1 if tag == 'idle' else 2}")
            dp.send_msg(
                parser.OFPFlowMod(
                    datapath=dp,
                    cookie=cookie,
                    priority=200,
                    match=match,
                    idle_timeout=idle,
                    hard_timeout=hard,
                    flags=ofp.OFPFF_SEND_FLOW_REM,
                    instructions=[
                        parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, [])
                    ],
                )
            )
            self.record("flow_installed", dpid=dp.id, tag=tag, idle=idle, hard=hard)

    @set_ev_cls(ofp_event.EventOFPFlowRemoved, MAIN_DISPATCHER)
    def flow_removed_handler(self, ev):
        """Log every OFPT_FLOW_REMOVED event with its reason code.

        Args:
            ev (EventOFPFlowRemoved): Flow removed event from the switch.
        """
        msg = ev.msg
        ofp = msg.datapath.ofproto
        reason = {
            ofp.OFPRR_IDLE_TIMEOUT: "IDLE_TIMEOUT",
            ofp.OFPRR_HARD_TIMEOUT: "HARD_TIMEOUT",
            ofp.OFPRR_DELETE: "DELETE",
            ofp.OFPRR_GROUP_DELETE: "GROUP_DELETE",
        }.get(msg.reason, str(msg.reason))
        self.record(
            "flow_removed",
            dpid=msg.datapath.id,
            cookie=msg.cookie,
            reason=reason,
            duration_sec=msg.duration_sec,
        )

    @set_ev_cls(ofp_event.EventOFPErrorMsg, [CONFIG_DISPATCHER, MAIN_DISPATCHER])
    def error_handler(self, ev):
        """Log OpenFlow error messages from the switch.

        Args:
            ev (EventOFPErrorMsg): OpenFlow error message event.
        """
        msg = ev.msg
        self.record("of_error", dpid=msg.datapath.id, type=msg.type, code=msg.code)


def main():
    """Launch the flow-removal probe os-ken controller application."""
    from os_ken import cfg, log

    log.early_init_log(20)
    cfg.CONF(args=[], project="os_ken")
    log.init_log()
    app_manager.AppManager.run_apps([__name__])


if __name__ == "__main__":
    main()
