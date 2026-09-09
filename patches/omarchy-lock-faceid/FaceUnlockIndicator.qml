import QtQuick
import qs.Commons

// Face ID-style capsule that drops in at the top of the lock screen while a
// face PAM module is scanning. Driven entirely by `phase`; the caller decides
// when a phase begins and ends, this file only decides how it looks.
//
//   hidden   -> parked above the screen edge, invisible
//   scanning -> slides in with a slight overshoot; a sweep runs around the
//               face glyph and the glyph breathes, like Glance on macOS
//   success  -> the sweep closes into a full ring, the glyph pops to a check
//   failure  -> the ring turns to the error colour, the capsule shakes once
Item {
  id: root

  property string phase: "hidden"
  property string message: ""
  property real topMargin: 64

  readonly property bool shown: phase !== "hidden"
  readonly property bool scanning: phase === "scanning"
  readonly property color ringColor: phase === "success" ? Color.accent
    : (phase === "failure" ? Color.lock.textError : Color.lock.text)
  readonly property string glyph: phase === "success" ? "󰄬" : (phase === "failure" ? "󰅖" : "󰱻")
  readonly property string label: {
    if (message !== "") return message
    if (phase === "success") return "Welcome back"
    if (phase === "failure") return "Face not recognized"
    return "Looking for you…"
  }

  readonly property int ringSize: 46
  readonly property int capsuleHeight: 64
  readonly property int padding: 10

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
      NumberAnimation { properties: "y"; duration: 520; easing.type: Easing.OutBack; easing.overshoot: 1.1 }
      NumberAnimation { properties: "opacity"; duration: 240 }
    },
    Transition {
      from: "shown"
      NumberAnimation { properties: "y"; duration: 300; easing.type: Easing.InCubic }
      NumberAnimation { properties: "opacity"; duration: 220 }
    }
  ]

  onPhaseChanged: {
    if (phase === "failure") shake.restart()
    if (phase === "success") pop.restart()
    ring.requestPaint()
  }
  onRingColorChanged: ring.requestPaint()

  Rectangle {
    id: capsule
    x: shakeOffset
    property real shakeOffset: 0

    width: row.implicitWidth + root.padding * 2 + 10
    height: root.capsuleHeight
    radius: height / 2
    color: Color.lock.background
    border.width: 1
    border.color: Color.lock.border

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

        // The sweep: a faint full track and a bright arc that circles while
        // scanning and closes into a complete ring on a verdict.
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
          Behavior on color { ColorAnimation { duration: 200 } }

          // The "breathing" pulse from Glance's scan state: scoped to the
          // glyph so the capsule and the text stay steady.
          SequentialAnimation {
            running: root.scanning
            loops: Animation.Infinite
            onRunningChanged: if (!running) { glyphText.opacity = 1; glyphText.scale = 1 }
            ParallelAnimation {
              NumberAnimation { target: glyphText; property: "opacity"; to: 0.55; duration: 420; easing.type: Easing.InOutSine }
              NumberAnimation { target: glyphText; property: "scale"; to: 0.94; duration: 420; easing.type: Easing.InOutSine }
            }
            ParallelAnimation {
              NumberAnimation { target: glyphText; property: "opacity"; to: 1; duration: 420; easing.type: Easing.InOutSine }
              NumberAnimation { target: glyphText; property: "scale"; to: 1; duration: 420; easing.type: Easing.InOutSine }
            }
          }

          SequentialAnimation {
            id: pop
            NumberAnimation { target: glyphText; property: "scale"; to: 1.28; duration: 140; easing.type: Easing.OutCubic }
            NumberAnimation { target: glyphText; property: "scale"; to: 1; duration: 260; easing.type: Easing.OutBack; easing.overshoot: 2 }
          }
        }
      }

      Column {
        anchors.verticalCenter: parent.verticalCenter
        spacing: 2

        Text {
          text: root.label
          color: Color.lock.text
          font.family: Style.font.family
          font.pixelSize: Style.font.heading
          font.weight: Font.Medium
        }

        Text {
          text: "Face unlock"
          color: Color.lock.placeholder
          font.family: Style.font.family
          font.pixelSize: Style.font.bodySmall
        }
      }
    }
  }
}
