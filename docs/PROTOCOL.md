# LATTICE Study Protocol (Consent-Based)

## Purpose
Collect latency telemetry from consenting participants to:
- falsify implausible location claims using physics bounds
- estimate coarse region with calibration or multiple endpoints

This protocol does not capture content, keystrokes, or browsing history.

## Consent
- Participant must explicitly opt in.
- Explain what is collected (RTT samples + interface metadata) and what is not collected.
- Provide a clear stop mechanism and data deletion option.

## Recommended setup
- 3 to 14 geographically distributed UDP echo servers
- All endpoints in config include lat/lon
- UDP/9000 allowed only from participant IPs

## Procedure
### 1) Baseline (VPN off)
- Duration: 2–5 minutes
- Goal: capture a ground-truth RTT vector
- Ask participant to stay on a stable network and avoid heavy uploads

### 2) Session (VPN on)
- Duration: 2–5 minutes
- Use a random VPN exit (or a specific region if testing claims)
- Record the time you enabled the VPN

### 3) Analysis
- Split the JSONL into baseline/session
- Run the analyzer to compute:
  - tight/loose physics bounds
  - least-squares estimate + fit band
  - baseline vs session deltas

## Notes on interpretation
- Physics bounds are strongest for falsifying claims ("not there").
- VPN-on estimates typically converge toward the exit, not the true origin.
- Fiber paths and routing stretch add latency beyond straight-line distance; use lower speed or a path-stretch factor to widen bounds.

## Data handling
- Store logs only as long as necessary
- Strip or anonymize IDs if sharing
- Provide deletion on request
