# Technologies & Libraries

## Runtime

| Technology | Version | Notes |
|---|---|---|
| Python | 3.11.14 | 3.12+ unsupported by dependencies |
| JavaScript | ES6+ | Vanilla, no frameworks |
| HTML5 | — | Single-page dashboard |
| CSS3 | — | Grid, Flexbox, custom properties, animations |

## Third-Party Python Libraries

| Library | Version | Purpose |
|---|---|---|
| `kathara` | 3.8.3 | Network emulator (Python API) |
| `os-ken` | 4.2.1 | OpenFlow controller framework (Ryu fork) |

## Python Standard Library

`argparse`, `collections.deque`, `dataclasses`, `enum`, `heapq`, `itertools`, `json`, `mimetypes`, `pathlib`, `re`, `socket`, `socketserver`, `subprocess`, `sys`, `threading`, `time`, `typing`, `urllib.request`, `wsgiref.simple_server`

## Network Protocols & Standards

| Protocol | Usage |
|---|---|
| OpenFlow 1.3 | Controller-switch communication (flow mods, meters, stats, port status) |
| TCP | OpenFlow channel (:6653), control socket (:9000), WSGI (:8080) |
| HTTP/WSGI | Dashboard API |
| IPv4 | `10.0.0.0/24` addressing |
| ARP | Static entries on all hosts |
| Ethernet (EthType 0x0800) | IPv4 flow matching |
| 5-tuple matching | src IP, dst IP, IP proto, src port, dst port |

## External Tools & Systems

| Tool | Purpose |
|---|---|
| Open vSwitch (OVS) | Software switch (flow tables, meters, bridges) |
| iperf3 | Traffic generation and throughput measurement |
| tc (traffic control) | HTB qdisc link capacity shaping |
| ip | Interface management, ARP, routing |
| Docker | Container runtime (Kathara backend) |
| Linux bridges | Kathara collision domains |
| ovs-vsctl | OVS configuration |
| ovs-ofctl | OVS flow/meter inspection |
| pyenv | Python version management |

## Web/Frontend

| Technology | Purpose |
|---|---|
| SVG | Dynamic topology visualization |
| CSS Grid | Dashboard layout (2-column, responsive) |
| CSS Flexbox | Panel sizing and alignment |
| CSS Custom Properties | Theme colours |
| CSS Animations | Hazard stripes for overbooked links |
| Fetch API | AJAX polling for state/events |
| async/await | Asynchronous API calls |
| FormData | Form submission handling |
| Responsive media queries | Narrow screen / short viewport adaptation |

## Concurrency Model

| Mechanism | Location |
|---|---|
| Native OS threads (os-ken `HUB_TYPE=native`) | `controller.py` |
| `hub.Semaphore` | `controller.py` — controller-wide lock |
| `threading.Thread(daemon=True)` | `controller.py` |
| `ThreadingMixIn` | `dashboard.py` — HTTP server |
| `hub.StreamServer` | `controller.py` — control socket |

## Testing

| Framework | Usage |
|---|---|
| `unittest` | All tests in `tests/` (3 files, 33+ methods) |

## Algorithms

| Algorithm | Location |
|---|---|
| Widest-path (max-bottleneck Dijkstra) | `routing.py` |
| Shortest-path (min-hop Dijkstra) | `routing.py` |
| Two-phase preemption | `admission.py`, `state.py` |
| Make-before-break rerouting | `controller.py` |
| Hold-down timer (anti-oscillation) | `state.py` |

## Ports

| Port | Protocol | Purpose |
|---|---|---|
| 6653 | TCP (OpenFlow) | Switch-to-controller |
| 9000 | TCP (JSON) | Control socket |
| 8080 | TCP (HTTP) | Dashboard |
| 5201+ | TCP/UDP | Per-flow iperf3 |
