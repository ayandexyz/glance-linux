.pragma library

// Pure functions over the `glancectl status --json` contract (schemaVersion 1)
// and the `authenticate --json` / `arm --json` results. No QML in here, so the
// node tests in tests/ exercise exactly what the panel renders.

// Every executable the plugin runs, by absolute path. Nothing is resolved
// through PATH: a shell plugin runs as the user, and a shadowed `systemctl`
// or `glancectl` earlier on an inherited PATH would otherwise run in place of
// the real one, on the way to the interactive PAM setup step.
var DEFAULT_GLANCECTL = "/usr/bin/glancectl"
var SYSTEMCTL = "/usr/bin/systemctl"
var SETSID = "/usr/bin/setsid"
var LAUNCH_TERMINAL = "/usr/bin/omarchy-launch-terminal"
var STAT = "/usr/bin/stat"

// The PATH handed to children. glancectl's own subprocesses (sudo, make,
// omarchy-apply-lock during setup-pam) resolve through this and nothing else.
var SAFE_PATH = "/usr/bin:/usr/share/omarchy/bin"

// Live ceilings on what a child may write before it is killed unread. A
// status document is a few hundred bytes; a scan result is one line. The
// timers bound how long a process may run, these bound how much it may say.
var STDOUT_CAP = 64 * 1024
var STDERR_CAP = 16 * 1024

function clamp(value, low, high) {
  var n = Number(value)
  if (!isFinite(n)) return low
  return Math.min(high, Math.max(low, n))
}

function listOrEmpty(value) {
  return Array.isArray(value) ? value : []
}

// Parse a status document. Returns null for anything that is not schema 1,
// so the panel refuses to guess at a format it was not written against.
function parseStatus(text) {
  var parsed = null
  try { parsed = JSON.parse(String(text)) } catch (e) { return null }
  if (!parsed || typeof parsed !== "object") return null
  if (Number(parsed.schemaVersion) !== 1) return null
  return {
    reachable: parsed.reachable === true,
    error: String(parsed.error || ""),
    armed: parsed.armed === true,
    scanning: parsed.scanning === true,
    enrolled: parsed.enrolled === true,
    remembered: parsed.remembered === true,
    mode: String(parsed.mode || ""),
    camera: String(parsed.camera || ""),
    models: parsed.models && typeof parsed.models === "object" ? parsed.models : {},
    pam: parsed.pam && typeof parsed.pam === "object" ? parsed.pam : null,
    identities: listOrEmpty(parsed.identities),
    lastScan: parsed.lastScan && typeof parsed.lastScan === "object" ? parsed.lastScan : null
  }
}

var OUTCOME_LABELS = {
  unlocked: "Unlocked",
  no_match: "No match",
  spoof_denied: "Spoof denied",
  timed_out: "Timed out",
  no_face: "No face seen",
  not_armed: "Not armed",
  error: "Error"
}

function outcomeLabel(outcome) {
  var key = String(outcome || "")
  return OUTCOME_LABELS[key] || (key === "" ? "" : key)
}

// good: an unlock. bad: a spoof or an error — something to look at.
// neutral: the ordinary ways a scan ends without unlocking.
function outcomeSeverity(outcome) {
  var key = String(outcome || "")
  if (key === "unlocked") return "good"
  if (key === "spoof_denied" || key === "error") return "bad"
  return "neutral"
}

function missingModels(status) {
  if (!status) return []
  var missing = []
  if (status.models.landmarker !== true) missing.push("landmarker")
  if (status.models.arcface !== true) missing.push("arcface")
  return missing
}

// One line for the hero's meta slot: the daemon's state, most urgent first.
function stateLabel(status) {
  if (!status) return "Checking..."
  if (!status.reachable) return "Daemon offline"
  if (status.scanning) return "Scanning..."
  if (missingModels(status).length > 0) return "Models missing"
  if (!status.enrolled) return "Not enrolled"
  if (!status.armed) return "Disarmed"
  return "Armed · " + (status.mode === "heavy" ? "heavy liveness" : "light liveness")
}

