# 📖 Guide de Démarrage Rapide — Kōdo POS
### Boutique Pilote : L'ADRESSE B (Bruxelles)

Bienvenue dans le guide d'utilisation quotidien de votre logiciel de caisse **Kōdo POS**. Ce document simple et illustré a été conçu pour vous aider, vous et votre équipe, à maîtriser les opérations indispensables de la journée.

---

## 🗺️ Aperçu de l'Interface
Kōdo POS se compose d'une barre de navigation latérale gauche pour passer d'une tâche à l'autre :

```text
┌────────────────────────┐
│      L'ADRESSE B       │
├────────────────────────┤
│  [ Caisse ]            │ <-- Écran de vente quotidien
│  [ Stocks ]            │ <-- Gestion des produits, tailles et quantités
│  [ Retours ]           │ <-- Remboursements & Historique des tickets
│  [ Clôture & Stats ]   │ <-- Chiffres de la journée & Rapport Z
│  [ Paramètres ]        │ <-- Gestion de l'équipe & Fond de caisse
├────────────────────────┤
│  [ Verrouiller ]       │ <-- Sécuriser l'écran (Code PIN requis)
└────────────────────────┘
```

---

## 1. ☀️ Le Matin : Ouverture & Saisie du Fond de Caisse

Pour démarrer la journée correctement, vous devez enregistrer le montant initial en espèces présent dans votre tiroir-caisse.

### 📝 Procédure étape par étape :
1. **Lancez l'application** : Double-cliquez sur l'icône **Kōdo POS** sur votre bureau ou dans vos applications Mac.
2. **Déverrouillez l'écran** : Saisissez votre code PIN personnel à 4 chiffres (Le code administrateur par défaut est `0000`).
3. **Accédez aux Paramètres** : Cliquez sur l'onglet **Paramètres** dans le menu de gauche.
4. **Saisissez le montant** : Allez dans la section **Caisse** (en bas de la page). Dans le champ **Fond de caisse (ouverture)**, tapez la somme en liquide disponible (ex: `200.00`).
5. **Enregistrez** : Cliquez sur le bouton **Enregistrer**. Un message de confirmation vert apparaît en bas de l'écran : `[OK] Fond de caisse enregistré : 200.00 €`.

```text
 ─────────────────────────────────────────────────────────────
  SECTION CAISSE
 ─────────────────────────────────────────────────────────────
  Fond de caisse (ouverture)   [ 200.00   ] €    [ Enregistrer ]
 ─────────────────────────────────────────────────────────────
```

> 💡 **Conseil d'utilisation** : Effectuez cette opération systématiquement le matin avant d'enregistrer votre première vente afin de garantir des calculs de clôture exacts le soir.

---

## 2. 🛍️ Pendant la Journée : Encaisser une Vente

L'encaissement s'effectue depuis l'onglet principal **Caisse**.

```text
┌───────────────────────────────────────────────┬──────────────────────┐
│  Scanner un produit...          [% Remise] [+Client]  │   Total à régler     │
├───────────────────────────────────────────────┤                      │
│                                               │       0,00 €         │
│  [Panier vide : scannez ou cherchez un EAN]   │       0 article      │
│                                               ├──────────────────────┤
│                                               │  [7]   [8]   [9]     │
│                                               │  [4]   [5]   [6]     │
│                                               │  [1]   [2]   [3]     │
│                                               │  [←]   [0]   [OK]    │
│                                               ├──────────────────────┤
│                                               │ [ Bancontact ]       │
│                                               │ [   Espèces  ]       │
│                                               │ [  QR Code   ]       │
│                                               │ [Annuler le ticket]  │
└───────────────────────────────────────────────┴──────────────────────┘
```

### 📝 Procédure étape par étape :

### Étape A : Ajouter les articles au panier
*   **Via le Scanner (Douchette USB)** : Pointez la douchette et scannez le code-barres de l'étiquette du vêtement. L'article s'ajoute instantanément au panier.
    *(Le logiciel s'occupe de traduire automatiquement les caractères de votre scanner s'il est configuré en clavier AZERTY).*
