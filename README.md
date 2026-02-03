# LATTICE — Low-Latency Temporal Tunnel Inference & Correlation Engine

LATTICE is a consent-based latency telemetry tool for research demos like:

> “My traffic exits via a Sweden VPN, but network latency makes that physical location implausible.”

It **does not** capture keystrokes, content, screenshots, or any user input. It is intended for **your own machines** and **explicitly willing participants**.

---

## What LATTICE collects

From the macOS client:

- **RTT samples** to known endpoints (UDP echo; fixed packet size)
- **Summary stats**: min / p05 / median RTT per endpoint per burst
- **Path hints**: interface type (wifi/ethernet/etc.)
- **Tunnel heuristic**: whether any `utun*` interfaces exist (common for VPN/tunnels)

What it **does not** collect:

- Keystrokes or key timings
- Screen contents
- File contents
- Browsing history
- Application usage beyond coarse “network path” signals

---

## Reality check: what latency can and can’t prove

### What you can prove reliably
- You can often demonstrate that you are **not physically local** to a claimed region.
  Example: if you claim “Stockholm”, but minRTT to a Stockholm host is consistently far higher than typical local latencies.

### What you generally **cannot** prove without cooperation
If a VPN is a **perfect full-tunnel** and an outside observer only sees tunneled traffic, latency alone usually can’t uniquely reveal “California” vs “Nevada” etc.

To estimate a more specific region with high confidence, you typically need one of:
1) **A short VPN-off calibration window** (10–30 seconds), with explicit consent, or
2) **VPN PoP rotation** (connect to multiple exit cities and compare RTT patterns), or
3) **An on-device agent** (which LATTICE is) performing controlled measurements.

LATTICE is built to support (1) and (2) cleanly.

---

## Architecture

```
lattice/
  server/        # UDP echo responder (Go)
  client-rs/     # Rust CLI probe + JSONL logger (macOS + Linux) + analyzer
  client-macos/  # Swift CLI probe + JSONL logger (legacy)
  dashboard/     # Native offline UI (Python + pywebview + JS)
  docs/          # Protocol + notes
```

### server/
A UDP echo server that:
- requires an HMAC tag (shared secret)
- only responds to fixed 32-byte packets
- echoes 1:1 (no amplification)

### client-rs/
A Rust CLI that:
- uses BSD sockets with **kernel RX timestamps** (macOS + Linux)
- probes endpoints via UDP (no ICMP required)
- logs one JSON line per endpoint per burst (JSONL)
- emits simple “physics mismatch” notes when configured
- supports split-probes (bind per-interface/IP via `probePaths`)
- supports low-jitter pacing (`pacingSpinUs`)

### lattice-analyze (client-rs)
Analyzes JSONL logs to:
- compute per-endpoint bounds (tight/loose max distance)
- estimate coarse location (grid search with jitter-weighted SSE)
- compare baseline vs session, claim checks, calibration bias

### client-macos/ (legacy)
A Swift CLI that:
- probes endpoints via Network.framework UDP (no ICMP required)
- logs one JSON line per endpoint per burst (JSONL)
- emits simple “physics mismatch” notes when configured

---

## UDP packet format (32 bytes)

Client → Server, then echoed back:

- 4B  magic: `LATO`
- 4B  version (u32 BE)
- 8B  send time (u64 BE) — monotonic nanoseconds
- 4B  sequence (u32 BE)
- 8B  nonce (u64 BE)
- 4B  tag (u32 BE) — first 4 bytes of HMAC-SHA256(secret, first 28 bytes)

Why:
- fixed size, low CPU
- prevents casual misuse as a public reflector (needs secret)
- response equals request size (not an amplifier)

---

## Deploying servers

### 1) Build/run
```bash
cd server
export LATTICE_SECRET="a-long-secret-string"
go run .
```

### 2) Firewall
Strongly recommended: allow UDP/9000 only from your IP/subnet (or participants’ subnets).

---

## Building the Rust client (macOS + Linux)

Requirements:
- Rust 1.75+

Build:
```bash
cd client-rs
cargo build -p lattice-client --release
```

Run:
```bash
./target/release/lattice ./config.json
```

---

## Analyzing logs (physics bounds + location estimate)

Build:
```bash
cd client-rs
cargo build -p lattice-analyze --release
```

Usage:
```bash
./target/release/lattice-analyze \
  --config ./config.json \
  --session /path/to/session.jsonl \
  --baseline /path/to/baseline.jsonl \
  --claim-lat 59.3293 --claim-lon 18.0686 \
  --json
```

Notes:
- `--baseline` is optional; if provided, the analyzer compares VPN-off vs VPN-on.
- Use `--calibration-out` with `--calib-lat/--calib-lon` to build a per-endpoint bias model from a known location.
- Use `--calibration` to apply that model when computing max-distance bounds and estimates.
- Provide `lat`/`lon` for each endpoint in `config.json` to enable estimates.
- The estimate treats RTTs as direct paths; with a VPN it approximates the exit, not your true origin.
- `--json` prints machine-readable output.
- `--band-factor` and `--band-window-deg` control the fit band size.
- `--path-stretch` (default 1.1) accounts for routing stretch; set to 1.0 for the most conservative falsification bounds.

Template:
- `client-rs/config.3endpoints.template.json` (3-region starter with lat/lon placeholders)

Scripts:
- `client-rs/scripts/lattice_split.py` (split a JSONL file into baseline/session)
- `client-rs/scripts/lattice_report.py` (generate a Markdown report from analyzer JSON)