// The one thing standing between the user and a working unlock, as something
// the panel can run rather than a line to copy — or null when nothing is
// needed. `glancectl` is the binary the backend actually resolved, so the
// button and the command shown under it can never disagree.
//
//   command   argv, or null when the panel handles it inline
//   terminal  needs a terminal window: interactive (sudo) or slow enough to
//             deserve the progress it prints
//   detached  outlives the call; the panel must not wait on it
//
// `arm` alone has no command. The passphrase belongs in the panel's own field,
// where it goes to stdin — never into an argv any other process can read.
function nextAction(status, glancectl, user) {
  var ctl = String(glancectl || DEFAULT_GLANCECTL)
  function action(key, explain, label, icon, command, terminal, detached) {
    return {
      key: key,
      explain: explain,
      label: label,
      icon: icon,
      command: command,
      hint: command ? command.join(" ") : ctl + " " + key,
      terminal: terminal === true,
      detached: detached === true
    }
  }
  if (!status) return null
  if (!status.reachable) {
    return action("start", "glanced is not running.", "Start daemon", "\udb80\udd0a",
                  [SYSTEMCTL, "--user", "enable", "--now", "glanced"], false, false)
  }
  var missing = missingModels(status)
  if (missing.length > 0) {
    return action("fetch-model", "Models not downloaded: " + missing.join(", ") + ".",
                  "Download models", "\udb80\udcac", [ctl, "fetch-model"], true, true)
  }
  if (!status.enrolled) {
    return action("enroll", "No face enrolled yet.", "Enroll", "\udb84\udc7b",
                  [ctl, "enroll", "--name", String(user || "$USER"), "--gui", "--remember"],
                  false, true)
  }
  if (!status.armed) {
    return action("arm", "Enrollment is encrypted; the daemon needs the passphrase to scan.",
                  "Arm", "\udb81\udc83", null, false, false)
  }
  if (status.pam && status.pam.wired !== true) {
    return action("setup-pam", "The lock screen is not wired to the daemon yet. "
                  + "This opens a terminal and asks for your password:",
                  "Wire lock screen", "\udb80\udd83", [ctl, "setup-pam"], true, true)
  }
  return null
}

// How an action's argv has to be wrapped to actually run it. Three shapes,
// because three kinds of command: one that needs a terminal to ask for a
// password or show a progress bar, one that opens its own window and must
// survive a shell reload mid-sweep, and one that just runs.
function launchCommand(action) {
  if (!action || !action.command) return null
  if (action.terminal) {
    // Omarchy's launcher already setsids into the user's chosen terminal.
    return [LAUNCH_TERMINAL].concat(action.command)
  }
  if (action.detached) {
    // A new session leader outlives this plugin: reloading the shell must not
    // kill an enrollment half way through the sweep.
    return [SETSID, "--fork"].concat(action.command)
  }
  return action.command.slice()
}

// The single command that unblocks the user, or "" when nothing is needed.
// Derived from nextAction so the text under the button is the command it runs.
function nextStep(status) {
  var action = nextAction(status, "glancectl", "$USER")
  return action ? action.hint : ""
}

// How the lock screen reaches the daemon, if at all. Null pam block: an older
// glancectl that does not report it, so say nothing rather than "not wired".
function lockLabel(status) {
  if (!status || !status.pam) return ""
  var pam = status.pam
  if (pam.module !== true) return "Module not installed"
  if (pam.shellFingerprint === true) return "Hands-free at lock"
  var stacks = []
  if (pam.shellPassword === true) stacks.push("shell lock")
  if (pam.hyprlock === true) stacks.push("hyprlock")
  if (stacks.length > 0) return "On Enter · " + stacks.join(", ")
  return "Not wired"
}

function identityLine(identity) {
  var name = String(identity && identity.name ? identity.name : "?")
  var captures = Number(identity && identity.captures)
  var text = name
  if (isFinite(captures) && captures > 0) {
    text += " · " + captures + (captures === 1 ? " capture" : " captures")
  }
  if (identity && identity.enabled === false) text += " · disabled"
  return text
}

function elapsed(thenMs, nowMs) {
  var seconds = Math.max(0, Math.round((nowMs - thenMs) / 1000))
  if (seconds < 5) return "just now"
  if (seconds < 60) return seconds + "s ago"
  var minutes = Math.round(seconds / 60)
  if (minutes < 60) return minutes + "m ago"
  var hours = Math.round(minutes / 60)
  if (hours < 24) return hours + "h ago"
  return Math.round(hours / 24) + "d ago"
}

// "Unlocked as ayan · 3s ago", "Spoof denied — Gloss/glare · 1m ago", ...
function lastScanText(status, nowMs) {
  if (!status || !status.lastScan) return ""
  var scan = status.lastScan
  var text = outcomeLabel(scan.outcome)
  if (scan.identity) text += " as " + String(scan.identity)
  var at = Number(scan.at)
  if (isFinite(at) && at > 0) text += " · " + elapsed(at * 1000, nowMs)
  return text
}

function lastScanReason(status) {
  if (!status || !status.lastScan) return ""
  return String(status.lastScan.reason || "")
}

// The result of an action process: arm/disarm/authenticate all print one JSON
// object. Anything else is surfaced verbatim as the error.
function parseActionResult(stdout, stderr, exitCode) {
  var parsed = null
  try { parsed = JSON.parse(String(stdout)) } catch (e) { parsed = null }
  if (parsed && typeof parsed === "object") {
    if (parsed.outcome !== undefined) {
      return { ok: parsed.outcome === "unlocked", outcome: String(parsed.outcome),
               identity: parsed.identity ? String(parsed.identity) : "",
               reason: String(parsed.reason || ""), error: "" }
    }
    return { ok: exitCode === 0, outcome: "", identity: "", reason: "", error: "" }
  }
  var message = String(stderr || "").trim() || String(stdout || "").trim()
  if (message === "") message = "glancectl exited with status " + exitCode
  return { ok: false, outcome: "", identity: "", reason: "", error: message }
}

