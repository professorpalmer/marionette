// Voice dictation helper for macOS 26 and later (SpeechAnalyzer, on-device).
//
// Input: raw PCM, signed 16-bit little-endian, 16 kHz, mono, on stdin.
// Output: one JSON object per line on stdout:
//   {"type":"ready"}                      the model is ready for audio
//   {"type":"partial","text":"..."}       best text so far (final + volatile)
//   {"type":"final","text":"..."}         all final text so far
//   {"type":"done","text":"..."}          stdin closed and the analyzer finished
//   {"type":"error","code":"...","message":"..."}
// `--check` prints {"type":"check","available":bool,"reason":...} and exits.
// `--locale <id>` selects the language (default en-US).

import AVFoundation
import CoreMedia
import Foundation
import Speech

let sampleRate = 16_000.0
let outputLock = NSLock()

func emit(_ object: [String: Any]) {
  guard let data = try? JSONSerialization.data(withJSONObject: object) else { return }
  outputLock.lock()
  FileHandle.standardOutput.write(data)
  FileHandle.standardOutput.write(Data([0x0A]))
  outputLock.unlock()
}

func fail(_ code: String, _ message: String) -> Never {
  emit(["type": "error", "code": code, "message": message])
  exit(1)
}

func argument(_ name: String) -> String? {
  let args = CommandLine.arguments
  guard let index = args.firstIndex(of: name), index + 1 < args.count else { return nil }
  return args[index + 1]
}

@available(macOS 26.0, *)
func supportedLocale(_ localeId: String) async -> Locale {
  guard let locale = await SpeechTranscriber.supportedLocale(equivalentTo: Locale(identifier: localeId)) else {
    fail("unsupported_locale", "On-device dictation does not support the language \(localeId).")
  }
  return locale
}

@available(macOS 26.0, *)
func installAssets(_ module: SpeechTranscriber) async throws {
  if let request = try await AssetInventory.assetInstallationRequest(supporting: [module]) {
    try await request.downloadAndInstall()
  }
}

/// Reads stdin on its own thread and yields converted buffers until EOF.
@available(macOS 26.0, *)
func pumpStdin(into continuations: [AsyncStream<AnalyzerInput>.Continuation], format target: AVAudioFormat) {
  guard let source = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: sampleRate, channels: 1, interleaved: true),
        let converter = AVAudioConverter(from: source, to: target) else {
    fail("audio_format", "Cannot convert 16 kHz PCM to \(target).")
  }
  let thread = Thread {
    let stdin = FileHandle.standardInput
    var carry = Data()
    while true {
      let chunk = stdin.availableData
      if chunk.isEmpty { break }
      carry.append(chunk)
      let frames = carry.count / 2
      if frames == 0 { continue }
      let bytes = frames * 2
      guard let input = AVAudioPCMBuffer(pcmFormat: source, frameCapacity: AVAudioFrameCount(frames)) else { continue }
      input.frameLength = AVAudioFrameCount(frames)
      _ = carry.withUnsafeBytes { raw in
        memcpy(input.int16ChannelData![0], raw.baseAddress!, bytes)
      }
      carry.removeFirst(bytes)
      let ratio = target.sampleRate / sampleRate
      let capacity = AVAudioFrameCount(Double(frames) * ratio) + 32
      guard let output = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: capacity) else { continue }
      var consumed = false
      var error: NSError?
      converter.convert(to: output, error: &error) { _, status in
        if consumed {
          status.pointee = .noDataNow
          return nil
        }
        consumed = true
        status.pointee = .haveData
        return input
      }
      if let error { fail("audio_convert", error.localizedDescription) }
      if output.frameLength > 0 {
        for continuation in continuations { continuation.yield(AnalyzerInput(buffer: output)) }
      }
    }
    for continuation in continuations { continuation.finish() }
  }
  thread.start()
}

