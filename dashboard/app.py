#!/usr/bin/env python3
import argparse
import csv
import json
import os
import shlex
import signal
import subprocess
import threading
import time
import socket
import ipaddress
from datetime import datetime
from math import asin, atan2, cos, pi, radians, sin, sqrt
from pathlib import Path

import pandas as pd
import webview

try:
    from .constants import (
        DEFAULT_AUTO_BASELINE_MINUTES,
        DEFAULT_BAND_FACTOR,
        DEFAULT_BAND_WINDOW_DEG,
        DEFAULT_GRID_DEG,
        DEFAULT_PATH_STRETCH,
        DEFAULT_PORT,
        DEFAULT_REFRESH_MS,
        DEFAULT_ESTIMATE_INTERVAL_MS,
        DEFAULT_REFINE_DEG,
        DEFAULT_SPEED_KM_S,
        DEFAULT_WINDOW_MINUTES,
        CALIB_DRIFT_WARN_MS,
        EARTH_RADIUS_KM,
        LOG_RESET_NOTICE_MS,
        MAX_CALIBRATION_SAMPLES,
        MAX_CALIBRATION_SCALE,
        MIN_JITTER_MS,
        MIN_CALIBRATION_SCALE,
        MS_PER_MIN,
        MS_PER_SEC,
        REFINE_WINDOW_MULT,
        RTT_FACTOR,
        SEC_PER_MIN,
        SSE_EPSILON,
        WORLD_LAT_MAX,
        WORLD_LON_MAX,
    )
except ImportError:
    from constants import (  # type: ignore
        DEFAULT_AUTO_BASELINE_MINUTES,
        DEFAULT_BAND_FACTOR,
        DEFAULT_BAND_WINDOW_DEG,
        DEFAULT_GRID_DEG,
        DEFAULT_PATH_STRETCH,
        DEFAULT_PORT,
        DEFAULT_REFRESH_MS,
        DEFAULT_ESTIMATE_INTERVAL_MS,
        DEFAULT_REFINE_DEG,
        DEFAULT_SPEED_KM_S,
        DEFAULT_WINDOW_MINUTES,
        CALIB_DRIFT_WARN_MS,
        EARTH_RADIUS_KM,
        LOG_RESET_NOTICE_MS,
        MAX_CALIBRATION_SAMPLES,
        MAX_CALIBRATION_SCALE,
        MIN_JITTER_MS,
        MIN_CALIBRATION_SCALE,
        MS_PER_MIN,
        MS_PER_SEC,
        REFINE_WINDOW_MULT,
        RTT_FACTOR,
        SEC_PER_MIN,
        SSE_EPSILON,
        WORLD_LAT_MAX,
        WORLD_LON_MAX,
    )


