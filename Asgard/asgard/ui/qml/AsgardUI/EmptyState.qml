// "Nothing here yet", with what to do about it and an optional button.
import QtQuick
import QtQuick.Layouts

ColumnLayout {
    id: root
    signal action()
    property string title: "Nothing here yet"
    property string detail: ""
    property string actionText: ""

    spacing: 8

    Text {
        text: root.title
        color: theme.text
        font.family: theme.fontFamily
        font.pixelSize: theme.fontSize + 4
        font.weight: Font.DemiBold
        horizontalAlignment: Text.AlignHCenter
        Layout.alignment: Qt.AlignHCenter
        Accessible.role: Accessible.Heading
    }
    Text {
        visible: root.detail.length > 0
        text: root.detail
        color: theme.textMuted
        font.family: theme.fontFamily
        font.pixelSize: theme.fontSize - 1
        wrapMode: Text.WordWrap
        horizontalAlignment: Text.AlignHCenter
        Layout.maximumWidth: 520
        Layout.alignment: Qt.AlignHCenter
    }
    AppButton {
        visible: root.actionText.length > 0
        text: root.actionText
        kind: "primary"
        Layout.alignment: Qt.AlignHCenter
        Layout.topMargin: 6
        onClicked: root.action()
    }
}
