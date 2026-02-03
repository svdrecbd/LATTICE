#!/usr/bin/env python3
import argparse
import json
import secrets
from pathlib import Path


DEFAULT_TEMPLATE = Path("client-rs/config.3endpoints.template.json")
DEFAULT_TARGET = Path("client-rs/config.local.json")


def load_json(path: Path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def generate_secret_hex():
    return secrets.token_hex(32)


def resolve_target(path_arg: str | None) -> Path:
    if path_arg:
        return Path(path_arg).expanduser()
    for candidate in [
        Path("client-rs/config.local.json"),
        Path("config.local.json"),
        Path("config.json"),
        Path("client-rs/config.json"),
    ]:
        if candidate.exists():
            return candidate
    return DEFAULT_TARGET


def main():
    parser = argparse.ArgumentParser(description="Rotate LATTICE secretHex safely.")
    parser.add_argument("--config", help="Config JSON to update/create")
    parser.add_argument("--template", help="Template JSON to clone if config missing")
    parser.add_argument("--secret", help="Optional hex secret to set (32 bytes)")
    args = parser.parse_args()

    secret_hex = args.secret or generate_secret_hex()
    target = resolve_target(args.config)
    template = Path(args.template).expanduser() if args.template else DEFAULT_TEMPLATE

    if target.exists():
        cfg = load_json(target)
    else:
        if not template.exists():
            raise SystemExit(f"Template not found: {template}")
        cfg = load_json(template)
        target = DEFAULT_TARGET if args.config is None else target

    cfg["secretHex"] = secret_hex
    save_json(target, cfg)

    print(f"Wrote secretHex to: {target}")
    print(f"secretHex: {secret_hex}")
    print(f"export LATTICE_SECRET_HEX={secret_hex}")


if __name__ == "__main__":
    main()