Protocol:
- `docs/PROTOCOL.md`

---

## Live Dashboard (native window, offline)

Requirements:
- Python 3.9+
- `pandas`, `pywebview` (see `dashboard/requirements.txt`)

Install:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r dashboard/requirements.txt
```

Run:
```bash
python dashboard/app.py \
  --config /path/to/config.json \
  --log /path/to/lattice.jsonl \
  --baseline /path/to/baseline.jsonl \
  --claim-lat 59.3293 --claim-lon 18.0686
```

Notes:
- Offline map is schematic (grid + endpoints + estimate). No external tiles.
- `--path-stretch` (default 1.1) widens physics bounds for routing stretch.
- Endpoints + split-probe paths can be edited inside the UI (CSV or JSON) and saved back to the config.
- Start/Stop controls for the Rust client are available in the UI (expects a built binary).
- Local server Start/Stop controls are available in the UI (requires Go and the server dir).
- Auto-baseline capture: `--auto-baseline-minutes` (default 5) and optional `--auto-baseline-out`.
- Calibration can be generated/loaded/cleared in the UI (bias model from a known location).
- Health UI: endpoint hygiene warnings, calibration drift, log-rotation resilience.
- Client path/log can be customized via `--client-bin` and `--client-log`.

---

## Building the macOS client (legacy Swift)

Requirements:
- macOS 13+
- Swift 5.9+

Build:
```bash
cd client-macos
swift build -c release
```

Run:
```bash
./.build/release/lattice ./config.json
```

---

## Config file

Example `config.json`:
```json
{
  "secretHex": "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff",
  "endpoints": [
    { "id": "usw-sjc", "host": "sjc.example.net", "port": 9000, "regionHint": "us-west" },
    { "id": "use-nyc", "host": "nyc.example.net", "port": 9000, "regionHint": "us-east" },
    { "id": "euc-fra", "host": "fra.example.net", "port": 9000, "regionHint": "europe" },
    { "id": "eun-sto", "host": "sto.example.net", "port": 9000, "regionHint": "stockholm" }
  ],
  "probePaths": [
    { "id": "vpn" },
    { "id": "direct", "bindInterface": "en0" }
  ],
  "samplesPerEndpoint": 10,
  "spacingMs": 30,
  "timeoutMs": 800,
  "intervalSeconds": 60,
  "pacingSpinUs": 200,
  "outputPath": "/Users/you/Library/Application Support/LATTICE/lattice.jsonl",
  "claimedEgressRegion": "stockholm",
  "physicsMismatchThresholdMs": 60
}
```

Notes:
- `secretHex` must match the server secret (same bytes, hex-encoded) to pass HMAC validation.
- `probePaths` duplicates each endpoint per path. Each path gets an `endpointId@pathId` tag in output.
- `bindInterface` (e.g., `en0`) or `bindIp` forces probes to a local interface/IP for split-probe testing.
- `pacingSpinUs` uses a short CPU spin to reduce timer jitter near send deadlines (set to 0 to disable).
- `claimedEgressRegion` is optional; it enables a simple “claimed vs measured” note.
- `physicsMismatchThresholdMs` is intentionally conservative. Tune after you collect ground truth.

---

## Output format (JSONL)

One JSON object per endpoint per burst, appended to `outputPath`.

Fields include:
- `samplesMs`, `minMs`, `p05Ms`, `medianMs` (stats are `null` when there are no valid samples)
- `probePath`, `probeBindIface`, `probeBindIp` (when split-probes are enabled)
- `iface`, `ifaceName`, `ifaceIsTunnel`
- `iface` is one of `wifi`/`ethernet`/`cellular`/`loopback`/`other`
- `localAddr` (the local IP:port the OS selected for the route to the endpoint)
- `destIsLoopback` (true when the target host is `127.0.0.1`, `::1`, or `localhost`)
- `utunPresent`, `utunActive`, `utunInterfaces` (`utunActive` means a tunnel interface is up/running with a non-loopback address; each entry includes decoded flags)
- `notes` (e.g., `"physics_mismatch: ..."`)

JSONL is easy to ingest into Python/R, log systems, or timeseries DBs.

---

## Demo workflows

### A) “VPN says Sweden, physics says no”
1. Connect to a Sweden VPN exit.
2. Set `claimedEgressRegion` to `"stockholm"` and include a Stockholm endpoint.
3. Run LATTICE and observe `physics_mismatch` notes when minRTT exceeds your threshold.

### B) “Estimate California-ish” (consent-based)
1. Run for 10–30 seconds **with VPN off** and record the RTT vector to all endpoints.
2. Turn VPN on and keep measuring.
3. Use the VPN-off session as ground truth for your region inference model.

---

## Testing

Single command:
```bash
make test
```

This runs:
- Rust analyzer unit tests
- Python dashboard unit tests (skips if dashboard deps are missing)

---

## Ethics & consent

LATTICE is designed for:
- self-instrumentation
- explicit opt-in research participants

Recommended:
- visible indicator while running
- export/delete controls for participants
- minimal retention windows for raw logs

Non-goals:
- deanonymizing unwilling users
- covert monitoring

---

## Next steps (ideas)
- Menubar UI wrapper for start/stop + status
- Baseline modeling per endpoint (rolling median/MAD)
- Region estimation module (least-squares fit of RTT vectors with calibration)
- VPN PoP rotation helper (controlled experiments)

---

## Contributing

See `CONTRIBUTING.md` for performance-first and ethics-first guidelines.

---

## License
Pick MIT or Apache-2.0 for research friendliness.
