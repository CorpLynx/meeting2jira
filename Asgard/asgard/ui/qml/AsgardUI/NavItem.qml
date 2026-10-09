// One sidebar entry. Keyboard: Tab to reach it, Enter or Space to open it.
import QtQuick
import QtQuick.Layouts

Rectangle {
    id: root
    signal clicked()
    property string label: ""
    property string iconText: ""
    property bool selected: false
    property color accentColor: theme.accentInk

    implicitHeight: 40
    radius: theme.radiusSmall
    color: selected ? theme.accentSoft : (mouse.containsMouse ? theme.surfaceHigh : "transparent")
    border.width: activeFocus ? 2 : 0
    border.color: theme.focus
    activeFocusOnTab: true

    Accessible.role: Accessible.Button
    Accessible.name: label
    Accessible.description: selected ? "Current page" : ""
    Accessible.onPressAction: root.clicked()
    Keys.onReturnPressed: root.clicked()
    Keys.onEnterPressed: root.clicked()
    Keys.onSpacePressed: root.clicked()

    Behavior on color { ColorAnimation { duration: 90 } }

    Rectangle {
        visible: root.selected
        width: 3
        height: parent.height - 14
        radius: 2
        color: root.accentColor
        anchors.left: parent.left
        anchors.verticalCenter: parent.verticalCenter
    }

    RowLayout {
        anchors.fill: parent
        anchors.leftMargin: 12
        anchors.rightMargin: 12
        spacing: 10

        Rectangle {
            visible: root.iconText.length > 0
            Layout.preferredWidth: 24
            Layout.preferredHeight: 24
            radius: 5
            color: root.selected ? theme.accent : theme.surfaceHigh
            Text {
                anchors.centerIn: parent
                text: root.iconText
                color: root.selected ? theme.accentText : root.accentColor
                font.family: theme.fontFamily
                font.pixelSize: theme.fontSize - 3
                font.weight: Font.Bold
            }
        }

        Text {
            text: root.label
            color: root.selected ? theme.text : theme.textMuted
            font.family: theme.fontFamily
            font.pixelSize: theme.fontSize
            font.weight: root.selected ? Font.DemiBold : Font.Normal
            elide: Text.ElideRight
            Layout.fillWidth: true
        }
    }

    MouseArea {
        id: mouse
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: root.clicked()
    }
}