class StateManager:
    def __init__(
        self,
        config_path,
        log_path,
        baseline_path=None,
        auto_baseline_minutes=DEFAULT_AUTO_BASELINE_MINUTES,
        auto_baseline_out=None,
        calibration_path=None,
        claim_lat=None,
        claim_lon=None,
        speed_km_s=DEFAULT_SPEED_KM_S,
        path_stretch=DEFAULT_PATH_STRETCH,
        window_minutes=DEFAULT_WINDOW_MINUTES,
        grid=DEFAULT_GRID_DEG,
        refine=DEFAULT_REFINE_DEG,
        band_factor=DEFAULT_BAND_FACTOR,
        band_window_deg=DEFAULT_BAND_WINDOW_DEG,
    ):
        self.config_path = Path(config_path).expanduser().resolve()
        self.config = load_config(self.config_path)
        self.endpoints = {ep["id"]: ep for ep in self.config.get("endpoints", [])}
        log_path = Path(log_path).expanduser().resolve()
        if log_path.is_dir():
            log_path = log_path / "lattice.jsonl"
        self.log_path = log_path
        self.baseline_path = (
            Path(baseline_path).expanduser().resolve() if baseline_path else None
        )
        self.auto_baseline_minutes = auto_baseline_minutes
        self.auto_baseline_out = Path(auto_baseline_out) if auto_baseline_out else None
        self.calibration_path = (
            Path(calibration_path).expanduser().resolve() if calibration_path else None
        )
        self.claim_lat = claim_lat
        self.claim_lon = claim_lon
        self.speed_km_s = speed_km_s
        self.path_stretch = max(1.0, path_stretch)
        self.effective_speed_km_s = self.speed_km_s / self.path_stretch
        self.window_minutes = window_minutes
        self.grid = grid
        self.refine = refine
        self.band_factor = band_factor
        self.band_window_deg = band_window_deg
        self.estimate_interval_ms = DEFAULT_ESTIMATE_INTERVAL_MS

        self._lock = threading.Lock()
        self._offset = 0
        self._samples = {}
        self._burst_meta = {}
        self._last_update = 0
        self._session_start_ms = None

        self._baseline_stats = None
        if self.baseline_path and self.baseline_path.exists():
            self._baseline_stats = compute_stats(load_records(self.baseline_path))
        self._calibration = None
        if self.calibration_path and self.calibration_path.exists():
            self._calibration = load_calibration(self.calibration_path)
        self._log_inode = None
        self._log_size = 0
        self._log_missing = False
        self._log_error = None
        self._log_reset_reason = None
        self._log_reset_ms = None
        self._auto_baseline_enabled = (
            self._baseline_stats is None and self.auto_baseline_minutes > 0
        )
        self._auto_baseline_start_ms = None
        self._auto_baseline_end_ms = None
        self._auto_baseline_records = []
        self._auto_baseline_lines = []
        self._auto_baseline_complete = False
        self._estimate_cache = None
        self._estimate_cache_ms = 0

    def mark_session(self):
        with self._lock:
            self._session_start_ms = int(time.time() * MS_PER_SEC)

    def set_config(self, cfg):
        with self._lock:
            self.config = cfg
            self.endpoints = {ep["id"]: ep for ep in cfg.get("endpoints", [])}

    def set_calibration(self, cal, path=None):
        with self._lock:
            self._calibration = cal
            if path:
                self.calibration_path = Path(path).expanduser().resolve()

    def clear_calibration(self):
        with self._lock:
            self._calibration = None
            self.calibration_path = None

    def generate_calibration(self, lat, lon, output_path=None, prefer_baseline=True):
        if lat is None or lon is None:
            return {"ok": False, "error": "lat/lon required"}
        with self._lock:
            cfg = dict(self.config)
            baseline_stats = self._baseline_stats
            samples = {k: list(v) for k, v in self._samples.items()}
            speed_km_s = self.speed_km_s
            path_stretch = self.path_stretch
            prev_cal = self._calibration
        stats_source = None
        source_label = "baseline"
        if prefer_baseline and baseline_stats:
            stats_source = baseline_stats
        else:
            stats_source = compute_stats_from_samples(samples)
            source_label = "window"
        if not stats_source:
            return {"ok": False, "error": "No stats available yet"}
        cal = build_calibration(
            cfg,
            stats_source,
            lat,
            lon,
            speed_km_s,
            path_stretch,
            previous=prev_cal,
            source=source_label,
        )
        if not cal.get("endpoints"):
            return {"ok": False, "error": "No endpoints with lat/lon in stats"}
        if output_path:
            out_path = Path(output_path).expanduser().resolve()
        else:
            out_path = self.config_path.parent / "calibration.json"
        cal["path"] = str(out_path)
        save_calibration(out_path, cal)
        self.set_calibration(cal, out_path)
        return {
            "ok": True,
            "path": str(out_path),
            "count": len(cal.get("endpoints") or {}),
            "source": source_label,
        }

    def _read_new_lines(self):
        if not self.log_path.exists():
            self._log_missing = True
            self._log_error = None
            return
        try:
            st = self.log_path.stat()
        except Exception as exc:
            self._log_missing = True
            self._log_error = str(exc)
            return
        self._log_missing = False
        self._log_error = None

        if self._log_inode is None:
            self._log_inode = st.st_ino
        else:
            if st.st_ino != self._log_inode:
                self._reset_log_state("rotated")
                self._log_inode = st.st_ino
            elif st.st_size < self._offset:
                self._reset_log_state("truncated")
        self._log_size = st.st_size

        with self.log_path.open("r", encoding="utf-8") as f:
            f.seek(self._offset)
            for line in f:
                raw = line.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                ts = rec.get("tsUnixMs")
                if not isinstance(ts, int):
                    continue
                ep_id = rec.get("endpointId")
                if not ep_id:
                    continue
                vals = rec.get("samplesMs", [])
                if not isinstance(vals, list):
                    continue
                if self._auto_baseline_enabled and not self._auto_baseline_complete:
                    if self._auto_baseline_start_ms is None:
                        self._auto_baseline_start_ms = ts
                        self._auto_baseline_end_ms = ts + int(
                            self.auto_baseline_minutes * MS_PER_MIN
                        )
                    if ts <= self._auto_baseline_end_ms:
                        self._auto_baseline_records.append(rec)
                        if self.auto_baseline_out:
                            self._auto_baseline_lines.append(raw)
                arr = self._samples.setdefault(ep_id, [])
                for v in vals:
                    if isinstance(v, (int, float)) and v >= 0 and v != float("inf"):
                        arr.append((ts, float(v)))
                bursts = self._burst_meta.setdefault(ep_id, [])
                bursts.append((ts, len(vals)))
            self._offset = f.tell()

    def _reset_log_state(self, reason):
        self._offset = 0
        self._samples = {}
        self._burst_meta = {}
        self._log_reset_reason = reason
        self._log_reset_ms = int(time.time() * MS_PER_SEC)

    def _trim_window(self, now_ms):
        if self.window_minutes <= 0:
            return
        cutoff = now_ms - int(self.window_minutes * MS_PER_MIN)
        for ep_id, arr in list(self._samples.items()):
            self._samples[ep_id] = [(ts, v) for (ts, v) in arr if ts >= cutoff]
        for ep_id, arr in list(self._burst_meta.items()):
            self._burst_meta[ep_id] = [(ts, n) for (ts, n) in arr if ts >= cutoff]

    def _maybe_finalize_auto_baseline(self, now_ms):
        if not self._auto_baseline_enabled or self._auto_baseline_complete:
            return
        if self._auto_baseline_end_ms is None or now_ms < self._auto_baseline_end_ms:
            return
        self._baseline_stats = compute_stats(self._auto_baseline_records)
        if self.auto_baseline_out and self._auto_baseline_lines:
            self.auto_baseline_out.parent.mkdir(parents=True, exist_ok=True)
            with self.auto_baseline_out.open("w", encoding="utf-8") as f:
                for line in self._auto_baseline_lines:
                    f.write(line + "\n")
        self._auto_baseline_complete = True
        self._auto_baseline_records = []
        self._auto_baseline_lines = []

    def get_state(self):
        with self._lock:
            self._read_new_lines()
            now_ms = int(time.time() * MS_PER_SEC)
            self._trim_window(now_ms)
            self._maybe_finalize_auto_baseline(now_ms)
            self._last_update = now_ms
            if self._log_reset_ms and now_ms - self._log_reset_ms > LOG_RESET_NOTICE_MS:
                self._log_reset_reason = None
                self._log_reset_ms = None

            session_samples = (
                filter_samples(self._samples, self._session_start_ms)
                if self._session_start_ms
                else self._samples
            )
            session_stats = compute_stats_from_samples(session_samples)
            endpoint_reports = build_endpoint_reports(
                session_stats,
                self.endpoints,
                self.effective_speed_km_s,
                self._calibration,
            )
            endpoint_reports = enrich_with_coords(endpoint_reports, self.endpoints)
            health_reports = build_health_reports(
                self._burst_meta, self.config.get("samplesPerEndpoint", 0)
            )

            claim_checks = None
            if self.claim_lat is not None and self.claim_lon is not None:
                claim_checks = build_claim_checks(
                    session_stats,
                    self.endpoints,
                    self.claim_lat,
                    self.claim_lon,
                    self.effective_speed_km_s,
                    self._calibration,
                )

            estimate = self._estimate_cache
            if (
                not self._estimate_cache_ms
                or now_ms - self._estimate_cache_ms >= self.estimate_interval_ms
            ):
                estimate = estimate_location(
                    session_stats,
                    self.endpoints,
                    self.effective_speed_km_s,
                    self.grid,
                    self.refine,
                    self.band_factor,
                    self.band_window_deg,
                    self._calibration,
                )
                self._estimate_cache = estimate
                self._estimate_cache_ms = now_ms

            baseline_reports = None
            deltas = None
            calibration_drift = None
            if self._baseline_stats is not None:
                baseline_reports = build_endpoint_reports(
                    self._baseline_stats,
                    self.endpoints,
                    self.effective_speed_km_s,
                    self._calibration,
                )
                baseline_reports = enrich_with_coords(baseline_reports, self.endpoints)
                deltas = build_deltas(self._baseline_stats, session_stats)
                calibration_drift = build_calibration_drift(
                    self._baseline_stats, session_stats, self._calibration
                )

            auto_baseline = None
            if self._auto_baseline_enabled:
                auto_baseline = {
                    "enabled": True,
                    "minutes": self.auto_baseline_minutes,
                    "startMs": self._auto_baseline_start_ms,
                    "endMs": self._auto_baseline_end_ms,
                    "complete": self._auto_baseline_complete,
                    "outputPath": str(self.auto_baseline_out)
                    if self.auto_baseline_out
                    else None,
                }

            state = {
                "updatedAt": now_ms,
                "windowMinutes": self.window_minutes,
                "params": {
                    "speedKmS": self.speed_km_s,
                    "effectiveSpeedKmS": self.effective_speed_km_s,
                    "pathStretch": self.path_stretch,
                    "gridDeg": self.grid,
                    "refineDeg": self.refine,
                    "bandFactor": self.band_factor,
                    "bandWindowDeg": self.band_window_deg,
                },
                "endpoints": endpoint_reports,
                "health": health_reports,
                "estimate": estimate,
                "calibration": calibration_meta(self._calibration),
                "calibrationHealth": calibration_health(
                    self._calibration, now_ms, calibration_drift
                ),
                "claimChecks": claim_checks,
                "claim": {"lat": self.claim_lat, "lon": self.claim_lon}
                if self.claim_lat is not None and self.claim_lon is not None
                else None,
                "hygiene": build_endpoint_hygiene(self.config.get("endpoints", [])),
                "logStatus": {
                    "missing": self._log_missing,
                    "error": self._log_error,
                    "resetReason": self._log_reset_reason,
                    "resetAtMs": self._log_reset_ms,
                    "path": str(self.log_path),
                },
                "session": {"startMs": self._session_start_ms}
                if self._session_start_ms
                else None,
                "autoBaseline": auto_baseline,
                "baseline": {
                    "endpoints": baseline_reports,
                    "deltas": deltas,
                    "calibrationDrift": calibration_drift,
                }
                if baseline_reports is not None
                else None,
            }
            return state


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(path, cfg):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def parse_endpoints_text(text, default_port=DEFAULT_PORT):
    raw = text.strip()
    if not raw:
        return []
    if raw[0] in "[{":
        data = json.loads(raw)
        if isinstance(data, dict):
            endpoints = data.get("endpoints") or []
        elif isinstance(data, list):
            endpoints = data
        else:
            raise ValueError("Invalid JSON payload for endpoints")
        out = []
        for ep in endpoints:
            if not isinstance(ep, dict):
                continue
            ep_id = str(ep.get("id", "")).strip()
            host = str(ep.get("host", "")).strip()
            if not ep_id or not host:
                continue
            port = int(ep.get("port") or default_port)
            item = {"id": ep_id, "host": host, "port": port}
            region = ep.get("regionHint") or ep.get("region")
            if region:
                item["regionHint"] = str(region)
            if ep.get("lat") is not None:
                item["lat"] = float(ep["lat"])
            if ep.get("lon") is not None:
                item["lon"] = float(ep["lon"])
            out.append(item)
        return out

    # CSV
    lines = [l for l in raw.splitlines() if l.strip()]
    if not lines:
        return []
    first = lines[0].lower()
    has_header = "id" in first and "host" in first
    out = []
    if has_header:
        reader = csv.DictReader(lines)
        for row in reader:
            ep_id = (row.get("id") or "").strip()
            host = (row.get("host") or "").strip()
            if not ep_id or not host:
                continue
            port = int((row.get("port") or default_port) or default_port)
            item = {"id": ep_id, "host": host, "port": port}
            region = (row.get("region") or row.get("regionHint") or "").strip()
            if region:
                item["regionHint"] = region
            lat = row.get("lat")
            lon = row.get("lon")
            if lat not in (None, ""):
                item["lat"] = float(lat)
            if lon not in (None, ""):
                item["lon"] = float(lon)
            out.append(item)
        return out

    reader = csv.reader(lines)
    for row in reader:
        if not row or len(row) < 2:
            continue
        ep_id = row[0].strip()
        host = row[1].strip()
        if not ep_id or not host:
            continue
        port = int(row[2]) if len(row) > 2 and row[2].strip() else default_port
        item = {"id": ep_id, "host": host, "port": port}
        if len(row) > 3 and row[3].strip():
            item["regionHint"] = row[3].strip()
        if len(row) > 4 and row[4].strip():
            item["lat"] = float(row[4])
        if len(row) > 5 and row[5].strip():
            item["lon"] = float(row[5])
        out.append(item)
    return out


