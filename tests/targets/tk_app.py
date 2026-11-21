"""Test target: a small Tk app.

A periodic after() keeps some Python running so sys.remote_exec can fire
(an idle Tk mainloop sits in C and won't run the bootstrap; see M0).
"""
import sys
import threading
import tkinter

seconds = float(sys.argv[sys.argv.index("--seconds") + 1]) if "--seconds" in sys.argv else 60


def where():
    return threading.current_thread().name


def main():
    root = tkinter.Tk()
    root.title("exposure tk test target")
    root.geometry("240x60+40+40")
    label = tkinter.Label(root, text="hello")
    label.pack()
    ticks = [0]

    def tick():
        ticks[0] += 1
        root.after(100, tick)

    root.after(0, lambda: print("ready", flush=True))
    root.after(100, tick)
    root.after(int(seconds * 1000), root.destroy)
    root.mainloop()


if __name__ == "__main__":
    main()
