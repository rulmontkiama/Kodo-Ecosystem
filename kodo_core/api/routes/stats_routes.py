# -*- coding: utf-8 -*-
"""
Routes API Statistiques, Rapports et Exports (Comptabilité, PDF, CSV, Excel) - Kōdo POS Core
"""

import os
import re
import logging
import tempfile
import datetime
from typing import Dict, Any, Tuple, Optional, Union

from kodo_core.domain.accounting.z_report import ZReportEngine
from kodo_core.api.session_manager import is_non_admin_session
import pdf_generator
import export_manager

logger = logging.getLogger("kodo.api.stats")


def handle_stats_request(method: str, path: str, query: Dict[str, Any], data: Dict[str, Any], headers: Optional[Dict[str, str]] = None) -> Optional[Tuple[int, Any, Optional[Dict[str, str]]]]:
    """
    Gestionnaire de requêtes pour les statistiques et les exports (PDF, CSV, Excel).
    Retourne (status_code, content, headers) ou (status_code, json_data, None).
    """
    if path.startswith("/api/stats") or path.startswith("/api/export"):
        if is_non_admin_session(headers):
            return 403, {"success": False, "error": "Accès réservé au Gérant."}, None

    # 1. Statistiques du Tableau de Bord (Dashboard)
    if method == "GET" and path == "/api/stats/dashboard":
        bilan = ZReportEngine.get_daily_z_summary()
        grand_totals = ZReportEngine.get_grand_totals()
        return 200, {
            "daily_summary": bilan,
            "grand_totals": grand_totals
        }, None

    # 2. Historique des rapports Z
    elif method == "GET" and path == "/api/stats/z-reports":
        raw_limit = query.get("limit", [30])[0]
        try:
            limit = int(raw_limit)
        except (ValueError, TypeError):
            limit = 30
        limit = max(1, min(365, limit))
        reports = ZReportEngine.get_past_z_reports(limit=limit)
        return 200, reports, None

    # 3. Export PDF (Rapport de caisse)
    elif method == "GET" and path == "/api/export/pdf":
        rep_type = query.get('type', ['jour'])[0]
        rep_date = query.get('date', [datetime.datetime.now().strftime("%Y-%m-%d")])[0]
        safe_date = re.sub(r'[^0-9A-Za-z_-]', '', str(rep_date)) or 'export'
        fd, tmp_pdf = tempfile.mkstemp(prefix="rapport_comptable_", suffix=".pdf")
        os.close(fd)
        try:
            pdf_generator.generer_rapport_pdf(rep_type, rep_date, tmp_pdf)
            with open(tmp_pdf, 'rb') as f:
                pdf_bytes = f.read()
        finally:
            try:
                if os.path.exists(tmp_pdf):
                    os.remove(tmp_pdf)
            except OSError as exc:
                logger.warning("Impossible de supprimer le PDF temporaire %s : %s", tmp_pdf, exc)

        headers = {
            'Content-Type': 'application/pdf',
            'Content-Disposition': f'attachment; filename="Rapport_Comptable_{safe_date}.pdf"',
            'Content-Length': str(len(pdf_bytes))
        }
        return 200, pdf_bytes, headers

    # 4. Export CSV / FEC (Comptabilité Belge / WinBooks / Z)
    elif method == "GET" and (path == "/api/export/csv" or path == "/api/export/fec"):
        fmt = query.get('format', ['belge'])[0]
        if fmt == 'winbooks':
            mois = query.get('mois', [None])[0]
            annee = query.get('annee', [None])[0]
            csv_path = export_manager.export_winbooks_csv(mois=mois, annee=annee)
            filename = "Export_WinBooks.csv"
        elif fmt == 'z':
            csv_path = ZReportEngine.export_z_reports_csv()
            filename = "Export_Rapports_Z.csv"
        else:
            csv_path = export_manager.export_comptable_belge()
            filename = "Export_Comptable_Belge.csv"

        with open(csv_path, 'rb') as f:
            csv_bytes = f.read()

        headers = {
            'Content-Type': 'text/csv; charset=utf-8',
            'Content-Disposition': f'attachment; filename="{filename}"',
            'Content-Length': str(len(csv_bytes))
        }
        return 200, csv_bytes, headers

    # 5. Export Excel (.xlsx)
    elif method == "GET" and path == "/api/export/excel":
        excel_path = ZReportEngine.export_z_reports_excel()
        with open(excel_path, 'rb') as f:
            excel_bytes = f.read()

        headers = {
            'Content-Type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            'Content-Disposition': 'attachment; filename="Export_Rapports_Z.xlsx"',
            'Content-Length': str(len(excel_bytes))
        }
        return 200, excel_bytes, headers

    return None
