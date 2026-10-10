// A pop-up menu in Asgard's look. Fill it with AppMenuItem and AppMenuSeparator.
import QtQuick
import QtQuick.Controls

Menu {
    id: root
    padding: 6
    implicitWidth: 248
    font.family: theme.fontFamily
    font.pixelSize: theme.fontSize
    background: Rectangle {
        implicitWidth: 248
        radius: theme.radiusMedium
        color: theme.surface
        border.color: theme.borderSoft
        border.width: 1
        Elevation {
            z: -1
            anchors.fill: parent
            radius: parent.radius
            level: 2
        }
    }
}
