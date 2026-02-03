#!/usr/bin/env python3
import argparse
import json
import sys


def main():
    p = argparse.ArgumentParser(description="Split LATTICE JSONL into baseline/session by split timestamp")
    p.add_argument("--input", required=True, help="Input JSONL path")
    p.add_argument("--split-ts", type=int, required=True, help="Unix ms timestamp to split at")
    p.add_argument("--baseline-out", required=True, help="Baseline JSONL output path")
    p.add_argument("--session-out", required=True, help="Session JSONL output path")
    args = p.parse_args()

    try:
        fin = open(args.input, "r", encoding="utf-8")
    except OSError as e:
        print(f"Failed to open input: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        fbase = open(args.baseline_out, "w", encoding="utf-8")
        fsess = open(args.session_out, "w", encoding="utf-8")
    except OSError as e:
        print(f"Failed to open output: {e}", file=sys.stderr)
        sys.exit(1)

    for line in fin:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = rec.get("tsUnixMs")
        if isinstance(ts, int) and ts < args.split_ts:
            fbase.write(line + "\n")
        elif isinstance(ts, int):
            fsess.write(line + "\n")

    fin.close()
    fbase.close()
    fsess.close()


if __name__ == "__main__":
    main()
