#!/usr/bin/env node
// Unit tests for GlanceLogic.js. The file is a QML `.pragma library`, so it
// is loaded by stripping that line and evaluating it as plain script.
"use strict"
const fs = require("fs")
const path = require("path")
const vm = require("vm")
const assert = require("assert")

const source = fs.readFileSync(path.join(__dirname, "..", "GlanceLogic.js"), "utf8")
  .replace(/^\.pragma library\s*$/m, "")
const G = vm.runInNewContext(source + "\n;({ parseStatus, stateLabel, nextStep, outcomeLabel, outcomeSeverity, missingModels, identityLine, elapsed, lastScanText, lastScanReason, parseActionResult, clamp, lockLabel, nextAction, launchCommand, pathSyntaxProblem, pathPrefixes, statCommand, checkBinary, appendCapped, childEnvironment, bounded, deadlineHit, sameIdentity, describeIdentity, SYSTEMCTL, SETSID, LAUNCH_TERMINAL, STAT, TIMEOUT, DEFAULT_GLANCECTL, SAFE_PATH, CHECK_DEADLINE_SEC, STATUS_DEADLINE_SEC, ACTION_DEADLINE_SEC, LAUNCH_DEADLINE_SEC, KILL_GRACE_SEC })")
// Objects built inside the vm have a foreign Object prototype, which trips
// deepStrictEqual; compare by value instead.
function assertSame(actual, expected, message) {
  assert.strictEqual(JSON.stringify(actual, Object.keys(actual).sort()), JSON.stringify(expected, Object.keys(expected).sort()), message)
}

const tests = []
function test(name, fn) { tests.push([name, fn]) }

const online = {
  schemaVersion: 1, reachable: true, armed: true, mode: "light", scanning: false, enrolled: true,
  remembered: true, camera: "/dev/video0", models: { landmarker: true, arcface: true },
  identities: [{ name: "ayan", enabled: true, captures: 5 }, { name: "glasses", enabled: false, captures: 3 }],
  pam: { module: true, hyprlock: false, shellPassword: true, shellFingerprint: false, wired: true },
  lastScan: { outcome: "unlocked", identity: "ayan", similarity: 0.71, reason: null, at: 1000, duration: 1.4 }
}

test("parses a schema-1 status and rejects others", () => {
  const status = G.parseStatus(JSON.stringify(online))
  assert.strictEqual(status.armed, true)
  assert.strictEqual(status.identities.length, 2)
  assert.strictEqual(G.parseStatus(JSON.stringify({ ...online, schemaVersion: 2 })), null)
  assert.strictEqual(G.parseStatus("not json"), null)
  assert.strictEqual(G.parseStatus("[]"), null)
})

test("state label ranks the most urgent condition first", () => {
  assert.strictEqual(G.stateLabel(null), "Checking...")
  assert.strictEqual(G.stateLabel(G.parseStatus(JSON.stringify({ ...online, reachable: false }))), "Daemon offline")
  assert.strictEqual(G.stateLabel(G.parseStatus(JSON.stringify({ ...online, scanning: true }))), "Scanning...")
  assert.strictEqual(G.stateLabel(G.parseStatus(JSON.stringify({ ...online, models: { landmarker: true } }))), "Models missing")
  assert.strictEqual(G.stateLabel(G.parseStatus(JSON.stringify({ ...online, enrolled: false }))), "Not enrolled")
  assert.strictEqual(G.stateLabel(G.parseStatus(JSON.stringify({ ...online, armed: false }))), "Disarmed")
  assert.strictEqual(G.stateLabel(G.parseStatus(JSON.stringify(online))), "Armed · light liveness")
  assert.strictEqual(G.stateLabel(G.parseStatus(JSON.stringify({ ...online, mode: "heavy" }))), "Armed · heavy liveness")
})

const status = overrides => G.parseStatus(JSON.stringify({ ...online, ...overrides }))
const action = (overrides, ctl, user) => G.nextAction(status(overrides), ctl || "glancectl", user || "$USER")

