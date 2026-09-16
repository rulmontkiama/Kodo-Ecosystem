import { NextResponse } from 'next/server';

export async function GET() {
  return NextResponse.json({
    latestVersion: "1.0.46",
    latest_version: "1.0.46",
    version: "1.0.46",
    releaseDate: "2026-09-16",
    downloadUrl: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.46.zip",
    distPatchUrl: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.46.zip",
    dist_patch_url: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.46.zip",
    has_update: true,
    changelog: "v1.0.46 : Module Live Shopping interactif avec gestion FIFO et paiement direct (Virement / Retrait boutique), persistance absolue du logo ticket (SQLite + multi-dossiers), impression de ticket test et fiabilisation des sauvegardes.",
  });
}