*   **Via la recherche manuelle** : Si une étiquette est abîmée, cliquez dans la zone blanche **"Scanner un produit..."**, tapez le code-barres à 13 chiffres à l'aide du clavier, puis appuyez sur **Entrée** (ou sur la touche **OK** du pavé numérique à l'écran).
*   **Sélection de la taille** : Si le vêtement dispose de variantes, une fenêtre s'ouvre pour vous demander de choisir la taille vendue (S, M, L, XL, etc.) parmi les stocks disponibles.

### Étape B : Fonctions optionnelles
*   **Appliquer une Remise** : Cliquez sur le bouton **% Remise** pour saisir un rabais en pourcentage applicable sur l'ensemble du panier.
*   **Associer un Client** : Cliquez sur le bouton **+ Client** pour l'ajouter sur le ticket.
*   **Prestation libre** : Cliquez sur le bouton **+ Prestation** pour ajouter un article sans code-barres (ex: retouches, livraison) en définissant son prix manuellement.

### Étape C : Enregistrer le Paiement
1. Cliquez sur l'un des boutons de paiement sur le panneau de droite : **Bancontact**, **Espèces**, ou **QR Code**.
2. Une fenêtre de confirmation de paiement s'affiche :
    *   **Paiement Simple** : Si le client paie la totalité, cliquez directement sur **Finaliser la vente** (le montant total restant est pré-rempli).
    *   **Paiement Multiple / Partagé** : Si le client paie une partie en espèces et le solde par carte, modifiez le montant à droite, sélectionnez la première méthode de paiement, puis réappliquez l'opération avec la deuxième méthode pour le solde restant.
    *   **Rendu de monnaie (Espèces)** : Si vous sélectionnez **Espèces** et tapez un montant supérieur à la vente (ex: billet de 50 € pour un achat de 45 €), le logiciel calculera la monnaie et ouvrira une petite fenêtre affichant la somme exacte à rendre au client (ex : `À rendre : 5.00 €`).
3. Une fois finalisé :
    *   Le ticket de caisse s'imprime automatiquement sur l'imprimante thermique.
    *   Le stock de l'article est décrémenté de `-1`.
    *   Le panier se vide, prêt pour le client suivant.

---

## 3. 🛡️ Gestion Réglementaire : Faire un Remboursement (NF525)

Conformément à la législation sur les systèmes de caisse (norme NF525), **vous ne pouvez pas simplement supprimer une vente ou jeter un ticket**. Toute modification doit être signée cryptographiquement et traçable afin d'éviter toute fraude fiscale.

Dans Kōdo POS, un remboursement se fait sous forme de **ticket négatif** (Avoir) chaîné au ticket d'origine.

### 📝 Procédure étape par étape :
1. Allez dans l'onglet **Retours** dans la barre latérale gauche.
2. Taper (ou scannez) le numéro du ticket original (ex: `TCK-2026-0001`) dans le champ de recherche, puis cliquez sur **Rechercher**.
3. La liste des articles achetés apparaît. Repérez l'article que le client retourne et cliquez sur le bouton rouge **Rembourser** situé à côté.
4. Une fenêtre s'ouvre pour vous demander la méthode de remboursement :
    *   **Espèces** : Si vous lui rendez du liquide.
    *   **Bancontact** : Si vous faites un crédit sur sa carte.
    *   **Carte Cadeau** : Si vous lui remettez un bon d'achat.
5. Cliquez sur le mode de remboursement choisi.

```text
 ┌─────────────────────────────────────────┐
 │             Remboursement               │
 ├─────────────────────────────────────────┤
 │         Moyen de Remboursement          │
 │                                         │
 │          [     Espèces      ]           │
 │          [    Bancontact    ]           │
 │          [   Carte Cadeau   ]           │
 │                                         │
 │               [Annuler]                 │
 └─────────────────────────────────────────┘
```

