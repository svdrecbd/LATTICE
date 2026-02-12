use lattice_core::{
    build_packet, hex_to_bytes, now_unix_ms, physics_notes, summarize, BurstRecord, Config, ProbePath,
    UtunInterface,
};
use rand::Rng;
use std::env;
use std::fs::{self, File};
use std::io::{self, BufWriter, Write};
use std::path::PathBuf;
use std::sync::{mpsc, Arc};
use std::thread;
use std::time::{Duration, Instant};
use std::net::{IpAddr, SocketAddr, ToSocketAddrs};

#[cfg(target_os = "macos")]
use lattice_os_macos as os;
#[cfg(target_os = "linux")]
use lattice_os_linux as os;

const RECONNECT_EMPTY_BURSTS: usize = 2;
const RECONNECT_INTERVAL_BURSTS: usize = 6;

fn main() -> io::Result<()> {
    let args: Vec<String> = env::args().collect();
    if args.len() < 2 {
        eprintln!("Usage: lattice-client <config.json>");
        std::process::exit(1);
    }

    let cfg = Config::load(&args[1])?;
    validate_config(&cfg)?;

    let secret = hex_to_bytes(&cfg.secret_hex).map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?;
    if secret.len() < 16 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "secretHex must be at least 16 bytes",
        ));
    }

    let output_path = expand_tilde(&cfg.output_path);
    println!("LATTICE (Rust) running");
    println!("  endpoints: {}", cfg.endpoints.len());
    println!("  interval:  {}s", cfg.interval_seconds);
    println!("  output:    {}", output_path.display());
    if let Some(claimed) = &cfg.claimed_egress_region {
        println!("  claimed:   {}", claimed);
    }

    let (tx, rx) = mpsc::channel::<BurstRecord>();
    let writer_path = output_path.clone();
    let writer_handle = thread::spawn(move || writer_thread(writer_path, rx));

    let targets = expand_probe_targets(&cfg)?;
    let cfg = Arc::new(cfg);
    let secret = Arc::new(secret);

    for target in targets {
        let tx = tx.clone();
        let cfg = Arc::clone(&cfg);
        let secret = Arc::clone(&secret);
        thread::spawn(move || endpoint_worker(target, cfg, secret, tx));
    }

    drop(tx);

    // Keep the main thread alive; the writer thread runs until all workers exit.
    let _ = writer_handle.join();
    Ok(())
}

fn validate_config(cfg: &Config) -> io::Result<()> {
    if cfg.endpoints.is_empty() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "endpoints must not be empty",
        ));
    }
    if cfg.samples_per_endpoint == 0 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "samplesPerEndpoint must be > 0",
        ));
    }
    if cfg.timeout_ms == 0 || cfg.interval_seconds == 0 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "timeoutMs and intervalSeconds must be > 0",
        ));
    }
    for path in &cfg.probe_paths {
        if path.id.trim().is_empty() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "probePaths entries must include a non-empty id",
            ));
        }
    }
    Ok(())
}

#[derive(Clone)]
struct ProbeTarget {
    endpoint: lattice_core::Endpoint,
    path_id: String,
    bind_iface: Option<String>,
    bind_ip: Option<IpAddr>,
}

fn expand_probe_targets(cfg: &Config) -> io::Result<Vec<ProbeTarget>> {
    let mut out = Vec::new();
    let paths: Vec<ProbePath> = if cfg.probe_paths.is_empty() {
        vec![ProbePath {
            id: "default".to_string(),
            bind_interface: None,
            bind_ip: None,
        }]
    } else {
        cfg.probe_paths.clone()
    };

    for path in paths {
        for ep in &cfg.endpoints {
            let mut endpoint = ep.clone();
            if path.id != "default" {
                endpoint.id = format!("{}@{}", endpoint.id, path.id);
            }
            let bind_ip = resolve_bind_ip(&path, &endpoint.host, endpoint.port)?;
            out.push(ProbeTarget {
                endpoint,
                path_id: path.id.clone(),
                bind_iface: path.bind_interface.clone(),
                bind_ip,
            });
        }
    }
    Ok(out)
}

fn resolve_bind_ip(path: &ProbePath, host: &str, port: u16) -> io::Result<Option<IpAddr>> {
    if let Some(ip_str) = &path.bind_ip {
        let ip = ip_str
            .parse::<IpAddr>()
            .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "invalid bindIp"))?;
        return Ok(Some(ip));
    }
    if let Some(iface) = &path.bind_interface {
        let ips = os::iface_ips(iface)?;
        if ips.is_empty() {
            return Err(io::Error::new(
                io::ErrorKind::NotFound,
                format!("no addresses found for interface {}", iface),
            ));
        }
        let mut want_v4 = false;
        let mut want_v6 = false;
        if let Ok(addrs) = (host, port).to_socket_addrs() {
            for addr in addrs {
                match addr {
                    SocketAddr::V4(_) => want_v4 = true,
                    SocketAddr::V6(_) => want_v6 = true,
                }
            }
        }
        if want_v4 {
            if let Some(ip) = ips.iter().find(|ip| ip.is_ipv4()) {
                return Ok(Some(*ip));
            }
        }
        if want_v6 {
            if let Some(ip) = ips.iter().find(|ip| ip.is_ipv6()) {
                return Ok(Some(*ip));
            }
        }
        return Ok(Some(ips[0]));
    }
    Ok(None)
}

