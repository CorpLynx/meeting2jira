// The app list: Dashboard, then each app's views under its name, Settings at the bottom.
// Built from navigation.items, so a new app's views appear without touching this file.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: root
    color: theme.sidebar

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 12
        spacing: 4

        RowLayout {
            spacing: 10
            Layout.fillWidth: true
            Layout.topMargin: 4
            Layout.bottomMargin: 12

            Rectangle {
                Layout.preferredWidth: 36
                Layout.preferredHeight: 36
                radius: theme.radiusSmall
                color: theme.accent
                Accessible.ignored: true
                Text {
                    anchors.centerIn: parent
                    text: shell.title.substring(0, 2)
                    color: theme.accentText
                    font.family: theme.fontFamily
                    font.pixelSize: theme.fontSize + 1
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
                    font.pixelSize: theme.fontSize + 3
                    font.weight: Font.DemiBold
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
                    text: modelData.title.toUpperCase()
                    color: themeCtl.appAccents[modelData.app] || theme.textDim
                    font.family: theme.fontFamily
                    font.pixelSize: theme.fontSize - 3
                    font.weight: Font.Bold
                    font.letterSpacing: 0.8
                    topPadding: 14
                    bottomPadding: 4
                    leftPadding: 12
                    Accessible.role: Accessible.Heading
                    Accessible.name: modelData.title
                }

                NavItem {
                    visible: modelData.kind === "page"
                    width: parent.width
                    label: modelData.title
                    iconText: modelData.icon
                    selected: navigation.currentKey === modelData.key
                    accentColor: modelData.app ? (themeCtl.appAccents[modelData.app] || theme.accentInk) : theme.accentInk
                    onClicked: navigation.go(modelData.key)
                }
            }
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 1
            color: theme.borderSoft
        }

        NavItem {
            Layout.fillWidth: true
            label: "Settings"
            iconText: "St"
            selected: navigation.currentKey === "settings"
            onClicked: navigation.go("settings")
        }
    }
}
