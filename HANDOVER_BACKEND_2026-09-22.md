# PASSATION — Chantier backend / Shopify / comptabilité

**Date : 2026-09-22** · Branche : `main` · Dernier commit : `a558d55 chore(release): v2.0.3`

> Ce document est le point d'entrée d'une nouvelle conversation. Il décrit ce qui a été fait,
> ce qui reste, et les règles à ne pas enfreindre. Le prompt d'amorçage est en section 7.

---

## 0. État en une ligne

**492 tests backend verts** (458 au début du dernier tour) · `tsc --noEmit` **0 erreur** ·
`git diff --stat` = **24 fichiers, +4 147 / −459**, plus **9 fichiers de tests neufs** ·
**RIEN N'EST COMMITÉ** · aucun DMG, aucune release, aucun appel réseau réel vers Shopify.

---

## 1. CE QUI A ÉTÉ FAIT

### 1.1 Shopify n'était pas branché du tout — corrigé

Le moteur de synchronisation existait et était correct. **Rien ne l'appelait.** Le seul code qui
démarrait le thread vivait dans `main_app.py`, l'ancienne interface Tkinter (~6 700 lignes) qui
ne s'exécute plus depuis le passage au serveur local. Le chemin réel de production est :

```
launch_app.py → server_pos.py::run_server → kodo_app.handle_request
```

Conséquence : l'écran Réglages affichait « connecté » et **aucun stock n'a jamais circulé, dans
aucun sens**.