/// Committed text from the accurate transcriber, then the fast transcriber's
/// text for the audio after it.
actor Transcript {
  private(set) var committed = ""
  private var committedEnd = 0.0
  private var fastFinals: [(text: String, start: Double)] = []
  private var fastVolatile: (text: String, start: Double)? = nil
  private var shown = ""

  func fast(_ text: String, start: Double, end: Double, isFinal: Bool) {
    if isFinal {
      fastFinals.append((text, start))
      fastVolatile = nil
    } else {
      fastVolatile = (text, start)
    }
    show()
  }

  func commit(_ text: String, end: Double) {
    committed += text
    committedEnd = max(committedEnd, end)
    fastFinals.removeAll { $0.start < committedEnd - 0.05 }
    emit(["type": "final", "text": committed])
    show()
  }

  private func show() {
    var text = committed
    for segment in fastFinals where segment.start >= committedEnd - 0.05 { text += segment.text }
    if let volatile = fastVolatile, volatile.start >= committedEnd - 0.05 { text += volatile.text }
    if text == shown { return }
    shown = text
    emit(["type": "partial", "text": text])
  }
}

@available(macOS 26.0, *)
func run(localeId: String, checkOnly: Bool) async {
  do {
    let locale = await supportedLocale(localeId)
    let tag = locale.identifier(.bcp47)
    if checkOnly {
      let installed = await SpeechTranscriber.installedLocales.contains { $0.identifier(.bcp47) == tag }
      emit(["type": "check", "available": true, "locale": tag, "installed": installed])
      exit(0)
    }
    // Two analyzers on the same audio: the fast one gives live text while the
    // user talks; the accurate one commits text at each pause. One analyzer
    // with both transcribers gives the fast (less accurate) text for both.
    let fast = SpeechTranscriber(locale: locale, transcriptionOptions: [], reportingOptions: [.volatileResults, .fastResults], attributeOptions: [])
    let accurate = SpeechTranscriber(locale: locale, transcriptionOptions: [], reportingOptions: [], attributeOptions: [])
    try await installAssets(fast)
    guard let format = await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: [fast, accurate]) else {
      fail("audio_format", "SpeechAnalyzer has no audio format for this language.")
    }
    let fastAnalyzer = SpeechAnalyzer(modules: [fast])
    let accurateAnalyzer = SpeechAnalyzer(modules: [accurate])
    let (fastInputs, fastContinuation) = AsyncStream<AnalyzerInput>.makeStream()
    let (accurateInputs, accurateContinuation) = AsyncStream<AnalyzerInput>.makeStream()
    let transcript = Transcript()
    let fastTask = Task {
      for try await result in fast.results {
        await transcript.fast(String(result.text.characters), start: result.range.start.seconds,
                              end: result.range.end.seconds, isFinal: result.isFinal)
      }
    }
    let results = Task { () -> String in
      for try await result in accurate.results {
        await transcript.commit(String(result.text.characters), end: result.range.end.seconds)
      }
      try await fastTask.value
      return await transcript.committed
    }
    try await fastAnalyzer.start(inputSequence: fastInputs)
    try await accurateAnalyzer.start(inputSequence: accurateInputs)
    emit(["type": "ready"])
    pumpStdin(into: [fastContinuation, accurateContinuation], format: format)
    async let fastDone: Void = fastAnalyzer.finalizeAndFinishThroughEndOfInput()
    try await accurateAnalyzer.finalizeAndFinishThroughEndOfInput()
    try await fastDone
    let text = try await results.value
    emit(["type": "done", "text": text.trimmingCharacters(in: .whitespacesAndNewlines)])
    exit(0)
  } catch {
    fail("speech_failed", error.localizedDescription)
  }
}

let localeId = argument("--locale") ?? "en-US"
let checkOnly = CommandLine.arguments.contains("--check")
if #available(macOS 26.0, *) {
  Task { await run(localeId: localeId, checkOnly: checkOnly) }
  dispatchMain()
} else {
  if checkOnly {
    emit(["type": "check", "available": false, "reason": "On-device dictation needs macOS 26 or later."])
    exit(0)
  }
  fail("unsupported_os", "On-device dictation needs macOS 26 or later.")
}
