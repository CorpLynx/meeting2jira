// One entry in an AppMenu: an optional line icon, the text, and a shortcut hint on the right.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

MenuItem {
    id: root
    property string iconName: ""
    property string hint: ""
    property bool danger: false
    readonly property color ink: !enabled ? theme.textDim : (danger ? theme.error : theme.text)
    implicitHeight: visible ? 38 : 0
    leftPadding: 10
    rightPadding: 12
    font.family: theme.fontFamily
    font.pixelSize: theme.fontSize
    contentItem: RowLayout {
        spacing: 10
        Icon {
            name: root.iconName
            size: 18
            color: root.ink
            opacity: root.enabled ? 1 : 0.6
            Layout.alignment: Qt.AlignVCenter
        }
        Text {
            text: root.text
            font: root.font
            color: root.ink
            elide: Text.ElideRight
            verticalAlignment: Text.AlignVCenter
            Layout.fillWidth: true
        }
        Text {
            visible: root.hint.length > 0
            text: root.hint
            font.family: theme.fontFamily
            font.pixelSize: theme.fontSize - 2
            color: theme.textDim
            Layout.alignment: Qt.AlignVCenter
        }
    }
    background: Rectangle {
        radius: theme.radiusSmall
        color: root.highlighted && root.enabled ? (root.danger ? Qt.alpha(theme.error, 0.12) : theme.accentSoft) : "transparent"
    }
}
