import threading
import time
import json
import urllib.request
import urllib.error
import urllib.parse
import datetime
from decimal import Decimal
from database_manager import get_connection

class ShopifySyncThread(threading.Thread):
    def __init__(self):
        super().__init__()
        self.daemon = True
        self.running = True
        self.store_url = ""
        self.access_token = ""
        self.api_version = "2024-01"
        self._location_id = None
        self._load_config()

    def _load_config(self):
        try:
            conn = get_connection()
            c = conn.cursor()
            c.execute("SELECT valeur FROM Parametres WHERE cle='shopify_store_url'")
            row_url = c.fetchone()
            if row_url:
                self.store_url = row_url[0].strip()
                
            c.execute("SELECT valeur FROM Parametres WHERE cle='shopify_access_token'")
            row_token = c.fetchone()
            if row_token:
                self.access_token = row_token[0].strip()
            conn.close()
        except Exception as e:
            print(f"[SHOPIFY SYNC] Erreur chargement config: {e}")

    def run(self):
        print("[SHOPIFY SYNC] Démarrage du thread en arrière-plan...")
        while self.running:
            self._load_config() # Reload config
            if self.store_url and self.access_token:
                self._sync_tickets_to_shopify()
                self._sync_orders_from_shopify()
            time.sleep(60) # Vérifie toutes les minutes
            
    def _make_request(self, endpoint, method="GET", data=None):
        base_url = self.store_url.replace("https://", "").replace("http://", "").strip("/")
        url = f"https://{base_url}/admin/api/{self.api_version}/{endpoint}"
        headers = {
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": self.access_token
        }
        
        for attempt in range(3):
            req = urllib.request.Request(url, headers=headers, method=method)
            if data:
                req.data = json.dumps(data).encode('utf-8')
                
            try:
                with urllib.request.urlopen(req) as response:
                    return json.loads(response.read().decode())
            except urllib.error.HTTPError as e:
                if e.code == 429: # Rate limit
                    retry_after = int(e.headers.get("Retry-After", 2))
                    print(f"[SHOPIFY SYNC] Rate limited (429). Attente de {retry_after}s (essai {attempt+1}/3)...")
                    time.sleep(retry_after)
                    continue
                else:
                    try:
                        err_detail = e.read().decode('utf-8', errors='ignore')
                    except Exception:
                        err_detail = str(e)
                    print(f"[SHOPIFY SYNC] Erreur HTTP ({e.code}) sur {method} {endpoint}: {err_detail}")
                    return None
            except Exception as e:
                print(f"[SHOPIFY SYNC] Erreur API ({method} {endpoint}): {e}")
                return None
        return None

    def _get_shopify_location_id(self):
        if self._location_id:
            return self._location_id
            
        data = self._make_request("locations.json")
        if data and "locations" in data and len(data["locations"]) > 0:
            active_locs = [l for l in data["locations"] if l.get("active", True)]
            if active_locs:
                self._location_id = active_locs[0]["id"]
                return self._location_id
        return None

    def _find_inventory_item_id(self, sku):
        # 1. Essai via l'API GraphQL de Shopify (méthode robuste et recommandée par Shopify)
        query = """
        query($query: String!) {
          productVariants(first: 1, query: $query) {
            edges {
              node {
                inventoryItem {
                  id
                }
              }
            }
          }
        }
        """
        variables = {"query": f"sku:{sku} OR barcode:{sku}"}
        graphql_data = {"query": query, "variables": variables}
        
        data = self._make_request("graphql.json", method="POST", data=graphql_data)
        if data and "data" in data:
            edges = data["data"].get("productVariants", {}).get("edges", [])
            if edges:
                node = edges[0].get("node", {})
                if node and "inventoryItem" in node:
                    gid = node["inventoryItem"].get("id", "")
                    if gid:
                        # Extrait l'ID numérique à partir du GID (ex: gid://shopify/InventoryItem/123456)
                        try:
                            return int(gid.split("/")[-1])
                        except ValueError:
                            pass
            
        # 2. Fallback de secours sur la liste complète si SKU introuvable en GraphQL
        products_data = self._make_request("products.json?limit=250&fields=variants")
        if products_data and "products" in products_data:
            for p in products_data["products"]:
                for v in p.get("variants", []):
                    if v.get("sku") == sku or v.get("barcode") == sku:
                        return v.get("inventory_item_id")
        return None

    def _adjust_shopify_stock(self, inventory_item_id, location_id, qty_change):
        data = {
            "location_id": location_id,
            "inventory_item_id": inventory_item_id,
            "available_adjustment": qty_change
        }
        res = self._make_request("inventory_levels/adjust.json", method="POST", data=data)
        return res is not None

    def _sync_tickets_to_shopify(self):
        conn = None
        try:
            location_id = self._get_shopify_location_id()
            if not location_id:
                print("[SHOPIFY SYNC] Impossible de récupérer la localisation de l'inventaire.")
                return
                
            conn = get_connection()
            c = conn.cursor()
            c.execute("SELECT id, numero_ticket FROM Tickets WHERE synced_shopify = 0")
            tickets = c.fetchall()
            
            for t_id, num in tickets:
                c.execute('''
                    SELECT p.code_barre, vd.quantite 
                    FROM Ventes_Details vd 
                    JOIN Stocks s ON vd.id_stock = s.id
                    JOIN Produits p ON s.id_produit = p.id
                    WHERE vd.id_ticket = ? AND p.code_barre IS NOT NULL AND p.code_barre != ''
                ''', (t_id,))
                items = c.fetchall()
                
                success = True
                for code_barre, quantite in items:
                    print(f"[SHOPIFY SYNC] Pousser la vente locale (Ticket {num}) - Article SKU {code_barre} - Qte: {-quantite}")
                    inv_item_id = self._find_inventory_item_id(code_barre)
                    if inv_item_id:
                        # push negative value for sales, positive for refunds
                        adjusted = self._adjust_shopify_stock(inv_item_id, location_id, -quantite)
                        if not adjusted:
                            success = False
                            print(f"[SHOPIFY SYNC] Impossible d'ajuster le stock Shopify pour SKU {code_barre}")
                    else:
                        print(f"[SHOPIFY SYNC] SKU {code_barre} introuvable sur Shopify.")
                        
                if success:
                    c.execute("UPDATE Tickets SET synced_shopify = 1 WHERE id = ?", (t_id,))
                    print(f"[SHOPIFY SYNC] Ticket {num} marqué comme synchronisé Shopify.")
                    
            conn.commit()
        except Exception as e:
            if conn:
                conn.rollback()
            print(f"[SHOPIFY SYNC] Erreur sync tickets vers Shopify : {e}")
        finally:
            if conn:
                conn.close()

    def _sync_orders_from_shopify(self):
        conn = None
        try:
            orders_data = self._make_request("orders.json?status=any&fulfillment_status=unfulfilled&financial_status=paid")
            if not orders_data or "orders" not in orders_data:
                return
                
            conn = get_connection()
            c = conn.cursor()
            
            from database_manager import signer_ticket, signer_ledger
            
            for order in orders_data["orders"]:
                order_id = str(order["id"])
                order_number = order["order_number"]
                
                c.execute("SELECT id FROM Tickets WHERE shopify_order_id = ?", (order_id,))
                if c.fetchone():
                    continue
                    
                print(f"[SHOPIFY SYNC] Traitement de la commande Shopify #{order_number} (ID: {order_id})")
                line_items = order.get("line_items", [])
                
                try:
                    stock_changes = []
                    for item in line_items:
                        sku = item.get("sku")
                        qty = int(item.get("quantity", 0))
                        if not sku or qty <= 0:
                            continue
                            
                        c.execute("""
                            SELECT s.id, s.quantite_actuelle, p.nom, p.taux_tva, p.prix_vente_tvac
                            FROM Stocks s
                            JOIN Produits p ON s.id_produit = p.id
                            WHERE p.code_barre = ? OR p.nom = ?
                            LIMIT 1
                        """, (sku, item.get("title")))
                        row = c.fetchone()
                        
                        if row:
                            sid, qte_actuelle, name, taux_tva, prix_vente_tvac = row
                            qty_to_deduct = min(qty, qte_actuelle)
                            if qty_to_deduct > 0:
                                c.execute("UPDATE Stocks SET quantite_actuelle = quantite_actuelle - ? WHERE id = ?", (qty_to_deduct, sid))
                                
                            stock_changes.append({
                                "stock_id": sid,
                                "nom": name,
                                "prix_vente_tvac": Decimal(str(prix_vente_tvac or 0.0)),
                                "taux_tva": Decimal(str(taux_tva or 0.21)),
                                "qty": qty
                            })
                        else:
                            print(f"[SHOPIFY SYNC] SKU local introuvable pour {sku} / {item.get('title')}")
                            
                    total_price = Decimal(str(order.get("total_price", "0.00")))
                    total_tax = Decimal(str(order.get("total_tax", "0.00")))
                    total_htva = total_price - total_tax
                    
                    # Nettoyage strict du numéro de commande pour éviter la traversée de chemin
                    safe_order_num = "".join(char for char in str(order_number) if char.isalnum() or char in "-_")
                    num_ticket = f"SHPF-{safe_order_num}"
                    date_heure = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    
                    sig_ticket, hash_prec_ticket = signer_ticket(c, num_ticket, total_price, date_heure)
                    
                    c.execute("""
                        INSERT INTO Tickets (numero_ticket, date_heure, total_tvac, total_htva, total_tva, methode_paiement, signature, hash_precedent, shopify_order_id, synced_shopify)
                        VALUES (?, ?, ?, ?, ?, 'Shopify', ?, ?, ?, 1)
                    """, (num_ticket, date_heure, total_price, total_htva, total_tax, sig_ticket, hash_prec_ticket, order_id))
                    
                    ticket_id = c.lastrowid
                    
                    for sc in stock_changes:
                        c.execute("""
                            INSERT INTO Ventes_Details (id_ticket, id_stock, quantite, prix_unitaire_tvac)
                            VALUES (?, ?, ?, ?)
                        """, (ticket_id, sc["stock_id"], sc["qty"], sc["prix_vente_tvac"]))
                        
                    sig_ledger, hash_ledger = signer_ledger(c, 'VENTE', total_price, 'Shopify', num_ticket, date_heure)
                    c.execute("""
                        INSERT INTO Ledger_Caisse (vendeur, type_mouvement, montant, methode_paiement, reference, date_heure, signature, hash_precedent)
                        VALUES ('Shopify Sync', 'VENTE', ?, 'Shopify', ?, ?, ?, ?)
                    """, (total_price, num_ticket, date_heure, sig_ledger, hash_ledger))
                    
                    conn.commit()
                    print(f"[SHOPIFY SYNC] Commande #{order_number} synchronisée en local.")
                    
                except Exception as ex_order:
                    if conn:
                        conn.rollback()
                    print(f"[SHOPIFY SYNC] Erreur traitement commande #{order_number} : {ex_order}")
                    
        except Exception as e:
            print(f"[SHOPIFY SYNC] Erreur sync commandes de Shopify : {e}")
        finally:
            if conn:
                conn.close()

    def import_shopify_catalog(self, progress_callback=None):
        """
        Importe le catalogue complet de Shopify vers la base locale Kōdo POS.
        """
        self._load_config()
        if not self.store_url or not self.access_token:
            raise ValueError("Configuration Shopify manquante (URL ou Token).")

        print("[SHOPIFY SYNC] Début de l'importation du catalogue...")
        if progress_callback: progress_callback("Récupération des produits Shopify...", 10)
        
        products_data = self._make_request("products.json?limit=250")
        if not products_data or "products" not in products_data:
            print("[SHOPIFY SYNC] Aucun produit trouvé sur Shopify ou erreur de connexion.")
            return 0

        shopify_products = products_data["products"]
        total_p = len(shopify_products)
        print(f"[SHOPIFY SYNC] {total_p} produits récupérés de Shopify.")

        conn = get_connection()
        c = conn.cursor()
        
        imported_count = 0
        for idx, p in enumerate(shopify_products):
            if progress_callback:
                pct = 10 + int((idx / total_p) * 80)
                progress_callback(f"Importation : {p.get('title')} ({idx+1}/{total_p})...", pct)
                
            nom = p.get("title", "Sans nom")
            cat = p.get("product_type", "Général") or "Général"
            
            c.execute("INSERT OR IGNORE INTO Categories (nom) VALUES (?)", (cat,))
            
            for v in p.get("variants", []):
                sku = v.get("sku") or v.get("barcode") or f"SHPF-{v.get('id')}"
                if not sku:
                    continue
                    
                price_str = v.get("price", "0.00")
                compare_str = v.get("compare_at_price")
                
                try: price_val = Decimal(price_str)
                except: price_val = Decimal("0.00")
                
                en_solde = 0
                prix_solde_tvac = None
                prix_vente_tvac = price_val
                
                if compare_str:
                    try:
                        compare_val = Decimal(compare_str)
                        if compare_val > price_val:
                            en_solde = 1
                            prix_vente_tvac = compare_val
                            prix_solde_tvac = price_val
                    except:
                        pass
                
                prix_achat_htva = (prix_vente_tvac / Decimal("2.5")).quantize(Decimal("0.01"))
                
                c.execute("""
                    INSERT OR REPLACE INTO Produits (code_barre, nom, categorie, prix_achat_htva, prix_vente_tvac, en_solde, prix_solde_tvac)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (sku, nom, cat, float(prix_achat_htva), float(prix_vente_tvac), en_solde, float(prix_solde_tvac) if prix_solde_tvac else None))
                
                pid = c.lastrowid
                if not pid:
                    c.execute("SELECT id FROM Produits WHERE code_barre=?", (sku,))
                    p_row = c.fetchone()
                    if p_row: pid = p_row[0]
                
                if pid:
                    opt1 = v.get("option1", "Unique")
                    opt2 = v.get("option2")
                    taille = opt1 if opt1 and opt1 != "Default Title" else "Unique"
                    if opt2 and opt2 != "Default Title":
                        taille = f"{taille} / {opt2}"
                        
                    qty = int(v.get("inventory_quantity", 0))
                    
                    c.execute("SELECT id FROM Stocks WHERE id_produit=? AND taille=?", (pid, taille))
                    s_row = c.fetchone()
                    if s_row:
                        c.execute("UPDATE Stocks SET quantite_actuelle=? WHERE id=?", (qty, s_row[0]))
                    else:
                        c.execute("INSERT INTO Stocks (id_produit, taille, quantite_actuelle, seuil_alerte) VALUES (?, ?, ?, 2)", (pid, taille, qty))
                
                imported_count += 1
                
        conn.commit()
        conn.close()
        
        if progress_callback: progress_callback("Importation terminée !", 100)
        print(f"[SHOPIFY SYNC] Importation terminée : {imported_count} variantes importées.")
        return imported_count
