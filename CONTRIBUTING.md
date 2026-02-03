# Contributing to LATTICE

Thanks for helping. This project is performance‑sensitive and measurement‑sensitive. The rules below are not optional.

---

## Quickstart for contributors

```bash
# tests
make test

# build client + analyzer
cd client-rs
cargo build -p lattice-client --release
cargo build -p lattice-analyze --release

# run dashboard (requires Python deps)
cd ..
python dashboard/app.py --config /path/to/config.json --log /path/to/lattice.jsonl
```

---

## Core principles

1) **Performance and measurement integrity first.**
   - Any change that adds overhead or jitter must be justified and measured.
   - Avoid allocations, logging, or work on the hot path unless necessary.

2) **Clarity without sacrificing speed.**
   - Readability matters, but never at the expense of latency/jitter.
   - Prefer small, explicit helpers and named constants over “magic numbers.”

3) **Consent and ethics are non‑negotiable.**
   - This is for self‑instrumentation and explicitly willing participants.
   - Do not add features that enable covert monitoring.

---

## Code hygiene expectations

- **No magic numbers.** Introduce constants in `dashboard/constants.py`, `dashboard/assets/constants.js`, or `client-rs/lattice-analyze/src/constants.rs` as appropriate.
- **Keep the hot path lean.** In Rust client code, avoid extra allocations, heap churn, or debug logging in per‑packet operations.
- **Prefer deterministic timing.** When adding scheduling or pacing logic, use monotonic clocks and keep jitter small.
- **Platform boundaries are real.** Don’t rely on Linux‑only behavior in macOS code paths (and vice‑versa).

---

## Testing

Run the full test suite:
```bash
make test
```

This runs:
- Rust analyzer unit tests
- Python dashboard unit tests (skipped if dependencies are missing)

If you touch the dashboard UI, verify the app starts and renders correctly with:
```bash
python dashboard/app.py --config /path/to/config.json --log /path/to/lattice.jsonl
```

---

## Repository layout

```
lattice/
  client-rs/     # Rust client + analyzer (macOS/Linux)
  client-macos/  # Swift client (legacy)
  server/        # UDP echo server (Go)
  dashboard/     # Native offline UI (Python + pywebview + JS/CSS/HTML)
  docs/          # Protocol + notes
  branding/      # Logo assets
```

---

## Support matrix

| Component | Platforms | Status |
| --- | --- | --- |
| Rust client (`client-rs`) | macOS, Linux | active |
| Swift client (`client-macos`) | macOS | legacy |
| Go server (`server`) | Any (Linux recommended) | active |
| Dashboard (`dashboard`) | macOS, Linux | active |

Notes:
- Windows support is currently out of scope.
- Rust client relies on kernel RX timestamps (macOS + Linux).

---

## Known issues / limitations

- VPNs can obscure true origin; estimates often track the **exit** unless you collect a short VPN‑off calibration.
- Endpoint quality dominates results (lat/lon accuracy, routing quirks, transient congestion).
- Calibration drift can occur with ISP routing changes; refresh calibration periodically.
- UI depends on a readable, append‑only JSONL log; log rotation/truncation resets the live view.
- Swift client is legacy and may lag behind Rust features.

---

## Getting started (forks & new contributors)

Minimum tooling:
- Rust (for `client-rs/`)
- Go (for `server/`)
- Python 3.9+ (for `dashboard/`)

Optional (if you want the full UI):
- `pandas`, `pywebview` (see `dashboard/requirements.txt`)

Quick sanity checks:
```bash
# Rust client + analyzer
cd client-rs
cargo build -p lattice-client --release
cargo build -p lattice-analyze --release

# Go server
cd ../server
go run .

# Dashboard (native UI)
cd ..
python3 -m venv .venv
source .venv/bin/activate
pip install -r dashboard/requirements.txt
python dashboard/app.py --config /path/to/config.json --log /path/to/lattice.jsonl
```

Config notes:
- `config.json` needs `secretHex` that matches the server.
- Endpoints should include `lat/lon` for estimates to work.
- Split probes use `probePaths` with `bindInterface` or `bindIp`.

---

## Style & organization

- **Rust:** keep modules small; favor named constants; no unsafe unless required.
- **Python:** avoid heavy work on the UI refresh loop; keep log parsing resilient.
- **JS/CSS:** keep UI offline (no external assets); minimal JS dependencies.
- **Docs:** keep README accurate; update it when you change behavior or flags.

---

## Pull request expectations

- Briefly describe **performance impact** (or “no change”) and **measurement impact**.
- If you modify endpoint logic or timing, include a note on jitter/latency effects.
- Add or update tests when behavior changes.

---

## Security / privacy

- Never add data collection beyond network timing and minimal path hints.
- Avoid logging secrets; the HMAC secret should only exist in config/env.

---

## Questions

If you’re unsure about a change that could affect timing or ethics, open an issue or ask before merging.
