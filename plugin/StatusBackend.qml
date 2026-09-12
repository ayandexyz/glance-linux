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
// Four rules hold for every child, because a shell plugin runs as the user
// and neither a shadowed PATH entry nor a runaway process tree may reach the
// daemon's setup step through it:
//   - fixed tools run by absolute path (GlanceLogic.SYSTEMCTL and friends);
//   - glancectl itself, default or configured, is checked with stat(1)
//     immediately before *every* run, never once and then trusted: absolute,
//     no symlinks, every component owned by root or you and writable by
//     nobody else, the file regular and executable;
//   - and it is then executed *as the object that check accepted*, never as
//     the pathname it had. Every invocation goes through
//     GlanceLogic.verifiedCommand, which opens the path once, fstats that
//     descriptor and execs the descriptor itself, so there is no second
//     pathname resolution for anything to be swapped into;
//   - the child environment pins PATH and drops interpreter and loader
//     overrides (GlanceLogic.childEnvironment);
//   - every child is wrapped in timeout(1), which runs it in a process group
//     of its own and signals the group, so the deadline and the output caps
//     below bound the whole tree — glancectl's sudo, make and
//     omarchy-apply-lock descendants included — rather than one pid.
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
  // True once stat(1) has vouched for binaryPath at least once. Nothing runs
  // before that, and nothing runs on the strength of it alone either: the
  // check is re-run ahead of each command.
  property bool binaryOk: false
  // Device, inode, size, mtime, owner and mode of the glancectl the last
  // accepted check saw. Null until there has been one.
  property var binaryIdentity: null
  property double lastSuccessAt: 0
  property bool pendingRefresh: false

  // Commands waiting on a check to vouch for the binary, keyed so a second
  // refresh while one is in flight replaces the first rather than stacking.
  property var pendingRuns: []

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

  // Nothing runs glancectl directly. Everything goes through here: the command
  // is held, stat(1) is re-run, and only a verdict that both passes the
  // ownership rules and matches the retained identity releases it.
  function withVerifiedBinary(key, run, fail) {
    var queue = []
    for (var i = 0; i < pendingRuns.length; i++) {
      if (pendingRuns[i].key !== key) queue.push(pendingRuns[i])
    }
    queue.push({ key: key, run: run, fail: fail })
    pendingRuns = queue
    checkBinary()
  }

  function drainPending() {
    var queue = pendingRuns
    pendingRuns = []
    for (var i = 0; i < queue.length; i++) queue[i].run()
  }

  function failPending(reason) {
    var queue = pendingRuns
    pendingRuns = []
    for (var i = 0; i < queue.length; i++) {
      if (queue[i].fail) queue[i].fail(reason)
    }
  }

  function checkBinary() {
    if (checkProcess.running) return
    var command = GlanceLogic.statCommand(binaryPath)
    if (!command) {
      refuseBinary(binaryPath + ": " + GlanceLogic.pathSyntaxProblem(binaryPath))
      return
    }
    checkProcess.command = GlanceLogic.bounded(command, GlanceLogic.CHECK_DEADLINE_SEC)
    checkProcess.running = true
  }

  function refuseBinary(reason) {
    binaryOk = false
    binaryIdentity = null
    binaryMissing = true
    binaryProblem = reason
    status = null
    loading = false
    fetchError = ""
    failPending(reason)
  }

  function settleCheck() {
    var verdict
    if (checkProcess.oversized) {
      verdict = { ok: false, reason: GlanceLogic.STAT + " produced too much output" }
    } else if (!checkProcess.exitSeen || GlanceLogic.deadlineHit(checkProcess.lastExit)) {
      verdict = { ok: false, reason: "could not run " + GlanceLogic.STAT }
    } else {
      verdict = GlanceLogic.checkBinary(binaryPath, checkProcess.body, binaryIdentity)
    }
    if (!verdict.ok) {
      refuseBinary(verdict.reason)
      return
    }
    binaryOk = true
    binaryMissing = false
    binaryProblem = ""
    var replaced = verdict.changed
    binaryIdentity = verdict.identity
    if (replaced) {
      // A different file now answers to the same name. It passes the rules, so
      // it is taken as the current glancectl, but what was queued was queued
      // against the old one: drop it and let the next check release it.
      failPending(binaryPath + " was replaced since the last command ran (now "
                  + GlanceLogic.describeIdentity(binaryIdentity) + "); re-checked, nothing ran")
      Qt.callLater(function() { root.refresh() })
      return
    }
    drainPending()
  }

  // --- stopping a tree ---------------------------------------------------

  // SIGTERM goes to timeout(1), which forwards it to the process group it put
  // the command in, so descendants go with it; the hardStop below is for a
  // wrapper that does not go quietly, and its group is condemned either way
  // because timeout's own --kill-after is already running.
  function stopTree(proc) {
    if (!proc.running) return
    proc.signal(15)
    hardStop.arm(proc)
  }

  // --- status ------------------------------------------------------------

  function refresh() {
    if (statusProcess.running) {
      pendingRefresh = true
      return
    }
    pendingRefresh = false
    withVerifiedBinary("status", function() { root.startStatus() },
                       function(reason) { root.loading = false; if (root.binaryOk) root.fetchError = reason })
  }

  function startStatus() {
    if (statusProcess.running) {
      pendingRefresh = true
      return
    }
    var command = GlanceLogic.verifiedCommand([binaryPath, "status", "--json"], binaryIdentity)
    if (!command) return
    statusProcess.command = GlanceLogic.bounded(command, GlanceLogic.STATUS_DEADLINE_SEC)
    statusProcess.running = true
  }

  function settleStatus() {
    loading = false
    if (statusProcess.oversized) {
      fetchError = "glancectl status wrote more than " + GlanceLogic.STDOUT_CAP + " bytes; output discarded"
    } else if (statusProcess.timedOut || GlanceLogic.deadlineHit(statusProcess.lastExit)) {
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

  function runAction(name, command, stdinText) {
    if (actionBusy) return
    // Held from the click, not from the spawn: the check runs in between and
    // a second click must not slip through it.
    actionBusy = true
    actionName = name
    actionResult = null
    withVerifiedBinary("action", function() {
      var argv = GlanceLogic.verifiedCommand(command, root.binaryIdentity)
      if (!argv) {
        root.actionResult = { name: name, ok: false, outcome: "", identity: "", reason: "",
                              error: "no checked glancectl to run" }
        root.actionName = ""
        root.actionBusy = false
        return
      }
      actionProcess.secret = stdinText
      actionProcess.command = GlanceLogic.bounded(argv, GlanceLogic.ACTION_DEADLINE_SEC)
      actionProcess.running = true
    }, function(reason) {
      root.actionResult = { name: name, ok: false, outcome: "", identity: "", reason: "", error: reason }
      root.actionName = ""
      root.actionBusy = false
    })
  }

  function settleAction() {
    var result
    if (actionProcess.oversized) {
      result = { ok: false, outcome: "", identity: "", reason: "", error: actionName + " wrote more than " + GlanceLogic.STDOUT_CAP + " bytes; output discarded" }
    } else if (actionProcess.timedOut || GlanceLogic.deadlineHit(actionProcess.lastExit)) {
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

  // --- launches -----------------------------------------------------------

  // Run the outstanding setup step, wrapped as its shape demands.
  function runNextAction() {
    if (!binaryOk || launchProcess.running) return
    var step = nextAction
    if (!step || !step.command) return
    launchError = ""
    withVerifiedBinary("launch", function() {
      var command = GlanceLogic.launchCommand(step, root.binaryIdentity)
      if (!command || launchProcess.running) return
      // The deadline is on the launcher, not on what it launches: both
      // wrappers hand their window to a session of their own, which is the
      // point — an enrollment sweep must survive a shell reload. Nothing in
      // that window is read back here, and nothing in it holds the panel.
      launchProcess.command = GlanceLogic.bounded(command, GlanceLogic.LAUNCH_DEADLINE_SEC)
      launchProcess.running = true
    }, function(reason) {
      root.launchError = reason
      root.launchCount += 1
    })
  }

  function settleLaunch() {
    launchCount += 1
    if (launchProcess.oversized) {
      launchError = "launcher wrote more than " + GlanceLogic.STDERR_CAP + " bytes to stderr; output discarded"
    } else if (GlanceLogic.deadlineHit(launchProcess.lastExit)) {
      launchError = "the launcher did not return within " + GlanceLogic.LAUNCH_DEADLINE_SEC + "s"
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

  // Append under a cap; over it, stop the process tree and remember why. The
  // kill is deferred one tick because this runs inside the process's own read
  // handler; the flag makes any bytes that land in between fall on the floor.
  function take(proc, field, data, cap) {
    if (proc.oversized) return
    var kept = GlanceLogic.appendCapped(proc[field], data, cap)
    proc[field] = kept.text
    if (kept.overflow) {
      proc.oversized = true
      Qt.callLater(function() { root.stopTree(proc) })
    }
  }

  onBinaryPathChanged: {
    binaryOk = false
    binaryIdentity = null
    binaryMissing = false
    binaryProblem = ""
    status = null
    failPending("glancectl path changed")
    checkBinary()
  }

  // One timer for every tree that has been told to go and has not yet gone.
  Timer {
    id: hardStop
    property var victims: []
    interval: (GlanceLogic.KILL_GRACE_SEC + 2) * 1000
    repeat: false
    function arm(proc) {
      victims = victims.concat([proc])
      restart()
    }
    onTriggered: {
      var list = victims
      victims = []
      for (var i = 0; i < list.length; i++) {
        if (list[i].running) {
          list[i].signal(9)
          list[i].running = false
        }
      }
    }
  }

  Process {
    id: checkProcess
    running: false
    environment: root.childEnvironment

    property string body: ""
    property bool exitSeen: false
    property int lastExit: 0
    property bool oversized: false

    stdout: SplitParser {
      splitMarker: ""
      onRead: function(data) { root.take(checkProcess, "body", data, GlanceLogic.STDOUT_CAP) }
    }
    stderr: SplitParser { splitMarker: "" }

    onExited: function(exitCode) {
      checkProcess.exitSeen = true
      checkProcess.lastExit = exitCode
    }

    onRunningChanged: {
      if (running) {
        body = ""
        exitSeen = false
        lastExit = 0
        oversized = false
        checkTimeout.restart()
      } else {
        checkTimeout.stop()
        root.settleCheck()
      }
    }
  }

  // Backstop only: timeout(1) enforces the deadline on the tree, and these
  // timers fire after its SIGKILL grace has also passed, for the case where
  // the wrapper itself is the thing that is stuck.
  Timer {
    id: checkTimeout
    interval: (GlanceLogic.CHECK_DEADLINE_SEC + GlanceLogic.KILL_GRACE_SEC + 3) * 1000
    repeat: false
    onTriggered: root.stopTree(checkProcess)
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
    interval: (GlanceLogic.STATUS_DEADLINE_SEC + GlanceLogic.KILL_GRACE_SEC + 3) * 1000
    repeat: false
    onTriggered: {
      statusProcess.timedOut = true
      root.stopTree(statusProcess)
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
    // other actions never read stdin at all. timeout(1) passes stdin straight
    // through to the child.
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

  Timer {
    id: actionTimeout
    interval: (GlanceLogic.ACTION_DEADLINE_SEC + GlanceLogic.KILL_GRACE_SEC + 3) * 1000
    repeat: false
    onTriggered: {
      actionProcess.timedOut = true
      root.stopTree(actionProcess)
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
        launchTimeout.restart()
      } else {
        launchTimeout.stop()
        root.settleLaunch()
      }
    }
  }

  Timer {
    id: launchTimeout
    interval: (GlanceLogic.LAUNCH_DEADLINE_SEC + GlanceLogic.KILL_GRACE_SEC + 3) * 1000
    repeat: false
    onTriggered: root.stopTree(launchProcess)
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
