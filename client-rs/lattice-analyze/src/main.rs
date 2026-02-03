mod constants;

use clap::Parser;
use lattice_core::{now_unix_ms, BurstRecord, Config, Endpoint};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::fs::File;
use std::io::{self, BufRead, BufReader};
use std::path::PathBuf;

use constants::*;

#[derive(Parser, Debug)]
#[command(about = "Analyze LATTICE JSONL logs for physics bounds and location estimates")]
struct Args {
    #[arg(long)]
    config: PathBuf,

    #[arg(long)]
    session: PathBuf,

    #[arg(long)]
    baseline: Option<PathBuf>,

    #[arg(long)]
    claim_lat: Option<f64>,

    #[arg(long)]
    claim_lon: Option<f64>,

    #[arg(long)]
    calibration: Option<PathBuf>,

    #[arg(long)]
    calib_lat: Option<f64>,

    #[arg(long)]
    calib_lon: Option<f64>,

    #[arg(long)]
    calibration_out: Option<PathBuf>,

    #[arg(long, default_value_t = DEFAULT_GRID_DEG)]
    grid: f64,

    #[arg(long, default_value_t = DEFAULT_REFINE_DEG)]
    refine: f64,

    #[arg(long, default_value_t = DEFAULT_SPEED_KM_S)]
    speed_km_s: f64,

    #[arg(long, default_value_t = DEFAULT_PATH_STRETCH)]
    path_stretch: f64,

    #[arg(long, default_value_t = DEFAULT_BAND_FACTOR)]
    band_factor: f64,

    #[arg(long, default_value_t = DEFAULT_BAND_WINDOW_DEG)]
    band_window_deg: f64,

    #[arg(long)]
    json: bool,
}

#[derive(Debug, Clone)]
struct EndpointStats {
    count: usize,
    min: Option<f64>,
    p05: Option<f64>,
    p50: Option<f64>,
    p95: Option<f64>,
    jitter_ms: Option<f64>,
}

