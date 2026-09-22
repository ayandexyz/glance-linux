.pragma library

// Pure functions over the `glancectl status --json` contract (schemaVersion 1)
// and the `authenticate --json` / `arm --json` results. No QML in here, so the
// node tests in tests/ exercise exactly what the panel renders.

// Every executable the plugin runs, by absolute path. Nothing is resolved
// through PATH: a shell plugin runs as the user, and a shadowed `systemctl`
// or `glancectl` earlier on an inherited PATH would otherwise run in place of
// the real one, on the way to the interactive PAM setup step.
var DEFAULT_GLANCECTL = "/usr/bin/glancectl"
// Where `pipx install glanced` puts the binary. pipx also drops a symlink in
// ~/.local/bin, which the ownership rules refuse, so the venv file itself is
// the only usable one.
var PIPX_GLANCECTL_SUFFIX = "/.local/share/pipx/venvs/glanced/bin/glancectl"
var SYSTEMCTL = "/usr/bin/systemctl"
var SETSID = "/usr/bin/setsid"
var LAUNCH_TERMINAL = "/usr/bin/omarchy-launch-terminal"
var STAT = "/usr/bin/stat"
var TIMEOUT = "/usr/bin/timeout"
var PYTHON3 = "/usr/bin/python3"

// The PATH handed to children. glancectl's own subprocesses (sudo, make,
// omarchy-apply-lock during setup-pam) resolve through this and nothing else.
var SAFE_PATH = "/usr/bin:/usr/share/omarchy/bin"

// Live ceilings on what a child may write before it is killed unread. A
// status document is a few hundred bytes; a scan result is one line. The
// timers bound how long a process may run, these bound how much it may say.
var STDOUT_CAP = 64 * 1024
var STDERR_CAP = 16 * 1024

// Wall-clock ceilings, in seconds. A scan can legitimately take the daemon's
// full scan timeout plus model warm-up on the first run, hence the wide one
// for actions; a launcher only has to hand its window off to a new session.
var CHECK_DEADLINE_SEC = 5
var STATUS_DEADLINE_SEC = 10
var ACTION_DEADLINE_SEC = 45
var LAUNCH_DEADLINE_SEC = 30
// How long the tree gets to die of SIGTERM before it is SIGKILLed.
var KILL_GRACE_SEC = 5

// timeout(1) exit codes: the deadline was hit, and the deadline was hit and
// SIGKILL was needed. Either way nothing the child wrote may be trusted.
var DEADLINE_EXIT = 124
var DEADLINE_KILLED_EXIT = 128 + 9

// Every child runs under timeout(1), which is what makes a deadline mean the
// whole process tree rather than just the process we spawned: without
// --foreground it puts the command in a new process group of its own and
// signals *the group*, so a glancectl that shelled out to sudo, make or
// omarchy-apply-lock cannot outlive the boundary by being a grandchild. It
// also forwards a SIGTERM of its own to that group, which is how the output
// caps below stop a tree rather than a process, and it waits for the child,
// so timeout(1) exiting means the child was reaped. --kill-after turns
// SIGTERM into SIGKILL for anything in the group still standing.
//
// A Quickshell Timer cannot do any of this: it can only flip `running`, which
// reaches one pid.
function bounded(argv, seconds) {
  if (!argv || argv.length === 0) return null
  return [TIMEOUT, "--kill-after=" + KILL_GRACE_SEC, "--signal=TERM",
          String(seconds)].concat(argv)
}

// True when an exit code means timeout(1) enforced the deadline rather than
// the command finishing on its own terms.
function deadlineHit(exitCode) {
  return Number(exitCode) === DEADLINE_EXIT || Number(exitCode) === DEADLINE_KILLED_EXIT
}

// --- executing the object that was checked, not the name it had -------------

// A pathname is resolved afresh by every exec, so a check over a pathname
// cannot say which object the exec went on to open. This runs instead of
// glancectl, and closes that: it opens the path *once*, fstats that
// descriptor, refuses unless the descriptor is the object the stat(1) check
// accepted, and then execs the descriptor itself. There is no second
// resolution to race — open, verify and exec all name the same open file
// description — and if the path was replaced in the interval, the identity
// will not match and nothing is executed.
//
// The interpreter is /usr/bin/python3: root-owned, absolute, and already a
// hard dependency of glanced, which is a Python entry point. The program is
// passed on argv rather than shipped as a file next to the plugin, because
// the plugin directory is user-owned and a file there would be exactly the
// kind of object this is meant to stop being swapped.
var EXEC_VERIFIER = [
  "import os,sys",
  "want=sys.argv[1].split(':');path=sys.argv[2]",
  "try:",
  "    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)",
  "except OSError as e:",
  "    sys.exit('glance: cannot open %s: %s' % (path,e))",
  "st=os.fstat(fd)",
  "got=[str(st.st_dev),str(st.st_ino),str(st.st_size),str(int(st.st_mtime)),str(st.st_uid),format(st.st_mode & 0o7777,'o')]",
  "if got!=want:",
  "    sys.exit('glance: %s is not the object that was checked (%s, wanted %s)' % (path,':'.join(got),':'.join(want)))",
  "os.set_inheritable(fd,True)",
  "os.execve(fd,[path]+sys.argv[3:],os.environ)"
].join("\n")

