import Foundation

struct BurstRecord: Codable {
    let tsUnixMs: Int64
    let endpointId: String
    let host: String
    let port: UInt16
    let regionHint: String?
    let samplesMs: [Double]
    let minMs: Double
    let p05Ms: Double
    let medianMs: Double
    let iface: String
    let utunPresent: Bool
    let claimedEgressRegion: String?
    let notes: [String]
}

enum Stats {
    static func summarize(samples: [Double]) -> (min: Double, p05: Double, median: Double) {
        guard !samples.isEmpty else { return (.nan, .nan, .nan) }
        let s = samples.sorted()
        let mn = s[0]
        let p05 = s[max(0, Int(Double(s.count - 1) * 0.05))]
        let med = s[s.count / 2]
        return (mn, p05, med)
    }
}

enum Detector {
    static func notes(regionHint: String?, claimed: String?, minRttMs: Double, thresholdMs: Double) -> [String] {
        guard let claimed, let regionHint else { return [] }
        let a = claimed.lowercased()
        let b = regionHint.lowercased()

        // crude match: if you're claiming Stockholm and this endpoint hints stockholm, etc.
        guard a.contains(b) || b.contains(a) else { return [] }

        if minRttMs.isFinite, minRttMs > thresholdMs {
            return ["physics_mismatch: claimed=\(claimed) endpoint=\(regionHint) min_rtt_ms=\(String(format: \"%.1f\", minRttMs)) threshold_ms=\(String(format: \"%.1f\", thresholdMs))"]
        }
        return []
    }
}
