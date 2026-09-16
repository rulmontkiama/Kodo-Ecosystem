# Dossier de Passation Technique & Audit de Code : Module Live Shopping (Kōdo POS)

Ce document a été spécialement structuré pour permettre à **Claude** (ou tout autre relecteur technique) d'examiner de manière exhaustive l'architecture, la sécurité, l'intégrité des données et l'ergonomie du module **Live Shopping Companion** développé pour le logiciel de caisse **Kōdo POS**.

---

## 1. Contexte & Périmètre Fonctionnel

Le module **Live Shopping Companion** permet à une boutique de mode ou de prêt-à-porter de vendre en direct (TikTok Live, Instagram Live, Facebook Live) en synchronisation parfaite et temps réel avec la base de données de sa caisse physique **Kōdo POS**.

### Acteurs & Interfaces
1. **L'exposant / Vendeur en magasin** (Interface POS Desktop) :
   * Prépare son portant virtuel avant le direct (sélection des articles du stock, tri de passage, scan douchette).
   * Pilote le direct via une télécommande temps réel (« Article Suivant / Précédent », mise en avant immédiate).
   * Suit la file d'attente FIFO (qui a réservé en 1er) par article ou par panier client consolidé.
   * Encaisse en 1 clic sur la caisse Kōdo POS avec émission du ticket de caisse et déduction du stock.
   * Génère les messages récapitulatifs personnalisés (WhatsApp / Instagram DM).

