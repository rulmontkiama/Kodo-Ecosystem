# Missions Backend pour Claude Code (Kōdo POS)

Ce document contient les 4 missions prioritaires prêtes à être exécutées par Claude Code.
Chaque mission est indépendante, ultra-cadrée et conçue pour consommer un minimum de tokens.

---

## 📋 MISSION 1 : Moteur de Calcul Panier & TVA (cart_service.py)

### 🎯 Objectif
Créer un service de calcul du panier robuste, 100% découplé de l interface graphique, utilisant exclusivement Decimal.

### 📂 Fichiers concernés
- kodo_core/domain/sales/models.py (dataclasses CartItem, CartDiscount, CartTotal)
- kodo_core/services/cart_service.py (moteur de calcul pur)
- tests/test_cart_service.py (tests unitaires complets)

### ⚙️ Spécifications Métier
1. Lignes d articles : Prix unitaire TTC, quantité, taux de TVA (ex: 20%, 5.5%, 10%).
2. Remises autorisées :
   - Remise par ligne (en % ou en € TTC).
   - Remise globale sur le panier (en % ou en € TTC).
   - Règle d application : si remise globale en valeur, ventiler la remise proportionnellement sur les lignes pour conserver la ventilation exacte de la TVA.
3. Calcul de TVA :
   - Calculer le HT à partir du TTC : HT = TTC / (1 + Taux_TVA).
   - Regrouper la TVA ventilée par taux (base HT, montant TVA, montant TTC) avec arrondi ROUND_HALF_UP à 2 décimales.
4. Précision : Aucun float. Si un float entre en paramètre, lever une TypeError ou convertir immédiatement en Decimal(str(val)).

---

## 📋 MISSION 2 : Gestion Atomique des Stocks & Mouvements (stock_service.py)

### 🎯 Objectif
Sécuriser les variations de stock lors d une vente, d un retour ou d un réapprovisionnement pour éviter les désynchronisations ou stocks négatifs inattendus.

### 📂 Fichiers concernés
- kodo_core/domain/catalog/models.py
- kodo_core/services/stock_service.py
- tests/test_stock_service.py

### ⚙️ Spécifications Métier
1. Opérations atomiques :
   - Décrémentation lors d une vente dans une transaction SQL unitaire.
   - Incrémentation lors d un retour/avoir.
2. Table d audit des mouvements (stock_movements) :
   - Tracer chaque mouvement : product_id, variation_id, type_mouvement (VENTE, RETOUR, INVENTAIRE, AJUSTEMENT), delta, stock_final, motif, date_heure.
3. Seuils d alerte :
   - Méthode verifier_alertes_stock() retournant les articles où stock_actuel <= stock_alerte.

---

## 📋 MISSION 3 : Clôture Comptable Z Étanche (closing_service.py)

### 🎯 Objectif
Fournir le moteur de calcul du Rapport Z de clôture de caisse conforme aux exigences fiscales et comptables.

### 📂 Fichiers concernés
- kodo_core/domain/accounting/models.py
- kodo_core/services/closing_service.py
- tests/test_closing_service.py

### ⚙️ Spécifications Métier
1. Période de clôture :
   - Prend toutes les ventes validées depuis le dernier Z clôturé.
2. Agrégats obligatoires :
   - CA Total TTC, Total HT, Total TVA.
   - Ventilation TVA par taux (Base HT, Montant TVA par taux).
   - Ventilation par mode de règlement (Espèces, Carte Bancaire, Avoir, Virement).
   - Nombre de transactions, panier moyen, total des remises accordées.
3. Intégrité & Immuabilité :
   - Une fois le Z généré, marquer les ventes avec le z_id associé pour empêcher tout double comptage.
   - Générer une empreinte de contrôle (hash SHA-256 des totaux et de l ID du Z précédent pour chaînage).

---

## 📋 MISSION 4 : File d Attente de Synchronisation Offline-First (offline_queue.py)

### 🎯 Objectif
Gérer une file d attente d événements à synchroniser avec Shopify et Firebase lorsque la caisse revient en ligne.

### 📂 Fichiers concernés
- kodo_core/sync/offline_queue.py
- tests/test_offline_queue.py

### ⚙️ Spécifications Métier
1. Événements supportés : VENTE_CREEE, STOCK_AJUSTE, CLIENT_MODIFIE.
2. Gestion des pannes :
   - Stocker les événements dans SQLite (sync_queue) avec statut PENDING, RETRY, FAILED, SUCCESS.
   - Mécanisme de retry avec backoff exponentiel.
3. Résolution de conflits basique :
   - Pour les stocks : appliquer les deltas relatifs plutôt que d écraser la valeur absolue brute.
