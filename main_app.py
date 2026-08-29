import os
os.environ["SYSTEM_VERSION_COMPAT"] = "0"
import customtkinter as ctk
import sys
import os
import sqlite3
import datetime
import json
from decimal import Decimal
from database_manager import get_connection, generer_numero_ticket, initialiser_db, resource_path, data_path, hash_pin
import views.stats_view as stats_view
from views.modals import NumpadModal, RemiseModal, EncaissementModal, ClientModal, ChangeReturnModal, PaniersEnAttenteModal, CrashRestorationModal
from core.crash_watcher import CrashWatcher
import ticket_printer
try:
    import barcode
    from barcode.writer import ImageWriter
except ImportError:
    barcode = None
    ImageWriter = None

ctk.set_appearance_mode("Light")
ctk.set_default_color_theme("blue")

# Palette Kōdo POS Redesign (Basée sur prototype React ~/Downloads/kōdo-pos/src)
BG        = ("#F4F6F8", "#121214")  # Canvas principal gris très léger / sombre
SEC_BG    = ("#E9ECEF", "#1C1C1E")  # Fond secondaire doux
CARD_BG   = ("#FFFFFF", "#1E1E22")  # Fond des cartes blanc pur
ACCENT    = ("#FF6B6B", "#FF6B6B")  # Nouveau Corail Red Kōdo POS
TEXT      = ("#212529", "#FFFFFF")  # Dark text / Light text
GRY       = ("#868E96", "#98989D")  # Secondary grey
LINE      = ("#DEE2E6", "#2C2C2E")  # Border grey
RED       = ("#FF6B6B", "#FF453A")  # Red accent
GRN       = ("#28C76F", "#32D74B")  # Emerald Green dispo
HOV       = ("#F8F9FA", "#2C2C2E")  # Hover state
LIGHT_GRN = ("#E6F8ED", "#1E3B2B")  # Badge stock vert
LIGHT_RED = ("#FFEBEB", "#3D1E1E")  # Badge alerte rouge

# Constantes de Style
FNT_TITLE = "Inter"
FNT_BODY  = "Inter"
RAD = 20
SHDW = {"color": "#000000", "alpha": 0.05, "blur": 20} # Pour référence conceptuelle

