use std::fs;
use std::io;
use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

use hmac::{Hmac, Mac};
use serde::{Deserialize, Serialize};
use sha2::Sha256;

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Endpoint {
    pub id: String,
    pub host: String,
    pub port: u16,
    pub region_hint: Option<String>,
    #[serde(default)]
    pub lat: Option<f64>,
    #[serde(default)]
    pub lon: Option<f64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ProbePath {
    pub id: String,
    #[serde(default)]
    pub bind_interface: Option<String>,
    #[serde(default)]
    pub bind_ip: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Config {
    pub secret_hex: String,
    pub endpoints: Vec<Endpoint>,
    #[serde(default)]
    pub probe_paths: Vec<ProbePath>,
    pub samples_per_endpoint: usize,
    pub spacing_ms: u64,
    pub timeout_ms: u64,
    pub interval_seconds: u64,
    #[serde(default = "default_pacing_spin_us")]
    pub pacing_spin_us: u64,
    pub output_path: String,
    pub claimed_egress_region: Option<String>,
    pub physics_mismatch_threshold_ms: f64,
}

impl Config {
    pub fn load<P: AsRef<Path>>(path: P) -> io::Result<Self> {
        let data = fs::read(path)?;
        let cfg = serde_json::from_slice(&data)
            .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?;
        Ok(cfg)
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct BurstRecord {
    pub ts_unix_ms: i64,
    pub endpoint_id: String,
    pub host: String,
    pub port: u16,
    #[serde(default)]
    pub probe_path: String,
    #[serde(default)]
    pub probe_bind_iface: String,
    #[serde(default)]
    pub probe_bind_ip: String,
    #[serde(default)]
    pub local_addr: String,
    pub region_hint: Option<String>,
    pub samples_ms: Vec<f64>,
    pub min_ms: Option<f64>,
    pub p05_ms: Option<f64>,
    pub median_ms: Option<f64>,
    pub iface: String,
    #[serde(default)]
    pub iface_name: String,
    #[serde(default)]
    pub iface_is_tunnel: bool,
    #[serde(default)]
    pub utun_present: bool,
    #[serde(default)]
    pub utun_active: bool,
    #[serde(default)]
    pub utun_interfaces: Vec<UtunInterface>,
    #[serde(default)]
    pub dest_is_loopback: bool,
    pub claimed_egress_region: Option<String>,
    pub notes: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct UtunInterface {
    pub name: String,
    pub flags: u32,
    #[serde(default)]
    pub flags_decoded: Vec<String>,
    pub has_non_loopback_addr: bool,
}

pub fn now_unix_ms() -> i64 {
    let dur = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default();
    (dur.as_secs() as i64) * 1000 + (dur.subsec_millis() as i64)
}

pub fn hex_to_bytes(s: &str) -> Result<Vec<u8>, String> {
    let s = s.trim();
    if s.len() % 2 != 0 {
        return Err("secretHex must be even-length hex".into());
    }
    let mut out = Vec::with_capacity(s.len() / 2);
    let mut i = 0;
    while i < s.len() {
        let byte = u8::from_str_radix(&s[i..i + 2], 16)
            .map_err(|_| "secretHex contains invalid hex".to_string())?;
        out.push(byte);
        i += 2;
    }
    Ok(out)
}

pub fn build_packet(seq: u32, send_ns: u64, nonce: u64, secret: &[u8]) -> [u8; 32] {
    let mut buf = [0u8; 32];
    buf[0..4].copy_from_slice(b"LATO");
    buf[4..8].copy_from_slice(&1u32.to_be_bytes());
    buf[8..16].copy_from_slice(&send_ns.to_be_bytes());
    buf[16..20].copy_from_slice(&seq.to_be_bytes());
    buf[20..28].copy_from_slice(&nonce.to_be_bytes());

    let mut mac = Hmac::<Sha256>::new_from_slice(secret).expect("HMAC key");
    mac.update(&buf[..28]);
    let tag = mac.finalize().into_bytes();
    buf[28..32].copy_from_slice(&tag[..4]);

    buf
}

pub fn summarize(samples: &[f64]) -> (Option<f64>, Option<f64>, Option<f64>) {
    if samples.is_empty() {
        return (None, None, None);
    }
    let mut s = samples.to_vec();
    s.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let mn = s[0];
    let p05 = s[((s.len() - 1) as f64 * 0.05).floor() as usize];
    let med = s[s.len() / 2];
    (Some(mn), Some(p05), Some(med))
}

pub fn physics_notes(
    region_hint: &Option<String>,
    claimed: &Option<String>,
    min_rtt_ms: Option<f64>,
    threshold_ms: f64,
) -> Vec<String> {
    let (Some(region_hint), Some(claimed)) = (region_hint, claimed) else {
        return Vec::new();
    };
    let a = claimed.to_lowercase();
    let b = region_hint.to_lowercase();
    if !(a.contains(&b) || b.contains(&a)) {
        return Vec::new();
    }
    if let Some(min_rtt_ms) = min_rtt_ms {
        if min_rtt_ms > threshold_ms {
            return vec![format!(
                "physics_mismatch: claimed={} endpoint={} min_rtt_ms={:.1} threshold_ms={:.1}",
                claimed, region_hint, min_rtt_ms, threshold_ms
            )];
        }
    }
    Vec::new()
}

fn default_pacing_spin_us() -> u64 {
    200
}
