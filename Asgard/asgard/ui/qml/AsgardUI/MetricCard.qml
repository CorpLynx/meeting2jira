// A number with a label, for the Dashboard. Clickable when it leads somewhere.
import QtQuick
import QtQuick.Layouts

Rectangle {
    id: root
    signal clicked()
    property string label: ""
    property string value: "-"
    property string detail: ""
    property string tone: ""            // "", "warning" or "error"
    property bool interactive: true
    readonly property color toneColor: tone === "error" ? theme.error : (tone === "warning" ? theme.warning : theme.accentInk)
    color: mouse.pressed && interactive ? theme.borderSoft : theme.surfaceHigh
    border.color: activeFocus ? theme.focus : (mouse.containsMouse && interactive ? Qt.alpha(theme.accent, 0.6) : "transparent")
    border.width: activeFocus ? 2 : 1
    radius: theme.radiusMedium
    implicitWidth: 200
    implicitHeight: 108
    activeFocusOnTab: interactive
    Accessible.role: interactive ? Accessible.Button : Accessible.StaticText
    Accessible.name: value + " " + label + (detail.length ? ". " + detail : "")
    Accessible.onPressAction: if (interactive) root.clicked()
    Keys.onReturnPressed: if (interactive) root.clicked()
    Keys.onSpacePressed: if (interactive) root.clicked()
    Behavior on color { ColorAnimation { duration: 100 } }

    Rectangle {
        visible: root.tone.length > 0
        width: 4
        radius: 2
        color: root.toneColor
        anchors.left: parent.left
        anchors.top: parent.top
        anchors.bottom: parent.bottom
        anchors.margins: 14
        anchors.leftMargin: 8
    }
    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 18
        anchors.leftMargin: root.tone.length > 0 ? 26 : 18
        spacing: 4
        Text {
            text: root.value
            color: root.tone.length ? root.toneColor : theme.text
            font.family: theme.fontFamily
            font.pixelSize: theme.fontSize + 14
            font.weight: Font.Bold
            elide: Text.ElideRight
            Layout.fillWidth: true
        }
        Text {
            text: root.label
            color: theme.text
            font.family: theme.fontFamily
            font.pixelSize: theme.fontSize
            font.weight: Font.DemiBold
            elide: Text.ElideRight
            Layout.fillWidth: true
        }
        Text {
            visible: root.detail.length > 0
            text: root.detail
            color: theme.textMuted
            font.family: theme.fontFamily
            font.pixelSize: theme.fontSize - 1
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
