import sys
import os
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
import downloader

def verify():
    app = QApplication(sys.argv)

    # We just want to check if the app initializes without crashing
    window = downloader.VideoEditorApp()
    window.show()

    # Close after 1 second
    QTimer.singleShot(1000, app.quit)

    sys.exit(app.exec())

if __name__ == "__main__":
    verify()