test("next step is the single command that unblocks the user", () => {
  assert.strictEqual(G.nextStep(status({ reachable: false })), "/usr/bin/systemctl --user enable --now glanced")
  assert.strictEqual(G.nextStep(status({ models: {} })), "glancectl fetch-model")
  assert.strictEqual(G.nextStep(status({ enrolled: false })), "glancectl enroll --name $USER --gui --remember")
  assert.strictEqual(G.nextStep(status({ armed: false })), "glancectl arm")
  assert.strictEqual(G.nextStep(status({})), "")
  assert.strictEqual(G.nextStep(status({ pam: { module: true, wired: false } })), "glancectl setup-pam")
  assert.strictEqual(G.nextStep(status({ pam: undefined })), "")
})

test("next action ranks the setup steps and stays runnable", () => {
  assert.strictEqual(G.nextAction(null, "glancectl", "ayan"), null)
  assert.strictEqual(action({}), null, "fully wired needs nothing")

  const start = action({ reachable: false })
  assert.strictEqual(start.key, "start")
  assertSame(start.command, ["/usr/bin/systemctl", "--user", "enable", "--now", "glanced"])
  // enable, not just start: the daemon has to come back after a reboot too.
  assert.ok(start.command.includes("--now") && start.command.includes("enable"))
  assert.strictEqual(start.terminal, false)
  assert.strictEqual(start.detached, false)

  // A long download deserves the progress bar it prints, so: a terminal.
  const models = action({ models: { landmarker: true } })
  assert.strictEqual(models.key, "fetch-model")
  assert.strictEqual(models.terminal, true)
  assert.strictEqual(models.detached, true)

  // Enrolling opens its own window and must outlive a shell reload.
  const enroll = action({ enrolled: false })
  assert.strictEqual(enroll.key, "enroll")
  assert.strictEqual(enroll.terminal, false)
  assert.strictEqual(enroll.detached, true)
  assert.ok(enroll.command.includes("--gui"), "the sweep is guided in a window")
  assert.ok(enroll.command.includes("--remember"), "so the daemon arms itself at login")

  // sudo has to be typed somewhere a user can see it.
  const pam = action({ pam: { module: true, wired: false } })
  assert.strictEqual(pam.key, "setup-pam")
  assert.strictEqual(pam.terminal, true)
})

test("launch wrapping matches the shape of each step", () => {
  assert.strictEqual(G.launchCommand(null), null)
  assert.strictEqual(G.launchCommand(action({ armed: false })), null, "arm is inline, never spawned")

  // sudo needs somewhere to be typed.
  assertSame(G.launchCommand(action({ pam: { module: true, wired: false } })),
    ["/usr/bin/omarchy-launch-terminal", "glancectl", "setup-pam"])

  // The download prints progress; give it a terminal to print into.
  assertSame(G.launchCommand(action({ models: {} })),
    ["/usr/bin/omarchy-launch-terminal", "glancectl", "fetch-model"])

  // Detached, so reloading the shell mid-sweep does not kill the window.
  assertSame(G.launchCommand(action({ enrolled: false }, "/opt/glancectl", "ayan")),
    ["/usr/bin/setsid", "--fork", "/opt/glancectl", "enroll", "--name", "ayan", "--gui", "--remember"])

  // Starting the daemon is quick and silent: no wrapper at all.
  assertSame(G.launchCommand(action({ reachable: false })),
    ["/usr/bin/systemctl", "--user", "enable", "--now", "glanced"])
})

test("arming has no command because the passphrase must not reach argv", () => {
  const arm = action({ armed: false })
  assert.strictEqual(arm.key, "arm")
  assert.strictEqual(arm.command, null)
  assert.strictEqual(arm.hint, "glancectl arm")
})

test("next action uses the glancectl the backend resolved, not the name", () => {
  const ctl = "/home/ayan/glance-linux/.venv/bin/glancectl"
  const enroll = action({ enrolled: false }, ctl, "ayan")
  assert.strictEqual(enroll.command[0], ctl)
  assertSame(enroll.command.slice(1), ["enroll", "--name", "ayan", "--gui", "--remember"])
  // The command shown under the button is the command the button runs.
  assert.strictEqual(enroll.hint, enroll.command.join(" "))
  // Starting the daemon is systemd's job, never glancectl's.
  assert.strictEqual(action({ reachable: false }, ctl).command[0], "/usr/bin/systemctl")
})

