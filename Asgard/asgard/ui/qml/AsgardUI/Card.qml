// A titled panel. Children go below the title, in a column.
import QtQuick
import QtQuick.Layouts

Rectangle {
    id: root
    default property alias content: body.data
    property string title: ""
    property string subtitle: ""
    property color accentColor: "transparent"     // an optional stripe along the top

    color: theme.surface
    border.color: theme.borderSoft
    border.width: 1
    radius: theme.radiusMedium
    implicitWidth: layout.implicitWidth + 32
    implicitHeight: layout.implicitHeight + 32

    Rectangle {
        visible: root.accentColor.a > 0
        height: 3
        radius: 2
        color: root.accentColor
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.leftMargin: root.radius
        anchors.rightMargin: root.radius
    }

    ColumnLayout {
        id: layout
        anchors.fill: parent
        anchors.margins: 16
        spacing: 12

        ColumnLayout {
            visible: root.title.length > 0 || root.subtitle.length > 0
            spacing: 3
            Layout.fillWidth: true

            Text {
                visible: root.title.length > 0
                text: root.title
                color: theme.text
                font.family: theme.fontFamily
                font.pixelSize: theme.fontSize + 2
                font.weight: Font.DemiBold
                elide: Text.ElideRight
                Layout.fillWidth: true
                Accessible.role: Accessible.Heading
            }
            Text {
                visible: root.subtitle.length > 0
                text: root.subtitle
                color: theme.textMuted
                font.family: theme.fontFamily
                font.pixelSize: theme.fontSize - 1
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }
        }

        ColumnLayout {
            id: body
            spacing: 10
            Layout.fillWidth: true
            Layout.fillHeight: true
        }
    }
}
