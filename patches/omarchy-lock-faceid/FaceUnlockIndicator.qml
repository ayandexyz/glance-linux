import QtQuick
import QtQuick.Effects
import Quickshell
import qs.Commons

// Face ID-style capsule that drops in at the top of the lock screen while a
// face PAM module is scanning. Driven entirely by `phase`; the caller decides
// when a phase begins and ends, this file only decides how it looks.
//
//   hidden   -> parked above the screen edge, invisible
//   scanning -> slides in with a slight overshoot; a sweep runs around a live
//               view of the camera, which breathes, like Glance on macOS
//   success  -> the sweep closes into a full ring, the view gives way to a check
//   failure  -> the ring turns to the error colour, the capsule shakes once
//
// The live view is not captured here — it cannot be, because the daemon holds
// the camera for the whole scan. The daemon publishes each frame as a small
// JPEG in its runtime directory (see preview.py) and this polls it. When
// there is no frame — no daemon, previews disabled, the moment before the
// first one lands — the face glyph stands in, so the indicator never depends
// on it.
Item {
  id: root

  property string phase: "hidden"
  property string message: ""
  property real topMargin: 64
  //: False for a copy that has to appear already on screen rather than drop
  //: in — the unlock afterglow, which continues a capsule that is already
  //: there. Animating its entry would read as a second, stuttering arrival.
  property bool animateEntry: true
  //: True for the copy that outlives the lock. The `lock.*` colours are tuned
  //: to sit on a blurred wallpaper and are translucent by design; over the
  //: desktop that turns the capsule into a window on whatever is behind it,
  //: with terminal text legible through the words. The notification colours
  //: are the set meant for a surface that floats over arbitrary content, and
  //: are opaque unless a theme says otherwise.
  property bool overDesktop: false

  readonly property color surfaceColor: overDesktop ? Color.notifications.background : Color.lock.background
  readonly property color surfaceBorder: overDesktop ? Color.notifications.border : Color.lock.border
  readonly property color textColor: overDesktop ? Color.notifications.text : Color.lock.text
  readonly property color subTextColor: overDesktop
    ? Qt.rgba(textColor.r, textColor.g, textColor.b, 0.66)
    : Color.lock.placeholder

  readonly property bool shown: phase !== "hidden"
  readonly property bool scanning: phase === "scanning"
  readonly property color ringColor: phase === "success" ? Color.accent
    : (phase === "failure" ? (overDesktop ? Color.urgent : Color.lock.textError) : textColor)
  readonly property string glyph: phase === "success" ? "󰄬" : (phase === "failure" ? "󰅖" : "󰱻")
  readonly property string label: {
    if (message !== "") return message
    if (phase === "success") return "Welcome back"
    if (phase === "failure") return "Face not recognized"
    return "Looking for you…"
  }

  readonly property int ringSize: 52
  readonly property int capsuleHeight: 68
  readonly property int padding: 10
  //: Gap between the ring stroke and the live view inside it.
  readonly property int previewInset: 4

  // The daemon's published frame. GLANCE_RUNTIME_DIR matches the daemon's own
  // override so a dev instance on a scratch directory previews correctly.
  readonly property string previewDir: {
    var explicit = Quickshell.env("GLANCE_RUNTIME_DIR")
    if (explicit) return explicit
    var runtime = Quickshell.env("XDG_RUNTIME_DIR")
    return runtime ? runtime + "/glance" : ""
  }
  readonly property string previewUrl: previewDir === "" ? "" : "file://" + previewDir + "/preview.jpg"

  property bool hasPreview: false
  readonly property bool showPreview: scanning && hasPreview

  // The scan "breathing", and the success pop, as plain animated properties
  // rather than animations aimed at the items themselves. An animation that
  // targets an item's `opacity` or `scale` replaces the binding on it for
  // good; keeping the motion here lets every visual property stay a binding,
  // so the glyph reliably gets out of the live view's way the instant a frame
  // lands instead of on the pulse's next cycle.
  property real pulseScale: 1.0
  property real pulseOpacity: 1.0
  property real popScale: 1.0

  SequentialAnimation {
    running: root.scanning
    loops: Animation.Infinite
    onRunningChanged: if (!running) { root.pulseScale = 1; root.pulseOpacity = 1 }
    ParallelAnimation {
      NumberAnimation { target: root; property: "pulseScale"; to: 0.95; duration: 420; easing.type: Easing.InOutSine }
      NumberAnimation { target: root; property: "pulseOpacity"; to: 0.55; duration: 420; easing.type: Easing.InOutSine }
    }
    ParallelAnimation {
      NumberAnimation { target: root; property: "pulseScale"; to: 1; duration: 420; easing.type: Easing.InOutSine }
      NumberAnimation { target: root; property: "pulseOpacity"; to: 1; duration: 420; easing.type: Easing.InOutSine }
    }
  }

  SequentialAnimation {
    id: pop
    NumberAnimation { target: root; property: "popScale"; to: 1.28; duration: 140; easing.type: Easing.OutCubic }
    NumberAnimation { target: root; property: "popScale"; to: 1; duration: 260; easing.type: Easing.OutBack; easing.overshoot: 2 }
  }

  onPhaseChanged: {
    if (phase === "failure") shake.restart()
    if (phase === "success") pop.restart()
    // Each scan starts from "no frame yet" so a verdict cannot leave the last
    // face on screen for the next one.
    if (!scanning) hasPreview = false
    ring.requestPaint()
  }
  onRingColorChanged: ring.requestPaint()

  width: capsule.width
  height: capsule.height
  anchors.horizontalCenter: parent ? parent.horizontalCenter : undefined
  y: -height - 12
  opacity: 0
  z: 10

  states: State {
    name: "shown"
    when: root.shown
    PropertyChanges { target: root; y: root.topMargin; opacity: 1 }
  }

  transitions: [
    Transition {
      to: "shown"
      enabled: root.animateEntry
      NumberAnimation { properties: "y"; duration: 520; easing.type: Easing.OutBack; easing.overshoot: 1.1 }
      NumberAnimation { properties: "opacity"; duration: 240 }
    },
    Transition {
      from: "shown"
      NumberAnimation { properties: "y"; duration: 300; easing.type: Easing.InCubic }
      NumberAnimation { properties: "opacity"; duration: 220 }
    }
  ]

  Rectangle {
    id: capsule
    x: shakeOffset
    property real shakeOffset: 0

    width: row.implicitWidth + root.padding * 2 + 10
    height: root.capsuleHeight
    radius: height / 2
    color: root.surfaceColor
    border.width: 1
    border.color: root.surfaceBorder

    // A failed scan is felt before it is read.
    SequentialAnimation {
      id: shake
      NumberAnimation { target: capsule; property: "shakeOffset"; to: -9; duration: 45 }
      NumberAnimation { target: capsule; property: "shakeOffset"; to: 8; duration: 70 }
      NumberAnimation { target: capsule; property: "shakeOffset"; to: -5; duration: 70 }
      NumberAnimation { target: capsule; property: "shakeOffset"; to: 3; duration: 60 }
      NumberAnimation { target: capsule; property: "shakeOffset"; to: 0; duration: 60 }
    }

    Row {
      id: row
      anchors.verticalCenter: parent.verticalCenter
      x: root.padding
      spacing: 14

      Item {
        id: badge
        width: root.ringSize
        height: root.ringSize
        anchors.verticalCenter: parent.verticalCenter

        // --- the live view --------------------------------------------------
        //
        // Two images, alternating. The one that is off screen loads the next
        // frame and only becomes the visible one once it has decoded, so a
        // frame is never shown half-loaded and the view never blinks between
        // frames. A single Image reloading in place does blink.
        Item {
          id: previewClip
          anchors.fill: parent
          anchors.margins: root.previewInset
          // Not `visible: showPreview` — that would cut the fade off at the
          // first frame instead of letting it run.
          visible: opacity > 0
          opacity: root.showPreview ? 1 : 0
          // Scale only: dimming a live face each cycle reads as flicker, not
          // as breathing.
          scale: root.showPreview ? root.pulseScale : 1
          Behavior on opacity { NumberAnimation { duration: 220; easing.type: Easing.OutCubic } }

          layer.enabled: true
          layer.effect: MultiEffect {
            maskEnabled: true
            maskSource: circleMask
            maskThresholdMin: 0.5
            maskSpreadAtMin: 0.08
          }

          property int shown: 0
          property int tick: 0

          function accept(index) {
            shown = index
            root.hasPreview = true
          }

          Image {
            id: frameA
            anchors.fill: parent
            fillMode: Image.PreserveAspectCrop
            cache: false
            asynchronous: true
            opacity: previewClip.shown === 0 ? 1 : 0
            onStatusChanged: if (status === Image.Ready) previewClip.accept(0)
          }

          Image {
            id: frameB
            anchors.fill: parent
            fillMode: Image.PreserveAspectCrop
            cache: false
            asynchronous: true
            opacity: previewClip.shown === 1 ? 1 : 0
            onStatusChanged: if (status === Image.Ready) previewClip.accept(1)
          }
        }

        // Mask for the round view. Never drawn itself — it is a texture the
        // effect samples.
        Item {
          id: circleMask
          anchors.fill: previewClip
          visible: false
          layer.enabled: true

          Rectangle {
            anchors.fill: parent
            radius: width / 2
            color: "white"
          }
        }

        // The query string is what forces a reload of a path that never
        // changes; the daemon replaces the file atomically, so whichever frame
        // is read is always a whole one.
        Timer {
          interval: 66  // ~15fps: ahead of the daemon's own frame rate
          repeat: true
          running: root.scanning && root.previewUrl !== ""
          triggeredOnStart: true
          onTriggered: {
            var next = previewClip.shown === 0 ? frameB : frameA
            next.source = root.previewUrl + "?v=" + (++previewClip.tick)
          }
        }

        // --- the sweep ------------------------------------------------------
        //
        // A faint full track and a bright arc that circles while scanning and
        // closes into a complete ring on a verdict.
        Canvas {
          id: ring
          anchors.fill: parent
          antialiasing: true

          property real angle: 0
          property real sweep: root.scanning ? 110 : 360
          Behavior on sweep { NumberAnimation { duration: 380; easing.type: Easing.OutCubic } }

          onAngleChanged: requestPaint()
          onSweepChanged: requestPaint()

          NumberAnimation on angle {
            from: 0; to: 360
            duration: 1100
            loops: Animation.Infinite
            running: root.scanning
          }

          onPaint: {
            var ctx = getContext("2d")
            ctx.reset()
            var c = width / 2
            var r = width / 2 - 2.5
            var col = root.ringColor
            ctx.lineWidth = 2.5
            ctx.lineCap = "round"

            ctx.beginPath()
            ctx.arc(c, c, r, 0, Math.PI * 2)
            ctx.strokeStyle = Qt.rgba(col.r, col.g, col.b, 0.18)
            ctx.stroke()

            var start = (angle - 90) * Math.PI / 180
            ctx.beginPath()
            ctx.arc(c, c, r, start, start + sweep * Math.PI / 180)
            ctx.strokeStyle = col
            ctx.stroke()
          }
        }

        Text {
          id: glyphText
          anchors.centerIn: parent
          text: root.glyph
          color: root.ringColor
          font.family: Style.font.family
          font.pixelSize: Math.round(root.ringSize * 0.5)
          horizontalAlignment: Text.AlignHCenter
          verticalAlignment: Text.AlignVCenter
          // Stands in for the live view until a frame lands, and takes over
          // again for the verdict. Drawn after the view, so it must actually
          // reach zero rather than merely dim.
          opacity: root.showPreview ? 0 : (root.scanning ? root.pulseOpacity : 1)
          scale: root.popScale * (root.scanning && !root.showPreview ? root.pulseScale : 1)
          Behavior on color { ColorAnimation { duration: 200 } }
          Behavior on opacity { NumberAnimation { duration: 200 } }
        }
      }

      Column {
        anchors.verticalCenter: parent.verticalCenter
        spacing: 2

        Text {
          text: root.label
          color: root.textColor
          font.family: Style.font.family
          font.pixelSize: Style.font.heading
          font.weight: Font.Medium
        }

        Text {
          text: "Face unlock"
          color: root.subTextColor
          font.family: Style.font.family
          font.pixelSize: Style.font.bodySmall
        }
      }
    }
  }
}
