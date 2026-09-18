# Instructions Claude Code — Kōdo POS (Fullstack Ecosystem)

Bienvenue sur le projet **Kōdo POS**. Tu es l'ingénieur principal en charge de l'ensemble de l'écosystème : **Frontend React/TypeScript**, **Backend Python/SQLite**, **Impression thermique ESC/POS**, **Synchronisations** et **Packaging de déploiement**.

---

## 🌐 ARCHITECTURE GLOBALE & PÉRIMÈTRE D'ACTION TOTAL

Tu as un accès et une responsabilité **pleine et entière sur l'ensemble des composants** du projet. Ne te limite jamais à un sous-dossier restreint : investigue, modifie et corrige partout où le besoin s'en fait sentir.

### 1. Frontend React / TypeScript (`/Users/kiamarulmont/Desktop/kōdo-pos-3`)
- **Framework** : React 19, Vite, Tailwind CSS, Lucide Icons, Recharts.
- **Dossiers clés** :
  - `src/components/CaisseView.tsx` : Encaissement, panier multi-onglets, raccourcis clavier, fast-scan code-barres.
  - `src/components/StocksView.tsx` : Gestion des stocks, inventaire, seuils d'alerte, déclinaisons.
  - `src/components/ParametresView.tsx` : Réglages boutique, TVA, seuil d'alerte global, options de vente, imprimante, Shopify.
  - `src/components/Modals/` : Toutes les fenêtres modales (`SizeSelectorModal`, `NewProductModal`, `WhatsNewModal`, `UpdateAvailableModal`, `LockModal`, etc.).
  - `src/services/api.ts` : Communication REST avec le serveur local backend (`/api/*`).
  - `src/types.ts` : Définitions TypeScript partagées (`Product`, `CartItem`, `SaleTransaction`, etc.).

### 2. Backend Python & Serveur Local (`/Volumes/Extreme SSD/KIAMA/Kōdo POS`)
- **Moteur** : Python 3.12, SQLite local (`ladresse_b.db` / `kodo_pos.db`), PyInstaller, CUPS macOS.
- **Dossiers & Fichiers clés** :
  - `kodo_core/api/routes/pos_routes.py` : Routes API locales pour les ventes, produits, clients, tickets, clôtures Z.
  - `kodo_core/services/updater.py` : Moteur de mise à jour automatique OTA via GitHub Releases / `latest.json`.
  - `ticket_printer.py` / `imprimer_ticket_soldes.py` : Moteurs d'impression thermique ESC/POS.
  - `database_manager.py` : Schéma SQLite, initialisation et intégrité des données.
  - `build_final_pro.sh` : Script de compilation PyInstaller et génération du DMG macOS livrable.
  - `public/` : Fichiers de mise à jour (`latest.json`, `dist_vX.X.XX.zip`).

---

## ⚡ RÈGLES D'OR DE DÉVELOPPEMENT & SÉCURITÉ

### 1. Vérification TypeScript OBLIGATOIRE
- Avant de valider ou de packager une modification frontend, exécute TOUJOURS :
  ```bash
  cd /Users/kiamarulmont/Desktop/kōdo-pos-3 && npm run lint
  ```
  (équivalent à `tsc --noEmit`). **Le code doit impérativement compiler avec 0 erreur**.
- Ne te fie jamais uniquement à `vite build` : Vite peut tolérer certaines erreurs d'identifiants non déclarés qui se transformeront en écran blanc (`ReferenceError`) pour l'utilisateur final.

### 2. Règle absolue sur les Imports (React & Lucide)
- À chaque ajout d'icône (`lucide-react`) ou de hook React (`useCallback`, `useMemo`, `useState`, `useRef`, `useEffect`), **vérifie explicitement sa présence dans les imports au sommet du fichier**.
- Porte une attention maximale aux constantes déclarées au niveau racine des modules (comme `STATIC_RELEASES_HISTORY` dans `WhatsNewModal.tsx`), car une variable manquante fait crasher l'application entière dès le démarrage.

### 3. Gestion des Tailles & Déclinaisons
- Format standard dans Kōdo POS : `NOM_TAILLE:QUANTITE | NOM_TAILLE:QUANTITE` (ex: `S:4 | M:6 | L:2`).
- Lors d'une vente, la taille choisie est portée par `CartItem.selectedSize`, transmise dans `saleData` et décomptée précisément de `prod.sizes`.

### 4. Précision Financière Backend
- Dans le code Python, interdiction d'utiliser des `float` pour les calculs de montants, centimes, remises ou TVA. Utilise toujours `decimal.Decimal` avec arrondi `ROUND_HALF_UP` à 2 décimales.

---

## 🚀 PROCÉDURE DE LIVRAISON / MISE À JOUR (RELEASE)

Pour déployer une nouvelle version (ex: `v1.0.XX`) :
1. **Vérifier les types** : `npm run lint` dans `kōdo-pos-3` (0 erreur).
2. **Compiler le frontend** : `npm run build` dans `kōdo-pos-3`.
3. **Mettre à jour le dist de l'app** : Copier le contenu de `kōdo-pos-3/dist` vers `/Volumes/Extreme SSD/KIAMA/Kōdo POS/dist`.
4. **Créer le zip OTA** : Compresser `dist/` vers `public/dist_v1.0.XX.zip`.
5. **Incrémenter les versions** :
   - `public/latest.json` : nouvelle version, lien zip et changelog détaillé.
   - `kodo_core/services/updater.py` : `CURRENT_VERSION = "1.0.XX"`.
   - `src/components/Modals/WhatsNewModal.tsx` & `Sidebar.tsx` & `App.tsx`.
6. **Git** : Committer, tagger `v1.0.XX` et pousser sur GitHub (`git push origin main && git push origin v1.0.XX`).
