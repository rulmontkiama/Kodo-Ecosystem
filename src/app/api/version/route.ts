import { NextResponse } from 'next/server';

export async function GET() {
  return NextResponse.json({
    latestVersion: "1.0.34",
    latest_version: "1.0.34",
    version: "1.0.34",
    releaseDate: "2026-08-19",
    downloadUrl: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.34.zip",
    distPatchUrl: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.34.zip",
    dist_patch_url: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.34.zip",
    has_update: true,
    changelog: "v1.0.34 : Transformation du switch « Mode de Règlement » et sélecteurs en capsule Liquid Glass Apple (biseau prismatique, track dépoli et liseré de lumière).",
  });
}
