// Short messages in the corner. Errors stay 12 s, others 6 s; click one to dismiss it.
import QtQuick

Column {
    id: root
    width: 380
    spacing: 8

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
            readonly property color tint: kind === "error" ? theme.error : (kind === "warning" ? theme.warning : theme.accent)

            width: root.width
            height: body.implicitHeight + 24
            radius: theme.radiusSmall
            color: theme.surface
            border.color: tint
            border.width: 1
            Accessible.role: Accessible.AlertMessage
            Accessible.name: message

            Rectangle {
                width: 4
                radius: 2
                color: toast.tint
                anchors.left: parent.left
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                anchors.margins: 6
            }
            Text {
                id: body
                text: toast.message
                color: theme.text
                font.family: theme.fontFamily
                font.pixelSize: theme.fontSize - 1
                wrapMode: Text.WordWrap
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                anchors.leftMargin: 18
                anchors.rightMargin: 12
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
