import QtQuick
import QtQuick.Layouts


Rectangle {
    id: root
    signal clicked()
    property string label: ""
    property string value: "-"
    property string detail: ""
    property bool interactive: true
    color: mouse.containsMouse && interactive ? theme.surfaceHigh : theme.surface
    border.color: mouse.containsMouse && interactive ? theme.accent : theme.borderSoft
    border.width: 1
    radius: theme.radiusMedium
    implicitHeight: 112

    Behavior on color { ColorAnimation { duration: 100 } }
    Behavior on border.color { ColorAnimation { duration: 100 } }

    ColumnLayout {
        anchors.fill: parent
        anchor.margins: 16
        spacing: 4
        Text {
            text: root.value
            color: theme.text
            font.family: theme.fontFamily
            font.pixelSize: 28
            font.weight: Font.Bold
            Layout.fillWidth: true
        }
        Text {
            text: root.label
            color: theme.textMuted
            font.family: theme.fontFamily
            font.pixelSize: 12
            font.weight: Font.DemiBold
            Layout.fillWidth: true
        }
        Text {
            visible: root.detail.length > 0
            text: root.detail
            color: theme.textDim
            font.family: theme.fontFamily
            font.pixelSize: 11
            elide: Text.ElideRight
            Layout.fillWidth: true
        }
    }

    MouseArea {
        id: mouse
        anchors.fill: parent
        enabled: root.interactive
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: root.clicked()
    }
}