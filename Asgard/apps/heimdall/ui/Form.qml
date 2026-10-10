// Heimdall: the form file, as Heimdall reads it. Edit the file, then Reload.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import AsgardUI

ScrollPage {
    id: page
    property var bridge: null
    property var st: ({})

    function reload() { if (bridge) st = bridge.call("state", []) }
    onBridgeChanged: reload()

    PageHeader {
        Layout.fillWidth: true
        title: "Form"
        subtitle: "Where the security change catalog item is and which fields Heimdall fills. Edit the form file, then Reload."
        AppButton {
            text: "Open form file"
            enabled: !page.st.form_missing
            onClicked: shell.openPath(page.st.form_path)
        }
        AppButton {
            text: "Reload"
            kind: "ghost"
            onClicked: page.reload()
        }
    }

    Card {
        visible: !!page.st.form_error
        Layout.fillWidth: true
        title: page.st.form_missing ? "Set up the form first" : "The form file needs fixing"
        subtitle: page.st.form_error || ""
        accentColor: page.st.form_missing ? theme.warning : theme.error
        AppButton {
            visible: !!page.st.form_missing
            text: "Create form file"
            kind: "primary"
            onClicked: {
                var r = page.bridge.call("init_form", [])
                if (r.error) { shell.toast(r.error, "error"); return }
                shell.toast("Created " + r.path + ". Edit it to match the security change form, then Reload.", "info")
                page.reload()
                shell.openPath(r.path)
            }
        }
    }

    Card {
        visible: !!page.st.form
        Layout.fillWidth: true
        title: "Catalog item"

        GridLayout {
            columns: 2
            columnSpacing: 16
            rowSpacing: 6
            Layout.fillWidth: true

            Repeater {
                model: page.st.form ? [["Instance", page.st.form.instance_url], ["Opens", page.st.form.item_url],
                                       ["Page", page.st.form.ui], ["Ready when visible", page.st.form.ready],
                                       ["Form file", page.st.form_path]] : []
                delegate: Item {
                    required property var modelData
                    Layout.columnSpan: 2
                    Layout.fillWidth: true
                    implicitHeight: Math.max(key.implicitHeight, val.implicitHeight)
                    Text {
                        id: key
                        width: 150
                        text: parent.modelData[0]
                        color: theme.textMuted
                        font.family: theme.fontFamily
                        font.pixelSize: theme.fontSize - 1
                    }
                    TextEdit {
                        id: val
                        anchors.left: key.right
                        anchors.leftMargin: 16
                        anchors.right: parent.right
                        text: parent.modelData[1]
                        readOnly: true
                        selectByMouse: true
                        wrapMode: TextEdit.WrapAnywhere
                        color: theme.text
                        selectionColor: theme.accent
                        selectedTextColor: theme.accentText
                        font.family: theme.fontFamily
                        font.pixelSize: theme.fontSize - 1
                        Accessible.name: parent.modelData[0]
                    }
                }
            }
        }
    }

    Card {
        visible: !!page.st.form
        Layout.fillWidth: true
        title: "Fields"
        subtitle: (page.st.fields || []).length + " fields. Templates set values for these by label."

        Repeater {
            model: page.st.fields || []
            delegate: RowLayout {
                id: fieldRow
                required property var modelData
                Layout.fillWidth: true
                spacing: 10

                Text {
                    text: fieldRow.modelData.label
                    color: theme.text
                    font.family: theme.fontFamily
                    font.pixelSize: theme.fontSize
                    elide: Text.ElideRight
                    Layout.preferredWidth: 260
                }
                Pill { text: fieldRow.modelData.kind; tone: "accent" }
                Pill { visible: fieldRow.modelData.required; text: "required"; tone: "warning" }
                Text {
                    visible: fieldRow.modelData["default"] !== null && fieldRow.modelData["default"] !== undefined
                    text: "default: " + fieldRow.modelData["default"]
                    color: theme.textMuted
                    font.family: theme.fontFamily
                    font.pixelSize: theme.fontSize - 1
                    elide: Text.ElideRight
                    Layout.fillWidth: true
                }
                Item { Layout.fillWidth: true }
            }
        }
    }
}
