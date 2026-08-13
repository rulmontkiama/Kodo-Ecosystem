import os
import sys
from decimal import Decimal
from ticket_printer import imprimer_ticket

# Contenu du ticket sélectionné par l'utilisateur
CONTENU_TICKET = """==========================================
               L'ADRESSE B
            Boutique de Mode
==========================================

         Une nouvelle saison...
         De nouvelles envies...
         
         Le moment est venu de
         renouveler votre garde-robe.
         
          C'EST LE DÉBUT DES
               S O L D E S
         
       Profitez de remises allant
        jusqu'à -30% durant tout
          le mois de juillet.
          
        L'Adresse B n'attend plus
                 que vous.
==========================================
"""

def main():
    print("Génération et tentative d'impression du ticket spécial soldes...")
    
    # Numéro fictif pour le fichier temporaire
    numero_ticket = "PROMO-SOLDES"
    
    try:
        chemin_fichier = imprimer_ticket(CONTENU_TICKET, numero_ticket)
        print(f"\n[OK] Fichier de ticket virtuel généré avec succès.")
        print(f"Chemin : {chemin_fichier}")
        print("\nSi l'imprimante USB physique est connectée, le ticket devrait s'imprimer avec le logo et le bloc Instagram.")
        print("Sinon, l'aperçu texte s'est ouvert sur votre écran.")
    except Exception as e:
        print(f"\n[ERREUR] Une erreur est survenue lors de l'impression : {e}")

if __name__ == "__main__":
    main()
