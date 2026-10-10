// Asgard's home page, the launcher: a tile for each app. Click a tile (or press Enter) to open it,
// right-click (or the Menu key) for its menu, type anywhere to search, F5 to reload the tile list.
// The logic is home.* (asgard/ui/home.py); this file only shows it and asks the questions.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: page

    readonly property int tileCount: grid.count          // for tests and diagnostics

    // ---- what the page does ------------------------------------------------
    function reload() { home.reload() }
    // Coming back to the window: the shell calls this, and the counts are brought up to date.
    function refresh() { home.refreshCounts() }

    function open(appId) {
        handle(home.activate(appId), appId)
    }

    // Some answers need a question or a dialog; the rest were already shown as a toast.
    function handle(reply, appId) {
        if (reply.result === "needs_setup" || reply.result === "missing") {
            setupDialog.begin(appId, reply.message)
        } else if (reply.result === "already_running") {
            againDialog.appId = appId
            againText.text = reply.message
            againDialog.open()
        } else if (reply.result === "uninstall") {
            uninstallDialog.begin()
        }
    }

    function showMenu(tile, point) {
        tileMenu.tile = tile
        tileMenu.popup(point.x, point.y)
    }

    function typeToSearch(event) {
        var typed = event.text
        var blocked = Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier
        if (typed.length !== 1 || typed.charCodeAt(0) < 33 || (event.modifiers & blocked))
            return false
        search.append(typed)
        return true
    }

    Connections {
        target: home
        function onNotice(title, text) {
            noticeDialog.title = title
            noticeText.text = text
            noticeDialog.open()
        }
    }

    // ---- the page ----------------------------------------------------------
    ColumnLayout {
        anchors.fill: parent
        anchors.leftMargin: 32
        anchors.rightMargin: 32
        anchors.topMargin: 28
        anchors.bottomMargin: 12
        spacing: 18

        RowLayout {
            Layout.fillWidth: true
            spacing: 14
            ColumnLayout {
                spacing: 4
                Layout.fillWidth: true
                Text {
                    text: "Apps"
                    color: theme.text
                    font.family: theme.fontFamily
                    font.pixelSize: theme.fontSize + 15
                    font.weight: Font.Bold
                    Accessible.role: Accessible.Heading
                }
                RowLayout {
                    spacing: 8
                    Text {
                        text: home.query.length > 0 ? home.shown + " of " + home.total + " apps" : home.total + " apps"
                        color: theme.textMuted
                        font.family: theme.fontFamily
                        font.pixelSize: theme.fontSize
                    }
                    Rectangle {
                        Layout.preferredWidth: 4
                        Layout.preferredHeight: 4
                        radius: 2
                        color: theme.textDim
                    }
                    Rectangle {
                        Layout.preferredWidth: 8
                        Layout.preferredHeight: 8
                        radius: 4
                        color: home.muninnState === "ready" ? theme.success
                               : (home.muninnState === "starting" ? theme.textDim : theme.warning)
                    }
                    Text {
                        text: home.muninnState === "ready" ? home.muninnText
                              : (home.muninnState === "starting" ? "Starting Muninn..." : "Muninn unavailable")
                        color: theme.textMuted
                        font.family: theme.fontFamily
                        font.pixelSize: theme.fontSize
                    }
                }
            }
            AppSearchField {
                id: search
                Layout.preferredWidth: 320
                Layout.alignment: Qt.AlignTop
                placeholder: "Search apps"
                label: "Search apps"
                onTextChanged: home.setQuery(text)
                onAccepted: if (home.tiles.length > 0) page.open(home.tiles[0].id)
                onDownPressed: grid.forceActiveFocus()
                onCleared: grid.forceActiveFocus()
            }
            AppButton {
                id: moreButton
                kind: "ghost"
                iconName: "more"
                Layout.alignment: Qt.AlignTop
                Accessible.name: "More actions"
                onClicked: mainMenu.popup(moreButton, moreButton.width - mainMenu.width, moreButton.height + 4)
            }
        }

        GridView {
            id: grid
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.leftMargin: -8
            Layout.rightMargin: -8
            clip: true
            focus: true
            keyNavigationEnabled: true
            boundsBehavior: Flickable.StopAtBounds
            model: home.tiles
            readonly property int columns: Math.max(1, Math.floor(width / 272))
            cellWidth: Math.floor(width / columns)
            cellHeight: 172
            ScrollBar.vertical: ScrollBar {}
            Accessible.role: Accessible.List
            Accessible.name: "Apps"

            delegate: AppTile {
                required property var modelData
                required property int index
                width: grid.cellWidth
                height: grid.cellHeight
                tile: modelData
                current: GridView.isCurrentItem && grid.activeFocus
                onActivated: {
                    grid.currentIndex = index
                    grid.forceActiveFocus()
                    page.open(modelData.id)
                }
                onMenuRequested: function (x, y) {
                    grid.currentIndex = index
                    page.showMenu(modelData, mapToItem(page, x, y))
                }
            }

            Keys.onReturnPressed: openCurrent()
            Keys.onEnterPressed: openCurrent()
            Keys.onSpacePressed: openCurrent()
            Keys.onPressed: function (event) {
                if (event.key === Qt.Key_Menu || (event.key === Qt.Key_F10 && (event.modifiers & Qt.ShiftModifier))) {
                    if (currentItem) {
                        page.showMenu(model[currentIndex], currentItem.mapToItem(page, 40, 40))
                        event.accepted = true
                    }
                } else if (event.key === Qt.Key_Slash) {
                    search.focusField()
                    event.accepted = true
                } else if (page.typeToSearch(event)) {
                    event.accepted = true
                }
            }
            function openCurrent() {
                if (currentIndex >= 0 && currentIndex < model.length)
                    page.open(model[currentIndex].id)
            }
        }
    }

    EmptyState {
        anchors.centerIn: parent
        visible: home.tiles.length === 0
        title: home.query.length > 0 ? "Nothing matches \"" + home.query + "\"" : "No apps to show"
        detail: home.query.length > 0 ? "Try part of an app's name or what it does."
                                      : "Edit my tiles from the menu, or reinstall Asgard if its tile list is damaged."
        actionText: home.query.length > 0 ? "Clear search" : ""
        onAction: search.text = ""
    }

    Shortcut {
        sequences: ["Ctrl+K", "Ctrl+F"]
        onActivated: search.focusField()
    }

    // ---- menus -------------------------------------------------------------
    AppMenu {
        id: mainMenu
        objectName: "mainMenu"
        AppMenuItem { text: "Edit my tiles"; iconName: "pencil"; onTriggered: home.editTiles() }
        AppMenuItem { text: "Reload tiles"; iconName: "refresh"; hint: "F5"; onTriggered: page.reload() }
        AppMenuItem { text: "Open Asgard folder"; iconName: "folder"; onTriggered: home.openData() }
        AppMenuItem {
            text: "Back up Muninn now"
            iconName: "file"
            enabled: home.muninnState === "ready"
            onTriggered: home.backup()
        }
        AppMenuSeparator {}
        AppMenuItem {
            text: "About Asgard"
            iconName: "info"
            onTriggered: {
                aboutText.text = home.about()
                aboutDialog.open()
            }
        }
        AppMenuItem { text: "Uninstall Asgard"; iconName: "trash"; danger: true; onTriggered: uninstallDialog.begin() }
    }

    AppMenu {
        id: tileMenu
        objectName: "tileMenu"
        property var tile: ({})
        AppMenuItem {
            text: "Open"
            iconName: "play"
            enabled: tileMenu.tile.canOpen === true
            onTriggered: page.open(tileMenu.tile.id)
        }
        AppMenuItem {
            text: "Change location"
            iconName: "pencil"
            visible: tileMenu.tile.canChange === true
            onTriggered: setupDialog.begin(tileMenu.tile.id, "Where is " + tileMenu.tile.name + "? Choose the file you use to start it.")
        }
        AppMenuItem {
            text: "Open file location"
            iconName: "folder"
            visible: tileMenu.tile.hasFolder === true
            onTriggered: home.openLocation(tileMenu.tile.id)
        }
        AppMenuItem {
            text: "View log"
            iconName: "file"
            visible: tileMenu.tile.hasLog === true
            onTriggered: home.viewLog(tileMenu.tile.id)
        }
    }

    // ---- dialogs -----------------------------------------------------------
    AppDialog {
        id: setupDialog
        objectName: "setupDialog"
        property string appId: ""
        function begin(id, message) {
            appId = id
            setupText.text = message
            pathField.text = ""
            open()
            pathField.forceActiveFocus()
        }
        function save() {
            var path = pathField.text.trim()
            if (path.length === 0)
                return
            close()
            page.handle(home.setUp(appId, path), appId)
        }
        title: "Where is the app?"
        Text {
            id: setupText
            color: theme.textMuted
            font.family: theme.fontFamily
            font.pixelSize: theme.fontSize
            wrapMode: Text.WordWrap
            Layout.fillWidth: true
        }
        RowLayout {
            spacing: 8
            Layout.fillWidth: true
            Rectangle {
                Layout.fillWidth: true
                implicitHeight: 40
                radius: theme.radiusSmall + 2
                color: theme.surfaceHigh
                border.width: pathField.activeFocus ? 2 : 1
                border.color: pathField.activeFocus ? theme.focus : theme.borderSoft
                TextField {
                    id: pathField
                    anchors.fill: parent
                    anchors.leftMargin: 6
                    anchors.rightMargin: 6
                    placeholderText: "C:\\path\\to\\the\\app.pyw"
                    placeholderTextColor: theme.textDim
                    color: theme.text
                    selectionColor: theme.accent
                    selectedTextColor: theme.accentText
                    font.family: theme.monoFamily
                    font.pixelSize: theme.fontSize - 1
                    verticalAlignment: TextInput.AlignVCenter
                    background: Item {}
                    Accessible.name: "Path to the app's start file"
                    onAccepted: setupDialog.save()
                }
            }
            AppButton {
                text: "Browse"
                iconName: "folder"
                onClicked: {
                    if (!picker.active)
                        picker.active = true
                    else
                        picker.item.open()
                }
            }
        }
        buttons: [
            AppButton { text: "Cancel"; onClicked: setupDialog.close() },
            AppButton { text: "Save and open"; kind: "primary"; enabled: pathField.text.trim().length > 0; onClicked: setupDialog.save() }
        ]
    }

    Loader {
        id: picker
        active: false
        source: "FilePicker.qml"
        onLoaded: item.open()
        onStatusChanged: {
            if (status === Loader.Error)
                shell.toast("This computer's Qt has no file chooser. Type or paste the path instead.", "warning")
        }
    }
    Connections {
        target: picker.item
        ignoreUnknownSignals: true
        function onPicked(path) { pathField.text = path }
    }

    AppDialog {
        id: againDialog
        property string appId: ""
        title: "Already open"
        Text {
            id: againText
            color: theme.textMuted
            font.family: theme.fontFamily
            font.pixelSize: theme.fontSize
            wrapMode: Text.WordWrap
            Layout.fillWidth: true
        }
        buttons: [
            AppButton { text: "Cancel"; onClicked: againDialog.close() },
            AppButton {
                text: "Open another"
                kind: "primary"
                onClicked: {
                    againDialog.close()
                    home.activateAgain(againDialog.appId)
                }
            }
        ]
    }

    AppDialog {
        id: noticeDialog
        maxWidth: 560
        Text {
            id: noticeText
            color: theme.textMuted
            font.family: theme.fontFamily
            font.pixelSize: theme.fontSize
            wrapMode: Text.WordWrap
            Layout.fillWidth: true
        }
        buttons: [ AppButton { text: "OK"; kind: "primary"; onClicked: noticeDialog.close() } ]
    }

    AppDialog {
        id: aboutDialog
        objectName: "aboutDialog"
        title: "About Asgard"
        maxWidth: 620
        TextEdit {
            id: aboutText
            readOnly: true
            selectByMouse: true
            color: theme.textMuted
            selectionColor: theme.accent
            selectedTextColor: theme.accentText
            font.family: theme.monoFamily
            font.pixelSize: theme.fontSize - 1
            wrapMode: TextEdit.Wrap
            Layout.fillWidth: true
            Accessible.name: "About Asgard"
            Accessible.role: Accessible.StaticText
        }
        buttons: [ AppButton { text: "Close"; kind: "primary"; onClicked: aboutDialog.close() } ]
    }

    // Uninstalling takes three answers, one dialog at a time: sure?, keep your data?, done.
    AppDialog {
        id: uninstallDialog
        objectName: "uninstallDialog"
        property string stage: "confirm"
        function begin() {
            var info = home.uninstallInfo()
            uninstallText.text = info.message
            stage = info.installed ? "confirm" : "none"
            open()
        }
        function run(purge) {
            var result = home.uninstall(purge)
            uninstallText.text = result.message || result.error || ""
            stage = "done"
        }
        title: stage === "done" ? "Asgard is uninstalled" : (stage === "purge" ? "Delete your Asgard data too?" : "Uninstall Asgard?")
        closePolicy: stage === "done" ? Popup.NoAutoClose : Popup.CloseOnEscape
        Text {
            id: uninstallText
            visible: uninstallDialog.stage !== "purge"
            color: theme.textMuted
            font.family: theme.fontFamily
            font.pixelSize: theme.fontSize
            wrapMode: Text.WordWrap
            Layout.fillWidth: true
        }
        Text {
            visible: uninstallDialog.stage === "purge"
            text: "Also delete your tile settings, logs and Muninn database? Keep them if you may reinstall later."
            color: theme.text
            font.family: theme.fontFamily
            font.pixelSize: theme.fontSize
            wrapMode: Text.WordWrap
            Layout.fillWidth: true
        }
        buttons: [
            AppButton { visible: uninstallDialog.stage === "none"; text: "OK"; kind: "primary"; onClicked: uninstallDialog.close() },
            AppButton { visible: uninstallDialog.stage === "confirm"; text: "Cancel"; onClicked: uninstallDialog.close() },
            AppButton {
                visible: uninstallDialog.stage === "confirm"
                text: "Uninstall"
                kind: "danger"
                onClicked: uninstallDialog.stage = "purge"
            },
            AppButton { visible: uninstallDialog.stage === "purge"; text: "Keep my data"; onClicked: uninstallDialog.run(false) },
            AppButton { visible: uninstallDialog.stage === "purge"; text: "Delete my data"; kind: "danger"; onClicked: uninstallDialog.run(true) },
            AppButton { visible: uninstallDialog.stage === "done"; text: "Close Asgard"; kind: "primary"; onClicked: Qt.quit() }
        ]
    }
}
