"""
Modale de gestion des Catégories et Marques pré-enregistrées pour Kōdo POS Core.
"""
import customtkinter as ctk
import sqlite3
from views.modals.base import BG, SEC_BG, ACCENT, TEXT, GRY, LINE, RED, GRN, FNT_TITLE, FNT_BODY, RAD, ToastNotification
from database_manager import get_connection

CATEGORIES_PAR_DEFAUT = [
    "T-Shirts & Tops",
    "Pantalons & Jeans",
    "Robes & Jupes",
    "Vestes & Manteaux",
    "Chaussures",
    "Accessoires",
    "Sacs",
    "Bijoux",
    "Lingerie",
    "Costumes & Tailleurs"
]

MARQUES_PAR_DEFAUT = [
    "Hugo Boss",
    "Ralph Lauren",
    "Zara",
    "Nike",
    "Adidas",
    "Levi's",
    "Mango",
    "H&M",
    "Tommy Hilfiger",
    "Calvin Klein"
]

def get_prepopulated_categories():
    """Récupère la liste des catégories pré-enregistrées depuis la BDD."""
    conn = None
    try:
        conn = get_connection()
        c = conn.cursor()
        c.execute("SELECT nom FROM Categories ORDER BY nom ASC")
        rows = c.fetchall()
        cats = [r[0] for r in rows if r[0]]
        if not cats:
            for cat in CATEGORIES_PAR_DEFAUT:
                c.execute("INSERT OR IGNORE INTO Categories (nom) VALUES (?)", (cat,))
            conn.commit()
            cats = list(CATEGORIES_PAR_DEFAUT)
        return cats
    except Exception:
        return list(CATEGORIES_PAR_DEFAUT)
    finally:
        if conn:
            conn.close()

def get_prepopulated_marques():
    """Récupère la liste des marques pré-enregistrées depuis la BDD."""
    conn = None
    try:
        conn = get_connection()
        c = conn.cursor()
        c.execute("SELECT nom FROM Marques ORDER BY nom ASC")
        rows = c.fetchall()
        marques = [r[0] for r in rows if r[0]]
        if not marques:
            for m in MARQUES_PAR_DEFAUT:
                c.execute("INSERT OR IGNORE INTO Marques (nom) VALUES (?)", (m,))
            conn.commit()
            marques = list(MARQUES_PAR_DEFAUT)
        return marques
    except Exception:
        return list(MARQUES_PAR_DEFAUT)
    finally:
        if conn:
            conn.close()