#[derive(Debug, Clone)]
struct EndpointObs {
    lat: f64,
    lon: f64,
    rtt_ms: f64,
    jitter_ms: f64,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct Estimate {
    lat: f64,
    lon: f64,
    bias_ms: f64,
    sse: f64,
    points: usize,
    band: Option<FitBand>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct FitBand {
    radius_km: f64,
    sse_threshold: f64,
    points: usize,
    min_lat: f64,
    max_lat: f64,
    min_lon: f64,
    max_lon: f64,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct EndpointReport {
    id: String,
    host: String,
    count: usize,
    p05_ms: Option<f64>,
    p50_ms: Option<f64>,
    p95_ms: Option<f64>,
    jitter_ms: Option<f64>,
    p05_adj_ms: Option<f64>,
    p50_adj_ms: Option<f64>,
    max_dist_km_tight: Option<f64>,
    max_dist_km_loose: Option<f64>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct ClaimCheck {
    id: String,
    dist_km: f64,
    max_tight_km: Option<f64>,
    max_loose_km: Option<f64>,
    falsify_tight: Option<bool>,
    falsify_loose: Option<bool>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct Delta {
    id: String,
    delta_p05_ms: f64,
    baseline_p05_ms: f64,
    session_p05_ms: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct Calibration {
    generated_at: String,
    calibration_lat: f64,
    calibration_lon: f64,
    speed_km_s: f64,
    path_stretch: f64,
    endpoints: HashMap<String, EndpointCalibration>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct EndpointCalibration {
    bias_ms: f64,
    scale: f64,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct SessionOutput {
    label: String,
    records: usize,
    endpoint_stats: Vec<EndpointReport>,
    estimate: Option<Estimate>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct AnalysisOutput {
    params: Params,
    session: SessionOutput,
    baseline: Option<SessionOutput>,
    claim_checks: Option<Vec<ClaimCheck>>,
    deltas: Option<Vec<Delta>>,
    estimate_separation_km: Option<f64>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct Params {
    speed_km_s: f64,
    effective_speed_km_s: f64,
    path_stretch: f64,
    grid_deg: f64,
    refine_deg: f64,
    band_factor: f64,
    band_window_deg: f64,
}

fn main() -> io::Result<()> {
    let args = Args::parse();

    let cfg = Config::load(&args.config)?;
    let endpoints = endpoints_by_id(&cfg.endpoints);

    let path_stretch = if args.path_stretch < 1.0 { 1.0 } else { args.path_stretch };
    let effective_speed = args.speed_km_s / path_stretch;

    let session_records = load_jsonl(&args.session)?;
    let session_stats = build_stats(&session_records);
    let mut calibration = match &args.calibration {
        Some(path) => load_calibration(path).ok(),
        None => None,
    };

    if let Some(out_path) = &args.calibration_out {
        let (lat, lon) = match (args.calib_lat, args.calib_lon) {
            (Some(lat), Some(lon)) => (lat, lon),
            _ => {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "calibrationOut requires --calib-lat and --calib-lon",
                ));
            }
        };
        let calib_stats = if let Some(baseline_path) = &args.baseline {
            let baseline_records = load_jsonl(baseline_path)?;
            build_stats(&baseline_records)
        } else {
            session_stats.clone()
        };
        let cal = build_calibration(
            &cfg,
            &calib_stats,
            lat,
            lon,
            args.speed_km_s,
            path_stretch,
        );
        save_calibration(out_path, &cal)?;
        calibration = Some(cal);
    }

    let session_reports = endpoint_reports(&session_stats, &endpoints, effective_speed, calibration.as_ref());

    let claim = match (args.claim_lat, args.claim_lon) {
        (Some(lat), Some(lon)) => Some((lat, lon)),
        _ => None,
    };
    let claim_checks = claim.map(|(lat, lon)| {
        claim_checks(
            &session_stats,
            &endpoints,
            lat,
            lon,
            effective_speed,
            calibration.as_ref(),
        )
    });

    let session_est = estimate_location(
        &session_stats,
        &endpoints,
        effective_speed,
        args.grid,
        args.refine,
        args.band_factor,
        args.band_window_deg,
        calibration.as_ref(),
    );

    let session_output = SessionOutput {
        label: "session".to_string(),
        records: session_records.len(),
        endpoint_stats: session_reports.clone(),
        estimate: session_est.clone(),
    };

    let mut baseline_output: Option<SessionOutput> = None;
    let mut deltas_out: Option<Vec<Delta>> = None;
    let mut estimate_separation_km: Option<f64> = None;

    if let Some(baseline_path) = args.baseline {
        let baseline_records = load_jsonl(&baseline_path)?;
        let baseline_stats = build_stats(&baseline_records);
        let baseline_reports =
            endpoint_reports(&baseline_stats, &endpoints, effective_speed, calibration.as_ref());

        let baseline_est = estimate_location(
            &baseline_stats,
            &endpoints,
            effective_speed,
            args.grid,
            args.refine,
            args.band_factor,
            args.band_window_deg,
            calibration.as_ref(),
        );

        baseline_output = Some(SessionOutput {
            label: "baseline".to_string(),
            records: baseline_records.len(),
            endpoint_stats: baseline_reports,
            estimate: baseline_est.clone(),
        });

        deltas_out = Some(deltas(&baseline_stats, &session_stats));
        if let (Some(b), Some(s)) = (baseline_est, session_est.clone()) {
            estimate_separation_km = Some(haversine_km(b.lat, b.lon, s.lat, s.lon));
        }
    }

    if args.json {
        let output = AnalysisOutput {
            params: Params {
                speed_km_s: args.speed_km_s,
                effective_speed_km_s: effective_speed,
                path_stretch,
                grid_deg: args.grid,
                refine_deg: args.refine,
                band_factor: args.band_factor,
                band_window_deg: args.band_window_deg,
            },
            session: session_output,
            baseline: baseline_output,
            claim_checks,
            deltas: deltas_out,
            estimate_separation_km,
        };
        let text = serde_json::to_string_pretty(&output)
            .unwrap_or_else(|_| "{\"error\":\"failed to serialize\"}".to_string());
        println!("{text}");
        return Ok(());
    }

    if path_stretch != args.path_stretch {
        println!(
            "Note: path_stretch < 1.0 is invalid; clamped to {:.2}",
            path_stretch
        );
    }
    println!("Session: {} records", session_records.len());
    print_stats_summary("session", &session_reports);

    if let Some((lat, lon)) = claim {
        println!("\nClaim check: lat={:.4}, lon={:.4}", lat, lon);
        if let Some(ref checks) = claim_checks {
            print_claim_checks(checks);
        }
    }

    if let Some(est) = &session_output.estimate {
        println!("\nSession estimate (treats RTTs as direct path; for VPN this approximates exit):");
        print_estimate(est);
    } else {
        println!("\nSession estimate: insufficient endpoint data (need lat/lon + RTTs).")
    }

    if let Some(baseline) = baseline_output {
        println!("\nBaseline: {} records", baseline.records);
        print_stats_summary("baseline", &baseline.endpoint_stats);

        if let Some(est) = baseline.estimate {
            println!("\nBaseline estimate (best-effort physical location):");
            print_estimate(&est);
        } else {
            println!("\nBaseline estimate: insufficient endpoint data (need lat/lon + RTTs).")
        }

        println!("\nBaseline vs Session deltas (p05):");
        if let Some(ref d) = deltas_out {
            print_deltas(d);
        }

        if let Some(dist) = estimate_separation_km {
            println!(
                "\nBaseline vs Session estimate separation: {:.1} km (VPN on often shifts toward exit)",
                dist
            );
        }
    }

    Ok(())
}

fn load_jsonl(path: &PathBuf) -> io::Result<Vec<BurstRecord>> {
    let file = File::open(path)?;
    let reader = BufReader::new(file);
    let mut out = Vec::new();
    for line in reader.lines() {
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }
        match serde_json::from_str::<BurstRecord>(&line) {
            Ok(rec) => out.push(rec),
            Err(_) => {}
        }
    }
    Ok(out)
}

fn load_calibration(path: &PathBuf) -> io::Result<Calibration> {
    let file = File::open(path)?;
    let calib: Calibration = serde_json::from_reader(file)
        .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?;
    Ok(calib)
}

fn save_calibration(path: &PathBuf, calib: &Calibration) -> io::Result<()> {
    let file = File::create(path)?;
    serde_json::to_writer_pretty(file, calib)
        .map_err(|e| io::Error::new(io::ErrorKind::Other, e))
}

fn build_calibration(
    cfg: &Config,
    stats: &HashMap<String, EndpointStats>,
    lat: f64,
    lon: f64,
    speed_km_s: f64,
    path_stretch: f64,
) -> Calibration {
    let effective_speed = speed_km_s / path_stretch.max(MIN_PATH_STRETCH);
    let mut endpoints = HashMap::new();
    for (id, st) in stats {
        let base_id = id.split('@').next().unwrap_or(id);
        let ep = match cfg.endpoints.iter().find(|e| e.id == base_id) {
            Some(ep) => ep,
            None => continue,
        };
        let (Some(ep_lat), Some(ep_lon)) = (ep.lat, ep.lon) else { continue };
        let rtt = match st.p05.or(st.min) {
            Some(v) if v.is_finite() && v > 0.0 => v,
            _ => continue,
        };
        let dist_km = haversine_km(lat, lon, ep_lat, ep_lon);
        let speed_km_ms = effective_speed / MS_PER_SEC;
        let expected = RTT_FACTOR * dist_km / speed_km_ms;
        let bias_ms = (rtt - expected).max(0.0);
        endpoints.insert(
            id.clone(),
            EndpointCalibration {
                bias_ms,
                scale: 1.0,
            },
        );
    }
    Calibration {
        generated_at: format!("{}", now_unix_ms()),
        calibration_lat: lat,
        calibration_lon: lon,
        speed_km_s,
        path_stretch,
        endpoints,
    }
}

fn endpoints_by_id(endpoints: &[Endpoint]) -> HashMap<String, Endpoint> {
    let mut map = HashMap::new();
    for ep in endpoints {
        map.insert(ep.id.clone(), ep.clone());
    }
    map
}

fn build_stats(records: &[BurstRecord]) -> HashMap<String, EndpointStats> {
    let mut samples: HashMap<String, Vec<f64>> = HashMap::new();
    for rec in records {
        let entry = samples.entry(rec.endpoint_id.clone()).or_insert_with(Vec::new);
        for v in &rec.samples_ms {
            if v.is_finite() && *v >= 0.0 {
                entry.push(*v);
            }
        }
    }

    let mut stats = HashMap::new();
    for (id, mut s) in samples {
        s.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
        let count = s.len();
        let min = s.first().copied();
        let p05 = quantile(&s, 0.05);
        let p50 = quantile(&s, 0.50);
        let p95 = quantile(&s, 0.95);
        let jitter_ms = match (p05, p95) {
            (Some(a), Some(b)) if b >= a => Some(b - a),
            _ => None,
        };
        stats.insert(
            id,
            EndpointStats {
                count,
                min,
                p05,
                p50,
                p95,
                jitter_ms,
            },
        );
    }
    stats
}

fn quantile(sorted: &[f64], q: f64) -> Option<f64> {
    if sorted.is_empty() {
        return None;
    }
    let idx = ((sorted.len() - 1) as f64 * q).round() as usize;
    sorted.get(idx).copied()
}

fn calibration_entry<'a>(
    calibration: Option<&'a Calibration>,
    endpoint_id: &str,
) -> Option<&'a EndpointCalibration> {
    let cal = calibration?;
    if let Some(entry) = cal.endpoints.get(endpoint_id) {
        return Some(entry);
    }
    if let Some(base) = endpoint_id.split('@').next() {
        return cal.endpoints.get(base);
    }
    None
}

fn adjust_rtt_ms(rtt_ms: f64, endpoint_id: &str, calibration: Option<&Calibration>) -> f64 {
    if let Some(entry) = calibration_entry(calibration, endpoint_id) {
        let mut scale = entry.scale;
        if scale <= 0.0 {
            scale = 1.0;
        }
        let adj = (rtt_ms - entry.bias_ms) / scale;
        return adj.max(0.0);
    }
    rtt_ms
}

fn endpoint_reports(
    stats: &HashMap<String, EndpointStats>,
    endpoints: &HashMap<String, Endpoint>,
    speed_km_s: f64,
    calibration: Option<&Calibration>,
) -> Vec<EndpointReport> {
    let mut ids: Vec<&String> = stats.keys().collect();
    ids.sort();
    let mut out = Vec::new();
    for id in ids {
        let st = &stats[id];
        let host = endpoints
            .get(id)
            .map(|e| e.host.clone())
            .or_else(|| {
                if let Some(base) = id.split('@').next() {
                    endpoints.get(base).map(|e| e.host.clone())
                } else {
                    None
                }
            })
            .unwrap_or_else(|| "?".to_string());
        let p05_adj = st.p05.map(|v| adjust_rtt_ms(v, id, calibration));
        let p50_adj = st.p50.map(|v| adjust_rtt_ms(v, id, calibration));
        let max_dist_km_tight = st
            .p05
            .map(|v| adjust_rtt_ms(v, id, calibration))
            .and_then(|v| max_distance_km(v, speed_km_s));
        let max_dist_km_loose = st
            .p50
            .map(|v| adjust_rtt_ms(v, id, calibration))
            .and_then(|v| max_distance_km(v, speed_km_s));
        out.push(EndpointReport {
            id: id.clone(),
            host,
            count: st.count,
            p05_ms: st.p05,
            p50_ms: st.p50,
            p95_ms: st.p95,
            jitter_ms: st.jitter_ms,
            p05_adj_ms: p05_adj,
            p50_adj_ms: p50_adj,
            max_dist_km_tight,
            max_dist_km_loose,
        });
    }
    out
}

fn print_stats_summary(label: &str, reports: &[EndpointReport]) {
    println!("\n{} endpoint stats (p05/p50/p95 in ms):", label);
    for r in reports {
        let p05 = r.p05_ms.unwrap_or(f64::NAN);
        let p50 = r.p50_ms.unwrap_or(f64::NAN);
        let p95 = r.p95_ms.unwrap_or(f64::NAN);
        let jitter = r.jitter_ms.unwrap_or(f64::NAN);
        println!(
            "- {} ({}) count={} p05={:.2} p50={:.2} p95={:.2} jitter={:.2}",
            r.id, r.host, r.count, p05, p50, p95, jitter
        );
        if let (Some(tight), Some(loose)) = (r.max_dist_km_tight, r.max_dist_km_loose) {
            println!("  max_dist_km tight={:.1} loose={:.1}", tight, loose);
        }
    }
}

fn claim_checks(
    stats: &HashMap<String, EndpointStats>,
    endpoints: &HashMap<String, Endpoint>,
    claim_lat: f64,
    claim_lon: f64,
    speed_km_s: f64,
    calibration: Option<&Calibration>,
) -> Vec<ClaimCheck> {
    let mut ids: Vec<&String> = stats.keys().collect();
    ids.sort();
    let mut out = Vec::new();
    for id in ids {
        let st = &stats[id];
        let ep = endpoints.get(id).or_else(|| {
            if let Some(base) = id.split('@').next() {
                endpoints.get(base)
            } else {
                None
            }
        });
        let Some(ep) = ep else { continue };
        let (Some(ep_lat), Some(ep_lon)) = (ep.lat, ep.lon) else { continue };
        let dist_km = haversine_km(claim_lat, claim_lon, ep_lat, ep_lon);
        let tight = st
            .p05
            .map(|v| adjust_rtt_ms(v, id, calibration))
            .and_then(|v| max_distance_km(v, speed_km_s));
        let loose = st
            .p50
            .map(|v| adjust_rtt_ms(v, id, calibration))
            .and_then(|v| max_distance_km(v, speed_km_s));
        out.push(ClaimCheck {
            id: id.clone(),
            dist_km,
            max_tight_km: tight,
            max_loose_km: loose,
            falsify_tight: tight.map(|t| dist_km > t),
            falsify_loose: loose.map(|l| dist_km > l),
        });
    }
    out
}

fn print_claim_checks(checks: &[ClaimCheck]) {
    for c in checks {
        let max_tight = c.max_tight_km.unwrap_or(f64::NAN);
        let max_loose = c.max_loose_km.unwrap_or(f64::NAN);
        let falsify_tight = c.falsify_tight.unwrap_or(false);
        let falsify_loose = c.falsify_loose.unwrap_or(false);
        println!(
            "- {} dist={:.1}km max_tight={:.1} max_loose={:.1} falsify_tight={} falsify_loose={}",
            c.id, c.dist_km, max_tight, max_loose, falsify_tight, falsify_loose
        );
    }
}

fn max_distance_km(rtt_ms: f64, speed_km_s: f64) -> Option<f64> {
    if !rtt_ms.is_finite() || rtt_ms <= 0.0 {
        return None;
    }
    let speed_km_ms = speed_km_s / MS_PER_SEC;
    Some(speed_km_ms * (rtt_ms / RTT_FACTOR))
}

fn estimate_location(
    stats: &HashMap<String, EndpointStats>,
    endpoints: &HashMap<String, Endpoint>,
    speed_km_s: f64,
    grid: f64,
    refine: f64,
    band_factor: f64,
    band_window_deg: f64,
    calibration: Option<&Calibration>,
) -> Option<Estimate> {
    let mut obs = Vec::new();
    for (id, st) in stats {
        let ep = endpoints.get(id).or_else(|| {
            if let Some(base) = id.split('@').next() {
                endpoints.get(base)
            } else {
                None
            }
        });
        let Some(ep) = ep else { continue };
        let (Some(lat), Some(lon)) = (ep.lat, ep.lon) else { continue };
        let rtt = match st.p05.or(st.min) {
            Some(v) if v.is_finite() && v > 0.0 => v,
            _ => continue,
        };
        let rtt = adjust_rtt_ms(rtt, id, calibration);
        if !rtt.is_finite() || rtt <= 0.0 {
            continue;
        }
        let jitter = st.jitter_ms.unwrap_or(MIN_JITTER_MS);
        obs.push(EndpointObs {
            lat,
            lon,
            rtt_ms: rtt,
            jitter_ms: jitter.max(MIN_JITTER_MS),
        });
    }
    if obs.len() < 3 {
        return None;
    }

    let (best_lat, best_lon, _best_sse, _best_bias) = grid_search(&obs, speed_km_s, grid)?;
    let window = grid.max(refine * REFINE_WINDOW_MULT);
    let (ref_lat, ref_lon, ref_sse, ref_bias) = grid_search_bounds(
        &obs,
        speed_km_s,
        best_lat - window,
        best_lat + window,
        best_lon - window,
        best_lon + window,
        refine,
    )?;

    let band = fit_band(
        &obs,
        speed_km_s,
        ref_lat,
        ref_lon,
        ref_sse,
        refine,
        band_factor,
        band_window_deg.max(window),
    );

    Some(Estimate {
        lat: ref_lat,
        lon: ref_lon,
        bias_ms: ref_bias,
        sse: ref_sse,
        points: obs.len(),
        band,
    })
}

fn grid_search(
    obs: &[EndpointObs],
    speed_km_s: f64,
    step: f64,
) -> Option<(f64, f64, f64, f64)> {
    grid_search_bounds(
        obs,
        speed_km_s,
        -WORLD_LAT_MAX,
        WORLD_LAT_MAX,
        -WORLD_LON_MAX,
        WORLD_LON_MAX,
        step,
    )
}

fn grid_search_bounds(
    obs: &[EndpointObs],
    speed_km_s: f64,
    lat_min: f64,
    lat_max: f64,
    lon_min: f64,
    lon_max: f64,
    step: f64,
) -> Option<(f64, f64, f64, f64)> {
    if step <= 0.0 {
        return None;
    }
    let mut best: Option<(f64, f64, f64, f64)> = None;
    let mut lat = lat_min.max(-WORLD_LAT_MAX);
    while lat <= lat_max.min(WORLD_LAT_MAX) {
        let mut lon = lon_min;
        while lon <= lon_max {
            let (sse, bias) = sse_for_candidate(lat, lon, obs, speed_km_s);
            match best {
                None => best = Some((lat, lon, sse, bias)),
                Some((_, _, best_sse, _)) if sse < best_sse => {
                    best = Some((lat, lon, sse, bias))
                }
                _ => {}
            }
            lon += step;
        }
        lat += step;
    }
    best
}

fn sse_for_candidate(lat: f64, lon: f64, obs: &[EndpointObs], speed_km_s: f64) -> (f64, f64) {
    let speed_km_ms = speed_km_s / MS_PER_SEC;
    let mut sum_w = 0.0;
    let mut sum_wx = 0.0;
    for o in obs {
        let dist = haversine_km(lat, lon, o.lat, o.lon);
        let pred_no_bias = RTT_FACTOR * dist / speed_km_ms;
        let w = 1.0 / o.jitter_ms.max(MIN_JITTER_MS);
        sum_w += w;
        sum_wx += w * (o.rtt_ms - pred_no_bias);
    }
    let mut bias = if sum_w > 0.0 { sum_wx / sum_w } else { 0.0 };
    if bias < 0.0 {
        bias = 0.0;
    }
    let mut sse = 0.0;
    for o in obs {
        let dist = haversine_km(lat, lon, o.lat, o.lon);
        let pred = RTT_FACTOR * dist / speed_km_ms + bias;
        let w = 1.0 / o.jitter_ms.max(MIN_JITTER_MS);
        let err = o.rtt_ms - pred;
        sse += w * err * err;
    }
    (sse, bias)
}

fn fit_band(
    obs: &[EndpointObs],
    speed_km_s: f64,
    center_lat: f64,
    center_lon: f64,
    best_sse: f64,
    step: f64,
    factor: f64,
    window_deg: f64,
) -> Option<FitBand> {
    if step <= 0.0 {
        return None;
    }
    let threshold = (best_sse * (1.0 + factor)).max(best_sse + SSE_EPSILON);
    let mut min_lat = center_lat;
    let mut max_lat = center_lat;
    let mut min_lon = center_lon;
    let mut max_lon = center_lon;
    let mut max_dist = 0.0;
    let mut points = 0usize;

    let lat_min = (center_lat - window_deg).max(-WORLD_LAT_MAX);
    let lat_max = (center_lat + window_deg).min(WORLD_LAT_MAX);
    let lon_min = center_lon - window_deg;
    let lon_max = center_lon + window_deg;

    let mut lat = lat_min;
    while lat <= lat_max {
        let mut lon = lon_min;
        while lon <= lon_max {
            let (sse, _) = sse_for_candidate(lat, lon, obs, speed_km_s);
            if sse <= threshold {
                points += 1;
                let dist = haversine_km(center_lat, center_lon, lat, lon);
                if dist > max_dist {
                    max_dist = dist;
                }
                if lat < min_lat {
                    min_lat = lat;
                }
                if lat > max_lat {
                    max_lat = lat;
                }
                if lon < min_lon {
                    min_lon = lon;
                }
                if lon > max_lon {
                    max_lon = lon;
                }
            }
            lon += step;
        }
        lat += step;
    }

    if points == 0 {
        return None;
    }
    Some(FitBand {
        radius_km: max_dist,
        sse_threshold: threshold,
        points,
        min_lat,
        max_lat,
        min_lon,
        max_lon,
    })
}

fn print_estimate(est: &Estimate) {
    println!(
        "- lat={:.4}, lon={:.4}, bias={:.2}ms, sse={:.2}, endpoints_used={}",
        est.lat, est.lon, est.bias_ms, est.sse, est.points
    );
    if let Some(band) = &est.band {
        println!(
            "  fit_band: radius={:.1}km points={} sse_threshold={:.2}",
            band.radius_km, band.points, band.sse_threshold
        );
        println!(
            "  fit_band_bounds: lat[{:.2},{:.2}] lon[{:.2},{:.2}]",
            band.min_lat, band.max_lat, band.min_lon, band.max_lon
        );
    }
}

fn deltas(base: &HashMap<String, EndpointStats>, sess: &HashMap<String, EndpointStats>) -> Vec<Delta> {
    let mut ids: Vec<&String> = base.keys().collect();
    ids.sort();
    let mut out = Vec::new();
    for id in ids {
        let b = &base[id];
        let s = match sess.get(id) {
            Some(v) => v,
            None => continue,
        };
        let (Some(bv), Some(sv)) = (b.p05, s.p05) else { continue };
        out.push(Delta {
            id: id.clone(),
            delta_p05_ms: sv - bv,
            baseline_p05_ms: bv,
            session_p05_ms: sv,
        });
    }
    out
}

fn print_deltas(deltas: &[Delta]) {
    for d in deltas {
        println!(
            "- {} delta_p05={:.2}ms (baseline {:.2} -> session {:.2})",
            d.id, d.delta_p05_ms, d.baseline_p05_ms, d.session_p05_ms
        );
    }
}

fn haversine_km(lat1: f64, lon1: f64, lat2: f64, lon2: f64) -> f64 {
    let r = EARTH_RADIUS_KM;
    let dlat = (lat2 - lat1).to_radians();
    let dlon = (lon2 - lon1).to_radians();
    let lat1 = lat1.to_radians();
    let lat2 = lat2.to_radians();
    let a = (dlat / 2.0).sin().powi(2)
        + lat1.cos() * lat2.cos() * (dlon / 2.0).sin().powi(2);
    let c = 2.0 * a.sqrt().asin();
    r * c
}

#[cfg(test)]
mod tests {
    use super::*;

    const TEST_GRID_DEG: f64 = 5.0;
    const TEST_REFINE_DEG: f64 = 1.0;
    const TEST_BIAS_MS: f64 = 1000.0;
    const TEST_PATH_STRETCH: f64 = 1.0;
    const TEST_EPSILON: f64 = 1e-6;
    const TEST_SCALE: f64 = 2.0;
    const TEST_EXPECTED_ADJ_MS: f64 = 2.0;

    fn sample_config(endpoints: Vec<Endpoint>) -> Config {
        Config {
            secret_hex: "00".to_string(),
            endpoints,
            probe_paths: Vec::new(),
            samples_per_endpoint: 10,
            spacing_ms: 10,
            timeout_ms: DEFAULT_TIMEOUT_MS,
            interval_seconds: 10,
            pacing_spin_us: 0,
            output_path: "out.jsonl".to_string(),
            claimed_egress_region: None,
            physics_mismatch_threshold_ms: DEFAULT_PHYSICS_MISMATCH_THRESHOLD_MS,
        }
    }

    fn endpoint(id: &str, lat: f64, lon: f64) -> Endpoint {
        Endpoint {
            id: id.to_string(),
            host: "127.0.0.1".to_string(),
            port: DEFAULT_PORT,
            region_hint: None,
            lat: Some(lat),
            lon: Some(lon),
        }
    }

    fn stats_with_p05(id: &str, p05: f64) -> HashMap<String, EndpointStats> {
        let mut stats = HashMap::new();
        stats.insert(
            id.to_string(),
            EndpointStats {
                count: 10,
                min: Some(p05),
                p05: Some(p05),
                p50: Some(p05),
                p95: Some(p05),
                jitter_ms: Some(0.0),
            },
        );
        stats
    }

    #[test]
    fn calibration_entry_resolves_base_id() {
        let mut endpoints = HashMap::new();
        endpoints.insert(
            "nyc".to_string(),
            EndpointCalibration {
                bias_ms: 5.0,
                scale: 1.0,
            },
        );
        let cal = Calibration {
            generated_at: "0".to_string(),
            calibration_lat: 0.0,
            calibration_lon: 0.0,
            speed_km_s: DEFAULT_SPEED_KM_S,
            path_stretch: DEFAULT_PATH_STRETCH,
            endpoints,
        };
        let entry = calibration_entry(Some(&cal), "nyc@vpn");
        assert!(entry.is_some());
        assert_eq!(entry.unwrap().bias_ms, 5.0);
    }

    #[test]
    fn adjust_rtt_applies_bias_and_scale() {
        let mut endpoints = HashMap::new();
        endpoints.insert(
            "a".to_string(),
            EndpointCalibration {
                bias_ms: 5.0,
                scale: TEST_SCALE,
            },
        );
        let cal = Calibration {
            generated_at: "0".to_string(),
            calibration_lat: 0.0,
            calibration_lon: 0.0,
            speed_km_s: DEFAULT_SPEED_KM_S,
            path_stretch: DEFAULT_PATH_STRETCH,
            endpoints,
        };
        let adj = adjust_rtt_ms(9.0, "a", Some(&cal));
        assert!((adj - TEST_EXPECTED_ADJ_MS).abs() < TEST_EPSILON);
        let adj2 = adjust_rtt_ms(3.0, "a", Some(&cal));
        assert_eq!(adj2, 0.0);
    }

    #[test]
    fn build_calibration_uses_known_location() {
        let cfg = sample_config(vec![endpoint("a", 0.0, 0.0)]);
        let stats = stats_with_p05("a", 12.5);
        let cal = build_calibration(
            &cfg,
            &stats,
            0.0,
            0.0,
            DEFAULT_SPEED_KM_S,
            TEST_PATH_STRETCH,
        );
        let entry = cal.endpoints.get("a").unwrap();
        assert!((entry.bias_ms - 12.5).abs() < TEST_EPSILON);
        assert_eq!(entry.scale, 1.0);
    }

    #[test]
    fn claim_checks_resolve_base_endpoint() {
        let mut stats = HashMap::new();
        stats.insert(
            "a@vpn".to_string(),
            EndpointStats {
                count: 10,
                min: Some(10.0),
                p05: Some(10.0),
                p50: Some(10.0),
                p95: Some(10.0),
                jitter_ms: Some(0.0),
            },
        );
        let mut endpoints = HashMap::new();
        endpoints.insert("a".to_string(), endpoint("a", 0.0, 0.0));

        let mut cal_eps = HashMap::new();
        cal_eps.insert(
            "a".to_string(),
            EndpointCalibration {
                bias_ms: 5.0,
                scale: 1.0,
            },
        );
        let cal = Calibration {
            generated_at: "0".to_string(),
            calibration_lat: 0.0,
            calibration_lon: 0.0,
            speed_km_s: DEFAULT_SPEED_KM_S,
            path_stretch: DEFAULT_PATH_STRETCH,
            endpoints: cal_eps,
        };
        let checks = claim_checks(
            &stats,
            &endpoints,
            0.0,
            0.0,
            DEFAULT_SPEED_KM_S,
            Some(&cal),
        );
        assert_eq!(checks.len(), 1);
        assert_eq!(checks[0].id, "a@vpn");
        let expected = (DEFAULT_SPEED_KM_S / MS_PER_SEC) * (5.0 / RTT_FACTOR);
        assert!((checks[0].max_tight_km.unwrap() - expected).abs() < TEST_EPSILON);
    }

    #[test]
    fn estimate_location_respects_calibration() {
        let mut stats = HashMap::new();
        stats.insert(
            "a".to_string(),
            EndpointStats {
                count: 10,
                min: Some(10.0),
                p05: Some(10.0),
                p50: Some(10.0),
                p95: Some(10.0),
                jitter_ms: Some(MIN_JITTER_MS),
            },
        );
        stats.insert(
            "b".to_string(),
            EndpointStats {
                count: 10,
                min: Some(10.0),
                p05: Some(10.0),
                p50: Some(10.0),
                p95: Some(10.0),
                jitter_ms: Some(MIN_JITTER_MS),
            },
        );
        stats.insert(
            "c".to_string(),
            EndpointStats {
                count: 10,
                min: Some(10.0),
                p05: Some(10.0),
                p50: Some(10.0),
                p95: Some(10.0),
                jitter_ms: Some(MIN_JITTER_MS),
            },
        );
        let mut endpoints = HashMap::new();
        endpoints.insert("a".to_string(), endpoint("a", 0.0, 0.0));
        endpoints.insert("b".to_string(), endpoint("b", 0.0, 1.0));
        endpoints.insert("c".to_string(), endpoint("c", 1.0, 0.0));

        let est = estimate_location(
            &stats,
            &endpoints,
            DEFAULT_SPEED_KM_S,
            TEST_GRID_DEG,
            TEST_REFINE_DEG,
            DEFAULT_BAND_FACTOR,
            DEFAULT_BAND_WINDOW_DEG,
            None,
        );
        assert!(est.is_some());

        let mut cal_eps = HashMap::new();
        for id in ["a", "b", "c"] {
            cal_eps.insert(
                id.to_string(),
                EndpointCalibration {
                    bias_ms: TEST_BIAS_MS,
                    scale: 1.0,
                },
            );
        }
        let cal = Calibration {
            generated_at: "0".to_string(),
            calibration_lat: 0.0,
            calibration_lon: 0.0,
            speed_km_s: DEFAULT_SPEED_KM_S,
            path_stretch: DEFAULT_PATH_STRETCH,
            endpoints: cal_eps,
        };
        let est2 = estimate_location(
            &stats,
            &endpoints,
            DEFAULT_SPEED_KM_S,
            TEST_GRID_DEG,
            TEST_REFINE_DEG,
            DEFAULT_BAND_FACTOR,
            DEFAULT_BAND_WINDOW_DEG,
            Some(&cal),
        );
        assert!(est2.is_none());
    }
}
