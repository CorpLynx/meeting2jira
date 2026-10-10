// Heimdall: saved templates. Pick one, check its values, then Fill in Edge (it stops before Submit).
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import AsgardUI

Item {
    id: page
    property var bridge: null
    property var st: ({})
    property string selected: ""
    property var detail: null
    property bool editing: false
    property bool creating: false
    property var output: []
    property string runState: ""        // "", "plan", "running", "done", "failed"

    readonly property bool ready: !!st.form && !st.templates_error
    readonly property var templates: st.templates || []

    function call(method, args) {
        if (!bridge)
            return null
        var r = bridge.call(method, args || [])
        if (r && r.error) {
            shell.toast(r.error, "error")
            return null
        }
        return r
    }
    function reload() {
        if (!bridge)
            return
        st = bridge.call("state", [])
        var names = templates.map(function (t) { return t.name })
        if (names.indexOf(selected) < 0)
            selected = names.length ? names[0] : ""
        loadDetail()
    }
    function loadDetail() { detail = selected ? call("template", [selected]) : null }
    function select(name) {
        selected = name
        editing = false
        output = []
        runState = ""
        loadDetail()
    }
    function startNew() {
        creating = true
        editing = true
        editor.load(null)
    }
    function startEdit() {
        creating = false
        editing = true
        editor.load(detail)
    }
    function dryRun() {
        var r = call("dry_run", [selected])
        if (r) {
            output = r.lines
            runState = "plan"
        }
    }
    function fill() {
        output = ["Starting Edge. If it asks, pick your certificate and enter your PIN."]
        runState = "running"
        if (!bridge.spawn("fill_command", [selected]))
            runState = ""
    }

    onBridgeChanged: reload()

    Connections {
        target: page.bridge
        ignoreUnknownSignals: true
        function onOutput(line) { page.output = page.output.concat([line.replace(/^heimdall: /i, "")]) }
        function onFinished(code) {
            page.runState = code === 0 ? "done" : "failed"
            if (code !== 0)
                shell.toast("The fill stopped early. The messages below say why.", "error")
        }
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 28
        spacing: 16

        PageHeader {
            Layout.fillWidth: true
            title: "Templates"
            subtitle: "Saved values for the security change form. Fill opens Edge, fills the form, and stops so you can "
                      + "review it and click Submit yourself."
            AppButton {
                text: "New template"
                kind: "primary"
                enabled: page.ready && !page.editing
                onClicked: page.startNew()
            }
        }

        Card {
            visible: !!page.st.form_error
            Layout.fillWidth: true
            title: page.st.form_missing ? "Set up the form first" : "The form file needs fixing"
            subtitle: page.st.form_error || ""
            accentColor: page.st.form_missing ? theme.warning : theme.error
            RowLayout {
                spacing: 8
                AppButton {
                    text: "Go to Form"
                    kind: "primary"
                    onClicked: navigation.go("heimdall:form")
                }
                AppButton {
                    text: "Reload"
                    kind: "ghost"
                    onClicked: page.reload()
                }
            }
        }

        Card {
            visible: !!page.st.templates_error
            Layout.fillWidth: true
            title: "The templates file needs fixing"
            subtitle: page.st.templates_error || ""
            accentColor: theme.error
            RowLayout {
                spacing: 8
                AppButton {
                    text: "Show templates file"
                    onClicked: shell.openFolder(page.st.templates_path)
                }
                AppButton {
                    text: "Reload"
                    kind: "ghost"
                    onClicked: page.reload()
                }
            }
        }

        RowLayout {
            visible: page.ready
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 16

            // ------------------------------------------------ the list
            Card {
                Layout.preferredWidth: 280
                Layout.fillHeight: true
                title: "Saved"
                subtitle: page.templates.length === 1 ? "1 template" : page.templates.length + " templates"

                ListView {
                    id: list
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    clip: true
                    spacing: 2
                    model: page.templates
                    Accessible.role: Accessible.List
                    Accessible.name: "Saved templates"
                    delegate: NavItem {
                        required property var modelData
                        width: list.width
                        label: modelData.name
                        iconText: modelData.problems.length ? "!" : String(modelData.fields_set)
                        accentColor: modelData.problems.length ? theme.warning : theme.accentInk
                        selected: modelData.name === page.selected && !page.creating
                        Accessible.description: modelData.problems.length ? "Needs fixing" : modelData.fields_set + " fields set"
                        onClicked: page.select(modelData.name)
                    }
                }
                Text {
                    visible: page.templates.length === 0
                    text: "No templates yet. Press New template."
                    color: theme.textMuted
                    font.family: theme.fontFamily
                    font.pixelSize: theme.fontSize - 1
                    wrapMode: Text.WordWrap
                    Layout.fillWidth: true
                }
            }

            // ------------------------------------------------ one template
            Card {
                visible: !page.editing
                Layout.fillWidth: true
                Layout.fillHeight: true
                title: page.detail ? page.detail.name : ""
                subtitle: page.detail ? (page.detail.description || "") : ""

                EmptyState {
                    visible: !page.detail
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    title: page.templates.length ? "Pick a template" : "Save your first template"
                    detail: "A template holds the values you use for one kind of submission. The form's fields come "
                            + "from the form file."
                    actionText: page.templates.length ? "" : "New template"
                    onAction: page.startNew()
                }

                Repeater {
                    model: page.detail ? page.detail.problems : []
                    delegate: Text {
                        required property string modelData
                        text: "Needs fixing: " + modelData
                        color: theme.error
                        font.family: theme.fontFamily
                        font.pixelSize: theme.fontSize - 1
                        wrapMode: Text.WordWrap
                        Layout.fillWidth: true
                    }
                }

                ListView {
                    id: rows
                    visible: !!page.detail
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    Layout.minimumHeight: 120
                    clip: true
                    spacing: 6
                    model: page.detail ? page.detail.fields : []
                    delegate: RowLayout {
                        required property var modelData
                        width: rows.width
                        spacing: 12
                        Text {
                            text: parent.modelData.label + (parent.modelData.required ? " *" : "")
                            color: theme.textMuted
                            font.family: theme.fontFamily
                            font.pixelSize: theme.fontSize - 1
                            elide: Text.ElideRight
                            Layout.preferredWidth: 220
                        }
                        Text {
                            text: parent.modelData.display
                                  + (parent.modelData.source === "default" ? "   (form default)" : "")
                            color: parent.modelData.source === "none" ? theme.textDim : theme.text
                            font.family: theme.fontFamily
                            font.pixelSize: theme.fontSize
                            wrapMode: Text.WordWrap
                            Layout.fillWidth: true
                        }
                    }
                }

                Text {
                    visible: !!page.detail
                    text: "Attachment: " + (page.detail && page.detail.attachment ? page.detail.attachment : "(none)")
                    color: theme.textMuted
                    font.family: theme.fontFamily
                    font.pixelSize: theme.fontSize - 1
                    elide: Text.ElideMiddle
                    Layout.fillWidth: true
                }

                RowLayout {
                    visible: !!page.detail
                    spacing: 8
                    Layout.fillWidth: true
                    AppButton {
                        text: "Fill in Edge"
                        kind: "primary"
                        enabled: !!page.bridge && !page.bridge.running && page.detail && page.detail.problems.length === 0
                        onClicked: page.fill()
                    }
                    AppButton {
                        text: "Dry run"
                        enabled: !!page.bridge && !page.bridge.running
                        onClicked: page.dryRun()
                    }
                    AppButton {
                        text: "Edit"
                        enabled: !!page.bridge && !page.bridge.running
                        onClicked: page.startEdit()
                    }
                    Item { Layout.fillWidth: true }
                    AppButton {
                        text: "Stop"
                        kind: "danger"
                        visible: !!page.bridge && page.bridge.running
                        onClicked: page.bridge.stop()
                    }
                    AppButton {
                        text: "Delete"
                        kind: "ghost"
                        enabled: !!page.bridge && !page.bridge.running
                        onClicked: confirmDelete.open()
                    }
                }

                Rectangle {
                    visible: page.output.length > 0
                    Layout.fillWidth: true
                    Layout.preferredHeight: Math.min(220, outputText.implicitHeight + 20)
                    radius: theme.radiusSmall
                    color: theme.surfaceHigh
                    border.color: page.runState === "failed" ? theme.error : theme.borderSoft
                    border.width: 1

                    ScrollView {
                        anchors.fill: parent
                        anchors.margins: 10
                        TextArea {
                            id: outputText
                            objectName: "fillOutput"
                            text: page.output.join("\n")
                            readOnly: true
                            selectByMouse: true
                            wrapMode: TextArea.Wrap
                            color: theme.text
                            font.family: theme.monoFamily
                            font.pixelSize: theme.fontSize - 1
                            background: null
                            Accessible.name: page.runState === "plan" ? "Dry run" : "Fill progress"
                        }
                    }
                }
            }

            // ------------------------------------------------ editor
            Card {
                id: editor
                visible: page.editing
                Layout.fillWidth: true
                Layout.fillHeight: true
                title: page.creating ? "New template" : "Edit " + page.selected
                subtitle: "Leave a field empty to leave it as it is on the form. Values may use $today or $today_us."

                function load(d) {
                    nameField.text = d ? d.name : ""
                    descField.text = d ? (d.description || "") : ""
                    attachField.text = d ? (d.attachment || "") : ""
                    for (var i = 0; i < fieldRepeater.count; i++) {
                        var item = fieldRepeater.itemAt(i)
                        var saved = null
                        if (d) {
                            for (var j = 0; j < d.fields.length; j++)
                                if (d.fields[j].label === item.label)
                                    saved = d.fields[j].saved
                        }
                        item.setValue(saved)
                    }
                    nameField.forceActiveFocus()
                }
                function save() {
                    var values = {}
                    for (var i = 0; i < fieldRepeater.count; i++) {
                        var item = fieldRepeater.itemAt(i)
                        values[item.label] = item.value()
                    }
                    var r = page.call("save_template", [page.creating ? "" : page.selected, {
                        "name": nameField.text, "description": descField.text,
                        "attachment": attachField.text, "values": values }])
                    if (!r)
                        return
                    shell.toast('Saved "' + r.name + '".', "info")
                    page.editing = false
                    page.creating = false
                    page.selected = r.name
                    page.reload()
                }

                GridLayout {
                    columns: 2
                    columnSpacing: 12
                    rowSpacing: 8
                    Layout.fillWidth: true
                    Label { text: "Name" }
                    TextField {
                        id: nameField
                        objectName: "templateName"
                        Layout.fillWidth: true
                        placeholderText: "Monthly scan"
                        Accessible.name: "Template name"
                    }
                    Label { text: "Description" }
                    TextField {
                        id: descField
                        Layout.fillWidth: true
                        placeholderText: "Optional"
                        Accessible.name: "Description"
                    }
                    Label { text: "Attachment" }
                    TextField {
                        id: attachField
                        Layout.fillWidth: true
                        placeholderText: "Optional: full path to the file, like C:\\Reports\\scan.xlsx"
                        Accessible.name: "Attachment path"
                    }
                }

                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 1
                    color: theme.borderSoft
                }

                ScrollView {
                    id: fieldScroll
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    clip: true
                    contentWidth: availableWidth

                    ColumnLayout {
                        width: fieldScroll.availableWidth
                        spacing: 10

                        Repeater {
                            id: fieldRepeater
                            model: page.st.fields || []
                            delegate: ColumnLayout {
                                id: fieldBox
                                required property var modelData
                                readonly property string label: modelData.label
                                readonly property bool isCheckbox: modelData.kind === "checkbox"
                                Layout.fillWidth: true
                                spacing: 4

                                function setValue(v) {
                                    if (isCheckbox)
                                        combo.currentIndex = v === true ? 1 : (v === false ? 2 : 0)
                                    else
                                        input.text = (v === null || v === undefined) ? "" : String(v)
                                }
                                function value() {
                                    if (isCheckbox)
                                        return [null, true, false][combo.currentIndex]
                                    return input.text.length ? input.text : null
                                }

                                RowLayout {
                                    spacing: 8
                                    Label {
                                        text: fieldBox.label + (fieldBox.modelData.required ? " *" : "")
                                        font.weight: Font.DemiBold
                                    }
                                    Pill { text: fieldBox.modelData.kind }
                                }
                                TextField {
                                    id: input
                                    visible: !fieldBox.isCheckbox
                                    Layout.fillWidth: true
                                    placeholderText: fieldBox.modelData["default"] !== null && fieldBox.modelData["default"] !== undefined
                                                     ? "Leave as is (form default: " + fieldBox.modelData["default"] + ")"
                                                     : "Leave as is"
                                    Accessible.name: fieldBox.label
                                }
                                ComboBox {
                                    id: combo
                                    visible: fieldBox.isCheckbox
                                    Layout.preferredWidth: 200
                                    model: ["Leave as is", "Checked", "Unchecked"]
                                    Accessible.name: fieldBox.label
                                }
                            }
                        }
                    }
                }

                RowLayout {
                    spacing: 8
                    AppButton {
                        text: "Save"
                        kind: "primary"
                        enabled: nameField.text.trim().length > 0
                        onClicked: editor.save()
                    }
                    AppButton {
                        text: "Cancel"
                        kind: "ghost"
                        onClicked: {
                            page.editing = false
                            page.creating = false
                        }
                    }
                }
            }
        }
    }

    Dialog {
        id: confirmDelete
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(440, page.width - 80)
        modal: true
        title: "Delete template?"
        standardButtons: Dialog.Yes | Dialog.Cancel
        Label {
            width: confirmDelete.availableWidth
            text: 'Delete "' + page.selected + '"? This can\'t be undone.'
            wrapMode: Text.WordWrap
        }
        onAccepted: {
            var name = page.selected
            if (page.call("delete_template", [name])) {
                shell.toast('Deleted "' + name + '".', "info")
                page.reload()
            }
        }
    }
}
