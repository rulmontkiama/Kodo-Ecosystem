import { NextResponse } from 'next/server';

export async function GET() {
  return NextResponse.json({
    latestVersion: "1.0.33",
    latest_version: "1.0.33",
    version: "1.0.33",
    releaseDate: "2026-08-19",
    downloadUrl: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.33.zip",
    distPatchUrl: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.33.zip",
    dist_patch_url: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.33.zip",
    has_update: true,
    changelog: "v1.0.33 : Activation du maillage d'orbes chromatiques ambiants et translucidité accrue pour un effet Liquid Glass éclatant et immédiatement visible.",
  });
}
