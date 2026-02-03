#!/usr/bin/env python3
import argparse
import json
import subprocess
import sys


def run_analyzer(args):
    cmd = [
        args.analyze_bin,
        "--config",
        args.config,
        "--session",
        args.session,
        "--json",
    ]
    if args.baseline:
        cmd.extend(["--baseline", args.baseline])
    if args.claim_lat is not None and args.claim_lon is not None:
        cmd.extend(["--claim-lat", str(args.claim_lat), "--claim-lon", str(args.claim_lon)])
    if args.speed_km_s is not None:
        cmd.extend(["--speed-km-s", str(args.speed_km_s)])
    if args.grid is not None:
        cmd.extend(["--grid", str(args.grid)])
    if args.refine is not None:
        cmd.extend(["--refine", str(args.refine)])
    if args.band_factor is not None:
        cmd.extend(["--band-factor", str(args.band_factor)])
    if args.band_window_deg is not None:
        cmd.extend(["--band-window-deg", str(args.band_window_deg)])

    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print(res.stderr, file=sys.stderr)
        sys.exit(res.returncode)
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        print("Analyzer did not return valid JSON", file=sys.stderr)
        sys.exit(1)


def fmt_ms(v):
    return "n/a" if v is None else f"{v:.2f} ms"


def fmt_km(v):
    return "n/a" if v is None else f"{v:.1f} km"


def render_report(data):
    out = []
    out.append("# LATTICE Report\n")

    params = data.get("params", {})
    out.append("## Parameters")
    out.append(
        f"- speed_km_s: {params.get('speedKmS', 'n/a')}"
    )
    out.append(
        f"- effective_speed_km_s: {params.get('effectiveSpeedKmS', 'n/a')}\n- path_stretch: {params.get('pathStretch', 'n/a')}"
    )
    out.append(
        f"- grid_deg: {params.get('gridDeg', 'n/a')}\n- refine_deg: {params.get('refineDeg', 'n/a')}"
    )
    out.append(
        f"- band_factor: {params.get('bandFactor', 'n/a')}\n- band_window_deg: {params.get('bandWindowDeg', 'n/a')}"
    )

    def section(label, block):
        out.append(f"\n## {label}")
        out.append(f"Records: {block.get('records', 0)}")
        out.append("\n### Endpoint stats")
        out.append("| id | host | count | p05 | p50 | p95 | jitter | max_dist_tight | max_dist_loose |")
        out.append("|---|---|---:|---:|---:|---:|---:|---:|---:|")
        for r in block.get("endpointStats", []):
            out.append(
                "| {id} | {host} | {count} | {p05} | {p50} | {p95} | {jit} | {tight} | {loose} |".format(
                    id=r.get("id", ""),
                    host=r.get("host", ""),
                    count=r.get("count", 0),
                    p05=fmt_ms(r.get("p05Ms")),
                    p50=fmt_ms(r.get("p50Ms")),
                    p95=fmt_ms(r.get("p95Ms")),
                    jit=fmt_ms(r.get("jitterMs")),
                    tight=fmt_km(r.get("maxDistKmTight")),
                    loose=fmt_km(r.get("maxDistKmLoose")),
                )
            )

        est = block.get("estimate")
        if est:
            out.append("\n### Estimate")
            out.append(
                "- lat: {lat:.4f}\n- lon: {lon:.4f}\n- bias_ms: {bias:.2f}\n- sse: {sse:.2f}\n- endpoints_used: {n}".format(
                    lat=est.get("lat"),
                    lon=est.get("lon"),
                    bias=est.get("biasMs"),
                    sse=est.get("sse"),
                    n=est.get("points"),
                )
            )
            band = est.get("band")
            if band:
                out.append("\n### Fit band")
                out.append(
                    "- radius_km: {r:.1f}\n- sse_threshold: {t:.2f}\n- bounds: lat[{min_lat:.2f},{max_lat:.2f}] lon[{min_lon:.2f},{max_lon:.2f}]".format(
                        r=band.get("radiusKm"),
                        t=band.get("sseThreshold"),
                        min_lat=band.get("minLat"),
                        max_lat=band.get("maxLat"),
                        min_lon=band.get("minLon"),
                        max_lon=band.get("maxLon"),
                    )
                )

    section("Session", data.get("session", {}))

    if data.get("baseline"):
        section("Baseline", data.get("baseline", {}))

    if data.get("claimChecks"):
        out.append("\n## Claim checks")
        out.append("| id | dist_km | max_tight_km | max_loose_km | falsify_tight | falsify_loose |")
        out.append("|---|---:|---:|---:|---:|---:|")
        for c in data.get("claimChecks", []):
            out.append(
                "| {id} | {dist:.1f} | {tight} | {loose} | {ft} | {fl} |".format(
                    id=c.get("id", ""),
                    dist=c.get("distKm", 0.0),
                    tight=fmt_km(c.get("maxTightKm")),
                    loose=fmt_km(c.get("maxLooseKm")),
                    ft=c.get("falsifyTight", ""),
                    fl=c.get("falsifyLoose", ""),
                )
            )

    if data.get("deltas"):
        out.append("\n## Baseline vs Session deltas (p05)")
        out.append("| id | delta_p05_ms | baseline_p05_ms | session_p05_ms |")
        out.append("|---|---:|---:|---:|")
        for d in data.get("deltas", []):
            out.append(
                "| {id} | {dp:.2f} | {b:.2f} | {s:.2f} |".format(
                    id=d.get("id", ""),
                    dp=d.get("deltaP05Ms", 0.0),
                    b=d.get("baselineP05Ms", 0.0),
                    s=d.get("sessionP05Ms", 0.0),
                )
            )

    if data.get("estimateSeparationKm") is not None:
        out.append(
            f"\n## Baseline vs Session estimate separation\n- {data.get('estimateSeparationKm'):.1f} km"
        )

    return "\n".join(out) + "\n"


def main():
    p = argparse.ArgumentParser(description="Generate a LATTICE report from JSONL logs")
    p.add_argument("--config", required=True, help="Config JSON with endpoints and lat/lon")
    p.add_argument("--session", required=True, help="Session JSONL path")
    p.add_argument("--baseline", help="Baseline JSONL path")
    p.add_argument("--claim-lat", type=float)
    p.add_argument("--claim-lon", type=float)
    p.add_argument("--speed-km-s", type=float)
    p.add_argument("--grid", type=float)
    p.add_argument("--refine", type=float)
    p.add_argument("--band-factor", type=float)
    p.add_argument("--band-window-deg", type=float)
    p.add_argument(
        "--analyze-bin",
        default="/Users/svdr/Downloads/lattice/client-rs/target/release/lattice-analyze",
        help="Path to lattice-analyze binary",
    )
    p.add_argument("--out", help="Output markdown path (default: stdout)")
    args = p.parse_args()

    data = run_analyzer(args)
    report = render_report(data)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(report)
    else:
        print(report)


if __name__ == "__main__":
    main()
