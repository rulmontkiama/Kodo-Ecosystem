import { NextResponse } from 'next/server';

export async function GET() {
  return NextResponse.json({
    latestVersion: "1.0.30",
    latest_version: "1.0.30",
    version: "1.0.30",
    releaseDate: "2026-08-19",
    downloadUrl: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.30.zip",
    distPatchUrl: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.30.zip",
    dist_patch_url: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.30.zip",
    has_update: true,
    changelog: "v1.0.30 : Déploiement du Design System « Liquid Glass » d'Apple (réfraction optique dynamique, saturation +190%, liserés spéculaires et finitions haute couture).",
  });
}
