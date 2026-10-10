// A titled panel with a soft shadow. Children go below the title, in a column.
// accentColor puts a small coloured dot before the title (an app's colour, or a warning).
import QtQuick
import QtQuick.Layouts

Rectangle {
    id: root
    default property alias content: body.data
    property string title: ""
    property string subtitle: ""
    property color accentColor: "transparent"
    color: theme.surface
    border.color: theme.borderSoft
    border.width: 1
    radius: theme.radiusLarge
    implicitWidth: layout.implicitWidth + 40
    implicitHeight: layout.implicitHeight + 40

    Elevation {
        z: -1
        anchors.fill: parent
        radius: root.radius
        level: 1
    }

    ColumnLayout {
        id: layout
        anchors.fill: parent
        anchors.margins: 20
        spacing: 14
        ColumnLayout {
            visible: root.title.length > 0 || root.subtitle.length > 0
            spacing: 3
            Layout.fillWidth: true
            RowLayout {
                visible: root.title.length > 0
                spacing: 8
                Layout.fillWidth: true
                Rectangle {
                    visible: root.accentColor.a > 0
                    Layout.preferredWidth: 10
                    Layout.preferredHeight: 10
                    radius: 5
                    color: root.accentColor
                    Accessible.ignored: true
                }
                Text {
                    text: root.title
                    color: theme.text
                    font.family: theme.fontFamily
                    font.pixelSize: theme.fontSize + 3
                    font.weight: Font.DemiBold
                    elide: Text.ElideRight
                    Layout.fillWidth: true
                    Accessible.role: Accessible.Heading
                }
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