// The identity, in the order the verifier compares it.
function identityToken(identity) {
  if (!identity) return ""
  return [identity.dev, identity.ino, identity.size, identity.mtime,
          String(identity.uid), identity.mode.toString(8)].join(":")
}

// argv for running `argv` (argv[0] being the checked glancectl) as the object
// `identity` describes. Null when there is no identity to hold it to, so a
// caller can never fall back to running the bare pathname.
function verifiedCommand(argv, identity) {
  if (!argv || argv.length === 0 || !identity) return null
  return [PYTHON3, "-c", EXEC_VERIFIER, identityToken(identity)].concat(argv)
}

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
    // Whether that install can open the enrollment window. Absent on a
    // glancectl older than this field, where --gui was the only path and
    // assuming it is what that version did.
    gui: parsed.gui === undefined ? true : parsed.gui === true,
    remembered: parsed.remembered === true,
    mode: String(parsed.mode || ""),
    camera: String(parsed.camera || ""),
    models: parsed.models && typeof parsed.models === "object" ? parsed.models : {},
    pam: parsed.pam && typeof parsed.pam === "object" ? parsed.pam : null,
    lock: parsed.lock && typeof parsed.lock === "object" ? parsed.lock : null,
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
  error: "Error",
  locked_out: "Locked out"
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
  if (key === "spoof_denied" || key === "error" || key === "locked_out") return "bad"
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
    // The window is the good version of this: a camera preview guiding the
    // sweep. It needs PySide6, which `glanced[runtime]` does not carry, and a
    // button has no terminal to fall back to for the passphrase -- so when the
    // window is unavailable the enrollment is run in a terminal instead of
    // offering a button that can only raise ImportError.
    if (status.gui === false) {
      return action("enroll", "No face enrolled yet. The enrollment window needs "
                    + "the 'gui' extra, so this opens a terminal instead:",
                    "Enroll", "\udb84\udc7b",
                    [ctl, "enroll", "--name", String(user || "$USER"), "--remember"],
                    true, true)
    }
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
  if (status.lock && status.lock.available === true && status.lock.patched !== true) {
    return action("setup-lock", "Face unlock works, but the lock screen has no indicator "
                  + "(an Omarchy update resets it). This opens a terminal and asks for your password:",
                  "Add lock indicator", "\udb80\udcf8", [ctl, "setup-lock"], true, true)
  }
  return null
}

