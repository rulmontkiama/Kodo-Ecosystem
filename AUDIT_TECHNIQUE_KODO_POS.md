# Audit technique — Kōdo POS

**Date :** 20 septembre 2026
**Version auditée :** 1.0.73 (`main`, commit `2d01f70`)
**Périmètre :** backend Python (`/Volumes/Extreme SSD/KIAMA/Kōdo POS`), frontend React/TS (`~/Desktop/kōdo-pos-3`), site Next.js `kodo-solutions-web`, chaîne de release, CI GitHub Actions, packaging macOS/Windows.

---

## 1. Synthèse

Kōdo POS est un logiciel de caisse nettement plus abouti que ce que sa taille laisse supposer. Le moteur de calcul financier est rigoureux, le système de correctifs à distance signés Ed25519 est d'un niveau professionnel rare sur ce type de produit, la logique de remboursement est réellement anti-fraude, et le code est commenté avec un soin remarquable : on y trouve documenté le *pourquoi* des décisions, y compris les incidents passés qui les ont motivées. Ce sont des fondations saines.

Le problème n'est pas la qualité du code métier, il est ailleurs : **la couche d'accès à ce code métier est entièrement ouverte**. L'API REST locale n'a aucune authentification, écoute sur toutes les interfaces réseau et accepte toutes les origines. Sur le Wi‑Fi d'une boutique, n'importe quel appareil connecté peut lire le fichier client complet, créer ou supprimer des ventes, modifier les réglages TVA et supprimer des vendeurs. C'est le point qui domine tout le reste.

Le second axe de fond concerne la revendication NF525. Le chaînage SHA‑256 des tickets est correctement implémenté comme *détecteur de corruption*, mais il ne comporte aucun secret : il est donc intégralement recalculable par quiconque accède au fichier `.db`. Il ne constitue pas une preuve d'inaltérabilité opposable, et le numéro de ticket n'est même pas scellé dans l'empreinte. L'écart entre ce que le code fait et ce que les commentaires affirment (« infalsifiabilité », « conforme NF525/LNE ») mérite d'être corrigé — dans le code, ou dans le discours.

Enfin, la chaîne de livraison n'a aucun garde-fou : ni le script de build, ni la CI GitHub Actions n'exécutent les 231 tests Python existants, et le frontend (19 000 lignes) n'a aucun test. Des régressions comme la version du bundle macOS figée à 1.0.44 ou l'export Excel cassé en production en sont la conséquence directe.

**Répartition des constats :** 3 critiques, 8 majeurs, 10 moyens, ~10 éléments de dette.

---

## 2. Ce qui est solide

Ces points méritent d'être préservés tels quels.

**Le chargeur de correctifs distants** (`patch_loader.py`, `kodo_ed25519.py`, `kodo_base.py`) est la meilleure pièce du projet. Implémentation Ed25519 en Python pur sans dépendance C, vérification de signature avant toute écriture, empreinte SHA‑256 par fichier, plage de versions de base contrôlée, liste de modules non patchables incluant la racine de confiance elle-même, compilation de validation avant activation, chargement en mémoire sans écriture de `.py` sur disque, quarantaine automatique après trois démarrages non confirmés et retour à la version précédente. La clé privée reste hors ligne. C'est une conception correcte de bout en bout.

**Le moteur de panier** (`kodo_core/domain/sales/cart_engine.py`) respecte la règle d'or : `Decimal` et `ROUND_HALF_UP` partout dans les calculs, `quantize_money` centralisé, TVA calculée par extraction depuis le TVAC. Aucun `float` dans un chemin de calcul.

**La logique de remboursement** (`database_manager.enregistrer_remboursement`) relit systématiquement le prix, le taux de TVA et le stock depuis la ligne de vente d'origine plutôt que de faire confiance à l'appelant, vérifie que la ligne appartient bien au ticket annoncé, et déduit les quantités déjà remboursées. C'est une vraie défense contre la fraude au remboursement, pas une façade.

