# Kōdo POS - Instructions Claude Code (Backend & Architecture)

Bienvenue sur le projet **Kōdo POS**. Tu es chargé de renforcer, structurer et sécuriser le **backend** et la **logique métier pure**.

---

## ⚡ RÈGLE D'OR : ÉCONOMIE STRICTE DE TOKENS

1. **Ne lis jamais de gros fichiers d'un coup** :
   - Le code UI (`main_app.py`, 3000 lignes) est ignoré par défaut. Si tu dois interagir avec lui, demande une extraction ou lis des plages précises (max 80 lignes).
   - Ne lis jamais de fichiers complets si tu n'as besoin que d'une fonction ou d'une classe.
2. **Ne lance pas de commandes verbeuses** :
   - Pour les tests : utilise toujours `pytest tests/ -q --tb=short` ou cible un fichier de test précis (ex: `pytest tests/test_cart.py -q`).
   - Évite les `find` ou `grep` récursifs globaux. Cible strictement `kodo_core/` ou `core/`.
3. **Périmètre d'action strict** :
   - Travaille **uniquement** dans `kodo_core/` (logique métier, domaines, BDD, services).
   - Ne touche **jamais** à l'interface CustomTkinter directement sans découpler la logique dans un service testable.

---

## 🏗️ ARCHITECTURE DU BACKEND (`kodo_core/`)

Tout le backend doit respecter une architecture modulaire et découplée de l'UI :

```
kodo_core/
├── domain/                  # Entités métier pures & Value Objects (dataclasses / Pydantic)
│   ├── sales/               # Lignes de vente, remises, arrondis, panier
│   ├── catalog/             # Articles, catégories, variantes, seuils d'alerte
│   ├── accounting/          # Clôtures de caisse, calculs TVA, X/Z de caisse
│   └── customers/           # Fidélité, avoirs, crédits clients
├── db/                      # Accès BDD SQLite, schémas & migrations
├── services/                # Cas d'usage métier (Orchestration)
│   ├── cart_service.py      # Calculs paniers, remises en cascade, TVA
│   ├── stock_service.py     # Décrémentation atomique, historique des mouvements
│   └── closing_service.py   # Clôture comptable Z étanche
└── sync/                    # Synchronisation Offline-First (Shopify, Firebase)
```

---

## 🛡️ RÈGLES MÉTIER ET SÉCURITÉ OBLIGATOIRES

1. **Précision Monétaire Absolue** :
   - **Interdiction formelle d'utiliser des `float`** pour les calculs de montants, centimes, remises ou TVA.
   - Utilise toujours `decimal.Decimal` avec quantification explicite (`quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)`).
2. **Intégrité BDD & Concurrence** :
   - Toute opération modifiant plusieurs tables (ex: valider une vente = insérer vente + décrémenter stock + mettre à jour fidélité) **doit être enveloppée dans une transaction atomique** (`BEGIN TRANSACTION ... COMMIT`).
   - Gérer systématiquement les `sqlite3.OperationalError: database is locked` avec retry ou timeout configuré.
3. **Règle de la BDD Vierge d'usine** :
   - Ne modifie jamais directement les bases de test/production réelles (`ladresse_b.db`).
   - Les tests unitaires doivent toujours utiliser une base SQLite en mémoire (`:memory:`) ou un fichier temporaire détruit après le test.

---

## 🧪 COMMANDES UTILES

- **Lancer les tests du backend** :
  ```bash
  pytest tests/ -q --tb=short
  ```
- **Lancer un test unitaire spécifique** :
  ```bash
  pytest tests/test_sales.py -q
  ```
- **Vérifier les types** :
  ```bash
  mypy kodo_core/ --ignore-missing-imports
  ```

---

## 🎯 OBJECTIF EN COURS
Consulte le fichier `CLAUDE_BACKEND_MISSIONS.md` pour prendre connaissance des tâches prioritaires prêtes à l'exécution.
