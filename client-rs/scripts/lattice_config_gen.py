#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path


def parse_float(val):
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_endpoints(path, default_port):
    endpoints = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError("Input CSV must include headers")
        for row in reader:
            ep_id = (row.get("id") or "").strip()
            host = (row.get("host") or "").strip()
            if not ep_id or not host:
                continue
            port_raw = (row.get("port") or "").strip()
            port = int(port_raw) if port_raw else default_port
            region = (row.get("region") or row.get("regionHint") or "").strip() or None
            lat = parse_float(row.get("lat"))
            lon = parse_float(row.get("lon"))
            ep = {"id": ep_id, "host": host, "port": port}
            if region:
                ep["regionHint"] = region
            if lat is not None:
                ep["lat"] = lat
            if lon is not None:
                ep["lon"] = lon
            endpoints.append(ep)
    return endpoints


def main():
    parser = argparse.ArgumentParser(description="Generate a LATTICE client config from CSV.")
    parser.add_argument("--input", required=True, help="CSV with headers: id,host,port,region,lat,lon")
    parser.add_argument("--out", required=True, help="Output config JSON path")
    parser.add_argument("--secret-hex", help="Secret hex (or set LATTICE_SECRET_HEX)")
    parser.add_argument("--default-port", type=int, default=9000)
    parser.add_argument("--samples-per-endpoint", type=int, default=25)
    parser.add_argument("--spacing-ms", type=int, default=25)
    parser.add_argument("--timeout-ms", type=int, default=800)
    parser.add_argument("--interval-seconds", type=int, default=10)
    parser.add_argument("--output-path", default="lattice.jsonl")
    parser.add_argument("--claimed-egress-region")
    parser.add_argument("--physics-mismatch-threshold-ms", type=float, default=5.0)
    args = parser.parse_args()

    secret_hex = args.secret_hex or os.environ.get("LATTICE_SECRET_HEX")
    if not secret_hex:
        raise SystemExit("secretHex required via --secret-hex or LATTICE_SECRET_HEX")

    endpoints = parse_endpoints(args.input, args.default_port)
    if not endpoints:
        raise SystemExit("No valid endpoints found in input")

    cfg = {
        "secretHex": secret_hex,
        "endpoints": endpoints,
        "samplesPerEndpoint": args.samples_per_endpoint,
        "spacingMs": args.spacing_ms,
        "timeoutMs": args.timeout_ms,
        "intervalSeconds": args.interval_seconds,
        "outputPath": args.output_path,
        "claimedEgressRegion": args.claimed_egress_region,
        "physicsMismatchThresholdMs": args.physics_mismatch_threshold_ms,
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
