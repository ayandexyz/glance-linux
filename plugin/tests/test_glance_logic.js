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
const G = vm.runInNewContext(source + "\n;({ parseStatus, stateLabel, nextStep, outcomeLabel, outcomeSeverity, missingModels, identityLine, elapsed, lastScanText, lastScanReason, parseActionResult, clamp })")
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

test("next step is the single command that unblocks the user", () => {
  assert.strictEqual(G.nextStep(G.parseStatus(JSON.stringify({ ...online, reachable: false }))), "systemctl --user start glanced")
  assert.strictEqual(G.nextStep(G.parseStatus(JSON.stringify({ ...online, models: {} }))), "glancectl fetch-model")
  assert.strictEqual(G.nextStep(G.parseStatus(JSON.stringify({ ...online, enrolled: false }))), 'glancectl enroll --name "$USER" --remember')
  assert.strictEqual(G.nextStep(G.parseStatus(JSON.stringify({ ...online, armed: false }))), "glancectl arm")
  assert.strictEqual(G.nextStep(G.parseStatus(JSON.stringify(online))), "")
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
