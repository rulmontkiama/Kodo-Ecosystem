import { NextResponse } from 'next/server';

export async function GET() {
  return NextResponse.json({
    latestVersion: "1.0.28",
    latest_version: "1.0.28",
    version: "1.0.28",
    releaseDate: "2026-08-19",
    downloadUrl: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.28.zip",
    distPatchUrl: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.28.zip",
    dist_patch_url: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.28.zip",
    has_update: true,
    changelog: "v1.0.28 : Agrandissement ergonomique des touches du pavé numérique et des coupures d'espèces pour un confort tactile optimal.",
  });
}
