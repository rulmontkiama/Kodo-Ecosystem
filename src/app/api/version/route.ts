import { NextResponse } from 'next/server';

export async function GET() {
  return NextResponse.json({
    latestVersion: "1.0.18",
    releaseDate: "2026-08-14",
    downloadUrl: "https://kodo-solutions.vercel.app/Installation_Kodo_POS.dmg",
    windowsDownloadUrl: "https://kodo-solutions.vercel.app/Kodo_POS_v1.0.18_Windows_Portable.zip",
    distPatchUrl: "https://kodo-solutions.vercel.app/dist_v1.0.18.zip",
    has_update: true,
    changelog: "v1.0.18 : Nouveau Design Moderne, Résolution des bugs de persistance (Ghost Data), correctif de l'auto-updater et compatibilité Windows.",
  });
}
