import { NextResponse } from 'next/server';

export async function GET() {
  return NextResponse.json({
    latestVersion: "1.0.36",
    latest_version: "1.0.36",
    version: "1.0.36",
    releaseDate: "2026-08-19",
    downloadUrl: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.36.zip",
    distPatchUrl: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.36.zip",
    dist_patch_url: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.36.zip",
    has_update: true,
    changelog: "v1.0.36 : Refonte minimaliste Apple de la fiche produit (suppression de tous les emojis, sélection par menu déroulant épuré et alignement ergonomique).",
  });
}
