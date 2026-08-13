"""
Modales de gestion des Produits, Stocks et Variantes (Prêt-à-porter & Multi-commerce).
"""
import customtkinter as ctk
import sqlite3
from views.modals.base import BG, SEC_BG, ACCENT, TEXT, GRY, LINE, RED, GRN, FNT_TITLE, FNT_BODY, RAD, ToastNotification
from database_manager import get_connection

import os
import random
from PIL import Image
import barcode
from barcode.writer import ImageWriter

from views.modals.category import get_prepopulated_categories, get_prepopulated_marques, GestionCategoriesModal, GestionMarquesModal

def generate_ean13_code():
    """Génère un code EAN-13 valide à 13 chiffres avec clé de contrôle."""
    prefix = "200"
    middle = "".join([str(random.randint(0, 9)) for _ in range(9)])
    raw = prefix + middle
    checksum = 0
    for i, digit in enumerate(raw):
        val = int(digit)
        checksum += val * 3 if i % 2 == 1 else val
    check_digit = (10 - (checksum % 10)) % 10
    return raw + str(check_digit)

# Tailles par défaut (Prêt-à-porter pur, du XXS au XXL + Taille Unique)
TAILLES_PRET_A_PORTER = ["XXS", "XS", "S", "M", "L", "XL", "XXL", "Taille Unique"]
COULEURS_PRET_A_PORTER = ["Noir", "Blanc", "Bleu Marine", "Gris", "Beige", "Kaki", "Marron", "Rouge", "Vert", "Bleu Ciel"]

