# Changelog - Kōdo POS

Toutes les modifications notables apportées au projet Kōdo POS sont documentées dans ce fichier.

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
