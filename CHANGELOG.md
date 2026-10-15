# Changelog - Kōdo POS

Toutes les modifications notables apportées au projet Kōdo POS sont documentées dans ce fichier.

---

## [2.2.0] - 2026-10-15

### 🛡️ Traçabilité Fiscale NF525 & Atomicité
- **Commit unique indivisible (`BEGIN IMMEDIATE`)** : L'écriture du ticket de vente et son scellement SHA-256 dans le journal fiscal sont désormais garantis atomiques. Tout échec ou crash annule l'ensemble de la transaction (`conn.rollback()`), interdisant structurellement tout ticket payé orphelin sans sceau.
- **Concurrence sans fork** : Élimination de toute bifurcation de la chaîne de hachage lors d'encaissements simultanés multi-caisses.
- **Moteur financier pur (`FinancialEngine`)** : Moteur de calcul financier isolé et mathématiquement vérifié, garantissant l'invariant fiscal TVA (`total_htva + total_tva == total_tvac`).
- **Arrondi légal belge à 5 centimes (Loi du 01/12/2019)** : Isolation stricte de l'arrondi sur les seuls règlements en espèces (`ecart_arrondi_cash == 0.00` sur tout règlement sans espèces).
- **Plafonnement des avoirs et cartes cadeaux** : Le débit d'une carte cadeau ou d'un bon d'avoir est strictement borné au reste dû pour éviter toute perte de solde client.

### 🌐 Synchronisation Shopify (Outbox Transactionnelle)
- **Outbox transactionnelle** : La file d'attente `sync_queue` est inscrite dans la même transaction que la vente.
- **Recul exponentiel** : En cas de panne réseau, chaque ligne décompte ses essais et temporise ses retentatives (30s, 1min, 2min... jusqu'à 15min) via `prochaine_tentative`.
- **Idempotence GraphQL** : Clé d'idempotence unique par ajustement d'inventaire (`kodo-vd-{vd_id}-{inv_item_id}`).
- **Isolation des commandes web** : Les commandes passées sur Shopify décomptent le stock physique sans générer de vente ou de ticket en caisse locale.

### ⚡ Matériel & Ergonomie
- **Protection anti-double scan 800ms** : Verrouillage matériel anti-rebond sur douchette code-barres évitant les sur-ajouts accidentels au comptoir.
- **Clôture Z v2** : Scellement cryptographique complet de l'ensemble des montants et de la période couverte.
- **Contrôle d'intégrité au lancement** : Diagnostic automatique à l'ouverture de l'application avec bandeau préventif.

---

## [2.1.2] - 2026-10-14

> Les versions 2.0.x → 2.1.1 sont documentées dans les messages de commit et dans `public/latest.json`.

### 🛡️ Filet de sécurité
- **Contrôle d'intégrité au lancement** (`startup_check.py`) : lecture seule, jamais bloquant ; bandeau d'alerte à l'écran.
- **Go / no-go de pré-livraison** (`scripts/release/preflight.py`) : une livraison n'est publiable que sur le verdict GO.

### 💶 Argent
- Une vente remisée n'est plus refusée « Paiement insuffisant » (l'écran envoie le pourcentage de remise).
- Un Z validé sans comptage des espèces ne scelle plus un faux écart de caisse.
- Un retour ne peut plus être remboursé plusieurs fois sur une ligne sans article en stock ; le retour intégral d'un ticket
  remisé rend exactement le total payé ; la ventilation TVA du Z reste juste après un retour remisé.
- Règlement en espèces seules : l'arrondi légal à 5 centimes est enregistré ; un avoir n'est jamais débité au-delà du dû.
- Taux de TVA à 0 % conservé (il devenait 21 %) ; jours de bons cadeaux seuls listés pour la clôture.

### 🧾 Stock, tickets, impression
- Une fiche produit enregistrée sans quantités ne détruit plus le stock par taille ; un refus (code-barres déjà pris…)
  est expliqué à l'écran.
- Liste des tickets en attente lisible après redémarrage ; taille conservée.
- Réimpression : date d'origine + mention DUPLICATA ; montant remis affiché avec le rendu ; un ticket écarté par une
  imprimante en panne est remis en file quelques minutes.