- Corrigé dans `server_pos.py` : le thread démarre au lancement du serveur.
- Démarrage **conditionnel** : uniquement si `Parametres.shopify_store_url` **et**
  `shopify_access_token` sont renseignés en base. **Rien n'est codé en dur** — c'est la clé
  enregistrée dans les paramètres qui désigne la boutique, et elle seule (consigne explicite de
  l'utilisateur).
- Une caisse non configurée ouvre **zéro connexion réseau** (mesuré : test qui empoisonne
  `urlopen` et compte les appels).

### 1.2 Les remboursements en ligne étaient structurellement invisibles — corrigé

`_fetch_all_orders` filtre sur `financial_status=paid` **et** `fulfillment_status=unfulfilled`.
Une commande remboursée quitte **ces deux états**. Le remboursement survenait donc au moment
exact où la commande devenait invisible : la vente restait dans le rapport Z et dans la TVA
déclarée, et l'article rendu ne revenait jamais en rayon.

- Passe **distincte** : `orders.json?status=any&updated_at_min=…`
- Repère `shopify_remboursements_verifies_jusqua` qui **n'avance qu'après une fenêtre
  entièrement traitée** (sinon un remboursement raté serait sauté définitivement).
- Fenêtre `FENETRE_REMBOURSEMENTS_JOURS = 90`, recouvrement 60 s (horloges non alignées).
- **Migration 2.0.6** : table `Shopify_Remboursements` (clé primaire = garantie d'idempotence).

**Trois arbitrages qui engagent la comptabilité — NE PAS LES INVERSER SANS DÉCISION EXPLICITE :**

1. **Une annulation sans remboursement n'est JAMAIS convertie en remboursement.** Shopify permet
   d'annuler en conservant le paiement. Fabriquer un mouvement d'argent et le sceller dans la
   chaîne NF525 serait une faute. Elle est signalée (`requires_stock_audit`), pas inventée.
2. **`restock_type: no_restock` rembourse l'argent sans remettre l'article en rayon.** Sinon on
   crée une pièce fantôme, repoussée ensuite vers Shopify et vendue une seconde fois.
3. **Shopify ne peut pas rembourser plus que ce que la caisse a vendu.** L'excédent est signalé,
   jamais absorbé.

### 1.3 Rattachement des lignes de commande — le SKU/code-barres n'a plus à être inversé

Point différé de longue date : SKU et code-barres étaient inversés. **Retourner le champ aurait
dédoublé le catalogue de toute base déjà synchronisée.** Résolu autrement :

`_resoudre_ligne_stock` rattache désormais par ordre de fiabilité :
1. **`variant_id`** via `Shopify_Variantes` JOIN `Produits` — la seule clé que Shopify ne laisse
   pas modifier ;
2. `sku`, puis `barcode`, sur `Produits.code_barre` ;
3. repli par nom **uniquement s'il n'existe qu'un seul homonyme**.

Prouvé par un test où le SKU **et** le titre changent côté Shopify. **Aucune migration risquée.**

### 1.4 `services/shopify_service.py` était un placebo — corrigé

`_execute_task` avait pour corps un `time.sleep(0.5)` commenté « Simulation d'un appel d'API REST
Shopify ». La file tournait, la caisse croyait synchroniser, rien n'était envoyé. Ce module est
maintenant **la file des corrections d'inventaire uniquement** (quantité comptée imposée à la
boutique). Ventes et remboursements passent en **relatif** par `kodo_core.sync.shopify`, avec
journal d'idempotence. **Ne jamais mélanger les deux : cela recrée la double décrémentation.**

### 1.5 Sécurité

- **Le jeton d'administration Shopify ne sort plus de la base.** `GET /api/settings` le renvoyait
  en clair à chaque ouverture des Réglages ; le frontend le recopiait dans `localStorage` (hors
  base, hors sauvegarde, sans expiration). Le serveur renvoie maintenant
  `shopifyTokenEnregistre: bool`. **Les jetons déjà posés dans les navigateurs installés sont
  purgés au premier démarrage** (`localStorage.removeItem("kodo_shopify_token")` au niveau module
  de `api.ts`). Garde-masque côté `POST /api/settings` : un client renvoyant les puces affichées
  ne peut plus écraser la vraie clé.
- **TLS rétabli** sur tous les canaux sensibles, dont les derniers : `main_app.py`
  (`_tester_connexion_shopify` forçait `CERT_NONE` alors que le jeton partait dans la requête),
  `license.py` (clé de licence + empreinte matérielle en clair, réponse forgeable), et
  `offline_engine.py` (un portail captif répondait 200 et était pris pour un accès Internet).
  Tous utilisent `kodo_core.services.updater.build_ssl_context()`.
- Écoute sur **127.0.0.1**, CORS restreint aux origines locales, refus des requêtes
  `Sec-Fetch-Site: cross-site` sur `/api/`, refus des écritures à origine non autorisée.

### 1.6 Comptabilité et scellement fiscal

- **`build_hash_payload` pouvait produire deux empreintes pour un même montant.** Il utilisait
  `f"{Decimal(v):.2f}"` : `Decimal()` sur un `float` scelle la valeur **binaire**, et `:.2f`
  applique l'arrondi **bancaire** (ROUND_HALF_EVEN). Mesuré : **5 000 divergences sur 100 000
  montants**. Corrigé par `_montant_canonique` en `ROUND_HALF_UP`.
  **Vérifié sur 20 000 montants déjà scellés : texte identique au caractère près — aucun passé
  réécrit.**
- `quantize_money` (dans `kodo_core/domain/sales/models.py`) est désormais **la référence unique
  d'arrondi du projet**. `cart_service`, `closing_service`, `fiscal_service` l'utilisent.
  `fiscal_service` faisait `Decimal(amount)` sur un float : 2.675 y devenait 2.67 quand tous les
  autres chemins rendaient 2.68 — un centime d'écart **au moment même du scellement**.
- `database_manager.enregistrer_remboursement` accepte `recrediter_stock=True/False` (nécessaire
  pour `no_restock`).

### 1.7 Matériel — la détection d'imprimante ne marchait qu'en français

Deux défauts **actifs chez les clientes aujourd'hui** :

- `lpstat -v` était analysé par une expression régulière écrite en français (« périphérique pour
  X : … »). Sur un Mac **en anglais, néerlandais ou allemand : ZÉRO imprimante détectée**, écran
  Réglages vide.
- `lpstat -p` cherchait « idle » / « enabled ». Sur un Mac **en français**, une imprimante
  parfaitement prête était rapportée indisponible.

Corrigé par `printer_service.env_cups()` (fige `LC_ALL=C`, `LANG=C`, `LANGUAGE=""`) appliqué à
tous les appels CUPS, et une expression **structurelle** (`<nom> : <schéma>://…`) au lieu d'une
phrase. Vérifié sur 6 langues et 6 types de connexion.

### 1.8 Code-barres (tours précédents, déjà livré)

`products_routes.py`, `inventory_manager.py`, `pdf.py`, `print_worker.py` : normalisation,
génération de codes internes, unicité, recherche produit, symbologies EAN-13 / Code128 / QR,
étiquettes PDF. `print_worker` a gagné `job_type` (TICKET par défaut — chemin ticket inchangé).
**65 tests dédiés, intacts.**

### 1.9 Tests — ce qui existe et ce qui les rend crédibles

| Fichier | Tests |
|---|---|
| `tests/test_barcode_backend.py` | 65 |
| `tests/test_compta_precision.py` | 45 |
| `tests/test_shopify_sync.py` | 38 |
| `tests/test_shopify_remboursements.py` | 18 |
| `tests/test_stock_integrite.py` | 17 |
| `tests/test_shopify_cablage.py` | 13 |
| `tests/test_stock_negatif_auditable.py` | 4 |
| `tests/test_tls_canaux_sensibles.py` | 4 |
| `tests/test_remboursement_numerotation.py` | 3 |
| **Total suite** | **492** |

- **Aucun appel réseau réel vers Shopify dans aucun test.** Un faux Shopify tourne sur
  `127.0.0.1` et répond **401 à tout jeton incorrect** — un test qui « passerait » par accident
  sans authentification est impossible.
- Le **bout-en-bout** (`test_shopify_sync.py::TestBoutEnBoutSansBoutique`) va : import catalogue →
  vente en boutique → poussée du stock → commande en ligne → **retour de la cliente**. Les deux
  stocks finissent d'accord.
- **Tests de mutation** effectués sur le code de remboursement : 7 sabotages ciblés, les 5 qui
  changent réellement le comportement font tomber un test. Les 2 survivants sont des gardes
  redondantes — en les retirant **toutes les deux**, le test tombe.

---

## 2. CE QUI RESTE À FAIRE — par ordre d'urgence réelle

### P0 — Le travail n'existe nulle part ailleurs que sur ce SSD
**4 147 lignes non commitées**, sur un disque externe. Le dépôt frontend
(`/Users/kiamarulmont/Desktop/kōdo-pos-3`) **est sous git** (historique jusqu'à v2.0.3) mais
**n'a aucun remote**.
→ Commits découpés par domaine (Shopify / sécurité / comptabilité / matériel / tests), **sur une
branche, pas sur `main`**. Un commit n'est pas une release : cela ne publie rien chez les clientes.

### P1 — Le travail Shopify n'a jamais rencontré un vrai Shopify
Tout est prouvé contre un faux serveur **écrit par l'assistant lui-même**, qui répond ce que la
documentation dit que Shopify répond. Si la lecture de la documentation est fausse quelque part,
le faux serveur reproduit fidèlement l'erreur et les 492 tests la valident.
→ Une **boutique de développement Shopify est gratuite et illimitée** (compte Shopify Partners) :
pas de carte bancaire, pas de vraie boutique. **Passe en lecture seule d'abord** (aucun stock
écrit) pour confronter : forme exacte du JSON des remboursements, en-têtes de pagination,
comportement réel du quota, `restock_type` tel que Shopify l'émet. Puis écriture sur un produit
de test unique.

### P2 — Le dépôt Shopify est choisi au hasard
`get_location_id()` (`kodo_core/sync/shopify.py:452`) prend **`active_locs[0]`**, le premier dépôt
actif renvoyé par l'API, **dans un ordre non garanti**. Aucun réglage de dépôt n'existe en base
(`shopify_location_id` absent de `Parametres`).
Sans conséquence pour une boutique à un seul emplacement. Mais avec boutique **+** entrepôt, la
caisse peut écrire le stock dans le mauvais dépôt : le site affiche 0 sur celui qui sert les
commandes en ligne. Silencieux, très difficile à diagnostiquer à distance.
→ Réglage « dépôt » dans les Paramètres, alimenté par la liste que `tester_connexion` reçoit déjà,
repli automatique s'il n'y a qu'un dépôt.

### P3 — Répéter la migration 2.0.6 sur une copie de la vraie base
Elle s'exécutera chez les clientes. Testée sur bases neuves, **jamais sur une base qui a vécu deux
ans** (produits `SHPF-`, stocks orphelins, vieux enregistrements).
→ L'utilisateur **copie** `~/Documents/Kodo_POS/db/kodo_pos.db` dans le dossier temporaire de la
session ; répétition sur la copie, avant/après chiffré. **La lecture directe de cette base est
refusée par le classifieur — NE PAS CONTOURNER.**

### P4 — La licence est forgeable
`SECRET_SALT = "KODO_SECURE_LIC_SALT_2026_BELGIUM"` est en clair dans `license.py:19`, et
`DEMO-ACTIVE-2026` (`license.py:501`) active n'importe quelle copie. Qui ouvre le binaire génère
des licences valides à volonté. **Risque de chiffre d'affaires, pas de risque client.**
→ La signature **Ed25519 est déjà dans le projet** (`kodo_ed25519.py`, mises à jour) : la
réutiliser, pas la réinventer. Attention : `kodo_ed25519.py` et `license.py` — vérifier la liste
des fichiers **non patchables à distance** dans `CLAUDE.md`.

### P5 — Deux petites choses
- **`/api/shopify/test`** (`system_routes.py:660`) : si l'appelant fournit un `domain` **sans**
  jeton, le jeton stocké est complété depuis la base et envoyé **au domaine choisi par
  l'appelant**. Un site web malveillant ne peut plus déclencher ça (gardes d'origine posées dans
  `server_pos.py`), mais tout programme local le peut.
  → Ne jamais compléter avec le jeton enregistré quand le domaine demandé diffère du domaine
  enregistré. ~3 lignes.
