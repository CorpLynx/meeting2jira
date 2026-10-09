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

    color: mouse.containsMouse && interactive ? theme.surfaceHigh : theme.surface
    border.color: activeFocus ? theme.focus : (mouse.containsMouse && interactive ? theme.accent : theme.borderSoft)
    border.width: activeFocus ? 2 : 1
    radius: theme.radiusMedium
    implicitWidth: 200
    implicitHeight: 104
    activeFocusOnTab: interactive

    Accessible.role: interactive ? Accessible.Button : Accessible.StaticText
    Accessible.name: value + " " + label + (detail.length ? ". " + detail : "")
    Accessible.onPressAction: if (interactive) root.clicked()
    Keys.onReturnPressed: if (interactive) root.clicked()
    Keys.onSpacePressed: if (interactive) root.clicked()

    Behavior on color { ColorAnimation { duration: 100 } }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 16
        spacing: 4

        Text {
            text: root.value
            color: root.tone.length ? root.toneColor : theme.text
            font.family: theme.fontFamily
            font.pixelSize: theme.fontSize + 13
            font.weight: Font.Bold
            elide: Text.ElideRight
            Layout.fillWidth: true
        }
        Text {
            text: root.label
            color: theme.textMuted
            font.family: theme.fontFamily
            font.pixelSize: theme.fontSize - 1
            font.weight: Font.DemiBold
            elide: Text.ElideRight
            Layout.fillWidth: true
        }
        Text {
            visible: root.detail.length > 0
            text: root.detail
            color: theme.textDim
            font.family: theme.fontFamily
            font.pixelSize: theme.fontSize - 2
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
