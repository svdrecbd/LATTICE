import Foundation

final class JSONLLogger {
    private let fileHandle: FileHandle
    private let encoder = JSONEncoder()

    init(path: String) throws {
        let url = URL(fileURLWithPath: path)
        let dir = url.deletingLastPathComponent()
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)

        if !FileManager.default.fileExists(atPath: url.path) {
            FileManager.default.createFile(atPath: url.path, contents: nil)
        }
        self.fileHandle = try FileHandle(forWritingTo: url)
        try fileHandle.seekToEnd()
        encoder.outputFormatting = [.withoutEscapingSlashes]
    }

    func write<T: Encodable>(_ obj: T) throws {
        let data = try encoder.encode(obj)
        fileHandle.write(data)
        fileHandle.write("\n".data(using: .utf8)!)
    }
}
