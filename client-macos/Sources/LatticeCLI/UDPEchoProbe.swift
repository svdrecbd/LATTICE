import Foundation
import Network
import CryptoKit

final class UDPEchoProbe {
    private let host: String
    private let port: UInt16
    private let key: SymmetricKey
    private let queue: DispatchQueue
    private var conn: NWConnection?
    private var started = false

    init(host: String, port: UInt16, secret: Data) {
        self.host = host
        self.port = port
        self.key = SymmetricKey(data: secret)
        self.queue = DispatchQueue(label: "lattice.udp.\(host):\(port)")
    }

    func probeBurst(count: Int, spacingMs: Int, timeoutMs: Int) async -> [Double] {
        guard let conn = await ensureConnection(timeoutMs: timeoutMs) else { return [] }

        var rtts: [Double] = []
        rtts.reserveCapacity(count)

        let clock = ContinuousClock()
        var nextSend = clock.now

        for i in 0..<count {
            if i > 0 {
                nextSend = nextSend + Duration.milliseconds(Int64(spacingMs))
                try? await clock.sleep(until: nextSend, tolerance: .milliseconds(1))
            }

            let nonce = UInt64.random(in: UInt64.min...UInt64.max)
            if let rtt = await sendAndReceiveOnce(conn: conn, seq: UInt32(i), nonce: nonce, timeoutMs: timeoutMs) {
                rtts.append(rtt)
            }
        }

        return rtts
    }

    private func ensureConnection(timeoutMs: Int) async -> NWConnection? {
        if conn == nil {
            conn = makeConnection()
            started = false
        }
        guard let conn else { return nil }
        if !started {
            let ready = await waitReady(conn: conn, queue: queue, timeoutMs: timeoutMs)
            if !ready {
                conn.cancel()
                self.conn = nil
                return nil
            }
            started = true
        }
        return conn
    }

    private func makeConnection() -> NWConnection? {
        let endpointHost = NWEndpoint.Host(host)
        guard let endpointPort = NWEndpoint.Port(rawValue: port) else { return nil }
        return NWConnection(host: endpointHost, port: endpointPort, using: .udp)
    }

    private func hmacTag32(msg28: Data) -> UInt32 {
        let mac = HMAC<SHA256>.authenticationCode(for: msg28, using: key)
        return mac.withUnsafeBytes { raw in
            (UInt32(raw[0]) << 24) | (UInt32(raw[1]) << 16) | (UInt32(raw[2]) << 8) | UInt32(raw[3])
        }
    }

    private func buildMessage(seq: UInt32, sendNs: UInt64, nonce: UInt64) -> Data {
        var msg = Data()
        msg.reserveCapacity(32)
        msg.append(contentsOf: [0x4C, 0x41, 0x54, 0x4F]) // "LATO"
        msg.appendBE(UInt32(1))        // version
        msg.appendBE(sendNs)           // send time (monotonic)
        msg.appendBE(seq)              // sequence
        msg.appendBE(nonce)            // nonce

        let tag = hmacTag32(msg28: msg) // msg is 28 bytes here
        msg.appendBE(tag)               // 32 bytes total
        return msg
    }

    private func waitReady(conn: NWConnection, queue: DispatchQueue, timeoutMs: Int) async -> Bool {
        await withCheckedContinuation { cont in
            var finished = false

            conn.stateUpdateHandler = { state in
                if finished { return }
                switch state {
                case .ready:
                    finished = true
                    cont.resume(returning: true)
                case .failed(_), .cancelled:
                    finished = true
                    cont.resume(returning: false)
                default:
                    break
                }
            }

            conn.start(queue: queue)

            queue.asyncAfter(deadline: .now() + .milliseconds(timeoutMs)) {
                if finished { return }
                finished = true
                cont.resume(returning: false)
            }
        }
    }

    private func sendAndReceiveOnce(conn: NWConnection, seq: UInt32, nonce: UInt64, timeoutMs: Int) async -> Double? {
        let sendTimeNs = DispatchTime.now().uptimeNanoseconds
        let msg = buildMessage(seq: seq, sendNs: sendTimeNs, nonce: nonce)

        return await withCheckedContinuation { cont in
            var finished = false

            // timeout
            queue.asyncAfter(deadline: .now() + .milliseconds(timeoutMs)) {
                if finished { return }
                finished = true
                cont.resume(returning: nil)
            }

            conn.send(content: msg, completion: .contentProcessed { [weak self] err in
                if finished { return }
                if err != nil {
                    finished = true
                    conn.cancel()
                    self?.conn = nil
                    self?.started = false
                    cont.resume(returning: nil)
                    return
                }

                conn.receiveMessage { [weak self] data, _, _, error in
                    if finished { return }
                    let recvTimeNs = DispatchTime.now().uptimeNanoseconds
                    finished = true
                    if error != nil {
                        conn.cancel()
                        self?.conn = nil
                        self?.started = false
                        cont.resume(returning: nil)
                        return
                    }
                    guard let data, data.count == msg.count, data == msg else {
                        cont.resume(returning: nil)
                        return
                    }
                    let dtMs = Double(recvTimeNs - sendTimeNs) / 1_000_000.0
                    cont.resume(returning: dtMs)
                }
            })
        }
    }
}

private extension Data {
    mutating func appendBE<T: FixedWidthInteger>(_ v: T) {
        var value = v.bigEndian
        withUnsafeBytes(of: &value) { raw in
            append(raw.bindMemory(to: UInt8.self))
        }
    }
}
