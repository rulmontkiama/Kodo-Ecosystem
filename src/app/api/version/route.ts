import { NextResponse } from 'next/server';

export async function GET() {
  return NextResponse.json({
    latestVersion: "1.0.25",
    latest_version: "1.0.25",
    version: "1.0.25",
    releaseDate: "2026-08-19",
    downloadUrl: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.25.zip",
    distPatchUrl: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.25.zip",
    dist_patch_url: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.25.zip",
    has_update: true,
    changelog: "v1.0.25 : Refonte UI/UX complète, symétrie parfaite de l'en-tête Caisse, intégration fluide du symbole Euro (€), nouveau pavé tactile et filtres de stocks.",
  });
}
