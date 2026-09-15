import tkinter as tk
import customtkinter as ctk

app = ctk.CTk()
var = ctk.StringVar()
ent1 = ctk.CTkEntry(app)
ent1.pack()
ent1.insert(0, "Test1")

ent2 = ctk.CTkEntry(app, textvariable=var)
ent2.pack()
ent2.insert(0, "Test2")

def print_vals():
    print("ent1:", ent1.get())
    print("ent2:", ent2.get())
    print("var:", var.get())
    app.destroy()

app.after(500, print_vals)
app.mainloop()
