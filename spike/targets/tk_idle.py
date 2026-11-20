"""Tiny idle tkinter window for the injection test. Closes itself after 60 s.

No Python callbacks run while idle except the single after() that closes it,
so this shows whether a remote_exec script fires inside Tk's mainloop.
"""
import tkinter

root = tkinter.Tk()
root.title("exposure tk_idle target")
root.geometry("220x60")
tkinter.Label(root, text="idle target (closes in 60 s)").pack()
root.after(60_000, root.destroy)
root.mainloop()
