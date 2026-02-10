import Foundation
import ScreenCaptureKit
import CoreMedia
import AVFoundation
import Dispatch
import ObjectiveC

// Framed stream format to stdout (binary):
// Header (12 bytes, little-endian):
//   u32 nFrames
//   u16 nChannels
//   u16 formatCode (1 = float32 interleaved)
//   u32 nBytesPayload
// Payload:
//   float32 interleaved PCM, length = nFrames * nChannels
//
// Notes:
// - We force "audio-only" at runtime via ObjC selector if the SDK doesn't expose capturesVideo.
// - This helps macOS categorize permission as "Only System Audio" instead of "Screen & System Audio".

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
        UInt8(truncatingIfNeeded: x >> 24)
    ]
}

@inline(__always)
func le_u16(_ v: UInt16) -> [UInt8] {
    let x = v.littleEndian
    return [
        UInt8(truncatingIfNeeded: x),
        UInt8(truncatingIfNeeded: x >> 8)
    ]
}

final class AudioOutput: NSObject, SCStreamOutput {
    private let outFD: Int32 = STDOUT_FILENO
    private let errFD: Int32 = STDERR_FILENO

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == SCStreamOutputType.audio else { return }
        guard CMSampleBufferIsValid(sampleBuffer) else { return }

        guard let formatDesc = CMSampleBufferGetFormatDescription(sampleBuffer),
              let asbdPtr = CMAudioFormatDescriptionGetStreamBasicDescription(formatDesc) else { return }
        let asbd = asbdPtr.pointee

        let channels = Int(asbd.mChannelsPerFrame)
        let frames = CMSampleBufferGetNumSamples(sampleBuffer)
        if channels <= 0 || frames <= 0 { return }

        let isFloat = (asbd.mFormatFlags & kAudioFormatFlagIsFloat) != 0
        let bits = Int(asbd.mBitsPerChannel)
        let isNonInterleaved = (asbd.mFormatFlags & kAudioFormatFlagIsNonInterleaved) != 0

        // Prototype: output float32 PCM (common for ScreenCaptureKit).
        guard isFloat, bits == 32 else {
            let msg = "Unsupported audio format: isFloat=\(isFloat) bits=\(bits) nonInterleaved=\(isNonInterleaved)\n"
            msg.withCString { cstr in writeAll(errFD, cstr, msg.utf8.count) }
            return
        }

        do {
            try sampleBuffer.withAudioBufferList(blockBufferMemoryAllocator: kCFAllocatorDefault, flags: []) { abl, _ in
                let nBuf = abl.count
                if nBuf <= 0 { return }

                let totalSamples = frames * channels
                var interleaved = [Float](repeating: 0, count: totalSamples)

                if isNonInterleaved {
                    // Planar: one buffer per channel
                    if nBuf < channels {
                        let msg = "Unexpected planar buffers: \(nBuf) < channels \(channels)\n"
                        msg.withCString { cstr in writeAll(errFD, cstr, msg.utf8.count) }
                        return
                    }
                    for ch in 0..<channels {
                        let ab = abl[ch]
                        guard let data = ab.mData else { continue }
                        let fptr = data.assumingMemoryBound(to: Float.self)
                        for i in 0..<frames {
                            interleaved[i * channels + ch] = fptr[i]
                        }
                    }
                } else {
                    // Interleaved: usually one buffer containing frames*channels float32
                    let ab = abl[0]
                    guard let data = ab.mData else { return }
                    let fptr = data.assumingMemoryBound(to: Float.self)
                    interleaved.withUnsafeMutableBufferPointer { dst in
                        dst.baseAddress!.update(from: fptr, count: totalSamples)
                    }
                }

                // Write framed packet: header + payload
                let payloadBytes = totalSamples * MemoryLayout<Float>.size
                var header = [UInt8]()
                header += le_u32(UInt32(frames))
                header += le_u16(UInt16(channels))
                header += le_u16(UInt16(1)) // float32 interleaved
                header += le_u32(UInt32(payloadBytes))

                header.withUnsafeBytes { hb in writeAll(outFD, hb.baseAddress!, hb.count) }
                interleaved.withUnsafeBytes { pb in writeAll(outFD, pb.baseAddress!, pb.count) }
                fflush(stdout)
            }
        } catch {
            let msg = "withAudioBufferList failed: \(error)\n"
            msg.withCString { cstr in writeAll(errFD, cstr, msg.utf8.count) }
        }
    }
}

@main
struct Main {
    static func main() async {
        do {
            let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
            guard let display = content.displays.first else {
                fputs("No display found\n", stderr)
                exit(2)
            }

            // Your SDK expects these labels:
            let filter = SCContentFilter(display: display, excludingApplications: [], exceptingWindows: [])

            let config = SCStreamConfiguration()
            config.capturesAudio = true
            config.sampleRate = 48_000
            config.channelCount = 2
            config.excludesCurrentProcessAudio = true

            // Force audio-only if the runtime supports it, even if the SDK doesn't expose it.
            // Avoid KVC here to prevent crashes if key doesn't exist.
            let sel = NSSelectorFromString("setCapturesVideo:")
            if (config as AnyObject).responds(to: sel) {
                _ = (config as AnyObject).perform(sel, with: NSNumber(value: false))
            }

            let output = AudioOutput()
            let stream = SCStream(filter: filter, configuration: config, delegate: nil)

            try stream.addStreamOutput(
                output,
                type: SCStreamOutputType.audio,
                sampleHandlerQueue: DispatchQueue.global(qos: .userInitiated)
            )

            try await stream.startCapture()
            fputs("System audio capture started (audio-only attempt)\n", stderr)

            while true {
                try await Task.sleep(nanoseconds: 1_000_000_000)
            }
        } catch {
            fputs("Failed: \(error)\n", stderr)
            exit(1)
        }
    }
}
