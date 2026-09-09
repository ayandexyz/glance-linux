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
Item {
  id: root
  visible: false

  property var settings: ({})

  // Parsed status, or null before the first successful poll.
  property var status: null
  property bool loading: false
  property string fetchError: ""
  // The one failure the user fixes by installing something or setting a path.
  property bool binaryMissing: false
  property double lastSuccessAt: 0
  property bool pendingRefresh: false

  // Actions: arm, disarm, authenticate. One at a time.
  property bool actionBusy: false
  property string actionName: ""
  property var actionResult: null

  readonly property int refreshIntervalSec: Math.round(GlanceLogic.clamp(
    setting("refreshIntervalSec", 30), 5, 600))
  // Configured path wins. Otherwise PATH, and if that fails once, the place
  // packaging/install.sh links the binary to — the shell's PATH is not the
  // user's login PATH, and a venv checkout is never on it.
  readonly property string configuredPath: String(setting("glancectlPath", "")).trim()
  readonly property string fallbackPath: Quickshell.env("HOME") + "/.local/bin/glancectl"
  property bool useFallback: false
  readonly property string binaryPath: configuredPath !== "" ? configuredPath
    : (useFallback ? fallbackPath : "glancectl")

  readonly property bool reachable: status ? status.reachable : false
  readonly property bool armed: status ? status.armed : false
  readonly property bool scanning: (status ? status.scanning : false) || actionName === "authenticate"
  readonly property var identities: status ? status.identities : []

  signal refreshed()

  function setting(name, fallback) {
    var value = settings ? settings[name] : undefined
    return value === undefined || value === null ? fallback : value
  }

  function refresh() {
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
    if (statusProcess.timedOut) {
      fetchError = "glancectl status timed out"
    } else if (!statusProcess.exitSeen) {
      if (configuredPath === "" && !useFallback) {
        // Not on PATH; try the install location once before giving up.
        useFallback = true
        return
      }
      binaryMissing = true
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
        binaryMissing = false
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

  function runAction(name, command, stdinText) {
    if (actionBusy) return
    actionName = name
    actionResult = null
    actionProcess.secret = stdinText
    actionProcess.command = command
    actionProcess.running = true
  }

  function settleAction() {
    var result
    if (actionProcess.timedOut) {
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

  onConfiguredPathChanged: {
    binaryMissing = false
    useFallback = false
  }
  onBinaryPathChanged: {
    binaryMissing = false
    refresh()
  }

  Process {
    id: statusProcess
    running: false

    property string body: ""
    property bool exitSeen: false
    property int lastExit: 0
    property bool timedOut: false

    stdout: SplitParser {
      splitMarker: ""
      onRead: function(data) { statusProcess.body += String(data) }
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

    property string secret: ""
    property string body: ""
    property string errBody: ""
    property bool exitSeen: false
    property int lastExit: 0
    property bool timedOut: false

    stdout: SplitParser {
      splitMarker: ""
      onRead: function(data) { actionProcess.body += String(data) }
    }
    stderr: SplitParser {
      splitMarker: ""
      onRead: function(data) { actionProcess.errBody += String(data) }
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

  Timer {
    id: refreshTimer
    interval: root.refreshIntervalSec * 1000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }
}
