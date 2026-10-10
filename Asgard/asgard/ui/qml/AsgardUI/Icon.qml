// A small line icon, drawn from a few shapes on a 24 x 24 grid so it stays sharp at any size and takes
// any colour. Names: apps, dashboard, settings, search, refresh, more, check, alert, external, folder,
// file, play, close, plus, pencil, info, trash, home. Unknown names draw nothing.
// Drawn with Canvas (part of Qt Quick itself), so no extra Qt module has to ship for icons. The canvas
// is made twice as big and shown at half scale, which keeps the lines crisp on high-DPI screens.
import QtQuick

Item {
    id: root
    property string name: ""
    property color color: theme.text
    property real size: 20
    property real weight: 1.9
    readonly property real ratio: 2

    width: size
    height: size
    Accessible.ignored: true

    // p: a line through points (close: join the ends, fill: colour it in), c: a circle (fill: solid),
    // r: a rounded square [x, y, width, height, radius], a: an arc [centre x, centre y, radius, from, to].
    readonly property var glyphs: ({
        "apps": [{ r: [4, 4, 6, 6, 1.6] }, { r: [14, 4, 6, 6, 1.6] }, { r: [4, 14, 6, 6, 1.6] }, { r: [14, 14, 6, 6, 1.6] }],
        "home": [{ p: [[3.5, 11], [12, 4], [20.5, 11]] }, { p: [[6, 9.5], [6, 20], [18, 20], [18, 9.5]] }],
        "dashboard": [{ p: [[6, 20], [6, 11]] }, { p: [[12, 20], [12, 5]] }, { p: [[18, 20], [18, 14]] }],
        "settings": [{ p: [[4, 7], [12.5, 7]] }, { p: [[17.5, 7], [20, 7]] }, { c: [15, 7, 2.5] },
                     { p: [[4, 17], [6.5, 17]] }, { p: [[11.5, 17], [20, 17]] }, { c: [9, 17, 2.5] }],
        "search": [{ c: [11, 11, 6.5] }, { p: [[16, 16], [20.5, 20.5]] }],
        "refresh": [{ a: [12, 12, 7.5, -1.1, 3.4] }, { p: [[16.5, 2.8], [16.5, 7], [12.3, 7]] }],
        "more": [{ c: [5, 12, 1.5], fill: true }, { c: [12, 12, 1.5], fill: true }, { c: [19, 12, 1.5], fill: true }],
        "check": [{ p: [[5, 12.5], [9.5, 17], [19, 7.5]] }],
        "alert": [{ p: [[12, 3.8], [21.2, 20], [2.8, 20]], close: true }, { p: [[12, 10], [12, 14.5]] },
                  { c: [12, 17.3, 0.9], fill: true }],
        "external": [{ p: [[14, 4], [20, 4], [20, 10]] }, { p: [[20, 4], [11, 13]] },
                     { p: [[10, 6], [5, 6], [5, 19], [18, 19], [18, 14]] }],
        "folder": [{ p: [[3, 7], [9, 7], [11, 9], [21, 9], [21, 19], [3, 19]], close: true }],
        "file": [{ p: [[6, 3], [14, 3], [19, 8], [19, 21], [6, 21]], close: true }, { p: [[14, 3], [14, 8], [19, 8]] },
                 { p: [[9, 13], [16, 13]] }, { p: [[9, 17], [16, 17]] }],
        "play": [{ p: [[7.5, 5], [19, 12], [7.5, 19]], close: true, fill: true }],
        "close": [{ p: [[6, 6], [18, 18]] }, { p: [[18, 6], [6, 18]] }],
        "plus": [{ p: [[12, 5], [12, 19]] }, { p: [[5, 12], [19, 12]] }],
        "pencil": [{ p: [[4, 20], [4, 16], [16, 4], [20, 8], [8, 20]], close: true }, { p: [[13, 7], [17, 11]] }],
        "info": [{ c: [12, 12, 9] }, { p: [[12, 11], [12, 17]] }, { c: [12, 7.6, 0.9], fill: true }],
        "trash": [{ p: [[4, 7], [20, 7]] }, { p: [[10, 7], [10, 4], [14, 4], [14, 7]] },
                  { p: [[6, 7], [7, 20], [17, 20], [18, 7]] }, { p: [[10, 11], [10, 16]] }, { p: [[14, 11], [14, 16]] }]
    })

    onNameChanged: canvas.requestPaint()
    onColorChanged: canvas.requestPaint()
    onWeightChanged: canvas.requestPaint()

    Canvas {
        id: canvas
        width: root.size * root.ratio
        height: root.size * root.ratio
        scale: 1 / root.ratio
        transformOrigin: Item.TopLeft
        renderTarget: Canvas.Image
        antialiasing: true
        smooth: true
        onWidthChanged: requestPaint()
        onPaint: {
            var ctx = getContext("2d")
            ctx.reset()
            ctx.clearRect(0, 0, width, height)
            var ops = root.glyphs[root.name]
            if (!ops)
                return
            var k = width / 24
            ctx.scale(k, k)
            ctx.lineWidth = root.weight
            ctx.lineCap = "round"
            ctx.lineJoin = "round"
            ctx.strokeStyle = root.color
            ctx.fillStyle = root.color
            for (var i = 0; i < ops.length; i++) {
                var op = ops[i]
                ctx.beginPath()
                if (op.p) {
                    ctx.moveTo(op.p[0][0], op.p[0][1])
                    for (var j = 1; j < op.p.length; j++)
                        ctx.lineTo(op.p[j][0], op.p[j][1])
                    if (op.close)
                        ctx.closePath()
                } else if (op.c) {
                    ctx.arc(op.c[0], op.c[1], op.c[2], 0, Math.PI * 2)
                } else if (op.a) {
                    ctx.arc(op.a[0], op.a[1], op.a[2], op.a[3], op.a[4])
                } else if (op.r) {
                    var x = op.r[0], y = op.r[1], w = op.r[2], h = op.r[3], r = op.r[4]
                    ctx.moveTo(x + r, y)
                    ctx.arcTo(x + w, y, x + w, y + h, r)
                    ctx.arcTo(x + w, y + h, x, y + h, r)
                    ctx.arcTo(x, y + h, x, y, r)
                    ctx.arcTo(x, y, x + w, y, r)
                    ctx.closePath()
                }
                if (op.fill)
                    ctx.fill()
                ctx.stroke()
            }
        }
    }
}
