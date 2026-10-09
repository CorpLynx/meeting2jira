import QtQuick
import QtQuick.Controls
import QtQuick.Layouts


ApplicationWindow {
    id: root
    visible: true
    width: appSpecWidth
    height: appSpecHeight
    minimumWidth: appSpecMinWidth
    minimumHeight: appSpecMinHeight
    title: appController.title
    color: theme.background



    RowLayout {
        anchors.fill: parent
        spacing: 0
        Sidebar {
            Layout.fillHeight: true
            Layout.preferredWidth: theme.sidebarWidth
            title: appController.title
            subtitle: appController.subtitle
        }
        Rectangle {
            Layout:fillWidth: true
            Layout:fillHeight: true
            color: theme.background

            Loader {
                id: pageLoader
                anchors.fill: parent
                source: navigationController.currentPage
                asynchronous: false
                onStatusChanged: {
                    if (status === Loader.Error)
                        appController.setStatus("Could not load " + navigationController.current, "error")
                }
            }
        }
    }
    ToastHost {id: ToastHost }
    Connections {
        target: appController
        function onToastRequested(text, kind) {toastHost.show(text, kind)}
    }
}