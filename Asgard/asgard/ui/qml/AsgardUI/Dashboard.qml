// What needs you, across apps: each app's backend.dashboard() cards and Muninn's tile counts.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ScrollPage {
    id: page
    function reload() { dashboard.refresh() }
    Component.onCompleted: dashboard.refresh()

    PageHeader {
        Layout.fillWidth: true
        title: "Dashboard"
        subtitle: dashboard.loading ? "Refreshing..." : "What needs you. Click a card to go there."
        AppButton {
            text: "Refresh"
            enabled: !dashboard.loading
            onClicked: dashboard.refresh()
        }
    }

    EmptyState {
        visible: dashboard.groups.length === 0 && !dashboard.loading
        Layout.fillWidth: true
        Layout.topMargin: 60
        title: "No apps with views yet"
        detail: "An app joins this window by adding apps/<id>/ui/manifest.json. Heimdall is the first."
    }

    Repeater {
        model: dashboard.groups
        delegate: Card {
            id: group
            required property var modelData
            Layout.fillWidth: true
            title: modelData.name
            subtitle: modelData.subtitle
            accentColor: themeCtl.appAccents[modelData.app] || theme.accentInk

            GridLayout {
                id: grid
                Layout.fillWidth: true
                columns: Math.max(1, Math.floor((width + columnSpacing) / (220 + columnSpacing)))
                rowSpacing: 12
                columnSpacing: 12

                Repeater {
                    model: group.modelData.cards
                    delegate: MetricCard {
                        required property var modelData
                        Layout.fillWidth: true
                        label: modelData.label
                        value: modelData.value
                        detail: modelData.detail || ""
                        tone: modelData.tone || ""
                        interactive: (modelData.target || "").length > 0
                        onClicked: navigation.go(modelData.target)
                    }
                }
            }

            Text {
                visible: group.modelData.cards.length === 0
                text: "Nothing to report."
                color: theme.textMuted
                font.family: theme.fontFamily
                font.pixelSize: theme.fontSize - 1
            }
        }
    }
}
