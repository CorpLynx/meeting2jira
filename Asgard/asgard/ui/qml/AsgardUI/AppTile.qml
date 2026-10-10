// One app on the Apps page. `tile` is a dict from HomeBackend.tiles(): id, name, description, monogram,
// color, colorTop, ink, pill, tone, badge, badgeHint, canOpen. Click or Enter opens it; a right click
// (or the Menu key) asks for its menu. The cell leaves an 8 px gap around the card for the shadow.
import QtQuick
import QtQuick.Layouts

Item {
    id: root
    property var tile: ({})
    property bool current: false
    readonly property bool hot: mouse.containsMouse || current
    signal activated()
    signal menuRequested(real x, real y)

    Accessible.role: Accessible.Button
    Accessible.name: tile.name + (tile.pill ? ", " + tile.pill : "")
    Accessible.description: tile.description + (tile.badge > 0 ? ". " + tile.badgeHint : "")
    Accessible.onPressAction: root.activated()

    Rectangle {
        id: card
        anchors.fill: parent
        anchors.margins: 8
        radius: theme.radiusLarge
        color: mouse.pressed ? theme.surfaceHigh : theme.surface
        border.width: root.current ? 2 : 1
        border.color: root.current ? theme.focus : (mouse.containsMouse ? Qt.alpha(root.tile.color, 0.6) : theme.borderSoft)
        scale: mouse.pressed ? 0.985 : 1
        Behavior on scale { NumberAnimation { duration: 90 } }
        Behavior on color { ColorAnimation { duration: 90 } }

        Elevation {
            z: -1
            anchors.fill: parent
            radius: card.radius
            level: root.hot ? 2 : 1
        }

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 18
            spacing: 0

            RowLayout {
                Layout.fillWidth: true
                spacing: 8
                Rectangle {
                    Layout.preferredWidth: 52
                    Layout.preferredHeight: 52
                    radius: 15
                    opacity: root.tile.canOpen ? 1 : 0.55
                    border.width: 1
                    border.color: Qt.alpha("#FFFFFF", 0.22)
                    gradient: Gradient {
                        GradientStop { position: 0.0; color: root.tile.colorTop }
                        GradientStop { position: 1.0; color: root.tile.color }
                    }
                    Accessible.ignored: true
                    Text {
                        anchors.centerIn: parent
                        text: root.tile.monogram
                        color: root.tile.ink
                        font.family: theme.fontFamily
                        font.pixelSize: 20
                        font.weight: Font.Bold
                    }
                }
                Item { Layout.fillWidth: true }
                Rectangle {
                    visible: root.tile.badge > 0
                    Layout.preferredHeight: 24
                    Layout.preferredWidth: Math.max(24, badgeText.implicitWidth + 14)
                    Layout.alignment: Qt.AlignTop
                    radius: 12
                    color: theme.accent
                    Accessible.ignored: true
                    Text {
                        id: badgeText
                        anchors.centerIn: parent
                        text: root.tile.badge > 99 ? "99+" : root.tile.badge
                        color: theme.accentText
                        font.family: theme.fontFamily
                        font.pixelSize: theme.fontSize - 2
                        font.weight: Font.Bold
                    }
                }
                Pill {
                    visible: root.tile.pill.length > 0
                    text: root.tile.pill
                    tone: root.tile.tone
                    dot: root.tile.tone === "success"
                    Layout.alignment: Qt.AlignTop
                }
            }

            Item { Layout.preferredHeight: 16 }
            Text {
                text: root.tile.name
                color: theme.text
                font.family: theme.fontFamily
                font.pixelSize: theme.fontSize + 3
                font.weight: Font.DemiBold
                elide: Text.ElideRight
                Layout.fillWidth: true
            }
            Text {
                text: root.tile.description
                color: theme.textMuted
                font.family: theme.fontFamily
                font.pixelSize: theme.fontSize - 1
                wrapMode: Text.WordWrap
                maximumLineCount: 2
                elide: Text.ElideRight
                Layout.fillWidth: true
                Layout.topMargin: 3
            }
            Item { Layout.fillHeight: true }
        }
    }

    MouseArea {
        id: mouse
        anchors.fill: parent
        anchors.margins: 8
        hoverEnabled: true
        acceptedButtons: Qt.LeftButton | Qt.RightButton
        cursorShape: root.tile.canOpen ? Qt.PointingHandCursor : Qt.ArrowCursor
        onClicked: function (m) {
            if (m.button === Qt.RightButton)
                root.menuRequested(m.x + 8, m.y + 8)
            else
                root.activated()
        }
    }
}
