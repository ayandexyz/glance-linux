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
const G = vm.runInNewContext(source + "\n;({ parseStatus, stateLabel, nextStep, outcomeLabel, outcomeSeverity, missingModels, identityLine, elapsed, lastScanText, lastScanReason, parseActionResult, clamp, lockLabel, nextAction, launchCommand })")
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
  assert.strictEqual(G.nextStep(status({ reachable: false })), "systemctl --user enable --now glanced")
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
  assertSame(start.command, ["systemctl", "--user", "enable", "--now", "glanced"])
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
    ["omarchy-launch-terminal", "glancectl", "setup-pam"])

  // The download prints progress; give it a terminal to print into.
  assertSame(G.launchCommand(action({ models: {} })),
    ["omarchy-launch-terminal", "glancectl", "fetch-model"])

  // Detached, so reloading the shell mid-sweep does not kill the window.
  assertSame(G.launchCommand(action({ enrolled: false }, "/opt/glancectl", "ayan")),
    ["setsid", "--fork", "/opt/glancectl", "enroll", "--name", "ayan", "--gui", "--remember"])

  // Starting the daemon is quick and silent: no wrapper at all.
  assertSame(G.launchCommand(action({ reachable: false })),
    ["systemctl", "--user", "enable", "--now", "glanced"])
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
  assert.strictEqual(action({ reachable: false }, ctl).command[0], "systemctl")
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

let failed = 0
for (const [name, fn] of tests) {
  try { fn(); console.log("  ok   " + name) } catch (error) { failed++; console.log("  FAIL " + name + "\n       " + error.message) }
}
if (failed) { console.log(failed + " failed"); process.exit(1) }
console.log(tests.length + " passed")
