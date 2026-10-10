// Odin: the meeting sub-tasks it made, newest first, and whether each meeting's time is in Jira.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import AsgardUI

ScrollPage {
    id: page
    property var bridge: null
    property var rows: []
    property string error: ""

    function reload() {
        if (!bridge)
            return
        var r = bridge.call("meetings", [60])
        if (r && r.error) {
            error = r.error
            rows = []
            return
        }
        error = ""
        rows = r.value || []
    }
    function worklogText(state) {
        return { "posted": "logged", "sending": "waiting for Jira", "failed": "refused", "wanted": "to log" }[state] || ""
    }
    function worklogTone(state) {
        return { "posted": "accent", "sending": "warning", "failed": "error", "wanted": "warning" }[state] || ""
    }
    onBridgeChanged: reload()

    PageHeader {
        Layout.fillWidth: true
        title: "Meetings"
        subtitle: "The sub-task Odin made for each finished meeting. Muninn records each one, so a meeting is "
                  + "never made twice; `odin forget KEY` lets one be pushed again."
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
        title: "No meeting sub-tasks yet"
        detail: "The daily run makes one for each meeting you attended, once it has ended."
    }

    Card {
        visible: page.rows.length > 0
        Layout.fillWidth: true
        title: page.rows.length + " most recent"

        Repeater {
            model: page.rows
            delegate: RowLayout {
                id: row
                required property var modelData
                Layout.fillWidth: true
                spacing: 10

                Text {
                    text: row.modelData.started
                    color: theme.textMuted
                    font.family: theme.monoFamily
                    font.pixelSize: theme.fontSize - 1
                    Layout.preferredWidth: 130
                }
                Text {
                    text: row.modelData.minutes + "m"
                    color: theme.textMuted
                    font.family: theme.fontFamily
                    font.pixelSize: theme.fontSize - 1
                    horizontalAlignment: Text.AlignRight
                    Layout.preferredWidth: 44
                }
                AppButton {
                    text: row.modelData.issue_key
                    kind: "ghost"
                    enabled: row.modelData.url !== ""
                    onClicked: Qt.openUrlExternally(row.modelData.url)
                    Accessible.name: "Open " + row.modelData.issue_key + " in Jira"
                }
                Text {
                    text: row.modelData.summary
                    color: theme.text
                    font.family: theme.fontFamily
                    font.pixelSize: theme.fontSize
                    elide: Text.ElideRight
                    Layout.fillWidth: true
                }
                Pill {
                    visible: page.worklogText(row.modelData.worklog) !== ""
                    text: page.worklogText(row.modelData.worklog)
                    tone: page.worklogTone(row.modelData.worklog)
                }
                Pill {
                    visible: row.modelData.origin === "state_db"
                    text: "from before Muninn"
                }
            }
        }
    }
}