fn expand_tilde(path: &str) -> PathBuf {
    if let Some(stripped) = path.strip_prefix("~/") {
        if let Ok(home) = env::var("HOME") {
            return PathBuf::from(home).join(stripped);
        }
    }
    PathBuf::from(path)
}

fn writer_thread(path: PathBuf, rx: mpsc::Receiver<BurstRecord>) {
    if let Some(parent) = path.parent() {
        if let Err(err) = fs::create_dir_all(parent) {
            eprintln!("[!!] failed to create log dir: {}", err);
            return;
        }
    }
    let file = match File::options().create(true).append(true).open(&path) {
        Ok(f) => f,
        Err(err) => {
            eprintln!("[!!] failed to open log file: {}", err);
            return;
        }
    };
    let mut writer = BufWriter::new(file);

    for rec in rx {
        if let Err(err) = serde_json::to_writer(&mut writer, &rec) {
            eprintln!("[!!] log write failed: {}", err);
            continue;
        }
        if let Err(err) = writer.write_all(b"\n") {
            eprintln!("[!!] log write failed: {}", err);
            continue;
        }
        if let Err(err) = writer.flush() {
            eprintln!("[!!] log flush failed: {}", err);
        }

        if !rec.notes.is_empty() {
            println!("[!] {} {}", rec.endpoint_id, rec.notes.join(" | "));
        } else if let (Some(min), Some(p05), Some(med)) = (rec.min_ms, rec.p05_ms, rec.median_ms) {
            println!(
                "[ok] {} min={:.1}ms p05={:.1}ms med={:.1}ms",
                rec.endpoint_id, min, p05, med
            );
        } else {
            println!("[??] {} no samples (timeout?)", rec.endpoint_id);
        }
    }
}

