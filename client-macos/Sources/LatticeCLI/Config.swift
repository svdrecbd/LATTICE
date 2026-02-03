import Foundation

struct Endpoint: Codable {
    let id: String
    let host: String
    let port: UInt16
    let regionHint: String?
}

struct Config: Codable {
    let secretHex: String
    let endpoints: [Endpoint]
    let samplesPerEndpoint: Int
    let spacingMs: Int
    let timeoutMs: Int
    let intervalSeconds: Int
    let outputPath: String
    let claimedEgressRegion: String?
    let physicsMismatchThresholdMs: Double
}

extension Config {
    static func load(from path: String) throws -> Config {
        let url = URL(fileURLWithPath: path)
        let data = try Data(contentsOf: url)
        return try JSONDecoder().decode(Config.self, from: data)
    }
}
