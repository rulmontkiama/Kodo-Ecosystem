import { NextResponse } from 'next/server';

export async function GET() {
  return NextResponse.json({
    latestVersion: "1.0.21",
    latest_version: "1.0.21",
    version: "1.0.21",
    releaseDate: "2026-08-19",
    downloadUrl: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.21.zip",
    distPatchUrl: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.21.zip",
    dist_patch_url: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.21.zip",
    has_update: true,
    changelog: "v1.0.21 : Refonte responsive paramètres, support fiscalité Belgique (BCE / TVA 21%), bouton enregistrer en en-tête.",
  });
}