**Le refactoring de `ShopConfig`** a été mené proprement : `core/config.py` est désormais un simple alias de réexport vers `kodo_core.config`, avec un commentaire expliquant l'incident de production qui l'a motivé. C'est le modèle à suivre pour le reste de la dette.

**231 tests Python** répartis sur 32 fichiers couvrent le panier, les clôtures, les licences, le pont Live Shopping, la file hors-ligne, les migrations et le patching. L'actif existe — il n'est simplement jamais exécuté automatiquement.

**La base de données vit hors du bundle applicatif** (`~/Documents/Kodo_POS/db/kodo_pos.db`), ce qui la fait survivre aux réinstallations. Mode WAL activé.

---

## 3. Critiques

### C1 — L'API locale est totalement ouverte sur le réseau

Trois décisions se combinent en une seule vulnérabilité.

Le serveur écoute sur toutes les interfaces : `server_pos.py` lignes 308 et 326 instancient `ReusableHTTPServer(('0.0.0.0', port), ...)`. Le message affiché au démarrage annonce pourtant `http://localhost:8765`, ce qui masque la portée réelle.

Aucune authentification n'existe, et surtout **aucune ne peut exister** en l'état : `KodoAPIApp.handle_request` reçoit bien les en-têtes HTTP en paramètre, mais ne les transmet jamais aux routeurs (`kodo_core/api/app.py` ligne 52 : `handler(method, normalized_path, query, data)`). Aucun handler ne peut donc lire un jeton, même si on voulait en ajouter un.

Les en-têtes CORS autorisent toutes les origines (`server_pos.py` ligne 126 : `Access-Control-Allow-Origin: *`), ce qui permet à n'importe quelle page web ouverte sur le poste de caisse d'interroger l'API et d'en lire les réponses.

Conséquence concrète : depuis un téléphone connecté au Wi‑Fi de la boutique, `GET http://<ip-du-mac>:8765/api/clients` renvoie l'intégralité du fichier clients (noms, téléphones, e‑mails, historiques d'achat, points de fidélité). `DELETE /api/users?id=1` supprime un vendeur. `POST /api/settings` modifie le numéro de TVA. Sur le plan RGPD, c'est une exposition de données personnelles par défaut, sans mesure technique de protection.

**Correctif immédiat** (quelques lignes, sans refonte) : remplacer `'0.0.0.0'` par `'127.0.0.1'` — le frontend et l'afficheur client tournent sur la même machine, `ShopConfig.get_host()` existe déjà pour rendre cela configurable si un second écran distant en a besoin. Puis restreindre CORS à `http://localhost:8765`.

**Correctif de fond** : transmettre `headers` aux handlers dans `app.py`, et introduire un jeton de session émis par `/api/pin/verify`, exigé sur toutes les routes hors `/api/status`. Si l'écoute réseau est nécessaire pour l'afficheur client ou une tablette, elle ne doit être activée qu'avec authentification.

### C2 — Le code PIN est cassable en quelques secondes

`hash_pin` (`kodo_core/db/connection.py` ligne 29 et son doublon `database_manager.py` ligne 69) calcule `SHA-256(pin + sel)` où le sel est une constante globale (`"KODO_POS_SECURE_SALT_2026"`, surchargeable par `KODO_SALT`). Le PIN fait exactement 4 chiffres (validé en dur dans `system_routes.py`). L'espace des possibles est donc de 10 000 valeurs, et une table arc-en-ciel complète se construit instantanément à partir du sel — lui-même extractible du binaire.

`/api/pin/verify` n'applique aucune limitation de tentatives : la recherche `rate.?limit|tentative|attempt|lockout|throttle` ne remonte rien dans `kodo_core/`. Combiné à C1, un attaquant sur le réseau local énumère les 10 000 PIN en quelques secondes et obtient le rôle Gérant.

