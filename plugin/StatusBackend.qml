pragma ComponentBehavior: Bound

import QtQuick
import Quickshell
import Quickshell.Io
import "GlanceLogic.js" as GlanceLogic

// Process boundary for everything the panel knows. The QML never touches a
// socket: it runs `glancectl status --json` and consumes the schema-1 document
// on stdout, and every action is one more `glancectl` invocation run as you.
//
// That keeps the plugin presentation-only in the sense that matters: it has no
// path to the daemon the user does not also have from a terminal.
//
// Three rules hold for every child, because a shell plugin runs as the user
// and neither a shadowed PATH entry nor a runaway executable may reach the
// daemon's setup step through it:
//   - fixed tools run by absolute path (GlanceLogic.SYSTEMCTL and friends),
//     and glancectl itself, default or configured, is checked with stat(1)
//     before its first use: absolute, no symlinks, every component owned by
//     root or you and writable by nobody else, the file regular and executable;
//   - the child environment pins PATH and drops interpreter and loader
//     overrides (GlanceLogic.childEnvironment);
//   - stdout and stderr are capped live. A child that exceeds the cap is
//     killed and what it wrote is never parsed or rendered.
Item {
  id: root
  visible: false

  property var settings: ({})

  // Parsed status, or null before the first successful poll.
  property var status: null
  property bool loading: false
  property string fetchError: ""
  // The one failure the user fixes by installing something or setting a path:
  // glancectl is absent, or was refused. `binaryProblem` says which and why.
  property bool binaryMissing: false
  property string binaryProblem: ""
  // True once stat(1) has vouched for binaryPath. Nothing runs before that.
  property bool binaryOk: false
  property double lastSuccessAt: 0
  property bool pendingRefresh: false

  // Actions: arm, disarm, authenticate. One at a time.
  property bool actionBusy: false
  property string actionName: ""
  property var actionResult: null

  // Launches: the setup steps, which open a window or a terminal and outlive
  // the call. They deliberately do not take actionBusy — enrolling a face is a
  // minute of sweeping, and the panel stays usable throughout.
  property string launchError: ""
  // Bumped every time a launch settles, so a test can tell one actually ran.
  property int launchCount: 0

  readonly property int refreshIntervalSec: Math.round(GlanceLogic.clamp(
    setting("refreshIntervalSec", 30), 5, 600))
  // The configured path wins; otherwise the one the glanced package installs.
  // There is deliberately no search: not PATH, not a guess under $HOME.
  readonly property string configuredPath: String(setting("glancectlPath", "")).trim()
  readonly property string binaryPath: configuredPath !== "" ? configuredPath : GlanceLogic.DEFAULT_GLANCECTL
  readonly property var childEnvironment: GlanceLogic.childEnvironment()

  readonly property string userName: Quickshell.env("USER") || "$USER"
  // The one setup step still outstanding, bound to the glancectl we checked
  // so the button and the command printed under it cannot disagree.
  readonly property var nextAction: binaryOk ? GlanceLogic.nextAction(status, binaryPath, userName) : null

  readonly property bool reachable: status ? status.reachable : false
  readonly property bool armed: status ? status.armed : false
  readonly property bool scanning: (status ? status.scanning : false) || actionName === "authenticate"
  readonly property var identities: status ? status.identities : []

  signal refreshed()

  function setting(name, fallback) {
    var value = settings ? settings[name] : undefined
    return value === undefined || value === null ? fallback : value
  }

  // --- the binary --------------------------------------------------------

  function checkBinary() {
    if (checkProcess.running) return
    var command = GlanceLogic.statCommand(binaryPath)
    if (!command) {
      refuseBinary(binaryPath + ": " + GlanceLogic.pathSyntaxProblem(binaryPath))
      return
    }
    checkProcess.command = command
    checkProcess.running = true
  }

  function refuseBinary(reason) {
    binaryOk = false
    binaryMissing = true
    binaryProblem = reason
    status = null
    loading = false
    fetchError = ""
  }

  function settleCheck() {
    var verdict
    if (checkProcess.oversized) {
      verdict = { ok: false, reason: GlanceLogic.STAT + " produced too much output" }
    } else if (!checkProcess.exitSeen) {
      verdict = { ok: false, reason: "could not run " + GlanceLogic.STAT }
    } else {
      verdict = GlanceLogic.checkBinary(binaryPath, checkProcess.body)
    }
    if (!verdict.ok) {
      refuseBinary(verdict.reason)
      return
    }
    binaryOk = true
    binaryMissing = false
    binaryProblem = ""
    refresh()
  }

  // --- status ------------------------------------------------------------

  function refresh() {
    if (!binaryOk) {
      checkBinary()
      return
    }
    if (statusProcess.running) {
      pendingRefresh = true
      return
    }
    pendingRefresh = false
    statusProcess.command = [binaryPath, "status", "--json"]
    statusProcess.running = true
  }

  function settleStatus() {
    loading = false
    if (statusProcess.oversized) {
      fetchError = "glancectl status wrote more than " + GlanceLogic.STDOUT_CAP + " bytes; output discarded"
    } else if (statusProcess.timedOut) {
      fetchError = "glancectl status timed out"
    } else if (!statusProcess.exitSeen) {
      fetchError = "Could not run " + binaryPath
    } else {
      // `status --json` exits 1 when the daemon is offline but still prints a
      // document, so the exit code is not the signal; the parse is.
      var parsed = GlanceLogic.parseStatus(statusProcess.body)
      if (!parsed) {
        fetchError = statusProcess.lastExit !== 0 && statusProcess.body.trim() === ""
          ? "glancectl exited with status " + statusProcess.lastExit
          : "glancectl returned unreadable status; update the plugin or glancectl"
      } else {
        status = parsed
        fetchError = ""
        lastSuccessAt = Date.now()
        refreshed()
      }
    }
    if (pendingRefresh) Qt.callLater(function() { root.refresh() })
  }

  // --- actions ----------------------------------------------------------

  function testScan() { runAction("authenticate", [binaryPath, "authenticate", "--json"], "") }
  function disarm() { runAction("disarm", [binaryPath, "disarm", "--json"], "") }
  function arm(passphrase, remember) {
    var args = [binaryPath, "arm", "--json", "--passphrase-stdin"]
    if (remember) args.push("--remember")
    // The passphrase goes over stdin, never argv, so it is not in `ps`.
    runAction("arm", args, passphrase + "\n")
  }

  // Run the outstanding setup step, wrapped as its shape demands.
  function runNextAction() {
    if (!binaryOk) return
    var command = GlanceLogic.launchCommand(nextAction)
    if (!command || launchProcess.running) return
    launchError = ""
    launchProcess.command = command
    launchProcess.running = true
  }

  function settleLaunch() {
    launchCount += 1
    if (launchProcess.oversized) {
      launchError = "launcher wrote more than " + GlanceLogic.STDERR_CAP + " bytes to stderr; output discarded"
    } else if (launchProcess.exitSeen && launchProcess.lastExit !== 0) {
      launchError = String(launchProcess.errBody).trim()
        || "exited with status " + launchProcess.lastExit
    }
    // Whatever was launched changes the daemon's state on its own schedule, so
    // poll briefly rather than trusting one refresh to catch it.
    followUp.count = 0
    followUp.restart()
    refresh()
  }

  function runAction(name, command, stdinText) {
    if (actionBusy || !binaryOk) return
    actionName = name
    actionResult = null
    actionProcess.secret = stdinText
    actionProcess.command = command
    actionProcess.running = true
  }

  function settleAction() {
    var result
    if (actionProcess.oversized) {
      result = { ok: false, outcome: "", identity: "", reason: "", error: actionName + " wrote more than " + GlanceLogic.STDOUT_CAP + " bytes; output discarded" }
    } else if (actionProcess.timedOut) {
      result = { ok: false, outcome: "", identity: "", reason: "", error: actionName + " timed out" }
    } else if (!actionProcess.exitSeen) {
      result = { ok: false, outcome: "", identity: "", reason: "", error: "Could not run " + binaryPath }
    } else {
      result = GlanceLogic.parseActionResult(actionProcess.body, actionProcess.errBody, actionProcess.lastExit)
    }
    result.name = actionName
    actionResult = result
    actionName = ""
    actionBusy = false
    refresh()
  }

  // Append under a cap; over it, stop the process and remember why. The kill
  // is deferred one tick because this runs inside the process's own read
  // handler; the flag makes any bytes that land in between fall on the floor.
  function take(proc, field, data, cap) {
    if (proc.oversized) return
    var kept = GlanceLogic.appendCapped(proc[field], data, cap)
    proc[field] = kept.text
    if (kept.overflow) {
      proc.oversized = true
      Qt.callLater(function() { proc.running = false })
    }
  }

  onBinaryPathChanged: {
    binaryOk = false
    binaryMissing = false
    binaryProblem = ""
    status = null
    checkBinary()
  }

  Process {
    id: checkProcess
    running: false
    environment: root.childEnvironment

    property string body: ""
    property bool exitSeen: false
    property bool oversized: false

    stdout: SplitParser {
      splitMarker: ""
      onRead: function(data) { root.take(checkProcess, "body", data, GlanceLogic.STDOUT_CAP) }
    }
    stderr: SplitParser { splitMarker: "" }

    onExited: function(exitCode) { checkProcess.exitSeen = true }

    onRunningChanged: {
      if (running) {
        body = ""
        exitSeen = false
        oversized = false
        checkTimeout.restart()
      } else {
        checkTimeout.stop()
        root.settleCheck()
      }
    }
  }

  Timer {
    id: checkTimeout
    interval: 5000
    repeat: false
    onTriggered: checkProcess.running = false
  }

  Process {
    id: statusProcess
    running: false
    environment: root.childEnvironment

    property string body: ""
    property bool exitSeen: false
    property int lastExit: 0
    property bool timedOut: false
    property bool oversized: false

    stdout: SplitParser {
      splitMarker: ""
      onRead: function(data) { root.take(statusProcess, "body", data, GlanceLogic.STDOUT_CAP) }
    }
    stderr: SplitParser { splitMarker: "" }

    onExited: function(exitCode) {
      statusProcess.exitSeen = true
      statusProcess.lastExit = exitCode
    }

    onRunningChanged: {
      if (running) {
        body = ""
        exitSeen = false
        lastExit = 0
        timedOut = false
        oversized = false
        root.loading = true
        statusTimeout.restart()
      } else {
        statusTimeout.stop()
        root.settleStatus()
      }
    }
  }

  Timer {
    id: statusTimeout
    interval: 10000
    repeat: false
    onTriggered: {
      statusProcess.timedOut = true
      statusProcess.running = false
    }
  }

  Process {
    id: actionProcess
    running: false
    stdinEnabled: true
    environment: root.childEnvironment

    property string secret: ""
    property string body: ""
    property string errBody: ""
    property bool exitSeen: false
    property int lastExit: 0
    property bool timedOut: false
    property bool oversized: false

    stdout: SplitParser {
      splitMarker: ""
      onRead: function(data) { root.take(actionProcess, "body", data, GlanceLogic.STDOUT_CAP) }
    }
    stderr: SplitParser {
      splitMarker: ""
      onRead: function(data) { root.take(actionProcess, "errBody", data, GlanceLogic.STDERR_CAP) }
    }

    // `arm --passphrase-stdin` reads exactly one line and needs no EOF; the
    // other actions never read stdin at all.
    onStarted: {
      if (secret !== "") write(secret)
      secret = ""
    }

    onExited: function(exitCode) {
      actionProcess.exitSeen = true
      actionProcess.lastExit = exitCode
    }

    onRunningChanged: {
      if (running) {
        body = ""
        errBody = ""
        exitSeen = false
        lastExit = 0
        timedOut = false
        oversized = false
        root.actionBusy = true
        actionTimeout.restart()
      } else {
        actionTimeout.stop()
        root.settleAction()
      }
    }
  }

  // A scan can legitimately take the daemon's full scan timeout plus model
  // warm-up on the first run; leave it well clear of that.
  Timer {
    id: actionTimeout
    interval: 45000
    repeat: false
    onTriggered: {
      actionProcess.timedOut = true
      actionProcess.running = false
    }
  }

  Process {
    id: launchProcess
    running: false
    environment: root.childEnvironment

    property string errBody: ""
    property bool exitSeen: false
    property int lastExit: 0
    property bool oversized: false

    // stdout is not retained at all; the launched command talks to its own
    // window, not to us.
    stdout: SplitParser { splitMarker: "" }
    stderr: SplitParser {
      splitMarker: ""
      onRead: function(data) { root.take(launchProcess, "errBody", data, GlanceLogic.STDERR_CAP) }
    }

    onExited: function(exitCode) {
      launchProcess.exitSeen = true
      launchProcess.lastExit = exitCode
    }

    onRunningChanged: {
      if (running) {
        errBody = ""
        exitSeen = false
        lastExit = 0
        oversized = false
      } else {
        root.settleLaunch()
      }
    }
  }

  // A minute of quick polling after a launch, so the panel notices the daemon
  // starting or the sweep finishing without waiting out the idle interval.
  Timer {
    id: followUp
    property int count: 0
    interval: 3000
    repeat: true
    onTriggered: {
      count += 1
      if (count >= 20) stop()
      root.refresh()
    }
  }

  Timer {
    id: refreshTimer
    interval: root.refreshIntervalSec * 1000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }
}