test("lock label reflects which PAM stack carries the module", () => {
  assert.strictEqual(G.lockLabel(G.parseStatus(JSON.stringify(online))), "On Enter · shell lock")
  assert.strictEqual(G.lockLabel(G.parseStatus(JSON.stringify({ ...online, pam: { module: true, shellFingerprint: true, wired: true } }))), "Hands-free at lock")
  assert.strictEqual(G.lockLabel(G.parseStatus(JSON.stringify({ ...online, pam: { module: true, hyprlock: true, shellPassword: true, wired: true } }))), "On Enter · shell lock, hyprlock")
  assert.strictEqual(G.lockLabel(G.parseStatus(JSON.stringify({ ...online, pam: { module: false, wired: false } }))), "Module not installed")
  assert.strictEqual(G.lockLabel(G.parseStatus(JSON.stringify({ ...online, pam: { module: true, wired: false } }))), "Not wired")
  assert.strictEqual(G.lockLabel(G.parseStatus(JSON.stringify({ ...online, pam: undefined }))), "")
})

test("outcomes map to labels and severities", () => {
  assert.strictEqual(G.outcomeLabel("spoof_denied"), "Spoof denied")
  assert.strictEqual(G.outcomeLabel("something_new"), "something_new")
  assert.strictEqual(G.outcomeLabel(""), "")
  assert.strictEqual(G.outcomeSeverity("unlocked"), "good")
  assert.strictEqual(G.outcomeSeverity("spoof_denied"), "bad")
  assert.strictEqual(G.outcomeSeverity("error"), "bad")
  assert.strictEqual(G.outcomeSeverity("no_match"), "neutral")
})

test("last scan text names the identity and the age", () => {
  const status = G.parseStatus(JSON.stringify(online))
  assert.strictEqual(G.lastScanText(status, 1000 * 1000 + 30 * 1000), "Unlocked as ayan · 30s ago")
  assert.strictEqual(G.lastScanReason(status), "")
  const denied = G.parseStatus(JSON.stringify({ ...online, lastScan: { outcome: "spoof_denied", reason: "Gloss/glare", at: 1000 } }))
  assert.strictEqual(G.lastScanText(denied, 1000 * 1000 + 2000), "Spoof denied · just now")
  assert.strictEqual(G.lastScanReason(denied), "Gloss/glare")
  assert.strictEqual(G.lastScanText(G.parseStatus(JSON.stringify({ ...online, lastScan: null })), 0), "")
})

test("identity lines", () => {
  assert.strictEqual(G.identityLine({ name: "ayan", enabled: true, captures: 5 }), "ayan · 5 captures")
  assert.strictEqual(G.identityLine({ name: "one", enabled: true, captures: 1 }), "one · 1 capture")
  assert.strictEqual(G.identityLine({ name: "old", enabled: false, captures: 0 }), "old · disabled")
})

test("elapsed", () => {
  assert.strictEqual(G.elapsed(0, 3000), "just now")
  assert.strictEqual(G.elapsed(0, 42000), "42s ago")
  assert.strictEqual(G.elapsed(0, 5 * 60000), "5m ago")
  assert.strictEqual(G.elapsed(0, 3 * 3600000), "3h ago")
  assert.strictEqual(G.elapsed(0, 2 * 86400000), "2d ago")
})

test("action results: scan verdicts, plain successes, and failures", () => {
  const scan = G.parseActionResult('{"outcome": "no_match", "identity": null, "reason": "No enrolled face matched."}', "", 1)
  assertSame(scan, { ok: false, outcome: "no_match", identity: "", reason: "No enrolled face matched.", error: "" })
  const unlocked = G.parseActionResult('{"outcome": "unlocked", "identity": "ayan", "reason": null}', "", 0)
  assert.strictEqual(unlocked.ok, true)
  assert.strictEqual(unlocked.identity, "ayan")
  const armed = G.parseActionResult('{"schemaVersion": 1, "armed": true}', "", 0)
  assert.strictEqual(armed.ok, true)
  const wrong = G.parseActionResult("", "wrong passphrase\n", 1)
  assertSame(wrong, { ok: false, outcome: "", identity: "", reason: "", error: "wrong passphrase" })
  assert.strictEqual(G.parseActionResult("", "", 3).error, "glancectl exited with status 3")
})

