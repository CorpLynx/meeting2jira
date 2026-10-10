// The system's file chooser, for "where is this app?". Loaded on demand (see Home.qml), so if this
// Qt build has no file dialog, only the Browse button is lost; the path can still be typed.
import QtQuick
import QtQuick.Dialogs

FileDialog {
    id: root
    signal picked(string path)
    title: "Choose the file you use to start the app"
    nameFilters: ["Apps and scripts (*.pyw *.py *.exe *.ps1 *.cmd *.bat *.lnk)", "All files (*)"]
    onAccepted: root.picked(home.localPath(root.selectedFile.toString()))
}