2. **Le spectateur / Client** (Interface Mobile Web `?view=live`) :
   * Scanne le QR Code affiché à la caméra du live ou clique sur le lien partagé en bio / story.
   * Renseigne rapidement ses coordonnées (nom, téléphone, adresse ou retrait magasin, tailles habituelles).
   * Voit en direct la pièce actuellement montrée par le vendeur à la caméra avec décompte des pièces restantes par taille.
   * Réserve sa taille en 1 clic (règle FIFO : si stock épuisé, il est placé automatiquement en liste d'attente Rang 1, 2, etc.).
   * Choisit son mode de finalisation : **Retrait en boutique** ou **Payer par Virement Bancaire**.

---

## 2. Cartographie Complète des Fichiers

### 🐍 Backend Python & SQLite (`/Volumes/Extreme SSD/KIAMA/Kōdo POS/`)

| Fichier | Rôle & Responsabilité |
|---|---|
| [`kodo_core/domain/live/live_manager.py`](file:///Volumes/Extreme%20SSD/KIAMA/Ko%CC%84do%20POS/kodo_core/domain/live/live_manager.py) | **Cœur de la logique métier** : gestion des sessions, algorithme de réservation FIFO, détection et décompte de stock, recalcul automatique des rangs lors d'une annulation, encaissement de claim vers ticket POS, synchronisation CRM `Clients` et messages récapitulatifs. |
| [`kodo_core/api/routes/live_routes.py`](file:///Volumes/Extreme%20SSD/KIAMA/Ko%CC%84do%20POS/kodo_core/api/routes/live_routes.py) | **Contrôleur REST API** : 13 routes HTTP `/api/live/*` (session, catalogue ordonné, inscription acheteur, claims FIFO, mise à jour de statut, encaissement caisse, QR code, messages). |
| [`kodo_core/api/app.py`](file:///Volumes/Extreme%20SSD/KIAMA/Ko%CC%84do%20POS/kodo_core/api/app.py) | **Dispatcher HTTP** central : enregistrement de `handle_live_request` dans la liste des routeurs modulaires. |
| [`kodo_core/api/routes/system_routes.py`](file:///Volumes/Extreme%20SSD/KIAMA/Ko%CC%84do%20POS/kodo_core/api/routes/system_routes.py) | **Paramètres & Matériel POS** : lecture/écriture `shop_iban` et endpoint `POST /api/printer/test` (ticket test d'impression). |
| [`kodo_core/hardware/printer.py`](file:///Volumes/Extreme%20SSD/KIAMA/Ko%CC%84do%20POS/kodo_core/hardware/printer.py) | **Pilote thermique ESC/POS** : génération du ticket de test (`generer_ticket_test`), envoi réseau/USB/CUPS (`imprimer_ticket_test`). |
| [`database_manager.py`](file:///Volumes/Extreme%20SSD/KIAMA/Ko%CC%84do%20POS/database_manager.py) | **Schéma BDD SQLite** : tables `Live_Sessions`, `Live_Buyers`, `Live_Claims`. |
| [`server_pos.py`](file:///Volumes/Extreme%20SSD/KIAMA/Ko%CC%84do%20POS/server_pos.py) | **Serveur HTTP multi-thread** sur le port `8765`. |

---

### ⚛️ Frontend React & TypeScript (`/Users/kiamarulmont/Desktop/kōdo-pos-3/`)

| Fichier | Rôle & Responsabilité |
|---|---|
| [`src/components/LiveClientView.tsx`](file:///Users/kiamarulmont/Desktop/ko%CC%84do-pos-3/src/components/LiveClientView.tsx) | **Interface Spectateur / Client Mobile** : formulaire d'inscription, affichage pièce live en temps réel (polling 2.5s), sélecteur de tailles avec état de disponibilité, onglet *Mes Réservations*, choix de règlement (**Virement bancaire** avec copie en 1 clic de l'IBAN et de la référence `LIVE-{NOM}-{ID}` vs **Retrait en boutique** sous 48h). |
| [`src/components/LiveShoppingView.tsx`](file:///Users/kiamarulmont/Desktop/ko%CC%84do-pos-3/src/components/LiveShoppingView.tsx) | **Studio & Télécommande Vendeur POS** : préparation du portant, ordonnancement par flèches ⬆️⬇️ et scan douchette, télécommande live (Next/Prev), gestion des commandes par article ou panier client, encaissement ticket POS, modale QR code, filtres de paiement (`virement`, `sur_place`, `paye`, `non_paye`). |
| [`src/services/api.ts`](file:///Users/kiamarulmont/Desktop/ko%CC%84do-pos-3/src/services/api.ts) | **Client API Typé** : objet `liveApi` regroupant tous les appels fetch backend + support de `iban` dans `posApi.getSettings()` et `saveSettings()`. |
| [`src/types.ts`](file:///Users/kiamarulmont/Desktop/ko%CC%84do-pos-3/src/types.ts) | **Interfaces TypeScript** : `LiveSession`, `LiveProduct`, `LiveSize`, `LiveBuyer`, `LiveClaim`, `LivePaymentStatus` (`'non_paye' \| 'paye' \| 'sur_place' \| 'virement'`). |
| [`src/components/Sidebar.tsx`](file:///Users/kiamarulmont/Desktop/ko%CC%84do-pos-3/src/components/Sidebar.tsx) | **Navigation POS** : onglet « Live Shopping » avec badge pulsant `LIVE` quand une session est en cours. |
| [`src/components/ParametresView.tsx`](file:///Users/kiamarulmont/Desktop/ko%CC%84do-pos-3/src/components/ParametresView.tsx) | **Paramètres Caisse** : champ de saisie de l'IBAN du magasin avec persistance locale et SQLite. |
| [`src/App.tsx`](file:///Users/kiamarulmont/Desktop/ko%CC%84do-pos-3/src/App.tsx) | **Routage d'accès** : détection automatique de l'URL client `?view=live` ou `/live` sans perturber la caisse principale. |

---

## 3. Schéma de la Base de Données SQLite

```sql
-- 1. Sessions de Live
CREATE TABLE IF NOT EXISTS Live_Sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    titre TEXT NOT NULL,
    statut TEXT DEFAULT 'en_cours',          -- 'en_cours' | 'terminé' | 'pause'
    date_debut DATETIME DEFAULT CURRENT_TIMESTAMP,
    date_fin DATETIME,
    produit_vedette_id INTEGER,               -- FK vers Produits(id)
    notes TEXT                                -- JSON contenant: product_ids (ordre portant), iban, etc.
);

-- 2. Profils Acheteurs (Spectateurs inscrits)
CREATE TABLE IF NOT EXISTS Live_Buyers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER,                        -- FK optionnelle vers Clients(id) (CRM POS)
    nom TEXT,
    prenom TEXT,
    telephone TEXT,
    email TEXT,
    pseudo_social TEXT,                       -- @pseudo TikTok / Instagram
    mode_reception TEXT DEFAULT 'retrait_magasin', -- 'retrait_magasin' | 'livraison'
    adresse_rue TEXT,
    code_postal TEXT,
    ville TEXT,
    pays TEXT,
    taille_haut TEXT,
    taille_bas TEXT,
    pointure TEXT,
    notes TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 3. Réservations / Claims (File FIFO)
CREATE TABLE IF NOT EXISTS Live_Claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,              -- FK vers Live_Sessions(id)
    buyer_id INTEGER NOT NULL,                -- FK vers Live_Buyers(id)
    client_id INTEGER,                        -- FK vers Clients(id)
    product_id INTEGER NOT NULL,              -- FK vers Produits(id)
    stock_id INTEGER,                         -- FK vers Stocks(id)
    article_nom TEXT NOT NULL,
    taille TEXT,
    prix_unitaire_tvac REAL NOT NULL DEFAULT 0.0,
    quantite INTEGER NOT NULL DEFAULT 1,
    statut_attribution TEXT DEFAULT 'file_attente', -- 'attribué' | 'file_attente' | 'annulé' | 'encaissé'
    rang_file INTEGER DEFAULT 1,              -- 0 si attribué, 1..N si file d'attente
    statut_paiement TEXT DEFAULT 'non_paye',  -- 'non_paye' | 'paye' | 'sur_place' | 'virement'
    statut_commande TEXT DEFAULT 'en_attente',-- 'en_attente' | 'validé' | 'expédié' | 'retiré' | 'annulé'
    ticket_pos_id INTEGER,                    -- FK vers Tickets(id) une fois encaissé
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

---

## 4. Spécification des Endpoints REST API (`kodo_core/api/routes/live_routes.py`)

| Méthode | URL | Description | Payload Body / Query |
|---|---|---|---|
| `GET` | `/api/live/session` | Récupère la session live active | - |
| `GET` | `/api/live/catalog` | Catalogue des articles du live dans l'ordre du portant | `?session_id=X` |
| `POST` | `/api/live/register` | Inscription spectateur & liaison CRM auto | `{ prenom, nom, telephone, email, pseudo_social, mode_reception, ... }` |
| `POST` | `/api/live/claim` | Réservation d'une taille (Algorithme FIFO) | `{ session_id, buyer_id, product_id, stock_id, taille, quantite }` |
| `GET` | `/api/live/my-claims` | Réservations d'un spectateur donné | `?buyer_id=X&session_id=Y` |
| `GET` | `/api/live/admin/claims` | Liste des réservations pour le caissier | `?session_id=X&statut_attribution=Y&statut_paiement=Z` |
| `POST` | `/api/live/admin/claims/update`| MAJ statut paiement/attribution d'une claim | `{ claim_id, statut_paiement, statut_attribution }` |
| `POST` | `/api/live/admin/session/start` | Démarre une session live avec sélection d'articles | `{ titre, product_ids, notes }` |
| `POST` | `/api/live/admin/session/stop` | Clôture la session live | `{ session_id }` |
| `POST` | `/api/live/admin/session/feature` | Met en avant un article en direct | `{ session_id, product_id }` |
| `POST` | `/api/live/admin/claims/checkout` | Encaisse une claim sur la caisse POS | `{ claim_id, payment_method, cashier_name }` |
| `POST` | `/api/live/admin/message` | Génère le message WhatsApp/DM avec IBAN/Récap | `{ buyer_id, session_id }` |
| `GET` | `/api/live/qr` | URL de génération du QR Code spectateurs | `?url=X` |

---

## 5. Algorithmes & Logique Métier Clé

### A. Algorithme d'attribution FIFO (`LiveManager.reserve_product`)
1. **Contrôle d'intégrité du stock** :
   Le stock physique disponible pour le live est calculé dynamiquement :
   $$\text{Stock Live Dispo} = \max(0, \text{Stock Réel} - \text{Claims Actives Attribuées})$$
2. **Attribution immédiate vs File d'attente** :
   * Si $\text{Stock Live Dispo} \ge \text{Quantité Demandée}$ :
     * `statut_attribution = 'attribué'`
     * `rang_file = 0`
   * Si stock épuisé :
     * `statut_attribution = 'file_attente'`
     * `rang_file = (\text{Nombre de claims déjà en attente}) + 1`
3. **Recalcul automatique lors d'une annulation** (`_recalculate_queue_ranks`) :
   Si une réservation attribuée est annulée par l'administrateur ou le client, l'algorithme sélectionne automatiquement la première claim en file d'attente (`rang_file = 1`), la promeut en `attribué`, et décrémente le rang de toutes les claims suivantes ($rang - 1$).

### B. Flux des Paiements (Virement vs Retrait boutique)
* **Retrait en boutique** (`sur_place`) :
  * Les articles sont réservés 48h à la boutique.
  * Règlement différé au comptoir en CB ou Espèces.
* **Virement bancaire** (`virement`) :
  * Affichage immédiat de l'IBAN du magasin et de la communication de virement unique (`LIVE-{PRENOM}-{ID}`).
  * Boutons de copie rapide dans le presse-papier avec feedback visuel.
  * Statut marqué comme `virement` en attente.
  * Le caissier dispose d'un bouton « Valider Virement Reçu » (`onUpdatePayment(id, 'paye')`).
  * Le message récapitulatif généré par l'admin intègre automatiquement les coordonnées bancaires et le libellé si un virement est attendu.

### C. Encaissement Caisse POS (`LiveManager.checkout_claim`)
* Création sécurisée d'une transaction dans la table `Tickets` de Kōdo POS avec génération du numéro de ticket séquentiel officiel.
* Déduction définitive du stock dans la table `Stocks`.
* Marquage de la claim en `statut_attribution = 'encaissé'` et liaison avec `ticket_pos_id`.
* Enregistrement dans le CRM `Clients` avec cumul du chiffre d'affaires du client (`total_depense`).

---

## 6. Checklist de Vérification & Points d'Attention pour Claude

Lors de votre audit, voici les points spécifiques à examiner en priorité :

1. **Transactions SQLite & Concurrence** :
   * Vérifier que toutes les opérations critiques de réservation (`reserve_product`) et d'encaissement (`checkout_claim`) s'exécutent sous transaction atomique (`conn.commit()` / `conn.rollback()`) avec gestion appropriée des verrous SQLite en cas d'appels concurrents simultanés.
2. **Recalcul FIFO** :
   * Examiner la méthode `_recalculate_queue_ranks` dans [`live_manager.py`](file:///Volumes/Extreme%20SSD/KIAMA/Ko%CC%84do%20POS/kodo_core/domain/live/live_manager.py) pour confirmer l'absence d'états incohérents lors de désistements multiples.
3. **Sécurité & Assainissement des Entrées** :
   * Vérifier le filtrage des champs textuels saisis par les acheteurs (`pseudo_social`, `telephone`, `adresse_rue`) afin d'éviter tout risque d'injection SQL ou XSS.
4. **Synchronisation Caisse & Stock** :
   * Valider que le passage au statut `encaissé` via la télécommande live décrémente correctement le stock sans générer de double vente si le caissier réalise simultanément une vente physique au comptoir.
5. **Comportement Réseau & Scalabilité** :
   * Tester la résilience du polling client (intervalle actuel à 2.5s) et l'expérience en cas de déconnexion réseau passagère du smartphone client. Possibilité d'évoluer vers Server-Sent Events (SSE) si nécessaire.

---

## 7. Commandes Utiles pour les Tests

```bash
# 1. Vérification TypeScript (0 erreur)
cd "/Users/kiamarulmont/Desktop/kōdo-pos-3"
npx tsc --noEmit

# 2. Vérification des routes de l'API backend
curl -s http://localhost:8765/api/live/session
curl -s http://localhost:8765/api/settings

# 3. Serveurs de développement actifs
# Backend Python : port 8765
python3.12 "/Volumes/Extreme SSD/KIAMA/Kōdo POS/server_pos.py"

# Frontend React : port 3000
cd "/Users/kiamarulmont/Desktop/kōdo-pos-3"
npm run dev
```
