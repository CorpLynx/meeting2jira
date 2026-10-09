// A button in one of four kinds: primary (the main action), secondary, danger, ghost (quiet).
import QtQuick
import QtQuick.Controls

Button {
    id: root
    property string kind: "secondary"

    readonly property color fill: kind === "primary" ? theme.accent : (kind === "danger" ? theme.error : theme.surface)
    readonly property color ink: kind === "primary" ? theme.accentText
                                : (kind === "danger" ? theme.errorText : (kind === "ghost" ? theme.accentInk : theme.text))

    implicitHeight: 34
    leftPadding: 14
    rightPadding: 14
    hoverEnabled: true
    font.family: theme.fontFamily
    font.pixelSize: theme.fontSize
    font.weight: Font.DemiBold
    Accessible.name: text

    contentItem: Text {
        text: root.text
        font: root.font
        color: root.ink
        opacity: root.enabled ? 1 : 0.45
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
    }

    background: Rectangle {
        radius: theme.radiusSmall
        opacity: root.enabled ? 1 : 0.6
        color: root.kind === "ghost"
               ? (root.hovered ? theme.surfaceHigh : "transparent")
               : (root.down ? Qt.darker(root.fill, 1.15) : (root.hovered ? Qt.darker(root.fill, 1.06) : root.fill))
        border.width: root.visualFocus ? 2 : (root.kind === "secondary" ? 1 : 0)
        border.color: root.visualFocus ? theme.focus : theme.border
    }
}
