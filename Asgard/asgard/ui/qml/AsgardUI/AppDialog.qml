// A modal dialog: a title, whatever you put inside it, and buttons along the bottom right.
//   AppDialog { title: "About"; Text { text: "..." }; buttons: [ AppButton { text: "Close"; onClicked: ... } ] }
// Escape closes it. The page behind is dimmed and can't be clicked while it is open.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Popup {
    id: root
    default property alias body: bodyColumn.data
    property alias buttons: buttonRow.data
    property string title: ""
    property int maxWidth: 520

    modal: true
    focus: true
    closePolicy: Popup.CloseOnEscape
    anchors.centerIn: Overlay.overlay
    width: Math.min(maxWidth, (Overlay.overlay ? Overlay.overlay.width : maxWidth) - 48)
    padding: 24
    enter: Transition {
        NumberAnimation { property: "opacity"; from: 0; to: 1; duration: 120 }
    }
    Overlay.modal: Rectangle { color: Qt.alpha("#000000", theme.mode === "dark" ? 0.6 : 0.4) }
    background: Rectangle {
        radius: theme.radiusLarge
        color: theme.surface
        border.color: theme.borderSoft
        border.width: 1
        Elevation {
            z: -1
            anchors.fill: parent
            radius: parent.radius
            level: 2
        }
    }
    contentItem: ColumnLayout {
        spacing: 14
        Text {
            visible: root.title.length > 0
            text: root.title
            color: theme.text
            font.family: theme.fontFamily
            font.pixelSize: theme.fontSize + 5
            font.weight: Font.DemiBold
            wrapMode: Text.WordWrap
            Layout.fillWidth: true
            Accessible.role: Accessible.Heading
        }
        ColumnLayout {
            id: bodyColumn
            spacing: 10
            Layout.fillWidth: true
        }
        RowLayout {
            id: buttonRow
            spacing: 8
            Layout.alignment: Qt.AlignRight
            Layout.topMargin: 6
        }
    }
}