Deux aggravants. Le PIN sert d'identifiant : la colonne est `pin TEXT UNIQUE NOT NULL` et la vérification se fait par `SELECT ... WHERE pin = ?`, sans nom d'utilisateur — le PIN est donc à la fois le login et le mot de passe. Et le PIN administrateur par défaut est `0000` (`migrations.py` lignes 903 et 932).

Enfin, le verrouillage est purement cosmétique côté client : `App.tsx` gère un `isLocked` initialisé depuis `localStorage`, et aucun contrôle de rôle n'existe dans le frontend (`plan_permissions.json` est chargé par `license.py` mais aucune vérification `role === 'Gérant'` n'apparaît dans `src/`). Rafraîchir la page ou appeler l'API directement contourne le verrou.

**Correctif** : passer à PBKDF2 ou scrypt avec un sel par utilisateur (SHA‑256 nu est inadapté à un secret à faible entropie) ; ajouter un verrouillage progressif après 5 échecs ; dissocier identité et secret ; forcer le changement du PIN `0000` au premier démarrage ; appliquer les rôles côté serveur, pas côté React.

### C3 — Le chaînage cryptographique n'est pas opposable

L'implémentation est correcte comme chaîne de hachage : `calculer_hash_transaction` produit `SHA-256(previous_hash | timestamp | montant | caisse_id | details_articles)`, chaque ticket reprend l'empreinte du précédent, et `verify_database_integrity` rejoue la chaîne en gérant la rétrocompatibilité avec l'ancienne formule. Le travail est soigné.

Mais **il n'y a aucun secret dans le calcul**. Toutes les entrées sont des données publiques stockées dans la même base. Quiconque ouvre `kodo_pos.db` avec un client SQLite peut modifier un montant, recalculer l'empreinte de la ligne et toutes les suivantes, et obtenir une chaîne qui se vérifie parfaitement. Le dispositif détecte une corruption accidentelle ; il ne détecte pas une fraude délibérée, qui est précisément ce que NF525 exige. La colonne s'appelle `signature` mais ne contient pas de signature : dans `record_audit_event`, `signature` reçoit littéralement la valeur de `current_hash`.

S'ajoutent trois faiblesses de détail :

Le numéro de ticket n'est pas scellé. `signer_ticket(cursor, numero_ticket, total_tvac, date_heure, ...)` accepte `numero_ticket` en paramètre et **ne l'utilise jamais** dans le calcul — vérifiable dans les deux implémentations (`kodo_core/db/audit_trail.py` ligne 43 et `database_manager.py` ligne 624). Une renumérotation de tickets ne casse donc pas la chaîne, alors que la continuité de la séquence est un pilier de NF525.

Le hash de clôture (`calculer_hash_cloture`) ne couvre que `date | caisse | total_tvac | espèces | carte`. Le total TVA, le nombre de tickets, le fond de caisse réel et l'écart restent modifiables sans rupture du sceau.

`verifier_chainage` laisse deux trous : la première ligne examinée n'est jamais vérifiée (`if last_sig is None: expected_hash_prec = actuel_hash_prec`, lignes 131‑132), et toute ligne dont la signature est `NULL` est ignorée par un `continue` (ligne 125‑126) — vider la colonne suffit à sortir une ligne du contrôle.

**Correctif** : remplacer le hash nu par un HMAC‑SHA256 avec une clé générée à l'installation et stockée dans le Trousseau macOS / DPAPI Windows, ou mieux, signer chaque ticket avec une clé Ed25519 par installation (la brique `kodo_ed25519` est déjà là). Inclure `numero_ticket` dans le payload. Traiter une signature `NULL` comme une erreur, pas comme une exemption. Vérifier réellement la première ligne contre le bloc genesis.

**Point de vigilance, hors code** : la conformité NF525 est une certification délivrée par un organisme accrédité (LNE ou Infocert), ou couverte par une attestation individuelle de l'éditeur engageant sa responsabilité. Les commentaires du code affirment « Conforme NF525/LNE » ; tant que la certification n'est pas obtenue, cette formulation expose juridiquement — et le client qui l'utiliserait en cas de contrôle fiscal serait exposé aussi. Je ne suis pas juriste et ce point mérite une vérification auprès d'un conseil, mais il vaut la peine d'être posé maintenant plutôt qu'après un contrôle. À noter par ailleurs que la Belgique, où opère la boutique cliente, relève du système SCE / boîte noire pour les secteurs concernés, pas de NF525 qui est un référentiel français.