- **Codes `SHPF-…`** : les nouveaux imports n'en fabriquent plus et `_retrouver_produit` **adopte**
  les produits historiques sans les dupliquer, mais ces codes **restent non scannables** sur les
  bases anciennes. → Conversion en EAN-13 **après** P3, pas avant.

### Hors périmètre backend
- Dépôt git de **7,4 Go** (`.git`) — à assainir.
- `kodo_core/services/stock_service.py` : **mort, sur aucun chemin de production** (il porte
  maintenant un avertissement en tête). **Non supprimé** : référencé par `SanctuaryShield`, c'est
  une décision de produit. Attention — sa signature est parfaitement crédible : y corriger un bug
  de stock ne changerait rien pour la commerçante. C'est exactement le piège qui a produit
  l'affaire Shopify.
- Défauts ouverts de la revue du 2026-09-20 encore non traités : voir la mémoire
  `project-revue-code-2026-09-20.md`.

---

## 3. CONSIGNES PERMANENTES — non négociables

1. **Précision financière.** Jamais de `float` pour un montant, un centime, une remise ou une TVA.
   `decimal.Decimal` + `ROUND_HALF_UP` à 2 décimales, via `quantize_money`.
   Pièges : `Decimal(v)` sur un float prend sa valeur **binaire** → toujours `Decimal(str(v))`.
   `f"{x:.2f}"` est l'arrondi **bancaire**, pas ROUND_HALF_UP.
   Exception assumée : les `float()` à la **frontière de sérialisation JSON**
   (`cart_engine.to_dict`, `z_report`) — frontière d'affichage, aucun calcul n'en dépend, y
   toucher casse le frontend.