class ProductEditModal(ctk.CTkToplevel):
    """Modale de création et de modification d'articles avec gestion des tailles dynamiques (XXS à XXL + Personnalisées)."""
    
    def __init__(self, parent, product_id=None, on_save_callback=None, callback=None):
        super().__init__(parent)
        self.product_id = product_id
        self.on_save_callback = on_save_callback or callback
        
        self.title("Éditer l'Article" if product_id else "Nouveau Produit")
        self.geometry("680x800")
        self.configure(fg_color=BG)
        self.resizable(False, False)
        
        self.parent = parent
        if hasattr(parent, "active_modal"):
            parent.active_modal = self

        self.transient(parent)
        self.grab_set()

        # Liste active des tailles gérées pour cet article
        self.active_sizes = list(TAILLES_PRET_A_PORTER)
        self.size_entries = {}
        self.size_widgets = {}

        # En-tête
        self.lbl_title = ctk.CTkLabel(
            self, 
            text="Nouveau Produit Prêt-à-porter" if not product_id else "Modification Produit", 
            font=ctk.CTkFont(FNT_TITLE, 20, "bold")
        )
        self.lbl_title.pack(pady=15)

        # Formulaire
        self.form_frame = ctk.CTkScrollableFrame(self, fg_color=SEC_BG, corner_radius=RAD)
        self.form_frame.pack(fill="both", expand=True, padx=20, pady=10)

        # Champs principaux
        self.entry_nom = self._create_field("Nom du produit :", "ex: Chemise Coton Premium")
        self._create_barcode_field()
        self._create_brand_field()
        self._create_category_field()
        self.entry_prix_ht = self._create_field("Prix Achat HTVA (€) :", "0.00")
        self.entry_prix_ttc = self._create_field("Prix Vente TVAC (€) :", "0.00")

        # Section Article en Solde
        solde_frame = ctk.CTkFrame(self.form_frame, fg_color="transparent")
        solde_frame.pack(fill="x", padx=15, pady=(8, 2))
        
        self.var_en_solde = ctk.BooleanVar(value=False)
        self.chk_en_solde = ctk.CTkCheckBox(
            solde_frame, 
            text="🏷️ Article en solde", 
            variable=self.var_en_solde, 
            font=ctk.CTkFont(FNT_BODY, 13, "bold"),
            text_color=RED,
            command=self._toggle_solde_fields
        )
        self.chk_en_solde.pack(side="left", anchor="w")

        # Container champ prix solde et boutons d'aide
        self.solde_input_box = ctk.CTkFrame(self.form_frame, fg_color="transparent")
        self.solde_input_box.pack(fill="x", padx=15, pady=(2, 5))
        
        self.entry_prix_solde = ctk.CTkEntry(
            self.solde_input_box, 
            placeholder_text="Prix Solde TVAC (ex: 15.00)", 
            height=36, 
            corner_radius=10
        )
        self.entry_prix_solde.pack(side="left", fill="x", expand=True, padx=(0, 8))
        
        # Boutons de calcul rapide de solde (-20%, -30%, -50%)
        for pct in [20, 30, 50]:
            btn_pct = ctk.CTkButton(
                self.solde_input_box,
                text=f"-{pct}%",
                width=50,
                height=34,
                fg_color=SEC_BG,
                text_color=RED,
                hover_color="#FFE5E5",
                corner_radius=8,
                command=lambda p=pct: self._calculer_prix_solde_pct(p)
            )
            btn_pct.pack(side="left", padx=2)

        # Section Variantes & Tailles
        self.lbl_vars = ctk.CTkLabel(self.form_frame, text="Déclinaisons de Tailles & Stocks :", font=ctk.CTkFont(FNT_TITLE, 14, "bold"))
        self.lbl_vars.pack(anchor="w", padx=15, pady=(15, 5))

        # Zone d'ajout de taille sur mesure
        add_size_frame = ctk.CTkFrame(self.form_frame, fg_color="transparent")
        add_size_frame.pack(fill="x", padx=15, pady=(0, 10))

        self.entry_new_size = ctk.CTkEntry(add_size_frame, placeholder_text="Ajouter une taille (ex: 38, 3XL, S/M)", height=32, corner_radius=8)
        self.entry_new_size.pack(side="left", fill="x", expand=True, padx=(0, 10))

        self.btn_add_size = ctk.CTkButton(add_size_frame, text="＋ Ajouter Taille", width=120, height=32, fg_color=ACCENT, command=self._add_custom_size)
        self.btn_add_size.pack(side="right")

        # Grille des tailles dynamiques
        self.size_grid = ctk.CTkFrame(self.form_frame, fg_color="transparent")
        self.size_grid.pack(fill="x", padx=15, pady=5)

        # Chargement initial des données si modification
        if product_id:
            self._load_product_data()
        else:
            self._render_size_grid()

        # Boutons d'action
        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(fill="x", padx=20, pady=15)

        self.btn_cancel = ctk.CTkButton(btn_frame, text="Annuler", fg_color=GRY, command=self.destroy)
        self.btn_cancel.pack(side="left", padx=10)

        self.btn_save = ctk.CTkButton(btn_frame, text="Enregistrer l'Article", fg_color=ACCENT, command=self._save_product)
    def _create_field(self, label_text, placeholder):
        lbl = ctk.CTkLabel(self.form_frame, text=label_text, font=ctk.CTkFont(FNT_BODY, 13, "bold"))
        lbl.pack(anchor="w", padx=15, pady=(8, 2))
        entry = ctk.CTkEntry(self.form_frame, placeholder_text=placeholder, height=36, corner_radius=10)
        entry.pack(fill="x", padx=15, pady=(0, 5))
        return entry

    def _create_barcode_field(self):
        lbl = ctk.CTkLabel(self.form_frame, text="Code-barres :", font=ctk.CTkFont(FNT_BODY, 13, "bold"))
        lbl.pack(anchor="w", padx=15, pady=(8, 2))
        
        box = ctk.CTkFrame(self.form_frame, fg_color="transparent")
        box.pack(fill="x", padx=15, pady=(0, 5))
        
        self.entry_barcode = ctk.CTkEntry(box, placeholder_text="ex: 200000000123", height=36, corner_radius=10)
        self.entry_barcode.pack(side="left", fill="x", expand=True, padx=(0, 8))
        
        btn_gen = ctk.CTkButton(
            box, 
            text="⚡ Générer EAN", 
            width=115, 
            height=36, 
            fg_color=SEC_BG, 
            text_color=TEXT, 
            hover_color="#E2E8F0", 
            corner_radius=10,
            command=self._generate_auto_barcode
        )
        btn_gen.pack(side="right")

    def _generate_auto_barcode(self):
        code = generate_ean13_code()
        self.entry_barcode.delete(0, "end")
        self.entry_barcode.insert(0, code)
        ToastNotification(self, f"Code-barres EAN-13 généré : {code}", type="success")

    def _create_brand_field(self):
        lbl = ctk.CTkLabel(self.form_frame, text="Marque / Enseigne :", font=ctk.CTkFont(FNT_BODY, 13, "bold"))
        lbl.pack(anchor="w", padx=15, pady=(8, 2))
        
        brand_frame = ctk.CTkFrame(self.form_frame, fg_color="transparent")
        brand_frame.pack(fill="x", padx=15, pady=(0, 5))
        
        marques = get_prepopulated_marques()
        self.entry_marque = ctk.CTkComboBox(
            brand_frame, 
            values=marques if marques else ["Generique"], 
            height=36, 
            corner_radius=10
        )
        self.entry_marque.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.entry_marque.set("")
        
        btn_manage_m = ctk.CTkButton(
            brand_frame, 
            text="⚙️ Gérer", 
            width=90, 
            height=36, 
            fg_color=SEC_BG, 
            text_color=TEXT, 
            hover_color="#E2E8F0", 
            corner_radius=10,
            command=self._open_manage_marques
        )
        btn_manage_m.pack(side="right")

    def _open_manage_marques(self):
        def on_marques_changed():
            updated = get_prepopulated_marques()
            self.entry_marque.configure(values=updated)
        GestionMarquesModal(self, on_change_callback=on_marques_changed)

    def _create_category_field(self):
        lbl = ctk.CTkLabel(self.form_frame, text="Catégorie :", font=ctk.CTkFont(FNT_BODY, 13, "bold"))
        lbl.pack(anchor="w", padx=15, pady=(8, 2))
        
        cat_frame = ctk.CTkFrame(self.form_frame, fg_color="transparent")
        cat_frame.pack(fill="x", padx=15, pady=(0, 5))
        
        cats = get_prepopulated_categories()
        self.entry_categorie = ctk.CTkComboBox(
            cat_frame, 
            values=cats if cats else ["Toutes"], 
            height=36, 
            corner_radius=10
        )
        self.entry_categorie.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.entry_categorie.set("")
        
        btn_manage_c = ctk.CTkButton(
            cat_frame, 
            text="⚙️ Gérer", 
            width=90, 
            height=36, 
            fg_color=SEC_BG, 
            text_color=TEXT, 
            hover_color="#E2E8F0", 
            corner_radius=10,
            command=self._open_manage_categories
        )
        btn_manage_c.pack(side="right")

    def _open_manage_categories(self):
        def on_cats_changed():
            updated = get_prepopulated_categories()
            self.entry_categorie.configure(values=updated)
            if hasattr(self.master, "_refresh_categories_tabs"):
                try: self.master._refresh_categories_tabs()
                except: pass
        GestionCategoriesModal(self, on_change_callback=on_cats_changed)

    def _render_size_grid(self, preserved_values=None):
        # Conserver les valeurs déjà saisies dans les champs
        current_vals = preserved_values or {}
        for taille, entry in list(self.size_entries.items()):
            if taille not in current_vals and entry.winfo_exists():
                current_vals[taille] = entry.get().strip()

        # Vider la grille visuelle
        for w in self.size_grid.winfo_children():
            w.destroy()

        self.size_entries.clear()

        for idx, taille in enumerate(self.active_sizes):
            col = idx % 3
            row = idx // 3

            cell = ctk.CTkFrame(self.size_grid, fg_color=BG, corner_radius=8)
            cell.grid(row=row, column=col, padx=4, pady=4, sticky="ew")

            lbl = ctk.CTkLabel(cell, text=f"{taille}:", width=55, font=ctk.CTkFont(FNT_BODY, 12, "bold"))
            lbl.pack(side="left", padx=(6, 2))

            entry = ctk.CTkEntry(cell, width=50, height=28, placeholder_text="0")
            entry.pack(side="left", padx=2)
            val = current_vals.get(taille, "")
            if val:
                entry.insert(0, val)
            self.size_entries[taille] = entry

            btn_del = ctk.CTkButton(
                cell, 
                text="×", 
                width=22, 
                height=22, 
                fg_color=RED, 
                hover_color="#B71C1C",
                command=lambda t=taille: self._remove_size(t)
            )
            btn_del.pack(side="right", padx=(2, 4))

    def _add_custom_size(self):
        new_size = self.entry_new_size.get().strip()
        if not new_size:
            return
        if new_size not in self.active_sizes:
            self.active_sizes.append(new_size)
            self.entry_new_size.delete(0, "end")
            self._render_size_grid()
        else:
            ToastNotification(self, f"La taille {new_size} existe déjà", type="error")

    def _remove_size(self, taille):
        if taille in self.active_sizes:
            self.active_sizes.remove(taille)
            self._render_size_grid()

    def _toggle_solde_fields(self):
        is_solde = self.var_en_solde.get()
        if is_solde:
            self.entry_prix_solde.configure(state="normal")
        else:
            self.entry_prix_solde.configure(state="normal")
            self.entry_prix_solde.delete(0, "end")

    def _calculer_prix_solde_pct(self, pct):
        try:
            prix_ttc_val = float(self.entry_prix_ttc.get().replace(",", ".") or 0)
            if prix_ttc_val > 0:
                prix_solde = round(prix_ttc_val * (1.0 - (pct / 100.0)), 2)
                self.var_en_solde.set(True)
                self.entry_prix_solde.configure(state="normal")
                self.entry_prix_solde.delete(0, "end")
                self.entry_prix_solde.insert(0, f"{prix_solde:.2f}")
        except ValueError:
            pass

    def _load_product_data(self):
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT nom, code_barre, categorie, prix_achat_htva, prix_vente_tvac, marque, en_solde, prix_solde_tvac FROM Produits WHERE id=?", (self.product_id,))
        row = cursor.fetchone()
        if row:
            self.entry_nom.delete(0, "end")
            self.entry_nom.insert(0, row[0] or "")
            self.entry_barcode.delete(0, "end")
            self.entry_barcode.insert(0, row[1] or "")
            self.entry_categorie.set(row[2] or "")
            self.entry_prix_ht.delete(0, "end")
            self.entry_prix_ht.insert(0, str(row[3] or ""))
            self.entry_prix_ttc.delete(0, "end")
            self.entry_prix_ttc.insert(0, str(row[4] or ""))
            if len(row) > 5 and row[5]:
                self.entry_marque.set(row[5] or "")
            if len(row) > 6 and row[6] == 1:
                self.var_en_solde.set(True)
            else:
                self.var_en_solde.set(False)
            if len(row) > 7 and row[7] is not None:
                self.entry_prix_solde.delete(0, "end")
                self.entry_prix_solde.insert(0, str(row[7]))

        # Chargement des quantités existantes en stock par taille
        cursor.execute("SELECT taille, quantite_actuelle FROM Stocks WHERE id_produit=?", (self.product_id,))
        stocks = cursor.fetchall()
        preserved_vals = {}
        for taille, qty in stocks:
            if taille and taille not in self.active_sizes:
                self.active_sizes.append(taille)
            preserved_vals[taille] = str(qty)

        conn.close()
        self._render_size_grid(preserved_vals)

    def _save_product(self):
        nom = self.entry_nom.get().strip()
        barcode_raw = self.entry_barcode.get().strip()
        cat_raw = self.entry_categorie.get().strip()
        marque_raw = self.entry_marque.get().strip()

        barcode_val = barcode_raw if barcode_raw else None
        cat_val = cat_raw if cat_raw else None
        marque_val = marque_raw if marque_raw else None

        try:
            prix_ht = float(self.entry_prix_ht.get().replace(",", ".") or 0)
            prix_ttc = float(self.entry_prix_ttc.get().replace(",", ".") or 0)
        except ValueError:
            ToastNotification(self, "Les prix doivent être des valeurs numériques (ex: 19.90)", type="error")
            return

        en_solde_val = 1 if self.var_en_solde.get() else 0
        prix_solde_raw = self.entry_prix_solde.get().strip().replace(",", ".")
        prix_solde_val = None
        if en_solde_val == 1 and prix_solde_raw:
            try:
                prix_solde_val = float(prix_solde_raw)
            except ValueError:
                ToastNotification(self, "Le prix solde doit être une valeur numérique (ex: 15.00)", type="error")
                return

        if not nom:
            ToastNotification(self, "Le nom du produit est obligatoire", type="error")
            return

        conn = None
        try:
            conn = get_connection()
            cursor = conn.cursor()

            # Enregistrer la catégorie et la marque dans les tables pré-enregistrées
            if cat_val:
                cursor.execute("INSERT OR IGNORE INTO Categories (nom) VALUES (?)", (cat_val,))
            if marque_val:
                cursor.execute("INSERT OR IGNORE INTO Marques (nom) VALUES (?)", (marque_val,))

            if self.product_id:
                cursor.execute("""
                    UPDATE Produits 
                    SET nom=?, code_barre=?, categorie=?, prix_achat_htva=?, prix_vente_tvac=?, marque=?, en_solde=?, prix_solde_tvac=?
                    WHERE id=?
                """, (nom, barcode_val, cat_val, prix_ht, prix_ttc, marque_val, en_solde_val, prix_solde_val, self.product_id))
                prod_id = self.product_id
            else:
                cursor.execute("""
                    INSERT INTO Produits (nom, code_barre, categorie, prix_achat_htva, prix_vente_tvac, marque, en_solde, prix_solde_tvac)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (nom, barcode_val, cat_val, prix_ht, prix_ttc, marque_val, en_solde_val, prix_solde_val))
                prod_id = cursor.lastrowid

            # Mettre à jour les stocks pour chaque taille active
            active_set = set(self.active_sizes)
            for taille, entry in self.size_entries.items():
                qty_str = entry.get().strip()
                try:
                    qty = int(qty_str) if qty_str else 0
                except ValueError:
                    qty = 0

                cursor.execute("SELECT id FROM Stocks WHERE id_produit=? AND taille=?", (prod_id, taille))
                s_row = cursor.fetchone()
                if s_row:
                    cursor.execute("UPDATE Stocks SET quantite_actuelle=? WHERE id=?", (qty, s_row[0]))
                else:
                    cursor.execute("INSERT INTO Stocks (id_produit, taille, quantite_actuelle, seuil_alerte) VALUES (?, ?, ?, 2)", (prod_id, taille, qty))

            # Purger les stocks des tailles supprimées
            cursor.execute("SELECT id, taille FROM Stocks WHERE id_produit=?", (prod_id,))
            for sid, t in cursor.fetchall():
                if t not in active_set:
                    cursor.execute("DELETE FROM Stocks WHERE id=?", (sid,))

            conn.commit()

            ToastNotification(
                self, 
                f"Article '{nom}' {'modifié' if self.product_id else 'créé'} avec succès !", 
                type="success"
            )

        except sqlite3.IntegrityError as ie:
            if conn: conn.rollback()
            if "code_barre" in str(ie).lower():
                ToastNotification(self, f"Le code-barres '{barcode_raw}' est déjà utilisé par un autre article.", type="error")
            else:
                ToastNotification(self, f"Erreur d'intégrité BDD: {ie}", type="error")
            return
        except Exception as e:
            if conn: conn.rollback()
            ToastNotification(self, f"Erreur d'enregistrement: {e}", type="error")
            return
        finally:
            if conn:
                conn.close()

        # Rafraîchissement complet de l'application
        if self.on_save_callback:
            try: self.on_save_callback()
            except Exception: pass

        if hasattr(self.parent, "_refresh_categories_tabs"):
            try: self.parent._refresh_categories_tabs()
            except Exception: pass

        self.destroy()

    def on_barcode_scanned(self, code):
        """Réception automatique d'un scan de code-barres quand la modale produit est ouverte."""
        if hasattr(self, "entry_barcode") and self.entry_barcode.winfo_exists():
            self.entry_barcode.delete(0, "end")
            self.entry_barcode.insert(0, code)
            ToastNotification(self, f"Code-barres scanné : {code}", type="success")

    def destroy(self):
        if hasattr(self.master, "active_modal") and self.master.active_modal == self:
            self.master.active_modal = None
        super().destroy()


class TailleSelectionModal(ctk.CTkToplevel):
    """Modale de sélection de taille lors de l'ajout d'un article multi-tailles au panier."""
    
    def __init__(self, parent, nom_produit, stock_rows, on_select_callback):
        super().__init__(parent)
        self.on_select_callback = on_select_callback
        self.title("Choisir la Taille")
        self.geometry("450x380")
        self.configure(fg_color=BG)
        self.resizable(False, False)
        
        self.transient(parent)
        self.grab_set()

        lbl_title = ctk.CTkLabel(
            self, 
            text=f"Sélectionnez la taille pour :\n{nom_produit}", 
            font=ctk.CTkFont(FNT_TITLE, 16, "bold"),
            wraplength=400
        )
        lbl_title.pack(pady=(20, 15))

        scroll = ctk.CTkScrollableFrame(self, fg_color=SEC_BG, corner_radius=RAD)
        scroll.pack(fill="both", expand=True, padx=20, pady=10)

        for sid, taille, qte in stock_rows:
            lbl_t = taille if (taille and taille != "—") else "Taille Unique"
            state = "normal" if qte > 0 else "disabled"
            color = ACCENT if qte > 0 else GRY
            txt = f"{lbl_t}   •   {qte} en stock" if qte > 0 else f"{lbl_t}   •   RUPTURE"

            btn = ctk.CTkButton(
                scroll,
                text=txt,
                fg_color=color,
                state=state,
                height=42,
                font=ctk.CTkFont(FNT_BODY, 13, "bold"),
                command=lambda s=sid, t=taille, q=qte: self._select(s, t, q)
            )
            btn.pack(fill="x", pady=4, padx=5)

        btn_cancel = ctk.CTkButton(self, text="Annuler", fg_color=GRY, command=self.destroy)
        btn_cancel.pack(pady=(5, 15))

    def _select(self, sid, taille, qte):
        if self.on_select_callback:
            self.on_select_callback(sid, taille, qte)
        self.destroy()

ProductModal = ProductEditModal


class EtiquetteCodeBarreModal(ctk.CTkToplevel):
    """Modale de visualisation, de génération et d'impression des étiquettes code-barres."""

    def __init__(self, parent, initial_product_id=None):
        super().__init__(parent)
        self.parent = parent
        self.current_product_id = initial_product_id

        self.title("🏷️ Visualiseur & Impression d'Étiquettes Code-barres")
        self.geometry("600x700")
        self.configure(fg_color=BG)
        self.resizable(False, False)

        self.transient(parent)
        self.grab_set()

        # En-tête
        lbl_title = ctk.CTkLabel(
            self,
            text="🏷️ Étiquettes Code-barres Produit",
            font=ctk.CTkFont(FNT_TITLE, 18, "bold"),
            text_color=TEXT
        )
        lbl_title.pack(pady=(18, 5))

        lbl_subtitle = ctk.CTkLabel(
            self,
            text="Visualisez, générez et imprimez les étiquettes code-barres de votre catalogue.",
            font=ctk.CTkFont(FNT_BODY, 12),
            text_color=GRY
        )
        lbl_subtitle.pack(pady=(0, 15))

        # Sélecteur de produit
        select_frame = ctk.CTkFrame(self, fg_color=SEC_BG, corner_radius=RAD)
        select_frame.pack(fill="x", padx=20, pady=(0, 15))

        lbl_select = ctk.CTkLabel(
            select_frame,
            text="Sélectionner un Article :",
            font=ctk.CTkFont(FNT_BODY, 12, "bold"),
            text_color=TEXT
        )
        lbl_select.pack(anchor="w", padx=15, pady=(10, 4))

        self.products_map = {}
        products_list = self._load_products_list()

        self.combo_products = ctk.CTkComboBox(
            select_frame,
            values=list(self.products_map.keys()) if self.products_map else ["Aucun produit"],
            height=38,
            corner_radius=10,
            command=self._on_product_selected
        )
        self.combo_products.pack(fill="x", padx=15, pady=(0, 12))

        # Zone d'aperçu de l'étiquette
        self.preview_card = ctk.CTkFrame(self, fg_color=BG, corner_radius=16, border_width=2, border_color="#E5E5EA")
        self.preview_card.pack(fill="both", expand=True, padx=25, pady=(0, 15))

        self.lbl_prod_name = ctk.CTkLabel(self.preview_card, text="", font=ctk.CTkFont(FNT_TITLE, 16, "bold"), text_color=TEXT)
        self.lbl_prod_name.pack(pady=(15, 2))

        self.lbl_prod_details = ctk.CTkLabel(self.preview_card, text="", font=ctk.CTkFont(FNT_BODY, 12), text_color=GRY)
        self.lbl_prod_details.pack(pady=(0, 8))

        self.lbl_prod_price = ctk.CTkLabel(self.preview_card, text="", font=ctk.CTkFont(FNT_TITLE, 20, "bold"), text_color=ACCENT)
        self.lbl_prod_price.pack(pady=(0, 12))

        self.barcode_img_label = ctk.CTkLabel(self.preview_card, text="Chargement de l'aperçu...")
        self.barcode_img_label.pack(pady=10, expand=True)

        self.lbl_barcode_str = ctk.CTkLabel(self.preview_card, text="", font=ctk.CTkFont(FNT_BODY, 14, "bold"), text_color=TEXT)
        self.lbl_barcode_str.pack(pady=(0, 15))

        # Actions
        act_frame = ctk.CTkFrame(self, fg_color="transparent")
        act_frame.pack(fill="x", padx=20, pady=(0, 20))

        self.btn_gen_code = ctk.CTkButton(
            act_frame,
            text="⚡ Générer Code-barres",
            fg_color=SEC_BG,
            text_color=TEXT,
            hover_color="#E2E8F0",
            height=40,
            corner_radius=10,
            command=self._generate_code_for_current
        )
        self.btn_gen_code.pack(side="left", padx=(0, 8))

        self.btn_print = ctk.CTkButton(
            act_frame,
            text="🖨️ Imprimer Étiquette",
            fg_color=ACCENT,
            hover_color="#FF5555",
            font=ctk.CTkFont(FNT_BODY, 13, "bold"),
            height=40,
            corner_radius=10,
            command=self._print_label
        )
        self.btn_print.pack(side="left", expand=True, fill="x", padx=4)

        btn_close = ctk.CTkButton(
            act_frame,
            text="Fermer",
            fg_color=GRY,
            width=90,
            height=40,
            corner_radius=10,
            command=self.destroy
        )
        btn_close.pack(side="right", padx=(8, 0))

        # Sélectionner le produit initial s'il est fourni
        if self.current_product_id:
            for display_name, pid in self.products_map.items():
                if pid == self.current_product_id:
                    self.combo_products.set(display_name)
                    break
        self._update_preview()

    def _load_products_list(self):
        conn = None
        self.products_map.clear()
        try:
            conn = get_connection()
            c = conn.cursor()
            c.execute("SELECT id, nom, code_barre, marque FROM Produits ORDER BY nom ASC")
            for pid, nom, code, marque in c.fetchall():
                label = f"{nom}"
                if marque:
                    label += f" ({marque})"
                if code:
                    label += f" — [{code}]"
                else:
                    label += " — [SANS CODE-BARRES]"
                self.products_map[label] = pid
        except Exception:
            pass
        finally:
            if conn:
                conn.close()
        return list(self.products_map.keys())

    def _on_product_selected(self, choice):
        if choice in self.products_map:
            self.current_product_id = self.products_map[choice]
            self._update_preview()

    def _update_preview(self):
        if not self.current_product_id:
            self.lbl_prod_name.configure(text="Aucun produit sélectionné")
            self.lbl_prod_details.configure(text="")
            self.lbl_prod_price.configure(text="")
            self.barcode_img_label.configure(text="Veuillez choisir un article", image="")
            self.lbl_barcode_str.configure(text="")
            return

        conn = None
        try:
            conn = get_connection()
            c = conn.cursor()
            c.execute("SELECT nom, code_barre, categorie, marque, prix_vente_tvac FROM Produits WHERE id=?", (self.current_product_id,))
            row = c.fetchone()
            if not row:
                return

            nom, code, cat, marque, prix = row
            prix_str = f"{prix:.2f} €" if prix is not None else "0.00 €"
            details = []
            if marque: details.append(f"Marque : {marque}")
            if cat: details.append(f"Catégorie : {cat}")
            details_str = " | ".join(details)

            self.lbl_prod_name.configure(text=nom or "Produit sans nom")
            self.lbl_prod_details.configure(text=details_str)
            self.lbl_prod_price.configure(text=prix_str)

            if not code:
                self.lbl_barcode_str.configure(text="❌ Aucun code-barres rattaché", text_color=RED)
                self.barcode_img_label.configure(text="Cliquez sur '⚡ Générer Code-barres' ci-dessous", image="")
                return

            self.lbl_barcode_str.configure(text=f"Code : {code}", text_color=TEXT)

            # Génération visuelle du code-barres avec la lib `barcode` + PIL
            try:
                CODE_CLASS = barcode.get_barcode_class('code128')
                b_instance = CODE_CLASS(str(code), writer=ImageWriter())
                tmp_path = f"/tmp/barcode_preview_{code}"
                final_img_path = b_instance.save(tmp_path)

                pil_img = Image.open(final_img_path)
                w, h = pil_img.size
                ratio = 240.0 / float(w)
                new_h = int(float(h) * ratio)
                pil_img_resized = pil_img.resize((240, max(75, new_h)), Image.Resampling.LANCZOS)

                ctk_img = ctk.CTkImage(light_image=pil_img_resized, dark_image=pil_img_resized, size=(240, max(75, new_h)))
                self.barcode_img_label.configure(image=ctk_img, text="")
            except Exception as e:
                self.barcode_img_label.configure(text=f"Aperçu code-barres impossible : {e}", image="")

        except Exception as e:
            ToastNotification(self, f"Erreur de chargement: {e}", type="error")
        finally:
            if conn:
                conn.close()

    def _generate_code_for_current(self):
        if not self.current_product_id:
            ToastNotification(self, "Aucun produit sélectionné", type="error")
            return

        new_code = generate_ean13_code()
        conn = None
        try:
            conn = get_connection()
            c = conn.cursor()
            c.execute("UPDATE Produits SET code_barre=? WHERE id=?", (new_code, self.current_product_id))
            conn.commit()
            ToastNotification(self, f"Code-barres {new_code} généré !", type="success")
            
            self._load_products_list()
            self.combo_products.configure(values=list(self.products_map.keys()))
            for display_name, pid in self.products_map.items():
                if pid == self.current_product_id:
                    self.combo_products.set(display_name)
                    break
            self._update_preview()
            if hasattr(self.parent, "_refresh_stocks_table"):
                try: self.parent._refresh_stocks_table()
                except: pass
        except Exception as e:
            ToastNotification(self, f"Erreur de génération : {e}", type="error")
        finally:
            if conn:
                conn.close()

    def _print_label(self):
        if not self.current_product_id:
            ToastNotification(self, "Aucun produit sélectionné", type="error")
            return

        conn = get_connection()
        c = conn.cursor()
        c.execute("SELECT nom, code_barre, prix_vente_tvac FROM Produits WHERE id=?", (self.current_product_id,))
        row = c.fetchone()
        conn.close()

        if not row or not row[1]:
            ToastNotification(self, "Veuillez d'abord générer un code-barres pour cet article.", type="error")
            return

        nom, code, prix = row
        try:
            CODE_CLASS = barcode.get_barcode_class('code128')
            b_instance = CODE_CLASS(str(code), writer=ImageWriter())
            save_path = os.path.expanduser(f"~/Desktop/Etiquette_{code}")
            generated_file = b_instance.save(save_path)
            ToastNotification(self, f"Étiquette générée sur le bureau !", type="success")
            os.system(f"open '{generated_file}'")
        except Exception as e:
            ToastNotification(self, f"Erreur lors de l'impression : {e}", type="error")


class RechercheProduitCaisseModal(ctk.CTkToplevel):
    """Modale affichant plusieurs articles correspondants lors d'une recherche ambiguë en caisse."""

    def __init__(self, parent, matching_products, on_select_callback):
        super().__init__(parent)
        from decimal import Decimal
        self.on_select_callback = on_select_callback

        self.title("Sélectionner l'Article")
        self.geometry("540x580")
        self.configure(fg_color=BG)
        self.resizable(False, False)

        self.transient(parent)
        self.grab_set()

        lbl_title = ctk.CTkLabel(
            self,
            text="🔎 Plusieurs articles correspondent",
            font=ctk.CTkFont(FNT_TITLE, 18, "bold"),
            text_color=TEXT
        )
        lbl_title.pack(pady=(18, 5))

        lbl_sub = ctk.CTkLabel(
            self,
            text="Veuillez cliquer sur l'article que vous souhaitez ajouter au panier :",
            font=ctk.CTkFont(FNT_BODY, 12),
            text_color=GRY
        )
        lbl_sub.pack(pady=(0, 12))

        scroll = ctk.CTkScrollableFrame(self, fg_color=SEC_BG, corner_radius=RAD)
        scroll.pack(fill="both", expand=True, padx=20, pady=(0, 15))

        for row in matching_products:
            # row: pid, nom, prix_vente, tva, en_solde, prix_solde, code_barre, marque, categorie
            pid = row[0]
            nom = row[1]
            prix_v = row[2]
            tva = row[3]
            en_solde = row[4]
            prix_solde = row[5]
            code = row[6] if len(row) > 6 else ""
            marque = row[7] if len(row) > 7 else ""
            cat = row[8] if len(row) > 8 else ""

            if en_solde == 1 and prix_solde is not None:
                prix_effective = Decimal(str(prix_solde))
            else:
                prix_effective = Decimal(str(prix_v)) if prix_v is not None else Decimal("0.00")

            card = ctk.CTkFrame(scroll, fg_color=BG, corner_radius=12, height=60)
            card.pack(fill="x", padx=5, pady=4)
            card.pack_propagate(False)

            info_f = ctk.CTkFrame(card, fg_color="transparent")
            info_f.pack(side="left", fill="both", expand=True, padx=12, pady=6)

            lbl_n = ctk.CTkLabel(info_f, text=nom, font=ctk.CTkFont(FNT_BODY, 13, "bold"), text_color=TEXT, anchor="w")
            lbl_n.pack(fill="x")

            meta = []
            if marque: meta.append(f"Marque: {marque}")
            if cat: meta.append(f"Cat: {cat}")
            if code: meta.append(f"Code: {code}")
            lbl_m = ctk.CTkLabel(info_f, text=" • ".join(meta) if meta else "—", font=ctk.CTkFont(FNT_BODY, 11), text_color=GRY, anchor="w")
            lbl_m.pack(fill="x")

            lbl_p = ctk.CTkLabel(card, text=f"{prix_effective:.2f} €", font=ctk.CTkFont(FNT_BODY, 14, "bold"), text_color=ACCENT)
            lbl_p.pack(side="right", padx=12)

            def pick(p_row=row):
                self.destroy()
                if self.on_select_callback:
                    self.on_select_callback(p_row)

            card.bind("<Button-1>", lambda e, r=row: pick(r))
            for child in card.winfo_children():
                child.bind("<Button-1>", lambda e, r=row: pick(r))

        btn_cancel = ctk.CTkButton(self, text="Annuler", fg_color=GRY, height=36, corner_radius=10, command=self.destroy)
        btn_cancel.pack(pady=(0, 15))


class RemiseLigneModal(ctk.CTkToplevel):
    """Modale pour appliquer une remise sur un article individuel du panier."""
    def __init__(self, parent, item, on_apply_callback):
        super().__init__(parent)
        self.item = item
        self.on_apply_callback = on_apply_callback

        self.title("Remise sur Article")
        self.geometry("440x480")
        self.configure(fg_color=BG)
        self.resizable(False, False)

        self.transient(parent)
        self.grab_set()

        from decimal import Decimal

        nom = item.get("nom", "Article")
        self.prix_orig = Decimal(str(item.get("prix_original_tvac") or item.get("prix_vente_tvac", "0.00")))
        self.prix_actuel = Decimal(str(item.get("prix_vente_tvac", "0.00")))

        # Titre
        lbl_title = ctk.CTkLabel(self, text="🏷️ Remise sur l'Article", font=ctk.CTkFont(FNT_TITLE, 18, "bold"), text_color=TEXT)
        lbl_title.pack(pady=(18, 5))

        lbl_nom = ctk.CTkLabel(self, text=nom, font=ctk.CTkFont(FNT_BODY, 14, "bold"), text_color=ACCENT)
        lbl_nom.pack(pady=(0, 10))

        # En-tête des prix
        info_frame = ctk.CTkFrame(self, fg_color=SEC_BG, corner_radius=12)
        info_frame.pack(fill="x", padx=20, pady=5)

        lbl_p1 = ctk.CTkLabel(info_frame, text=f"Prix de base : {self.prix_orig:.2f} €", font=ctk.CTkFont(FNT_BODY, 12), text_color=GRY)
        lbl_p1.pack(pady=(8, 2))

        self.lbl_preview = ctk.CTkLabel(info_frame, text=f"Nouveau Prix : {self.prix_actuel:.2f} €", font=ctk.CTkFont(FNT_BODY, 16, "bold"), text_color=GRN)
        self.lbl_preview.pack(pady=(0, 8))

        # Boutons d'accès rapide en %
        pct_frame = ctk.CTkFrame(self, fg_color="transparent")
        pct_frame.pack(fill="x", padx=20, pady=10)

        ctk.CTkLabel(pct_frame, text="Remises rapides :", font=ctk.CTkFont(FNT_BODY, 12, "bold"), text_color=TEXT).pack(anchor="w", pady=(0, 4))
        btns_box = ctk.CTkFrame(pct_frame, fg_color="transparent")
        btns_box.pack(fill="x")

        for pct in [5, 10, 15, 20, 30, 50]:
            btn = ctk.CTkButton(
                btns_box,
                text=f"-{pct}%",
                width=55,
                height=32,
                fg_color=SEC_BG,
                text_color=TEXT,
                hover_color="#E2E8F0",
                corner_radius=8,
                command=lambda p=pct: self._apply_pct(p)
            )
            btn.pack(side="left", padx=2, expand=True)

        # Mode de saisie personnalisée
        custom_frame = ctk.CTkFrame(self, fg_color="transparent")
        custom_frame.pack(fill="x", padx=20, pady=10)

        # Entrée pour % personnalisé
        pct_row = ctk.CTkFrame(custom_frame, fg_color="transparent")
        pct_row.pack(fill="x", pady=3)
        ctk.CTkLabel(pct_row, text="Remise en % :", width=120, anchor="w", font=ctk.CTkFont(FNT_BODY, 12)).pack(side="left")
        self.entry_pct = ctk.CTkEntry(pct_row, placeholder_text="ex: 25", height=32, corner_radius=8)
        self.entry_pct.pack(side="left", fill="x", expand=True, padx=(0, 6))
        btn_calc_pct = ctk.CTkButton(pct_row, text="Appliquer %", width=90, height=32, fg_color=ACCENT, command=self._calc_custom_pct)
        btn_calc_pct.pack(side="right")

        # Entrée pour Montant fixe €
        val_row = ctk.CTkFrame(custom_frame, fg_color="transparent")
        val_row.pack(fill="x", pady=3)
        ctk.CTkLabel(val_row, text="Réduction en € :", width=120, anchor="w", font=ctk.CTkFont(FNT_BODY, 12)).pack(side="left")
        self.entry_val = ctk.CTkEntry(val_row, placeholder_text="ex: 5.00", height=32, corner_radius=8)
        self.entry_val.pack(side="left", fill="x", expand=True, padx=(0, 6))
        btn_calc_val = ctk.CTkButton(val_row, text="Appliquer €", width=90, height=32, fg_color=ACCENT, command=self._calc_custom_val)
        btn_calc_val.pack(side="right")

        # Result storage
        self.calculated_price = self.prix_actuel
        self.remise_label = item.get("remise_label", "")

        # Actions
        btn_box = ctk.CTkFrame(self, fg_color="transparent")
        btn_box.pack(fill="x", padx=20, pady=15)

        btn_reset = ctk.CTkButton(btn_box, text="Réinitialiser", fg_color=GRY, width=100, height=36, corner_radius=10, command=self._reset_remise)
        btn_reset.pack(side="left")

        btn_val = ctk.CTkButton(btn_box, text="Valider", fg_color=GRN, hover_color="#2E7D32", height=36, corner_radius=10, command=self._valider)
        btn_val.pack(side="right", fill="x", expand=True, padx=(10, 0))

    def _apply_pct(self, pct):
        from decimal import Decimal
        reduction = (self.prix_orig * Decimal(str(pct)) / Decimal("100")).quantize(Decimal("0.01"))
        new_p = max(Decimal("0.00"), self.prix_orig - reduction)
        self.calculated_price = new_p
        self.remise_label = f"-{pct}%"
        self.lbl_preview.configure(text=f"Nouveau Prix : {new_p:.2f} € (-{pct}%)")

    def _calc_custom_pct(self):
        from decimal import Decimal
        raw = self.entry_pct.get().strip().replace(",", ".")
        try:
            val = float(raw)
            if val < 0 or val > 100:
                return
            pct_dec = Decimal(str(val))
            reduction = (self.prix_orig * pct_dec / Decimal("100")).quantize(Decimal("0.01"))
            new_p = max(Decimal("0.00"), self.prix_orig - reduction)
            self.calculated_price = new_p
            self.remise_label = f"-{val:g}%"
            self.lbl_preview.configure(text=f"Nouveau Prix : {new_p:.2f} € (-{val:g}%)")
        except ValueError:
            pass

    def _calc_custom_val(self):
        from decimal import Decimal
        raw = self.entry_val.get().strip().replace(",", ".")
        try:
            val = float(raw)
            if val < 0:
                return
            val_dec = Decimal(str(val))
            new_p = max(Decimal("0.00"), self.prix_orig - val_dec)
            self.calculated_price = new_p
            self.remise_label = f"-{val_dec:.2f}€"
            self.lbl_preview.configure(text=f"Nouveau Prix : {new_p:.2f} € (-{val_dec:.2f}€)")
        except ValueError:
            pass

    def _reset_remise(self):
        self.calculated_price = self.prix_orig
        self.remise_label = ""
        self.lbl_preview.configure(text=f"Nouveau Prix : {self.prix_orig:.2f} € (Aucune)")

    def _valider(self):
        if self.on_apply_callback:
            self.on_apply_callback(self.calculated_price, self.prix_orig, self.remise_label)
        self.destroy()