---

## 4. Majeurs

### M1 — Perte silencieuse des réglages

`src/services/api.ts`, `saveSettings` :

```ts
    return res.ok;
  } catch {
    return true; // Saved in localStorage
  }
```

Si le backend est injoignable ou renvoie une erreur réseau, la fonction retourne `true` et l'interface confirme l'enregistrement. Les réglages ne sont que dans `localStorage` ; SQLite ne les a jamais reçus. C'est exactement la classe de bug « ghost data » corrigée en v1.0.18, réintroduite ici. Le correctif est d'une ligne : `return false`.

### M2 — L'export Excel des clôtures Z est cassé en production

`kodo_core/domain/accounting/z_report.py` ligne 271 fait `import pandas as pd` dans `export_z_reports_excel`. Or `Kodo_POS.spec` exclut explicitement `pandas` et `numpy` de l'analyse PyInstaller. Vérification sur le livrable : l'archive `Installation_Kodo_POS_macOS.zip` contient 158 fichiers `.so` et les paquets à extensions compilées présents (PIL : 64 entrées, qrcode : 107) apparaissent bien dans le listing — `pandas` et `numpy` retournent zéro entrée. `pandas` embarque des extensions compilées, il serait donc visible s'il était présent.

L'import étant local à la fonction, l'application démarre normalement et l'échec ne survient qu'au clic sur « exporter en Excel », avec un `ModuleNotFoundError`. Deux options : réintégrer pandas au bundle (coûteux, ~50 Mo), ou réécrire l'export avec `openpyxl` seul, déjà présent dans les `hiddenimports` et déjà utilisé par `export_manager.py`. La seconde est préférable.

### M3 — Version du bundle macOS figée à 1.0.44

`Kodo_POS.spec`, bloc `BUNDLE` : `CFBundleShortVersionString: '1.0.44'` et `CFBundleVersion: '1.0.44'`, alors que `kodo_base.BASE_VERSION` vaut `1.0.73`. macOS, le Finder et tout mécanisme d'installation voient donc une application 1.0.44 depuis 29 versions. La procédure de release décrite dans `CLAUDE.md` énumère cinq fichiers à incrémenter et oublie le `.spec`. Le plus sûr est de faire lire la version au `.spec` depuis `kodo_base.BASE_VERSION` plutôt que de la maintenir à la main.

### M4 — Aucune vérification automatique dans la chaîne de livraison

`.github/workflows/build-windows.yml` installe les dépendances, initialise les bases et compile — il n'exécute **aucun test**. `pytest` n'est d'ailleurs pas dans `requirements.txt`, la CI ne pourrait donc pas les lancer sans modification. `build_final_pro.sh` ne lance ni `npm run lint` ni les tests Python avant de produire le DMG. Le frontend, 19 000 lignes réparties sur des composants allant jusqu'à 1 752 lignes, ne contient aucun fichier de test.

Résultat : les 231 tests existants ne protègent rien, et les régressions M2, M3 et M1 sont passées en production. Ajouter `pytest -q` et `npm run lint` comme étapes bloquantes de la CI, et les mêmes commandes en tête de `build_final_pro.sh`, est le changement au meilleur rapport effort/bénéfice de tout cet audit.

### M5 — Le serveur est mono-thread : une requête lente fige la caisse

`server_pos.py` utilise `HTTPServer`, qui traite une requête à la fois. Une impression thermique vers une imprimante réseau absente (timeout socket), une synchronisation Shopify, une génération de PDF de bordereaux ou un export comptable bloquent toutes les autres requêtes. Pendant ce temps l'interface de caisse ne répond plus, y compris pour l'afficheur client qui interroge l'API en boucle. Remplacer par `ThreadingHTTPServer` est une modification d'une ligne, mais elle impose de vérifier la sécurité concurrente des accès SQLite (voir M6 et le point sur le chaînage ci-dessous).

