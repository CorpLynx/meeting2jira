// A small rounded label: a field's kind, a status, "needs fixing".
import QtQuick

Rectangle {
    id: root
    property string text: ""
    property string tone: ""            // "", "accent", "warning" or "error"
    readonly property color ink: tone === "error" ? theme.error : (tone === "warning" ? theme.warning
                                 : (tone === "accent" ? theme.accentInk : theme.textMuted))

    implicitWidth: label.implicitWidth + 16
    implicitHeight: label.implicitHeight + 6
    radius: height / 2
    color: tone === "accent" ? theme.accentSoft : theme.surfaceHigh
    border.width: tone === "error" || tone === "warning" ? 1 : 0
    border.color: ink
    Accessible.role: Accessible.StaticText
    Accessible.name: text

    Text {
        id: label
        anchors.centerIn: parent
        text: root.text
        color: root.ink
        font.family: theme.fontFamily
        font.pixelSize: theme.fontSize - 2
        font.weight: Font.DemiBold
    }
}