2. **Frontend : `npm run lint` ET `npx tsc --noEmit` doivent rendre 0 erreur** avant toute
   validation. Ne jamais se fier à `vite build` seul (écran blanc `ReferenceError` chez la
   cliente). Vérifier explicitement chaque import React / lucide-react ajouté.
3. **Respecter l'intégrité du code existant sans le dénaturer** (consigne explicite de
   l'utilisateur). Pas de réécriture de confort, pas de refactor non demandé.
4. **Shopify vise exclusivement la boutique de `Parametres.shopify_store_url` +
   `shopify_access_token`.** Rien en dur, jamais.
5. **Tolérance zéro : vérifier et sur-vérifier.** Un test qui passe n'est pas une preuve — vérifier
   qu'il échoue quand on casse le code qu'il prétend couvrir (test de mutation).
6. Tout nouveau comportement vient avec son test. La suite doit rester à **0 échec**.

---

## 4. LIMITES ET INTERDITS

| Interdit | Raison |
|---|---|
| **Lire `~/Documents/Kodo_POS/db/kodo_pos.db`** | Refusé par le classifieur (« Production Reads »). **Ne pas contourner.** Travailler sur une copie fournie par l'utilisateur. |
| **Commit / tag / build / release non demandés** | Aucun n'a été fait. Ne rien publier sans demande explicite. |
| **Appel réseau réel vers Shopify dans un test** | Toute la suite doit rester hermétique. |
| **`kodo_release.py make-patch`** | Refusé par le classifieur malgré accord utilisateur. **C'est l'utilisateur qui le lance dans son Terminal.** |
| **Toucher `views/modals/product.py` / zones stock de `main_app.py`** | Traité par une tâche parallèle (`task_e51e1d71`). Risque de collision. |
| **Supprimer `stock_service.py`** | Protégé par `SanctuaryShield`. Décision de produit, pas de correctif. |
| **Inverser un des 3 arbitrages de remboursement (§1.2)** | Engage la chaîne fiscale NF525. Décision utilisateur uniquement. |
| **Écrire dans les vrais dossiers depuis les tests** | `tests/conftest.py` redirige `HOME`. Ne pas le désactiver. |