class GestionCategoriesModal(ctk.CTkToplevel):
    """Modale permettant d'ajouter, modifier et supprimer des catégories pré-enregistrées."""

    def __init__(self, parent, on_change_callback=None):
        super().__init__(parent)
        self.parent = parent
        self.on_change_callback = on_change_callback

        self.title("Gestion des Catégories")
        self.geometry("520x640")
        self.configure(fg_color=BG)
        self.resizable(False, False)

        self.transient(parent)
        self.grab_set()

        # En-tête
        lbl_title = ctk.CTkLabel(
            self,
            text="🏷️ Catégories Pré-enregistrées",
            font=ctk.CTkFont(FNT_TITLE, 18, "bold"),
            text_color=TEXT
        )
        lbl_title.pack(pady=(18, 5))

        lbl_subtitle = ctk.CTkLabel(
            self,
            text="Gérez les catégories disponibles pour vos articles et filtres.",
            font=ctk.CTkFont(FNT_BODY, 12),
            text_color=GRY
        )
        lbl_subtitle.pack(pady=(0, 15))

        # Zone d'ajout de catégorie
        add_frame = ctk.CTkFrame(self, fg_color=SEC_BG, corner_radius=RAD)
        add_frame.pack(fill="x", padx=20, pady=(0, 15))

        lbl_add = ctk.CTkLabel(
            add_frame,
            text="Nouvelle Catégorie :",
            font=ctk.CTkFont(FNT_BODY, 12, "bold"),
            text_color=TEXT
        )
        lbl_add.pack(anchor="w", padx=15, pady=(10, 4))

        input_box = ctk.CTkFrame(add_frame, fg_color="transparent")
        input_box.pack(fill="x", padx=15, pady=(0, 12))

        self.entry_nouvelle_cat = ctk.CTkEntry(
            input_box,
            placeholder_text="ex: Sweats & Sweatshirts",
            height=38,
            corner_radius=10
        )
        self.entry_nouvelle_cat.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.entry_nouvelle_cat.bind("<Return>", lambda e: self._ajouter_categorie())

        btn_add = ctk.CTkButton(
            input_box,
            text="＋ Ajouter",
            width=110,
            height=38,
            fg_color=ACCENT,
            hover_color="#FF5555",
            font=ctk.CTkFont(FNT_BODY, 13, "bold"),
            corner_radius=10,
            command=self._ajouter_categorie
        )
        btn_add.pack(side="right")

        # Liste scrollable des catégories
        self.scroll_frame = ctk.CTkScrollableFrame(self, fg_color=SEC_BG, corner_radius=RAD)
        self.scroll_frame.pack(fill="both", expand=True, padx=20, pady=(0, 15))

        # Boutons du bas
        bottom_frame = ctk.CTkFrame(self, fg_color="transparent")
        bottom_frame.pack(fill="x", padx=20, pady=(0, 18))

        btn_default = ctk.CTkButton(
            bottom_frame,
            text="🔄 Restaurer par défaut",
            fg_color=SEC_BG,
            text_color=TEXT,
            hover_color="#E9ECEF",
            height=36,
            corner_radius=10,
            command=self._restaurer_defaut
        )
        btn_default.pack(side="left")

        btn_close = ctk.CTkButton(
            bottom_frame,
            text="Fermer",
            fg_color=GRY,
            width=100,
            height=36,
            corner_radius=10,
            command=self.destroy
        )
        btn_close.pack(side="right")

        # Charger la liste initiale
        self._refresh_list()

    def _refresh_list(self):
        """Recharge et affiche la liste des catégories."""
        for w in self.scroll_frame.winfo_children():
            w.destroy()

        cats = get_prepopulated_categories()

        if not cats:
            lbl_empty = ctk.CTkLabel(
                self.scroll_frame,
                text="Aucune catégorie enregistrée.",
                font=ctk.CTkFont(FNT_BODY, 13, "italic"),
                text_color=GRY
            )
            lbl_empty.pack(pady=30)
            return

        for cat_name in cats:
            card = ctk.CTkFrame(self.scroll_frame, fg_color=BG, corner_radius=10, height=44)
            card.pack(fill="x", padx=5, pady=4)
            card.pack_propagate(False)

            lbl_cat = ctk.CTkLabel(
                card,
                text=f"🏷️  {cat_name}",
                font=ctk.CTkFont(FNT_BODY, 13, "bold"),
                text_color=TEXT
            )
            lbl_cat.pack(side="left", padx=12)

            actions = ctk.CTkFrame(card, fg_color="transparent")
            actions.pack(side="right", padx=6)

            # Bouton Modifier
            btn_edit = ctk.CTkButton(
                actions,
                text="✏️",
                width=32,
                height=30,
                fg_color=SEC_BG,
                hover_color="#E2E8F0",
                text_color=TEXT,
                corner_radius=6,
                command=lambda c=cat_name: self._renommer_categorie(c)
            )
            btn_edit.pack(side="left", padx=2)

            # Bouton Supprimer
            btn_del = ctk.CTkButton(
                actions,
                text="🗑️",
                width=32,
                height=30,
                fg_color="#FFE5E5",
                hover_color="#FFC1C1",
                text_color=RED,
                corner_radius=6,
                command=lambda c=cat_name: self._supprimer_categorie(c)
            )
            btn_del.pack(side="left", padx=2)

        # Notifier les composants parents si nécessaire
        if hasattr(self.parent, "_refresh_categories_tabs"):
            try:
                self.parent._refresh_categories_tabs()
            except Exception:
                pass
        if self.on_change_callback:
            try:
                self.on_change_callback()
            except Exception:
                pass

    def _ajouter_categorie(self):
        nom = self.entry_nouvelle_cat.get().strip()
        if not nom:
            ToastNotification(self, "Veuillez saisir un nom de catégorie", type="error")
            return

        conn = None
        try:
            conn = get_connection()
            c = conn.cursor()
            c.execute("INSERT INTO Categories (nom) VALUES (?)", (nom,))
            conn.commit()
            self.entry_nouvelle_cat.delete(0, "end")
            ToastNotification(self, f"Catégorie '{nom}' ajoutée avec succès !", type="success")
            self._refresh_list()
        except sqlite3.IntegrityError:
            ToastNotification(self, f"La catégorie '{nom}' existe déjà", type="error")
        except Exception as e:
            ToastNotification(self, f"Erreur lors de l'ajout: {e}", type="error")
        finally:
            if conn:
                conn.close()

    def _renommer_categorie(self, ancien_nom):
        dialog = ctk.CTkInputDialog(
            text=f"Nouveau nom pour la catégorie '{ancien_nom}' :",
            title="Renommer la Catégorie"
        )
        dialog.geometry("400x200")
        nouveau_nom = dialog.get_input()

        if nouveau_nom is not None:
            nouveau_nom = nouveau_nom.strip()
            if not nouveau_nom or nouveau_nom == ancien_nom:
                return

            conn = None
            try:
                conn = get_connection()
                c = conn.cursor()
                c.execute("UPDATE Categories SET nom=? WHERE nom=?", (nouveau_nom, ancien_nom))
                c.execute("UPDATE Produits SET categorie=? WHERE categorie=?", (nouveau_nom, ancien_nom))
                conn.commit()
                ToastNotification(self, f"Catégorie renommée en '{nouveau_nom}'", type="success")
                self._refresh_list()
            except sqlite3.IntegrityError:
                ToastNotification(self, f"La catégorie '{nouveau_nom}' existe déjà", type="error")
            except Exception as e:
                ToastNotification(self, f"Erreur lors de la modification: {e}", type="error")
            finally:
                if conn:
                    conn.close()

    def _supprimer_categorie(self, nom):
        conn = None
        try:
            conn = get_connection()
            c = conn.cursor()

            c.execute("SELECT COUNT(*) FROM Produits WHERE categorie=?", (nom,))
            count_prods = c.fetchone()[0] or 0

            c.execute("DELETE FROM Categories WHERE nom=?", (nom,))
            if count_prods > 0:
                c.execute("UPDATE Produits SET categorie=NULL WHERE categorie=?", (nom,))
            conn.commit()

            msg = f"Catégorie '{nom}' supprimée."
            if count_prods > 0:
                msg += f" ({count_prods} article(s) remis sans catégorie)"

            ToastNotification(self, msg, type="success")
            self._refresh_list()
        except Exception as e:
            ToastNotification(self, f"Erreur lors de la suppression: {e}", type="error")
        finally:
            if conn:
                conn.close()

    def _restaurer_defaut(self):
        conn = None
        try:
            conn = get_connection()
            c = conn.cursor()
            for cat in CATEGORIES_PAR_DEFAUT:
                c.execute("INSERT OR IGNORE INTO Categories (nom) VALUES (?)", (cat,))
            conn.commit()
            ToastNotification(self, "Catégories par défaut restaurées !", type="success")
            self._refresh_list()
        except Exception as e:
            ToastNotification(self, f"Erreur lors de la restauration: {e}", type="error")
        finally:
            if conn:
                conn.close()

