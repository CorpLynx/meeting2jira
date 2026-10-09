// A page's title, a line about it, and its actions on the right.
import QtQuick
import QtQuick.Layouts

RowLayout {
    id: root
    default property alias actions: actionRow.data
    property string title: ""
    property string subtitle: ""
    spacing: 12

    ColumnLayout {
        spacing: 2
        Layout.fillWidth: true
        Text {
            text: root.title
            color: theme.text
            font.family: theme.fontFamily
            font.pixelSize: theme.fontSize + 9
            font.weight: Font.Bold
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

    RowLayout {
        id: actionRow
        spacing: 8
        Layout.alignment: Qt.AlignTop
    }
}
