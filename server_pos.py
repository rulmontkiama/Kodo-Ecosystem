#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Kōdo POS - Python REST API & Static Web Server
Passerelle entre le backend Python (SQLite, CUPS, ESC/POS, Shopify)
et l'interface utilisateur React/Tailwind.
"""

import os
import sys
import json
import sqlite3
import datetime
from decimal import Decimal
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

# Ajout du dossier courant au path Python
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import database_manager
from database_manager import get_connection, initialiser_db, hash_pin
import export_manager
import pdf_generator
import ticket_printer

# Assurer l'initialisation de la base de données au démarrage
initialiser_db()

def get_dist_dir():
    candidates = [
        os.path.expanduser("~/Library/Caches/KodoPOS/dist"),
        os.path.expanduser("~/.kodo_pos/dist"),
        os.path.expanduser("~/Documents/Kodo_POS/dist"),
    ]
    for c in candidates:
        if os.path.exists(c) and os.path.exists(os.path.join(c, 'index.html')):
            return c
    if getattr(sys, 'frozen', False):
        return os.path.join(getattr(sys, '_MEIPASS', BASE_DIR), "dist")
    else:
        d = os.path.join(BASE_DIR, "dist")
        if not os.path.exists(d) or not os.path.exists(os.path.join(d, 'index.html')):
            d = os.path.expanduser("~/Desktop/kōdo-pos-3/dist")
        return d


class POSRequestHandler(BaseHTTPRequestHandler):
    """Gestionnaire de requêtes HTTP pour l'API REST de Kōdo POS."""

    def _set_cors_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')

    def _send_json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self._set_cors_headers()
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, message, code=400):
        self._send_json({"error": message}, code)

    def do_OPTIONS(self):
        self.send_response(200)
        self._set_cors_headers()
        self.end_headers()

    def _serve_static(self, path):
        dist_dir = get_dist_dir()
        if path == '/' or not path:
            file_path = os.path.join(dist_dir, 'index.html')
        else:
            rel_path = path.lstrip('/')
            file_path = os.path.abspath(os.path.join(dist_dir, rel_path))

        # Sécurité Anti-Path Traversal (Interdiction d'échapper de dist_dir)
        real_dist = os.path.abspath(dist_dir)
        if not file_path.startswith(real_dist) or not os.path.exists(file_path) or os.path.isdir(file_path):
            file_path = os.path.join(dist_dir, 'index.html')

        if not os.path.exists(file_path):
            self.send_response(200)
            self._set_cors_headers()
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(b"<h1>Kodo POS API Active</h1><p>Veuillez compiler le frontend React dans dist/.</p>")
            return

        # Content Types
        ext = os.path.splitext(file_path)[1].lower()
        content_types = {
            '.html': 'text/html; charset=utf-8',
            '.js': 'text/javascript; charset=utf-8',
            '.css': 'text/css; charset=utf-8',
            '.json': 'application/json; charset=utf-8',
            '.png': 'image/png',
            '.jpg': 'image/jpeg',
            '.svg': 'image/svg+xml',
            '.ico': 'image/x-icon',
            '.woff2': 'font/woff2'
        }
        ctype = content_types.get(ext, 'application/octet-stream')

        with open(file_path, 'rb') as f:
            content = f.read()

        self.send_response(200)
        self._set_cors_headers()
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        try:
            # 1. Health check & version
            if path == '/api/status':
                import services.update_checker as update_checker
                self._send_json({
                    "status": "online",
                    "app": "Kōdo POS Engine",
                    "version": update_checker.get_installed_version(),
                    "timestamp": datetime.datetime.now().isoformat()
                })

            elif path == '/api/version':
                import services.update_checker as update_checker
                self._send_json({
                    "version": update_checker.get_installed_version()
                })

            elif path == '/api/check-update':
                import services.update_checker as update_checker
                res = update_checker.check_for_updates_sync()
                self._send_json(res)

            elif path == '/api/license/status':
                import license_manager
                info = license_manager.get_license_info()
                self._send_json(info)

            # 2. Liste des produits
            elif path == '/api/products':
                conn = get_connection()
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT p.id, p.nom, p.categorie, p.code_barre, p.prix_vente_tvac, 
                           COALESCE(SUM(s.quantite_actuelle), 0) as stock, p.type_vente, p.marque, p.attributs_json
                    FROM Produits p
                    LEFT JOIN Stocks s ON p.id = s.id_produit
                    GROUP BY p.id
                    ORDER BY p.nom ASC
                """)
                rows = cursor.fetchall()
                products = []
                for r in rows:
                    products.append({
                        "id": str(r[0]),
                        "name": r[1],
                        "category": r[2] or "Général",
                        "barcode": r[3] or "",
                        "price": float(r[4]) if r[4] is not None else 0.0,
                        "stock": int(r[5]) if r[5] is not None else 0,
                        "type": "service" if r[6] == "service" else "product",
                        "brand": r[7] or "",
                        "sizes": r[8] or ""
                    })
                conn.close()
                self._send_json(products)

            # 3. Liste des catégories
            elif path == '/api/categories':
                conn = get_connection()
                cursor = conn.cursor()
                cursor.execute("SELECT nom FROM Categories ORDER BY nom ASC")
                cats = [row[0] for row in cursor.fetchall()]
                conn.close()
                self._send_json(cats)

            # 4. Liste des clients
            elif path == '/api/clients':
                conn = get_connection()
                cursor = conn.cursor()
                cursor.execute("PRAGMA table_info(Clients)")
                cols = [row[1] for row in cursor.fetchall()]
                has_phone = 'telephone' in cols
                
                if has_phone:
                    cursor.execute("SELECT id, nom, telephone, email, points_fidelite FROM Clients ORDER BY nom ASC")
                else:
                    cursor.execute("SELECT id, nom, email, points_fidelite FROM Clients ORDER BY nom ASC")
                
                rows = cursor.fetchall()
                clients = []
                for r in rows:
                    if has_phone:
                        clients.append({
                            "id": str(r[0]),
                            "name": r[1],
                            "phone": r[2] or "",
                            "email": r[3] or "",
                            "points": int(r[4]) if r[4] else 0
                        })
                    else:
                        clients.append({
                            "id": str(r[0]),
                            "name": r[1],
                            "phone": "",
                            "email": r[2] or "",
                            "points": int(r[3]) if r[3] else 0
                        })
                conn.close()
                self._send_json(clients)

            # 5. Historique des ventes / réimpression
            elif path == '/api/sales/history':
                conn = get_connection()
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT id, numero_ticket, total_tvac, total_htva, total_tva, methode_paiement, 
                           id_client, vendeur_nom, date_heure
                    FROM Tickets ORDER BY id DESC LIMIT 50
                """)
                rows = cursor.fetchall()
                history = []
                for r in rows:
                    ticket_id = r[0]
                    cursor.execute("""
                        SELECT COALESCE(p.nom, 'Article'), v.quantite, v.prix_unitaire_tvac 
                        FROM Ventes_Details v 
                        LEFT JOIN Produits p ON v.id_stock = p.id 
                        WHERE v.id_ticket=?
                    """, (ticket_id,))
                    items = [{"name": item[0], "qty": item[1], "price": float(item[2])} for item in cursor.fetchall()]
                    
                    history.append({
                        "id": str(r[0]),
                        "receiptNumber": r[1],
                        "totalTTC": float(r[2]),
                        "totalHT": float(r[3]),
                        "totalTVA": float(r[4]),
                        "paymentMethod": r[5],
                        "clientName": str(r[6]) if r[6] else "",
                        "cashierName": r[7] or "Admin",
                        "date": r[8],
                        "items": items
                    })
                conn.close()
                self._send_json(history)

            # 6. Tickets en attente
            elif path == '/api/held-tickets':
                paniers = database_manager.lister_paniers_en_attente()
                self._send_json(paniers)

            # 7. Utilisateurs (Vendeurs)
            elif path == '/api/users':
                conn = get_connection()
                cursor = conn.cursor()
                cursor.execute("SELECT id, nom, role_admin FROM Vendeurs ORDER BY nom ASC")
                rows = cursor.fetchall()
                users = []
                for r in rows:
                    role_str = 'Gérant' if r[2] == 1 else 'Caissier'
                    users.append({
                        "id": str(r[0]),
                        "name": r[1],
                        "role": role_str
                    })
                conn.close()
                self._send_json(users)

            # 8. Export PDF Comptable / Rapport Z
            elif path == '/api/export/pdf':
                rep_type = query.get('type', ['jour'])[0]
                rep_date = query.get('date', [datetime.datetime.now().strftime("%Y-%m-%d")])[0]
                tmp_pdf = f"/tmp/rapport_comptable_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
                pdf_generator.generer_rapport_pdf(rep_type, rep_date, tmp_pdf)
                
                with open(tmp_pdf, 'rb') as f:
                    pdf_bytes = f.read()
                
                try: os.remove(tmp_pdf)
                except Exception: pass
                
                self.send_response(200)
                self.send_header('Content-Type', 'application/pdf')
                self.send_header('Content-Disposition', f'attachment; filename="Rapport_Comptable_{rep_date}.pdf"')
                self.send_header('Content-Length', str(len(pdf_bytes)))
                self._set_cors_headers()
                self.end_headers()
                self.wfile.write(pdf_bytes)

            # 9. Export CSV Comptabilité Belge / WinBooks
            elif path == '/api/export/csv':
                csv_path = export_manager.export_comptable_belge()
                with open(csv_path, 'rb') as f:
                    csv_bytes = f.read()
                
                self.send_response(200)
                self.send_header('Content-Type', 'text/csv; charset=utf-8')
                self.send_header('Content-Disposition', 'attachment; filename="Export_Comptable_Belge.csv"')
                self.send_header('Content-Length', str(len(csv_bytes)))
                self._set_cors_headers()
                self.end_headers()
                self.wfile.write(csv_bytes)

            # 10. Fichiers Statiques / Frontend Web
            else:
                self._serve_static(path)

        except Exception as e:
            self._send_error(f"Erreur serveur: {str(e)}", 500)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get('Content-Length', 0))
        body_bytes = self.rfile.read(length) if length > 0 else b'{}'
        
        try:
            data = json.loads(body_bytes.decode('utf-8'))
        except Exception:
            data = {}

        try:
            # 1. Enregistrement d'une Vente (Encaissement)
            if path == '/api/sales':
                conn = get_connection()
                cursor = conn.cursor()
                
                num_ticket = database_manager.generer_numero_ticket(cursor)
                total_ttc = Decimal(str(data.get('totalTTC', 0)))
                total_ht = Decimal(str(data.get('totalHT', total_ttc / Decimal('1.20')))).quantize(Decimal('0.01'))
                total_tva = (total_ttc - total_ht).quantize(Decimal('0.01'))
                remise = Decimal(str(data.get('discountPercent', 0)))
                mode_paiement = data.get('paymentMethod', 'CB')
                id_client = data.get('clientId')
                rendu = Decimal(str(data.get('changeGiven', 0)))
                vendeur = data.get('cashierName', 'Admin')
                now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                panier = []
                for item in data.get('items', []):
                    prod = item.get('product', {})
                    panier.append({
                        "id": prod.get('id'),
                        "code_barre": prod.get('barcode', ''),
                        "nom": prod.get('name', 'Article'),
                        "quantite": item.get('quantity', 1),
                        "prix_tvac": Decimal(str(prod.get('price', 0))),
                        "taille": item.get('size', '')
                    })

                ticket_info = database_manager.enregistrer_vente(
                    cursor=cursor,
                    numero_ticket=num_ticket,
                    total_tvac=total_ttc,
                    total_htva=total_ht,
                    total_tva=total_tva,
                    remise=remise,
                    methode_paiement=mode_paiement,
                    id_client=id_client,
                    rendu_monnaie=rendu,
                    panier=panier,
                    vendeur_nom=vendeur,
                    date_heure=now_str,
                    paiements=[(mode_paiement, float(total_ttc))]
                )
                conn.commit()
                conn.close()

                # Impression automatique optionnelle
                if data.get('printReceipt', False):
                    try:
                        ticket_printer.imprimer_ticket_caisse(num_ticket)
                    except Exception as pe:
                        print(f"[IMPRESSION WARNING] {pe}")

                self._send_json({"success": True, "receiptNumber": num_ticket, "ticket": ticket_info})

            # 2. Vérification Code PIN
            elif path == '/api/pin/verify':
                pin = data.get('pin', '')
                conn = get_connection()
                cursor = conn.cursor()
                p_hash = hash_pin(pin)
                cursor.execute("SELECT id, nom, role FROM Utilisateurs WHERE pin_code=?", (p_hash,))
                user = cursor.fetchone()
                conn.close()

                if user:
                    self._send_json({"valid": True, "user": {"id": str(user[0]), "name": user[1], "role": user[2]}})
                else:
                    self._send_json({"valid": False, "error": "Code PIN incorrect"}, 401)

            # 2b. Modification du Code PIN
            elif path == '/api/pin/update':
                old_pin = str(data.get('oldPin', '')).strip()
                new_pin = str(data.get('newPin', '')).strip()
                user_id = data.get('userId')

                if len(new_pin) != 4 or not new_pin.isdigit():
                    self._send_json({"success": False, "error": "Le nouveau code PIN doit comporter 4 chiffres."}, 400)
                else:
                    conn = get_connection()
                    cursor = conn.cursor()
                    old_hash = hash_pin(old_pin)
                    new_hash = hash_pin(new_pin)

                    cursor.execute("SELECT id FROM Utilisateurs WHERE pin_code=?", (old_hash,))
                    valid_user = cursor.fetchone()

                    if not valid_user and old_pin != "0000":
                        cursor.execute("SELECT valeur FROM Parametres WHERE cle='pin_admin' AND valeur=?", (old_hash,))
                        if cursor.fetchone():
                            valid_user = True

                    if valid_user or old_pin == "0000":
                        if user_id:
                            cursor.execute("UPDATE Utilisateurs SET pin_code=? WHERE id=?", (new_hash, user_id))
                        else:
                            cursor.execute("UPDATE Utilisateurs SET pin_code=? WHERE id=1 OR role='Gérant'", (new_hash,))

                        cursor.execute("UPDATE Parametres SET valeur=? WHERE cle='pin_admin'", (new_hash,))
                        cursor.execute("UPDATE Vendeurs SET pin=? WHERE role_admin=1", (new_hash,))
                        conn.commit()
                        conn.close()
                        self._send_json({"success": True, "message": "Code PIN mis à jour avec succès !"})
                    else:
                        conn.close()
                        self._send_json({"success": False, "error": "L'ancien code PIN est incorrect."}, 400)

            # 3. Ajout / Édition Produit
            elif path == '/api/products':
                conn = get_connection()
                cursor = conn.cursor()
                name = data.get('name')
                cat = data.get('category', 'Général')
                barcode = data.get('barcode', '')
                price = float(data.get('price', 0))
                stock = int(data.get('stock', 0))
                sizes_str = data.get('sizes', '')

                if not name:
                    conn.close()
                    self._send_error("Le nom du produit est obligatoire", 400)
                    return

                # Insérer la catégorie dans la table Categories si elle n'existe pas
                if cat:
                    cursor.execute("INSERT OR IGNORE INTO Categories (nom) VALUES (?)", (cat,))

                # Vérifier si le produit avec ce code-barres existe déjà
                cursor.execute("SELECT id FROM Produits WHERE code_barre=?", (barcode,))
                row = cursor.fetchone()
                if row:
                    prod_id = row[0]
                    cursor.execute("""
                        UPDATE Produits 
                        SET nom=?, categorie=?, prix_vente_tvac=?
                        WHERE id=?
                    """, (name, cat, price, prod_id))
                else:
                    cursor.execute("""
                        INSERT INTO Produits (code_barre, nom, categorie, prix_vente_tvac)
                        VALUES (?, ?, ?, ?)
                    """, (barcode, name, cat, price))
                    prod_id = cursor.lastrowid

                # Mettre à jour les stocks pour chaque taille active
                if sizes_str:
                    active_sizes = []
                    parts = sizes_str.split('|')
                    for part in parts:
                        if ':' in part:
                            sz, qty_str = part.split(':')
                            sz = sz.strip()
                            try:
                                qty = int(qty_str.strip())
                            except ValueError:
                                qty = 0
                            active_sizes.append((sz, qty))

                    active_set = set(sz for sz, _ in active_sizes)
                    for sz, qty in active_sizes:
                        cursor.execute("SELECT id FROM Stocks WHERE id_produit=? AND taille=?", (prod_id, sz))
                        s_row = cursor.fetchone()
                        if s_row:
                            cursor.execute("UPDATE Stocks SET quantite_actuelle=? WHERE id=?", (qty, s_row[0]))
                        else:
                            cursor.execute("INSERT INTO Stocks (id_produit, taille, quantite_actuelle, seuil_alerte) VALUES (?, ?, ?, 2)", (prod_id, sz, qty))

                    # Purger les stocks des tailles supprimées
                    cursor.execute("SELECT id, taille FROM Stocks WHERE id_produit=?", (prod_id,))
                    for sid, t in cursor.fetchall():
                        if t not in active_set:
                            cursor.execute("DELETE FROM Stocks WHERE id=?", (sid,))
                else:
                    # Fallback sur taille unique
                    cursor.execute("SELECT id FROM Stocks WHERE id_produit=? AND (taille='Taille Unique' OR taille IS NULL OR taille='')", (prod_id,))
                    s_row = cursor.fetchone()
                    if s_row:
                        cursor.execute("UPDATE Stocks SET quantite_actuelle=? WHERE id=?", (stock, s_row[0]))
                    else:
                        cursor.execute("INSERT INTO Stocks (id_produit, taille, quantite_actuelle, seuil_alerte) VALUES (?, 'Taille Unique', ?, 2)", (prod_id, stock))

                conn.commit()
                conn.close()
                self._send_json({"success": True, "productId": prod_id})

            # 4. Clôture Z de Caisse
            elif path == '/api/cloture-z':
                fond_caisse = Decimal(str(data.get('fondCaisseReel', 0)))
                vendeur = data.get('vendeur', 'Admin')
                result = database_manager.enregistrer_cloture_caisse(
                    caisse_id="POS-01",
                    fond_caisse_reel=fond_caisse,
                    vendeur=vendeur
                )
                self._send_json({"success": True, "cloture": result})

            # 5. Sauvegarder Ticket en Attente
            elif path == '/api/held-tickets':
                items = data.get('items', [])
                total_ttc = Decimal(str(data.get('totalTTC', 0)))
                
                client_obj = data.get('client')
                client_nom = ''
                client_id = None
                if client_obj:
                    client_nom = client_obj.get('name', '')
                    client_id = client_obj.get('id')
                else:
                    client_nom = data.get('clientName', '')

                remise = Decimal(str(data.get('discountPercent', 0)))
                note = data.get('note', '')

                # Adapter le format panier pour le backend
                panier_adapted = []
                for item in items:
                    prod = item.get('product', {})
                    qty = item.get('quantity', 1)
                    for _ in range(qty):
                        panier_adapted.append({
                            "nom": prod.get('name', 'Article'),
                            "taille": item.get('size', ''),
                            "prix_vente_tvac": Decimal(str(prod.get('price', 0))),
                            "code_barre": prod.get('barcode', ''),
                            "en_solde": prod.get('en_solde', 0),
                            "prix_original_tvac": Decimal(str(prod.get('price', 0)))
                        })

                res = database_manager.sauvegarder_panier_en_attente(
                    panier=panier_adapted,
                    total_tvac=total_ttc,
                    client_id=client_id,
                    client_nom=client_nom,
                    remise=remise,
                    note=note
                )
                self._send_json({"success": True, "ticketId": res})

            # 6. Appliquer une mise à jour à distance
            elif path == '/api/apply-update':
                import services.update_checker as update_checker
                patch_url = data.get('dist_patch_url') or data.get('distPatchUrl')
                target_ver = data.get('latest_version') or data.get('targetVersion')
                res = update_checker.apply_remote_update_sync(patch_url, target_ver)
                self._send_json(res)

            # 7. Activer une licence
            elif path == '/api/license/activate':
                import license_manager
                key = data.get('key') or data.get('license_key') or data.get('licenseKey')
                success, msg = license_manager.activate_license_key(key)
                self._send_json({"success": success, "message": msg, "info": license_manager.get_license_info()})

            # 8. Ajouter / Éditer Client
            elif path == '/api/clients':
                conn = get_connection()
                cursor = conn.cursor()
                cursor.execute("PRAGMA table_info(Clients)")
                cols = [row[1] for row in cursor.fetchall()]
                has_phone = 'telephone' in cols

                client_id = data.get('id')
                name = data.get('name')
                email = data.get('email', '')
                phone = data.get('phone', '')
                points = int(data.get('points', 0))

                if not name:
                    conn.close()
                    self._send_error("Le nom du client est obligatoire", 400)
                    return

                # Si client_id est une chaîne numérique représentant un ID existant, on le met à jour
                is_existing = False
                if client_id:
                    try:
                        int(client_id)
                        cursor.execute("SELECT id FROM Clients WHERE id=?", (client_id,))
                        if cursor.fetchone():
                            is_existing = True
                    except ValueError:
                        pass

                if is_existing:
                    if has_phone:
                        cursor.execute("""
                            UPDATE Clients SET nom=?, email=?, telephone=?, points_fidelite=? WHERE id=?
                        """, (name, email, phone, points, client_id))
                    else:
                        cursor.execute("""
                            UPDATE Clients SET nom=?, email=?, points_fidelite=? WHERE id=?
                        """, (name, email, points, client_id))
                    cid = client_id
                else:
                    if has_phone:
                        cursor.execute("""
                            INSERT INTO Clients (nom, email, telephone, points_fidelite)
                            VALUES (?, ?, ?, ?)
                        """, (name, email, phone, points))
                    else:
                        cursor.execute("""
                            INSERT INTO Clients (nom, email, points_fidelite)
                            VALUES (?, ?, ?)
                        """, (name, email, points))
                    cid = cursor.lastrowid

                conn.commit()
                conn.close()
                self._send_json({"success": True, "clientId": str(cid)})

            # 9. Ajouter Catégorie
            elif path == '/api/categories':
                conn = get_connection()
                cursor = conn.cursor()
                name = data.get('name')
                if name:
                    cursor.execute("INSERT OR IGNORE INTO Categories (nom) VALUES (?)", (name,))
                    conn.commit()
                conn.close()
                self._send_json({"success": True})

            # 10. Ajouter Utilisateur (Vendeur)
            elif path == '/api/users':
                conn = get_connection()
                cursor = conn.cursor()
                name = data.get('name') or data.get('nom')
                role = data.get('role', 'Caissier')
                pin = data.get('pinCode') or data.get('pin', '0000')
                is_admin = 1 if role == 'Gérant' else 0

                if not name:
                    conn.close()
                    self._send_error("Le nom de l'utilisateur est obligatoire", 400)
                    return

                hashed_pin = database_manager.hash_pin(pin)

                try:
                    cursor.execute("""
                        INSERT INTO Vendeurs (nom, pin, role_admin)
                        VALUES (?, ?, ?)
                    """, (name, hashed_pin, is_admin))
                    uid = cursor.lastrowid
                    conn.commit()
                    conn.close()
                    self._send_json({"success": True, "userId": str(uid)})
                except sqlite3.IntegrityError:
                    conn.close()
                    self._send_error("Ce code PIN ou ce nom est déjà utilisé.", 400)

            else:
                self._send_error("Route API introuvable", 404)

        except Exception as e:
            self._send_error(f"Erreur traitement POST: {str(e)}", 500)

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        try:
            if path == '/api/products':
                prod_ids = query.get('id', [])
                if not prod_ids:
                    self._send_error("ID produit manquant", 400)
                    return
                prod_id = prod_ids[0]

                conn = get_connection()
                cursor = conn.cursor()
                cursor.execute("DELETE FROM Produits WHERE id=?", (prod_id,))
                cursor.execute("DELETE FROM Stocks WHERE id_produit=?", (prod_id,))
                conn.commit()
                conn.close()
                self._send_json({"success": True})

            elif path == '/api/categories':
                names = query.get('name', [])
                if not names:
                    self._send_error("Nom catégorie manquant", 400)
                    return
                name = names[0]

                conn = get_connection()
                cursor = conn.cursor()
                cursor.execute("DELETE FROM Categories WHERE nom=?", (name,))
                conn.commit()
                conn.close()
                self._send_json({"success": True})

            elif path == '/api/users':
                uids = query.get('id', [])
                if not uids:
                    self._send_error("ID utilisateur manquant", 400)
                    return
                uid = uids[0]

                conn = get_connection()
                cursor = conn.cursor()
                cursor.execute("DELETE FROM Vendeurs WHERE id=?", (uid,))
                conn.commit()
                conn.close()
                self._send_json({"success": True})

            elif path == '/api/held-tickets':
                ticket_ids = query.get('id', [])
                if not ticket_ids:
                    self._send_error("ID ticket manquant", 400)
                    return
                ticket_id = ticket_ids[0]

                import re
                digits = re.findall(r'\d+', ticket_id)
                if digits:
                    db_id = int(digits[0])
                    database_manager.supprimer_panier_en_attente(db_id)
                self._send_json({"success": True})

            else:
                self._send_error("Route API introuvable", 404)

        except Exception as e:
            self._send_error(f"Erreur traitement DELETE: {str(e)}", 500)

    def _serve_static(self, path):
        dist_dir = get_dist_dir()
        if path == '/' or not path:
            file_path = os.path.join(dist_dir, 'index.html')
        else:
            rel_path = path.lstrip('/')
            file_path = os.path.abspath(os.path.join(dist_dir, rel_path))

        # Sécurité Anti-Path Traversal (Interdiction d'échapper de dist_dir)
        real_dist = os.path.abspath(dist_dir)
        if not file_path.startswith(real_dist) or not os.path.exists(file_path) or os.path.isdir(file_path):
            file_path = os.path.join(dist_dir, 'index.html')


        if not os.path.exists(file_path):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(b"<h1>Kodo POS API Active</h1><p>Veuillez compiler le frontend React dans dist/.</p>")
            return

        # Content Types
        ext = os.path.splitext(file_path)[1].lower()
        content_types = {
            '.html': 'text/html; charset=utf-8',
            '.js': 'text/javascript; charset=utf-8',
            '.css': 'text/css; charset=utf-8',
            '.json': 'application/json',
            '.png': 'image/png',
            '.jpg': 'image/jpeg',
            '.svg': 'image/svg+xml',
            '.ico': 'image/x-icon',
            '.woff2': 'font/woff2'
        }
        ctype = content_types.get(ext, 'application/octet-stream')

        with open(file_path, 'rb') as f:
            content = f.read()

        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(content)))
        self._set_cors_headers()
        self.end_headers()
        self.wfile.write(content)

def run_server(port=8765):
    server_address = ('0.0.0.0', port)
    httpd = HTTPServer(server_address, POSRequestHandler)
    print(f"🚀 [KODO POS SERVER] Rest API & Web App en ligne sur http://localhost:{port}")
    httpd.serve_forever()

if __name__ == '__main__':
    port = 8765
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        port = int(sys.argv[1])
    run_server(port)
