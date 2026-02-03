import Foundation

extension String {
    var expandingTildeInPath: String {
        (self as NSString).expandingTildeInPath
    }
}

func hexToData(_ hex: String) -> Data? {
    let s = hex.trimmingCharacters(in: .whitespacesAndNewlines)
    guard s.count % 2 == 0 else { return nil }
    var data = Data()
    data.reserveCapacity(s.count / 2)
    var idx = s.startIndex
    for _ in 0..<(s.count / 2) {
        let next = s.index(idx, offsetBy: 2)
        let byteStr = s[idx..<next]
        guard let b = UInt8(byteStr, radix: 16) else { return nil }
        data.append(b)
        idx = next
    }
    return data
}

func nowUnixMs() -> Int64 {
    Int64(Date().timeIntervalSince1970 * 1000.0)
}

guard CommandLine.arguments.count >= 2 else {
    print("Usage: lattice <config.json>")
    exit(1)
}

let cfg = try Config.load(from: CommandLine.arguments[1])
guard let secret = hexToData(cfg.secretHex) else {
    print("Invalid secretHex (must be even-length hex).")
    exit(1)
}
guard !cfg.endpoints.isEmpty else {
    print("Config error: endpoints must not be empty.")
    exit(1)
}
guard cfg.samplesPerEndpoint > 0, cfg.spacingMs >= 0, cfg.timeoutMs > 0, cfg.intervalSeconds > 0 else {
    print("Config error: samplesPerEndpoint, timeoutMs, and intervalSeconds must be > 0; spacingMs must be >= 0.")
    exit(1)
}

let pathState = PathState()
pathState.start()

let outputPath = cfg.outputPath.expandingTildeInPath
let logger = try JSONLLogger(path: outputPath)

struct Target {
    let endpoint: Endpoint
    let probe: UDPEchoProbe
}

let targets: [Target] = cfg.endpoints.map { ep in
    Target(endpoint: ep, probe: UDPEchoProbe(host: ep.host, port: ep.port, secret: secret))
}

print("LATTICE running")
print("  endpoints: \(cfg.endpoints.count)")
print("  interval:  \(cfg.intervalSeconds)s")
print("  output:    \(outputPath)")
if let claimed = cfg.claimedEgressRegion {
    print("  claimed:   \(claimed)")
}

let clock = ContinuousClock()
let interval = Duration.seconds(Int64(cfg.intervalSeconds))
var nextTick = clock.now + interval

while true {
    let utun = UTun.present()
    let iface = pathState.ifaceSnapshot()

    await withTaskGroup(of: BurstRecord.self) { group in
        for target in targets {
            group.addTask {
                let ep = target.endpoint
                let samples = await target.probe.probeBurst(
                    count: cfg.samplesPerEndpoint,
                    spacingMs: cfg.spacingMs,
                    timeoutMs: cfg.timeoutMs
                )

                let (mn, p05, med) = Stats.summarize(samples: samples)
                let notes = Detector.notes(regionHint: ep.regionHint,
                                           claimed: cfg.claimedEgressRegion,
                                           minRttMs: mn,
                                           thresholdMs: cfg.physicsMismatchThresholdMs)

                return BurstRecord(
                    tsUnixMs: nowUnixMs(),
                    endpointId: ep.id,
                    host: ep.host,
                    port: ep.port,
                    regionHint: ep.regionHint,
                    samplesMs: samples,
                    minMs: mn,
                    p05Ms: p05,
                    medianMs: med,
                    iface: iface,
                    utunPresent: utun,
                    claimedEgressRegion: cfg.claimedEgressRegion,
                    notes: notes
                )
            }
        }

        for await rec in group {
            do {
                try logger.write(rec)
            } catch {
                print("[!!] log write failed: \(error)")
            }

            if !rec.notes.isEmpty {
                print("[!] \(rec.endpointId) \(rec.notes.joined(separator: " | "))")
            } else {
                if rec.minMs.isFinite {
                    print("[ok] \(rec.endpointId) min=\(String(format: "%.1f", rec.minMs))ms p05=\(String(format: "%.1f", rec.p05Ms))ms med=\(String(format: "%.1f", rec.medianMs))ms")
                } else {
                    print("[??] \(rec.endpointId) no samples (timeout?)")
                }
            }
        }
    }

    let now = clock.now
    if now < nextTick {
        try? await clock.sleep(until: nextTick, tolerance: .milliseconds(50))
        nextTick = nextTick + interval
    } else {
        nextTick = now + interval
    }
}
