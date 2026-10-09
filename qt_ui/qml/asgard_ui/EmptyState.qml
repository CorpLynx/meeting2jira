import QtQuick
import QtQuick.Layouts

ColumnLayout {
    id: root
    property string title: "Nothing here yet"
    property string detail: ""
    spacing: 6
    Layout.alignment: Qt.AlignHCenter | Qt.AlignVCenter
    Text {
        text: root.title
        color: theme.text
        font.family: theme.fontFamily
        font.pixelSize: 17
        font.weight: Font.DemiBold
        Layout.alignment: Qt.AlignHCenter
    }
    Text {
        text: root.detail
        color: theme.textMuted
        font.family: theme.fontFamily
        font.pixelSize: 12
        wrapMode: Text.WordWrap
        horizontalAlignment: Text.AlignHCenter
        Layout.maximumWidth: 520
        Layout.alignment: Qt.AlignHCenter
    }
}