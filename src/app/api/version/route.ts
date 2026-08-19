import { NextResponse } from 'next/server';

export async function GET() {
  return NextResponse.json({
    latestVersion: "1.0.19",
    latest_version: "1.0.19",
    version: "1.0.19",
    releaseDate: "2026-08-19",
    downloadUrl: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.19.zip",
    distPatchUrl: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.19.zip",
    dist_patch_url: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.19.zip",
    has_update: true,
    changelog: "v1.0.19 : Suppression complète des données fictives, version vierge d'usine, statistiques dynamiques en temps réel, nouvel export et transfert de pack de données ZIP.",
  });
}
