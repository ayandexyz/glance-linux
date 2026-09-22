import QtQuick
import QtQuick.Effects
import Quickshell
import qs.Commons

// The face unlock overlay: a detached pill that drops from the top of the
// screen, springs open, shows the Omarchy mark, then a live view of the camera
// inside a sweeping ring, and finally a tick.
//
// The motion is Glance's, ported from `NotchOverlay/NotchOverlayView.swift` in
// the macOS app. The part worth copying exactly is the *choreography*: the
// slide and the expansion are two independent timelines rather than one. On
// the way in the pill slides down first and the size follows a beat later; on
// the way out it contracts first and slides away a beat later. Moving both
// together reads as a box changing size. Staggering them reads as an object
// arriving. Upstream's springs are asymmetric for the same reason — a little
// overshoot opening, none closing.
//
// There is no text anywhere in here. The mark says whose lock screen this is,
// the ring says it is looking, the tick says it worked.
//
// Phases, driven entirely by the caller:
//
//   hidden   -> parked above the top edge, contracted
//   scanning -> slides in, springs open on the mark, then morphs to the ring
//               and the live camera view
//   success  -> the sweep closes into a full ring, the sphere spins inside
//               it, the tick pops
//   failure  -> the ring turns to the error colour and the pill shakes once
Item {
  id: root

  property string phase: "hidden"
  //: Kept for callers that set it; nothing here renders text.
  property string message: ""
  property real topMargin: 64

  //: False for a copy that has to appear already on screen rather than arrive
  //: — the unlock afterglow, which continues a pill that is already there.
  property bool animateEntry: true
  //: True for the copy that outlives the lock. The `lock.*` colours are tuned
  //: to sit on a blurred wallpaper and are translucent by design; over the
  //: desktop that leaves whatever is behind legible through the pill. The
  //: notification colours are the set meant to float over arbitrary content.
  property bool overDesktop: false

  readonly property bool shown: phase !== "hidden"
  readonly property bool scanning: phase === "scanning"
  readonly property bool settled: phase === "success" || phase === "failure"

  readonly property color surfaceColor: overDesktop ? Color.notifications.background : Color.lock.background
  readonly property color surfaceBorder: overDesktop ? Color.notifications.border : Color.lock.border
  readonly property color markColor: overDesktop ? Color.notifications.text : Color.lock.text
  //: The phase the pill is *wearing*, which lags `phase` on the way out. The
  //: caller drops straight to "hidden" while the pill is still fading, and
  //: anything bound live to `phase` would swap the verdict back to the
  //: scanning face for the length of that fade — a tick, then a face, then
  //: nothing. The pill leaves showing whatever it settled on.
  property string litPhase: "scanning"
  readonly property color ringColor: litPhase === "success" ? Color.accent
    : (litPhase === "failure" ? (overDesktop ? Color.urgent : Color.lock.textError) : markColor)

  // --- footprints ---------------------------------------------------------
  //
  // The mark is a square, so every footprint is a rounded square too — the
  // contracted bar, the open panel, the camera view and the progress outline
  // all share one corner treatment, and the shape never changes as the pill
  // grows. The contracted size is deliberately narrower than any open one, so
  // the growth is an event rather than a nudge (upstream's note on
  // `pillClosedSize`).
  readonly property real closedWidth: 92
  readonly property real closedHeight: 26
  //: One open footprint for every stage. The mark, the camera view and the
  //: verdict all sit in the same square, so the pill grows once on arrival and
  //: then holds still — only its contents change.
  readonly property real openSize: 80
  readonly property real ringSize: 64
  readonly property real previewInset: 4

  //: Proportional to the short side, so the contracted bar and the open panel
  //: read as the same shape rather than as a rounded rectangle turning into a
  //: circle. Capped so the open square never rounds away into one.
  readonly property real cornerRadius: Math.min(22, Math.min(width, height) * 0.32)
  //: The inner radii are the outer one stepped down by each inset, so the
  //: outline and the camera view stay concentric with the pill's own corners.
  //: That holds while the pill is looking. The verdict is the one exception:
  //: the tick and the cross sit in a circle, and both the pill and the outline
  //: round into one as the sweep closes — the square was the frame for a
  //: face, the circle is a badge.
  readonly property real ringCornerRadius: 11.5
  readonly property real previewCornerRadius: 10
  readonly property bool verdict: litPhase !== "scanning"
  //: 0 is the square, 1 is the circle. One factor rather than an animated
  //: radius, so the rounding stays put while the pill is changing size
  //: underneath it — on the way out it contracts to a capsule, not to a bar
  //: whose corners are still catching up.
  property real verdictRound: verdict ? 1 : 0
  Behavior on verdictRound { NumberAnimation { duration: 380; easing.type: Easing.OutCubic } }

  //: The mark is square (icon.png is 300 x 300), so one number sizes it. It
  //: fills the open square the way the camera view does, leaving the corners
  //: as the only breathing room.
  readonly property real markHeight: 52
  readonly property real markWidth: markHeight

  readonly property string omarchyPath: {
    var fromEnv = Quickshell.env("OMARCHY_PATH")
    return fromEnv ? fromEnv : "/usr/share/omarchy"
  }
  //: Shipped with Omarchy itself, a flat mark on transparent, so it can be
  //: masked to whatever the current theme's foreground happens to be rather
  //: than pinned to one theme's palette.
  readonly property string markUrl: "file://" + omarchyPath + "/icon.png"

  // --- stages -------------------------------------------------------------

  //: The mark holds the pill for a beat at the start of a scan, then gives way
  //: to the ring. Long enough to register, short enough that it is never
  //: standing between the user and their session. The beat is measured from
  //: the moment the pill starts to open, not from the start of the scan: the
  //: slide-in and the growth come first, and a hold counted from the scan
  //: would spend most of itself on a mark that is still arriving.
  property bool markStage: false
  readonly property bool showRing: shown && !markStage
  readonly property bool showPreview: scanning && hasPreview && !markStage
  readonly property bool showGlyph: showRing && !showPreview

  property bool hasPreview: false

  //: The success beat between the ring closing and the tick: the Face ID
  //: sphere from the macOS app's unlock video, spinning once inside the
  //: ring. The tick waits for it. A failure has nothing to wait for.
  property bool spinning: false
  property bool tickPending: false

  function finishSpin() {
    spinning = false
    if (!tickPending) return
    tickPending = false
    pop.restart()
  }

  // --- the two timelines --------------------------------------------------

  property bool positioned: false
  property bool expanded: false
  //: Behaviours stay inert until after the first layout, so a copy created
  //: already on screen appears there instead of flying in from nowhere.
  property bool ready: false

  function runChoreography() {
    if (!animateEntry) {
      positioned = shown
      growEasing = shown ? Easing.OutBack : Easing.OutCubic
      expanded = shown
      return
    }
    if (shown) {
      positioned = true          // leading: slide down
      expandDelay.restart()      // trailing: then grow
      slideDelay.stop()
    } else {
      growEasing = Easing.OutCubic
      expanded = false           // leading: contract
      slideDelay.restart()       // trailing: then leave
      expandDelay.stop()
    }
  }

  Timer { id: expandDelay; interval: 160; onTriggered: { root.growEasing = Easing.OutBack; root.expanded = true } }
  Timer { id: slideDelay; interval: 180; onTriggered: root.positioned = false }
  Timer { id: markTimer; interval: 750; onTriggered: root.markStage = false }
  //: Just short of the sweep's own 380ms, so the sphere is already turning
  //: as the outline completes rather than starting from a finished circle.
  Timer {
    id: spinDelay
    interval: 340
    onTriggered: if (root.tickPending) { root.spinning = true; spin.restart() }
  }

  onShownChanged: runChoreography()
  onExpandedChanged: if (expanded && markStage) markTimer.restart()
  onPhaseChanged: {
    if (phase === "scanning") {
      markStage = true
      // Already open (a retry while the last verdict is still up): count from
      // now. Otherwise the expansion starts the clock.
      if (expanded) markTimer.restart()
      else markTimer.stop()
    } else {
      markStage = false
      markTimer.stop()
    }
    // A verdict must never leave the previous scan's face on screen.
    if (!scanning) hasPreview = false
    if (phase !== "hidden") litPhase = phase
    if (phase === "failure") shake.restart()
    if (phase === "success") {
      tickPending = true
      spinDelay.restart()
    } else {
      tickPending = false
      spinDelay.stop()
      spinning = false
      spin.stop()
    }
    ring.requestPaint()
  }
  onRingColorChanged: ring.requestPaint()

  Component.onCompleted: {
    runChoreography()
    Qt.callLater(function() { root.ready = true })
  }

  // --- geometry -----------------------------------------------------------

  width: expanded ? openSize : closedWidth
  height: expanded ? openSize : closedHeight
  anchors.horizontalCenter: parent ? parent.horizontalCenter : undefined
  y: positioned ? topMargin : -height - 24
  opacity: positioned ? 1 : 0
  z: 10

  // Opening overshoots a little, closing does not — the asymmetry is what
  // makes arriving feel eager and leaving feel deliberate. The curve is set
  // imperatively just before `expanded` flips, rather than bound to it: a
  // binding on the same flag is not guaranteed to have re-evaluated by the
  // time the Behavior starts, and a collapse that begins on the opening curve
  // dips below the closed size before it settles.
  property int growEasing: Easing.OutBack
  Behavior on width {
    enabled: root.ready
    NumberAnimation { duration: 450; easing.type: root.growEasing; easing.overshoot: 1.4 }
  }
  Behavior on height {
    enabled: root.ready
    NumberAnimation { duration: 450; easing.type: root.growEasing; easing.overshoot: 1.4 }
  }
  Behavior on y {
    enabled: root.ready
    NumberAnimation { duration: 250; easing.type: root.positioned ? Easing.OutCubic : Easing.InCubic }
  }
  Behavior on opacity {
    enabled: root.ready
    NumberAnimation { duration: 200 }
  }

  // --- the pill -----------------------------------------------------------

  Rectangle {
    id: pill
    anchors.fill: parent
    x: shakeOffset
    property real shakeOffset: 0

    radius: root.cornerRadius + (Math.min(width, height) / 2 - root.cornerRadius) * root.verdictRound
    color: root.surfaceColor
    border.width: 1
    border.color: root.surfaceBorder
    clip: true

    // A failed scan is felt before it is read.
    SequentialAnimation {
      id: shake
      NumberAnimation { target: pill; property: "shakeOffset"; to: -9; duration: 45 }
      NumberAnimation { target: pill; property: "shakeOffset"; to: 8; duration: 70 }
      NumberAnimation { target: pill; property: "shakeOffset"; to: -5; duration: 70 }
      NumberAnimation { target: pill; property: "shakeOffset"; to: 3; duration: 60 }
      NumberAnimation { target: pill; property: "shakeOffset"; to: 0; duration: 60 }
    }

    // --- the Omarchy mark -------------------------------------------------
    //
    // Tinted by masking a rectangle of the theme's own foreground with the
    // mark's alpha, rather than by recolouring the artwork: the shipped file
    // is one flat colour, which no hue-based tint can move off that colour.
    Item {
      id: mark
      anchors.centerIn: parent
      width: root.markWidth
      height: root.markHeight
      visible: opacity > 0
      opacity: root.markStage && root.expanded ? 1 : 0
      scale: root.markStage ? 1 : 0.92
      Behavior on opacity { NumberAnimation { duration: 240; easing.type: Easing.OutCubic } }
      Behavior on scale { NumberAnimation { duration: 320; easing.type: Easing.OutCubic } }

      layer.enabled: true
      layer.effect: MultiEffect {
        maskEnabled: true
        maskSource: markMask
        maskThresholdMin: 0.4
        maskSpreadAtMin: 0.1
      }

      Rectangle {
        anchors.fill: parent
        color: root.markColor
      }
    }

    Image {
      id: markMask
      anchors.centerIn: parent
      width: root.markWidth
      height: root.markHeight
      source: root.markUrl
      fillMode: Image.PreserveAspectFit
      // Rendered at twice the drawn size so the mark stays crisp on a
      // scaled output.
      sourceSize.width: root.markWidth * 2
      sourceSize.height: root.markHeight * 2
      visible: false
      layer.enabled: true
    }

    // --- the ring, the camera, the verdict --------------------------------

    Item {
      id: badge
      anchors.centerIn: parent
      width: root.ringSize
      height: root.ringSize
      visible: opacity > 0
      opacity: root.showRing && root.expanded ? 1 : 0
      Behavior on opacity { NumberAnimation { duration: 240; easing.type: Easing.OutCubic } }

      // Two images, alternating. The one that is off screen loads the next
      // frame and only becomes the visible one once it has decoded, so the
      // view never blinks between frames.
      Item {
        id: previewClip
        anchors.fill: parent
        anchors.margins: root.previewInset
        visible: opacity > 0
        opacity: root.showPreview ? 1 : 0
        // Scale only: dimming a live face each cycle reads as flicker, not as
        // breathing. Follows the pulse unconditionally: it settles back to 1
        // on its own when the scan ends, so the view never snaps mid-fade.
        scale: root.pulseScale
        Behavior on opacity { NumberAnimation { duration: 220; easing.type: Easing.OutCubic } }

        layer.enabled: true
        layer.effect: MultiEffect {
          maskEnabled: true
          maskSource: previewMask
          maskThresholdMin: 0.5
          maskSpreadAtMin: 0.08
        }

        property int currentFrame: 0
        property int tick: 0

        function accept(index) {
          currentFrame = index
          root.hasPreview = true
        }

        Image {
          id: frameA
          anchors.fill: parent
          fillMode: Image.PreserveAspectCrop
          cache: false
          asynchronous: true
          opacity: previewClip.currentFrame === 0 ? 1 : 0
          onStatusChanged: if (status === Image.Ready) previewClip.accept(0)
        }

        Image {
          id: frameB
          anchors.fill: parent
          fillMode: Image.PreserveAspectCrop
          cache: false
          asynchronous: true
          opacity: previewClip.currentFrame === 1 ? 1 : 0
          onStatusChanged: if (status === Image.Ready) previewClip.accept(1)
        }
      }

      Item {
        id: previewMask
        anchors.fill: previewClip
        visible: false
        layer.enabled: true

        Rectangle {
          anchors.fill: parent
          radius: root.previewCornerRadius
          color: "white"
        }
      }

      // The query string forces a reload of a path that never changes; the
      // daemon replaces the file atomically, so whichever frame is read is
      // always a whole one. Polling runs for the whole scan, mark included, so
      // a frame is ready the moment the ring appears.
      Timer {
        interval: 66  // ~15fps: ahead of the daemon's own frame rate
        repeat: true
        running: root.scanning && root.previewUrl !== ""
        triggeredOnStart: true
        onTriggered: {
          var next = previewClip.currentFrame === 0 ? frameB : frameA
          next.source = root.previewUrl + "?v=" + (++previewClip.tick)
        }
      }

      // The sphere. Frames 16-45 of `unlockanimation.mp4` from the macOS
      // app, cut to the inside of its circle — the outline here is the
      // Canvas below, closing on its own — and kept as white on alpha, so
      // it takes the theme's colour the way the ring does.
      AnimatedSprite {
        id: spin
        anchors.centerIn: parent
        // The sheet's frame is 340 across with the video's ring 317 across
        // inside it; the badge's outline is the ring size less the padding.
        // Sized so the two circles coincide.
        width: (root.ringSize - 5) * (340 / 317)
        height: width
        source: Qt.resolvedUrl("unlock-spin.png")
        frameCount: 30
        frameWidth: 128
        frameHeight: 128
        frameRate: 60
        loops: 1
        running: false
        interpolate: false
        visible: root.spinning
        layer.enabled: true
        layer.effect: MultiEffect {
          colorization: 1.0
          colorizationColor: root.ringColor
        }
        onFinished: root.finishSpin()
      }

      // A faint full track and a bright arc that travel the same rounded
      // square, closing into a complete outline on a verdict. The sweep walks
      // the perimeter by length rather than by angle, so it keeps one steady
      // speed through the corners instead of racing them.
      Canvas {
        id: ring
        anchors.fill: parent
        antialiasing: true

        property real angle: 0
        property real sweep: root.scanning ? 110 : 360
        Behavior on sweep { NumberAnimation { duration: 380; easing.type: Easing.OutCubic } }
        //: Half the traced size is a full circle. Follows the pill's own
        //: rounding, so the outline and the container round off together as
        //: the sweep completes. The factor reads `litPhase` rather than
        //: `phase`, so the badge keeps its circle while the pill leaves and
        //: only squares up again once the next scan begins — behind the mark,
        //: where nobody sees it.
        property real cornerRadius: root.ringCornerRadius + ((width - 5) / 2 - root.ringCornerRadius) * root.verdictRound

        onAngleChanged: requestPaint()
        onSweepChanged: requestPaint()
        onCornerRadiusChanged: requestPaint()

        NumberAnimation on angle {
          from: 0; to: 360
          duration: 1100
          loops: Animation.Infinite
          running: root.showRing && root.scanning
        }

        //: The outline as contiguous pieces, clockwise from the top centre,
        //: each carrying its own length so a run of the whole can be picked
        //: out of it by distance.
        function outline(x, y, w, h, r) {
          var segs = []
          function line(x1, y1, x2, y2) {
            var dx = x2 - x1, dy = y2 - y1
            var len = Math.sqrt(dx * dx + dy * dy)
            // At a full circle the straights vanish; a zero-length piece
            // would only give the tracer something to spin on.
            if (len < 0.01) return
            segs.push({ curved: false, x1: x1, y1: y1, x2: x2, y2: y2, len: len })
          }
          function corner(cx, cy, a0) {
            segs.push({ curved: true, cx: cx, cy: cy, r: r,
                        a0: a0, a1: a0 + Math.PI / 2, len: r * Math.PI / 2 })
          }
          var mid = x + w / 2
          line(mid, y, x + w - r, y)
          corner(x + w - r, y + r, -Math.PI / 2)
          line(x + w, y + r, x + w, y + h - r)
          corner(x + w - r, y + h - r, 0)
          line(x + w - r, y + h, x + r, y + h)
          corner(x + r, y + h - r, Math.PI / 2)
          line(x, y + h - r, x, y + r)
          corner(x + r, y + r, Math.PI)
          line(x + r, y, mid, y)
          return segs
        }

        //: Strokes `length` of the outline starting `from` along it, wrapping
        //: past the top centre as many times as it takes.
        function trace(ctx, segs, total, from, length) {
          ctx.beginPath()
          var pos = ((from % total) + total) % total
          var left = Math.min(length, total)
          var started = false
          var guard = 0
          while (left > 0.01 && guard++ < 64) {
            var acc = 0
            for (var i = 0; i < segs.length; i++) {
              var s = segs[i]
              if (pos < acc + s.len - 0.001 || i === segs.length - 1) {
                var head = pos - acc
                var take = Math.min(s.len - head, left)
                if (s.curved) {
                  var da = s.a1 - s.a0
                  var a0 = s.a0 + da * (head / s.len)
                  var a1 = s.a0 + da * ((head + take) / s.len)
                  if (!started)
                    ctx.moveTo(s.cx + s.r * Math.cos(a0), s.cy + s.r * Math.sin(a0))
                  ctx.arc(s.cx, s.cy, s.r, a0, a1)
                } else {
                  var t0 = head / s.len
                  var t1 = (head + take) / s.len
                  if (!started)
                    ctx.moveTo(s.x1 + (s.x2 - s.x1) * t0, s.y1 + (s.y2 - s.y1) * t0)
                  ctx.lineTo(s.x1 + (s.x2 - s.x1) * t1, s.y1 + (s.y2 - s.y1) * t1)
                }
                started = true
                left -= take
                pos += take
                if (pos >= total) pos -= total
                break
              }
              acc += s.len
            }
          }
          ctx.stroke()
        }

        onPaint: {
          var ctx = getContext("2d")
          ctx.reset()
          var pad = 2.5
          var side = Math.min(width, height) - pad * 2
          var segs = outline(pad, pad, width - pad * 2, height - pad * 2, Math.min(cornerRadius, side / 2))
          var total = 0
          for (var i = 0; i < segs.length; i++) total += segs[i].len

          var col = root.ringColor
          ctx.lineWidth = 2.5
          ctx.lineCap = "round"
          ctx.lineJoin = "round"

          ctx.strokeStyle = Qt.rgba(col.r, col.g, col.b, 0.18)
          trace(ctx, segs, total, 0, total)

          ctx.strokeStyle = col
          trace(ctx, segs, total, total * angle / 360, total * sweep / 360)
        }
      }

      Text {
        id: glyphText
        anchors.centerIn: parent
        text: root.litPhase === "success" && !root.tickPending ? "󰄬"
          : (root.litPhase === "failure" ? "󰅖" : "󰱻")
        color: root.ringColor
        font.family: Style.font.family
        font.pixelSize: Math.round(root.ringSize * 0.46)
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
        // Stands in for the live view until a frame lands, and takes the
        // circle back for the verdict. Drawn after the view, so it has to
        // reach zero rather than merely dim. On a success it leaves while
        // the sphere spins and returns as the tick.
        opacity: root.showGlyph && !root.tickPending ? root.pulseOpacity : 0
        scale: root.popScale * root.pulseScale
        Behavior on color { ColorAnimation { duration: 200 } }
        Behavior on opacity { NumberAnimation { duration: 200 } }
      }
    }
  }

  // --- the scan pulse and the success pop ---------------------------------
  //
  // Animated properties rather than animations aimed at the items: an
  // animation that targets an item's own `opacity` or `scale` replaces the
  // binding on it permanently, and these two have to coexist with bindings
  // that decide which of the mark, the view and the tick is showing.
  property real pulseScale: 1.0
  property real pulseOpacity: 1.0
  property real popScale: 1.0

  readonly property string previewDir: {
    var explicit = Quickshell.env("GLANCE_RUNTIME_DIR")
    if (explicit) return explicit
    var runtime = Quickshell.env("XDG_RUNTIME_DIR")
    return runtime ? runtime + "/glance" : ""
  }
  readonly property string previewUrl: previewDir === "" ? "" : "file://" + previewDir + "/preview.jpg"

  // When the scan ends the pulse is wherever it was in its cycle; it eases
  // home from there rather than jumping, so the hand-off to the verdict is
  // a fade and not a fade with a twitch in it.
  ParallelAnimation {
    id: pulseSettle
    NumberAnimation { target: root; property: "pulseScale"; to: 1; duration: 200; easing.type: Easing.OutCubic }
    NumberAnimation { target: root; property: "pulseOpacity"; to: 1; duration: 200; easing.type: Easing.OutCubic }
  }

  SequentialAnimation {
    running: root.scanning
    loops: Animation.Infinite
    onRunningChanged: if (running) pulseSettle.stop(); else pulseSettle.restart()
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
}