---

## 5. COMMANDES DE VÉRIFICATION

```bash
# Suite backend complète (attendu : 492 passed)
cd "/Volumes/Extreme SSD/KIAMA/Kōdo POS" && python3 -m pytest tests/ -q

# Le chantier Shopify seul
cd "/Volumes/Extreme SSD/KIAMA/Kōdo POS" && python3 -m pytest tests/test_shopify_remboursements.py tests/test_shopify_sync.py tests/test_shopify_cablage.py -v

# Non-régression code-barres (attendu : 65)
cd "/Volumes/Extreme SSD/KIAMA/Kōdo POS" && python3 -m pytest tests/test_barcode_backend.py -q

# Frontend (attendu : 0 erreur)
cd /Users/kiamarulmont/Desktop/kōdo-pos-3 && npm run lint && npx tsc --noEmit

# Mesure du chantier
cd "/Volumes/Extreme SSD/KIAMA/Kōdo POS" && git diff --stat && git status --short
```

---

## 6. PIÈGES CONNUS DE CE DÉPÔT

- `main_app.py` (~6 700 lignes, Tkinter) est **mort** mais porte des **copies vivantes** de
  certains défauts. Corriger un bug uniquement là = ne rien corriger du tout.
- `kodo_core/services/stock_service.py` est mort lui aussi. La vraie gestion de stock passe par la
  table `Stocks` et `inventory_manager.py`.
- `git diff` affiche des `error: non-monotonic index .git/objects/pack/._loose-*.idx` : ce sont des
  fichiers `._` créés par macOS sur le volume externe. Bruit, pas une corruption.
- `timeout` n'existe pas dans ce shell (zsh macOS) — utiliser le paramètre de timeout de l'outil.
- Les migrations prennent un **instantané avant exécution** (`migrations.py:616-637`) et refusent
  de tourner sans sauvegarde vérifiée.
- Sous macOS (BSD `SO_REUSEADDR`), lier `127.0.0.1:port` réussit même si une instance écoute sur
  `0.0.0.0:port` : l'échec du bind ne détecte pas une autre instance.

---

## 7. PROMPT D'AMORÇAGE — nouvelle conversation

> Copier-coller tel quel dans une nouvelle conversation Claude Code ouverte sur
> `/Volumes/Extreme SSD/KIAMA/Kōdo POS`.

