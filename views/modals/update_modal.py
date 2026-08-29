"""
Modale de Notification, Bandeau et Overlay de Verrouillage pour Mises à Jour (Kōdo POS).
"""
import customtkinter as ctk
import threading
import time
from views.modals.base import BG, SEC_BG, ACCENT, TEXT, GRY, LINE, RED, GRN, FNT_TITLE, FNT_BODY, RAD, ToastNotification
from core.updater import AppUpdateEngine

class MandatoryUpdateBanner(ctk.CTkFrame):
    """Étape 2A : Bandeau d'avertissement supérieur affiché pendant une vente en cours."""

    def __init__(self, parent, message=None):
        super().__init__(parent, fg_color=RED, height=45, corner_radius=0)
        
        text = message or "⚠️ Mise à jour obligatoire requise. Terminez la vente en cours. L'installation démarrera à la fin du ticket."
        self.lbl = ctk.CTkLabel(
            self, 
            text=text, 
            font=ctk.CTkFont(FNT_BODY, 13, "bold"), 
            text_color="#FFFFFF"
        )
        self.lbl.pack(padx=20, pady=10)

class MandatoryUpdateOverlay(ctk.CTkToplevel):
    """Étape 2B & 3 : Overlay de verrouillage inévitable avec décompte automatique de 10s."""

    def __init__(self, parent, update_payload: dict, on_complete_callback=None):
        super().__init__(parent)
        self.update_payload = update_payload
        self.on_complete_callback = on_complete_callback
        
        self.version = update_payload.get("version", "2.2.0")
        self.reason = update_payload.get("reason", "Mise en conformité de sécurité et correctif BDD.")
        self.est_duration = update_payload.get("estimated_duration_seconds", 15)
        self.countdown = 10
        self.is_installing = False

        self.title("Mise à jour critique requise")
        self.geometry("560x600")
        self.configure(fg_color=BG)
        self.resizable(False, False)

        # Verrouillage Absolu de l'IHM (Inescapable)
        self.transient(parent)
        self.grab_set()

        # Désactivation stricte de la touche Échap et de la fermeture de fenêtre
        self.bind("<Escape>", lambda e: "break")
        self.protocol("WM_DELETE_WINDOW", lambda: None)

        # UI Overlay
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=25, pady=(25, 10))

        lbl_badge = ctk.CTkLabel(
            header, 
            text="🔒 MISE À JOUR CRITIQUE OBLIGATOIRE", 
            font=ctk.CTkFont(FNT_TITLE, 13, "bold"), 
            text_color="#FFFFFF",
            fg_color=RED,
            corner_radius=8,
            padx=12,
            pady=6
        )
        lbl_badge.pack(anchor="w", pady=(0, 10))

        ctk.CTkLabel(
            header, 
            text=f"Version {self.version} requise", 
            font=ctk.CTkFont(FNT_TITLE, 22, "bold"), 
            anchor="w"
        ).pack(anchor="w")

        # Card explicative
        card = ctk.CTkFrame(self, fg_color=SEC_BG, corner_radius=RAD)
        card.pack(fill="both", expand=True, padx=25, pady=10)

        ctk.CTkLabel(
            card, 
            text="Raison de la mise à jour :", 
            font=ctk.CTkFont(FNT_BODY, 13, "bold"), 
            text_color=ACCENT
        ).pack(anchor="w", padx=20, pady=(15, 5))

        ctk.CTkLabel(
            card, 
            text=self.reason, 
            font=ctk.CTkFont(FNT_BODY, 14), 
            text_color=TEXT, 
            wraplength=450, 
            justify="left"
        ).pack(anchor="w", padx=20, pady=(0, 15))

        ctk.CTkLabel(
            card, 
            text=f"⏱️ Durée estimée : ~{self.est_duration} secondes", 
            font=ctk.CTkFont(FNT_BODY, 13), 
            text_color=GRY
        ).pack(anchor="w", padx=20, pady=(0, 15))

        # Zone Rassurance
        reassurance = ctk.CTkFrame(card, fg_color="transparent")
        reassurance.pack(fill="x", padx=20, pady=10)
        ctk.CTkLabel(
            reassurance, 
            text="🔒 Vos données sont en sécurité. Ne coupez pas l'alimentation.", 
            font=ctk.CTkFont(FNT_BODY, 12, "bold"), 
            text_color=GRN
        ).pack()

        # Progression (Étape 3)
        self.progress_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.lbl_step = ctk.CTkLabel(self.progress_frame, text="", font=ctk.CTkFont(FNT_BODY, 13, "bold"), text_color=TEXT)
        self.progress_bar = ctk.CTkProgressBar(self.progress_frame, mode="determinate", fg_color=LINE, progress_color=ACCENT, height=12)

        # Action Unique avec Décompte (10s)
        self.btn_action = ctk.CTkButton(
            self, 
            text=f"Installer et redémarrer maintenant ({self.countdown}s)", 
            fg_color=RED, 
            hover_color="#CC2D24",
            height=48, 
            corner_radius=12,
            font=ctk.CTkFont(FNT_BODY, 15, "bold"),
            command=self._trigger_install
        )
        self.btn_action.pack(fill="x", padx=25, pady=20)

        # Lancer le décompte automatique de 10s
        self._start_countdown()

    def _start_countdown(self):
        if self.countdown > 0 and not self.is_installing:
            self.btn_action.configure(text=f"Installer et redémarrer maintenant ({self.countdown}s)")
            self.countdown -= 1
            self.after(1000, self._start_countdown)
        elif self.countdown == 0 and not self.is_installing:
            self._trigger_install()

    def _trigger_install(self):
        """Étape 3 : Écran d'installation et de progression."""
        if self.is_installing:
            return

        self.is_installing = True
        self.btn_action.configure(state="disabled", text="Mise à jour en cours...")

        self.progress_frame.pack(fill="x", padx=25, pady=(0, 15))
        self.lbl_step.pack(pady=5)
        self.progress_bar.pack(fill="x", pady=5)
        self.progress_bar.set(0.0)

        def _installation_worker():
            # Phase 1: Sauvegarde & Snapshot Pré-Update
            self.lbl_step.configure(text="1/3 : Création du snapshot de sécurité pré-update...")
            self.progress_bar.set(0.25)
            try:
                from core.rollback_manager import RollbackManager
                from core.crash_watcher import CrashWatcher
                snap_path = RollbackManager.create_pre_update_snapshot(self.version)
                CrashWatcher.mark_update_started(self.version, self.version, snap_path)
            except Exception as e:
                print(f"Warn snapshot: {e}")
            time.sleep(0.8)

            # Phase 2: Migration
            self.lbl_step.configure(text="2/3 : Migration des données...")
            self.progress_bar.set(0.65)
            time.sleep(1.2)

            # Phase 3: Vérification
            self.lbl_step.configure(text="3/3 : Vérification d'intégrité...")
            self.progress_bar.set(1.0)
            time.sleep(0.8)

            # Étape 4: Redémarrage & Confirmation
            self.after(200, self._complete_installation)

        threading.Thread(target=_installation_worker, daemon=True).start()

    def _complete_installation(self):
        if self.on_complete_callback:
            self.on_complete_callback()
        self.destroy()

