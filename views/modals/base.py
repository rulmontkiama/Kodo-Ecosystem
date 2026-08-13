"""
Base et styles partagés pour les modales Kōdo POS (Apple Luxury Style).
"""
import customtkinter as ctk

# Palette Kōdo POS Redesign (Adaptatif)
BG      = ("#FFFFFF", "#121214")
SEC_BG  = ("#F4F6F8", "#1C1C1E")
ACCENT  = ("#FF6B6B", "#FF6B6B") # Nouveau Corail Red Kōdo POS
TEXT    = ("#212529", "#FFFFFF")
GRY     = ("#868E96", "#98989D")
LINE    = ("#DEE2E6", "#2C2C2E")
RED     = ("#FF6B6B", "#FF453A")
GRN     = ("#28C76F", "#32D74B")

FNT_TITLE = "Inter"
FNT_BODY  = "Inter"
RAD = 20

class ToastNotification(ctk.CTkFrame):
    def __init__(self, parent, message, type="info", duration=3000):
        super().__init__(parent, corner_radius=12)
        
        if type == "success":
            self.configure(fg_color=GRN)
            icon = "✓"
        elif type == "error":
            self.configure(fg_color=RED)
            icon = "✕"
        elif type == "loading":
            self.configure(fg_color=ACCENT)
            icon = "⏳"
        else:
            self.configure(fg_color=TEXT)
            icon = "ℹ"
            
        self.lbl = ctk.CTkLabel(self, text=f"{icon}  {message}", font=ctk.CTkFont(FNT_BODY, 14, "bold"), text_color="#FFF")
        self.lbl.pack(padx=20, pady=12)
        
        self.place(relx=0.5, rely=0.9, anchor="center")
        self.after(10, self.lift)
        
        if type != "loading":
            self.after(duration, self.destroy)

def show_toast(parent, message, type="info", duration=3000):
    return ToastNotification(parent, message, type, duration)