class GestionMarquesModal(ctk.CTkToplevel):
    """Modale permettant d'ajouter, modifier et supprimer des marques pré-enregistrées."""

    def __init__(self, parent, on_change_callback=None):
        super().__init__(parent)
        self.parent = parent
        self.on_change_callback = on_change_callback

        self.title("Gestion des Marques")
        self.geometry("520x640")
        self.configure(fg_color=BG)
        self.resizable(False, False)

        self.transient(parent)
        self.grab_set()

        # En-tête
        lbl_title = ctk.CTkLabel(
            self,
            text="✨ Marques Pré-enregistrées",
            font=ctk.CTkFont(FNT_TITLE, 18, "bold"),
            text_color=TEXT
        )
        lbl_title.pack(pady=(18, 5))

        lbl_subtitle = ctk.CTkLabel(
            self,
            text="Gérez la liste des marques pré-définies pour vos produits.",
            font=ctk.CTkFont(FNT_BODY, 12),
            text_color=GRY
        )
        lbl_subtitle.pack(pady=(0, 15))

        # Zone d'ajout de marque
        add_frame = ctk.CTkFrame(self, fg_color=SEC_BG, corner_radius=RAD)
        add_frame.pack(fill="x", padx=20, pady=(0, 15))

        lbl_add = ctk.CTkLabel(
            add_frame,
            text="Nouvelle Marque :",
            font=ctk.CTkFont(FNT_BODY, 12, "bold"),
            text_color=TEXT
        )
        lbl_add.pack(anchor="w", padx=15, pady=(10, 4))

        input_box = ctk.CTkFrame(add_frame, fg_color="transparent")
        input_box.pack(fill="x", padx=15, pady=(0, 12))

        self.entry_nouvelle_marque = ctk.CTkEntry(
            input_box,
            placeholder_text="ex: Lacoste, Calvin Klein...",
            height=38,
            corner_radius=10
        )
        self.entry_nouvelle_marque.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.entry_nouvelle_marque.bind("<Return>", lambda e: self._ajouter_marque())

        btn_add = ctk.CTkButton(
            input_box,
            text="＋ Ajouter",
            width=110,
            height=38,
            fg_color=ACCENT,
            hover_color="#FF5555",
            font=ctk.CTkFont(FNT_BODY, 13, "bold"),
            corner_radius=10,
            command=self._ajouter_marque
        )
        btn_add.pack(side="right")

        # Liste scrollable des marques
        self.scroll_frame = ctk.CTkScrollableFrame(self, fg_color=SEC_BG, corner_radius=RAD)
        self.scroll_frame.pack(fill="both", expand=True, padx=20, pady=(0, 15))

        # Boutons du bas
        bottom_frame = ctk.CTkFrame(self, fg_color="transparent")
        bottom_frame.pack(fill="x", padx=20, pady=(0, 18))

        btn_default = ctk.CTkButton(
            bottom_frame,
            text="🔄 Restaurer par défaut",
            fg_color=SEC_BG,
            text_color=TEXT,
            hover_color="#E9ECEF",
            height=36,
            corner_radius=10,
            command=self._restaurer_defaut
        )
        btn_default.pack(side="left")

        btn_close = ctk.CTkButton(
            bottom_frame,
            text="Fermer",
            fg_color=GRY,
            width=100,
            height=36,
            corner_radius=10,
            command=self.destroy
        )
        btn_close.pack(side="right")

        # Charger la liste initiale
        self._refresh_list()

    def _refresh_list(self):
        """Recharge et affiche la liste des marques."""
        for w in self.scroll_frame.winfo_children():
            w.destroy()

        marques = get_prepopulated_marques()

        if not marques:
            lbl_empty = ctk.CTkLabel(
                self.scroll_frame,
                text="Aucune marque enregistrée.",
                font=ctk.CTkFont(FNT_BODY, 13, "italic"),
                text_color=GRY
            )
            lbl_empty.pack(pady=30)
            return

        for m_name in marques:
            card = ctk.CTkFrame(self.scroll_frame, fg_color=BG, corner_radius=10, height=44)
            card.pack(fill="x", padx=5, pady=4)
            card.pack_propagate(False)

            lbl_m = ctk.CTkLabel(
                card,
                text=f"✨  {m_name}",
                font=ctk.CTkFont(FNT_BODY, 13, "bold"),
                text_color=TEXT
            )
            lbl_m.pack(side="left", padx=12)

            actions = ctk.CTkFrame(card, fg_color="transparent")
            actions.pack(side="right", padx=6)

            # Bouton Modifier
            btn_edit = ctk.CTkButton(
                actions,
                text="✏️",
                width=32,
                height=30,
                fg_color=SEC_BG,
                hover_color="#E2E8F0",
                text_color=TEXT,
                corner_radius=6,
                command=lambda m=m_name: self._renommer_marque(m)
            )
            btn_edit.pack(side="left", padx=2)

            # Bouton Supprimer
            btn_del = ctk.CTkButton(
                actions,
                text="🗑️",
                width=32,
                height=30,
                fg_color="#FFE5E5",
                hover_color="#FFC1C1",
                text_color=RED,
                corner_radius=6,
                command=lambda m=m_name: self._supprimer_marque(m)
            )
            btn_del.pack(side="left", padx=2)

        if self.on_change_callback:
            try:
                self.on_change_callback()
            except Exception:
                pass

    def _ajouter_marque(self):
        nom = self.entry_nouvelle_marque.get().strip()
        if not nom:
            ToastNotification(self, "Veuillez saisir un nom de marque", type="error")
            return

        conn = None
        try:
            conn = get_connection()
            c = conn.cursor()
            c.execute("INSERT INTO Marques (nom) VALUES (?)", (nom,))
            conn.commit()
            self.entry_nouvelle_marque.delete(0, "end")
            ToastNotification(self, f"Marque '{nom}' ajoutée avec succès !", type="success")
            self._refresh_list()
        except sqlite3.IntegrityError:
            ToastNotification(self, f"La marque '{nom}' existe déjà", type="error")
        except Exception as e:
            ToastNotification(self, f"Erreur lors de l'ajout: {e}", type="error")
        finally:
            if conn:
                conn.close()

    def _renommer_marque(self, ancien_nom):
        dialog = ctk.CTkInputDialog(
            text=f"Nouveau nom pour la marque '{ancien_nom}' :",
            title="Renommer la Marque"
        )
        dialog.geometry("400x200")
        nouveau_nom = dialog.get_input()

        if nouveau_nom is not None:
            nouveau_nom = nouveau_nom.strip()
            if not nouveau_nom or nouveau_nom == ancien_nom:
                return

            conn = None
            try:
                conn = get_connection()
                c = conn.cursor()
                c.execute("UPDATE Marques SET nom=? WHERE nom=?", (nouveau_nom, ancien_nom))
                c.execute("UPDATE Produits SET marque=? WHERE marque=?", (nouveau_nom, ancien_nom))
                conn.commit()
                ToastNotification(self, f"Marque renommée en '{nouveau_nom}'", type="success")
                self._refresh_list()
            except sqlite3.IntegrityError:
                ToastNotification(self, f"La marque '{nouveau_nom}' existe déjà", type="error")
            except Exception as e:
                ToastNotification(self, f"Erreur lors de la modification: {e}", type="error")
            finally:
                if conn:
                    conn.close()

    def _supprimer_marque(self, nom):
        conn = None
        try:
            conn = get_connection()
            c = conn.cursor()

            c.execute("SELECT COUNT(*) FROM Produits WHERE marque=?", (nom,))
            count_prods = c.fetchone()[0] or 0

            c.execute("DELETE FROM Marques WHERE nom=?", (nom,))
            if count_prods > 0:
                c.execute("UPDATE Produits SET marque=NULL WHERE marque=?", (nom,))
            conn.commit()

            msg = f"Marque '{nom}' supprimée."
            if count_prods > 0:
                msg += f" ({count_prods} article(s) remis sans marque)"

            ToastNotification(self, msg, type="success")
            self._refresh_list()
        except Exception as e:
            ToastNotification(self, f"Erreur lors de la suppression: {e}", type="error")
        finally:
            if conn:
                conn.close()

    def _restaurer_defaut(self):
        conn = None
        try:
            conn = get_connection()
            c = conn.cursor()
            for m in MARQUES_PAR_DEFAUT:
                c.execute("INSERT OR IGNORE INTO Marques (nom) VALUES (?)", (m,))
            conn.commit()
            ToastNotification(self, "Marques par défaut restaurées !", type="success")
            self._refresh_list()
        except Exception as e:
            ToastNotification(self, f"Erreur lors de la restauration: {e}", type="error")
        finally:
            if conn:
                conn.close()