À noter qu'en l'état mono-thread, la séquence `SELECT ... ORDER BY id DESC LIMIT 1` puis `INSERT` utilisée pour chaîner tickets et ledger est protégée de fait. Dès que le serveur devient multi-thread — ou dès que deux instances tournent en parallèle, ce que la logique de réutilisation de port rend possible — deux écritures peuvent reprendre la même empreinte précédente et forker la chaîne. Le passage au multi-thread doit donc s'accompagner d'une transaction `IMMEDIATE` autour du couple lecture/écriture.

### M6 — Des montants stockés en `float`, contre la règle d'or du projet

`database_manager.py`, dans `enregistrer_vente`, insère le montant du ledger converti en flottant :

```python
        cursor.execute("""
            INSERT INTO Ledger_Caisse (vendeur, type_mouvement, montant, ...)
            VALUES (?, 'VENTE', ?, ?, ?, ?, ?, ?, ?)
        """, (vendeur_nom, float(montant_reel), methode, ...))
```

La signature a pourtant été calculée juste avant sur le `Decimal` exact. La colonne est déclarée `DECIMAL` et un adaptateur SQLite `Decimal ↔ TEXT` est enregistré : passer `montant_reel` directement aurait fonctionné. En l'état, la valeur scellée et la valeur stockée peuvent diverger sur les montants dont la représentation binaire n'est pas exacte. C'est une violation directe de la règle n°4 de `CLAUDE.md`.

La même logique se retrouve à la sérialisation : `json_serial` convertit tout `Decimal` en `float`, et `cart_engine.to_dict()` comme `z_report` produisent des `float` pour le JSON (38 occurrences dans `cart_engine.py`, 20 dans `z_report.py`). Pour l'affichage c'est acceptable, mais tout montant qui repart en écriture après un aller-retour JSON perd sa garantie décimale. Sérialiser les montants en chaînes (`"12.34"`) supprime la classe entière de problème.

### M7 — Deux sources de vérité pour les métadonnées de release

`public/latest.json` et `src/app/api/version/route.ts` décrivent la même release et doivent être édités à la main tous les deux — `CLAUDE.md` le documente explicitement. La route Next.js contient en plus `has_update: true` en dur : elle annonce donc une mise à jour disponible même à un client déjà en 1.0.73. Selon la façon dont `updater.py` exploite ce champ, cela peut produire une invite de mise à jour permanente. Faire lire `latest.json` par la route Next.js élimine la duplication et le risque de dérive.

### M8 — Les pannes backend sont indiscernables d'un catalogue vide

`src/services/api.ts` contient 47 blocs `catch`, dont 14 retournent `[]` et d'autres `false`. Un backend arrêté, une base verrouillée ou une erreur 500 produisent donc exactement le même résultat visuel qu'un catalogue réellement vide : une liste vide, un `console.warn`, et aucun signal pour la caissière. Dans un contexte de vente, c'est un scénario de perte de chiffre d'affaires — l'article est en stock mais introuvable à l'écran. Il faut distinguer « pas de données » de « erreur », et afficher un bandeau de perte de connexion au backend.

---

## 5. Moyens

**Contrôle anti-path-traversal imparfait.** `server_pos._serve_static` valide par `file_path.startswith(real_dist)`, sans exiger un séparateur. Un répertoire frère nommé `dist-quelquechose` passerait le contrôle. `os.path.commonpath` ou un `startswith(real_dist + os.sep)` corrige.

**Aucune limite de taille de requête.** `body_bytes = self.rfile.read(length)` lit la valeur de `Content-Length` fournie par le client sans plafond : une requête annoncée à plusieurs gigaoctets épuise la mémoire du processus.

