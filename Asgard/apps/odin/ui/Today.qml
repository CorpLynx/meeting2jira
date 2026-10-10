// Odin: setup, the last run, what waits, and the daily run by hand (it runs `odin` as the scheduled task does).
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import AsgardUI

ScrollPage {
    id: page
    property var bridge: null
    property var st: ({})
    property var output: []
    property string runState: ""        // "", "running", "done", "failed"
    property string runTitle: ""

    readonly property var counts: st.counts || ({})
    readonly property var last: st.last_run || null
    readonly property bool ready: !!st.config_path && !st.config_error && !!st.token && !st.muninn_error
    readonly property bool busy: !!bridge && bridge.running

    function reload() { if (bridge) st = bridge.call("state", []) }
    function run(kind) {
        if (!bridge)
            return
        runTitle = (st.commands || {})[kind] || kind
        output = []
        runState = "running"
        if (!bridge.spawn("run_command", [kind]))
            runState = ""
    }
    function lastLine() {
        if (!page.last)
            return "Odin hasn't run on this computer yet."
        var l = page.last
        var verdict = l.exit_code === 0 ? "worked" : "failed"
        var age = page.st.last_run_age_days
        var when = l.finished_utc + (age !== null && age !== undefined && age > 0 ? " (" + age + " day(s) ago)" : "")
        return "The last run (" + (l.command || "push") + ") " + verdict + " at " + when + "."
    }
    onBridgeChanged: reload()

    Connections {
        target: page.bridge
        ignoreUnknownSignals: true
        function onOutput(line) { page.output = page.output.concat([line]) }
        function onFinished(code) {
            page.runState = code === 0 ? "done" : "failed"
            if (code !== 0)
                shell.toast(page.runTitle + " stopped with problems. The messages below say why.", "error")
            page.reload()
        }
    }

    PageHeader {
        Layout.fillWidth: true
        title: "Today"
        subtitle: "Odin turns your finished meetings into Jira sub-tasks, keeps Jira in Muninn for Asgard's other "
                  + "apps, and posts the time you approve in Baldur. The scheduled task does this every weekday."
        AppButton {
            text: "Preview"
            enabled: page.ready && !page.busy
            onClicked: page.run("preview")
        }
        AppButton {
            text: "Run now"
            kind: "primary"
            enabled: page.ready && !page.busy
            onClicked: page.run("daily")
        }
        AppButton {
            text: "Reload"
            kind: "ghost"
            onClicked: page.reload()
        }
    }

    // ------------------------------------------------ setup
    Card {
        visible: !!page.st.config_error
        Layout.fillWidth: true
        title: page.st.config_missing ? "Set Odin up first" : "Odin's settings need fixing"
        subtitle: page.st.config_missing
                  ? "Create the settings file, then set jira.base_url and jira.default_parent (the issue your "
                    + "meeting sub-tasks go under), and save it."
                  : (page.st.config_error || "")
        accentColor: page.st.config_missing ? theme.warning : theme.error
        RowLayout {
            spacing: 8
            AppButton {
                visible: !!page.st.config_missing
                text: "Create settings"
                kind: "primary"
                onClicked: {
                    var r = page.bridge.call("init_config", [])
                    if (r.error) { shell.toast(r.error, "error"); return }
                    shell.toast("Created " + r.path + ". Edit it, save it, then Reload.", "info")
                    shell.openPath(r.path)
                    page.reload()
                }
            }
            AppButton {
                visible: !page.st.config_missing
                text: "Open settings"
                onClicked: shell.openPath(page.st.config_path)
            }
            AppButton {
                text: "Reload"
                kind: "ghost"
                onClicked: page.reload()
            }
        }
    }

    Card {
        visible: !page.st.config_error && !page.st.token
        Layout.fillWidth: true
        title: "Your Jira token"
        subtitle: "Create a personal access token in Jira (your profile, Personal Access Tokens) and paste it here. "
                  + "Odin keeps it encrypted for your Windows account, in Odin's folder, and never shows it again."
        accentColor: theme.warning
        RowLayout {
            spacing: 8
            Layout.fillWidth: true
            TextField {
                id: tokenField
                Layout.fillWidth: true
                echoMode: TextInput.Password
                placeholderText: "Personal access token"
                font.family: theme.fontFamily
                font.pixelSize: theme.fontSize
                Accessible.name: "Jira personal access token"
            }
            AppButton {
                text: "Save token"
                kind: "primary"
                enabled: tokenField.text.length > 0
                onClicked: {
                    var r = page.bridge.call("save_token", [tokenField.text])
                    tokenField.text = ""
                    if (r.error) { shell.toast(r.error, "error"); return }
                    shell.toast("Token saved. Check the setup next.", "info")
                    page.reload()
                }
            }
        }
    }

    Card {
        visible: !!page.st.muninn_error
        Layout.fillWidth: true
        title: "Muninn isn't ready"
        subtitle: page.st.muninn_error || ""
        accentColor: theme.error
    }

    Card {
        visible: (page.st.legacy_records || 0) > 0
        Layout.fillWidth: true
        title: "Odin's history from before Muninn"
        subtitle: "state.db records " + page.st.legacy_records + " meeting sub-task(s). The next run moves them "
                  + "into Muninn, so none of those meetings is created again, and keeps the old file for 30 days."
    }

    // ------------------------------------------------ the last run and what waits
    Card {
        visible: !page.st.config_error
        Layout.fillWidth: true
        title: "Last run"
        subtitle: page.lastLine()
        accentColor: page.last && page.last.exit_code !== 0 ? theme.error : "transparent"

        Text {
            visible: !!page.last && !!page.last.first_error
            text: page.last && page.last.first_error ? page.last.first_error : ""
            color: theme.error
            wrapMode: Text.Wrap
            font.family: theme.fontFamily
            font.pixelSize: theme.fontSize - 1
            Layout.fillWidth: true
        }
        Text {
            visible: !!page.last
            text: page.last ? ("Created " + (page.last.created || 0) + " sub-task(s), " + (page.last.existing || 0)
                               + " already there, " + (page.last.skipped || 0) + " skipped; logged "
                               + (page.last.worklogs_logged || 0) + " meeting worklog(s); posted "
                               + (page.last.approved_posted || 0) + " approved day(s).") : ""
            color: theme.textMuted
            wrapMode: Text.Wrap
            font.family: theme.fontFamily
            font.pixelSize: theme.fontSize - 1
            Layout.fillWidth: true
        }
        Text {
            visible: !!page.st.alert
            text: "A failure notice (ATTENTION-Odin.txt) is outstanding; the next run that works takes it down."
            color: theme.warning
            wrapMode: Text.Wrap
            font.family: theme.fontFamily
            font.pixelSize: theme.fontSize - 1
            Layout.fillWidth: true
        }
    }

    GridLayout {
        visible: !page.st.muninn_error && !page.st.config_error
        Layout.fillWidth: true
        columns: width > 800 ? 4 : 2
        columnSpacing: 12
        rowSpacing: 12
        MetricCard {
            Layout.fillWidth: true
            label: "Approved days to post"
            value: String(page.counts.posts_due || 0)
            detail: (page.counts.unpostable || 0) > 0 ? page.counts.unpostable + " Jira can't take" : "From Baldur"
            tone: (page.counts.unpostable || 0) > 0 ? "warning" : ""
            interactive: false
        }
        MetricCard {
            Layout.fillWidth: true
            label: "Meeting sub-tasks"
            value: String(page.counts.subtasks || 0)
            detail: (page.counts.worklogs_waiting || 0) > 0 ? page.counts.worklogs_waiting + " worklog(s) to retry"
                                                            : "Recorded in Muninn"
            tone: (page.counts.worklogs_waiting || 0) > 0 ? "warning" : ""
            onClicked: navigation.go("odin:meetings")
        }
        MetricCard {
            Layout.fillWidth: true
            label: "Assigned to me"
            value: String(page.counts.assigned || 0)
            detail: "Open issues"
            onClicked: navigation.go("odin:issues")
        }
        MetricCard {
            Layout.fillWidth: true
            label: "Posts in doubt"
            value: String(page.counts.posts_in_doubt || 0)
            detail: (page.counts.posts_in_doubt || 0) > 0 ? "Asked again each run; odin settle lists them"
                                                          : "Checked against Jira by the next run"
            tone: (page.counts.posts_in_doubt || 0) > 0 ? "warning" : ""
            interactive: false
        }
    }

    // ------------------------------------------------ by hand
    Card {
        visible: !page.st.config_error
        Layout.fillWidth: true
        title: "Run a part"
        subtitle: "Each runs Odin's command line, as the scheduled task does. One Odin run at a time."

        RowLayout {
            spacing: 8
            Layout.fillWidth: true
            AppButton {
                text: "Read Jira into Muninn"
                enabled: page.ready && !page.busy
                onClicked: page.run("sync")
            }
            AppButton {
                text: "Post approved days"
                enabled: page.ready && !page.busy
                onClicked: page.run("post")
            }
            AppButton {
                text: "List days to post"
                enabled: page.ready && !page.busy
                onClicked: page.run("post_preview")
            }
            AppButton {
                text: "Check the setup"
                enabled: !page.st.config_error && !page.busy
                onClicked: page.run("check")
            }
            Item { Layout.fillWidth: true }
            AppButton {
                text: "Stop"
                kind: "danger"
                visible: page.busy
                onClicked: page.bridge.stop()
            }
        }

        Text {
            visible: page.runState !== ""
            text: page.runTitle + (page.runState === "running" ? "..." : page.runState === "done" ? ": done." : ": stopped with problems.")
            color: page.runState === "failed" ? theme.error : theme.textMuted
            font.family: theme.fontFamily
            font.pixelSize: theme.fontSize - 1
            Layout.fillWidth: true
        }

        Rectangle {
            visible: page.output.length > 0
            Layout.fillWidth: true
            Layout.preferredHeight: Math.min(320, outputText.implicitHeight + 20)
            radius: theme.radiusSmall
            color: theme.surfaceHigh
            border.color: page.runState === "failed" ? theme.error : theme.borderSoft
            border.width: 1

            ScrollView {
                anchors.fill: parent
                anchors.margins: 10
                TextArea {
                    id: outputText
                    objectName: "odinOutput"
                    text: page.output.join("\n")
                    readOnly: true
                    selectByMouse: true
                    wrapMode: TextArea.Wrap
                    color: theme.text
                    font.family: theme.monoFamily
                    font.pixelSize: theme.fontSize - 1
                    background: null
                    Accessible.name: page.runTitle
                }
            }
        }
    }
}