def parse_probe_paths_text(text):
    raw = text.strip()
    if not raw:
        return []
    if raw[0] in "[{":
        data = json.loads(raw)
        if isinstance(data, dict):
            paths = data.get("probePaths") or []
        elif isinstance(data, list):
            paths = data
        else:
            raise ValueError("Invalid JSON payload for probePaths")
        out = []
        for p in paths:
            if not isinstance(p, dict):
                continue
            pid = str(p.get("id", "")).strip()
            if not pid:
                continue
            item = {"id": pid}
            bind_iface = p.get("bindInterface")
            bind_ip = p.get("bindIp")
            if bind_iface:
                item["bindInterface"] = str(bind_iface)
            if bind_ip:
                item["bindIp"] = str(bind_ip)
            out.append(item)
        return out

    # CSV
    lines = [l for l in raw.splitlines() if l.strip()]
    if not lines:
        return []
    first = lines[0].lower()
    has_header = "id" in first
    out = []
    if has_header:
        reader = csv.DictReader(lines)
        for row in reader:
            pid = (row.get("id") or "").strip()
            if not pid:
                continue
            item = {"id": pid}
            bind_iface = (row.get("bindInterface") or row.get("bind_interface") or "").strip()
            bind_ip = (row.get("bindIp") or row.get("bind_ip") or "").strip()
            if bind_iface:
                item["bindInterface"] = bind_iface
            if bind_ip:
                item["bindIp"] = bind_ip
            out.append(item)
        return out

    reader = csv.reader(lines)
    for row in reader:
        if not row or len(row) < 1:
            continue
        pid = row[0].strip()
        if not pid:
            continue
        item = {"id": pid}
        if len(row) > 1 and row[1].strip():
            item["bindInterface"] = row[1].strip()
        if len(row) > 2 and row[2].strip():
            item["bindIp"] = row[2].strip()
        out.append(item)
    return out


def validate_endpoint_list(endpoints):
    errors = []
    seen = set()
    for idx, ep in enumerate(endpoints, start=1):
        ep_id = ep.get("id", "")
        host = ep.get("host", "")
        port = ep.get("port")
        if not ep_id:
            errors.append(f"Row {idx}: missing id")
        if not host:
            errors.append(f"Row {idx}: missing host")
        if ep_id in seen:
            errors.append(f"Row {idx}: duplicate id {ep_id}")
        seen.add(ep_id)
        if port is None or not (1 <= int(port) <= 65535):
            errors.append(f"Row {idx}: invalid port {port}")
        lat = ep.get("lat")
        lon = ep.get("lon")
        if lat is not None and not (-WORLD_LAT_MAX <= float(lat) <= WORLD_LAT_MAX):
            errors.append(f"Row {idx}: invalid lat {lat}")
        if lon is not None and not (-WORLD_LON_MAX <= float(lon) <= WORLD_LON_MAX):
            errors.append(f"Row {idx}: invalid lon {lon}")
    return errors


def validate_probe_paths(paths):
    errors = []
    seen = set()
    for idx, p in enumerate(paths, start=1):
        pid = p.get("id", "")
        if not pid:
            errors.append(f"Path {idx}: missing id")
        if pid in seen:
            errors.append(f"Path {idx}: duplicate id {pid}")
        seen.add(pid)
        bind_ip = p.get("bindIp")
        if bind_ip:
            try:
                ipaddress.ip_address(bind_ip)
            except ValueError:
                errors.append(f"Path {idx}: invalid bindIp {bind_ip}")
    return errors


