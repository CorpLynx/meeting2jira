// A soft shadow under a rounded panel, made of a few translucent layers so it looks the same on every
// Qt Quick backend (no shader effects, which the software renderer can't draw). Put it inside the
// panel with z: -1 and anchors.fill: parent; level 1 rests on the page, 2 is raised (hovered).
import QtQuick

Item {
    id: root
    property real radius: theme.radiusMedium
    property real level: 1
    property color tint: theme.mode === "dark" ? "#000000" : "#1B2A4A"
    readonly property real strength: theme.mode === "dark" ? 0.26 : 0.05

    Repeater {
        model: 5
        delegate: Rectangle {
            required property int index
            readonly property real spread: (index + 1) * root.level * 1.25
            x: -spread
            y: -spread + root.level * 2.2 + index * 0.6
            width: root.width + spread * 2
            height: root.height + spread * 2
            radius: root.radius + spread
            color: Qt.alpha(root.tint, root.strength / (index + 2.2))
            Behavior on color { ColorAnimation { duration: 120 } }
        }
    }
    Behavior on level { NumberAnimation { duration: 120 } }
}