**`_free_port_from_zombie` tue un processus tiers.** Si le port 8765 est occupé par une autre application qui ne répond pas à `/api/status`, elle reçoit un `SIGKILL`. Il faudrait vérifier que le processus visé est bien une instance de Kōdo avant de le tuer, ou choisir un autre port.

**Les exceptions internes fuient vers le client.** Motif `except Exception as e: return 500, {"error": str(e)}`, visible notamment dans `live_bridge_routes.py` ligne 72 : les messages d'erreur SQLite et les chemins de fichiers du poste remontent à l'interface. À journaliser côté serveur et à remplacer par un message générique côté client.

**Application non notarisée.** `build_final_pro.sh` lignes 112 et 119 signent en ad‑hoc (`codesign --sign -`) avec `|| true`, donc l'échec est silencieux. Sans Developer ID ni notarisation, les utilisateurs rencontrent les avertissements Gatekeeper, et l'expérience d'installation en pâtit. `--deep` est par ailleurs déprécié par Apple.

**`NSAllowsArbitraryLoads: True`** dans le `Info.plist` désactive App Transport Security globalement. `NSAllowsLocalNetworking`, déjà présent, suffit pour le trafic vers `localhost`.

**Le jeton Shopify transite et réside dans `localStorage`.** `api.ts` écrit `kodo_shopify_token` en clair côté navigateur et le renvoie au backend. Un jeton Admin API Shopify donne accès à la boutique en ligne. Il devrait rester exclusivement côté serveur, le frontend ne recevant qu'un booléen `shopifyConnected`.

**`/api/pos-capture` écrit dans Firestore sans protection.** L'endpoint public du site Next.js crée un document `prospects` à chaque POST, sans limitation de débit, sans captcha ni validation d'e‑mail. Il est trivial d'y injecter des milliers d'enregistrements.

**Le pont Live Shopping accepte un fichier non signé.** `LiveBridge._parse_file` calcule une empreinte SHA‑256 canonique du JSON, mais celle-ci sert à la déduplication, pas à l'authentification. Le fichier revient d'une application Vercel déployée par le client ; `apply_import` en reprend `discount_percent` (`live_bridge.py` ligne 768) pour calculer le ticket. Un fichier modifié peut donc imposer une remise arbitraire. Les prix, eux, sont bien relus depuis le POS — c'est la bonne décision, à étendre à la remise, ou à borner par un plafond configurable.

**`ensure_schema` fait des `ALTER TABLE` hors du système de migrations.** `live_bridge.py` lignes 125‑206 modifie le schéma à l'exécution, en parallèle de `kodo_core/db/migrations.py`. Deux mécanismes de migration concurrents rendent l'état réel du schéma difficile à raisonner.

---

## 6. Dette technique

**Environ 6 500 lignes de code mort toujours présentes.** `main_app.py` (3 056 lignes) et `views/` (~3 500 lignes) constituent l'ancienne interface customtkinter. Plus rien dans le chemin d'exécution réel (`launch_app.py` → `patch_loader` → `server_pos` → `kodo_core`) ne les importe ; seuls `views/` et `main_app.py` s'importent mutuellement. Ils continuent pourtant d'être analysés au build et de brouiller toute recherche dans le code.

**`database_manager.py` (1 364 lignes) duplique `kodo_core/db/`.** `signer_ticket`, `signer_ledger`, `signer_rapport_z`, `hash_pin` et `SafeConnection` y existent en double des versions de `kodo_core/db/connection.py` et `audit_trail.py`. `system_routes.py` importe la version `database_manager`, `migrations.py` importe celle de `kodo_core`. Les deux `hash_pin` coïncident aujourd'hui par défaut, mais divergent dès que `KODO_SALT` est défini : la version `database_manager` ignore la variable d'environnement et utilise le sel en dur. Tout PIN créé par un chemin cesserait alors d'être vérifiable par l'autre. Le modèle de correction est celui déjà appliqué à `core/config.py` : réduire `database_manager.py` à un alias de réexport.