def is_udp_port_in_use(port):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", port))
            return False
    except OSError:
        return True


class ClientRunner:
    def __init__(self, bin_path, config_path, log_path=None):
        self.bin_path = Path(bin_path)
        self.config_path = Path(config_path)
        self.log_path = Path(log_path) if log_path else None
        self.proc = None
        self._log_handle = None

    def start(self):
        if self.proc and self.proc.poll() is None:
            return {"running": True, "pid": self.proc.pid}
        if not self.bin_path.exists():
            return {"running": False, "error": f"client binary not found: {self.bin_path}"}
        if not self.config_path.exists():
            return {
                "running": False,
                "error": f"client config not found: {self.config_path}",
            }
        stdout = stderr = subprocess.DEVNULL
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_handle = self.log_path.open("a", encoding="utf-8")
            stdout = self._log_handle
            stderr = self._log_handle
        self.proc = subprocess.Popen(
            [str(self.bin_path), str(self.config_path)],
            stdout=stdout,
            stderr=stderr,
            cwd=str(self.bin_path.parent),
            start_new_session=True,
        )
        return {"running": True, "pid": self.proc.pid}

    def stop(self):
        if not self.proc or self.proc.poll() is not None:
            return {"running": False}
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
        except Exception:
            self.proc.terminate()
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except Exception:
                self.proc.kill()
        if self._log_handle:
            self._log_handle.close()
            self._log_handle = None
        return {"running": False}

    def status(self):
        if not self.proc:
            return {"running": False}
        running = self.proc.poll() is None
        return {"running": running, "pid": self.proc.pid if running else None}


