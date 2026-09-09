import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons

// Headless harness: instantiates the real StatusBackend and Panel in
// Quickshell against a fake glancectl, so the process bridge and the QML
// actually run. Writes a JSON verdict and quits.
ShellRoot {
  id: root

  readonly property string resultPath: Quickshell.env("GLANCE_TEST_RESULT")
  readonly property string panelUrl: Quickshell.env("GLANCE_PANEL_URL")
  readonly property string backendUrl: Quickshell.env("GLANCE_BACKEND_URL")
  readonly property string fakeOnline: Quickshell.env("GLANCE_FAKE_ONLINE")
  readonly property string fakeOffline: Quickshell.env("GLANCE_FAKE_OFFLINE")
  readonly property string fakeGarbage: Quickshell.env("GLANCE_FAKE_GARBAGE")
  readonly property string fakeCrash: Quickshell.env("GLANCE_FAKE_CRASH")

  property var failures: []
  property var widget: null
  property var online: null
  property var offline: null
  property var garbage: null
  property var crash: null
  property var missing: null
  property int phase: 0
  property int waits: 0

  function fail(message) { failures.push(String(message)) }
  function assertTrue(condition, message) { if (!condition) fail(message) }
  function assertEqual(actual, expected, message) {
    if (actual !== expected) fail(message + " expected=" + expected + " actual=" + actual)
  }

  function loadComponent(url, properties, label) {
    var component = Qt.createComponent(url, Component.PreferSynchronous)
    if (component.status !== Component.Ready) {
      fail(label + " failed to load: " + component.errorString())
      return null
    }
    var object = component.createObject(host, properties || {})
    if (!object) fail(label + " failed to instantiate: " + component.errorString())
    return object
  }

  function backendWith(path, label) {
    var backend = loadComponent(backendUrl, { settings: { refreshIntervalSec: 600, glancectlPath: path } }, label)
    if (backend) backend.refresh()
    return backend
  }

  function settled(backend) { return backend && !backend.loading && (backend.status !== null || backend.fetchError !== "") }

  function finish() {
    poll.stop()
    var report = { ok: failures.length === 0, failures: failures }
    resultFile.setText(JSON.stringify(report, null, 2))
    quitTimer.start()
  }

  // Give the FileView a tick to flush before the process goes away.
  Timer {
    id: quitTimer
    interval: 200
    repeat: false
    onTriggered: Qt.quit()
  }

  Item { id: host }

  FileView {
    id: resultFile
    path: root.resultPath
    blockWrites: false
  }

  Component.onCompleted: {
    widget = loadComponent(panelUrl, { settings: { refreshIntervalSec: 600, glancectlPath: fakeOnline } }, "Panel")
    online = backendWith(fakeOnline, "online backend")
    offline = backendWith(fakeOffline, "offline backend")
    garbage = backendWith(fakeGarbage, "garbage backend")
    crash = backendWith(fakeCrash, "crash backend")
    missing = backendWith("/nonexistent/glancectl", "missing backend")
    poll.start()
  }

  Timer {
    id: poll
    interval: 100
    repeat: true
    onTriggered: {
      root.waits++
      if (root.waits > 200) {
        root.fail("timed out waiting for phase " + root.phase)
        root.finish()
        return
      }

      if (root.phase === 0) {
        if (!(settled(root.online) && settled(root.offline) && settled(root.garbage) && settled(root.crash) && settled(root.missing))) return

        assertTrue(root.widget !== null, "panel instantiated")
        if (root.widget) {
          assertTrue(root.widget.implicitWidth > 0 && root.widget.implicitHeight > 0, "panel has a bar footprint")
        }

        assertEqual(root.online.reachable, true, "online: reachable")
        assertEqual(root.online.armed, true, "online: armed")
        assertEqual(root.online.identities.length, 1, "online: identities")
        assertEqual(root.online.fetchError, "", "online: no error")

        assertEqual(root.offline.reachable, false, "offline: not reachable")
        assertEqual(root.offline.fetchError, "", "offline: still a valid document")
        assertTrue(root.offline.status !== null, "offline: status parsed despite exit 1")

        assertTrue(root.garbage.fetchError.indexOf("unreadable") >= 0, "garbage: flagged as unreadable, got: " + root.garbage.fetchError)
        assertTrue(root.crash.fetchError.indexOf("status 3") >= 0, "crash: exit status surfaced, got: " + root.crash.fetchError)
        assertEqual(root.missing.binaryMissing, true, "missing: binary flagged")

        // Phase 1: the arm action, passphrase over stdin.
        root.online.arm("wrong", false)
        root.phase = 1
        root.waits = 0
        return
      }

      if (root.phase === 1) {
        if (root.online.actionBusy || root.online.actionResult === null) return
        assertEqual(root.online.actionResult.ok, false, "arm: wrong passphrase refused")
        assertEqual(root.online.actionResult.error, "wrong passphrase", "arm: stderr surfaced")
        root.online.arm("correct", false)
        root.phase = 2
        root.waits = 0
        return
      }

      if (root.phase === 2) {
        if (root.online.actionBusy || root.online.actionResult === null || root.online.actionResult.error === "wrong passphrase") return
        assertEqual(root.online.actionResult.ok, true, "arm: correct passphrase accepted")
        root.online.testScan()
        root.phase = 3
        root.waits = 0
        return
      }

      if (root.phase === 3) {
        if (root.online.actionBusy || root.online.actionResult === null || root.online.actionResult.name !== "authenticate") return
        assertEqual(root.online.actionResult.outcome, "unlocked", "scan: outcome parsed")
        assertEqual(root.online.actionResult.identity, "tester", "scan: identity parsed")
        root.finish()
      }
    }
  }
}
