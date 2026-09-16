import { NextResponse } from 'next/server';

export async function GET() {
  return NextResponse.json({
    latestVersion: "1.0.48",
    latest_version: "1.0.48",
    version: "1.0.48",
    releaseDate: "2026-09-16",
    downloadUrl: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.48.zip",
    distPatchUrl: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.48.zip",
    dist_patch_url: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.48.zip",
    has_update: true,
    changelog: "v1.0.48 : Correctif critique fond de caisse matinal (isolation des sessions actives sans altération de l'historique clôturé), logo dynamique boutique en direct sur Live Shopping, synchronisation stock hors-ligne et sécurité PII.",
  });
}
