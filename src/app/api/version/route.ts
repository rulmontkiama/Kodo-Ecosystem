import { NextResponse } from 'next/server';

export async function GET() {
  return NextResponse.json({
    latestVersion: "1.0.29",
    latest_version: "1.0.29",
    version: "1.0.29",
    releaseDate: "2026-08-19",
    downloadUrl: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.29.zip",
    distPatchUrl: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.29.zip",
    dist_patch_url: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.29.zip",
    has_update: true,
    changelog: "v1.0.29 : Correction du module comptable (sélection et changement de format FEC / Excel / PDF dynamiques, téléchargement fiable sans blocage).",
  });
}
