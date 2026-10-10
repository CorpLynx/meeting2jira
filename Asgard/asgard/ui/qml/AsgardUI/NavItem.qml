// One sidebar entry. Keyboard: Tab to reach it, Enter or Space to open it.
// iconText is either the name of a line icon (apps, dashboard, settings) or up to two letters.
import QtQuick
import QtQuick.Layouts

Rectangle {
    id: root
    signal clicked()
    property string label: ""
    property string iconText: ""
    property bool selected: false
    property color accentColor: theme.accentInk
    readonly property bool lineIcon: ["apps", "home", "dashboard", "settings"].indexOf(iconText) >= 0
    readonly property color ink: selected ? theme.accentInk : theme.textMuted

    implicitHeight: 42
    radius: theme.radiusSmall + 2
    color: selected ? theme.accentSoft : (mouse.pressed ? theme.borderSoft : (mouse.containsMouse ? theme.surfaceHigh : "transparent"))
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

    RowLayout {
        anchors.fill: parent
        anchors.leftMargin: 12
        anchors.rightMargin: 12
        spacing: 12
        Icon {
            visible: root.lineIcon
            name: root.iconText
            size: 20
            color: root.ink
            Layout.alignment: Qt.AlignVCenter
        }
        Rectangle {
            visible: !root.lineIcon && root.iconText.length > 0
            Layout.preferredWidth: 24
            Layout.preferredHeight: 24
            radius: 7
            color: root.selected ? theme.accent : Qt.alpha(root.accentColor, 0.14)
            Accessible.ignored: true
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