### ⚙️ Ce que fait le logiciel en arrière-plan :
*   Il génère un ticket de remboursement officiel numéroté `REF-TCK-XXXX-YYYYY` avec des valeurs négatives (ex: `-79.00 €`).
*   Il **réinjecte automatiquement** l'article retourné dans votre inventaire (+1 en stock).
*   Il enregistre le mouvement de sortie de caisse dans le journal comptable scellé.
*   Il imprime le reçu de remboursement (ticket d'avoir) destiné au client.

---

## 4. 🌙 Le Soir : Clôture de Caisse & Rapport Z

La clôture de caisse scelle et signe de façon définitive les ventes de la journée. Après avoir généré le Rapport Z, aucune transaction ne peut plus être ajoutée à cette journée.

### 📝 Procédure étape par étape :
1. Cliquez sur l'onglet **Clôture & Stats** dans la barre latérale gauche.
2. Dans le panneau inférieur, cliquez sur le bouton **Clôture Jour (Z)**.
3. Une fenêtre de comptage s'affiche avec la liste des billets et des pièces :
    *   Comptez physiquement l'argent liquide contenu dans votre tiroir-caisse.
    *   Saisissez la quantité pour chaque billet (50€, 20€, 10€...) et pièce (2€, 1€...).
    *   Le logiciel calcule le total automatiquement en bas au fur et à mesure de votre saisie.
4. Cliquez sur **Valider et Imprimer Z**.

```text
 ┌────────────────────────────────────────────────────────┐
 │                Comptage de Caisse (Z)                  │
 ├────────────────────────────┬───────────────────────────┤
 │  BILLETS                   │  PIÈCES                   │
 │  50 € : [ 4 ]  --> 200.00€ │  2 €   : [ 10 ] --> 20.00€│
 │  20 € : [ 5 ]  --> 100.00€ │  1 €   : [ 5  ] --> 5.00€ │
 │  10 € : [ 2 ]  --> 20.00€  │  0.50 €: [ 4  ] --> 2.00€ │
 ├────────────────────────────┴───────────────────────────┤
 │              TOTAL CAISSE COMPTÉ : 347.00 €            │
 ├────────────────────────────────────────────────────────┤
 │                 [ Valider et Imprimer Z ]              │
 └────────────────────────────────────────────────────────┘
```

### ⚠️ Gestion des écarts de caisse :
Si le montant en espèces compté ne correspond pas au montant théorique attendu par le logiciel (ventes espèces du jour + fond de caisse du matin - dépenses), une alerte apparaît :

*   **Écart négatif** (ex: `-10.00 €`) : Il manque de l'argent dans le tiroir.
*   **Écart positif** (ex: `+5.00 €`) : Il y a un trop-perçu.

> Deux options s'offrent à vous :
> 1.  **Recompter** : Ferme l'alerte pour vous permettre de vérifier vos calculs ou recomposer votre saisie.
> 2.  **Forcer la Clôture** : Enregistre l'écart constaté dans la base de données et finalise la journée.

### 💾 Résultats de la Clôture :
Dès que vous validez définitivement :
1.  **Impression** : Le ticket **Rapport Z officiel** est imprimé sur votre imprimante thermique (CA TTC, CA HT, détail TVA 6% et 21%, répartition des paiements, écarts).
2.  **Export Excel** : Un fichier Excel complet de la journée est automatiquement enregistré dans le dossier **`Exports_L_ADRESSE_B`** situé sur votre Mac (contenant la synthèse, le Top 5 des ventes et le détail de chaque ticket).
3.  **Sauvegarde automatique** : Une copie de sauvegarde chiffrée et sécurisée de vos données de caisse est générée et stockée dans votre dossier Documents (`Backups_L_ADRESSE_B`).

---

## 💡 Conseils & Dépannage Rapide

*   **L'imprimante ne répond pas ?** Vérifiez qu'elle est bien allumée, que le voyant bleu est fixe, et qu'elle est connectée en USB à votre Mac. Si nécessaire, débranchez puis rebranchez le câble USB et relancez Kōdo POS.
*   **Erreur de saisie ?** Si vous ajoutez un mauvais article au panier, cliquez sur le panier, sélectionnez l'article et ajustez sa quantité ou retirez-le avant de procéder au paiement.
*   **Vente à l'aveugle (Pas de douchette) ?** Vous pouvez toujours faire vos ventes sans douchette en saisissant les codes EAN manuellement dans la barre de recherche ou en créant une **+ Prestation** rapide pour les articles de dernière minute.
*   **Sauvegarde physique ?** Pensez à copier régulièrement le contenu du dossier `Backups_L_ADRESSE_B` sur une clé USB externe pour ne jamais perdre l'historique de votre boutique en cas de panne de votre ordinateur.
