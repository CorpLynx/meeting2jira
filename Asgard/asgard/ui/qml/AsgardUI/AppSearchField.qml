// A rounded search box. `text` is what is typed; accepted() on Enter, cleared() on Escape.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: root
    property alias text: field.text
    property string placeholder: "Search"
    property string label: "Search"
    signal accepted()
    signal cleared()
    signal downPressed()
    function focusField() { field.forceActiveFocus() }
    // Type-to-search: the page passes the key the person pressed, and typing carries on from there.
    function append(typed) {
        field.forceActiveFocus()
        field.text = field.text + typed
        field.cursorPosition = field.text.length
    }

    implicitWidth: 300
    implicitHeight: 40
    radius: height / 2
    color: field.activeFocus ? theme.surface : theme.surfaceHigh
    border.width: field.activeFocus ? 2 : 1
    border.color: field.activeFocus ? theme.focus : theme.borderSoft
    Behavior on color { ColorAnimation { duration: 100 } }

    RowLayout {
        anchors.fill: parent
        anchors.leftMargin: 14
        anchors.rightMargin: 8
        spacing: 8
        Icon {
            name: "search"
            size: 18
            color: theme.textDim
            Layout.alignment: Qt.AlignVCenter
        }
        TextField {
            id: field
            Layout.fillWidth: true
            Layout.fillHeight: true
            placeholderText: root.placeholder
            placeholderTextColor: theme.textDim
            color: theme.text
            selectionColor: theme.accent
            selectedTextColor: theme.accentText
            font.family: theme.fontFamily
            font.pixelSize: theme.fontSize
            verticalAlignment: TextInput.AlignVCenter
            leftPadding: 0
            rightPadding: 0
            background: Item {}
            Accessible.name: root.label
            Accessible.role: Accessible.EditableText
            onAccepted: root.accepted()
            Keys.onDownPressed: root.downPressed()
            Keys.onEscapePressed: {
                text = ""
                root.cleared()
            }
        }
        AppButton {
            visible: field.text.length > 0
            kind: "ghost"
            iconName: "close"
            implicitHeight: 28
            implicitWidth: 28
            Accessible.name: "Clear search"
            onClicked: {
                field.text = ""
                root.cleared()
                field.forceActiveFocus()
            }
        }
    }
}
