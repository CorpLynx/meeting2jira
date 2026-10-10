// A button in one of four kinds: primary (the main action), secondary, danger, ghost (quiet).
// iconName puts a line icon (see Icon.qml) before the text; with no text it is an icon button.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Button {
    id: root
    property string kind: "secondary"
    property string iconName: ""
    readonly property color fill: kind === "primary" ? theme.accent : (kind === "danger" ? theme.error : theme.surface)
    readonly property color ink: kind === "primary" ? theme.accentText
                                : (kind === "danger" ? theme.errorText : (kind === "ghost" ? theme.textMuted : theme.text))
    implicitHeight: 38
    implicitWidth: Math.max(38, contentItem.implicitWidth + leftPadding + rightPadding)
    leftPadding: text.length > 0 ? 16 : 0
    rightPadding: text.length > 0 ? 16 : 0
    hoverEnabled: true
    font.family: theme.fontFamily
    font.pixelSize: theme.fontSize
    font.weight: Font.DemiBold
    Accessible.name: text.length > 0 ? text : iconName
    scale: down ? 0.97 : 1
    Behavior on scale { NumberAnimation { duration: 80 } }

    contentItem: RowLayout {
        spacing: 8
        Icon {
            visible: root.iconName.length > 0
            name: root.iconName
            size: 18
            color: root.ink
            Layout.alignment: Qt.AlignVCenter
            Layout.leftMargin: root.text.length > 0 ? 0 : 10
            Layout.rightMargin: root.text.length > 0 ? 0 : 10
            opacity: root.enabled ? 1 : 0.45
        }
        Text {
            visible: root.text.length > 0
            text: root.text
            font: root.font
            color: root.ink
            opacity: root.enabled ? 1 : 0.45
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
            Layout.alignment: Qt.AlignVCenter
        }
    }
    background: Rectangle {
        radius: theme.radiusSmall + 2
        opacity: root.enabled ? 1 : 0.6
        color: {
            if (root.kind === "ghost")
                return root.down ? theme.borderSoft : (root.hovered ? theme.surfaceHigh : "transparent")
            if (root.kind === "secondary")
                return root.down ? theme.borderSoft : (root.hovered ? theme.surfaceHigh : theme.surface)
            return root.down ? Qt.darker(root.fill, 1.18) : (root.hovered ? Qt.darker(root.fill, 1.08) : root.fill)
        }
        border.width: root.visualFocus ? 2 : (root.kind === "secondary" ? 1 : 0)
        border.color: root.visualFocus ? theme.focus : theme.border
        Behavior on color { ColorAnimation { duration: 90 } }
    }
}