**Le dépôt embarque des binaires volumineux.** `public/Installation_Kodo_POS.dmg` (37 Mo), deux ZIP macOS de 33 Mo chacun, et surtout `.npm-cache/` — listé dans `.gitignore` mais **déjà suivi par git**, avec des blobs de 24 Mo et 3 Mo. `git rm -r --cached .npm-cache` règle le suivi ; l'historique, lui, reste lourd.

**Code mort ponctuel.** La fonction `hash_ticket` définie dans `audit_complet` (`audit_trail.py` ligne 236) n'est jamais utilisée — elle emploie l'ancienne formule et induit en erreur à la lecture. `ShopConfig.get_secret_key` est définie et jamais appelée. Les imports `INITIAL_PRODUCTS` / `INITIAL_CLIENTS` subsistent dans `api.ts` alors que les tableaux sont vides.

**Dépendances frontend inutilisées.** `@google/genai`, `express`, `dotenv`, `tsx` et `@types/express` sont des résidus du gabarit AI Studio ; aucune occurrence de `genai` ou `GEMINI` dans `src/`. `.env.example` documente des variables inexistantes. Le `package.json` s'appelle encore `react-example` et porte la version `1.0.20`, sans rapport avec la version applicative. Le script `dev` expose Vite sur `0.0.0.0`.

**`license_cache.json` est versionné** avec une empreinte machine et une signature — artefact de développement qui n'a pas sa place dans le dépôt.

**La licence repose sur un HMAC à secret embarqué.** `license.py` signe le cache local avec `SECRET_SALT` compilé dans l'application. C'est une limite intrinsèque à toute licence hors ligne, mais autant en être conscient : le contournement est à la portée de qui sait ouvrir un binaire.

**Composants frontend surdimensionnés.** `ParametresView.tsx` (1 752 lignes), `api.ts` (1 594), `LiveShoppingView.tsx` (1 523), `CaisseView.tsx` (1 236), `App.tsx` (1 113). Sans tests, ces fichiers sont difficiles à modifier sans risque — ce qui explique la nature des régressions observées.

---

## 7. Plan d'action proposé

**Immédiat — à faire cette semaine.** Basculer l'écoute sur `127.0.0.1` et restreindre CORS. Corriger `saveSettings` (`return false`). Corriger la version dans `Kodo_POS.spec`. Ajouter `pytest` à `requirements.txt` puis `pytest -q` et `npm run lint` en étapes bloquantes de la CI et de `build_final_pro.sh`. Ces cinq actions représentent moins d'une journée et suppriment l'exposition réseau la plus large ainsi que la principale cause de régression.

**Court terme — deux à trois semaines.** Introduire un jeton de session : transmettre `headers` aux handlers dans `app.py`, émettre un jeton depuis `/api/pin/verify`, l'exiger partout ailleurs. Durcir le PIN (PBKDF2, sel par utilisateur, verrouillage après échecs, changement obligatoire de `0000`). Réécrire l'export Excel sur `openpyxl`. Distinguer erreur et vide dans `api.ts` avec un indicateur de connexion backend. Sortir le jeton Shopify du navigateur.

**Moyen terme — un à deux mois.** Reprendre le scellement fiscal : HMAC ou Ed25519 avec clé par installation, `numero_ticket` inclus dans le payload, `NULL` traité comme une erreur, première ligne réellement vérifiée. Passer en `ThreadingHTTPServer` avec transactions `IMMEDIATE` sur le chaînage. Supprimer `main_app.py` et `views/`, réduire `database_manager.py` à un alias. Purger `.npm-cache` du suivi git. Mettre en place la signature Developer ID et la notarisation.

**À trancher hors code.** La position sur NF525 : soit engager la certification, soit ajuster la formulation dans le code, la documentation et le discours commercial. Et vérifier le référentiel applicable en Belgique pour la boutique cliente, qui n'est pas le même qu'en France.

---

*Audit réalisé par lecture du code source. Aucun test dynamique, aucune exécution de test et aucune modification n'ont été effectués sur le projet.*
