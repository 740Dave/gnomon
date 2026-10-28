// presence_helper.swift -- ask macOS to prove the DEVICE OWNER is present (spec 13 amendment, g76).
//
// usage: presence_helper <timeout-seconds> <reason text...>
// Policy: LAPolicyDeviceOwnerAuthentication = Touch ID when a sensor exists, else the login PASSWORD
// (this Mac has no Touch ID sensor, so it is the password dialog). The system draws the dialog; the
// answer is typed into WindowServer's secure input, never into this process's stdin or any PTY.
//
// Exit codes (the ONLY output channel; nothing is read from stdin):
//   0  authenticated
//   1  refused: the operator cancelled / failed the check
//   2  timed out waiting for the operator (the dialog is invalidated, i.e. dismissed)
//   3  unavailable: the policy cannot be evaluated here (no password set, no GUI session, ...)
//   64 usage error
// stderr carries one line "LA-ERROR <code> <domain>" for the non-zero cases (evidence for the audit log).
import Foundation
import LocalAuthentication

setvbuf(stdout, nil, _IOLBF, 0)
let args = CommandLine.arguments
guard args.count >= 3, let timeout = Double(args[1]), timeout > 0, timeout <= 600 else {
    FileHandle.standardError.write("usage: presence_helper <timeout-seconds 0-600> <reason>\n".data(using: .utf8)!)
    exit(64)
}
let reason = args[2...].joined(separator: " ")
let ctx = LAContext()
var probeErr: NSError?
if !ctx.canEvaluatePolicy(.deviceOwnerAuthentication, error: &probeErr) {
    let e = probeErr
    FileHandle.standardError.write("LA-ERROR \(e?.code ?? 0) \(e?.domain ?? "?")\n".data(using: .utf8)!)
    exit(3)
}
let sem = DispatchSemaphore(value: 0)
var outcome: Int32 = 1
var laErr: NSError?
ctx.evaluatePolicy(.deviceOwnerAuthentication, localizedReason: reason) { ok, err in
    outcome = ok ? 0 : 1
    laErr = err as NSError?
    sem.signal()
}
if sem.wait(timeout: .now() + timeout) == .timedOut {
    ctx.invalidate()              // dismisses the system dialog
    FileHandle.standardError.write("LA-ERROR timeout LocalAuthentication-helper\n".data(using: .utf8)!)
    exit(2)
}
if outcome != 0 {
    FileHandle.standardError.write("LA-ERROR \(laErr?.code ?? 0) \(laErr?.domain ?? "?")\n".data(using: .utf8)!)
}
exit(outcome)
