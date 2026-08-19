import { NextResponse } from 'next/server';

export async function GET() {
  return NextResponse.json({
    latestVersion: "1.0.35",
    latest_version: "1.0.35",
    version: "1.0.35",
    releaseDate: "2026-08-19",
    downloadUrl: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.35.zip",
    distPatchUrl: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.35.zip",
    dist_patch_url: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.35.zip",
    has_update: true,
    changelog: "v1.0.35 : Restauration des fonds et fenêtres épurés classiques (100% netteté & lisibilité) tout en conservant le Liquid Glass sur les switchs, boutons et sélecteurs.",
  });
}
