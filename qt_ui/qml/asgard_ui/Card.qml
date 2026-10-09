import QtQuick
import QtQuick.Layouts


Rectangle {
    id: root
    property alias content: contentLayout.data
    property string title: ""
    property string subtitle: ""
    color: theme.surface
    border.color: theme.borderSoft
    border.width: 1
    radius: theme.radiusMedium
    implicitHeight: content.Layout.implicitHeight + 32

    ColumnLayout {
        id: contentLayout
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
                font.pixelSize: 15
                font.weight: Font.DemiBold
            }
            Text {
                visible: root.subtitle.length > 0
                text: root.subtitle
                color: theme.textMuted
                font.family: theme.fontFamily
                font.pixelSize: 12
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }
        }
    }
}