test("clamp tolerates junk", () => {
  assert.strictEqual(G.clamp("x", 5, 600), 5)
  assert.strictEqual(G.clamp(9999, 5, 600), 600)
  assert.strictEqual(G.clamp(30, 5, 600), 30)
})

test("every fixed tool is an absolute path and nothing is left to PATH", () => {
  for (const tool of [G.SYSTEMCTL, G.SETSID, G.LAUNCH_TERMINAL, G.STAT, G.DEFAULT_GLANCECTL]) {
    assert.ok(tool.startsWith("/usr/bin/"), tool + " is not under /usr/bin")
  }
  // With no glancectl given, the packaged one is used — not a bare name.
  assert.strictEqual(G.nextAction(status({ enrolled: false }), "", "ayan").command[0], "/usr/bin/glancectl")
  // Every argv the plugin can produce starts with an absolute path.
  for (const overrides of [{ reachable: false }, { models: {} }, { enrolled: false }, { pam: { module: true, wired: false } }]) {
    const argv = G.launchCommand(G.nextAction(status(overrides), "/usr/bin/glancectl", "ayan"))
    assert.ok(argv[0].startsWith("/"), JSON.stringify(argv) + " resolves through PATH")
  }
})

test("path syntax: absolute, no relative or empty components, no control characters", () => {
  assert.strictEqual(G.pathSyntaxProblem("/usr/bin/glancectl"), "")
  assert.strictEqual(G.pathSyntaxProblem(""), "no path given")
  assert.strictEqual(G.pathSyntaxProblem("glancectl"), "must be an absolute path")
  assert.strictEqual(G.pathSyntaxProblem("~/glancectl"), "must be an absolute path")
  assert.strictEqual(G.pathSyntaxProblem("/usr/../bin/glancectl"), "contains a relative path component")
  assert.strictEqual(G.pathSyntaxProblem("/usr//bin/glancectl"), "contains an empty path component")
  assert.strictEqual(G.pathSyntaxProblem("/usr/bin/glancectl/"), "contains an empty path component")
  assert.strictEqual(G.pathSyntaxProblem("/usr/bin/glance\nctl"), "contains control characters")
  assertSame(G.pathPrefixes("/usr/bin/glancectl"), ["/", "/usr", "/usr/bin", "/usr/bin/glancectl"])
  assert.strictEqual(G.pathPrefixes("bin/glancectl"), null)
  assert.strictEqual(G.statCommand("relative"), null)
})

test("the stat command reports every component and our own uid, without following symlinks", () => {
  const argv = G.statCommand("/home/ayan/.venv/bin/glancectl")
  assert.strictEqual(argv[0], "/usr/bin/stat")
  assert.ok(!argv.includes("-L"), "must not dereference symlinks")
  assertSame(argv.slice(3), ["/proc/self/status", "/", "/home", "/home/ayan", "/home/ayan/.venv", "/home/ayan/.venv/bin", "/home/ayan/.venv/bin/glancectl"])
  // Identity, not just permission: a pathname says nothing about which object
  // answers to it, so device, inode, size and mtime come back too.
  const format = argv[argv.indexOf("-c") + 1]
  for (const field of ["%u", "%a", "%d", "%i", "%s", "%Y", "%F", "%n"]) {
    assert.ok(format.includes(field), "stat format is missing " + field)
  }
})

// Test lines are written "uid mode type name" and expanded here, so a change
// of identity is spelled out only where a test is about one.
let nextInode = 100
function statLine(line, identity) {
  const match = /^(\d+) ([0-7]+) ([a-z ]+?) (\/.*)$/.exec(line)
  const id = Object.assign({ dev: 2049, ino: nextInode++, size: 4096, mtime: 1700000000 }, identity || {})
  return [match[1], match[2], id.dev, id.ino, id.size, id.mtime, match[3], match[4]].join(" ")
}
const statLines = (lines, identity) => statLine("1000 444 regular empty file /proc/self/status") + "\n"
  + lines.map((line, index) => statLine(line, index === lines.length - 1 ? identity : null)).join("\n") + "\n"