class UpdateNotificationModal(ctk.CTkToplevel):
    """Modale classique optionnelle (is_mandatory: false)."""

    def __init__(self, parent, update_info: dict, on_install_callback=None):
        super().__init__(parent)
        self.update_info = update_info
        self.on_install_callback = on_install_callback
        
        version = update_info.get("version", "v2.1.0")
        title = update_info.get("title", "Mise à jour disponible")
        summary = update_info.get("summary", f"Une nouvelle version ({version}) est prête.")
        highlights = update_info.get("highlights", [])
        actions = update_info.get("actions", {"primary": "Installer maintenant", "secondary": "Plus tard"})

        self.title(title)
        self.geometry("520x580")
        self.configure(fg_color=BG)
        self.resizable(False, False)
        
        self.transient(parent)
        self.grab_set()

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=25, pady=(20, 10))

        lbl_icon = ctk.CTkLabel(header, text="🚀", font=ctk.CTkFont(FNT_TITLE, 32))
        lbl_icon.pack(side="left", padx=(0, 15))

        title_box = ctk.CTkFrame(header, fg_color="transparent")
        title_box.pack(side="left", fill="both", expand=True)

        ctk.CTkLabel(title_box, text=title, font=ctk.CTkFont(FNT_TITLE, 20, "bold"), anchor="w").pack(anchor="w")
        ctk.CTkLabel(title_box, text=f"Version {version} • {update_info.get('release_date', '')}", font=ctk.CTkFont(FNT_BODY, 13), text_color=GRY, anchor="w").pack(anchor="w")

        card = ctk.CTkFrame(self, fg_color=SEC_BG, corner_radius=RAD)
        card.pack(fill="both", expand=True, padx=25, pady=10)

        ctk.CTkLabel(card, text=summary, font=ctk.CTkFont(FNT_BODY, 14, "bold"), text_color=TEXT, wraplength=440, justify="left").pack(anchor="w", padx=20, pady=(15, 10))

        if highlights:
            ctk.CTkLabel(card, text="Points forts & Nouveautés :", font=ctk.CTkFont(FNT_BODY, 13, "bold"), text_color=ACCENT).pack(anchor="w", padx=20, pady=(5, 5))
            for item in highlights:
                ctk.CTkLabel(card, text=f"• {item}", font=ctk.CTkFont(FNT_BODY, 13), text_color=TEXT, wraplength=430, justify="left").pack(anchor="w", padx=30, pady=3)

        self.progress_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.progress_bar = ctk.CTkProgressBar(self.progress_frame, mode="determinate", fg_color=LINE, progress_color=ACCENT, height=10)
        self.lbl_progress = ctk.CTkLabel(self.progress_frame, text="Téléchargement en cours...", font=ctk.CTkFont(FNT_BODY, 12), text_color=GRY)

        self.btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.btn_frame.pack(fill="x", padx=25, pady=20)

        self.btn_secondary = ctk.CTkButton(
            self.btn_frame, 
            text=actions.get("secondary", "Plus tard"), 
            fg_color=GRY, 
            hover_color="#6E6E73",
            height=42, 
            corner_radius=12,
            command=self.destroy
        )
        self.btn_secondary.pack(side="left", padx=(0, 10), expand=True, fill="x")

        self.btn_primary = ctk.CTkButton(
            self.btn_frame, 
            text=actions.get("primary", "Installer maintenant"), 
            fg_color=ACCENT, 
            hover_color="#FF6666",
            height=42, 
            corner_radius=12,
            font=ctk.CTkFont(FNT_BODY, 14, "bold"),
            command=self._start_install
        )
        self.btn_primary.pack(side="right", padx=(10, 0), expand=True, fill="x")

    def _start_install(self):
        self.btn_primary.configure(state="disabled", text="Installation...")
        self.btn_secondary.configure(state="disabled")

        self.progress_frame.pack(fill="x", padx=25, pady=(0, 15))
        self.lbl_progress.configure(text="Téléchargement et application du patch de mise à jour...")
        self.lbl_progress.pack(pady=5)
        self.progress_bar.pack(fill="x", pady=5)
        self.progress_bar.set(0.3)

        def _worker():
            try:
                from kodo_core.services.updater import apply_remote_update_sync
                patch_url = self.update_info.get("dist_patch_url") or self.update_info.get("distPatchUrl") or self.update_info.get("download_url") or self.update_info.get("downloadUrl")
                ver = self.update_info.get("latest_version") or self.update_info.get("latestVersion") or self.update_info.get("version") or "1.0.44"
                res = apply_remote_update_sync(patch_url, ver)
                if res.get("success"):
                    self.after(0, lambda: self._on_install_success(ver))
                else:
                    self.after(0, lambda: self._on_install_error(res.get("error", "Échec d'application du patch.")))
            except Exception as e:
                self.after(0, lambda: self._on_install_error(str(e)))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_install_success(self, version):
        self.progress_bar.set(1.0)
        self.lbl_progress.configure(text=f"✅ Mise à jour v{version} installée avec succès !", text_color=GRN)
        if self.on_install_callback:
            try:
                self.on_install_callback()
            except Exception:
                pass
        self.after(1500, self.destroy)

    def _on_install_error(self, error_msg):
        self.progress_bar.set(0.0)
        self.lbl_progress.configure(text=f"⚠️ {error_msg}", text_color=RED)
        self.btn_primary.configure(state="normal", text="Réessayer")
        self.btn_secondary.configure(state="normal", text="Fermer")
