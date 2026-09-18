# -*- coding: utf-8 -*-
"""
Gestion Comptable et Clôture Z (NF525) - Kōdo POS Core
Gère la clôture journalière Z certifiée avec signature cryptographique SHA-256,
le cumulatif Grand Total de caisse, la ventilation de la TVA par taux,
et l'exportation des données vers CSV et Excel.
"""

import csv
import json
import os
import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Any, List, Optional

import database_manager
from database_manager import get_connection, generer_bilan_z_journalier, enregistrer_cloture_caisse
import export_manager

TWO_DECIMALS = Decimal('0.01')


def quantize_money(val: Any) -> Decimal:
    if val is None:
        return Decimal('0.00')
    return Decimal(str(val)).quantize(TWO_DECIMALS, rounding=ROUND_HALF_UP)


class ZReportEngine:
    """
    Moteur de rapport Z journalier conforme NF525 et comptabilité certifiée.
    """

    @classmethod
    def get_daily_z_summary(cls, caisse_id: str = "POS-01", conn=None) -> Dict[str, Any]:
        """
        Génère le bilan des ventes en cours non encore clôturées pour la journée/session.
        Inclut la ventilation de TVA par taux et le détail des modes de règlement.
        """
        should_close = False
        if conn is None:
            conn = get_connection()
            should_close = True

        try:
            bilan = generer_bilan_z_journalier(caisse_id=caisse_id, conn=conn)

            # Ventilation de TVA par taux : DOIT porter exactement sur le même ensemble
            # de tickets que le bilan ci-dessus (caisse_id + z_id IS NULL). L'ancienne
            # requête comparait `date_heure > dernière_date_de_cloture` sans filtrer par
            # caisse_id : elle mélangeait les tickets de toutes les caisses et pouvait
            # diverger du vrai total facturé (tickets à la même seconde que la clôture,
            # horloge modifiée, etc.).
            #
            # De plus, Ventes_Details.prix_unitaire_tvac stocke le prix unitaire BRUT (avant
            # remise globale) : sommer quantite*prix_unitaire_tvac donne donc le total AVANT
            # remise, qui dépasse structurellement Tickets.total_tvac (net) dès qu'une remise
            # est appliquée. On pondère chaque ticket par son propre ratio net/brut (le même
            # calcul que celui déjà fait au moment de la vente dans process_sale_transaction)
            # pour que la somme de la ventilation corresponde exactement au total facturé.
            cursor = conn.cursor()
            vat_breakdown: Dict[str, Dict[str, float]] = {}

            if bilan["ticket_ids"]:
                placeholders = ",".join("?" for _ in bilan["ticket_ids"])
                cursor.execute(f"""
                    SELECT t.id, t.total_tvac, p.taux_tva, SUM(v.quantite * v.prix_unitaire_tvac) as tvac_brut
                    FROM Tickets t
                    JOIN Ventes_Details v ON v.id_ticket = t.id
                    LEFT JOIN Stocks s ON v.id_stock = s.id
                    LEFT JOIN Produits p ON s.id_produit = p.id
                    WHERE t.id IN ({placeholders})
                    GROUP BY t.id, p.taux_tva
                """, bilan["ticket_ids"])
                rows = cursor.fetchall()

                gross_by_ticket: Dict[int, Decimal] = {}
                lines_by_ticket: Dict[int, list] = {}
                net_by_ticket: Dict[int, Decimal] = {}
                for t_id, t_total_tvac, taux, tvac_brut in rows:
                    taux_dec = Decimal(str(taux)) if taux is not None else Decimal('0.21')
                    tvac_brut_dec = Decimal(str(tvac_brut or "0.00"))
                    gross_by_ticket[t_id] = gross_by_ticket.get(t_id, Decimal('0.00')) + tvac_brut_dec
                    net_by_ticket[t_id] = Decimal(str(t_total_tvac or "0.00"))
                    lines_by_ticket.setdefault(t_id, []).append((taux_dec, tvac_brut_dec))

                accum: Dict[Decimal, Decimal] = {}
                for t_id, lines in lines_by_ticket.items():
                    gross_total = gross_by_ticket.get(t_id, Decimal('0.00'))
                    net_total = net_by_ticket.get(t_id, Decimal('0.00'))
                    ratio = (net_total / gross_total) if gross_total > Decimal('0.00') else Decimal('1.00')
                    for taux_dec, tvac_brut_dec in lines:
                        tvac_net = quantize_money(tvac_brut_dec * ratio)
                        accum[taux_dec] = accum.get(taux_dec, Decimal('0.00')) + tvac_net

                for taux_dec, tvac_d in accum.items():
                    htva_d = quantize_money(tvac_d / (Decimal('1.00') + taux_dec))
                    tva_d = tvac_d - htva_d
                    rate_label = f"{float(taux_dec)*100:.1f}%".rstrip('0').rstrip('.') + "%"
                    vat_breakdown[rate_label] = {
                        "htva": float(htva_d),
                        "tva": float(tva_d),
                        "tvac": float(tvac_d),
                        "rate": float(taux_dec)
                    }

            bilan["vat_breakdown"] = vat_breakdown

            # Conversion des Decimals en floats pour la sérialisation
            for key in ["total_tvac", "total_htva", "total_tva", "total_remises", "total_especes",
                        "total_carte", "total_qr", "total_avoir", "total_apports", "total_prelevements"]:
                if key in bilan and isinstance(bilan[key], Decimal):
                    bilan[key] = float(bilan[key])

            return bilan

        finally:
            if should_close and conn:
                conn.close()

    @classmethod
    def close_z_report(
        cls,
        caisse_id: str = "POS-01",
        fond_caisse_reel: float = 0.0,
        fond_caisse_matin: float = 0.0,
        vendeur: str = "Admin",
        conn=None
    ) -> Dict[str, Any]:
        """
        Exécute et scelle la clôture comptable Z journalière (NF525).
        """
        fond_dec = Decimal(str(fond_caisse_reel))
        fond_matin_dec = Decimal(str(fond_caisse_matin))
        res = enregistrer_cloture_caisse(
            caisse_id=caisse_id,
            fond_caisse_reel=fond_dec,
            fond_caisse_matin=fond_matin_dec,
            vendeur=vendeur,
            conn=conn
        )
        return res

    @classmethod
    def get_grand_totals(cls, conn=None) -> Dict[str, Any]:
        """
        Calcule le Grand Total cumulatif historique inaltérable de la caisse (NF525).
        """
        should_close = False
        if conn is None:
            conn = get_connection()
            should_close = True

        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT 
                    COUNT(*),
                    COALESCE(SUM(total_ventes_tvac), 0.0),
                    COALESCE(SUM(total_htva), 0.0),
                    COALESCE(SUM(total_tva), 0.0),
                    COALESCE(SUM(total_remises), 0.0),
                    COALESCE(SUM(total_tickets), 0)
                FROM Clotures_Caisse
            """)
            r = cursor.fetchone()

            cursor.execute("SELECT COUNT(*), COALESCE(SUM(total_tvac), 0.0), COALESCE(SUM(total_htva), 0.0) FROM Tickets")
            t_row = cursor.fetchone()

            return {
                "total_clotures_z": int(r[0]),
                "grand_total_z_tvac": float(quantize_money(r[1])),
                "grand_total_z_htva": float(quantize_money(r[2])),
                "grand_total_z_tva": float(quantize_money(r[3])),
                "grand_total_z_remises": float(quantize_money(r[4])),
                "grand_total_z_tickets": int(r[5]),
                "lifetime_tickets_count": int(t_row[0]),
                "lifetime_sales_tvac": float(quantize_money(t_row[1])),
                "lifetime_sales_htva": float(quantize_money(t_row[2]))
            }

        finally:
            if should_close and conn:
                conn.close()

    @classmethod
    def get_past_z_reports(cls, limit: int = 30, conn=None) -> List[Dict[str, Any]]:
        """Retourne la liste des Z de caisse clôturés précédemment."""
        should_close = False
        if conn is None:
            conn = get_connection()
            should_close = True

        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, date_cloture, caisse_id, total_ventes_tvac, total_htva, total_tva,
                       total_especes, total_carte, total_remises, total_tickets, fond_caisse_reel,
                       ecart, vendeur, current_hash, signature, created_at_utc
                FROM Clotures_Caisse
                ORDER BY id DESC
                LIMIT ?
            """, (limit,))
            rows = cursor.fetchall()

            reports = []
            for r in rows:
                reports.append({
                    "id": r[0],
                    "date_cloture": r[1],
                    "caisse_id": r[2],
                    "total_ventes_tvac": float(r[3]),
                    "total_htva": float(r[4]),
                    "total_tva": float(r[5]),
                    "total_especes": float(r[6]),
                    "total_carte": float(r[7]),
                    "total_remises": float(r[8]),
                    "total_tickets": int(r[9]),
                    "fond_caisse_reel": float(r[10]),
                    "ecart": float(r[11]),
                    "vendeur": r[12] or "Admin",
                    "hash": r[13] or r[14] or "",
                    "created_at_utc": r[15] or ""
                })

            return reports
        finally:
            if should_close and conn:
                conn.close()

    # Exports Excel et CSV

    @classmethod
    def export_z_reports_csv(cls, output_path: Optional[str] = None, conn=None) -> str:
        """Exporte l'historique des clôtures Z au format CSV."""
        reports = cls.get_past_z_reports(limit=1000, conn=conn)

        if not output_path:
            from core.config import ShopConfig
            export_dir = ShopConfig.get_exports_dir()
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = os.path.join(export_dir, f"export_z_reports_{ts}.csv")

        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        fieldnames = [
            "id", "date_cloture", "caisse_id", "total_ventes_tvac", "total_htva",
            "total_tva", "total_especes", "total_carte", "total_remises", "total_tickets",
            "fond_caisse_reel", "ecart", "vendeur", "hash"
        ]

        with open(output_path, "w", newline="", encoding="utf-8-sig") as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames, delimiter=";")
            writer.writeheader()
            for rep in reports:
                row = {k: rep.get(k, "") for k in fieldnames}
                writer.writerow(row)

        return output_path

    @classmethod
    def export_z_reports_excel(cls, output_path: Optional[str] = None, conn=None) -> str:
        """Exporte l'historique des clôtures Z au format Excel (.xlsx)."""
        import pandas as pd
        reports = cls.get_past_z_reports(limit=1000, conn=conn)

        if not output_path:
            from core.config import ShopConfig
            export_dir = ShopConfig.get_exports_dir()
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = os.path.join(export_dir, f"export_z_reports_{ts}.xlsx")

        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        df = pd.DataFrame(reports)
        df.to_excel(output_path, index=False, engine='openpyxl')
        return output_path

    @classmethod
    def export_belgian_accounting_csv(cls) -> str:
        """Appelle le module d'exportation comptable normé Belge."""
        return export_manager.export_comptable_belge()

    @classmethod
    def export_winbooks_accounting_csv(cls, month: Optional[int] = None, year: Optional[int] = None) -> str:
        """Appelle l'exportation au format WinBooks / Exact Online."""
        return export_manager.export_winbooks_csv(mois=month, annee=year)