test("a binary is accepted only when every component is owned by root or us and writable by nobody else", () => {
  const ok = G.checkBinary("/usr/bin/glancectl", statLines([
    "0 755 directory /", "0 755 directory /usr", "0 755 directory /usr/bin", "0 755 regular file /usr/bin/glancectl"]))
  assert.strictEqual(ok.ok, true, ok.reason)
  assert.strictEqual(ok.changed, false, "nothing to compare against on the first check")
  assert.strictEqual(ok.identity.uid, 0)
  assert.strictEqual(ok.identity.mode, 0o755)

  const venv = G.checkBinary("/home/ayan/venv/bin/glancectl", statLines([
    "0 755 directory /", "0 755 directory /home", "1000 700 directory /home/ayan",
    "1000 755 directory /home/ayan/venv", "1000 755 directory /home/ayan/venv/bin", "1000 755 regular file /home/ayan/venv/bin/glancectl"]))
  assert.strictEqual(venv.ok, true, venv.reason)

  // /tmp is world-writable: anyone could have put that file there.
  const tmp = G.checkBinary("/tmp/glancectl", statLines(["0 755 directory /", "0 1777 directory /tmp", "1000 755 regular file /tmp/glancectl"]))
  assert.strictEqual(tmp.ok, false)
  assert.strictEqual(tmp.reason, "/tmp is writable by group or others")

  // Another user's file, even if executable.
  const other = G.checkBinary("/opt/glancectl", statLines(["0 755 directory /", "0 755 directory /opt", "1001 755 regular file /opt/glancectl"]))
  assert.strictEqual(other.reason, "/opt/glancectl is owned by uid 1001, not root or you")

  // A group-writable directory on the way.
  const groupw = G.checkBinary("/srv/tools/glancectl", statLines(["0 755 directory /", "0 775 directory /srv", "0 755 directory /srv/tools", "0 755 regular file /srv/tools/glancectl"]))
  assert.strictEqual(groupw.reason, "/srv is writable by group or others")

  // Symlinks are named as such, whether a directory (/bin on merged-usr) or the file.
  const bin = G.checkBinary("/bin/glancectl", statLines(["0 755 directory /", "0 777 symbolic link /bin", "0 755 regular file /bin/glancectl"]))
  assert.strictEqual(bin.reason, "/bin is a symbolic link, not a directory")
  const link = G.checkBinary("/usr/local/bin/glancectl", statLines(["0 755 directory /", "0 755 directory /usr", "0 755 directory /usr/local", "0 755 directory /usr/local/bin", "0 777 symbolic link /usr/local/bin/glancectl"]))
  assert.strictEqual(link.reason, "/usr/local/bin/glancectl is a symbolic link, not a regular file")

  // Not executable, or not there at all: stat prints nothing for a missing component.
  const noexec = G.checkBinary("/usr/bin/glancectl", statLines(["0 755 directory /", "0 755 directory /usr", "0 755 directory /usr/bin", "0 644 regular file /usr/bin/glancectl"]))
  assert.strictEqual(noexec.reason, "/usr/bin/glancectl is not executable")
  const missing = G.checkBinary("/usr/bin/glancectl", statLines(["0 755 directory /", "0 755 directory /usr", "0 755 directory /usr/bin"]))
  assert.strictEqual(missing.reason, "/usr/bin/glancectl does not exist")

  // Garbage from stat, or no uid line, is a refusal — never a pass.
  assert.strictEqual(G.checkBinary("/usr/bin/glancectl", "").ok, false)
  assert.strictEqual(G.checkBinary("/usr/bin/glancectl", "0 755 directory /\n").ok, false)
  assert.strictEqual(G.checkBinary("relative", statLines([])).ok, false)
})

test("output is capped live and the overflow is reported, not parsed", () => {
  assertSame(G.appendCapped("", "abc", 10), { text: "abc", overflow: false })
  assertSame(G.appendCapped("abc", "defgh", 8), { text: "abcdefgh", overflow: false })
  assertSame(G.appendCapped("abc", "defghi", 8), { text: "abcdefgh", overflow: true })
  assertSame(G.appendCapped("abcdefgh", "i", 8), { text: "abcdefgh", overflow: true })
})

test("the child environment pins PATH and drops interpreter and loader overrides", () => {
  const env = G.childEnvironment()
  assert.strictEqual(env.PATH, "/usr/bin:/usr/share/omarchy/bin")
  for (const name of ["PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT"]) {
    assert.strictEqual(env[name], null, name + " must be unset")
  }
  assert.strictEqual(env.PYTHONNOUSERSITE, "1")
})

