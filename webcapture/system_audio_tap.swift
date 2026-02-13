import Foundation
import CoreAudio
import AudioToolbox
import Dispatch

// Framed stream format to stdout (binary):
// Header (12 bytes, little-endian):
//   u32 nFrames
//   u16 nChannels
//   u16 formatCode (1 = float32 interleaved)
//   u32 nBytesPayload
// Payload:
//   float32 interleaved PCM, length = nFrames * nChannels

@inline(__always)
func writeAll(_ fd: Int32, _ data: UnsafeRawPointer, _ count: Int) {
    var written = 0
    while written < count {
        let n = write(fd, data.advanced(by: written), count - written)
        if n <= 0 { return }
        written += n
    }
}

@inline(__always)
func le_u32(_ v: UInt32) -> [UInt8] {
    let x = v.littleEndian
    return [
        UInt8(truncatingIfNeeded: x),
        UInt8(truncatingIfNeeded: x >> 8),
        UInt8(truncatingIfNeeded: x >> 16),
        UInt8(truncatingIfNeeded: x >> 24),
    ]
}

@inline(__always)
func le_u16(_ v: UInt16) -> [UInt8] {
    let x = v.littleEndian
    return [
        UInt8(truncatingIfNeeded: x),
        UInt8(truncatingIfNeeded: x >> 8),
    ]
}

@inline(__always)
func statusString(_ status: OSStatus) -> String {
    let n = UInt32(bitPattern: status)
    return String(format: "0x%08x (%d)", n, Int(status))
}

final class TapContext {
    let outFD: Int32 = STDOUT_FILENO
    var channels: Int = 2
}

private let errFD: Int32 = STDERR_FILENO
private var gTapID: AudioObjectID = 0
private var gAggregateDeviceID: AudioObjectID = 0
private var gIOProcID: AudioDeviceIOProcID?
private var gContext: Unmanaged<TapContext>?

private func emitError(_ msg: String) {
    let line = "\(msg)\n"
    line.withCString { cstr in
        writeAll(errFD, cstr, line.utf8.count)
    }
}

private let ioProc: AudioDeviceIOProc = { _, _, inputData, _, _, _, inClientData in
    guard let inClientData else { return noErr }
    let ctx = Unmanaged<TapContext>.fromOpaque(inClientData).takeUnretainedValue()

    let abl = inputData.pointee
    let nBuf = Int(abl.mNumberBuffers)
    if nBuf <= 0 { return noErr }

    // The tap should deliver float32 samples. Handle both interleaved and planar.
    let buffers = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer<AudioBufferList>(mutating: inputData))
    let channels = max(1, ctx.channels)

    var frames = 0
    var interleaved: [Float] = []

    if nBuf == 1 {
        // Interleaved buffer.
        let b = buffers[0]
        guard let mData = b.mData else { return noErr }
        let totalSamples = Int(b.mDataByteSize) / MemoryLayout<Float>.size
        if totalSamples <= 0 { return noErr }
        let inferredChannels = max(1, Int(b.mNumberChannels))
        frames = totalSamples / inferredChannels
        if frames <= 0 { return noErr }
        interleaved = Array(repeating: 0, count: totalSamples)
        interleaved.withUnsafeMutableBufferPointer { dst in
            dst.baseAddress!.update(from: mData.assumingMemoryBound(to: Float.self), count: totalSamples)
        }
        ctx.channels = inferredChannels
    } else {
        // Planar buffers.
        let planarChannels = nBuf
        var inferredFrames = 0
        for i in 0..<nBuf {
            let b = buffers[i]
            let f = Int(b.mDataByteSize) / MemoryLayout<Float>.size
            if i == 0 {
                inferredFrames = f
            } else if f != inferredFrames {
                return noErr
            }
        }
        if inferredFrames <= 0 { return noErr }
        frames = inferredFrames
        interleaved = Array(repeating: 0, count: frames * planarChannels)
        for ch in 0..<planarChannels {
            let b = buffers[ch]
            guard let mData = b.mData else { continue }
            let src = mData.assumingMemoryBound(to: Float.self)
            for i in 0..<frames {
                interleaved[i * planarChannels + ch] = src[i]
            }
        }
        ctx.channels = planarChannels
    }

    let ch = max(1, ctx.channels)
    let payloadBytes = interleaved.count * MemoryLayout<Float>.size
    var header = [UInt8]()
    header += le_u32(UInt32(frames))
    header += le_u16(UInt16(ch))
    header += le_u16(UInt16(1))
    header += le_u32(UInt32(payloadBytes))
    header.withUnsafeBytes { hb in
        writeAll(ctx.outFD, hb.baseAddress!, hb.count)
    }
    interleaved.withUnsafeBytes { pb in
        writeAll(ctx.outFD, pb.baseAddress!, pb.count)
    }
    fflush(stdout)
    return noErr
}

