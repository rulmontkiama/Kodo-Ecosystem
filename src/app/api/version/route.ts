import { NextResponse } from 'next/server';

export async function GET() {
  return NextResponse.json({
    latestVersion: "1.0.40",
    latest_version: "1.0.40",
    version: "1.0.40",
    releaseDate: "2026-08-19",
    downloadUrl: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.40.zip",
    distPatchUrl: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.40.zip",
    dist_patch_url: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.40.zip",
    has_update: true,
    changelog: "v1.0.40 : Alignement parfait à 2 niveaux sans aucun vide ni saut de ligne (Titre + Profil Vendeur en haut, Paniers + Actions en sous-barre).",
  });
}
