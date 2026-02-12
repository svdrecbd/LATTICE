# The String Project

An end‑to‑end latency calibration campaign for LATTICE that builds a global
RTT matrix and a reusable calibration pack. The goal is to map **network
distance** (not physical distance) between Google Cloud zones, then fold that
knowledge into LATTICE’s live estimator to tighten bounds and reduce drift.

---

## Goals

- Build a **zone‑to‑zone RTT matrix** (p05/p50/p95/jitter).
- Derive **per‑endpoint calibration priors** (bias + scale) from real paths.
- Produce a **global reference dataset** for future modeling and comparison.

---

## Scope (Full Zone‑Level Campaign)

- **Provider:** Google Cloud
- **Topology:** 1 VM **per zone** (full zone coverage)
- **Server:** LATTICE UDP echo server (port 9000/udp)
- **Client:** LATTICE client running full‑mesh probes
- **Duration:** **15 minutes**
- **Samples:** `samplesPerEndpoint = 25`
- **Spacing:** `spacingMs = 25`
- **Interval:** `intervalSeconds = 10`
- **Timeout:** `timeoutMs = 800`

Full‑mesh means each VM probes every other VM each interval. All endpoints run
simultaneously to avoid temporal drift.

---

## Hardware Configuration

Baseline configuration for “best possible” signal quality:

- **Machine type:** `e2-standard-2` everywhere
- **Disk:** 10 GB pd‑standard boot disk
- **Network:** 1 external IPv4 per VM
- **Firewall:** UDP/9000 open to the internet

> Note: E2‑standard‑2 everywhere is aggressive but clean. You can reduce cost by
> using `e2-small` for non‑anchor zones without materially affecting RTT quality.

---

## Expected Outputs

1) **Global RTT matrix**
   - p05 / p50 / p95 per zone‑pair
   - jitter estimates (p95 − p05)
   - min observed RTT

2) **Calibration pack**
   - per‑endpoint `biasMs` + `scale`
   - accumulated sample history (for fit quality)
   - packaged as JSON for LATTICE

3) **Derived artifacts**
   - “effective distance” estimates per zone‑pair
   - anomaly reports (unexpectedly slow or inconsistent routes)

---

## Time & Cost (Ballpark)

Let `Z` = number of zones.

Per‑VM hourly cost (approx):
```
e2-standard-2 + IPv4 + 10GB pd-standard ≈ $0.0726 / hr
```

15‑minute cost per VM:
```
~ $0.0181
```

Total cost:
```
Total ≈ Z * $0.0181
```

Example:
- If **Z = 130**, 15‑minute run ≈ **$2.36** (compute + IP + disk only).

> These are estimates. Pricing varies by region; always verify current rates in
> GCP Billing.

---

## Data Volume (Local Storage)

Using ~0.9 KB per record at 10 samples, scaled to 25 samples (~2.3 KB/record),
full‑mesh for 15 minutes:

```
Records = Z * (Z − 1) * 90
Data ≈ 2.3 KB * Records
```

Example:
- Z = 130 ⇒ ~1.51M records ⇒ **~3.2 GiB** total

Local storage is fine for a one‑off campaign.

---

## Campaign Steps (High Level)

1) **Provision**
   - Create one VM per zone
   - Install LATTICE server + client
   - Open UDP/9000

2) **Run**
   - Start echo server everywhere
   - Start client everywhere (full‑mesh)
   - Collect logs for 15 minutes

3) **Collect**
   - Pull all logs locally
   - Merge into a single session file

4) **Analyze**
   - Compute p05/p50/p95/jitter matrix
   - Build calibration pack
   - Store artifacts in `docs/` or `artifacts/`

5) **Teardown**
   - Delete all instances
   - Delete firewall rule

---

## Potential Outcomes

**Best‑case**
- Stable p05 per zone‑pair
- Consistent calibration scales
- Clear evidence of route inflation by region
- Reliable priors for live estimation

**Mixed**
- Some zones have noisy p05 due to congestion
- Missing data due to zone outages or quota limits

**Worst‑case**
- Intermittent failures (startup scripts, firewall errors)
- Not enough zones available for a full mesh

---

## Limitations

- Measures **network distance**, not physical distance.
- Routes can change; results are time‑sensitive.
- Intra‑region zones often share upstream paths (not independent).
- VPN exits will still dominate observed RTTs.

---

## Ethics & Consent

The String Project is intended for **consenting participants** and network
measurement research only. It is **not** designed for covert tracking or
inference without consent.

---

## Future Extensions

- Cross‑provider calibration packs (GCP + AWS + Hetzner)
- Longitudinal drift tracking (monthly reruns)
- Route anomaly detection
- Endpoint weighting based on historical stability

