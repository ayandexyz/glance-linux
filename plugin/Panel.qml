pragma ComponentBehavior: Bound

import QtQuick
import Quickshell.Io
import qs.Commons
import qs.Ui as Ui
import "GlanceLogic.js" as GlanceLogic

// Bar icon + panel for the glanced face-unlock daemon.
//
// Presentation only: the bar can show you whether unlock is armed and what the
// last scan decided, and the buttons run the same `glancectl` commands you
// would type. Nothing here is on the unlock path — if the shell is not
// running, PAM still talks to the daemon exactly the same.
Ui.Panel {
  id: root
  moduleName: "ayande.glance"
  ipcTarget: "ayande.glance"
  manageIpc: false

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  readonly property var status: backend.status
  readonly property string stateLabel: GlanceLogic.stateLabel(status)
  readonly property string nextStep: GlanceLogic.nextStep(status)
  readonly property bool canArm: status && status.reachable && status.enrolled && !status.armed
    && GlanceLogic.missingModels(status).length === 0
  readonly property bool canScan: status && status.reachable && status.armed && !backend.actionBusy

  // Ticks only while the panel is open, so "3s ago" stays honest without
  // waking the shell every second in the background.
  property double nowMs: Date.now()
  property string passphrase: ""

  implicitWidth: iconButton.implicitWidth
  implicitHeight: iconButton.implicitHeight

  function refreshNow() { backend.refresh() }
  function testScan() { if (root.canScan) backend.testScan() }
  function submitArm(remember) {
    if (!root.canArm || root.passphrase === "" || backend.actionBusy) return
    backend.arm(root.passphrase, remember)
    root.passphrase = ""
  }

  function severityColor(severity) {
    if (severity === "good") return Color.accent
    if (severity === "bad") return urgent
    return foreground
  }

  function handleBarPress(buttonCode) {
    if (buttonCode === Qt.RightButton) {
      testScan()
      return
    }
    toggle()
  }

  StatusBackend {
    id: backend
    settings: root.settings
  }

  Timer {
    interval: 1000
    running: root.opened
    repeat: true
    triggeredOnStart: true
    onTriggered: root.nowMs = Date.now()
  }

  IpcHandler {
    target: root.ipcTarget
    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function refresh(): string { root.refreshNow(); return "ok" }
    function scan(): string { root.testScan(); return root.canScan ? "ok" : "not armed" }
  }

  Ui.BarIconButton {
    id: iconButton
    anchors.fill: parent
    bar: root.bar
    text: "󰱻"
    // Lit while a scan is in flight, so a lock-screen attempt shows on the bar.
    active: backend.scanning
    tooltipText: "Glance · " + root.stateLabel
    onPressed: function(buttonCode) { root.handleBarPress(buttonCode) }
  }

  Ui.KeyboardPanel {
    id: panel
    anchorItem: iconButton
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(380))
    contentHeight: panel.fittedContentHeight(contentColumn.implicitHeight)

    Ui.PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      // The passphrase field owns the keyboard while it is showing.
      blocked: passphraseField.visible && passphraseField.activeFocus

      onActivateRequested: root.testScan()
      onCloseRequested: root.close()

      Column {
        id: contentColumn
        width: parent.width
        spacing: Style.spacing.lg

        Ui.PanelHero {
          width: parent.width
          foreground: root.foreground
          fontFamily: root.fontFamily
          title: "Glance"
          meta: root.stateLabel
          detail: {
            if (!root.status || !root.status.reachable) return "Face unlock daemon"
            var parts = []
            var count = root.status.identities.length
            if (root.status.armed) parts.push(count + (count === 1 ? " identity" : " identities"))
            if (root.status.camera !== "") parts.push(root.status.camera)
            if (root.status.remembered) parts.push("passphrase remembered")
            return parts.join(" · ")
          }
          iconComponent: Component {
            Text {
              text: "󰱻"
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.display
            }
          }
        }

        // What to do next, when the daemon cannot scan yet.
        Column {
          width: parent.width
          spacing: Style.spacing.xs
          visible: root.nextStep !== "" && !backend.binaryMissing && backend.fetchError === ""

          Text {
            width: parent.width
            text: {
              if (!root.status) return ""
              if (!root.status.reachable) return "glanced is not running."
              var missing = GlanceLogic.missingModels(root.status)
              if (missing.length > 0) return "Models not downloaded: " + missing.join(", ") + "."
              if (!root.status.enrolled) return "No face enrolled yet."
              return "Enrollment is encrypted; the daemon needs the passphrase to scan."
            }
            color: root.foreground
            wrapMode: Text.WordWrap
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
          }

          Text {
            width: parent.width
            visible: !root.canArm
            text: root.nextStep
            color: root.dim
            wrapMode: Text.WrapAnywhere
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }
        }

        // Inline arming. The passphrase never touches argv.
        Column {
          width: parent.width
          spacing: Style.spacing.sm
          visible: root.canArm

          Ui.TextField {
            id: passphraseField
            width: parent.width
            password: true
            placeholderText: "Passphrase"
            foreground: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            enabled: !backend.actionBusy
            text: root.passphrase
            onTextChanged: if (text !== root.passphrase) root.passphrase = text
            onAccepted: root.submitArm(false)
            Keys.onEscapePressed: root.close()
            onVisibleChanged: if (visible && root.opened) Qt.callLater(forceActiveFocus)
          }

          Row {
            spacing: Style.spacing.sm

            Ui.Button {
              text: "Arm"
              iconText: "󰒃"
              bordered: true
              foreground: root.foreground
              fontFamily: root.fontFamily
              enabled: root.passphrase !== "" && !backend.actionBusy
              onClicked: root.submitArm(false)
            }

            Ui.Button {
              text: "Arm and remember"
              bordered: true
              foreground: root.foreground
              fontFamily: root.fontFamily
              tooltipText: "Also store the passphrase (0600) so the daemon arms itself at login"
              enabled: root.passphrase !== "" && !backend.actionBusy
              onClicked: root.submitArm(true)
            }
          }
        }

        // Identities, while armed. Disarmed, the store is ciphertext and the
        // daemon genuinely does not know who is in it.
        Column {
          width: parent.width
          spacing: Style.spacing.xs
          visible: root.status && root.status.armed && root.status.identities.length > 0

          Ui.PanelSectionHeader {
            width: parent.width
            text: "Enrolled"
            foreground: root.foreground
            fontFamily: root.fontFamily
          }

          Repeater {
            model: root.status ? root.status.identities : []

            delegate: Text {
              required property var modelData
              width: parent ? parent.width : 0
              text: GlanceLogic.identityLine(modelData)
              color: modelData.enabled === false ? root.dim : root.foreground
              elide: Text.ElideRight
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
            }
          }
        }

        // Last scan — the daemon's, so a lock-screen attempt shows here too.
        Column {
          width: parent.width
          spacing: Style.spacing.xs
          visible: root.status && root.status.lastScan !== null

          Ui.PanelSectionHeader {
            width: parent.width
            text: "Last scan"
            foreground: root.foreground
            fontFamily: root.fontFamily
          }

          Text {
            width: parent.width
            text: GlanceLogic.lastScanText(root.status, root.nowMs)
            color: root.severityColor(GlanceLogic.outcomeSeverity(
              root.status && root.status.lastScan ? root.status.lastScan.outcome : ""))
            wrapMode: Text.WordWrap
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            font.weight: Font.Medium
          }

          Text {
            width: parent.width
            visible: text !== ""
            text: GlanceLogic.lastScanReason(root.status)
            color: root.dim
            wrapMode: Text.WordWrap
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }
        }

        // Action feedback: the error from arm/disarm, or a scan that is running.
        Text {
          width: parent.width
          visible: text !== ""
          text: {
            if (backend.actionName === "authenticate") return "Scanning — look at the camera..."
            if (backend.actionName !== "") return "Running " + backend.actionName + "..."
            if (backend.actionResult && backend.actionResult.error !== "") return backend.actionResult.error
            return ""
          }
          color: backend.actionBusy ? root.foreground : root.urgent
          wrapMode: Text.WordWrap
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
        }

        // The one failure a user fixes by installing something.
        Column {
          width: parent.width
          spacing: Style.spacing.xs
          visible: backend.binaryMissing

          Text {
            width: parent.width
            text: "glancectl was not found"
            color: root.urgent
            wrapMode: Text.WordWrap
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            font.weight: Font.Medium
          }

          Text {
            width: parent.width
            text: "Set 'glancectl path' in the widget settings, or put glancectl on PATH."
            color: root.dim
            wrapMode: Text.WordWrap
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }
        }

        Text {
          width: parent.width
          visible: !backend.binaryMissing && backend.fetchError !== ""
          text: backend.fetchError
          color: root.urgent
          wrapMode: Text.WordWrap
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
        }

        Ui.PanelSeparator {
          width: parent.width
          foreground: root.foreground
        }

        Item {
          width: parent.width
          height: Math.max(footerText.implicitHeight, footerActions.implicitHeight)

          Text {
            id: footerText
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            text: {
              if (backend.loading && backend.lastSuccessAt <= 0) return "Checking..."
              if (backend.lastSuccessAt <= 0) return "Not checked yet"
              return "Updated " + GlanceLogic.elapsed(backend.lastSuccessAt, root.nowMs)
            }
          }

          Row {
            id: footerActions
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.spacing.xs

            Ui.PanelActionButton {
              foreground: root.foreground
              fontFamily: root.fontFamily
              iconText: "󰈈"
              tooltipText: root.canScan ? "Test scan (right-click the bar icon does this too)" : "Arm the daemon to scan"
              enabled: root.canScan
              onClicked: root.testScan()
            }

            Ui.PanelActionButton {
              visible: root.status && root.status.reachable && root.status.armed
              foreground: root.foreground
              fontFamily: root.fontFamily
              iconText: "󰒃"
              tooltipText: "Disarm (drop the decrypted enrollment from memory)"
              enabled: !backend.actionBusy
              onClicked: backend.disarm()
            }

            Ui.PanelActionButton {
              foreground: root.foreground
              fontFamily: root.fontFamily
              iconText: "󰑐"
              tooltipText: "Refresh now"
              onClicked: root.refreshNow()
            }
          }
        }
      }
    }
  }

  onOpenedChanged: {
    nowMs = Date.now()
    if (opened) {
      refreshNow()
      if (canArm) Qt.callLater(function() { passphraseField.forceActiveFocus() })
    } else {
      passphrase = ""
    }
  }
}