fn endpoint_worker(
    target: ProbeTarget,
    cfg: Arc<Config>,
    secret: Arc<Vec<u8>>,
    tx: mpsc::Sender<BurstRecord>,
) {
    let mut prober_opt: Option<os::UdpProber> = None;
    let mut last_utun_active: Option<bool> = None;
    let mut burst_since_refresh: usize = 0;
    let mut empty_burst_streak: usize = 0;

    let interval = Duration::from_secs(cfg.interval_seconds);
    let spacing = Duration::from_millis(cfg.spacing_ms);
    let timeout = Duration::from_millis(cfg.timeout_ms);
    let mut rng = rand::thread_rng();
    let mut seq: u32 = 0;

    let mut next_tick = Instant::now() + interval;

    loop {
        let utun_report = os::utun_report();
        let mut refresh_socket = false;
        if let Some(prev) = last_utun_active {
            if prev != utun_report.active {
                refresh_socket = true;
            }
        }
        if burst_since_refresh >= RECONNECT_INTERVAL_BURSTS {
            refresh_socket = true;
        }
        if refresh_socket {
            prober_opt = None;
            burst_since_refresh = 0;
            empty_burst_streak = 0;
        }

        if prober_opt.is_none() {
            match os::UdpProber::new(&target.endpoint.host, target.endpoint.port, target.bind_ip) {
                Ok(p) => prober_opt = Some(p),
                Err(err) => {
                    eprintln!("[!!] {} probe init failed: {}", target.endpoint.id, err);
                    last_utun_active = Some(utun_report.active);
                    sleep_until(next_tick, cfg.pacing_spin_us);
                    next_tick += interval;
                    continue;
                }
            }
        }

        let prober = prober_opt.as_mut().unwrap();
        let iface_name = prober.iface_name().unwrap_or_else(|_| "unknown".to_string());
        let local_addr = prober
            .local_addr()
            .map(|a| a.to_string())
            .unwrap_or_else(|_| "unknown".to_string());
        let dest_is_loopback = is_loopback_host(&target.endpoint.host)
            || local_addr
                .parse::<IpAddr>()
                .map(|ip| ip.is_loopback())
                .unwrap_or(false);
        let iface = if dest_is_loopback {
            "loopback".to_string()
        } else {
            os::iface_type(&iface_name)
        };
        let iface_is_tunnel = is_tunnel_iface_name(&iface_name);

        let mut samples = Vec::with_capacity(cfg.samples_per_endpoint);
        let mut next_send = Instant::now();

        for i in 0..cfg.samples_per_endpoint {
            if i > 0 {
                next_send += spacing;
                sleep_until(next_send, cfg.pacing_spin_us);
            }

            let nonce: u64 = rng.gen();
            let send_realtime_ns = os::realtime_now_ns();
            let send_mono_ns = os::monotonic_now_ns();
            let msg = build_packet(seq, send_realtime_ns, nonce, secret.as_ref());
            seq = seq.wrapping_add(1);

            match prober.send_and_receive_rtt(&msg, send_realtime_ns, send_mono_ns, timeout) {
                Ok(Some(rtt)) => samples.push(rtt),
                Ok(None) => {}
                Err(err) => {
                    eprintln!("[!!] {} send/recv failed: {}", target.endpoint.id, err);
                }
            }
        }

        if samples.is_empty() {
            empty_burst_streak += 1;
        } else {
            empty_burst_streak = 0;
        }

        let (mn, p05, med) = summarize(&samples);
        let notes = physics_notes(
            &target.endpoint.region_hint,
            &cfg.claimed_egress_region,
            mn,
            cfg.physics_mismatch_threshold_ms,
        );

        let utun_interfaces: Vec<UtunInterface> = utun_report
            .interfaces
            .into_iter()
            .map(|i| UtunInterface {
                name: i.name,
                flags: i.flags,
                flags_decoded: decode_if_flags(i.flags),
                has_non_loopback_addr: i.has_non_loopback_addr,
            })
            .collect();

        let rec = BurstRecord {
            ts_unix_ms: now_unix_ms(),
            endpoint_id: target.endpoint.id.clone(),
            host: target.endpoint.host.clone(),
            port: target.endpoint.port,
            probe_path: target.path_id.clone(),
            probe_bind_iface: target
                .bind_iface
                .clone()
                .unwrap_or_else(String::new),
            probe_bind_ip: target
                .bind_ip
                .map(|ip| ip.to_string())
                .unwrap_or_else(String::new),
            local_addr,
            region_hint: target.endpoint.region_hint.clone(),
            samples_ms: samples,
            min_ms: mn,
            p05_ms: p05,
            median_ms: med,
            iface,
            iface_name: iface_name.clone(),
            iface_is_tunnel,
            utun_present: utun_report.present,
            utun_active: utun_report.active,
            utun_interfaces,
            dest_is_loopback,
            claimed_egress_region: cfg.claimed_egress_region.clone(),
            notes,
        };

        if tx.send(rec).is_err() {
            break;
        }

        if empty_burst_streak >= RECONNECT_EMPTY_BURSTS {
            prober_opt = None;
            burst_since_refresh = 0;
        } else {
            burst_since_refresh += 1;
        }
        last_utun_active = Some(utun_report.active);

        let now = Instant::now();
        if now < next_tick {
            sleep_until(next_tick, cfg.pacing_spin_us);
            next_tick += interval;
        } else {
            next_tick = now + interval;
        }
    }
}

fn sleep_until(target: Instant, spin_us: u64) {
    let spin = Duration::from_micros(spin_us);
    loop {
        let now = Instant::now();
        if now >= target {
            break;
        }
        let remaining = target - now;
        if spin_us == 0 || remaining > spin {
            thread::sleep(remaining - spin);
        } else {
            while Instant::now() < target {
                std::hint::spin_loop();
            }
            break;
        }
    }
}

fn is_loopback_host(host: &str) -> bool {
    if host.eq_ignore_ascii_case("localhost") {
        return true;
    }
    if let Ok(ip) = host.parse::<IpAddr>() {
        return ip.is_loopback();
    }
    false
}

fn is_tunnel_iface_name(name: &str) -> bool {
    let n = name.to_ascii_lowercase();
    n.starts_with("utun")
        || n.starts_with("tun")
        || n.starts_with("tap")
        || n.starts_with("wg")
        || n.starts_with("ppp")
        || n.starts_with("ipsec")
}

fn decode_if_flags(flags: u32) -> Vec<String> {
    let mut out = Vec::new();
    if (flags & (libc::IFF_UP as u32)) != 0 {
        out.push("UP".to_string());
    }
    if (flags & (libc::IFF_RUNNING as u32)) != 0 {
        out.push("RUNNING".to_string());
    }
    if (flags & (libc::IFF_LOOPBACK as u32)) != 0 {
        out.push("LOOPBACK".to_string());
    }
    if (flags & (libc::IFF_POINTOPOINT as u32)) != 0 {
        out.push("POINTOPOINT".to_string());
    }
    if (flags & (libc::IFF_MULTICAST as u32)) != 0 {
        out.push("MULTICAST".to_string());
    }
    out
}
