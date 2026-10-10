// Odin: Assigned to Me, as Muninn last saw Jira (the daily run, or Read Jira into Muninn, refreshes it).
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import AsgardUI

ScrollPage {
    id: page
    property var bridge: null
    property var rows: []
    property string error: ""
    property bool includeDone: false

    function reload() {
        if (!bridge)
            return
        var r = bridge.call("issues", [includeDone])
        if (r && r.error) {
            error = r.error
            rows = []
            return
        }
        error = ""
        rows = r.value || []
    }
    function tone(category) {
        return { "done": "", "in_progress": "accent", "todo": "warning" }[category] || ""
    }
    onBridgeChanged: reload()
    onIncludeDoneChanged: reload()

    PageHeader {
        Layout.fillWidth: true
        title: "My issues"
        subtitle: "Jira issues assigned to you now, newest change first within each status."
        CheckBox {
            text: "Include done"
            checked: page.includeDone
            onToggled: page.includeDone = checked
            font.family: theme.fontFamily
            font.pixelSize: theme.fontSize
        }
        AppButton {
            text: "Reload"
            kind: "ghost"
            onClicked: page.reload()
        }
    }

    Card {
        visible: page.error !== ""
        Layout.fillWidth: true
        title: "Muninn couldn't be read"
        subtitle: page.error
        accentColor: theme.error
    }

    EmptyState {
        visible: page.error === "" && page.rows.length === 0
        Layout.fillWidth: true
        title: "Nothing assigned to you yet"
        detail: "Odin reads your issues from Jira in the daily run, or when you Read Jira into Muninn on Today."
    }

    Card {
        visible: page.rows.length > 0
        Layout.fillWidth: true
        title: page.rows.length + " issue(s)"

        Repeater {
            model: page.rows
            delegate: RowLayout {
                id: row
                required property var modelData
                Layout.fillWidth: true
                spacing: 10

                AppButton {
                    text: row.modelData.key
                    kind: "ghost"
                    onClicked: Qt.openUrlExternally(row.modelData.url)
                    Accessible.name: "Open " + row.modelData.key + " in Jira"
                }
                Text {
                    text: row.modelData.summary
                    color: theme.text
                    font.family: theme.fontFamily
                    font.pixelSize: theme.fontSize
                    elide: Text.ElideRight
                    Layout.fillWidth: true
                }
                Pill { text: row.modelData.type }
                Pill { text: row.modelData.status; tone: page.tone(row.modelData.category) }
                Text {
                    text: row.modelData.updated
                    color: theme.textMuted
                    font.family: theme.monoFamily
                    font.pixelSize: theme.fontSize - 1
                    Layout.preferredWidth: 130
                }
            }
        }
    }
}
