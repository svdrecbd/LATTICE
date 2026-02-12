#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

fail=0
placeholder="REPLACE_WITH_SHARED_HEX_SECRET"

check_placeholder() {
  local file="$1"
  if [[ ! -f "$file" ]]; then
    return
  fi
  local value
  value="$(sed -nE 's/.*"secretHex"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/p' "$file" | head -n 1)"
  if [[ -z "$value" ]]; then
    echo "ERROR: $file is missing secretHex."
    fail=1
    return
  fi
  if [[ "$value" != "$placeholder" ]]; then
    echo "ERROR: $file must keep secretHex as $placeholder."
    fail=1
  fi
}

check_placeholder "client-macos/config.sample.json"
check_placeholder "client-rs/config.3endpoints.template.json"

json_files=()
while IFS= read -r file; do
  json_files+=("$file")
done < <(git ls-files '*.json')
if ((${#json_files[@]} > 0)); then
  if matches="$(rg -n -S '"secretHex"\s*:\s*"[0-9A-Fa-f]{64}"' "${json_files[@]}")"; then
    echo "ERROR: concrete 64-hex secretHex found in tracked JSON:"
    echo "$matches"
    fail=1
  fi
fi

tracked_files=()
while IFS= read -r file; do
  tracked_files+=("$file")
done < <(git ls-files)
if ((${#tracked_files[@]} > 0)); then
  if matches="$(rg -n -I -S '(-----BEGIN [A-Z ]*PRIVATE KEY-----|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|sk-[A-Za-z0-9]{20,})' "${tracked_files[@]}")"; then
    echo "ERROR: potential secret/token pattern found:"
    echo "$matches"
    fail=1
  fi
fi

if ((fail != 0)); then
  exit 1
fi

echo "Secret scan passed."