class ServerRunner:
    def __init__(self, server_dir, server_cmd, secret_hex, log_path=None):
        self.server_dir = Path(server_dir)
        self.server_cmd = server_cmd
        self.secret_hex = secret_hex
        self.log_path = Path(log_path) if log_path else None
        self.proc = None
        self._log_handle = None

    def start(self):
        if self.proc and self.proc.poll() is None:
            return {"running": True, "pid": self.proc.pid}
        if not self.server_dir.exists():
            return {
                "running": False,
                "error": f"server directory not found: {self.server_dir}",
            }
        stdout = stderr = subprocess.DEVNULL
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_handle = self.log_path.open("a", encoding="utf-8")
            stdout = self._log_handle
            stderr = self._log_handle
        env = os.environ.copy()
        if self.secret_hex:
            env["LATTICE_SECRET_HEX"] = self.secret_hex
        try:
            self.proc = subprocess.Popen(
                self.server_cmd,
                stdout=stdout,
                stderr=stderr,
                cwd=str(self.server_dir),
                env=env,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            return {"running": False, "error": str(exc)}
        return {"running": True, "pid": self.proc.pid}

    def stop(self):
        if not self.proc or self.proc.poll() is not None:
            return {"running": False}
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
        except Exception:
            self.proc.terminate()
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except Exception:
                self.proc.kill()
        if self._log_handle:
            self._log_handle.close()
            self._log_handle = None
        return {"running": False}

    def status(self):
        if not self.proc:
            return {"running": False}
        running = self.proc.poll() is None
        return {"running": running, "pid": self.proc.pid if running else None}


def load_records(path):
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def load_calibration(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                data.setdefault("path", str(path))
            return data
    except Exception:
        return None


def save_calibration(path, cal):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(cal, f, indent=2)


def _append_calibration_sample(samples, ep_id, sample, max_samples):
    entries = samples.get(ep_id)
    if entries is None:
        entries = []
        samples[ep_id] = entries
    entries.append(sample)
    if len(entries) > max_samples:
        entries[:] = entries[-max_samples:]


def _fit_calibration_curve(samples):
    points = [
        (s.get("expectedMs"), s.get("rttMs"))
        for s in samples
        if isinstance(s.get("expectedMs"), (int, float))
        and isinstance(s.get("rttMs"), (int, float))
    ]
    if not points:
        return 0.0, 1.0, None
    if len(points) < 2:
        expected, rtt = points[-1]
        bias = max(0.0, float(rtt) - float(expected))
        return bias, 1.0, None
    xs = [float(p[0]) for p in points]
    ys = [float(p[1]) for p in points]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    var_x = sum((x - mean_x) ** 2 for x in xs) / len(xs)
    if var_x <= 0:
        bias = max(0.0, mean_y - mean_x)
        return bias, 1.0, None
    cov_xy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / len(xs)
    scale = cov_xy / var_x
    if scale < MIN_CALIBRATION_SCALE:
        scale = MIN_CALIBRATION_SCALE
    if scale > MAX_CALIBRATION_SCALE:
        scale = MAX_CALIBRATION_SCALE
    bias = mean_y - scale * mean_x
    if bias < 0:
        bias = 0.0
    rmse = sqrt(
        sum((y - (bias + scale * x)) ** 2 for x, y in zip(xs, ys)) / len(xs)
    )
    return bias, scale, rmse


def build_calibration(cfg, stats, lat, lon, speed_km_s, path_stretch, previous=None, source="baseline"):
    endpoints_cfg = cfg.get("endpoints") or []
    effective_speed = speed_km_s / max(1.0, path_stretch)
    now_ms = int(time.time() * MS_PER_SEC)
    prev_samples = {}
    if previous and isinstance(previous, dict):
        prev_samples = previous.get("samples") or {}
    samples = {}
    if isinstance(prev_samples, dict):
        for key, value in prev_samples.items():
            if isinstance(value, list):
                samples[key] = list(value)
    endpoints = {}
    for ep_id, st in stats.items():
        base_id = ep_id.split("@", 1)[0]
        ep = next((e for e in endpoints_cfg if e.get("id") == base_id), None)
        if not ep:
            continue
        ep_lat = ep.get("lat")
        ep_lon = ep.get("lon")
        if ep_lat is None or ep_lon is None:
            continue
        rtt = st.get("p05") or st.get("min")
        if not isinstance(rtt, (int, float)) or rtt <= 0:
            continue
        dist_km = haversine_km(lat, lon, ep_lat, ep_lon)
        speed_km_ms = effective_speed / MS_PER_SEC
        expected = RTT_FACTOR * dist_km / speed_km_ms
        sample = {
            "lat": lat,
            "lon": lon,
            "distKm": dist_km,
            "expectedMs": expected,
            "rttMs": float(rtt),
            "source": source,
            "ts": now_ms,
        }
        _append_calibration_sample(samples, ep_id, sample, MAX_CALIBRATION_SAMPLES)

    for ep_id, ep_samples in samples.items():
        bias_ms, scale, rmse = _fit_calibration_curve(ep_samples)
        entry = {"biasMs": bias_ms, "scale": scale, "sampleCount": len(ep_samples)}
        if rmse is not None:
            entry["rmseMs"] = rmse
        endpoints[ep_id] = entry
    return {
        "generatedAt": now_ms,
        "calibrationLat": lat,
        "calibrationLon": lon,
        "speedKmS": speed_km_s,
        "pathStretch": max(1.0, path_stretch),
        "endpoints": endpoints,
        "samples": samples,
    }


def calibration_meta(cal):
    if not cal:
        return None
    endpoints = cal.get("endpoints") or {}
    samples = cal.get("samples") or {}
    sample_count = sum(len(v) for v in samples.values()) if isinstance(samples, dict) else 0
    return {
        "path": cal.get("path"),
        "generatedAt": cal.get("generatedAt"),
        "calibrationLat": cal.get("calibrationLat"),
        "calibrationLon": cal.get("calibrationLon"),
        "count": len(endpoints),
        "sampleCount": sample_count,
    }


def calibration_health(cal, now_ms, drift):
    if not cal:
        return None
    generated = cal.get("generatedAt")
    try:
        gen_ms = int(generated)
        if gen_ms < 1_000_000_000_000:
            gen_ms = gen_ms * MS_PER_SEC
    except Exception:
        gen_ms = None
    age_ms = now_ms - gen_ms if gen_ms else None
    drift_warn = None
    if drift and drift.get("medianAbsMs") is not None:
        drift_warn = drift.get("medianAbsMs") >= CALIB_DRIFT_WARN_MS
    return {
        "generatedAt": generated,
        "ageMs": age_ms,
        "drift": drift,
        "driftWarn": drift_warn,
        "warnThresholdMs": CALIB_DRIFT_WARN_MS,
    }


def calibration_entry(cal, endpoint_id):
    if not cal:
        return None
    endpoints = cal.get("endpoints") or {}
    if endpoint_id in endpoints:
        return endpoints[endpoint_id]
    if "@" in endpoint_id:
        base = endpoint_id.split("@", 1)[0]
        return endpoints.get(base)
    return None


def adjust_rtt_ms(rtt_ms, endpoint_id, cal):
    if rtt_ms is None:
        return None
    entry = calibration_entry(cal, endpoint_id)
    if not entry:
        return rtt_ms
    bias = entry.get("biasMs") or entry.get("bias_ms") or 0.0
    scale = entry.get("scale") or entry.get("stretch") or 1.0
    try:
        bias = float(bias)
        scale = float(scale)
    except Exception:
        return rtt_ms
    if scale <= 0:
        scale = 1.0
    adj = (rtt_ms - bias) / scale
    if adj < 0:
        adj = 0.0
    return adj


def median(values):
    if not values:
        return None
    values = sorted(values)
    n = len(values)
    mid = n // 2
    if n % 2 == 1:
        return float(values[mid])
    return (values[mid - 1] + values[mid]) / 2.0


def build_calibration_drift(baseline_stats, session_stats, calibration):
    if not baseline_stats or not session_stats or not calibration:
        return None
    deltas = []
    for ep_id, b in baseline_stats.items():
        s = session_stats.get(ep_id)
        if not s:
            continue
        b_p05 = adjust_rtt_ms(b.get("p05"), ep_id, calibration)
        s_p05 = adjust_rtt_ms(s.get("p05"), ep_id, calibration)
        if b_p05 is None or s_p05 is None:
            continue
        delta = s_p05 - b_p05
        deltas.append({"id": ep_id, "deltaMs": delta})
    if not deltas:
        return None
    abs_vals = [abs(d["deltaMs"]) for d in deltas]
    median_abs = median(abs_vals)
    max_abs = max(abs_vals) if abs_vals else None
    worst = sorted(deltas, key=lambda d: abs(d["deltaMs"]), reverse=True)[:3]
    return {
        "count": len(deltas),
        "medianAbsMs": median_abs,
        "maxAbsMs": max_abs,
        "worst": worst,
    }


def build_endpoint_hygiene(endpoints):
    missing_coords = []
    missing_region = []
    host_map = {}
    for ep in endpoints or []:
        ep_id = ep.get("id") or "?"
        host = ep.get("host") or ""
        lat = ep.get("lat")
        lon = ep.get("lon")
        if lat is None or lon is None:
            missing_coords.append(ep_id)
        region = ep.get("regionHint") or ep.get("region")
        if not region:
            missing_region.append(ep_id)
        if host:
            host_map.setdefault(host, []).append(ep_id)
    duplicate_hosts = [
        {"host": host, "ids": ids}
        for host, ids in host_map.items()
        if len(ids) > 1
    ]
    return {
        "missingCoords": missing_coords,
        "missingRegion": missing_region,
        "duplicateHosts": duplicate_hosts,
    }


def compute_stats(records):
    samples = {}
    for rec in records:
        ep = rec.get("endpointId")
        if not ep:
            continue
        vals = rec.get("samplesMs", [])
        if not isinstance(vals, list):
            continue
        arr = samples.setdefault(ep, [])
        ts = rec.get("tsUnixMs")
        if not isinstance(ts, int):
            ts = 0
        for v in vals:
            if isinstance(v, (int, float)) and v >= 0:
                arr.append((ts, float(v)))
    return compute_stats_from_samples(samples)


def filter_samples(samples, min_ts):
    if min_ts is None:
        return samples
    out = {}
    for ep_id, arr in samples.items():
        out[ep_id] = [(ts, v) for (ts, v) in arr if ts >= min_ts]
    return out


def compute_stats_from_samples(samples):
    stats = {}
    for ep_id, arr in samples.items():
        if not arr:
            continue
        vals = [v for (_, v) in arr if isinstance(v, (int, float))]
        if not vals:
            continue
        s = pd.Series(vals)
        p05 = float(s.quantile(0.05))
        p50 = float(s.quantile(0.50))
        p95 = float(s.quantile(0.95))
        jitter = p95 - p05 if p95 >= p05 else None
        stats[ep_id] = {
            "count": len(vals),
            "p05": p05,
            "p50": p50,
            "p95": p95,
            "min": float(s.min()),
            "jitter": jitter,
        }
    return stats


def build_endpoint_reports(stats, endpoints, speed_km_s, calibration=None):
    out = []
    for ep_id in sorted(stats.keys()):
        st = stats[ep_id]
        ep = endpoints.get(ep_id, {})
        p05 = st.get("p05")
        p50 = st.get("p50")
        p05_adj = adjust_rtt_ms(p05, ep_id, calibration)
        p50_adj = adjust_rtt_ms(p50, ep_id, calibration)
        out.append(
            {
                "id": ep_id,
                "host": ep.get("host", "?"),
                "count": st.get("count", 0),
                "p05Ms": p05,
                "p50Ms": p50,
                "p95Ms": st.get("p95"),
                "jitterMs": st.get("jitter"),
                "p05AdjMs": p05_adj,
                "p50AdjMs": p50_adj,
                "maxDistKmTight": max_distance_km(p05_adj, speed_km_s),
                "maxDistKmLoose": max_distance_km(p50_adj, speed_km_s),
            }
        )
    return out


def build_health_reports(burst_meta, samples_per_endpoint):
    out = []
    for ep_id in sorted(burst_meta.keys()):
        bursts = burst_meta.get(ep_id, [])
        burst_count = len(bursts)
        sample_count = sum(n for (_, n) in bursts)
        expected = burst_count * samples_per_endpoint if samples_per_endpoint else None
        loss_pct = None
        if expected and expected > 0:
            loss_pct = max(0.0, (expected - sample_count) / expected * 100.0)
        last_seen = max((ts for (ts, _) in bursts), default=None)
        out.append(
            {
                "id": ep_id,
                "burstCount": burst_count,
                "sampleCount": sample_count,
                "expectedSamples": expected,
                "lossPct": loss_pct,
                "lastSeenMs": last_seen,
            }
        )
    return out


def enrich_with_coords(reports, endpoints):
    for r in reports:
        ep = endpoints.get(r.get("id"))
        if not ep:
            continue
        r["lat"] = ep.get("lat")
        r["lon"] = ep.get("lon")
    return reports


def build_claim_checks(stats, endpoints, claim_lat, claim_lon, speed_km_s, calibration=None):
    out = []
    for ep_id in sorted(stats.keys()):
        st = stats[ep_id]
        ep = endpoints.get(ep_id)
        if not ep:
            continue
        lat = ep.get("lat")
        lon = ep.get("lon")
        if lat is None or lon is None:
            continue
        dist_km = haversine_km(claim_lat, claim_lon, lat, lon)
        p05_adj = adjust_rtt_ms(st.get("p05"), ep_id, calibration)
        p50_adj = adjust_rtt_ms(st.get("p50"), ep_id, calibration)
        tight = max_distance_km(p05_adj, speed_km_s)
        loose = max_distance_km(p50_adj, speed_km_s)
        out.append(
            {
                "id": ep_id,
                "distKm": dist_km,
                "maxTightKm": tight,
                "maxLooseKm": loose,
                "falsifyTight": dist_km > tight if tight is not None else None,
                "falsifyLoose": dist_km > loose if loose is not None else None,
            }
        )
    return out


def build_deltas(baseline, session):
    out = []
    for ep_id in sorted(baseline.keys()):
        if ep_id not in session:
            continue
        b = baseline[ep_id]
        s = session[ep_id]
        if b.get("p05") is None or s.get("p05") is None:
            continue
        out.append(
            {
                "id": ep_id,
                "deltaP05Ms": s["p05"] - b["p05"],
                "baselineP05Ms": b["p05"],
                "sessionP05Ms": s["p05"],
            }
        )
    return out


def estimate_location(
    stats,
    endpoints,
    speed_km_s,
    grid,
    refine,
    band_factor,
    band_window_deg,
    calibration=None,
):
    obs = []
    for ep_id, st in stats.items():
        ep = endpoints.get(ep_id)
        if not ep:
            continue
        lat = ep.get("lat")
        lon = ep.get("lon")
        if lat is None or lon is None:
            continue
        rtt_raw = st.get("p05") or st.get("min")
        rtt = adjust_rtt_ms(rtt_raw, ep_id, calibration)
        if rtt is None or rtt <= 0:
            continue
        jitter = st.get("jitter") or MIN_JITTER_MS
        obs.append((lat, lon, rtt, max(jitter, MIN_JITTER_MS)))
    if len(obs) < 3:
        return None

    best_lat, best_lon, best_sse, best_bias = grid_search(obs, speed_km_s, grid)
    window = max(grid, refine * REFINE_WINDOW_MULT)
    ref_lat, ref_lon, ref_sse, ref_bias = grid_search_bounds(
        obs,
        speed_km_s,
        best_lat - window,
        best_lat + window,
        best_lon - window,
        best_lon + window,
        refine,
    )

    tight_factor = max(0.05, band_factor * 0.5)
    band_tight = fit_band(
        obs,
        speed_km_s,
        ref_lat,
        ref_lon,
        ref_sse,
        refine,
        tight_factor,
        max(band_window_deg, window),
    )
    band_loose = fit_band(
        obs,
        speed_km_s,
        ref_lat,
        ref_lon,
        ref_sse,
        refine,
        band_factor,
        max(band_window_deg, window),
    )

    return {
        "lat": ref_lat,
        "lon": ref_lon,
        "biasMs": ref_bias,
        "sse": ref_sse,
        "points": len(obs),
        "bandTight": band_tight,
        "bandLoose": band_loose,
    }


def grid_search(obs, speed_km_s, step):
    return grid_search_bounds(
        obs,
        speed_km_s,
        -WORLD_LAT_MAX,
        WORLD_LAT_MAX,
        -WORLD_LON_MAX,
        WORLD_LON_MAX,
        step,
    )


def grid_search_bounds(obs, speed_km_s, lat_min, lat_max, lon_min, lon_max, step):
    lat = max(lat_min, -WORLD_LAT_MAX)
    best = None
    while lat <= min(lat_max, WORLD_LAT_MAX):
        lon = lon_min
        while lon <= lon_max:
            sse, bias = sse_for_candidate(lat, lon, obs, speed_km_s)
            if best is None or sse < best[2]:
                best = (lat, lon, sse, bias)
            lon += step
        lat += step
    return best


def sse_for_candidate(lat, lon, obs, speed_km_s):
    speed_km_ms = speed_km_s / MS_PER_SEC
    sum_w = 0.0
    sum_wx = 0.0
    for o in obs:
        dist = haversine_km(lat, lon, o[0], o[1])
        pred_no_bias = RTT_FACTOR * dist / speed_km_ms
        w = 1.0 / o[3]
        sum_w += w
        sum_wx += w * (o[2] - pred_no_bias)
    bias = sum_wx / sum_w if sum_w > 0 else 0.0
    if bias < 0:
        bias = 0.0
    sse = 0.0
    for o in obs:
        dist = haversine_km(lat, lon, o[0], o[1])
        pred = RTT_FACTOR * dist / speed_km_ms + bias
        w = 1.0 / o[3]
        err = o[2] - pred
        sse += w * err * err
    return sse, bias


def fit_band(obs, speed_km_s, center_lat, center_lon, best_sse, step, factor, window_deg):
    if step <= 0:
        return None
    threshold = max(best_sse * (1.0 + factor), best_sse + SSE_EPSILON)
    lat_min = max(center_lat - window_deg, -WORLD_LAT_MAX)
    lat_max = min(center_lat + window_deg, WORLD_LAT_MAX)
    lon_min = center_lon - window_deg
    lon_max = center_lon + window_deg

    min_lat = center_lat
    max_lat = center_lat
    min_lon = center_lon
    max_lon = center_lon
    max_dist = 0.0
    points = 0
    sum_dx = 0.0
    sum_dy = 0.0
    sum_dx2 = 0.0
    sum_dy2 = 0.0
    sum_dxdy = 0.0
    km_per_deg = (2.0 * pi * EARTH_RADIUS_KM) / 360.0

    lat = lat_min
    while lat <= lat_max:
        lon = lon_min
        while lon <= lon_max:
            sse, _ = sse_for_candidate(lat, lon, obs, speed_km_s)
            if sse <= threshold:
                points += 1
                dist = haversine_km(center_lat, center_lon, lat, lon)
                max_dist = max(max_dist, dist)
                min_lat = min(min_lat, lat)
                max_lat = max(max_lat, lat)
                min_lon = min(min_lon, lon)
                max_lon = max(max_lon, lon)
                dx = (lon - center_lon) * cos(radians(center_lat)) * km_per_deg
                dy = (lat - center_lat) * km_per_deg
                sum_dx += dx
                sum_dy += dy
                sum_dx2 += dx * dx
                sum_dy2 += dy * dy
                sum_dxdy += dx * dy
            lon += step
        lat += step

    if points == 0:
        return None

    ellipse = None
    if points >= 2:
        mean_dx = sum_dx / points
        mean_dy = sum_dy / points
        var_x = sum_dx2 / points - mean_dx * mean_dx
        var_y = sum_dy2 / points - mean_dy * mean_dy
        cov_xy = sum_dxdy / points - mean_dx * mean_dy
        if var_x < 0:
            var_x = 0.0
        if var_y < 0:
            var_y = 0.0
        trace = var_x + var_y
        det = var_x * var_y - cov_xy * cov_xy
        term = trace * trace / 4.0 - det
        if term < 0:
            term = 0.0
        root = sqrt(term)
        eig1 = trace / 2.0 + root
        eig2 = trace / 2.0 - root
        major = sqrt(eig1) if eig1 > 0 else 0.0
        minor = sqrt(eig2) if eig2 > 0 else 0.0
        angle = 0.5 * (180.0 / pi) * atan2(
            2.0 * cov_xy, var_x - var_y
        )
        ellipse = {"majorKm": major, "minorKm": minor, "angleDeg": angle}

    return {
        "radiusKm": max_dist,
        "sseThreshold": threshold,
        "points": points,
        "minLat": min_lat,
        "maxLat": max_lat,
        "minLon": min_lon,
        "maxLon": max_lon,
        "ellipse": ellipse,
    }


def max_distance_km(rtt_ms, speed_km_s):
    if rtt_ms is None or rtt_ms <= 0:
        return None
    speed_km_ms = speed_km_s / MS_PER_SEC
    return speed_km_ms * (rtt_ms / RTT_FACTOR)


def haversine_km(lat1, lon1, lat2, lon2):
    r = EARTH_RADIUS_KM
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    lat1 = radians(lat1)
    lat2 = radians(lat2)
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    c = 2 * asin(sqrt(a))
    return r * c


class Api:
    def __init__(self, state_mgr, client_runner, server_runner):
        self.state_mgr = state_mgr
        self.client_runner = client_runner
        self.server_runner = server_runner
        # Prevent pywebview from introspecting these objects for API exposure.
        self.state_mgr._serializable = False
        self.client_runner._serializable = False
        self.server_runner._serializable = False
        self._calib_lock = threading.Lock()
        self._calib_job = {
            "running": False,
            "kind": None,
            "startedAt": None,
            "finishedAt": None,
            "error": None,
            "result": None,
        }

    def get_state(self):
        state = self.state_mgr.get_state()
        state["client"] = self.client_runner.status()
        state["server"] = self.server_runner.status()
        state["configPath"] = str(self.state_mgr.config_path)
        state["logPath"] = str(self.state_mgr.log_path)
        state["calibrationJob"] = self.get_calibration_status()
        return state

    def mark_session(self):
        self.state_mgr.mark_session()
        return {"ok": True, "startMs": self.state_mgr._session_start_ms}

    def get_config(self):
        return self.state_mgr.config

    def validate_endpoints(self, payload):
        text = (payload or {}).get("text", "")
        try:
            endpoints = parse_endpoints_text(text)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        if not endpoints:
            return {"ok": False, "error": "No valid endpoints found"}
        errors = validate_endpoint_list(endpoints)
        return {"ok": len(errors) == 0, "count": len(endpoints), "errors": errors}

    def validate_probe_paths(self, payload):
        text = (payload or {}).get("text", "")
        try:
            paths = parse_probe_paths_text(text)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        errors = validate_probe_paths(paths)
        return {"ok": len(errors) == 0, "count": len(paths), "errors": errors}

    def set_endpoints(self, payload):
        text = (payload or {}).get("text", "")
        try:
            endpoints = parse_endpoints_text(text)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        if not endpoints:
            return {"ok": False, "error": "No valid endpoints found"}
        errors = validate_endpoint_list(endpoints)
        if errors:
            return {"ok": False, "error": "Validation failed", "errors": errors}
        cfg = dict(self.state_mgr.config)
        cfg["endpoints"] = endpoints
        save_config(self.state_mgr.config_path, cfg)
        self.state_mgr.set_config(cfg)
        return {"ok": True, "count": len(endpoints)}

    def set_config_parts(self, payload):
        payload = payload or {}
        endpoints_text = payload.get("endpointsText", "")
        paths_text = payload.get("probePathsText", "")
        try:
            endpoints = parse_endpoints_text(endpoints_text)
        except Exception as exc:
            return {"ok": False, "error": str(exc), "endpoints": True}
        if not endpoints:
            return {"ok": False, "error": "No valid endpoints found", "endpoints": True}
        try:
            paths = parse_probe_paths_text(paths_text)
        except Exception as exc:
            return {"ok": False, "error": str(exc), "probePaths": True}

        endpoint_errors = validate_endpoint_list(endpoints)
        path_errors = validate_probe_paths(paths)
        if endpoint_errors or path_errors:
            return {
                "ok": False,
                "error": "Validation failed",
                "endpointErrors": endpoint_errors,
                "probePathErrors": path_errors,
            }

        cfg = dict(self.state_mgr.config)
        cfg["endpoints"] = endpoints
        if paths:
            cfg["probePaths"] = paths
        else:
            cfg.pop("probePaths", None)
        save_config(self.state_mgr.config_path, cfg)
        self.state_mgr.set_config(cfg)
        return {"ok": True, "count": len(endpoints), "pathCount": len(paths)}

    def _start_calibration_job(self, kind, fn):
        with self._calib_lock:
            if self._calib_job.get("running"):
                return {"ok": False, "error": "Calibration already running"}
            self._calib_job = {
                "running": True,
                "kind": kind,
                "startedAt": int(time.time() * MS_PER_SEC),
                "finishedAt": None,
                "error": None,
                "result": None,
            }

        def runner():
            result = None
            error = None
            try:
                result = fn()
                if isinstance(result, dict) and result.get("ok") is False:
                    error = result.get("error")
            except Exception as exc:
                error = str(exc)
            with self._calib_lock:
                self._calib_job["running"] = False
                self._calib_job["finishedAt"] = int(time.time() * MS_PER_SEC)
                self._calib_job["error"] = error
                self._calib_job["result"] = result

        threading.Thread(target=runner, daemon=True).start()
        return {"ok": True, "running": True}

    def get_calibration_status(self):
        with self._calib_lock:
            return dict(self._calib_job)

    def generate_calibration(self, payload):
        payload = payload or {}
        lat = payload.get("lat")
        lon = payload.get("lon")
        output_path = payload.get("outputPath") or None
        try:
            lat = float(lat)
            lon = float(lon)
        except Exception:
            return {"ok": False, "error": "Invalid lat/lon"}
        return self._start_calibration_job(
            "generate",
            lambda: self.state_mgr.generate_calibration(lat, lon, output_path, True),
        )

    def load_calibration(self, payload):
        payload = payload or {}
        path = payload.get("path")
        if not path:
            return {"ok": False, "error": "Path required"}
        def run():
            cal = load_calibration(Path(path).expanduser().resolve())
            if not cal:
                return {"ok": False, "error": "Failed to load calibration"}
            self.state_mgr.set_calibration(cal, cal.get("path") or path)
            return {"ok": True}

        return self._start_calibration_job("load", run)

    def clear_calibration(self):
        return self._start_calibration_job(
            "clear",
            lambda: (self.state_mgr.clear_calibration() or {"ok": True}),
        )

    def start_client(self):
        return self.client_runner.start()

    def stop_client(self):
        return self.client_runner.stop()

    def clear_state(self, payload=None):
        payload = payload or {}
        truncate = payload.get("truncateLog", False)
        with self.state_mgr._lock:
            self.state_mgr._samples = {}
            self.state_mgr._burst_meta = {}
            self.state_mgr._offset = 0
            if truncate:
                try:
                    self.state_mgr.log_path.write_text("", encoding="utf-8")
                except Exception:
                    pass
        return {"ok": True}

    def start_server(self):
        if is_udp_port_in_use(DEFAULT_PORT):
            return {"running": False, "error": f"port {DEFAULT_PORT} already in use"}
        return self.server_runner.start()

    def stop_server(self):
        return self.server_runner.stop()

    def export_state(self, payload=None):
        payload = payload or {}
        state = payload.get("state") or {}
        map_svg = payload.get("mapSvg")

        ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        out_dir = Path(__file__).parent / "exports" / ts
        out_dir.mkdir(parents=True, exist_ok=True)

        (out_dir / "state.json").write_text(
            json.dumps(state, indent=2, sort_keys=False),
            encoding="utf-8",
        )

        if map_svg:
            (out_dir / "map.svg").write_text(map_svg, encoding="utf-8")

        def write_csv(name, rows, headers):
            if not rows:
                return
            path = out_dir / name
            with path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=headers)
                writer.writeheader()
                for row in rows:
                    writer.writerow({k: row.get(k) for k in headers})

        endpoints = state.get("endpoints") or []
        write_csv(
            "endpoints.csv",
            endpoints,
            [
                "id",
                "host",
                "count",
                "p05Ms",
                "p50Ms",
                "p95Ms",
                "jitterMs",
                "maxDistKmTight",
                "maxDistKmLoose",
                "lat",
                "lon",
            ],
        )

        health = state.get("health") or []
        write_csv(
            "health.csv",
            health,
            [
                "id",
                "burstCount",
                "sampleCount",
                "expectedSamples",
                "lossPct",
                "lastSeenMs",
            ],
        )

        claims = state.get("claimChecks") or []
        write_csv(
            "claims.csv",
            claims,
            ["id", "distKm", "maxTightKm", "maxLooseKm", "falsifyTight", "falsifyLoose"],
        )

        deltas = (state.get("baseline") or {}).get("deltas") or []
        write_csv(
            "deltas.csv",
            deltas,
            ["id", "deltaP05Ms", "baselineP05Ms", "sessionP05Ms"],
        )

        return {"ok": True, "path": str(out_dir)}


def main():
    parser = argparse.ArgumentParser(description="LATTICE live dashboard")
    parser.add_argument("--config", required=True, help="Config JSON with endpoints and lat/lon")
    parser.add_argument("--log", required=True, help="Live JSONL log path")
    parser.add_argument("--baseline", help="Baseline JSONL path (optional)")
    parser.add_argument(
        "--auto-baseline-minutes",
        type=int,
        default=DEFAULT_AUTO_BASELINE_MINUTES,
        help="Capture the first N minutes as baseline when --baseline is omitted",
    )
    parser.add_argument(
        "--auto-baseline-out",
        help="Optional output path to save captured baseline JSONL",
    )
    parser.add_argument("--calibration", help="Optional calibration JSON path")
    parser.add_argument("--claim-lat", type=float)
    parser.add_argument("--claim-lon", type=float)
    parser.add_argument("--speed-km-s", type=float, default=DEFAULT_SPEED_KM_S)
    parser.add_argument("--path-stretch", type=float, default=DEFAULT_PATH_STRETCH)
    parser.add_argument("--window-minutes", type=int, default=DEFAULT_WINDOW_MINUTES)
    parser.add_argument("--grid", type=float, default=DEFAULT_GRID_DEG)
    parser.add_argument("--refine", type=float, default=DEFAULT_REFINE_DEG)
    parser.add_argument("--band-factor", type=float, default=DEFAULT_BAND_FACTOR)
    parser.add_argument("--band-window-deg", type=float, default=DEFAULT_BAND_WINDOW_DEG)
    parser.add_argument("--refresh-ms", type=int, default=DEFAULT_REFRESH_MS)
    parser.add_argument("--client-bin", help="Path to lattice client binary")
    parser.add_argument("--client-log", help="Optional client log file path")
    parser.add_argument("--server-dir", help="Path to server directory (Go)")
    parser.add_argument(
        "--server-cmd",
        help='Server command (default: "go run .")',
    )
    parser.add_argument("--server-log", help="Optional server log file path")
    args = parser.parse_args()

    state_mgr = StateManager(
        args.config,
        args.log,
        args.baseline,
        args.auto_baseline_minutes,
        args.auto_baseline_out,
        args.calibration,
        args.claim_lat,
        args.claim_lon,
        args.speed_km_s,
        args.path_stretch,
        args.window_minutes,
        args.grid,
        args.refine,
        args.band_factor,
        args.band_window_deg,
    )

    root = Path(__file__).resolve().parents[1]
    default_client_bin = root / "client-rs" / "target" / "release" / "lattice"
    client_bin = args.client_bin or str(default_client_bin)
    client_log = args.client_log or str(root / "dashboard" / "client.log")

    default_server_dir = root / "server"
    server_dir = args.server_dir or str(default_server_dir)
    server_cmd = shlex.split(args.server_cmd) if args.server_cmd else ["go", "run", "."]
    server_log = args.server_log or str(root / "dashboard" / "server.log")

    html_path = Path(__file__).parent / "assets" / "index.html"
    client_runner = ClientRunner(client_bin, state_mgr.config_path, client_log)
    server_runner = ServerRunner(
        server_dir,
        server_cmd,
        state_mgr.config.get("secretHex"),
        server_log,
    )
    api = Api(state_mgr, client_runner, server_runner)

    window = webview.create_window(
        "LATTICE", html_path.as_uri(), js_api=api, width=1200, height=800
    )

    def inject_refresh():
        window.evaluate_js(f"window.__REFRESH_MS__ = {args.refresh_ms};")

    webview.start(inject_refresh)


if __name__ == "__main__":
    main()