// How an action's argv has to be wrapped to actually run it. Three shapes,
// because three kinds of command: one that needs a terminal to ask for a
// password or show a progress bar, one that opens its own window and must
// survive a shell reload mid-sweep, and one that just runs.
// `identity` is the glancectl the check accepted. The step's own command is
// rewritten to go through the verifier above, so the setup steps — setup-pam,
// the one that leads to an interactive sudo, included — exec the checked
// object rather than re-resolving its name inside a terminal. A step that
// does not run glancectl at all (starting the daemon is systemd's business)
// is left as it is.
function launchCommand(action, identity) {
  if (!action || !action.command) return null
  var command = action.command
  if (command[0] !== SYSTEMCTL) {
    command = verifiedCommand(command, identity)
    if (!command) return null
  }
  if (action.terminal) {
    // Omarchy's launcher already setsids into the user's chosen terminal.
    return [LAUNCH_TERMINAL].concat(command)
  }
  if (action.detached) {
    // A new session leader outlives this plugin: reloading the shell must not
    // kill an enrollment half way through the sweep.
    return [SETSID, "--fork"].concat(command)
  }
  return command.slice()
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

// The lock screen indicator: absent on a stock lock screen, and reverted by
// every Omarchy update until the post-update hook puts it back.
function indicatorLabel(status) {
  if (!status || !status.lock || status.lock.available !== true) return ""
  if (status.lock.patched !== true) return "Indicator missing"
  return status.lock.hook === true ? "Indicator on, survives updates" : "Indicator on, no update hook"
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
// The places an installed glancectl actually is, tried in order when the
// setting is empty: the distribution package first, then the pipx venv. This
// is a fixed list of two known install locations, not a search -- no PATH
// lookup, no globbing, nothing walking $HOME -- and each one still has to pass
// the ownership rules before anything runs through it. Without the second, a
// `pipx install glanced` leaves every button in the panel broken until the
// user finds the setting, which is the common install now that there is no
// AUR package.
function defaultGlancectlCandidates(home) {
  var candidates = [DEFAULT_GLANCECTL]
  var base = String(home === undefined || home === null ? "" : home).trim()
  // An empty or relative HOME would make a path the verifier refuses anyway;
  // leaving it out keeps the refusal about the package path the user can see.
  if (base.charAt(0) === "/") {
    candidates.push(base.replace(/\/+$/, "") + PIPX_GLANCECTL_SUFFIX)
  }
  return candidates
}

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
//
// Device and inode, size and mtime come back alongside the ownership bits
// because a pathname is not an identity: they are what `sameIdentity` below
// compares, so a path that is re-checked before every run is also known to
// still be the same object it was the last time it ran.
function statCommand(path) {
  var prefixes = pathPrefixes(path)
  if (!prefixes) return null
  return [STAT, "-c", "%u %a %d %i %s %Y %F %n", "/proc/self/status"].concat(prefixes)
}

// %F is the only field that contains spaces ("regular empty file"), so it sits
// last before the name. The numbers that make up an identity are kept as the
// strings stat printed: an inode is 64-bit and a JS number is not.
function parseStatLine(line) {
  var match = /^(\d+) ([0-7]+) (\d+) (\d+) (\d+) (\d+) ([a-z ]+?) (\/.*)$/.exec(line)
  if (!match) return null
  return {
    uid: Number(match[1]),
    mode: parseInt(match[2], 8),
    dev: match[3],
    ino: match[4],
    size: match[5],
    mtime: match[6],
    type: match[7],
    name: match[8]
  }
}

// What is retained between a check and the run it vouches for.
function identityOf(info) {
  return { dev: info.dev, ino: info.ino, size: info.size, mtime: info.mtime,
           uid: info.uid, mode: info.mode }
}

function sameIdentity(a, b) {
  if (!a || !b) return false
  return a.dev === b.dev && a.ino === b.ino && a.size === b.size
      && a.mtime === b.mtime && a.uid === b.uid && a.mode === b.mode
}

function describeIdentity(identity) {
  return identity ? "device " + identity.dev + " inode " + identity.ino : "unknown"
}

// The verdict on `path` given stat's stdout. Every directory on the way must
// be owned by root or by us and writable by neither group nor others; the
// file itself the same, regular, and executable. Anything else, including a
// component that is a symlink, is a reason to refuse, and the reason names
// the component so the user can see what to fix.
//
// `previous` is the identity the last accepted check retained, or null on the
// first one. Because this runs immediately before every execution, a `changed`
// verdict means the object behind the pathname was replaced since the command
// that last ran through it — a package upgrade, ordinarily. The identity is
// adopted, but nothing already queued is run against it: the caller is told,
// and the next check (which will match) is what releases the command. So no
// invocation ever reaches a file that was not stat'd, as that file, moments
// earlier.
//
// The ownership rules are what make "moments earlier" enough. Every component
// is writable only by root or by the uid the plugin runs as, and that uid is
// the user, who can already run whatever they like as themselves — so no other
// principal can swap the object inside the window, and there is no privilege
// boundary for the user to cross by swapping it themselves.
function checkBinary(path, statOutput, previous) {
  var prefixes = pathPrefixes(path)
  if (!prefixes) return { ok: false, reason: String(path) + ": " + pathSyntaxProblem(path), identity: null, changed: false }
  var lines = String(statOutput || "").split("\n").filter(function(l) { return l !== "" })
  if (lines.length === 0) return { ok: false, reason: "could not inspect " + path, identity: null, changed: false }
  var self = parseStatLine(lines[0])
  if (!self || self.name !== "/proc/self/status") return { ok: false, reason: "could not determine own uid", identity: null, changed: false }
  var uid = self.uid
  var byName = {}
  for (var i = 1; i < lines.length; i++) {
    var entry = parseStatLine(lines[i])
    if (entry) byName[entry.name] = entry
  }
  function refuse(reason) { return { ok: false, reason: reason, identity: null, changed: false } }
  for (var j = 0; j < prefixes.length; j++) {
    var name = prefixes[j]
    var info = byName[name]
    var last = j === prefixes.length - 1
    if (!info) return refuse(name + " does not exist")
    var want = last ? "regular file" : "directory"
    if (info.type !== want) return refuse(name + " is a " + info.type + ", not a " + want)
    if (info.uid !== 0 && info.uid !== uid) return refuse(name + " is owned by uid " + info.uid + ", not root or you")
    if ((info.mode & 0o022) !== 0) return refuse(name + " is writable by group or others")
    if (last && (info.mode & 0o111) === 0) return refuse(name + " is not executable")
  }
  var identity = identityOf(byName[prefixes[prefixes.length - 1]])
  return {
    ok: true,
    reason: "",
    identity: identity,
    changed: previous ? !sameIdentity(previous, identity) : false
  }
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
