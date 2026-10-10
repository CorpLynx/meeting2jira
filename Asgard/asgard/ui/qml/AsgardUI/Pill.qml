// A small rounded label: a field's kind, a status, "needs fixing". Tones: "" (quiet), accent, success,
// warning, error, muted. With dot: true a coloured dot comes first (used for "Running").
import QtQuick
import QtQuick.Layouts

Rectangle {
    id: root
    property string text: ""
    property string tone: ""
    property bool dot: false
    readonly property color ink: tone === "error" ? theme.error
                                 : (tone === "warning" ? theme.warning
                                 : (tone === "success" ? theme.success
                                 : (tone === "accent" ? theme.accentInk : theme.textMuted)))
    implicitWidth: row.implicitWidth + 18
    implicitHeight: row.implicitHeight + 8
    radius: height / 2
    color: tone === "accent" ? theme.accentSoft
           : (tone === "success" || tone === "warning" || tone === "error") ? Qt.alpha(ink, theme.mode === "dark" ? 0.18 : 0.12)
           : theme.surfaceHigh
    border.width: tone === "error" || tone === "warning" ? 1 : 0
    border.color: Qt.alpha(ink, 0.45)
    Accessible.role: Accessible.StaticText
    Accessible.name: text

    RowLayout {
        id: row
        anchors.centerIn: parent
        spacing: 6
        Rectangle {
            visible: root.dot
            Layout.preferredWidth: 7
            Layout.preferredHeight: 7
            radius: 4
            color: root.ink
        }
        Text {
            text: root.text
            color: root.ink
            font.family: theme.fontFamily
            font.pixelSize: theme.fontSize - 2
            font.weight: Font.DemiBold
        }
    }
}