```text
Lis d'abord HANDOVER_BACKEND_2026-09-22.md à la racine du projet : c'est la passation
complète du chantier backend (Shopify, comptabilité, sécurité, matériel). Lis aussi
CLAUDE.md. Ne commence rien avant de les avoir lus.

Contexte : tu reprends un chantier en cours. 492 tests backend sont verts, tsc rend 0
erreur, et RIEN N'EST COMMITÉ (24 fichiers modifiés, +4147/-459, 9 fichiers de tests
neufs). Je n'ai pas de vraie boutique Shopify : j'utilise mon ordinateur comme machine
de test.

Tes consignes permanentes :
- Tolérance zéro. Vérifie et sur-vérifie. Un test qui passe n'est pas une preuve :
  vérifie qu'il échoue quand tu casses le code qu'il prétend couvrir.
- Jamais de float sur un montant : Decimal + ROUND_HALF_UP via quantize_money.
- Respecte l'intégrité du code actuel sans le dénaturer. Pas de refactor non demandé.
- Shopify vise exclusivement la boutique enregistrée dans Parametres
  (shopify_store_url + shopify_access_token). Rien en dur, jamais.
- Aucun appel réseau réel vers Shopify dans les tests.
- Ne commit pas, ne tag pas, ne build pas, ne publie pas sans que je le demande.
- Ne lis pas ~/Documents/Kodo_POS/db/kodo_pos.db (refusé par le classifieur) et ne
  cherche pas à contourner ce refus.
- Avant de corriger un bug, vérifie que le fichier que tu modifies est bien sur le
  chemin de production (launch_app.py -> server_pos.py -> kodo_app). main_app.py et
  kodo_core/services/stock_service.py sont MORTS mais parfaitement crédibles.

Commence par la section 2 du HANDOVER (« ce qui reste à faire »), par ordre de
priorité, et dis-moi ce que tu comptes faire avant de le faire.
```

---

## 8. PROMPT POUR LES AGENTS SPÉCIALISÉS

> Bloc à placer en tête de la consigne de tout sous-agent lancé sur ce dépôt
> (les agents `barcode-*` existants sont dans `.claude/agents/`).

```text
Tu interviens sur Kōdo POS, un logiciel de caisse en production chez de vraies
commerçantes. Une erreur ici fausse une comptabilité ou fait perdre du stock réel.

RÈGLES ABSOLUES
1. Périmètre. Tiens-toi strictement au périmètre qu'on te donne. Si tu découvres un
   défaut hors périmètre, SIGNALE-LE dans ton rapport, ne le corrige pas.
2. Chemin de production. Avant de modifier un fichier, prouve qu'il est exécuté :
   launch_app.py -> server_pos.py::run_server -> kodo_app.handle_request.
   PIÈGES : main_app.py (~6700 lignes Tkinter) et kodo_core/services/stock_service.py
   sont MORTS. Leur signature est crédible ; y corriger un bug ne corrige rien.
3. Argent. Jamais de float pour un montant, un centime, une remise ou une TVA.
   decimal.Decimal + ROUND_HALF_UP via quantize_money (kodo_core/domain/sales/models.py).
   Decimal(v) sur un float prend sa valeur BINAIRE -> toujours Decimal(str(v)).
   f"{x:.2f}" est l'arrondi BANCAIRE, pas ROUND_HALF_UP.
4. Chaîne fiscale. Le journal signé (NF525) n'est JAMAIS modifié rétroactivement.
   Ne fabrique jamais un mouvement d'argent qui n'a pas eu lieu, même si cela
   « équilibre » un compte. Signale l'anomalie à la place.
5. Réseau. Aucun appel réel vers Shopify, ni vers aucun service externe, dans un test.
   Un faux serveur local existe déjà dans tests/test_shopify_sync.py : réutilise-le.
6. Base de production. Ne lis jamais ~/Documents/Kodo_POS/db/kodo_pos.db. Travaille sur
   une base temporaire ou une copie fournie.
7. Preuve. Tout comportement nouveau ou corrigé vient avec son test. Puis CASSE
   volontairement le code que ce test couvre et vérifie qu'il échoue. Un test qui
   passe dans les deux cas est un test inutile : réécris-le.
8. Livraison. Ne commit pas, ne tag pas, ne build pas, ne publie rien.

CE QUE DOIT CONTENIR TON RAPPORT
- Ce que tu as changé, fichier par fichier, et POURQUOI (la conséquence concrète pour
  la commerçante, pas la description technique).
- La commande exacte qui prouve que ça marche, et sa sortie réelle.
- Le résultat de ton test de mutation (ce que tu as cassé, ce qui est tombé).
- Ce que tu as trouvé et volontairement PAS corrigé, avec la raison.
- Ce dont tu n'es pas sûr. Ne le masque pas : c'est l'information la plus utile.

VÉRIFICATION FINALE OBLIGATOIRE
cd "/Volumes/Extreme SSD/KIAMA/Kōdo POS" && python3 -m pytest tests/ -q
Attendu : 492 passed (ou davantage si tu as ajouté des tests). Zéro échec.
```
