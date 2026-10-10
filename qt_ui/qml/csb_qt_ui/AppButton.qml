import QtQuick
import QtQuick.Controls

Button (
    id: root
    property bool primary: false
    property bool danger: false
    implicitHeight: 38
    leftPadding: 16
    rightPadding: 16
    font.family: theme.fontFamily
    font.pixelSize: 13
    font.weight: Font.DemiBold

    contentItem: Text {
        text: root.text
        color: root.enabled
        font: root.font
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
    }

    background: Rectangle {
        radius: theme.radiusSmall
        color: {
            if (!root.enabled) return theme.surface
            if (root.down) return root.danger ? theme.errorSoft : (root.primary ? theme.accentPressed : theme.surfaceHigh)
            if (root.hovered) return root.danger ? theme.errorSoft : (root.primary ? theme.accentHover : theme.surfaceHigh)
            return root.danger ? theme.errorSoft : (root.primary ? theme.accent : theme.surfaceAlt)
        }
        border.width: 1
        border.color: root.danger ? theme.error : (root.primary ? theme.accent : theme.border)
        Behavior on color {ColorAnimation { duration: 100}}
    }
)