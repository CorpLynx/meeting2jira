// The app list: Apps and Dashboard, then each app's views under its name, Settings at the bottom.
// Built from navigation.items, so a new app's views appear without touching this file.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: root
    color: theme.sidebar

    Rectangle {                       // the hairline between the sidebar and the page
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.bottom: parent.bottom
        width: 1
        color: theme.borderSoft
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.leftMargin: 14
        anchors.rightMargin: 15
        anchors.topMargin: 18
        anchors.bottomMargin: 14
        spacing: 4

        RowLayout {
            spacing: 12
            Layout.fillWidth: true
            Layout.bottomMargin: 16
            Layout.leftMargin: 4
            Rectangle {
                Layout.preferredWidth: 40
                Layout.preferredHeight: 40
                radius: 12
                Accessible.ignored: true
                gradient: Gradient {
                    GradientStop { position: 0.0; color: Qt.lighter(theme.accent, 1.18) }
                    GradientStop { position: 1.0; color: Qt.darker(theme.accent, 1.12) }
                }
                Text {
                    anchors.centerIn: parent
                    text: shell.title.substring(0, 1)
                    color: theme.accentText
                    font.family: theme.fontFamily
                    font.pixelSize: theme.fontSize + 7
                    font.weight: Font.Bold
                }
            }
            ColumnLayout {
                spacing: 0
                Layout.fillWidth: true
                Text {
                    text: shell.title
                    color: theme.text
                    font.family: theme.fontFamily
                    font.pixelSize: theme.fontSize + 4
                    font.weight: Font.Bold
                    elide: Text.ElideRight
                    Layout.fillWidth: true
                }
                Text {
                    text: shell.subtitle
                    color: theme.textMuted
                    font.family: theme.fontFamily
                    font.pixelSize: theme.fontSize - 2
                    elide: Text.ElideRight
                    Layout.fillWidth: true
                }
            }
        }

        ListView {
            id: list
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            spacing: 2
            boundsBehavior: Flickable.StopAtBounds
            model: navigation.items
            Accessible.role: Accessible.List
            Accessible.name: "Pages"
            delegate: Column {
                width: list.width
                Text {
                    visible: modelData.kind === "header"
                    width: parent.width
                    height: visible ? implicitHeight : 0
                    text: modelData.title.toUpperCase()
                    color: themeCtl.appAccents[modelData.app] || theme.textDim
                    font.family: theme.fontFamily
                    font.pixelSize: theme.fontSize - 3
                    font.weight: Font.Bold
                    font.letterSpacing: 1.0
                    topPadding: 18
                    bottomPadding: 6
                    leftPadding: 12
                    Accessible.role: Accessible.Heading
                    Accessible.name: modelData.title
                }
                NavItem {
                    visible: modelData.kind === "page"
                    width: parent.width
                    height: visible ? implicitHeight : 0
                    label: modelData.title
                    iconText: modelData.icon
                    selected: navigation.currentKey === modelData.key
                    accentColor: modelData.app ? (themeCtl.appAccents[modelData.app] || theme.accentInk) : theme.accentInk
                    onClicked: navigation.go(modelData.key)
                }
            }
        }

        NavItem {
            Layout.fillWidth: true
            label: "Settings"
            iconText: "settings"
            selected: navigation.currentKey === "settings"
            onClicked: navigation.go("settings")
        }
        Text {
            text: "Version " + shell.version
            color: theme.textDim
            font.family: theme.fontFamily
            font.pixelSize: theme.fontSize - 3
            Layout.leftMargin: 14
            Layout.topMargin: 6
            Accessible.name: "Asgard version " + shell.version
        }
    }
}
