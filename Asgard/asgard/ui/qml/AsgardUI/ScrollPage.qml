// A page whose content scrolls: put PageHeader and Cards inside; they stack in a column.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Flickable {
    id: root
    default property alias content: column.data
    property int maxWidth: 1100

    contentWidth: width
    contentHeight: column.implicitHeight + 56
    clip: true
    boundsBehavior: Flickable.StopAtBounds
    ScrollBar.vertical: ScrollBar {}

    ColumnLayout {
        id: column
        x: 28
        y: 28
        width: Math.max(200, Math.min(root.width - 56, root.maxWidth))
        spacing: 16
    }
}