// --- child processes: what may run, and how much it may say ----------------

// Environment overrides for every child. PATH is pinned; interpreter and
// loader overrides are dropped (null unsets), because glancectl is a Python
// entry point and PYTHONPATH or LD_PRELOAD would let a user-writable file run
// inside it. The rest of the session environment stays: the terminal launcher
// and the enrollment window need the Wayland and D-Bus variables.
function childEnvironment() {
  return {
    PATH: SAFE_PATH,
    PYTHONPATH: null,
    PYTHONHOME: null,
    PYTHONSTARTUP: null,
    PYTHONNOUSERSITE: "1",
    PYTHONSAFEPATH: "1",
    LD_PRELOAD: null,
    LD_LIBRARY_PATH: null,
    LD_AUDIT: null
  }
}

// Why `path` cannot even be looked at, or "" if it is a plain absolute path.
// Syntax only: what it points at is stat(1)'s business, below.
function pathSyntaxProblem(path) {
  var text = String(path || "")
  if (text === "") return "no path given"
  if (text.charAt(0) !== "/") return "must be an absolute path"
  if (/[\x00-\x1f\x7f]/.test(text)) return "contains control characters"
  var parts = text.split("/").slice(1)
  for (var i = 0; i < parts.length; i++) {
    if (parts[i] === "" ) return "contains an empty path component"
    if (parts[i] === "." || parts[i] === "..") return "contains a relative path component"
  }
  return ""
}

// "/usr/bin/glancectl" -> ["/", "/usr", "/usr/bin", "/usr/bin/glancectl"].
// Null when the syntax is unacceptable.
function pathPrefixes(path) {
  if (pathSyntaxProblem(path) !== "") return null
  var parts = String(path).split("/").slice(1)
  var prefixes = ["/"]
  var acc = ""
  for (var i = 0; i < parts.length; i++) {
    acc += "/" + parts[i]
    prefixes.push(acc)
  }
  return prefixes
}

// One stat(1) invocation that reports every component of `path` plus the
// caller's own uid: /proc/self/status is a regular file owned by whoever runs
// stat, so the uid comes from the kernel rather than from an inherited
// environment variable. No -L: a symlink anywhere shows up as one and is
// refused, so the checked path is the executed path.
function statCommand(path) {
  var prefixes = pathPrefixes(path)
  if (!prefixes) return null
  return [STAT, "-c", "%u %a %F %n", "/proc/self/status"].concat(prefixes)
}

function parseStatLine(line) {
  var match = /^(\d+) ([0-7]+) ([a-z ]+?) (\/.*)$/.exec(line)
  if (!match) return null
  return { uid: Number(match[1]), mode: parseInt(match[2], 8), type: match[3], name: match[4] }
}

// The verdict on `path` given stat's stdout. Every directory on the way must
// be owned by root or by us and writable by neither group nor others; the
// file itself the same, regular, and executable. Anything else, including a
// component that is a symlink, is a reason to refuse, and the reason names
// the component so the user can see what to fix.
function checkBinary(path, statOutput) {
  var prefixes = pathPrefixes(path)
  if (!prefixes) return { ok: false, reason: String(path) + ": " + pathSyntaxProblem(path) }
  var lines = String(statOutput || "").split("\n").filter(function(l) { return l !== "" })
  if (lines.length === 0) return { ok: false, reason: "could not inspect " + path }
  var self = parseStatLine(lines[0])
  if (!self || self.name !== "/proc/self/status") return { ok: false, reason: "could not determine own uid" }
  var uid = self.uid
  var byName = {}
  for (var i = 1; i < lines.length; i++) {
    var entry = parseStatLine(lines[i])
    if (entry) byName[entry.name] = entry
  }
  for (var j = 0; j < prefixes.length; j++) {
    var name = prefixes[j]
    var info = byName[name]
    var last = j === prefixes.length - 1
    if (!info) return { ok: false, reason: name + " does not exist" }
    var want = last ? "regular file" : "directory"
    if (info.type !== want) return { ok: false, reason: name + " is a " + info.type + ", not a " + want }
    if (info.uid !== 0 && info.uid !== uid) return { ok: false, reason: name + " is owned by uid " + info.uid + ", not root or you" }
    if ((info.mode & 0o022) !== 0) return { ok: false, reason: name + " is writable by group or others" }
    if (last && (info.mode & 0o111) === 0) return { ok: false, reason: name + " is not executable" }
  }
  return { ok: true, reason: "" }
}

// Append `data` to `current` under `cap`. Once the cap is crossed the caller
// kills the process and never parses `text`; what is kept is only so an
// error message can quote the start of it.
function appendCapped(current, data, cap) {
  var text = String(current || "")
  var chunk = String(data || "")
  if (text.length + chunk.length <= cap) return { text: text + chunk, overflow: false }
  return { text: text + chunk.slice(0, Math.max(0, cap - text.length)), overflow: true }
}
