import { NextResponse } from 'next/server';

export async function GET() {
  return NextResponse.json({
    latestVersion: "1.0.24",
    latest_version: "1.0.24",
    version: "1.0.24",
    releaseDate: "2026-08-19",
    downloadUrl: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.24.zip",
    distPatchUrl: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.24.zip",
    dist_patch_url: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.24.zip",
    has_update: true,
    changelog: "v1.0.24 : Correctif de stabilité complet, intégration posApi.checkUpdate, popup automatique et modale notes de version (i).",
  });
}