test("a bounded command hands the deadline to timeout(1), which owns the whole group", () => {
  const argv = G.bounded(["/usr/bin/glancectl", "setup-pam"], G.ACTION_DEADLINE_SEC)
  assert.strictEqual(argv[0], "/usr/bin/timeout")
  // --foreground would leave the command in our own process group, and the
  // signal would then reach one process instead of the tree.
  assert.ok(!argv.includes("--foreground"), "the command must get a process group of its own")
  assertSame(argv, ["/usr/bin/timeout", "--kill-after=" + G.KILL_GRACE_SEC, "--signal=TERM",
                    String(G.ACTION_DEADLINE_SEC), "/usr/bin/glancectl", "setup-pam"])
  assert.strictEqual(G.bounded([], 5), null)
  assert.strictEqual(G.bounded(null, 5), null)

  // Every deadline is a positive number of seconds, and the wide one is the
  // action deadline: a first scan warms models up.
  for (const seconds of [G.CHECK_DEADLINE_SEC, G.STATUS_DEADLINE_SEC, G.ACTION_DEADLINE_SEC, G.LAUNCH_DEADLINE_SEC, G.KILL_GRACE_SEC]) {
    assert.ok(Number.isFinite(seconds) && seconds > 0, "deadline must be a positive number of seconds")
  }
  assert.ok(G.ACTION_DEADLINE_SEC > G.STATUS_DEADLINE_SEC)

  // 124 is timeout(1) giving up; 137 is the group having needed SIGKILL.
  assert.strictEqual(G.deadlineHit(124), true)
  assert.strictEqual(G.deadlineHit(137), true)
  assert.strictEqual(G.deadlineHit(0), false)
  assert.strictEqual(G.deadlineHit(1), false)
})

test("the identity behind the path is retained, and a swap is caught before anything runs", () => {
  const lines = ["0 755 directory /", "0 755 directory /usr", "0 755 directory /usr/bin", "0 755 regular file /usr/bin/glancectl"]
  const first = G.checkBinary("/usr/bin/glancectl", statLines(lines, { ino: 4242, mtime: 1700000000, size: 8192 }))
  assert.strictEqual(first.ok, true, first.reason)

  // The same object again: the command it was checked for is released.
  const again = G.checkBinary("/usr/bin/glancectl", statLines(lines, { ino: 4242, mtime: 1700000000, size: 8192 }), first.identity)
  assert.strictEqual(again.changed, false, "same device, inode, size and mtime is the same file")

  // A different file under the same name, still owned by root and still
  // executable: accepted as the new glancectl, but flagged, so the caller
  // drops what it had queued instead of running it against a file that was
  // never the one checked.
  for (const swap of [{ ino: 9999 }, { dev: 2050 }, { mtime: 1700000001 }, { size: 8193 }]) {
    const identity = Object.assign({ ino: 4242, mtime: 1700000000, size: 8192 }, swap)
    const verdict = G.checkBinary("/usr/bin/glancectl", statLines(lines, identity), first.identity)
    assert.strictEqual(verdict.ok, true, verdict.reason)
    assert.strictEqual(verdict.changed, true, "replacement not noticed: " + JSON.stringify(swap))
  }

  // A refusal never hands back an identity to carry forward.
  const refused = G.checkBinary("/tmp/glancectl", statLines(["0 755 directory /", "0 1777 directory /tmp", "1000 755 regular file /tmp/glancectl"]), first.identity)
  assert.strictEqual(refused.ok, false)
  assert.strictEqual(refused.identity, null)

  assert.strictEqual(G.sameIdentity(first.identity, null), false)
  assert.strictEqual(G.sameIdentity(null, null), false)
  assert.ok(G.describeIdentity(first.identity).includes("4242"))
})

let failed = 0
for (const [name, fn] of tests) {
  try { fn(); console.log("  ok   " + name) } catch (error) { failed++; console.log("  FAIL " + name + "\n       " + error.message) }
}
if (failed) { console.log(failed + " failed"); process.exit(1) }
console.log(tests.length + " passed")
