import QtQuick
import QtQuick.Layouts

Rectangle {
    id: root
    signal clicked()
    property string label: ""
    property string iconText: ""
    property bool selected: false
    implicitHeight: 44
    radius: theme.radiusSmall
    color: selected ? theme.accentSoft : (mouse.containsMouse ? theme.surfaceHigh : "transparent")
    border.width: selected ? 1 : 0
    border.color: selected ? theme.accent : "transparent"

    Behavior on color { ColorAnimation { duration: 90 } }

    RowLayout {
        acnhors.fill: parent
        anchors.leftMargin: 13
        anchor.rightMargin: 13
        spacing: 10
        Text {
            text: root.label
            color: root.selected ? theme.accent
        }
    }
}