- Registre fiscal : les ventes dont le scellement a échoué sont rescellées au lancement.
- Shopify : un 502/504 sur l'envoi d'une vente est tenu pour « issue inconnue » (jamais rejoué à l'aveugle).

---

## [1.0.44] - 2026-08-29

### 🚀 Intégrations & Correctifs Majeurs
- **Synchronisation Shopify Live (Admin API 2025-01) :**
  - Ajout du support de contexte SSL universel (`get_ssl_context()`) contournant le blocage de certificats racine sur macOS.
  - Détection automatique et temps réel des dépôts et inventaires (`locations.json`).
  - Déduction de stock bidirectionnelle magasin <-> web et import de catalogue direct.
  - Résolution des requêtes SQL d'enregistrement des paramètres SQLite (`shop_name`, `shopify_store_url`, `shopify_access_token`).
- **Stabilisation du Moteur d'Auto-Update :**
  - Correction de la boucle d'alerte de version et comparaison SemVer ascendante.
  - Persistance de la version installée dans la base SQLite locale et dans les caches de session.

---

## [1.0.43] - 2026-08-24

### 🐛 Correctifs & Améliorations IHM
- **Stabilisation des Modales de Modification & Déclinaisons :**
  - Correction de la synchronisation réactive dans `DeclinationBuilder.tsx` avec écoute dynamique de `sizesString`.
  - Intégration d'un parseur robuste multi-formats tolérant les formats avec/sans espaces et virgules.
  - Cycle de vie sécurisé de `EditProductModal` avec clé d'instance unique `key` basée sur l'ID du produit.
  - Réinitialisation systématique des sous-vues à l'ouverture pour `NewProductModal`, `ClientModal`, `UsersModal` et `CategoriesModal`.

---

## [1.0.18] - 2026-08-14

### 🐛 Correctifs de Bugs Critiques (Hotfixes)
- **Persistance Défaillante (Ghost Data) :**
  - Réinitialisation des états React à vide `[]` au lieu des tableaux statiques `INITIAL_PRODUCTS` / `INITIAL_CLIENTS`.
  - Ajout du chargement dynamique `posApi.getCategories()` dans `React.useEffect`.
  - Suppression des fallbacks aveugles vers les données de démo dans `services/api.ts`.
  - Sécurisation des handlers de suppression `handleDeleteProduct`, `handleDeleteCategory`, `handleDeleteUser`, et `handleDeleteHeldTicket` avec validation `async/await` et messages d'erreur.
  - Correction de la conversion de l'ID des tickets en attente dans `server_pos.py` (`re.findall(r'\d+', ticket_id)`) pour supporter les formats `ht_101` et `ht-101`.

- **Échec du Déclencheur de Mise à Jour (Dead Update Trigger) :**
  - Normalisation des champs d'URL d'installation (`patchUrl`, `downloadUrl`, `targetVersion`) dans `ParametresView.tsx`.
  - Intégration de `get_target_dist_dir()` dans `services/update_checker.py` résolvant le répertoire de destination vers le cache inscriptible de l'utilisateur (`~/Library/Caches/KodoPOS/dist`) pour éliminer l'erreur `PermissionError: [Errno 13] Access Denied` dans le bundle compilé macOS.
  - Ajout d'une redirection de secours via `window.location.href` lorsque `window.open` est intercepté.

### 🚀 Nouveautés & Portage Multi-Plateformes
- **Support Natif Windows :**
  - Intégration du support dynamique `%APPDATA%\Kodo_POS` et `%LOCALAPPDATA%` dans `core/config.py` pour isoler la base SQLite `kodo_pos.db` et garantir la persistance NTFS sous Windows.
  - Mise à jour de la configuration de build PyInstaller `Kodo_POS_Windows.spec` et du script d'automatisation `build_windows.bat`.
- **Publication & Manifestes Web :**
  - Création du manifeste de version Web JSON (`/public/latest.json`) et de l'endpoint Next.js `/api/version` exposant les binaires macOS et Windows.
