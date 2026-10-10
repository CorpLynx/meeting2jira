// Short messages in the corner. Errors stay 12 s, others 6 s; click one to dismiss it.
import QtQuick
import QtQuick.Layouts

Column {
    id: root
    width: 400
    spacing: 10
    function show(message, kind) {
        toasts.append({ "message": message, "kind": kind || "info" })
        if (toasts.count > 4)
            toasts.remove(0)
    }
    ListModel { id: toasts }
    Repeater {
        model: toasts
        delegate: Rectangle {
            id: toast
            required property int index
            required property string message
            required property string kind
            readonly property color tint: kind === "error" ? theme.error : (kind === "warning" ? theme.warning : theme.accentInk)
            width: root.width
            height: row.implicitHeight + 28
            radius: theme.radiusMedium + 2
            color: theme.surface
            border.color: theme.borderSoft
            border.width: 1
            opacity: 0
            Accessible.role: Accessible.AlertMessage
            Accessible.name: message
            Component.onCompleted: opacity = 1
            Behavior on opacity { NumberAnimation { duration: 160 } }
            Elevation {
                z: -1
                anchors.fill: parent
                radius: toast.radius
                level: 2
            }
            RowLayout {
                id: row
                anchors.fill: parent
                anchors.margins: 14
                spacing: 12
                Rectangle {
                    Layout.preferredWidth: 30
                    Layout.preferredHeight: 30
                    Layout.alignment: Qt.AlignTop
                    radius: 15
                    color: Qt.alpha(toast.tint, theme.mode === "dark" ? 0.22 : 0.13)
                    Accessible.ignored: true
                    Icon {
                        anchors.centerIn: parent
                        name: toast.kind === "info" ? "check" : "alert"
                        size: 18
                        color: toast.tint
                    }
                }
                Text {
                    text: toast.message
                    color: theme.text
                    font.family: theme.fontFamily
                    font.pixelSize: theme.fontSize
                    wrapMode: Text.WordWrap
                    Layout.fillWidth: true
                    Layout.alignment: Qt.AlignVCenter
                }
            }
            MouseArea {
                anchors.fill: parent
                onClicked: toasts.remove(toast.index)
            }
            Timer {
                interval: toast.kind === "error" ? 12000 : 6000
                running: true
                onTriggered: toasts.remove(toast.index)
            }
        }
    }
}
