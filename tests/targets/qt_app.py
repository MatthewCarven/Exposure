"""Test target: a small Qt (PySide6) app.

A QTimer keeps some Python running so sys.remote_exec can fire (an idle Qt
event loop sits in C, like Tk's).
"""
import sys
import threading

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QLabel

seconds = float(sys.argv[sys.argv.index("--seconds") + 1]) if "--seconds" in sys.argv else 60


def where():
    return threading.current_thread().name


def main():
    app = QApplication(sys.argv[:1])
    label = QLabel("hello")
    label.setWindowTitle("exposure qt test target")
    label.resize(240, 60)
    label.show()
    ticks = [0]

    def tick():
        ticks[0] += 1

    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(100)
    QTimer.singleShot(0, lambda: print("ready", flush=True))
    QTimer.singleShot(int(seconds * 1000), app.quit)
    app.exec()


if __name__ == "__main__":
    main()
