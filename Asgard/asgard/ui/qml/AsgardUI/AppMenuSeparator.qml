// A thin line between groups of entries in an AppMenu.
import QtQuick
import QtQuick.Controls

MenuSeparator {
    padding: 4
    topPadding: 4
    bottomPadding: 4
    contentItem: Rectangle {
        implicitHeight: 1
        color: theme.borderSoft
    }
}
