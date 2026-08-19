import { NextResponse } from 'next/server';

export async function GET() {
  return NextResponse.json({
    latestVersion: "1.0.32",
    latest_version: "1.0.32",
    version: "1.0.32",
    releaseDate: "2026-08-19",
    downloadUrl: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.32.zip",
    distPatchUrl: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.32.zip",
    dist_patch_url: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.32.zip",
    has_update: true,
    changelog: "v1.0.32 : Intégration du composant Hybride Liquid Glass (dispersion prismatique RGB, suivi dynamique de la lumière, shader optique SVG et 60 FPS constants).",
  });
}
