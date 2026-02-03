import Foundation
import Network

final class PathState {
    private let monitor = NWPathMonitor()
    private let queue = DispatchQueue(label: "lattice.path.monitor")
    private var iface: String = "unknown"

    func start() {
        monitor.pathUpdateHandler = { [weak self] path in
            guard let self else { return }
            if path.usesInterfaceType(.wifi) { self.iface = "wifi" }
            else if path.usesInterfaceType(.wiredEthernet) { self.iface = "ethernet" }
            else if path.usesInterfaceType(.cellular) { self.iface = "cellular" }
            else if path.usesInterfaceType(.loopback) { self.iface = "loopback" }
            else { self.iface = "other" }
        }
        monitor.start(queue: queue)
    }

    func stop() { monitor.cancel() }

    func ifaceSnapshot() -> String {
        queue.sync { iface }
    }
}