class LockScreen(ctk.CTkFrame):
    def __init__(self, parent, on_success):
        super().__init__(parent, fg_color=BG)
        self.on_success = on_success
        self.pin = ""
        self.correct_pin = self._get_correct_pin()
        app_name = "Kōdo POS"

        self.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure((0,1,2,3,4), weight=1)

        # Titre dynamique (Branding App)
        ctk.CTkLabel(self, text=app_name, font=ctk.CTkFont(FNT_TITLE, 54, "bold"), text_color=ACCENT).grid(row=0, column=0, pady=(60, 0))

        # Cercles PIN
        self.circles_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.circles_frame.grid(row=1, column=0)
        self.circles = []
        for i in range(4):
            c = ctk.CTkLabel(self.circles_frame, text="○", font=ctk.CTkFont(FNT_BODY, 32), text_color=GRY)
            c.grid(row=0, column=i, padx=12)
            self.circles.append(c)

        # Message d'erreur
        self.error_label = ctk.CTkLabel(self, text="", font=ctk.CTkFont(FNT_BODY, 14), text_color=RED)
        self.error_label.grid(row=2, column=0)

        # Pavé numérique
        numpad_f = ctk.CTkFrame(self, fg_color="transparent")
        numpad_f.grid(row=3, column=0, pady=20)

        buttons = [
            '1', '2', '3',
            '4', '5', '6',
            '7', '8', '9',
            '',  '0', '←'
        ]

        for i, b in enumerate(buttons):
            if b == '':
                # Espace vide
                continue
                
            if b == '←':
                btn_col = "transparent"
                txt_col = TEXT
                hov_col = SEC_BG
            else:
                btn_col = SEC_BG
                txt_col = TEXT
                hov_col = HOV

            btn = ctk.CTkButton(numpad_f, text=b, width=80, height=80, corner_radius=40,
                                font=ctk.CTkFont(FNT_BODY, 28, "bold"),
                                fg_color=btn_col, text_color=txt_col, hover_color=hov_col,
                                command=lambda x=b: self._press(x))
            btn.grid(row=i//3, column=i%3, padx=16, pady=12)

        self.parent = parent
        self.parent.bind("<Key>", self._on_key_press, add="+")

    def _get_correct_pin(self):
        try:
            conn = get_connection(); c = conn.cursor()
            c.execute("SELECT valeur FROM Parametres WHERE cle='pin_admin'")
            res = c.fetchone()
            return res[0] if res else "0000"
        except: return "0000"

    def _press(self, key):
        if key == '←':
            self.pin = self.pin[:-1]
            self.error_label.configure(text="")
        else:
            if len(self.pin) < 4: self.pin += key
        self._update_circles()
        if len(self.pin) == 4:
            self.after(200, self._verify)

    def _on_key_press(self, event):
        if not self.winfo_viewable(): return
        if event.char.isdigit() and len(self.pin) < 4:
            self.pin += event.char
            self._update_circles()
            if len(self.pin) == 4: self.after(200, self._verify)
        elif event.keysym == "BackSpace":
            self.pin = self.pin[:-1]; self._update_circles()
        elif event.keysym == "Return": self._verify()
        elif event.keysym == "Escape":
            self.pin = ""; self._update_circles()

    def _update_circles(self):
        for i, c in enumerate(self.circles):
            c.configure(text="●" if i < len(self.pin) else "○", text_color=ACCENT if i < len(self.pin) else GRY)

    def _verify(self):
        try:
            conn = get_connection(); c = conn.cursor()
            hashed_pin = hash_pin(self.pin)
            c.execute("SELECT nom, role_admin FROM Vendeurs WHERE pin=?", (hashed_pin,))
            res = c.fetchone()
            if res:
                self.destroy()
                self.on_success({"nom": res[0], "admin": bool(res[1])})
            else:
                self.pin = ""
                self._update_circles()
                self.error_label.configure(text="PIN incorrect")
                self.after(2000, lambda: self.error_label.configure(text=""))
        except: self.error_label.configure(text="Erreur base de données")

class LicenseScreen(ctk.CTkFrame):
    def __init__(self, parent, error_message, on_retry):
        super().__init__(parent, fg_color=BG)
        self.parent = parent
        self.on_retry = on_retry
        self.place(relx=0, rely=0, relwidth=1, relheight=1)
        
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure((0,1,2,3,4,5), weight=1)
        
        # Titre
        ctk.CTkLabel(self, text="Kōdo POS", font=ctk.CTkFont(FNT_TITLE, 54, "bold"), text_color=ACCENT).grid(row=0, column=0, pady=(60, 0))
        
        # Icône d'alerte / cadenas
        ctk.CTkLabel(self, text="🔒", font=ctk.CTkFont(FNT_BODY, 72)).grid(row=1, column=0, pady=10)
        
        # Message d'erreur
        ctk.CTkLabel(self, text="Licence Inactive ou Expirée", font=ctk.CTkFont(FNT_TITLE, 24, "bold"), text_color=TEXT).grid(row=2, column=0)
        
        self.err_desc = ctk.CTkLabel(self, text=error_message, font=ctk.CTkFont(FNT_BODY, 16), text_color=RED, wraplength=500)
        self.err_desc.grid(row=3, column=0, pady=10)
        
        # Hardware ID
        import license_manager
        fingerprint = license_manager.get_machine_fingerprint()
        hw_text = f"Identifiant Matériel (Hardware ID) : {fingerprint}"
        ctk.CTkLabel(self, text=hw_text, font=ctk.CTkFont(FNT_BODY, 14, "bold"), text_color=GRY).grid(row=4, column=0, pady=10)
        
        # Bouton Réessayer / Activer
        self.retry_btn = ctk.CTkButton(self, text="Réessayer l'activation", font=ctk.CTkFont(FNT_BODY, 16, "bold"),
                                       fg_color=ACCENT, text_color="#000000", hover_color=HOV,
                                       width=250, height=50, corner_radius=25,
                                       command=self._retry)
        self.retry_btn.grid(row=5, column=0, pady=(20, 60))

    def _retry(self):
        self.retry_btn.configure(state="disabled", text="Vérification...")
        self.update()
        import license_manager
        is_valid, msg = license_manager.check_license()
        if is_valid:
            self.destroy()
            self.on_retry()
        else:
            self.retry_btn.configure(state="normal", text="Réessayer l'activation")
            self.err_desc.configure(text=msg)

class MainApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.shop_name    = self._get_param("shop_name", "L'ADRESSE B")
        self.shop_subtitle = self._get_param("shop_subtitle", "Boutique de Mode")
        self.shop_address = self._get_param("shop_address", "Chemin Rue 53, 4960 Malmedy")
        self.shop_vat     = self._get_param("shop_vat", "BE 1035.331.577")
        self.title("Kōdo POS")
        
        # Initialisation de la licence et des features
        import license_manager
        self.app_license = license_manager.get_license_info()
        self.app_features = self.app_license.get("enabled_features", {})
        
        # Configuration responsive selon la résolution de l'écran (DPI Scaling Auto)
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()

        if screen_w < 1360 or screen_h < 768:
            try:
                ctk.set_widget_scaling(0.92)
                ctk.set_window_scaling(0.92)
            except Exception: pass
            width = min(1150, max(980, int(screen_w * 0.95)))
            height = min(680, max(600, int(screen_h * 0.90)))
        elif screen_w >= 2400:
            try:
                ctk.set_widget_scaling(1.15)
                ctk.set_window_scaling(1.15)
            except Exception: pass
            width = int(screen_w * 0.82)
            height = int(screen_h * 0.82)
        else:
            try:
                ctk.set_widget_scaling(1.0)
                ctk.set_window_scaling(1.0)
            except Exception: pass
            width = 1150
            height = 680

        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 2)

        self.geometry(f"{width}x{height}+{x}+{y}")
        self.minsize(980, 600)
        self.configure(fg_color=BG)

        # Overlay layer pour les modales in-window (compatible mode grand écran / plein écran macOS)
        self._overlay_layer = ctk.CTkFrame(self, fg_color="transparent")
        self._overlay_layer.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._overlay_layer.lower()

        try:
            from PIL import Image, ImageTk
            img = Image.open(resource_path("logo.png"))
            self.icon_photo = ImageTk.PhotoImage(img)
            self.wm_iconphoto(True, self.icon_photo)
        except: pass
        initialiser_db()
        ctk.set_appearance_mode("Light")
        self.current_view  = "Caisse"
        self.buffer_cb     = ""
        self.panier        = []
        self.total_tvac    = Decimal("0.00")
        self.remise        = Decimal("0.00")
        self.id_client     = None
        self.nom_client    = None
        self.locked        = True
        self.vendeur_actif = None
        self.active_modal  = None
        self.suggestion_popup = None
        self.stats_frame   = None
        self.caisse_frame  = None
        self.stocks_frame  = None
        self.retours_frame = None
        self.params_frame  = None
        self.frames        = {}
        self._last_key_press_time_caisse = 0

        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=0) # Sidebar
        self.grid_columnconfigure(1, weight=1) # Main View

        # ─── VÉRIFICATION DE LA LICENCE (DÉSACTIVÉE) ───
        self.lock_screen = LockScreen(self, self._on_login_success)

    def _on_license_activated(self):
        print("[LICENCE] Licence activée avec succès, affichage du LockScreen.")
        self.lock_screen = LockScreen(self, self._on_login_success)

    def _get_param(self, key, default):
        try:
            conn = get_connection()
            c = conn.cursor()
            c.execute("SELECT valeur FROM Parametres WHERE cle=?", (key,))
            res = c.fetchone()
            conn.close()
            return res[0] if res else default
        except:
            return default

    def _set_param(self, key, val):
        try:
            conn = get_connection()
            c = conn.cursor()
            c.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES (?, ?)", (key, str(val)))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"Error saving param {key}: {e}")

    def _show_toast(self, text, bg_color="#18181A", text_color="#FFFFFF"):
        """Affiche une mini-notification 'Toast' flottante en bas à droite."""
        try:
            if hasattr(self, "_active_toast") and self._active_toast and self._active_toast.winfo_exists():
                self._active_toast.destroy()
        except Exception:
            pass

        toast = ctk.CTkFrame(self, fg_color=bg_color, corner_radius=20, border_width=1, border_color="#333336")
        toast.place(relx=0.98, rely=0.93, anchor="se")
        
        lbl = ctk.CTkLabel(toast, text=text, font=ctk.CTkFont(FNT_BODY, 13, "bold"), text_color=text_color)
        lbl.pack(padx=18, pady=10)
        
        self._active_toast = toast
        self.after(2000, lambda: toast.destroy() if toast.winfo_exists() else None)

    def _on_login_success(self, vendeur):
        self.locked = False
        self.vendeur_actif = vendeur
        self.title(f"{self.shop_name} — Kōdo POS")
        self._build_side_nav()
        self._build_all_frames()
        self._charger_panier_session()
        self.bind("<Key>", self._on_key)
        self._update_badge_paniers_attente()
        self.afficher_caisse()
        self.after(500, self._verifier_reprise_crash)
        self.after(2000, self._verifier_mises_a_jour_au_demarrage)

    def _build_side_nav(self):
        nav_w = 230
        self.nav_bar = ctk.CTkFrame(self, width=nav_w, corner_radius=20, fg_color="#FFFFFF", border_width=1, border_color="#E9ECEF")
        self.nav_bar.grid(row=0, column=0, sticky="ns", padx=(16, 10), pady=16)
        self.nav_bar.grid_propagate(False)

        # En-tête Logo marque
        logo_frame = ctk.CTkFrame(self.nav_bar, fg_color="transparent")
        logo_frame.pack(fill="x", padx=16, pady=(20, 16))
        ctk.CTkLabel(logo_frame, text="KŌDO POS", font=ctk.CTkFont(FNT_TITLE, 22, "bold"), text_color="#212529").pack(anchor="w")
        ctk.CTkLabel(logo_frame, text="L'ADRESSE B • CAISSE", font=ctk.CTkFont(FNT_BODY, 10, "bold"), text_color="#868E96").pack(anchor="w")

        inner_nav = ctk.CTkFrame(self.nav_bar, fg_color="transparent")
        inner_nav.pack(expand=True, fill="both", padx=10, pady=5)

        self._nav_items = [
            ("Caisse",           self.afficher_caisse),
            ("Stocks",           self.afficher_stocks),
            ("Retours",          self.afficher_retours),
            ("Clôture & Stats",  self.afficher_stats),
            ("Paramètres",       self.afficher_params),
        ]
        self._nav_btns = {}

        for i, (label, cmd) in enumerate(self._nav_items):
            btn = ctk.CTkButton(
                inner_nav,
                text=label,
                height=44,
                font=ctk.CTkFont(FNT_BODY, 13, "bold"),
                fg_color="#FFFFFF",
                border_width=1,
                border_color="#DEE2E6",
                hover_color="#F8F9FA",
                text_color=TEXT, corner_radius=14,
                command=cmd
            )
            btn.pack(fill="x", pady=4)
            self._nav_btns[label] = btn

        # Actions bas de sidebar
        bottom_nav = ctk.CTkFrame(self.nav_bar, fg_color="transparent")
        bottom_nav.pack(side="bottom", fill="x", padx=10, pady=14)

        z_caisse_text = "🧾 Clôture Z (NF525)" if self.app_features.get("nf525_compliance", False) else "🧾 Clôture Z 🔒"
        self.btn_z_caisse = ctk.CTkButton(bottom_nav, text=z_caisse_text, height=42,
                      font=ctk.CTkFont(FNT_BODY, 11, "bold"),
                      fg_color="#1E854A", hover_color="#176B3B", text_color="#FFFFFF", corner_radius=14,
                      command=self._ouvrir_z_caisse
                      )
        self.btn_z_caisse.pack(fill="x", pady=3)

        self.btn_lock = ctk.CTkButton(bottom_nav, text="🔒 Verrouiller", height=42,
                      font=ctk.CTkFont(FNT_BODY, 12, "bold"),
                      fg_color="#FFFFFF", border_width=1, border_color="#FF6B6B",
                      text_color="#FF6B6B", hover_color="#FFEBEB", corner_radius=14,
                      command=self._verrouiller
                      )
        self.btn_lock.pack(fill="x", pady=3)

    def _verrouiller(self):
        self.locked = True
        for w in list(self.winfo_children()):
            try:
                w.destroy()
            except Exception:
                pass
        self.frames = {}
        self.stats_frame = None
        self.vendeur_actif = None
        self.title("Kōdo POS")
        self.lock_screen = LockScreen(self, self._on_login_success)

    def _set_active_nav(self, label):
        is_admin = self.vendeur_actif.get("admin", False) if self.vendeur_actif else False
        if label in ["Clôture & Stats", "Paramètres"] and not is_admin:
            self._st("Accès réservé aux administrateurs.", RED)
            return False

        for lbl, btn in self._nav_btns.items():
            active = (lbl == label)
            btn.configure(
                text_color="#FFFFFF" if active else TEXT,
                fg_color=ACCENT if active else "#FFFFFF",
                border_width=0 if active else 1,
                border_color="#DEE2E6" if not active else ACCENT,
                hover_color="#FF5554" if active else "#F8F9FA"
            )
        return True

    def _build_all_frames(self):
        self.frames = {}
        self.caisse_frame  = self._build_caisse()
        self.stocks_frame  = self._build_stocks()
        self.params_frame  = self._build_params()
        self.retours_frame = self._build_retours()
        self.stats_frame   = None
        self.frames = {"Caisse":  self.caisse_frame,
                       "Stocks":  self.stocks_frame,
                       "Params":  self.params_frame,
                       "Retours": self.retours_frame}

    def _placeholder(self, title):
        f = ctk.CTkFrame(self, fg_color=BG, corner_radius=24)
        ctk.CTkLabel(f, text=title.upper(), font=ctk.CTkFont(FNT_TITLE, 28, "bold"),
                     text_color=GRY).place(relx=.5, rely=.5, anchor="center")
        return f

    def _build_caisse(self):
        f = ctk.CTkFrame(self, fg_color=BG, corner_radius=24)
        f.grid_rowconfigure(1, weight=1); f.grid_columnconfigure(0, weight=1)

        hdr_top = ctk.CTkFrame(f, fg_color="transparent")
        hdr_top.grid(row=0, column=0, sticky="ew", padx=30, pady=(15, 10))
        ctk.CTkLabel(hdr_top, text="Caisse", font=ctk.CTkFont(FNT_TITLE, 32, "bold"), text_color=TEXT).pack(side="left")

        # Horloge temps réel
        self.lbl_clock = ctk.CTkLabel(hdr_top, text="", font=ctk.CTkFont(FNT_BODY, 15), text_color=GRY)
        self.lbl_clock.pack(side="left", padx=20)
        self._tick_clock()

        v_f = ctk.CTkFrame(hdr_top, fg_color="#FFFFFF", corner_radius=16, border_width=1, border_color="#E5E5EA")
        v_f.pack(side="right")
        admin_txt = self.vendeur_actif.get('nom', 'Administrateur') if self.vendeur_actif else 'Administrateur'
        ctk.CTkLabel(v_f, text=admin_txt, font=ctk.CTkFont(FNT_BODY, 12, "bold"), text_color=TEXT).pack(padx=16, pady=6)

        body = ctk.CTkFrame(f, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew", padx=20, pady=(0, 15))
        body.grid_columnconfigure(0, weight=18) # Panier / Recherche
        body.grid_columnconfigure(1, weight=10) # Panneau règlement ~340px
        body.grid_rowconfigure(0, weight=1)

        panier_f = ctk.CTkFrame(body, fg_color="transparent")
        panier_f.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        panier_f.grid_rowconfigure(1, weight=1); panier_f.grid_columnconfigure(0, weight=1)

        # 1. Barre de recherche scanner moderne
        scan_f = ctk.CTkFrame(panier_f, fg_color="#FFFFFF", corner_radius=16, border_width=1, border_color="#E5E5EA")
        scan_f.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        
        search_icon = ctk.CTkLabel(scan_f, text="🔍", font=ctk.CTkFont(FNT_BODY, 14), text_color=GRY)
        search_icon.pack(side="left", padx=(16, 4))

        self.entry_caisse = ctk.CTkEntry(scan_f, placeholder_text="Rechercher un produit ou un service...",
                                         font=ctk.CTkFont(FNT_BODY, 14), height=48,
                                         fg_color="transparent", border_width=0, text_color=TEXT)
        self.entry_caisse.pack(side="left", padx=4, fill="x", expand=True)
        self.entry_caisse.bind("<Return>", lambda e: self._valider_entry_caisse())
        self.entry_caisse.bind("<KeyRelease>", self._on_key_release_caisse)
        self.entry_caisse.bind("<FocusOut>", lambda e: self.after(250, self._fermer_suggestions_caisse))

        self.var_caisse = ctk.StringVar()
        self.entry_caisse.configure(textvariable=self.var_caisse)
        self._in_trace_caisse = False
        def _trace_caisse(*args):
            if getattr(self, "_in_trace_caisse", False): return
            val = self.var_caisse.get()
            if not val: return
            azerty_map = str.maketrans('&é"\'(§-è!_çà°', '1234566788900')
            if any(c in '&é"\'(§-è!_çà°' for c in val):
                new_val = val.translate(azerty_map)
                if new_val != val:
                    self._in_trace_caisse = True
                    self.var_caisse.set(new_val)
                    self._in_trace_caisse = False
                    self.entry_caisse.after_idle(lambda: self.entry_caisse.icursor("end") if hasattr(self, "entry_caisse") and self.entry_caisse.winfo_exists() else None)
        self.var_caisse.trace_add("write", _trace_caisse)

        # 2. Raccourcis cartes d'actions rapides (Client, Prestation, Remise)
        quick_actions_bar = ctk.CTkFrame(panier_f, fg_color="transparent")
        quick_actions_bar.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        quick_actions_bar.grid_columnconfigure((0, 1, 2), weight=1)

        self.btn_client = ctk.CTkButton(quick_actions_bar, text="👤+\nClient", height=54,
                                        font=ctk.CTkFont(FNT_BODY, 11, "bold"),
                                        fg_color="#FFFFFF", text_color=TEXT, border_width=1, border_color="#E5E5EA",
                                        corner_radius=14, hover_color="#F8F9FA",
                                        command=self._ouvrir_client)
        self.btn_client.grid(row=0, column=0, padx=(0, 4), sticky="ew")

        btn_prestation = ctk.CTkButton(quick_actions_bar, text="🛒\nPrestation", height=54,
                                       font=ctk.CTkFont(FNT_BODY, 11, "bold"),
                                       fg_color="#FFFFFF", text_color=TEXT, border_width=1, border_color="#E5E5EA",
                                       corner_radius=14, hover_color="#F8F9FA",
                                       command=self._ouvrir_prestation)
        btn_prestation.grid(row=0, column=1, padx=2, sticky="ew")

        btn_remise = ctk.CTkButton(quick_actions_bar, text="%\nRemise", height=54,
                                   font=ctk.CTkFont(FNT_BODY, 11, "bold"),
                                   fg_color="#FFFFFF", text_color=TEXT, border_width=1, border_color="#E5E5EA",
                                   corner_radius=14, hover_color="#F8F9FA",
                                   command=self._ouvrir_remise)
        btn_remise.grid(row=0, column=2, padx=(4, 0), sticky="ew")

        # 3. Zone Scrollable du Panier
        self.panier_scroll = ctk.CTkScrollableFrame(panier_f, fg_color="transparent",
                                                    scrollbar_button_color="#E5E5EA",
                                                    scrollbar_button_hover_color="#D1D1D6")
        self.panier_scroll.grid(row=2, column=0, sticky="nsew", pady=(5, 0))
        self.panier_scroll.grid_columnconfigure(0, weight=1)

        # 4. Panneau latéral de règlement moderne (Fond bleu-gris très doux #EEF3FA avec touches pavé numérique blanc)
        checkout_f = ctk.CTkFrame(body, fg_color="#EEF3FA", corner_radius=24, border_width=1, border_color="#E0E6F0", width=350)
        checkout_f.grid(row=0, column=1, sticky="nsew")
        checkout_f.grid_columnconfigure(0, weight=1)

        # En-tête & Montant Total géant
        tot_card = ctk.CTkFrame(checkout_f, fg_color="transparent")
        tot_card.pack(fill="x", padx=16, pady=(18, 10))
        
        hdr_row = ctk.CTkFrame(tot_card, fg_color="transparent")
        hdr_row.pack(fill="x")
        ctk.CTkLabel(hdr_row, text="Règlement Vente", font=ctk.CTkFont(FNT_TITLE, 15, "bold"), text_color=TEXT).pack(side="left")
        self.label_nb = ctk.CTkLabel(hdr_row, text="0 art.", font=ctk.CTkFont(FNT_BODY, 12), text_color=GRY)
        self.label_nb.pack(side="right")

        self.label_total = ctk.CTkLabel(tot_card, text="0,00 €", font=ctk.CTkFont(FNT_TITLE, 42, "bold"), text_color=ACCENT)
        self.label_total.pack(pady=(6, 2))

        sub_info_f = ctk.CTkFrame(tot_card, fg_color="transparent")
        sub_info_f.pack(fill="x", pady=(0, 2))
        self.lbl_htva = ctk.CTkLabel(sub_info_f, text="HTVA : 0.00 €", font=ctk.CTkFont(FNT_BODY, 11), text_color=GRY)
        self.lbl_htva.pack(side="left")
        self.lbl_tva_21 = ctk.CTkLabel(sub_info_f, text="TVA : 0.00 €", font=ctk.CTkFont(FNT_BODY, 11), text_color=GRY)
        self.lbl_tva_21.pack(side="right")

        self.label_remise = ctk.CTkLabel(tot_card, text="", font=ctk.CTkFont(FNT_BODY, 11, "bold"), text_color=RED)
        self.label_remise.pack()

        # Pavé Numérique Tactile (Touches Blanches sur fond bleu-gris)
        num_wrap = ctk.CTkFrame(checkout_f, fg_color="transparent")
        num_wrap.pack(fill="x", padx=12, pady=6)
        num_wrap.grid_columnconfigure((0, 1, 2), weight=1)

        def _num_press(k):
            if k == "←":
                cur = self.entry_caisse.get()
                self.entry_caisse.delete(0, "end"); self.entry_caisse.insert(0, cur[:-1])
            elif k == "OK": self._valider_entry_caisse()
            else: self.entry_caisse.insert("end", k)

        n_keys = [("7","8","9"), ("4","5","6"), ("1","2","3"), ("←","0","OK")]
        for r, row_keys in enumerate(n_keys):
            for c, k in enumerate(row_keys):
                if k == "OK":
                    btn_col = ACCENT
                    txt_col = "#FFFFFF"
                    hov_col = "#FF5554"
                elif k == "←":
                    btn_col = "#FFFFFF"
                    txt_col = RED
                    hov_col = "#FFEBEB"
                else:
                    btn_col = "#FFFFFF"
                    txt_col = TEXT
                    hov_col = "#F8F9FA"

                ctk.CTkButton(num_wrap, text=k, font=ctk.CTkFont(FNT_BODY, 18, "bold"),
                              fg_color=btn_col, text_color=txt_col,
                              height=50, corner_radius=14,
                              hover_color=hov_col,
                              command=lambda val=k: _num_press(val)).grid(row=r, column=c, padx=3, pady=3, sticky="ew")

        # Mode de Règlement Direct (CB / Espèces / QR)
        ctk.CTkLabel(checkout_f, text="Mode de règlement :", font=ctk.CTkFont(FNT_BODY, 11, "bold"), text_color=TEXT).pack(anchor="w", padx=16, pady=(8, 4))
        pay_selector_frame = ctk.CTkFrame(checkout_f, fg_color="transparent")
        pay_selector_frame.pack(fill="x", padx=12, pady=(0, 6))
        pay_selector_frame.grid_columnconfigure((0, 1, 2), weight=1)

        self.current_pay_mode = "Bancontact"

        self.btn_mode_cb = ctk.CTkButton(pay_selector_frame, text="CB", height=40, font=ctk.CTkFont(FNT_BODY, 12, "bold"), fg_color=ACCENT, text_color="#FFFFFF", corner_radius=12, command=lambda: self._set_pay_method("Bancontact"))
        self.btn_mode_cb.grid(row=0, column=0, padx=2, sticky="ew")

        self.btn_mode_esp = ctk.CTkButton(pay_selector_frame, text="Espèces", height=40, font=ctk.CTkFont(FNT_BODY, 12, "bold"), fg_color="#FFFFFF", text_color=TEXT, corner_radius=12, command=lambda: self._set_pay_method("Espèces"))
        self.btn_mode_esp.grid(row=0, column=1, padx=2, sticky="ew")

        self.btn_mode_qr = ctk.CTkButton(pay_selector_frame, text="QR", height=40, font=ctk.CTkFont(FNT_BODY, 12, "bold"), fg_color="#FFFFFF", text_color=TEXT, corner_radius=12, command=lambda: self._set_pay_method("QR_Code"))
        self.btn_mode_qr.grid(row=0, column=2, padx=2, sticky="ew")

        # Calculateur de Monnaie Espèces
        self.cash_calc_frame = ctk.CTkFrame(checkout_f, fg_color="transparent")
        self.cash_calc_frame.pack(fill="x", padx=12, pady=(4, 8))
        
        preset_frame = ctk.CTkFrame(self.cash_calc_frame, fg_color="transparent")
        preset_frame.pack(fill="x", padx=0, pady=(4, 2))
        preset_frame.grid_columnconfigure((0, 1, 2, 3), weight=1)

        presets = [("10 €", 10), ("20 €", 20), ("50 €", 50), ("Exact", "exact")]
        for p_idx, (p_label, p_val) in enumerate(presets):
            ctk.CTkButton(preset_frame, text=p_label, height=32, font=ctk.CTkFont(FNT_BODY, 11, "bold"), fg_color="#FFFFFF", text_color=TEXT, corner_radius=8, command=lambda v=p_val: self._preset_cash(v)).grid(row=0, column=p_idx, padx=2, sticky="ew")

        cash_input_row = ctk.CTkFrame(self.cash_calc_frame, fg_color="transparent")
        cash_input_row.pack(fill="x", padx=4, pady=(4, 6))
        
        ctk.CTkLabel(cash_input_row, text="Reçu (€) :", font=ctk.CTkFont(FNT_BODY, 11, "bold"), text_color=TEXT).pack(side="left", padx=(2, 4))
        self.entry_cash_received = ctk.CTkEntry(cash_input_row, placeholder_text="0.00", height=34, width=70, font=ctk.CTkFont(FNT_BODY, 12, "bold"), fg_color="#FFFFFF", border_width=1, border_color="#E0E6F0")
        self.entry_cash_received.pack(side="left", padx=2)
        self.entry_cash_received.bind("<KeyRelease>", lambda e: self._recalculer_rendu())

        self.lbl_rendu_monnaie = ctk.CTkLabel(cash_input_row, text="Rendu : 0,00 €", font=ctk.CTkFont(FNT_BODY, 11, "bold"), text_color="#28C76F")
        self.lbl_rendu_monnaie.pack(side="right", padx=4)

        # Grand Bouton d'Encaissement Direct Corail
        self.btn_valider_encaissement = ctk.CTkButton(
            checkout_f,
            text="VALIDER LE PAIEMENT (0,00 €)",
            height=54,
            font=ctk.CTkFont(FNT_BODY, 13, "bold"),
            fg_color=ACCENT,
            text_color="#FFFFFF",
            hover_color="#FF5554",
            corner_radius=16,
            command=self._executer_encaissement_direct
        )
        self.btn_valider_encaissement.pack(fill="x", padx=12, pady=(6, 10))

        # Actions secondaires en bas de carte
        sub_actions_frame = ctk.CTkFrame(checkout_f, fg_color="transparent")
        sub_actions_frame.pack(fill="x", padx=12, pady=(0, 14))
        sub_actions_frame.grid_columnconfigure((0, 1), weight=1)

        self.btn_paniers_attente = ctk.CTkButton(sub_actions_frame, text="Attente (0)", height=34, font=ctk.CTkFont(FNT_BODY, 11, "bold"), fg_color="#FFFFFF", text_color=TEXT, corner_radius=10, command=self._ouvrir_paniers_en_attente_modal)
        self.btn_paniers_attente.grid(row=0, column=0, padx=2, pady=2, sticky="ew")

        ctk.CTkButton(sub_actions_frame, text="+ En attente", height=34, font=ctk.CTkFont(FNT_BODY, 11), fg_color="#FFFFFF", text_color=TEXT, corner_radius=10, command=self._mettre_panier_en_attente).grid(row=0, column=1, padx=2, pady=2, sticky="ew")
        ctk.CTkButton(sub_actions_frame, text="Annuler", height=34, font=ctk.CTkFont(FNT_BODY, 11), fg_color="transparent", text_color=RED, corner_radius=10, command=self.vider_panier).grid(row=1, column=0, padx=2, pady=2, sticky="ew")
        ctk.CTkButton(sub_actions_frame, text="Réimprimer", height=34, font=ctk.CTkFont(FNT_BODY, 11), fg_color="#FFFFFF", text_color=TEXT, corner_radius=10, command=self._reimprimer_dernier_ticket).grid(row=1, column=1, padx=2, pady=2, sticky="ew")

        self.statut = ctk.CTkLabel(f, text="", font=ctk.CTkFont(FNT_BODY, 12), text_color=GRY)
        self.statut.place(relx=0.5, rely=0.05, anchor="center")
        return f

    def _build_stocks(self):
        f = ctk.CTkFrame(self, fg_color=BG, corner_radius=24)
        f.grid_rowconfigure(3, weight=1); f.grid_columnconfigure(0, weight=1)

        # 1. En-tête + KPI Badges (haut droite)
        hdr = ctk.CTkFrame(f, fg_color="transparent")
        hdr.grid(row=0, column=0, sticky="ew", padx=30, pady=(20, 10))
        ctk.CTkLabel(hdr, text="Inventaire", font=ctk.CTkFont(FNT_TITLE, 32, "bold"), text_color=TEXT).pack(side="left")

        # KPI Badges
        kpi_f = ctk.CTkFrame(hdr, fg_color="transparent")
        kpi_f.pack(side="right")
        self.kpi_ref = ctk.CTkLabel(kpi_f, text="0 ref", font=ctk.CTkFont(FNT_BODY, 13, "bold"), text_color=TEXT, fg_color="#FFFFFF", corner_radius=16, padx=16, pady=6)
        self.kpi_ref.pack(side="left", padx=4)
        self.kpi_stock = ctk.CTkLabel(kpi_f, text="0 pcs", font=ctk.CTkFont(FNT_BODY, 13, "bold"), text_color=ACCENT, fg_color="#FFEAEA", corner_radius=16, padx=16, pady=6)
        self.kpi_stock.pack(side="left", padx=4)
        self.kpi_rupture = ctk.CTkLabel(kpi_f, text="• 0 alerte(s)", font=ctk.CTkFont(FNT_BODY, 13, "bold"), text_color="#FF4D4D", fg_color="#FFEBEB", corner_radius=16, padx=16, pady=6)
        self.kpi_rupture.pack(side="left", padx=4)

        # 2. Barre d'outils (Recherche + Bascule vue Grille/Liste + Actions)
        tool_f = ctk.CTkFrame(f, fg_color="transparent")
        tool_f.grid(row=1, column=0, sticky="ew", padx=30, pady=(0, 12))

        search_container = ctk.CTkFrame(tool_f, fg_color="#FFFFFF", corner_radius=20, border_width=1, border_color="#E5E5EA", height=44)
        search_container.pack(side="left", padx=(0, 12))

        ctk.CTkLabel(search_container, text="🔍", font=ctk.CTkFont(FNT_BODY, 13), text_color=GRY).pack(side="left", padx=(14, 4))
        self.entry_stocks = ctk.CTkEntry(search_container, placeholder_text="Rechercher un produit, un code-barres...",
                                         width=280, height=42, fg_color="transparent", border_width=0, text_color=TEXT)
        self.entry_stocks.pack(side="left", padx=(0, 10))
        self.entry_stocks.bind("<Return>", lambda e: self._valider_entry_stocks())
        self.entry_stocks.bind("<KeyRelease>", lambda e: self._on_stock_search_change())

        # Bascule Mode Liste / Mode Grille (Grille sélectionné par défaut comme sur la maquette)
        self.stock_view_mode = getattr(self, 'stock_view_mode', "grid")
        
        switch_container = ctk.CTkFrame(tool_f, fg_color="#FFFFFF", corner_radius=20, border_width=1, border_color="#E5E5EA", height=44)
        switch_container.pack(side="left", padx=(0, 12))

        self.btn_view_list = ctk.CTkButton(switch_container, text="Liste", width=70, height=36, corner_radius=16,
                                           font=ctk.CTkFont(FNT_BODY, 12, "bold"),
                                           fg_color=ACCENT if self.stock_view_mode == "list" else "transparent",
                                           text_color="#FFFFFF" if self.stock_view_mode == "list" else TEXT,
                                           command=lambda: self._set_stock_view_mode("list"))
        self.btn_view_list.pack(side="left", padx=3, pady=3)
        
        self.btn_view_grid = ctk.CTkButton(switch_container, text="Grille", width=70, height=36, corner_radius=16,
                                           font=ctk.CTkFont(FNT_BODY, 12, "bold"),
                                           fg_color=ACCENT if self.stock_view_mode == "grid" else "transparent",
                                           text_color="#FFFFFF" if self.stock_view_mode == "grid" else TEXT,
                                           command=lambda: self._set_stock_view_mode("grid"))
        self.btn_view_grid.pack(side="left", padx=(0, 3), pady=3)

        self.var_stocks = ctk.StringVar()
        self.entry_stocks.configure(textvariable=self.var_stocks)
        self._in_trace_stocks = False
        def _trace_stocks(*args):
            if getattr(self, "_in_trace_stocks", False): return
            val = self.var_stocks.get()
            if not val: return
            azerty_map = str.maketrans('&é"\'(§-è!_çà°', '1234566788900')
            if any(c in '&é"\'(§-è!_çà°' for c in val):
                new_val = val.translate(azerty_map)
                if new_val != val:
                    self._in_trace_stocks = True
                    self.var_stocks.set(new_val)
                    self._in_trace_stocks = False
                    self.entry_stocks.after_idle(lambda: self.entry_stocks.icursor("end") if hasattr(self, "entry_stocks") and self.entry_stocks.winfo_exists() else None)
        self.var_stocks.trace_add("write", _trace_stocks)

        # Groupe d'actions unifiées à droite
        actions_group = ctk.CTkFrame(tool_f, fg_color="transparent")
        actions_group.pack(side="right")

        ctk.CTkButton(actions_group, text="+ Nouveau Produit", height=42, fg_color=ACCENT, text_color="#FFFFFF",
                      font=ctk.CTkFont(FNT_BODY, 12, "bold"), corner_radius=14, hover_color="#FF5554",
                      command=self._nouveau_produit).pack(side="left", padx=4)

        ctk.CTkButton(actions_group, text="Catégories", height=42, fg_color="#FFFFFF", text_color=TEXT,
                      border_width=1, border_color="#E5E5EA",
                      font=ctk.CTkFont(FNT_BODY, 12, "bold"), corner_radius=14, hover_color="#F8F9FA",
                      command=self._gerer_categories).pack(side="left", padx=4)

        ctk.CTkButton(actions_group, text="Étiquette Code-barres", height=42, fg_color="#FFFFFF", text_color=TEXT,
                      border_width=1, border_color="#E5E5EA",
                      font=ctk.CTkFont(FNT_BODY, 12, "bold"), corner_radius=14, hover_color="#F8F9FA",
                      command=self._imprimer_etiquette).pack(side="left", padx=4)

        ctk.CTkButton(actions_group, text="Supprimer", height=42, fg_color="#FFFFFF", text_color=RED,
                      border_width=1, border_color="#FF6F6E",
                      font=ctk.CTkFont(FNT_BODY, 12, "bold"), corner_radius=14, hover_color="#FFEBEB",
                      command=self._supprimer_produits_selection).pack(side="left", padx=4)

        # 3. Barre des catégories (Pilules horizontales)
        self.active_category = "Toutes"
        self.categories_scroll = ctk.CTkScrollableFrame(f, fg_color="transparent", height=45, orientation="horizontal")
        self.categories_scroll.grid(row=2, column=0, sticky="ew", padx=30, pady=(0, 10))
        self._refresh_categories_tabs()

        # 4. Zone centrale scrollable + Pagination footer
        center_f = ctk.CTkFrame(f, fg_color="transparent")
        center_f.grid(row=3, column=0, sticky="nsew", padx=25, pady=(0, 10))
        center_f.grid_rowconfigure(0, weight=1)
        center_f.grid_columnconfigure(0, weight=1)

        self.stocks_scroll = ctk.CTkScrollableFrame(center_f, fg_color="transparent")
        self.stocks_scroll.grid(row=0, column=0, sticky="nsew")
        self.stocks_scroll.grid_columnconfigure((0, 1, 2, 3), weight=1)

        # Barre de pagination
        pag_f = ctk.CTkFrame(center_f, fg_color="transparent")
        pag_f.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        
        self.btn_prev_page = ctk.CTkButton(pag_f, text="◀ Précédent", width=110, height=36, corner_radius=18,
                                           fg_color="#FFFFFF", border_width=1, border_color="#E5E5EA",
                                           text_color=TEXT, font=ctk.CTkFont(FNT_BODY, 12, "bold"),
                                           command=self._prev_stock_page)
        self.btn_prev_page.pack(side="left")

        self.lbl_page_info = ctk.CTkLabel(pag_f, text="Page 1 sur 1 (0 articles)", font=ctk.CTkFont(FNT_BODY, 12), text_color=GRY)
        self.lbl_page_info.pack(side="left", expand=True)

        self.btn_next_page = ctk.CTkButton(pag_f, text="Suivant ▶", width=110, height=36, corner_radius=18,
                                           fg_color="#FFFFFF", border_width=1, border_color="#E5E5EA",
                                           text_color=TEXT, font=ctk.CTkFont(FNT_BODY, 12, "bold"),
                                           command=self._next_stock_page)
        self.btn_next_page.pack(side="right")

        self.stock_current_page = 1
        self.stock_per_page = 16

        return f

    def _set_stock_view_mode(self, mode):
        self.stock_view_mode = mode
        self.btn_view_grid.configure(fg_color=ACCENT if mode == "grid" else "transparent",
                                    text_color="#FFFFFF" if mode == "grid" else TEXT)
        self.btn_view_list.configure(fg_color=ACCENT if mode == "list" else "transparent",
                                    text_color="#FFFFFF" if mode == "list" else TEXT)
        self._refresh_stocks_table()

    def _on_stock_search_change(self):
        if hasattr(self, '_search_timer') and self._search_timer:
            try:
                self.after_cancel(self._search_timer)
            except Exception:
                pass
        self._search_timer = self.after(200, self._exec_stock_search)

    def _exec_stock_search(self):
        self._search_timer = None
        self.stock_current_page = 1
        self._refresh_stocks_table()

    def _prev_stock_page(self):
        if hasattr(self, 'stock_current_page') and self.stock_current_page > 1:
            self.stock_current_page -= 1
            self._refresh_stocks_table()

    def _next_stock_page(self):
        if hasattr(self, 'stock_current_page') and hasattr(self, '_stock_max_pages') and self.stock_current_page < self._stock_max_pages:
            self.stock_current_page += 1
            self._refresh_stocks_table()

    def _gerer_categories(self):
        from views.modals import GestionCategoriesModal
        GestionCategoriesModal(self)

    def _refresh_categories_tabs(self):
        """Rafraîchit les onglets de catégories avec l'onglet Alerte sous forme de pilules."""
        for w in self.categories_scroll.winfo_children(): w.destroy()
        
        cats = ["Toutes", "Alertes"]
        try:
            conn = get_connection(); c = conn.cursor()
            c.execute("SELECT nom FROM Categories ORDER BY nom")
            for r in c.fetchall(): cats.append(r[0])
            conn.close()
        except Exception:
            pass

        for cat in cats:
            is_active = (cat == self.active_category)
            if cat == "Alertes":
                bg_col = ACCENT if is_active else "#FFFFFF"
                border_col = ACCENT if is_active else "#FF6F6E"
                txt_col = "#FFFFFF" if is_active else "#FF6F6E"
            else:
                bg_col = ACCENT if is_active else "#FFFFFF"
                border_col = ACCENT if is_active else "#E5E5EA"
                txt_col = "#FFFFFF" if is_active else TEXT

            btn = ctk.CTkButton(self.categories_scroll, text=cat, height=36,
                                fg_color=bg_col, text_color=txt_col,
                                border_width=0 if is_active else 1, border_color=border_col,
                                font=ctk.CTkFont(FNT_BODY, 12, "bold" if is_active else "normal"), corner_radius=18,
                                command=lambda c=cat: self._set_category(c))
            btn.pack(side="left", padx=4)

    def _set_category(self, cat):
        self.active_category = cat
        self.stock_current_page = 1
        self._refresh_categories_tabs()
        self._refresh_stocks_table()

    def _refresh_stocks_table(self):
        for w in self.stocks_scroll.winfo_children(): w.destroy()
        conn = None
        try:
            conn = get_connection(); c = conn.cursor()
            
            # --- 1. Calcul des statistiques KPI globales ---
            c.execute("SELECT COUNT(*) FROM Produits")
            total_prods = c.fetchone()[0] or 0
            
            c.execute("SELECT SUM(quantite_actuelle) FROM Stocks")
            total_pcs = c.fetchone()[0] or 0
            
            c.execute("SELECT COUNT(DISTINCT id_produit) FROM Stocks WHERE quantite_actuelle <= seuil_alerte")
            total_alertes = c.fetchone()[0] or 0

            if hasattr(self, 'kpi_ref'):
                self.kpi_ref.configure(text=f"{total_prods} ref")
                self.kpi_stock.configure(text=f"{total_pcs} pcs")
                self.kpi_rupture.configure(text=f"{total_alertes} alerte(s)")

            # --- 2. Requête filtrée ---
            query = """SELECT id, code_barre, nom, categorie, prix_vente_tvac, image_path, en_solde, prix_solde_tvac
                       FROM Produits"""
            conditions = []
            params = []
            
            search_term = self.entry_stocks.get().strip() if hasattr(self, 'entry_stocks') else ""
            if search_term:
                conditions.append("(code_barre LIKE ? OR nom LIKE ? OR categorie LIKE ? OR marque LIKE ?)")
                params.extend([f"%{search_term}%", f"%{search_term}%", f"%{search_term}%", f"%{search_term}%"])

            if hasattr(self, 'active_category'):
                if self.active_category == "Alertes":
                    conditions.append("id IN (SELECT id_produit FROM Stocks WHERE quantite_actuelle <= seuil_alerte)")
                elif self.active_category != "Toutes":
                    conditions.append("categorie = ?")
                    params.append(self.active_category)

            if conditions:
                query += " WHERE " + " AND ".join(conditions)
                
            if search_term:
                query += """ ORDER BY 
                              CASE 
                                WHEN code_barre = ? THEN 1
                                WHEN code_barre LIKE ? THEN 2
                                WHEN LOWER(nom) = ? THEN 3
                                WHEN LOWER(nom) LIKE ? THEN 4
                                WHEN LOWER(marque) LIKE ? THEN 5
                                WHEN LOWER(categorie) LIKE ? THEN 6
                                ELSE 7 
                              END ASC,
                              LENGTH(code_barre) ASC,
                              code_barre ASC,
                              nom ASC"""
                st_lower = search_term.lower()
                params.extend([
                    search_term, f"{search_term}%", 
                    st_lower, f"{st_lower}%", 
                    f"{st_lower}%", f"{st_lower}%"
                ])
            else:
                query += " ORDER BY nom ASC"
            
            c.execute(query, params)
            all_products = c.fetchall()
            
            # --- 3. Gestion de la pagination ---
            total_found = len(all_products)
            per_page = getattr(self, 'stock_per_page', 16)
            self._stock_max_pages = max(1, (total_found + per_page - 1) // per_page)
            
            if not hasattr(self, 'stock_current_page') or self.stock_current_page < 1:
                self.stock_current_page = 1
            if self.stock_current_page > self._stock_max_pages:
                self.stock_current_page = self._stock_max_pages
                
            page = self.stock_current_page
            start_idx = (page - 1) * per_page
            end_idx = start_idx + per_page
            page_products = all_products[start_idx:end_idx]

            if hasattr(self, 'lbl_page_info'):
                self.lbl_page_info.configure(text=f"Page {page} sur {self._stock_max_pages} ({total_found} articles)")
            if hasattr(self, 'btn_prev_page'):
                self.btn_prev_page.configure(state="normal" if page > 1 else "disabled")
            if hasattr(self, 'btn_next_page'):
                self.btn_next_page.configure(state="normal" if page < self._stock_max_pages else "disabled")

            self._stock_rows = []
            self._stock_product_ids = []
            self._selected_product_ids = set()
            self._selected_product_id = None
            view_mode = getattr(self, 'stock_view_mode', "list")

            # Pré-chargement des variantes et stocks pour la page en 1 seule requête batch
            stock_map = {}
            if page_products:
                pids = [p[0] for p in page_products]
                placeholders = ",".join("?" * len(pids))
                c.execute(f"SELECT id_produit, taille, quantite_actuelle, seuil_alerte FROM Stocks WHERE id_produit IN ({placeholders})", pids)
                for id_p, t, q, s in c.fetchall():
                    if id_p not in stock_map:
                        stock_map[id_p] = []
                    stock_map[id_p].append((t, q, s))

            if view_mode == "list":
                # --- MODE LISTE / TABLEAU "SHOPIFY POS" ---
                tbl_hdr = ctk.CTkFrame(self.stocks_scroll, fg_color=SEC_BG, corner_radius=12)
                tbl_hdr.pack(fill="x", padx=5, pady=(0, 6))
                
                ctk.CTkLabel(tbl_hdr, text="Code-barres", font=ctk.CTkFont(FNT_BODY, 12, "bold"), text_color=GRY, width=130, anchor="w").pack(side="left", padx=15, pady=8)
                ctk.CTkLabel(tbl_hdr, text="Désignation", font=ctk.CTkFont(FNT_BODY, 12, "bold"), text_color=GRY, anchor="w").pack(side="left", expand=True, fill="x", padx=10)
                ctk.CTkLabel(tbl_hdr, text="Catégorie", font=ctk.CTkFont(FNT_BODY, 12, "bold"), text_color=GRY, width=110, anchor="w").pack(side="left", padx=10)
                ctk.CTkLabel(tbl_hdr, text="Prix TVAC", font=ctk.CTkFont(FNT_BODY, 12, "bold"), text_color=GRY, width=90, anchor="e").pack(side="left", padx=10)
                ctk.CTkLabel(tbl_hdr, text="Statut", font=ctk.CTkFont(FNT_BODY, 12, "bold"), text_color=GRY, width=90, anchor="center").pack(side="left", padx=10)
                ctk.CTkLabel(tbl_hdr, text="Tailles", font=ctk.CTkFont(FNT_BODY, 12, "bold"), text_color=GRY, width=150, anchor="w").pack(side="left", padx=10)
                ctk.CTkLabel(tbl_hdr, text="Actions", font=ctk.CTkFont(FNT_BODY, 12, "bold"), text_color=GRY, width=100, anchor="center").pack(side="right", padx=10)

                for pid, code, nom, cat, prix, img_path, en_solde, prix_solde in page_products:
                    variants = stock_map.get(pid, [])
                    total_stock = sum(qte for _, qte, _ in variants)
                    
                    var_texts = [f"{t}:{q}" for t, q, _ in variants if t != "Unique"]
                    var_desc = " | ".join(var_texts) if var_texts else "Unique"
                    
                    # Ligne avec fond très léger et contour fin
                    row_frame = ctk.CTkFrame(self.stocks_scroll, fg_color="#FAFAFC", corner_radius=14, border_width=1, border_color="#E5E5EA")
                    row_frame.pack(fill="x", padx=5, pady=3)

                    code_str = code if code else "-"
                    ctk.CTkLabel(row_frame, text=code_str, font=ctk.CTkFont(FNT_BODY, 12), text_color=GRY, width=130, anchor="w").pack(side="left", padx=15, pady=10)
                    
                    # Conteneur désignation + badge
                    nom_f = ctk.CTkFrame(row_frame, fg_color="transparent")
                    nom_f.pack(side="left", expand=True, fill="x", padx=10)
                    ctk.CTkLabel(nom_f, text=nom, font=ctk.CTkFont(FNT_BODY, 13, "bold"), text_color=TEXT, anchor="w").pack(side="left")
                    # Non-affichage du badge solde dans l'inventaire
                    cat_str = cat.upper() if cat else "GÉNÉRAL"
                    ctk.CTkLabel(row_frame, text=cat_str, font=ctk.CTkFont(FNT_BODY, 10, "bold"), text_color=GRY, width=110, anchor="w").pack(side="left", padx=10)
                    
                    # Conteneur Prix (Uniquement le prix normal en inventaire)
                    prix_container = ctk.CTkFrame(row_frame, fg_color="transparent", width=90)
                    prix_container.pack(side="left", padx=10)
                    prix_val = prix if prix is not None else 0.0
                    ctk.CTkLabel(prix_container, text=f"{prix_val:.2f} €", font=ctk.CTkFont(FNT_BODY, 13, "bold"), text_color=ACCENT, anchor="e").pack(anchor="e")

                    q = int(total_stock)
                    b_col = "#E8F5E9" if q > 5 else ("#FFF3E0" if q > 0 else "#FFE5E5")
                    t_col = "#2E7D32" if q > 5 else ("#EF6C00" if q > 0 else RED)
                    stk_b = ctk.CTkFrame(row_frame, fg_color=b_col, corner_radius=14, width=80)
                    stk_b.pack(side="left", padx=10)
                    ctk.CTkLabel(stk_b, text=f"{q} dispo", font=ctk.CTkFont(FNT_BODY, 11, "bold"), text_color=t_col).pack(padx=8, pady=2)

                    ctk.CTkLabel(row_frame, text=var_desc, font=ctk.CTkFont(FNT_BODY, 11), text_color=GRY, width=150, anchor="w").pack(side="left", padx=10)

                    # Boutons d'action rapide sur la ligne
                    act_f = ctk.CTkFrame(row_frame, fg_color="transparent")
                    act_f.pack(side="right", padx=10)
                    
                    btn_edit = ctk.CTkButton(act_f, text="Modifier", width=80, height=32, corner_radius=16,
                                             fg_color=SEC_BG, text_color=TEXT, hover_color=ACCENT,
                                             command=lambda p=pid: self._quick_edit_product(p))
                    btn_edit.pack(side="left", padx=2)

                    btn_del = ctk.CTkButton(act_f, text="Supprimer", width=90, height=32, corner_radius=16,
                                            fg_color="transparent", text_color=RED, hover_color="#FFE5E5",
                                            command=lambda p=pid: self._quick_delete_product(p))
                    btn_del.pack(side="left", padx=2)

                    def select(ev, r=row_frame, p=pid):
                        if not hasattr(self, "_selected_product_ids"): self._selected_product_ids = set()
                        if p in self._selected_product_ids:
                            self._selected_product_ids.remove(p)
                            r.configure(border_width=1, border_color="#E5E5EA")
                        else:
                            self._selected_product_ids.add(p)
                            r.configure(border_color=ACCENT, border_width=2)
                        self._selected_product_id = p
                    row_frame.bind("<Button-1>", select)
                    for child in row_frame.winfo_children():
                        if child != act_f and child not in act_f.winfo_children():
                            child.bind("<Button-1>", select)

                    self._stock_rows.append(row_frame)
                    self._stock_product_ids.append(pid)

            else:
                # --- MODE GRILLE (ADAPTATIF SELON LA LARGEUR D'ÉCRAN) ---
                container_w = self.stocks_scroll.winfo_width()
                if container_w <= 100:
                    container_w = self.winfo_width() - 250
                num_cols = max(2, min(6, max(2, container_w // 220)))
                for col_idx in range(num_cols):
                    self.stocks_scroll.grid_columnconfigure(col_idx, weight=1)

                for i, (pid, code, nom, cat, prix, img_path, en_solde, prix_solde) in enumerate(page_products):
                    variants = stock_map.get(pid, [])
                    total_stock = sum(qte for _, qte, _ in variants)
                    
                    variante_texts = [f"{t}:{q}" for t, q, _ in variants if t != "Unique"]
                    var_desc = " | ".join(variante_texts) if variante_texts else "Taille Unique"
                    
                    # Carte blanc pur #FFFFFF, coins très arrondis (corner_radius=18), bordure ultra-subtile
                    card = ctk.CTkFrame(self.stocks_scroll, fg_color="#FFFFFF", corner_radius=18, border_width=1, border_color="#E5E5EA")
                    card.grid(row=i//num_cols, column=i%num_cols, padx=8, pady=8, sticky="nsew")
                    card.grid_columnconfigure(0, weight=1)

                    info_f = ctk.CTkFrame(card, fg_color="transparent")
                    info_f.pack(fill="both", expand=True, padx=16, pady=16)

                    # Nom & Catégorie majuscule
                    ctk.CTkLabel(info_f, text=nom, font=ctk.CTkFont(FNT_BODY, 15, "bold"), text_color=TEXT, anchor="w").pack(fill="x")
                    cat_str = (cat.upper() if cat else "GÉNÉRAL")
                    ctk.CTkLabel(info_f, text=cat_str, font=ctk.CTkFont(FNT_BODY, 10, "bold"), text_color=GRY, anchor="w").pack(fill="x", pady=(2, 6))

                    # Badge Code-barres
                    code_val = code if code else "20000000000"
                    code_badge = ctk.CTkFrame(info_f, fg_color="#F4F5F7", corner_radius=8)
                    code_badge.pack(anchor="w", pady=(0, 10))
                    ctk.CTkLabel(code_badge, text=f"🏷️ {code_val}", font=ctk.CTkFont(FNT_BODY, 10), text_color=GRY).pack(padx=8, pady=3)

                    # Prix Corail géant & Badge de stock vert
                    bot_f = ctk.CTkFrame(info_f, fg_color="transparent")
                    bot_f.pack(fill="x", pady=(6, 8))

                    prix_val = prix if prix is not None else 0.0
                    ctk.CTkLabel(bot_f, text=f"{prix_val:.2f} €", font=ctk.CTkFont(FNT_BODY, 20, "bold"), text_color=ACCENT).pack(side="left")

                    q = int(total_stock)
                    b_col = "#E6F8ED" if q > 5 else ("#FFF3E0" if q > 0 else "#FFEBEB")
                    t_col = "#28C76F" if q > 5 else ("#EF6C00" if q > 0 else "#FF4D4D")
                    stk_b = ctk.CTkFrame(bot_f, fg_color=b_col, corner_radius=16)
                    stk_b.pack(side="right")
                    ctk.CTkLabel(stk_b, text=f"{q} dispo", font=ctk.CTkFont(FNT_BODY, 11, "bold"), text_color=t_col).pack(padx=10, pady=4)

                    # Déclinaisons de tailles en bas
                    ctk.CTkLabel(info_f, text=f"Tailles : {var_desc}", font=ctk.CTkFont(FNT_BODY, 11), text_color=GRY, anchor="w").pack(fill="x", pady=(4, 0))

                    def select(ev, r=card, p=pid):
                        if not hasattr(self, "_selected_product_ids"): self._selected_product_ids = set()
                        if p in self._selected_product_ids:
                            self._selected_product_ids.remove(p)
                            r.configure(border_width=1, border_color="#E5E5EA")
                        else:
                            self._selected_product_ids.add(p)
                            r.configure(border_color=ACCENT, border_width=2)
                        self._selected_product_id = p
                    card.bind("<Button-1>", select)
                    self._stock_rows.append(card)
                    self._stock_product_ids.append(pid)
                    for child in card.winfo_children():
                        if isinstance(child, ctk.CTkFrame):
                            for subchild in child.winfo_children(): subchild.bind("<Button-1>", select)
                        child.bind("<Button-1>", select)

        except Exception as e:
            print(f"Error refreshing stocks: {e}")
        finally:
            if conn:
                conn.close()

    def _quick_edit_product(self, pid):
        from views.modals import ProductModal
        ProductModal(self, product_id=pid, callback=self._refresh_stocks_table)

    def _quick_delete_product(self, pid):
        from views.modals import ConfirmModal
        def _do_del():
            try:
                from database_manager import get_connection
                conn = get_connection(); c = conn.cursor()
                c.execute("DELETE FROM Produits WHERE id=?", (pid,))
                c.execute("DELETE FROM Stocks WHERE id_produit=?", (pid,))
                conn.commit(); conn.close()
                self._refresh_stocks_table()
                self._st("Produit supprimé avec succès.", "#2E7D32")
            except Exception as e:
                self._st(f"Erreur : {e}", RED)
        ConfirmModal(self, "Supprimer le produit ?", "Êtes-vous sûr de vouloir supprimer ce produit ?", _do_del)

    def _nouveau_produit(self):
        from views.modals import ProductModal
        ProductModal(self, callback=self._refresh_stocks_table)

    def _modifier_produit(self):
        if hasattr(self, "_selected_product_ids") and len(self._selected_product_ids) == 1:
            from views.modals import ProductModal
            ProductModal(self, product_id=list(self._selected_product_ids)[0], callback=self._refresh_stocks_table)
        elif hasattr(self, "_selected_product_ids") and len(self._selected_product_ids) > 1:
            self._st("Sélectionnez un seul produit à modifier.", RED)
        elif hasattr(self, "_selected_product_id") and self._selected_product_id:
            from views.modals import ProductModal
            ProductModal(self, product_id=self._selected_product_id, callback=self._refresh_stocks_table)
        else:
            self._st("Sélectionnez un produit à modifier.", RED)

    def _supprimer_produits_selection(self):
        if not hasattr(self, "_selected_product_ids") or not self._selected_product_ids:
            if hasattr(self, "_selected_product_id") and self._selected_product_id:
                self._selected_product_ids = {self._selected_product_id}
            else:
                self._st("Sélectionnez au moins un produit à supprimer.", RED)
                return

        pids = list(self._selected_product_ids)
        
        def _do_delete():
            try:
                from database_manager import get_connection
                conn = get_connection()
                c = conn.cursor()
                for pid in pids:
                    c.execute("DELETE FROM Produits WHERE id=?", (pid,))
                    c.execute("DELETE FROM Stocks WHERE id_produit=?", (pid,))
                conn.commit()
                conn.close()
                self._selected_product_ids.clear()
                self._refresh_stocks_table()
                self._st(f"{len(pids)} produit(s) supprimé(s).", "#2E7D32")
            except Exception as e:
                self._st(f"Erreur suppression : {e}", RED)
                
        from views.modals import ConfirmModal
        ConfirmModal(self, "Supprimer", f"Voulez-vous vraiment supprimer les {len(pids)} produits sélectionnés ?", _do_delete)

    def _imprimer_etiquette(self):
        from views.modals import EtiquetteCodeBarreModal
        selected_pid = None
        if hasattr(self, "_selected_product_ids") and self._selected_product_ids:
            selected_pid = list(self._selected_product_ids)[0]
        elif hasattr(self, "_selected_product_id") and self._selected_product_id:
            selected_pid = self._selected_product_id
        EtiquetteCodeBarreModal(self, initial_product_id=selected_pid)

    def _build_retours(self):
        f = ctk.CTkFrame(self, fg_color=BG, corner_radius=24)
        f.grid_rowconfigure(2, weight=1); f.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(f, text="RETOURS & REMBOURSEMENTS", font=ctk.CTkFont(FNT_TITLE, 20, "bold"), text_color=TEXT).grid(row=0, column=0, padx=32, pady=(28,0), sticky="w")

        search_f = ctk.CTkFrame(f, fg_color="transparent")
        search_f.grid(row=1, column=0, sticky="ew", padx=32, pady=16)
        self.entry_ret_tk = ctk.CTkEntry(search_f, placeholder_text="Numéro du ticket (ex: TCK-2026-0001)", width=350, height=44, fg_color="#F8F9FA")
        self.entry_ret_tk.pack(side="left", padx=(0,12))
        self.entry_ret_tk.bind("<Return>", lambda e: self._rechercher_ticket_retour())
        ctk.CTkButton(search_f, text="Rechercher", height=44, font=ctk.CTkFont(FNT_BODY, 13, "bold"), fg_color=TEXT, text_color="#FFFFFF", hover_color="#333333",
                      command=self._rechercher_ticket_retour).pack(side="left")

        self.ret_scroll = ctk.CTkScrollableFrame(f, fg_color=BG, corner_radius=RAD, border_width=0)
        self.ret_scroll.grid(row=2, column=0, sticky="nsew", padx=32, pady=10)
        self.ret_scroll.grid_columnconfigure(0, weight=1)
        return f

    def _rechercher_ticket_retour(self):
        tk = self.entry_ret_tk.get().strip()
        for w in self.ret_scroll.winfo_children(): w.destroy()
        try:
            conn = get_connection(); c = conn.cursor()
            c.execute("SELECT id, total_tvac, methode_paiement FROM Tickets WHERE numero_ticket=?", (tk,))
            res = c.fetchone()
            if not res: self._st("Ticket introuvable", RED); return
            tid, total, meth = res
            c.execute("""SELECT vd.id, p.nom, vd.quantite, vd.prix_unitaire_tvac, s.id
                         FROM Ventes_Details vd
                         JOIN Stocks s ON s.id=vd.id_stock
                         JOIN Produits p ON p.id=s.id_produit
                         WHERE vd.id_ticket=?""", (tid,))
            items = c.fetchall()

            for i, (vd_id, nom, qte, prix, sid) in enumerate(items):
                row = ctk.CTkFrame(self.ret_scroll, fg_color="#F8F9FA", height=56, corner_radius=24)
                row.pack(fill="x", pady=4, padx=10)
                ctk.CTkLabel(row, text=f"{nom} (x{qte}) - {prix:.2f} €", font=ctk.CTkFont(FNT_BODY, 13), text_color=TEXT).pack(side="left", padx=20)
                ctk.CTkButton(row, text="Rembourser", width=110, height=34, fg_color=RED, text_color="#FFFFFF",
                              font=ctk.CTkFont(FNT_BODY, 12, "bold"), corner_radius=24,
                              command=lambda v=vd_id, s=sid, p=prix: self._rembourser_item(tk, v, s, p)).pack(side="right", padx=15, pady=10)
        except Exception as e: self._st(f"Erreur : {e}", RED)

    def _rembourser_item(self, tk_num, vd_id, sid, prix):
        from views.modals import RefundModal
        from database_manager import enregistrer_remboursement
        def proceed(mode):
            try:
                conn = get_connection(); c = conn.cursor()
                date_heure = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                vendeur = self.vendeur_actif['nom'] if self.vendeur_actif else 'Inconnu'
                new_tk = enregistrer_remboursement(c, tk_num, vd_id, sid, prix, mode, vendeur, date_heure)
                conn.commit()
                self._st(f"Remboursement ({mode}) effectué : {new_tk}", GRN)
                self._rechercher_ticket_retour()
            except Exception as e: self._st(f"Erreur : {e}", RED)

        RefundModal(self, callback=proceed)

    def _build_params(self):
        f = ctk.CTkFrame(self, fg_color=SEC_BG, corner_radius=24)
        f.grid_columnconfigure(0, weight=0)  # Left Sidebar
        f.grid_columnconfigure(1, weight=1)  # Right Content Pane
        f.grid_rowconfigure(0, weight=1)

        # ── Sidebar Gauche (Navigation) ─────────────────────────
        sidebar = ctk.CTkFrame(f, fg_color="#F2F2F7", corner_radius=24, width=220)
        sidebar.grid(row=0, column=0, sticky="nsew", padx=(20, 0), pady=20)
        sidebar.pack_propagate(False)
        
        # Titre Sidebar
        ctk.CTkLabel(sidebar, text="Configuration", font=ctk.CTkFont(FNT_TITLE, 20, "bold"), text_color=TEXT, anchor="w").pack(fill="x", padx=20, pady=(24, 16))
        
        # Boutons Sidebar
        self.params_sidebar_buttons = {}
        tabs = [
            ("boutique", "Boutique"),
            ("equipe",   "Équipe"),
            ("caisse",   "Caisse"),
            ("shopify",  "Shopify" if self.app_features.get("shopify_sync", False) else "Shopify 🔒"),
            ("cartes",   "Cartes Cadeaux"),
            ("backup",   "Sauvegarde")
        ]
        
        for tab_id, label in tabs:
            btn = ctk.CTkButton(sidebar, text=label, height=44, anchor="w",
                                font=ctk.CTkFont(FNT_BODY, 13, "bold"),
                                fg_color="transparent", text_color=GRY, hover_color="#E5E5EA",
                                corner_radius=12,
                                command=lambda t=tab_id: self._switch_params_tab(t))
            btn.pack(fill="x", padx=12, pady=3)
            self.params_sidebar_buttons[tab_id] = btn

        # ── Conteneur Right (Contenu Actif) ─────────────────────
        right_container = ctk.CTkFrame(f, fg_color="transparent")
        right_container.grid(row=0, column=1, sticky="nsew", padx=30, pady=20)
        right_container.grid_columnconfigure(0, weight=1)
        right_container.grid_rowconfigure(0, weight=1)

        self.params_tab_frames = {}

        # ────────────────────────────────────────────────────────
        # 1. TAB BOUTIQUE
        # ────────────────────────────────────────────────────────
        t_boutique = ctk.CTkScrollableFrame(right_container, fg_color="transparent")
        self.params_tab_frames["boutique"] = t_boutique
        
        ctk.CTkLabel(t_boutique, text="Paramètres de la Boutique", font=ctk.CTkFont(FNT_TITLE, 26, "bold"), text_color=TEXT, anchor="w").pack(fill="x", pady=(10, 2))
        ctk.CTkLabel(t_boutique, text="Configurez les détails affichés sur les tickets de caisse.", font=ctk.CTkFont(FNT_BODY, 13), text_color=GRY, anchor="w").pack(fill="x", pady=(0, 20))
        
        card_b = ctk.CTkFrame(t_boutique, fg_color="#FFFFFF", corner_radius=16, border_width=1, border_color="#E5E5EA")
        card_b.pack(fill="x", pady=10)
        
        def _add_input_row(parent, label, default_val, placeholder=""):
            row = ctk.CTkFrame(parent, fg_color="transparent")
            row.pack(fill="x", padx=24, pady=12)
            ctk.CTkLabel(row, text=label, font=ctk.CTkFont(FNT_BODY, 13, "bold"), text_color=TEXT, width=180, anchor="w").pack(side="left")
            entry = ctk.CTkEntry(row, height=44, fg_color=SEC_BG, border_width=1, border_color="#E5E5EA", corner_radius=22, font=ctk.CTkFont(FNT_BODY, 13), placeholder_text=placeholder)
            entry.pack(side="left", fill="x", expand=True)
            if default_val:
                entry.insert(0, default_val)
            return entry

        self.entry_shop_name = _add_input_row(card_b, "Nom de l'établissement", self.shop_name)
        self.entry_shop_subtitle = _add_input_row(card_b, "Sous-titre", getattr(self, "shop_subtitle", ""))
        self.entry_shop_address = _add_input_row(card_b, "Adresse complète", getattr(self, "shop_address", ""))
        self.entry_shop_vat = _add_input_row(card_b, "N° Entreprise / TVA", getattr(self, "shop_vat", ""))
        self.entry_def_tva = _add_input_row(card_b, "Taux TVA par défaut", self._get_param("default_tva", "0.21"))
        
        # Row bouton
        row_btn_b = ctk.CTkFrame(card_b, fg_color="transparent")
        row_btn_b.pack(fill="x", padx=24, pady=(12, 20))
        ctk.CTkButton(row_btn_b, text="Sauvegarder les modifications", fg_color=ACCENT, text_color="#FFFFFF",
                      font=ctk.CTkFont(FNT_BODY, 13, "bold"), height=40, corner_radius=20,
                      command=self._save_global_params).pack(side="right")

        # ────────────────────────────────────────────────────────
        # 2. TAB ÉQUIPE
        # ────────────────────────────────────────────────────────
        t_equipe = ctk.CTkScrollableFrame(right_container, fg_color="transparent")
        self.params_tab_frames["equipe"] = t_equipe
        
        ctk.CTkLabel(t_equipe, text="Gestion de l'Équipe", font=ctk.CTkFont(FNT_TITLE, 26, "bold"), text_color=TEXT, anchor="w").pack(fill="x", pady=(10, 2))
        ctk.CTkLabel(t_equipe, text="Gérez les comptes vendeurs et leurs codes PIN d'accès.", font=ctk.CTkFont(FNT_BODY, 13), text_color=GRY, anchor="w").pack(fill="x", pady=(0, 20))
        
        card_e = ctk.CTkFrame(t_equipe, fg_color="#FFFFFF", corner_radius=16, border_width=1, border_color="#E5E5EA")
        card_e.pack(fill="x", pady=10)
        
        e_top = ctk.CTkFrame(card_e, fg_color="transparent")
        e_top.pack(fill="x", padx=24, pady=16)
        ctk.CTkLabel(e_top, text="Comptes Utilisateurs", font=ctk.CTkFont(FNT_BODY, 14, "bold"), text_color=TEXT).pack(side="left")
        ctk.CTkButton(e_top, text="+ Ajouter un membre", width=160, height=36,
                      font=ctk.CTkFont(FNT_BODY, 12, "bold"),
                      fg_color=SEC_BG, text_color=TEXT, corner_radius=18, hover_color="#E0E0E0",
                      command=self._ajouter_vendeur).pack(side="right")
                      
        sep_e = ctk.CTkFrame(card_e, fg_color=SEC_BG, height=1)
        sep_e.pack(fill="x", padx=24)
        
        self.vendeurs_list_frame = ctk.CTkFrame(card_e, fg_color="transparent")
        self.vendeurs_list_frame.pack(fill="x", padx=0, pady=8)

        # ────────────────────────────────────────────────────────
        # 3. TAB CAISSE
        # ────────────────────────────────────────────────────────
        t_caisse = ctk.CTkScrollableFrame(right_container, fg_color="transparent")
        self.params_tab_frames["caisse"] = t_caisse
        
        ctk.CTkLabel(t_caisse, text="Gestion de la Caisse", font=ctk.CTkFont(FNT_TITLE, 26, "bold"), text_color=TEXT, anchor="w").pack(fill="x", pady=(10, 2))
        ctk.CTkLabel(t_caisse, text="Configurez le fond de caisse et enregistrez les mouvements d'espèces manuels.", font=ctk.CTkFont(FNT_BODY, 13), text_color=GRY, anchor="w").pack(fill="x", pady=(0, 20))
        
        card_c = ctk.CTkFrame(t_caisse, fg_color="#FFFFFF", corner_radius=16, border_width=1, border_color="#E5E5EA")
        card_c.pack(fill="x", pady=10)
        
        row_c1 = ctk.CTkFrame(card_c, fg_color="transparent")
        row_c1.pack(fill="x", padx=24, pady=20)
        ctk.CTkLabel(row_c1, text="Fond de caisse (ouverture)", font=ctk.CTkFont(FNT_BODY, 14, "bold"), text_color=TEXT).pack(side="left")
        
        c1_right = ctk.CTkFrame(row_c1, fg_color="transparent")
        c1_right.pack(side="right")
        self.entry_fond_caisse = ctk.CTkEntry(c1_right, font=ctk.CTkFont(FNT_BODY, 13), height=40, width=120, fg_color=SEC_BG, border_width=1, border_color="#E5E5EA", corner_radius=20, justify="center")
        self.entry_fond_caisse.insert(0, self._get_param("fond_caisse_matin", "200.00"))
        self.entry_fond_caisse.pack(side="left", padx=(0, 12))
        ctk.CTkButton(c1_right, text="Enregistrer", fg_color=ACCENT, text_color="#FFFFFF",
                      font=ctk.CTkFont(FNT_BODY, 12, "bold"), height=40, corner_radius=20,
                      command=self._save_fond_caisse).pack(side="left")
                      
        sep_c = ctk.CTkFrame(card_c, fg_color=SEC_BG, height=1)
        sep_c.pack(fill="x", padx=24)
        
        row_c2 = ctk.CTkFrame(card_c, fg_color="transparent")
        row_c2.pack(fill="x", padx=24, pady=20)
        ctk.CTkLabel(row_c2, text="Opérations manuelles d'espèces", font=ctk.CTkFont(FNT_BODY, 14, "bold"), text_color=TEXT).pack(side="left")
        ctk.CTkButton(row_c2, text="Sortie de Caisse / Dépense", fg_color=SEC_BG, text_color=TEXT,
                      font=ctk.CTkFont(FNT_BODY, 12, "bold"), height=40, corner_radius=20, hover_color="#E0E0E0",
                      command=self._ouvrir_depense_caisse).pack(side="right")

        # ────────────────────────────────────────────────────────
        # 4. TAB SHOPIFY
        # ────────────────────────────────────────────────────────
        t_shopify = ctk.CTkScrollableFrame(right_container, fg_color="transparent")
        self.params_tab_frames["shopify"] = t_shopify
        
        ctk.CTkLabel(t_shopify, text="Synchronisation Shopify", font=ctk.CTkFont(FNT_TITLE, 26, "bold"), text_color=TEXT, anchor="w").pack(fill="x", pady=(10, 2))
        ctk.CTkLabel(t_shopify, text="Liez votre boutique Shopify pour synchroniser automatiquement les ventes et le stock.", font=ctk.CTkFont(FNT_BODY, 13), text_color=GRY, anchor="w").pack(fill="x", pady=(0, 20))
        
        card_s = ctk.CTkFrame(t_shopify, fg_color="#FFFFFF", corner_radius=16, border_width=1, border_color="#E5E5EA")
        card_s.pack(fill="x", pady=10)
        
        self.entry_shopify_url = _add_input_row(card_s, "URL de la boutique", self._get_param("shopify_store_url", ""), "ma-boutique.myshopify.com")
        self.entry_shopify_token = _add_input_row(card_s, "Token d'accès API", self._get_param("shopify_access_token", ""))
        self.entry_shopify_token.configure(show="*")
        
        sep_s = ctk.CTkFrame(card_s, fg_color=SEC_BG, height=1)
        sep_s.pack(fill="x", padx=24)
        
        row_s3 = ctk.CTkFrame(card_s, fg_color="transparent")
        row_s3.pack(fill="x", padx=24, pady=16)
        
        ctk.CTkButton(row_s3, text="Tester la Connexion", fg_color="transparent", text_color=ACCENT, border_width=0, font=ctk.CTkFont(FNT_BODY, 13, "bold"),
                      command=self._tester_connexion_shopify).pack(side="left")
        
        ctk.CTkButton(row_s3, text="Importer le Catalogue", fg_color=SEC_BG, text_color=TEXT, border_width=1, border_color="#E5E5EA", font=ctk.CTkFont(FNT_BODY, 13, "bold"), height=40, corner_radius=20,
                      command=self._lancer_import_shopify).pack(side="left", padx=(15, 0))
        
        ctk.CTkButton(row_s3, text="Enregistrer Shopify", fg_color=ACCENT, text_color="#FFFFFF",
                      font=ctk.CTkFont(FNT_BODY, 13, "bold"), height=40, corner_radius=20,
                      command=self._save_shopify_params).pack(side="right")

        # ────────────────────────────────────────────────────────
        # 4.5. TAB CARTES CADEAUX
        # ────────────────────────────────────────────────────────
        t_cartes = ctk.CTkScrollableFrame(right_container, fg_color="transparent")
        self.params_tab_frames["cartes"] = t_cartes
        
        c_top = ctk.CTkFrame(t_cartes, fg_color="transparent")
        c_top.pack(fill="x", pady=(10, 2))
        
        ctk.CTkLabel(c_top, text="Cartes Cadeaux & Bons", font=ctk.CTkFont(FNT_TITLE, 26, "bold"), text_color=TEXT, anchor="w").pack(side="left")
        ctk.CTkButton(c_top, text="+ Émettre une Carte", width=160, height=36,
                      font=ctk.CTkFont(FNT_BODY, 12, "bold"),
                      fg_color=ACCENT, text_color="#FFFFFF", corner_radius=18,
                      command=self._ouvrir_emission_carte).pack(side="right")
                      
        ctk.CTkLabel(t_cartes, text="Consultez et gérez les cartes cadeaux en circulation.", font=ctk.CTkFont(FNT_BODY, 13), text_color=GRY, anchor="w").pack(fill="x", pady=(0, 20))
        
        self.gift_cards_container = ctk.CTkFrame(t_cartes, fg_color="#FFFFFF", corner_radius=16, border_width=1, border_color="#E5E5EA")
        self.gift_cards_container.pack(fill="x", pady=10)

        # ────────────────────────────────────────────────────────
        # 5. TAB SAUVEGARDE
        # ────────────────────────────────────────────────────────
        t_backup = ctk.CTkScrollableFrame(right_container, fg_color="transparent")
        self.params_tab_frames["backup"] = t_backup
        
        ctk.CTkLabel(t_backup, text="Sauvegarde & Maintenance", font=ctk.CTkFont(FNT_TITLE, 26, "bold"), text_color=TEXT, anchor="w").pack(fill="x", pady=(10, 2))
        ctk.CTkLabel(t_backup, text="Protégez vos données de vente en réalisant des sauvegardes régulières.", font=ctk.CTkFont(FNT_BODY, 13), text_color=GRY, anchor="w").pack(fill="x", pady=(0, 20))
        
        card_d = ctk.CTkFrame(t_backup, fg_color="#FFFFFF", corner_radius=16, border_width=1, border_color="#E5E5EA")
        card_d.pack(fill="x", pady=10)
        
        row_d1 = ctk.CTkFrame(card_d, fg_color="transparent")
        row_d1.pack(fill="x", padx=24, pady=24)
        
        ctk.CTkLabel(row_d1, text="Sauvegarde locale", font=ctk.CTkFont(FNT_BODY, 14, "bold"), text_color=TEXT).pack(anchor="w", pady=(0, 5))
        ctk.CTkLabel(row_d1, text="Génère une copie miroir de la base de données dans le dossier de secours de l'application.", font=ctk.CTkFont(FNT_BODY, 12), text_color=GRY, anchor="w").pack(fill="x", pady=(0, 16))
        
        ctk.CTkButton(row_d1, text="Créer une Sauvegarde", fg_color=RED, text_color="#FFFFFF",
                      font=ctk.CTkFont(FNT_BODY, 13, "bold"), height=40, corner_radius=20,
                      command=self._sauvegarder_db).pack(anchor="w")

        # Initialiser avec le premier onglet
        self._switch_params_tab("boutique")
        self._refresh_vendeurs_list()

        return f

    def _switch_params_tab(self, tab_name):
        if tab_name == "shopify" and not self.app_features.get("shopify_sync", False):
            self._show_upsell_modal("shopify_sync")
            return
            
        self.params_active_tab = tab_name
        
        # Masquer tous les conteneurs d'onglets
        for name, frame in self.params_tab_frames.items():
            frame.grid_forget()
            
        # Afficher l'onglet actif
        self.params_tab_frames[tab_name].grid(row=0, column=0, sticky="nsew")
        if tab_name == "cartes":
            self._refresh_gift_cards()
        
        # Mettre à jour les couleurs des boutons dans la sidebar
        for name, btn in self.params_sidebar_buttons.items():
            if name == tab_name:
                btn.configure(fg_color="#E5E5EA", text_color=TEXT)
            else:
                btn.configure(fg_color="transparent", text_color=GRY)

    def _section_header(self, parent, title, row, col=0):
        """Ajoute un titre de section stylé dans un parent grid."""
        lbl = ctk.CTkLabel(parent, text=title, font=ctk.CTkFont(FNT_TITLE, 18, "bold"), text_color=TEXT, anchor="w")
        lbl.grid(row=row, column=col, sticky="w", pady=(10, 8))

    def _refresh_vendeurs_list(self):
        """Rafraîchit la liste des vendeurs dans la section Paramètres."""
        for w in self.vendeurs_list_frame.winfo_children():
            w.destroy()
        try:
            conn = get_connection(); c = conn.cursor()
            c.execute("SELECT id, nom, pin, role_admin FROM Vendeurs ORDER BY role_admin DESC, nom ASC")
            vendeurs = c.fetchall()

            for idx, (vid, nom, pin, is_admin) in enumerate(vendeurs):
                # Ligne transparente sur fond blanc de la carte
                row = ctk.CTkFrame(self.vendeurs_list_frame, fg_color="transparent", height=56)
                row.pack(fill="x", pady=0)
                row.pack_propagate(False)

                # Badge rôle
                role_txt = "Admin" if is_admin else "Vendeur"
                role_col = ACCENT if is_admin else GRY
                
                badge = ctk.CTkFrame(row, fg_color="#FFF5F5" if is_admin else SEC_BG, corner_radius=12, width=64, height=28)
                badge.pack(side="left", padx=(24, 16), pady=14)
                badge.pack_propagate(False)
                ctk.CTkLabel(badge, text=role_txt, font=ctk.CTkFont(FNT_BODY, 10, "bold"),
                             text_color=role_col).place(relx=0.5, rely=0.5, anchor="center")

                # Nom
                ctk.CTkLabel(row, text=nom, font=ctk.CTkFont(FNT_BODY, 15, "bold"),
                             text_color=TEXT).pack(side="left")

                # Boutons d'action
                if not is_admin:
                    btn_del = ctk.CTkButton(row, text="✕", width=36, height=36,
                                            font=ctk.CTkFont(FNT_BODY, 14, "bold"),
                                            fg_color="transparent", text_color=RED,
                                            hover_color="#FFE5E5", corner_radius=18,
                                            command=lambda v=vid, n=nom: self._supprimer_vendeur(v, n))
                    btn_del.pack(side="right", padx=(4, 24), pady=10)

                btn_pin = ctk.CTkButton(row, text="Changer PIN", width=100, height=36,
                                        font=ctk.CTkFont(FNT_BODY, 12, "bold"),
                                        fg_color=SEC_BG, text_color=TEXT, border_width=0, corner_radius=18, hover_color="#E0E0E0",
                                        command=lambda v=vid, n=nom: self._changer_pin_vendeur(v, n))
                
                btn_pin.pack(side="right", padx=(0, 4) if not is_admin else (0, 24), pady=10)
                
                # Séparateur subtil entre les vendeurs
                if idx < len(vendeurs) - 1:
                    sep = ctk.CTkFrame(self.vendeurs_list_frame, fg_color=SEC_BG, height=1)
                    sep.pack(fill="x", padx=24)

        except Exception as e:
            ctk.CTkLabel(self.vendeurs_list_frame, text=f"Erreur : {e}",
                         font=ctk.CTkFont(FNT_BODY, 12), text_color=RED).pack(padx=20, pady=10)

    def _ajouter_vendeur(self):
        from views.modals import VendeurModal
        VendeurModal(self, callback=self._refresh_vendeurs_list)

    def _changer_pin_vendeur(self, vendeur_id, nom):
        from views.modals import ChangePinModal
        ChangePinModal(self, vendeur_id=vendeur_id, nom=nom)

    def _supprimer_vendeur(self, vendeur_id, nom):
        from views.modals import ConfirmModal
        def _confirmer():
            try:
                conn = get_connection(); c = conn.cursor()
                c.execute("DELETE FROM Vendeurs WHERE id=? AND role_admin=0", (vendeur_id,))
                conn.commit()
                if c.rowcount == 0:
                    self._st("Impossible de supprimer cet Administrateur.", RED)
                else:
                    self._refresh_vendeurs_list()
                    self._st(f"Vendeur '{nom}' supprimé.", GRY)
            except Exception as e:
                self._st(f"Erreur : {e}", RED)
        ConfirmModal(self, titre="Supprimer le vendeur",
                     message=f"Supprimer '{nom}' ? Cette action est irréversible.",
                     callback=_confirmer)

    def _save_fond_caisse(self):
        val = self.entry_fond_caisse.get().strip().replace(",", ".")
        try:
            from decimal import Decimal
            Decimal(val)  # Validation
            self._set_param("fond_caisse_matin", val)
            # Mise à jour de la session courante en DB
            conn = get_connection(); c = conn.cursor()
            c.execute("UPDATE Sessions_Caisse SET fond_caisse_matin=? WHERE id=(SELECT MAX(id) FROM Sessions_Caisse)", (val,))
            if c.rowcount == 0:
                c.execute("INSERT INTO Sessions_Caisse (fond_caisse_matin) VALUES (?)", (val,))
            conn.commit()
            self._st(f"Fond de caisse enregistré : {val} €", GRN)
        except Exception:
            self._st("Valeur invalide pour le fond de caisse.", RED)

    def _ouvrir_depense_caisse(self):
        from views.modals import DepenseCaisseModal
        DepenseCaisseModal(self, vendeur_nom=self.vendeur_actif['nom'] if self.vendeur_actif else 'Inconnu',
                           callback=lambda: self._st("Dépense enregistrée.", GRN))

    def _save_global_params(self):
        name = self.entry_shop_name.get().strip()
        subtitle = self.entry_shop_subtitle.get().strip()
        address = self.entry_shop_address.get().strip()
        vat = self.entry_shop_vat.get().strip()
        tva  = self.entry_def_tva.get().strip()
        
        if name:
            self._set_param("shop_name", name)
            self.shop_name = name
            self.title(f"{name} — Système POS")
        
        self._set_param("shop_subtitle", subtitle)
        self.shop_subtitle = subtitle
        
        self._set_param("shop_address", address)
        self.shop_address = address
        
        self._set_param("shop_vat", vat)
        self.shop_vat = vat

        if tva:
            self._set_param("default_tva", tva)
        self._st("Paramètres sauvegardés avec succès.", GRN)

    def _save_shopify_params(self):
        url = self.entry_shopify_url.get().strip()
        token = self.entry_shopify_token.get().strip()
        clean_url = url.replace("https://", "").replace("http://", "").strip("/")
        self._set_param("shopify_store_url", clean_url)
        self._set_param("shopify_access_token", token)
        self._st("Paramètres Shopify enregistrés.", GRN)
        
    def _tester_connexion_shopify(self):
        url = self.entry_shopify_url.get().strip()
        token = self.entry_shopify_token.get().strip()
        if not url or not token:
            self._st("URL et Token requis pour le test.", RED)
            return
        clean_url = url.replace("https://", "").replace("http://", "").strip("/")
        import threading
        def run_test():
            import urllib.request
            import json
            import ssl
            
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

            api_url = f"https://{clean_url}/admin/api/2025-01/locations.json"
            req = urllib.request.Request(api_url, headers={
                "Content-Type": "application/json",
                "X-Shopify-Access-Token": token,
                "User-Agent": "KodoPOS-Engine/1.0"
            })
            try:
                with urllib.request.urlopen(req, context=ctx, timeout=8) as resp:
                    data = json.loads(resp.read().decode())
                    if "locations" in data:
                        locs = [l["name"] for l in data["locations"]]
                        self._st(f"Connexion OK! Dépôts: {', '.join(locs)}", GRN)
                        from views.modals import show_toast
                        show_toast(self, "Connexion Shopify Réussie !", type="success")
                    else:
                        self._st("Connexion OK, aucun dépôt.", "#FF9900")
            except Exception as e:
                self._st(f"Échec : {e}", RED)
                from views.modals import show_toast
                show_toast(self, f"Erreur Shopify: {e}", type="error")
        threading.Thread(target=run_test, daemon=True).start()

    def _lancer_import_shopify(self):
        import threading
        from shopify_sync import ShopifySyncThread
        
        def run_import():
            try:
                sync = ShopifySyncThread()
                from views.modals import show_toast
                self.after(10, lambda: show_toast(self, "Import Shopify démarré...", type="loading"))
                
                def update_progress(msg, pct):
                    self.after(10, lambda: self._st(f"[SHOPIFY IMPORT] {msg} ({pct}%)", ACCENT))
                
                count = sync.import_shopify_catalog(progress_callback=update_progress)
                self.after(10, lambda: show_toast(self, f"Importation réussie : {count} variantes chargées !", type="success"))
                self.after(10, lambda: self._st(f"Import Shopify : {count} variantes importées.", GRN))
                
                if self.current_view == "Stocks":
                    self.after(500, self._refresh_stocks_table)
            except Exception as e:
                from views.modals import show_toast
                self.after(10, lambda: show_toast(self, f"Erreur d'import : {e}", type="error"))
                self.after(10, lambda: self._st(f"Erreur import Shopify : {e}", RED))
                
        threading.Thread(target=run_import, daemon=True).start()

    def _toggle_theme(self):
        pass

    def _ouvrir_emission_carte(self):
        from views.modals import EmissionCarteCadeauModal
        EmissionCarteCadeauModal(self, callback=lambda code, amt: self._refresh_gift_cards())

    def _refresh_gift_cards(self):
        if not hasattr(self, "gift_cards_container") or not self.gift_cards_container.winfo_exists():
            return
        for w in self.gift_cards_container.winfo_children():
            w.destroy()
            
        try:
            conn = get_connection(); c = conn.cursor()
            c.execute("SELECT code, solde_initial, solde_actuel, date_creation FROM Cartes_Cadeaux ORDER BY date_creation DESC")
            rows = c.fetchall()
            conn.close()
        except:
            rows = []
            
        if not rows:
            ctk.CTkLabel(self.gift_cards_container, text="Aucune carte cadeau émise pour le moment.", font=ctk.CTkFont(FNT_BODY, 13), text_color=GRY).pack(pady=30)
            return
            
        hdr = ctk.CTkFrame(self.gift_cards_container, fg_color=SEC_BG, height=36)
        hdr.pack(fill="x", padx=1, pady=1)
        
        ctk.CTkLabel(hdr, text="Code", font=ctk.CTkFont(FNT_BODY, 12, "bold"), text_color=TEXT, width=150, anchor="w").pack(side="left", padx=20, pady=8)
        ctk.CTkLabel(hdr, text="Solde Initial", font=ctk.CTkFont(FNT_BODY, 12, "bold"), text_color=TEXT, width=120, anchor="w").pack(side="left", padx=10, pady=8)
        ctk.CTkLabel(hdr, text="Solde Restant", font=ctk.CTkFont(FNT_BODY, 12, "bold"), text_color=TEXT, width=120, anchor="w").pack(side="left", padx=10, pady=8)
        ctk.CTkLabel(hdr, text="Date de création", font=ctk.CTkFont(FNT_BODY, 12, "bold"), text_color=TEXT, anchor="e").pack(side="right", padx=20, pady=8)
        
        for code, initial, actuel, dt in rows:
            row = ctk.CTkFrame(self.gift_cards_container, fg_color="transparent")
            row.pack(fill="x", padx=1)
            
            sep = ctk.CTkFrame(self.gift_cards_container, fg_color=SEC_BG, height=1)
            sep.pack(fill="x", padx=10)
            
            ctk.CTkLabel(row, text=code, font=ctk.CTkFont(FNT_BODY, 13, "bold"), text_color=TEXT, width=150, anchor="w").pack(side="left", padx=20, pady=12)
            ctk.CTkLabel(row, text=f"{initial:.2f} €", font=ctk.CTkFont(FNT_BODY, 13), text_color=TEXT, width=120, anchor="w").pack(side="left", padx=10, pady=12)
            
            color_solde = GRN if actuel > 0 else RED
            ctk.CTkLabel(row, text=f"{actuel:.2f} €", font=ctk.CTkFont(FNT_BODY, 13, "bold"), text_color=color_solde, width=120, anchor="w").pack(side="left", padx=10, pady=12)
            
            dt_str = dt[:16] if dt else ""
            ctk.CTkLabel(row, text=dt_str, font=ctk.CTkFont(FNT_BODY, 12), text_color=GRY, anchor="e").pack(side="right", padx=20, pady=12)

    def _sauvegarder_db(self):
        from tkinter import filedialog
        import shutil
        from database_manager import DB_NAME
        file_path = filedialog.asksaveasfilename(parent=self,
                                                 defaultextension=".db",
                                                 initialfile=f"sauvegarde_kodo_{datetime.date.today().strftime('%d-%m-%Y')}.db",
                                                 title="Choisir l'emplacement de la sauvegarde")
        if file_path:
            try:
                shutil.copy2(DB_NAME, file_path)
                self._st("Sauvegarde créée avec succès !", GRN)
            except Exception as e:
                self._st(f"Erreur sauvegarde : {e}", RED)

    # ── NAVIGATION ───────────────────────────────────────────
    def _hide_all(self):
        if hasattr(self, 'frames') and self.frames:
            for fr in list(self.frames.values()):
                if fr and hasattr(fr, 'grid_forget'):
                    fr.grid_forget()
        if hasattr(self, 'stats_frame') and self.stats_frame and hasattr(self.stats_frame, 'grid_forget'):
            self.stats_frame.grid_forget()

    def afficher_caisse(self):
        if not self._set_active_nav("Caisse"): return
        self._hide_all(); self.current_view = "Caisse"
        self.caisse_frame.grid(row=0, column=1, sticky="nsew", padx=(10, 20), pady=10)

    def afficher_stocks(self):
        if not self._set_active_nav("Stocks"): return
        self._hide_all(); self.current_view = "Stocks"
        self.stocks_frame.grid(row=0, column=1, sticky="nsew", padx=(10, 20), pady=10)
        self._refresh_stocks_table()

    def afficher_retours(self):
        if not self._set_active_nav("Retours"): return
        self._hide_all(); self.current_view = "Retours"
        self.retours_frame.grid(row=0, column=1, sticky="nsew", padx=(10, 20), pady=10)

    def afficher_stats(self):
        if not self._set_active_nav("Clôture & Stats"): return
        self._hide_all(); self.current_view = "Stats"
        if not self.stats_frame: self.stats_frame = stats_view.build(self)
        self.stats_frame.grid(row=0, column=1, sticky="nsew", padx=(10, 20), pady=10)

    def afficher_params(self):
        if not self._set_active_nav("Paramètres"): return
        self._hide_all(); self.current_view = "Params"
        self.params_frame.grid(row=0, column=1, sticky="nsew", padx=(10, 20), pady=10)

    # ── DASHBOARD UPDATE ─────────────────────────────────────
    def update_dashboard(self):
        if self.stats_frame:
            self.stats_frame.destroy()
        self.stats_frame = stats_view.build(self)
        self.stats_frame.grid(row=0, column=1, sticky="nsew")

    # ── SCANNER ──────────────────────────────────────────────
    def _nettoyer_entry_apres_scan(self):
        """Nettoie le champ d'entrée caisse/stocks et ferme la popup de suggestions après un scan."""
        try:
            focused = self.focus_get()
            if hasattr(self, "entry_caisse") and focused in (self.entry_caisse, getattr(self.entry_caisse, "_entry", None)):
                self.entry_caisse.delete(0, "end")
            if hasattr(self, "entry_stocks") and focused in (self.entry_stocks, getattr(self.entry_stocks, "_entry", None)):
                self.entry_stocks.delete(0, "end")
        except Exception:
            pass
        self._fermer_suggestions_caisse()

    def _on_key(self, event):
        import time
        now = time.time()

        # 1. Si la touche pressée est de type fin de saisie (Return, KP_Enter ou Tab), on traite le scan
        if event.keysym in ("Return", "KP_Enter", "Tab"):
            if self.buffer_cb:
                code = self.buffer_cb.strip()
                self.buffer_cb = ""
                
                # Traduction AZERTY vers Chiffres (Douchette configurée en clavier AZERTY FR/Mac)
                code = self._translate_azerty(code)

                # Anti-bounce (500ms sur le même code)
                if hasattr(self, "_last_scan_time") and hasattr(self, "_last_scan_code"):
                    if self._last_scan_code == code and (now - self._last_scan_time) < 0.5:
                        self._nettoyer_entry_apres_scan()
                        return "break"

                self._last_scan_code = code
                self._last_scan_time = now

                # Nettoyer l'entrée et fermer les suggestions
                self._nettoyer_entry_apres_scan()

                # Routage du code
                self._route_scan(code)
                return "break"

            # Gestion standard de la validation du pavé si l'utilisateur est focalisé sur l'entrée de caisse ou stocks
            focused = self.focus_get()
            if hasattr(self, "entry_caisse") and focused in (self.entry_caisse, getattr(self.entry_caisse, "_entry", None)):
                self._valider_entry_caisse()
                return "break"
            elif hasattr(self, "entry_stocks") and focused in (self.entry_stocks, getattr(self.entry_stocks, "_entry", None)):
                self._valider_entry_stocks()
                return "break"
            return

        # 2. Accumulation du buffer si la touche est imprimable
        if event.char and event.char.isprintable():
            delta = now - getattr(self, "_last_key_press_time", 0)
            self._last_key_press_time = now

            # Heuristique temporelle (< 180ms) pour supporter les douchettes Bluetooth / sans fil :
            if len(self.buffer_cb) > 0 and delta > 0.18:
                self.buffer_cb = "" # Saisie lente, on réinitialise

            self.buffer_cb += event.char

            # Si c'est ultra-rapide (douchette), on bloque la touche pour éviter qu'elle s'écrive dans un champ non désiré.
            if delta < 0.18:
                # Si le tout premier caractère s'est déjà écrit dans le widget, on l'efface
                if len(self.buffer_cb) == 2:
                    focused = self.focus_get()
                    if focused and hasattr(focused, "delete") and hasattr(focused, "get"):
                        val = focused.get()
                        if val:
                            focused.delete(len(val)-1, "end")
                return "break"

    def _route_scan(self, code):
        # Si une modale est ouverte et possède une méthode de réception de scan (comme ProductModal)
        if hasattr(self, "active_modal") and self.active_modal and hasattr(self.active_modal, "on_barcode_scanned"):
            try:
                self.active_modal.on_barcode_scanned(code)
                self.jouer_son_caisse()
                return
            except Exception as e:
                print(f"Erreur envoi scan modale : {e}")

        # Sinon, routage classique selon la vue active
        if self.current_view == "Stocks":
            self.on_scan_inventaire(code)
        else:
            self.on_scan_caisse(code)

    def _translate_azerty(self, code):
        if not code:
            return ""
        azerty_map = str.maketrans('&é"\'(§-è!_çà°', '1234566788900')
        return code.translate(azerty_map)

    def _on_key_release_caisse(self, event):
        if event.keysym in ("Return", "KP_Enter", "Tab", "Escape"):
            if hasattr(self, "_live_search_timer") and self._live_search_timer:
                self.after_cancel(self._live_search_timer)
                self._live_search_timer = None
            self._fermer_suggestions_caisse()
            return

        import time
        now = time.time()
        delta = now - getattr(self, "_last_key_press_time_caisse", 0)
        self._last_key_press_time_caisse = now

        # Si le délai entre les caractères est très court (< 0.05s / 50ms),
        # il s'agit d'un scanneur douchette ultra-rapide -> ne pas afficher la modale d'autocomplétion
        if delta < 0.05:
            if hasattr(self, "_live_search_timer") and self._live_search_timer:
                self.after_cancel(self._live_search_timer)
                self._live_search_timer = None
            self._fermer_suggestions_caisse()
            return

        # Annuler toute requête de recherche en attente et débouncer (120ms)
        if hasattr(self, "_live_search_timer") and self._live_search_timer:
            self.after_cancel(self._live_search_timer)
            self._live_search_timer = None

        self._live_search_timer = self.after(120, self._update_live_suggestions_caisse)

    def _update_live_suggestions_caisse(self):
        txt = self.entry_caisse.get().strip()
        if len(txt) < 2:
            self._fermer_suggestions_caisse()
            return

        txt_clean = self._translate_azerty(txt)
        st_lower = txt_clean.lower()

        try:
            conn = get_connection()
            c = conn.cursor()
            c.execute("""
                SELECT id, nom, code_barre, categorie, marque, prix_vente_tvac, en_solde, prix_solde_tvac, taux_tva
                FROM Produits
                WHERE code_barre LIKE ? OR LOWER(nom) LIKE ? OR LOWER(marque) LIKE ? OR LOWER(categorie) LIKE ?
                ORDER BY
                  CASE 
                    WHEN code_barre = ? THEN 1
                    WHEN code_barre LIKE ? THEN 2
                    WHEN LOWER(nom) = ? THEN 3
                    WHEN LOWER(nom) LIKE ? THEN 4
                    WHEN LOWER(marque) LIKE ? THEN 5
                    WHEN LOWER(categorie) LIKE ? THEN 6
                    ELSE 7
                  END ASC,
                  nom ASC
                LIMIT 7
            """, (
                f"%{txt_clean}%", f"%{st_lower}%", f"%{st_lower}%", f"%{st_lower}%",
                txt_clean, f"{txt_clean}%", st_lower, f"{st_lower}%", f"{st_lower}%", f"{st_lower}%"
            ))
            rows = c.fetchall()
            conn.close()

            if not rows:
                self._fermer_suggestions_caisse()
                return

            self._afficher_popup_suggestions_caisse(rows)
        except Exception as e:
            print("Erreur autocomplétion caisse:", e)

    def _afficher_popup_suggestions_caisse(self, rows):
        if not hasattr(self, "entry_caisse") or not self.entry_caisse.winfo_exists():
            return

        try:
            root_x = self.entry_caisse.winfo_rootx()
            root_y = self.entry_caisse.winfo_rooty()
            entry_h = self.entry_caisse.winfo_height()
            entry_w = max(450, self.entry_caisse.winfo_width())

            if hasattr(self, "suggestion_popup") and self.suggestion_popup and self.suggestion_popup.winfo_exists():
                popup = self.suggestion_popup
                for w in popup.winfo_children():
                    w.destroy()
            else:
                popup = ctk.CTkToplevel(self)
                popup.overrideredirect(True)
                popup.attributes("-topmost", True)
                popup.configure(fg_color="#FFFFFF")
                self.suggestion_popup = popup

            popup.geometry(f"{entry_w}x{min(320, len(rows)*48 + 12)}+{root_x}+{root_y + entry_h + 4}")

            container = ctk.CTkFrame(popup, fg_color="#FFFFFF", corner_radius=12, border_width=1, border_color="#E5E5EA")
            container.pack(fill="both", expand=True)

            for idx, r in enumerate(rows):
                pid, nom, code, cat, marque, prix_v, en_s, p_solde, tva = r
                p_eff = Decimal(str(p_solde)) if (en_s == 1 and p_solde is not None) else Decimal(str(prix_v or 0))

                row_f = ctk.CTkFrame(container, fg_color="#FAFAFC" if idx % 2 == 0 else "#FFFFFF", corner_radius=8, height=44)
                row_f.pack(fill="x", padx=4, pady=2)
                row_f.pack_propagate(False)

                left_f = ctk.CTkFrame(row_f, fg_color="transparent")
                left_f.pack(side="left", fill="both", expand=True, padx=10, pady=2)

                title_row = ctk.CTkFrame(left_f, fg_color="transparent")
                title_row.pack(fill="x", anchor="w")

                if en_s == 1:
                    ctk.CTkLabel(title_row, text="SOLDE", font=ctk.CTkFont(FNT_BODY, 8, "bold"), fg_color=RED, text_color="#FFFFFF", corner_radius=4, height=14, width=40).pack(side="left", padx=(0, 4))

                lbl_nom = ctk.CTkLabel(title_row, text=nom, font=ctk.CTkFont(FNT_BODY, 12, "bold"), text_color=TEXT, anchor="w")
                lbl_nom.pack(side="left")

                meta_str = " • ".join([x for x in [cat, marque, code] if x])
                lbl_meta = ctk.CTkLabel(left_f, text=meta_str if meta_str else "Général", font=ctk.CTkFont(FNT_BODY, 10), text_color=GRY, anchor="w")
                lbl_meta.pack(fill="x")

                lbl_px = ctk.CTkLabel(row_f, text=f"{p_eff:.2f} €", font=ctk.CTkFont(FNT_BODY, 13, "bold"), text_color=ACCENT)
                lbl_px.pack(side="right", padx=12)

                def _select_prod(pd=r):
                    self._fermer_suggestions_caisse()
                    self.entry_caisse.delete(0, "end")
                    p_id, p_nom, p_code, p_cat, p_marque, p_prix_v, p_en_s, p_p_solde, p_tva = pd
                    self._process_ajouter_produit_caisse(p_id, p_nom, p_prix_v, p_tva, p_en_s, p_p_solde)

                for widget in (row_f, left_f, title_row, lbl_nom, lbl_meta, lbl_px):
                    widget.bind("<Button-1>", lambda e, pd=r: _select_prod(pd))

        except Exception as ex:
            print("Erreur affichage popup suggestions:", ex)

    def _fermer_suggestions_caisse(self):
        if hasattr(self, "suggestion_popup") and self.suggestion_popup and self.suggestion_popup.winfo_exists():
            try:
                self.suggestion_popup.destroy()
            except:
                pass
            self.suggestion_popup = None

    def _valider_entry_caisse(self):
        self._fermer_suggestions_caisse()
        code = self.entry_caisse.get().strip()
        if code:
            code = self._translate_azerty(code)
            self.on_scan_caisse(code)
            self.entry_caisse.delete(0, "end")

    def _valider_entry_stocks(self):
        code = self.entry_stocks.get().strip()
        if code:
            translated_code = self._translate_azerty(code)
            try:
                conn = get_connection(); c = conn.cursor()
                c.execute("SELECT id FROM Produits WHERE code_barre=? LIMIT 1", (translated_code,))
                row = c.fetchone()
                conn.close()
                if row:
                    self.on_scan_inventaire(translated_code)
                    self.entry_stocks.delete(0, "end")
                else:
                    self._refresh_stocks_table()
            except Exception:
                self._refresh_stocks_table()

    # ── HORLOGE TEMPS RÉEL ───────────────────────────────────
    def _tick_clock(self):
        """Met à jour l'horloge dans le header Caisse toutes les secondes."""
        try:
            if hasattr(self, 'lbl_clock') and self.lbl_clock.winfo_exists():
                now = datetime.datetime.now()
                self.lbl_clock.configure(text=now.strftime("%H:%M:%S"))
                self.after(1000, self._tick_clock)
        except Exception:
            pass  # Widget détruit (verrouillage) — on arrête silencieusement

    # ── SONS ─────────────────────────────────────────────────
    def _snd(self, f):
        if sys.platform == "darwin":
            import threading
            threading.Thread(target=lambda: os.system(f"afplay {f} > /dev/null 2>&1"), daemon=True).start()
    def jouer_son_caisse(self):     self._snd("/System/Library/Sounds/Ping.aiff")
    def jouer_son_inventaire(self): self._snd("/System/Library/Sounds/Basso.aiff")
    def jouer_son_erreur(self):     self._snd("/System/Library/Sounds/Sosumi.aiff")

    # ── LOGIQUE CAISSE ───────────────────────────────────────
    def on_scan_caisse(self, code):
        print(f"[SCAN] {code}")
        try:
            conn = get_connection(); c = conn.cursor()
            code_str = code.strip()

            # 1. NIVEAU 1 : Recherche par CODE-BARRES EXACT (Priorité absolue)
            c.execute("""
                SELECT id, nom, prix_vente_tvac, taux_tva, en_solde, prix_solde_tvac 
                FROM Produits 
                WHERE code_barre = ?
            """, (code_str,))
            prod_row = c.fetchone()

            # 2. NIVEAU 2 : Recherche par NOM EXACT si non trouvé par code-barres
            if not prod_row:
                c.execute("""
                    SELECT id, nom, prix_vente_tvac, taux_tva, en_solde, prix_solde_tvac 
                    FROM Produits 
                    WHERE LOWER(nom) = ?
                """, (code_str.lower(),))
                prod_row = c.fetchone()

            # 3. NIVEAU 3 : Recherche par CORRESPONDANCE PARTIELLE (Tri anticipé par priorité)
            if not prod_row:
                st_lower = code_str.lower()
                c.execute("""
                    SELECT id, nom, prix_vente_tvac, taux_tva, en_solde, prix_solde_tvac, code_barre, marque, categorie
                    FROM Produits 
                    WHERE code_barre LIKE ? OR LOWER(nom) LIKE ? OR LOWER(marque) LIKE ? OR LOWER(categorie) LIKE ?
                    ORDER BY 
                      CASE 
                        WHEN code_barre = ? THEN 1
                        WHEN code_barre LIKE ? THEN 2
                        WHEN LOWER(nom) = ? THEN 3
                        WHEN LOWER(nom) LIKE ? THEN 4
                        WHEN LOWER(marque) LIKE ? THEN 5
                        WHEN LOWER(categorie) LIKE ? THEN 6
                        ELSE 7 
                      END ASC,
                      LENGTH(code_barre) ASC,
                      code_barre ASC,
                      nom ASC
                    LIMIT 20
                """, (
                    f"%{code_str}%", f"%{st_lower}%", f"%{st_lower}%", f"%{st_lower}%",
                    code_str, f"{code_str}%", st_lower, f"{st_lower}%", f"{st_lower}%", f"{st_lower}%"
                ))
                matching_rows = c.fetchall()

                if not matching_rows:
                    self._st(f"Produit inconnu : {code_str}", RED)
                    self.jouer_son_erreur()
                    conn.close()
                    return
                elif len(matching_rows) == 1:
                    r = matching_rows[0]
                    prod_row = (r[0], r[1], r[2], r[3], r[4], r[5])
                else:
                    # Plusieurs articles correspondent : ouvrir la modale de choix explicite pour éviter toute confusion
                    conn.close()
                    from views.modals import RechercheProduitCaisseModal
                    def _on_product_chosen(chosen_row):
                        pid, nom_chosen, prix_v, tva_val, en_s, p_solde = chosen_row[0], chosen_row[1], chosen_row[2], chosen_row[3], chosen_row[4], chosen_row[5]
                        self._process_ajouter_produit_caisse(pid, nom_chosen, prix_v, tva_val, en_s, p_solde)
                    RechercheProduitCaisseModal(self, matching_rows, _on_product_chosen)
                    return

            conn.close()
            pid, nom, prix_vente, tva, en_solde, prix_solde = prod_row
            self._process_ajouter_produit_caisse(pid, nom, prix_vente, tva, en_solde, prix_solde)

        except Exception as e:
            self._st(f"Erreur : {e}", RED); print(e)

    def _process_ajouter_produit_caisse(self, pid, nom, prix_vente, tva, en_solde, prix_solde):
        try:
            if en_solde == 1 and prix_solde is not None:
                prix = Decimal(str(prix_solde))
            else:
                prix = Decimal(str(prix_vente)) if prix_vente is not None else Decimal("0.00")
            tva = Decimal(str(tva)) if tva is not None else Decimal("0.21")
            
            conn = get_connection(); c = conn.cursor()
            c.execute("SELECT id, taille, quantite_actuelle FROM Stocks WHERE id_produit=?", (pid,))
            stock_rows = c.fetchall()
            conn.close()
            
            if not stock_rows:
                self._st(f"Aucun stock configuré pour : {nom}", "#FF9900"); self.jouer_son_erreur(); return

            def _ajouter_au_panier(stock_id, taille, qte_actuelle):
                orig_p = Decimal(str(prix_vente)) if prix_vente is not None else prix
                self.panier.append({
                    "nom": nom,
                    "taille": taille or "—",
                    "prix_vente_tvac": prix,
                    "taux_tva": tva,
                    "stock_id": stock_id,
                    "en_solde": en_solde,
                    "prix_original_tvac": orig_p,
                    "remise_label": ""
                })
                self.total_tvac += prix
                self._st(f"Ajouté : {nom} ({taille or '—'}) → {prix} €", GRN)
                self._show_toast(f"＋1 {nom} ({taille or '—'}) • {prix:.2f} €")
                self.jouer_son_caisse()
                self._refresh_panier()

            # S'il y a plusieurs variantes de taille
            if len(stock_rows) > 1:
                from views.modals import TailleSelectionModal
                TailleSelectionModal(self, nom, stock_rows, _ajouter_au_panier)
            else:
                # S'il y a une seule variante de taille, l'ajouter directement si en stock
                sid, taille, qte = stock_rows[0]
                if qte <= 0:
                    self._st(f"Stock épuisé : {nom} ({taille})", "#FF9900"); self.jouer_son_erreur(); return
                _ajouter_au_panier(sid, taille, qte)

        except Exception as e:
            self._st(f"Erreur ajout panier : {e}", RED); print(e)

    def _refresh_panier(self):
        for w in self.panier_scroll.winfo_children(): w.destroy()
        
        row_offset = 0
        if self.nom_client:
            taille_haut, taille_bas, pointure, pref, bday = "", "", "", "", ""
            achats = []
            try:
                conn = get_connection(); c = conn.cursor()
                c.execute("SELECT taille_haut, taille_bas, pointure, pref_couleurs, date_anniversaire FROM Clients WHERE id=?", (self.id_client,))
                row = c.fetchone()
                if row:
                    taille_haut, taille_bas, pointure, pref, bday = [x or "" for x in row]
                
                c.execute("""
                    SELECT DISTINCT p.nom, vd.taille 
                    FROM Ventes_Details vd
                    JOIN Tickets t ON vd.id_ticket = t.id
                    JOIN Stocks s ON vd.id_stock = s.id
                    JOIN Produits p ON s.id_produit = p.id
                    WHERE t.id_client = ?
                    ORDER BY t.date_heure DESC
                    LIMIT 3
                """, (self.id_client,))
                achats = c.fetchall()
                conn.close()
            except Exception as e:
                print("Err get client details:", e)
                
            client_card = ctk.CTkFrame(self.panier_scroll, fg_color="#F3F9F6", corner_radius=16, border_width=1, border_color="#D1E7DD")
            client_card.grid(row=0, column=0, columnspan=4, sticky="ew", padx=4, pady=(0, 10))
            client_card.grid_columnconfigure(0, weight=1)
            
            title_f = ctk.CTkFrame(client_card, fg_color="transparent")
            title_f.pack(fill="x", padx=16, pady=(10, 4))
            
            ctk.CTkLabel(title_f, text=f"👤 CLIENT : {self.nom_client.upper()}", font=ctk.CTkFont(FNT_BODY, 13, "bold"), text_color="#0F5132").pack(side="left")
            
            if bday:
                try:
                    import datetime
                    today = datetime.date.today()
                    bday_d, bday_m = map(int, bday.split("-"))
                    if today.day == bday_d and today.month == bday_m:
                        ctk.CTkLabel(title_f, text="🎉 ANNIVERSAIRE AUJOURD'HUI !", font=ctk.CTkFont(FNT_BODY, 11, "bold"), text_color=RED).pack(side="right")
                except:
                    pass
            
            pref_parts = []
            if taille_haut: pref_parts.append(f"Haut: {taille_haut}")
            if taille_bas: pref_parts.append(f"Bas: {taille_bas}")
            if pointure: pref_parts.append(f"Pied: {pointure}")
            pref_text = "Tailles : " + (" | ".join(pref_parts) if pref_parts else "Non renseignées")
            if pref:
                pref_text += f"  •  Pref: {pref}"
            ctk.CTkLabel(client_card, text=pref_text, font=ctk.CTkFont(FNT_BODY, 11), text_color="#155724", anchor="w").pack(fill="x", padx=16, pady=(0, 4))
            
            if achats:
                achats_str = ", ".join([f"{n} ({t})" for n, t in achats])
                ctk.CTkLabel(client_card, text=f"Derniers achats : {achats_str}", font=ctk.CTkFont(FNT_BODY, 11, "italic"), text_color="#155724", anchor="w").pack(fill="x", padx=16, pady=(0, 10))
            
            row_offset = 1

        self.total_tvac = sum((it.get("prix_vente_tvac") or Decimal("0.00")) for it in self.panier)

        if not self.panier:
            ctk.CTkLabel(self.panier_scroll, text="Scannez un article pour commencer…",
                         font=ctk.CTkFont(FNT_BODY, 13), text_color=GRY
                         ).grid(row=row_offset, column=0, columnspan=4, pady=50)
        else:
            for idx, it in enumerate(self.panier):
                r = ctk.CTkFrame(self.panier_scroll, fg_color="#FFFFFF", corner_radius=16, border_width=1, border_color="#E5E5EA", height=54)
                r.grid(row=idx + row_offset, column=0, columnspan=4, sticky="ew", padx=4, pady=4)
                r.grid_propagate(False)
                r.grid_columnconfigure(0, weight=5) # Nom & Catégorie
                r.grid_columnconfigure(1, weight=3) # Contrôle Quantité - 1 +
                r.grid_columnconfigure(2, weight=2) # Prix
                r.grid_columnconfigure(3, weight=1) # Action Supprimer

                # Colonne 0 : Nom produit + Taille / Catégorie
                name_frame = ctk.CTkFrame(r, fg_color="transparent")
                name_frame.grid(row=0, column=0, padx=14, sticky="w")

                if it.get("en_solde") == 1:
                    prix_sold = it.get("prix_vente_tvac", Decimal("0"))
                    prix_orig = it.get("prix_original_tvac")
                    pct_str = ""
                    if prix_orig and prix_orig > 0:
                        pct = int(round(((prix_orig - prix_sold) / prix_orig) * 100))
                        if pct > 0:
                            pct_str = f" -{pct}%"
                    badge = ctk.CTkLabel(name_frame, text=f"SOLDE{pct_str}", font=ctk.CTkFont(FNT_BODY, 9, "bold"), fg_color=RED, text_color="#FFFFFF", corner_radius=6, height=16, width=60)
                    badge.pack(side="left", padx=(0, 4))

                if it.get("remise_label"):
                    badge_rem = ctk.CTkLabel(name_frame, text=f"REM {it['remise_label']}", font=ctk.CTkFont(FNT_BODY, 9, "bold"), fg_color="#FF9500", text_color="#FFFFFF", corner_radius=6, height=16, width=60)
                    badge_rem.pack(side="left", padx=(0, 4))

                text_col_f = ctk.CTkFrame(name_frame, fg_color="transparent")
                text_col_f.pack(side="left")
                ctk.CTkLabel(text_col_f, text=it["nom"], font=ctk.CTkFont(FNT_BODY, 13, "bold"), text_color=TEXT, anchor="w").pack(anchor="w")
                cat_desc = it.get("taille", "Unique")
                ctk.CTkLabel(text_col_f, text=cat_desc, font=ctk.CTkFont(FNT_BODY, 10), text_color=GRY, anchor="w").pack(anchor="w")

                # Colonne 1 : Contrôle Quantité (- 1 +)
                qty_f = ctk.CTkFrame(r, fg_color="transparent")
                qty_f.grid(row=0, column=1, padx=4, sticky="center")
                
                def _dec_item(i=idx):
                    if 0 <= i < len(self.panier):
                        self.panier.pop(i)
                        self._refresh_panier()

                def _inc_item(i=idx):
                    if 0 <= i < len(self.panier):
                        dup = dict(self.panier[i])
                        self.panier.insert(i, dup)
                        self._refresh_panier()

                btn_minus = ctk.CTkButton(qty_f, text="−", width=22, height=22, fg_color="transparent", hover_color="#FFEBEB", text_color=RED, corner_radius=11, font=ctk.CTkFont(FNT_BODY, 13, "bold"), command=_dec_item)
                btn_minus.pack(side="left", padx=2)

                ctk.CTkLabel(qty_f, text="1", font=ctk.CTkFont(FNT_BODY, 12, "bold"), text_color=TEXT, width=16).pack(side="left", padx=2)

                btn_plus = ctk.CTkButton(qty_f, text="+", width=22, height=22, fg_color="transparent", hover_color="#FFEBEB", text_color=RED, corner_radius=11, font=ctk.CTkFont(FNT_BODY, 13, "bold"), command=_inc_item)
                btn_plus.pack(side="left", padx=2)

                # Colonne 2 : Zone Prix (Prix barré si réduction)
                prix_box = ctk.CTkFrame(r, fg_color="transparent")
                prix_box.grid(row=0, column=2, padx=6, sticky="e")

                prix_act = it["prix_vente_tvac"]
                prix_orig = it.get("prix_original_tvac")

                if prix_orig and prix_orig > prix_act:
                    ctk.CTkLabel(prix_box, text=f"{prix_orig:.2f} €", font=ctk.CTkFont(FNT_BODY, 10, "overstrike"), text_color=GRY).pack(side="left", padx=(0, 4))

                ctk.CTkLabel(prix_box, text=f"{prix_act:.2f} €", font=ctk.CTkFont(FNT_BODY, 13, "bold"), text_color=TEXT).pack(side="right")

                # Colonne 3 : Boutons d'action (Remise + Supprimer poubelle)
                actions_f = ctk.CTkFrame(r, fg_color="transparent")
                actions_f.grid(row=0, column=3, padx=10, sticky="e")

                btn_rem = ctk.CTkButton(actions_f, text="🏷️", width=24, height=24, fg_color="#F4F5F7", hover_color="#E5E5EA", text_color=TEXT, corner_radius=12, font=ctk.CTkFont(FNT_BODY, 10),
                                        command=lambda i=idx: self._ouvrir_remise_ligne_modal(i))
                btn_rem.pack(side="left", padx=2)

                btn_del = ctk.CTkButton(actions_f, text="🗑", width=24, height=24, fg_color="transparent", hover_color="#FFEBEB", text_color=GRY, corner_radius=12, font=ctk.CTkFont(FNT_BODY, 12),
                                        command=lambda i=idx: self._del(i))
                btn_del.pack(side="left", padx=2)
        # Total net après remise
        net = (self.total_tvac - self.remise).quantize(Decimal("0.01"))
        htva = (net / Decimal("1.21")).quantize(Decimal("0.01"))
        tva = (net - htva).quantize(Decimal("0.01"))
        fmt = f"{net:,.2f}".replace(",","_").replace(".",",").replace("_"," ")
        
        self.label_total.configure(text=f"{fmt} €")
        if hasattr(self, "lbl_htva") and self.lbl_htva:
            self.lbl_htva.configure(text=f"Hors TVA : {htva:.2f} €")
        if hasattr(self, "lbl_tva_21") and self.lbl_tva_21:
            self.lbl_tva_21.configure(text=f"TVA (21%) : {tva:.2f} €")
        if hasattr(self, "btn_valider_encaissement") and self.btn_valider_encaissement:
            self.btn_valider_encaissement.configure(text=f"VALIDER LE PAIEMENT ({fmt} €)")

        if self.remise > 0:
            self.label_remise.configure(text=f"Remise : −{self.remise:.2f} €")
        else:
            self.label_remise.configure(text="")
        self.label_nb.configure(text=f"{len(self.panier)} article(s)")
        
        if self.nom_client:
            pts = 0
            try:
                conn = get_connection(); c = conn.cursor()
                c.execute("SELECT points_fidelite FROM Clients WHERE id=?", (self.id_client,))
                row = c.fetchone()
                if row: pts = row[0]
                conn.close()
            except:
                pass
            self.btn_client.configure(text=f"OK {self.nom_client[:12]} ({pts} pts)", text_color=GRN)
        else:
            self.btn_client.configure(text="+ Client", text_color=GRY)

        self._recalculer_rendu()

        # Sauvegarde ou purge automatique de la session panier
        if self.panier:
            self._sauvegarder_panier_session()
        else:
            self._purger_panier_session()

    def _set_pay_method(self, method):
        self.current_pay_mode = method
        for m, btn in [("Bancontact", getattr(self, "btn_mode_cb", None)), ("Espèces", getattr(self, "btn_mode_esp", None)), ("QR_Code", getattr(self, "btn_mode_qr", None))]:
            if btn:
                if m == method:
                    btn.configure(fg_color=ACCENT, text_color="#FFFFFF", border_width=0)
                else:
                    btn.configure(fg_color="#FFFFFF", text_color=TEXT, border_width=1, border_color="#CED4DA")

    def _preset_cash(self, val):
        net = (self.total_tvac - self.remise).quantize(Decimal("0.01"))
        if val == "exact":
            target = net
        else:
            target = Decimal(str(val))
        
        if hasattr(self, "entry_cash_received") and self.entry_cash_received:
            self.entry_cash_received.delete(0, "end")
            self.entry_cash_received.insert(0, f"{target:.2f}")
        self._recalculer_rendu()

    def _recalculer_rendu(self):
        if not hasattr(self, "entry_cash_received") or not self.entry_cash_received:
            return
        net = (self.total_tvac - self.remise).quantize(Decimal("0.01"))
        val_str = self.entry_cash_received.get().strip().replace(",", ".")
        try:
            recu = Decimal(val_str) if val_str else Decimal("0.00")
            rendu = max(Decimal("0.00"), recu - net)
            if hasattr(self, "lbl_rendu_monnaie") and self.lbl_rendu_monnaie:
                self.lbl_rendu_monnaie.configure(text=f"Rendu : {rendu:.2f} €")
        except Exception:
            if hasattr(self, "lbl_rendu_monnaie") and self.lbl_rendu_monnaie:
                self.lbl_rendu_monnaie.configure(text="Rendu : 0,00 €")

    def _executer_encaissement_direct(self):
        if not self.panier:
            self._show_toast("Le panier est vide, impossible d'encaisser.", bg_color="#FF3B30")
            return

        net = (self.total_tvac - self.remise).quantize(Decimal("0.01"))
        methode = getattr(self, "current_pay_mode", "Bancontact")
        rendu = Decimal("0.00")

        if methode == "Espèces":
            val_str = self.entry_cash_received.get().strip().replace(",", ".") if hasattr(self, "entry_cash_received") else ""
            try:
                recu = Decimal(val_str) if val_str else net
                rendu = max(Decimal("0.00"), recu - net)
            except Exception:
                recu = net
                rendu = Decimal("0.00")

        paiements = [(methode, net)]
        self._finaliser_vente(paiements, rendu)

    def _ouvrir_remise_ligne_modal(self, i):
        if 0 <= i < len(self.panier):
            item = self.panier[i]
            from views.modals import RemiseLigneModal
            def _on_apply(new_price, orig_price, remise_label):
                item["prix_vente_tvac"] = new_price
                item["prix_original_tvac"] = orig_price
                item["remise_label"] = remise_label
                self._st(f"Remise sur {item['nom']} : {new_price:.2f} € ({remise_label})", GRN)
                self._refresh_panier()

            RemiseLigneModal(self, item, _on_apply)

    def _del(self, i):
        if 0 <= i < len(self.panier):
            self.panier.pop(i)
            self.after(10, self._refresh_panier)

    def vider_panier(self):
        self.panier=[]; self.total_tvac=Decimal("0.00")
        self.remise=Decimal("0.00"); self.id_client=None; self.nom_client=None
        self._refresh_panier(); self._st("Panier vidé.",GRY)

    def _update_badge_paniers_attente(self):
        try:
            import database_manager
            paniers = database_manager.lister_paniers_en_attente()
            nb = len(paniers)
            if hasattr(self, "btn_paniers_attente") and self.btn_paniers_attente:
                if nb > 0:
                    self.btn_paniers_attente.configure(text=f"Attente ({nb})", fg_color=ACCENT[0], text_color="#FFFFFF")
                else:
                    self.btn_paniers_attente.configure(text="Attente (0)", fg_color="transparent", text_color=TEXT[0])
        except Exception as e:
            print(f"Erreur mise à jour badge paniers attente: {e}")

    def _mettre_panier_en_attente(self):
        if not self.panier:
            self._show_toast("Le panier est vide, impossible de le mettre en attente.", bg_color="#FF3B30")
            return

        try:
            import database_manager
            pid = database_manager.sauvegarder_panier_en_attente(
                panier=self.panier,
                total_tvac=self.total_tvac,
                client_id=self.id_client,
                client_nom=self.nom_client,
                remise=self.remise
            )
            self.vider_panier()
            self._show_toast(f"Panier #{pid} mis en attente avec succès !", bg_color="#34C759")
            self._update_badge_paniers_attente()
        except Exception as e:
            self._show_toast(f"Erreur de mise en attente: {e}", bg_color="#FF3B30")

    def _ouvrir_paniers_en_attente_modal(self):
        try:
            from views.modals import PaniersEnAttenteModal
            def _on_restore(data):
                panier_items = data.get("panier_raw", [])
                self.panier = []
                for item in panier_items:
                    self.panier.append({
                        "nom": item["nom"],
                        "taille": item["taille"],
                        "prix_vente_tvac": Decimal(item["prix_vente_tvac"]),
                        "taux_tva": Decimal(item.get("taux_tva", "0.21")),
                        "stock_id": item["stock_id"],
                        "en_solde": item.get("en_solde", 0),
                        "prix_original_tvac": Decimal(item["prix_original_tvac"]) if item.get("prix_original_tvac") is not None else None,
                        "code_barre": item.get("code_barre")
                    })
                self.remise = data.get("remise", Decimal("0.00"))
                self.id_client = data.get("client_id")
                self.nom_client = data.get("client_nom")
                self._refresh_panier()
                self._update_badge_paniers_attente()
                self._show_toast("Panier restauré avec succès !", bg_color="#34C759")

            PaniersEnAttenteModal(self, _on_restore)
        except Exception as e:
            print(f"Erreur ouverture modale paniers en attente: {e}")

    def _verifier_reprise_crash(self):
        try:
            session_data = CrashWatcher.get_unfinalized_basket()
            if session_data:
                def _do_restore():
                    panier_data = session_data.get("panier", [])
                    self.panier = []
                    for item in panier_data:
                        self.panier.append({
                            "nom": item["nom"],
                            "taille": item["taille"],
                            "prix_vente_tvac": Decimal(item["prix_vente_tvac"]),
                            "taux_tva": Decimal(item.get("taux_tva", "0.21")),
                            "stock_id": item["stock_id"],
                            "en_solde": item.get("en_solde", 0),
                            "prix_original_tvac": Decimal(item["prix_original_tvac"]) if item.get("prix_original_tvac") is not None else None,
                            "code_barre": item.get("code_barre")
                        })
                    self.remise = Decimal(session_data.get("remise", "0.00"))
                    self.id_client = session_data.get("id_client")
                    self.nom_client = session_data.get("nom_client")
                    self._refresh_panier()
                    self._show_toast("Panier restauré après interruption !", bg_color="#34C759")

                def _do_ignore():
                    CrashWatcher.clear_unfinalized_basket()
                    self._show_toast("Session précédente ignorée.")

                CrashRestorationModal(self, session_data, _do_restore, _do_ignore)
        except Exception as e:
            print(f"Erreur vérification reprise post-crash: {e}")

    def _verifier_mises_a_jour_au_demarrage(self):
        try:
            from services.update_checker import check_for_updates_async
            check_for_updates_async(self._on_update_check_result)
        except Exception as e:
            print(f"[UPDATE] Erreur lancement vérification maj: {e}")

    def _on_update_check_result(self, result):
        if result.get("has_update"):
            self.after(0, lambda: self._afficher_modal_mise_a_jour(result))

    def _afficher_modal_mise_a_jour(self, result):
        try:
            from views.modals.update_modal import UpdateNotificationModal, MandatoryUpdateOverlay
            from services.update_checker import open_download_page

            update_payload = {
                "version": result.get("latest_version"),
                "title": f"Mise à jour v{result.get('latest_version')} disponible",
                "summary": "Une nouvelle version de Kōdo POS est disponible avec des améliorations.",
                "highlights": [result.get("changelog", "Améliorations de sécurité et de performances.")],
                "actions": {"primary": "Télécharger la mise à jour", "secondary": "Plus tard"},
                "release_date": "Disponible dès maintenant"
            }

            if result.get("mandatory"):
                MandatoryUpdateOverlay(self, update_payload, on_complete_callback=lambda: open_download_page(result.get("download_url")))
            else:
                UpdateNotificationModal(self, update_payload, on_install_callback=lambda: open_download_page(result.get("download_url")))
        except Exception as e:
            print(f"[UPDATE] Erreur affichage modale maj: {e}")

    def _st(self, msg, color=GRY):
        self.statut.configure(text=msg, text_color=color)
        self.after(4000, lambda: self.statut.configure(text=""))

    def _toggle_theme(self, mode=None):
        if mode is None:
            current = ctk.get_appearance_mode()
            mode = "Light" if current == "Dark" else "Dark"
        ctk.set_appearance_mode(mode)
        self.is_dark_mode = (mode == "Dark")
        if hasattr(self, "btn_theme") and self.btn_theme:
            self.btn_theme.configure(text="☀️ Mode Clair" if self.is_dark_mode else "🌙 Mode Sombre")
        self._show_toast(f"Mode visuel : {mode}", bg_color="#FF7F7F" if self.is_dark_mode else "#1D1D1F")

    def _ouvrir_z_caisse(self):
        if not self.app_features.get("nf525_compliance", False):
            self._show_upsell_modal("nf525_compliance")
            return
            
        try:
            from views.modals.z_caisse_modal import ZDeCaisseModal
            vendeur = self.vendeur_actif['nom'] if self.vendeur_actif else 'Admin'
            ZDeCaisseModal(self, caisse_id="POS-01", vendeur=vendeur, on_complete=lambda r: self._st(f"[OK] Z de Caisse clôturé (Hash: {r['current_hash'][:12]}...)", GRN))
        except Exception as e:
            print(f"Erreur ouverture Z de Caisse: {e}")

    # ── MODALES ──────────────────────────────────────────────
    def _show_upsell_modal(self, feature_key):
        import license_manager
        modal_data = license_manager.get_upsell_modal_text(feature_key)
        title = modal_data.get("title", "Fonctionnalité Verrouillée 🔒")
        message = modal_data.get("message", "Cette fonctionnalité nécessite une licence supérieure.")
        btn_text = modal_data.get("button_text", "Mettre à niveau")

        mod = ctk.CTkToplevel(self)
        mod.title(title)
        mod.geometry("450x250")
        mod.attributes("-topmost", True)
        mod.transient(self)
        
        mod.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() - 450) // 2
        y = self.winfo_y() + (self.winfo_height() - 250) // 2
        mod.geometry(f"+{x}+{y}")
        
        f = ctk.CTkFrame(mod, fg_color="#FFFFFF", corner_radius=16)
        f.pack(fill="both", expand=True, padx=2, pady=2)
        
        ctk.CTkLabel(f, text=title, font=ctk.CTkFont(FNT_TITLE, 20, "bold"), text_color=TEXT).pack(pady=(30,10))
        ctk.CTkLabel(f, text=message, font=ctk.CTkFont(FNT_BODY, 14), text_color=GRY, wraplength=380, justify="center").pack(pady=(0,30))
        
        ctk.CTkButton(f, text=btn_text, height=44, font=ctk.CTkFont(FNT_BODY, 14, "bold"), command=mod.destroy).pack(pady=(0, 20))

    def _ouvrir_remise(self):
        from views.modals import RemiseModal
        if not self.panier: return
        tot = sum(it["prix_vente_tvac"] for it in self.panier)
        RemiseModal(self, tot, lambda r, m: self._appliquer_remise(r))

    def _ouvrir_prestation(self):
        from views.modals import PrestationModal
        def _add_prest(nom, prix, tva):
            # Ajout au panier comme un produit virtuel
            prest_item = {
                "stock_id": None, # Pas de gestion de stock
                "code_barre": None,
                "nom": f"[Prestation] {nom}",
                "taille": "—",
                "prix_vente_tvac": prix,
                "taux_tva": tva
            }
            self.panier.append(prest_item)
            self.total_tvac += prix
            self._st(f"Prestation ajoutée : {nom}", GRN)
            self._refresh_panier()
        
        PrestationModal(self, callback=_add_prest)

    def _appliquer_remise(self, montant, mode=None):
        self.remise = montant
        self._refresh_panier()
        self._st(f"Remise appliquée : −{montant:.2f} €", GRN)

    def _ouvrir_client(self):
        ClientModal(self, self._lier_client)

    def _lier_client(self, cid, nom):
        self.id_client = cid; self.nom_client = nom
        self._refresh_panier()
        self._st(f"Client lié : {nom}", GRN)

    def _ouvrir_encaissement(self, methode=None):
        if not self.panier:
            self._st("Le panier est vide.", "#FF9900"); return
        net = (self.total_tvac - self.remise).quantize(Decimal("0.01"))
        EncaissementModal(self, net, self._finaliser_vente, methode_defaut=methode, panier_items=list(self.panier))

    def _finaliser_vente(self, paiements, rendu, ticket_cadeau=False):
        """Callback de EncaissementModal — enregistre la vente et génère le ticket."""
        try:
            conn = get_connection(); c = conn.cursor()
            num  = generer_numero_ticket(c)
            net  = (self.total_tvac - self.remise).quantize(Decimal("0.01"))
            htva = sum((it["prix_vente_tvac"]/(Decimal("1")+it["taux_tva"])).quantize(Decimal("0.0001")) for it in self.panier).quantize(Decimal("0.01"))
            tva  = (net - htva).quantize(Decimal("0.01"))
            # Méthodes concatenatées si split
            methodes = " + ".join(m for m, _ in paiements)

            import datetime
            from database_manager import enregistrer_vente
            
            date_heure = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            vendeur = self.vendeur_actif['nom'] if self.vendeur_actif else 'Inconnu'
            
            enregistrer_vente(c, num, net, htva, tva, self.remise, methodes, self.id_client, rendu, self.panier, vendeur, date_heure, paiements)
            conn.commit()

            # Génération du ticket thermique
            contenu = ticket_printer.generer_ticket(
                numero=num, panier=self.panier,
                total_tvac=net, remise=self.remise,
                paiements=paiements, rendu_monnaie=rendu,
                nom_client=self.nom_client, shop_name=self.shop_name,
                shop_subtitle=self.shop_subtitle,
                shop_address=self.shop_address,
                shop_vat=self.shop_vat,
                vendeur_nom=vendeur,
                is_gift=False
            )
            ticket_printer.imprimer_ticket(contenu, num)

            if ticket_cadeau:
                contenu_cadeau = ticket_printer.generer_ticket(
                    numero=num, panier=self.panier,
                    total_tvac=net, remise=self.remise,
                    paiements=paiements, rendu_monnaie=rendu,
                    nom_client=self.nom_client, shop_name=self.shop_name,
                    shop_subtitle=self.shop_subtitle,
                    shop_address=self.shop_address,
                    shop_vat=self.shop_vat,
                    vendeur_nom=vendeur,
                    is_gift=True
                )
                ticket_printer.imprimer_ticket(contenu_cadeau, f"{num}_cadeau")

            self._st(f"[OK] Ticket {num}  —  {methodes}  —  {net} €", GRN)
            from views.modals import show_toast
            show_toast(self, f"Paiement validé : {net} €", type="success")
            
            self.vider_panier()
            
            if rendu > Decimal("0.00"):
                ChangeReturnModal(self, rendu)
        except Exception as e:
            self._st(f"Erreur paiement : {e}", RED); print(e)
            from views.modals import show_toast
            show_toast(self, f"Erreur paiement: {e}", type="error")

    def _reimprimer_dernier_ticket(self):
        """Réimprime le tout dernier ticket enregistré, soit depuis le fichier texte, soit en le régénérant depuis la base de données."""
        try:
            conn = get_connection()
            c = conn.cursor()
            c.execute("""
                SELECT id, numero_ticket, total_tvac, remise, methode_paiement, rendu_monnaie, vendeur_nom, id_client
                FROM Tickets 
                ORDER BY id DESC LIMIT 1
            """)
            row = c.fetchone()
            if not row:
                conn.close()
                self._st("Aucun ticket trouvé dans la base de données.", RED)
                from views.modals import show_toast
                show_toast(self, "Aucun ticket à réimprimer.", type="error")
                return
            
            t_id, numero, total_tvac, remise, methode_paiement, rendu_monnaie, vendeur_nom, id_client = row
            
            # Tenter de lire d'abord le fichier texte existant dans ~/Documents/Kodo_POS
            from database_manager import data_path
            nom_fichier = data_path(f"ticket_virtuel_{numero}.txt")
            
            contenu = None
            if os.path.exists(nom_fichier):
                try:
                    with open(nom_fichier, "r", encoding="utf-8") as f:
                        contenu = f.read()
                    print(f"[REPRINT] Chargement du fichier existant: {nom_fichier}")
                except Exception as fe:
                    print(f"[REPRINT] Erreur lors de la lecture du fichier ticket: {fe}")

            if not contenu:
                print(f"[REPRINT] Fichier non trouvé ou illisible. Régénération depuis SQLite...")
                # Régénération à partir de la base de données
                # 1. Récupérer le nom du client
                nom_client = None
                if id_client:
                    c.execute("SELECT nom FROM Clients WHERE id=?", (id_client,))
                    crow = c.fetchone()
                    if crow:
                        nom_client = crow[0]
                
                # 2. Récupérer les détails de vente (panier)
                c.execute("""
                    SELECT vd.prix_unitaire_tvac, p.nom, s.taille, p.taux_tva, vd.id_stock
                    FROM Ventes_Details vd
                    LEFT JOIN Stocks s ON vd.id_stock = s.id
                    LEFT JOIN Produits p ON s.id_produit = p.id
                    WHERE vd.id_ticket = ?
                """, (t_id,))
                panier_items = c.fetchall()
                
                panier = []
                for prix_unitaire, p_nom, s_taille, p_taux, id_stock in panier_items:
                    nom_item = p_nom or "Prestation"
                    panier.append({
                        "nom": nom_item,
                        "taille": s_taille or "—",
                        "prix_vente_tvac": Decimal(str(prix_unitaire)),
                        "taux_tva": Decimal(str(p_taux or "0.21")),
                        "stock_id": id_stock
                    })
                
                # 3. Récupérer le détail des paiements depuis Ledger_Caisse
                c.execute("""
                    SELECT methode_paiement, montant
                    FROM Ledger_Caisse
                    WHERE reference = ? AND type_mouvement = 'VENTE'
                """, (numero,))
                ledger_items = c.fetchall()
                
                paiements = []
                # Si pas trouvé dans le ledger
                if not ledger_items:
                    paiements = [(methode_paiement or "Espèces", total_tvac + remise)]
                else:
                    for m, mt in ledger_items:
                        paiements.append((m, Decimal(str(mt))))
                    # Si Espèces est présent et qu'il y a du rendu monnaie, on réajuste le montant donné par le client
                    if rendu_monnaie and rendu_monnaie > Decimal("0"):
                        for idx, (m, mt) in enumerate(paiements):
                            if m == "Espèces":
                                paiements[idx] = ("Espèces", mt + rendu_monnaie)
                                break

                # Génération du ticket
                contenu = ticket_printer.generer_ticket(
                    numero=numero, panier=panier,
                    total_tvac=total_tvac, remise=remise,
                    paiements=paiements, rendu_monnaie=rendu_monnaie,
                    nom_client=nom_client, shop_name=self.shop_name,
                    shop_subtitle=self.shop_subtitle,
                    shop_address=self.shop_address,
                    shop_vat=self.shop_vat,
                    vendeur_nom=vendeur_nom
                )
            
            conn.close()

            # Impression effective
            ticket_printer.imprimer_ticket(contenu, numero)
            self._st(f"[OK] Réimpression Ticket {numero} relancée.", GRN)
            from views.modals import show_toast
            show_toast(self, f"Réimpression Ticket {numero} relancée", type="success")
            
        except Exception as e:
            self._st(f"Erreur réimpression : {e}", RED)
            print(f"[REPRINT ERROR] {e}")
            from views.modals import show_toast
            show_toast(self, f"Erreur réimpression: {e}", type="error")

    # Ancienne méthode conservée pour compatibilité
    def valider_paiement(self, methode):
        self._ouvrir_encaissement()

    def on_scan_inventaire(self, code):
        """Recherche et met en surbrillance le produit scanné dans la vue Stocks."""
        print(f"[INVENTAIRE] {code}")
        self.jouer_son_inventaire()
        try:
            conn = get_connection(); c = conn.cursor()
            c.execute("SELECT id, nom FROM Produits WHERE code_barre=? LIMIT 1", (code,))
            row = c.fetchone()
            if not row:
                self._st(f"Produit inconnu : {code}", RED)
                self.jouer_son_erreur()
                return
            pid, nom = row
            # Remettre toutes les cartes à leur style normal
            if hasattr(self, '_stock_rows'):
                for card in self._stock_rows:
                    card.configure(border_color=LINE, border_width=0)
            # Trouver et mettre en valeur la carte correspondante
            if hasattr(self, '_stock_rows') and hasattr(self, '_stock_product_ids'):
                for idx, card_pid in enumerate(self._stock_product_ids):
                    if card_pid == pid and idx < len(self._stock_rows):
                        self._stock_rows[idx].configure(border_color=ACCENT, border_width=0)
                        self._selected_product_id = pid
                        # Scroll vers la carte
                        self.stocks_scroll._parent_canvas.yview_moveto(
                            (idx // 4) / max(1, len(self._stock_rows) // 4)
                        )
                        break
            self._st(f"[OK] {nom} trouvé", GRN)
        except Exception as e:
            self._st(f"Erreur recherche : {e}", RED)

    def _sauvegarder_panier_session(self):
        try:
            panier_serialisable = []
            for item in self.panier:
                panier_serialisable.append({
                    "nom": item["nom"],
                    "taille": item["taille"],
                    "prix_vente_tvac": str(item["prix_vente_tvac"]),
                    "taux_tva": str(item["taux_tva"]),
                    "stock_id": item["stock_id"],
                    "en_solde": item.get("en_solde", 0),
                    "prix_original_tvac": str(item["prix_original_tvac"]) if item.get("prix_original_tvac") is not None else None
                })
            
            data = {
                "panier": panier_serialisable,
                "remise": str(self.remise),
                "id_client": self.id_client,
                "nom_client": self.nom_client,
                "total_tvac": str(self.total_tvac)
            }
            
            with open(data_path("panier_session.json"), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
        except Exception as e:
            print(f"Erreur de sauvegarde de la session panier : {e}")

    def _charger_panier_session(self):
        if not os.path.exists(data_path("panier_session.json")):
            return
        try:
            with open(data_path("panier_session.json"), "r", encoding="utf-8") as f:
                data = json.load(f)
            
            panier_data = data.get("panier", [])
            self.panier = []
            for item in panier_data:
                self.panier.append({
                    "nom": item["nom"],
                    "taille": item["taille"],
                    "prix_vente_tvac": Decimal(item["prix_vente_tvac"]),
                    "taux_tva": Decimal(item["taux_tva"]),
                    "stock_id": item["stock_id"],
                    "en_solde": item.get("en_solde", 0),
                    "prix_original_tvac": Decimal(item["prix_original_tvac"]) if item.get("prix_original_tvac") is not None else None
                })
            
            self.remise = Decimal(data.get("remise", "0.00"))
            self.id_client = data.get("id_client")
            self.nom_client = data.get("nom_client")
            self.total_tvac = Decimal(data.get("total_tvac", "0.00"))
            
            if self.panier:
                self._refresh_panier()
                self._st("Session panier restaurée !", GRN)
        except Exception as e:
            print(f"Erreur lors du chargement de la session panier : {e}")

    def _purger_panier_session(self):
        import os
        from database_manager import data_path
        session_file = data_path("panier_session.json")
        if os.path.exists(session_file):
            try:
                os.remove(session_file)
            except Exception as e:
                print(f"Erreur purge session panier : {e}")


if __name__ == "__main__":
    try:
        from firebase_sync import start_sync_thread
        sync_thread = start_sync_thread()
    except Exception as e:
        print(f"Erreur firebase_sync: {e}")
        
    try:
        from shopify_sync import ShopifySyncThread
        shopify_thread = ShopifySyncThread()
        shopify_thread.start()
    except Exception as e:
        print(f"Erreur shopify_sync: {e}")
        
    app = MainApp()
    app.mainloop()
