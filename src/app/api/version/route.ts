import { NextResponse } from 'next/server';

export async function GET() {
  return NextResponse.json({
    latestVersion: "1.0.50",
    latest_version: "1.0.50",
    version: "1.0.50",
    releaseDate: "2026-09-17",
    downloadUrl: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.50.zip",
    distPatchUrl: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.50.zip",
    dist_patch_url: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.50.zip",
    has_update: true,
    changelog: "v1.0.50 : Support et détection automatique des imprimantes thermiques USB (CUPS Printer_POS_80 sans délai IP), normalisation des routes API (slash trailing), endpoints de statut matériel et commande d'ouverture tiroir-caisse.",
  });
}
