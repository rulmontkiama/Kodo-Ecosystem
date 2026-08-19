import { NextResponse } from 'next/server';

export async function GET() {
  return NextResponse.json({
    latestVersion: "1.0.26",
    latest_version: "1.0.26",
    version: "1.0.26",
    releaseDate: "2026-08-19",
    downloadUrl: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.26.zip",
    distPatchUrl: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.26.zip",
    dist_patch_url: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.26.zip",
    has_update: true,
    changelog: "v1.0.26 : Correction de l'alignement du bouton de fermeture de la modale client (sans chevauchement) et synchronisation dynamique du nom d'enseigne dans la barre latérale.",
  });
}
