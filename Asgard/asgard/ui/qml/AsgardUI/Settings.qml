// Shared settings: light or dark, text size, each app's colour, and where each app's files are.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ScrollPage {
    id: page

    PageHeader {
        Layout.fillWidth: true
        title: "Settings"
        subtitle: "Changes apply straight away and are saved in your UI preferences."
    }

    Card {
        visible: prefs.warnings.length > 0
        Layout.fillWidth: true
        title: "Needs attention"
        accentColor: theme.warning
        Repeater {
            model: prefs.warnings
            delegate: Text {
                required property string modelData
                text: modelData
                color: theme.text
                font.family: theme.fontFamily
                font.pixelSize: theme.fontSize - 1
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }
        }
    }

    Card {
        Layout.fillWidth: true
        title: "Appearance"
        subtitle: "Match Windows follows the light or dark setting in Windows."

        RowLayout {
            spacing: 8
            Repeater {
                model: [{ "id": "system", "name": "Match Windows" }, { "id": "light", "name": "Light" },
                        { "id": "dark", "name": "Dark" }]
                delegate: AppButton {
                    required property var modelData
                    text: modelData.name
                    kind: prefs.mode === modelData.id ? "primary" : "secondary"
                    Accessible.checkable: true
                    Accessible.checked: prefs.mode === modelData.id
                    onClicked: prefs.setMode(modelData.id)
                }
            }
        }

        RowLayout {
            spacing: 10
            Layout.topMargin: 6
            Text {
                text: "Text size"
                color: theme.text
                font.family: theme.fontFamily
                font.pixelSize: theme.fontSize
            }
            SpinBox {
                id: sizeBox
                from: 10
                to: 20
                value: prefs.fontSize
                editable: true
                Accessible.name: "Text size in pixels"
                onValueModified: prefs.setFontSize(value)
            }
            Text {
                text: "pixels (default 13)"
                color: theme.textMuted
                font.family: theme.fontFamily
                font.pixelSize: theme.fontSize - 1
            }
        }
    }

    Card {
        Layout.fillWidth: true
        title: "Colours"
        subtitle: "Each app has its own accent colour. Type any #RRGGBB colour and press Apply. Text drawn in "
                  + "that colour is adjusted if needed so it stays readable."

        Repeater {
            model: prefs.appearance
            delegate: RowLayout {
                id: row
                required property var modelData
                property string problem: ""
                Layout.fillWidth: true
                spacing: 12

                Rectangle {
                    Layout.preferredWidth: 30
                    Layout.preferredHeight: 30
                    radius: theme.radiusSmall
                    color: row.modelData.accent
                    border.color: theme.border
                    border.width: 1
                    Accessible.ignored: true
                }

                ColumnLayout {
                    spacing: 1
                    Layout.fillWidth: true
                    Text {
                        text: row.modelData.name + (row.modelData.custom ? "  (your colour)" : "")
                        color: theme.text
                        font.family: theme.fontFamily
                        font.pixelSize: theme.fontSize
                        font.weight: Font.DemiBold
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                    }
                    Text {
                        visible: text.length > 0
                        text: row.modelData.detail
                        color: theme.textMuted
                        font.family: theme.fontFamily
                        font.pixelSize: theme.fontSize - 2
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                    }
                    Text {
                        visible: row.problem.length > 0
                        text: row.problem
                        color: theme.error
                        font.family: theme.fontFamily
                        font.pixelSize: theme.fontSize - 2
                        wrapMode: Text.WordWrap
                        Layout.fillWidth: true
                    }
                }

                TextField {
                    id: hex
                    text: row.modelData.accent
                    Layout.preferredWidth: 110
                    font.family: theme.monoFamily
                    validator: RegularExpressionValidator { regularExpression: /#?[0-9A-Fa-f]{0,6}/ }
                    Accessible.name: row.modelData.name + " accent colour"
                    onAccepted: row.problem = prefs.setAccent(row.modelData.app, text)
                }
                AppButton {
                    text: "Apply"
                    onClicked: row.problem = prefs.setAccent(row.modelData.app, hex.text)
                }
                AppButton {
                    text: "Reset"
                    kind: "ghost"
                    enabled: row.modelData.custom
                    Accessible.description: "Back to " + row.modelData["default"]
                    onClicked: {
                        row.problem = ""
                        prefs.resetAccent(row.modelData.app)
                    }
                }
            }
        }
    }

    Card {
        Layout.fillWidth: true
        title: "Settings files"
        subtitle: "Each app keeps its settings in a file in Asgard's data folder. Open one to edit it."

        Repeater {
            model: prefs.files
            delegate: RowLayout {
                id: fileRow
                required property var modelData
                Layout.fillWidth: true
                spacing: 8

                ColumnLayout {
                    spacing: 1
                    Layout.fillWidth: true
                    Text {
                        text: fileRow.modelData.label
                        color: theme.text
                        font.family: theme.fontFamily
                        font.pixelSize: theme.fontSize
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                    }
                    Text {
                        text: fileRow.modelData.path
                        color: theme.textDim
                        font.family: theme.monoFamily
                        font.pixelSize: theme.fontSize - 2
                        elide: Text.ElideMiddle
                        Layout.fillWidth: true
                    }
                }
                AppButton {
                    text: "Open"
                    onClicked: shell.openPath(fileRow.modelData.path)
                }
                AppButton {
                    text: "Show folder"
                    kind: "ghost"
                    onClicked: shell.openFolder(fileRow.modelData.path)
                }
            }
        }
    }

    Card {
        Layout.fillWidth: true
        title: "About"
        Text {
            text: "Asgard " + shell.version
            color: theme.text
            font.family: theme.fontFamily
            font.pixelSize: theme.fontSize
        }
        RowLayout {
            spacing: 8
            Layout.fillWidth: true
            Text {
                text: "Data folder: " + shell.dataFolder
                color: theme.textMuted
                font.family: theme.fontFamily
                font.pixelSize: theme.fontSize - 1
                elide: Text.ElideMiddle
                Layout.fillWidth: true
            }
            AppButton {
                text: "Open data folder"
                kind: "ghost"
                onClicked: shell.openPath(shell.dataFolder)
            }
        }
    }
}
