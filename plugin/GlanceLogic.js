.pragma library

// Pure functions over the `glancectl status --json` contract (schemaVersion 1)
// and the `authenticate --json` / `arm --json` results. No QML in here, so the
// node tests in tests/ exercise exactly what the panel renders.

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
  var ctl = String(glancectl || "glancectl")
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
                  ["systemctl", "--user", "enable", "--now", "glanced"], false, false)
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
    return ["omarchy-launch-terminal"].concat(action.command)
  }
  if (action.detached) {
    // A new session leader outlives this plugin: reloading the shell must not
    // kill an enrollment half way through the sweep.
    return ["setsid", "--fork"].concat(action.command)
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