@discardableResult
private func stopCapture() -> Bool {
    if gAggregateDeviceID != 0, let io = gIOProcID {
        _ = AudioDeviceStop(gAggregateDeviceID, io)
        _ = AudioDeviceDestroyIOProcID(gAggregateDeviceID, io)
    }
    gIOProcID = nil

    if gAggregateDeviceID != 0 {
        _ = AudioHardwareDestroyAggregateDevice(gAggregateDeviceID)
    }
    gAggregateDeviceID = 0

    if gTapID != 0 {
        _ = AudioHardwareDestroyProcessTap(gTapID)
    }
    gTapID = 0

    gContext?.release()
    gContext = nil
    return true
}

private func run() -> Int32 {
    guard #available(macOS 14.4, *) else {
        emitError("Failed: Core Audio taps require macOS 14.4+.")
        return 2
    }

    // Important: this API expects CoreAudio process object IDs, not raw PIDs.
    // Use an empty exclusion list for a global tap to avoid invalid object IDs.
    let tapDescription = CATapDescription(stereoGlobalTapButExcludeProcesses: [])

    tapDescription.name = "Transcriba System Audio Tap"
    tapDescription.isPrivate = true
    tapDescription.muteBehavior = .unmuted

    var tapID = AudioObjectID(0)
    var status = AudioHardwareCreateProcessTap(tapDescription, &tapID)
    guard status == noErr else {
        emitError("Failed: AudioHardwareCreateProcessTap: \(statusString(status)) (permission denied or not permitted)")
        return 1
    }
    gTapID = tapID

    let uid = "com.local.transcriba.recorder.tap.aggregate.\(UUID().uuidString)"
    let aggregateDescription: [String: Any] = [
        kAudioAggregateDeviceNameKey as String: "Transcriba System Audio",
        kAudioAggregateDeviceUIDKey as String: uid,
        kAudioAggregateDeviceTapListKey as String: [
            [kAudioSubTapUIDKey as String: tapDescription.uuid.uuidString],
        ],
        kAudioAggregateDeviceTapAutoStartKey as String: true,
        kAudioAggregateDeviceIsPrivateKey as String: true,
    ]

    var aggregateID = AudioObjectID(0)
    status = AudioHardwareCreateAggregateDevice(aggregateDescription as CFDictionary, &aggregateID)
    guard status == noErr else {
        emitError("Failed: AudioHardwareCreateAggregateDevice: \(statusString(status)) (permission denied or not permitted)")
        _ = stopCapture()
        return 1
    }
    gAggregateDeviceID = aggregateID

    let ctx = TapContext()
    let unmanaged = Unmanaged.passRetained(ctx)
    gContext = unmanaged

    status = AudioDeviceCreateIOProcID(aggregateID, ioProc, unmanaged.toOpaque(), &gIOProcID)
    guard status == noErr else {
        emitError("Failed: AudioDeviceCreateIOProcID: \(statusString(status)) (permission denied or not permitted)")
        _ = stopCapture()
        return 1
    }

    guard let ioID = gIOProcID else {
        emitError("Failed: IOProc not created.")
        _ = stopCapture()
        return 1
    }

    status = AudioDeviceStart(aggregateID, ioID)
    guard status == noErr else {
        emitError("Failed: AudioDeviceStart: \(statusString(status)) (permission denied or not permitted)")
        _ = stopCapture()
        return 1
    }

    emitError("Core Audio taps capture started.")
    signal(SIGTERM) { _ in _ = stopCapture(); exit(0) }
    signal(SIGINT) { _ in _ = stopCapture(); exit(0) }

    while true {
        Thread.sleep(forTimeInterval: 1.0)
    }
}

@main
struct Main {
    static func main() {
        let code = run()
        exit(code)
    }
}
