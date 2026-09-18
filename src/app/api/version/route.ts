import { NextResponse } from 'next/server';

export async function GET() {
  return NextResponse.json({
    version: "v1.0.70",
    latestVersion: "1.0.70",
    latest_version: "v1.0.70",
    has_update: true,
    releaseDate: "2026-09-18",
    download_url: "https://kodo-solutions-web.vercel.app/Installation_Kodo_POS.dmg",
    downloadUrl: "https://kodo-solutions-web.vercel.app/Installation_Kodo_POS.dmg",
    dmgUrl: "https://kodo-solutions-web.vercel.app/Installation_Kodo_POS.dmg",
    dmg_url: "https://kodo-solutions-web.vercel.app/Installation_Kodo_POS.dmg",
    distPatchUrl: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.70.zip",
    dist_patch_url: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.70.zip",
    changelog: "v1.0.70 :\n• Live Shopping : l'onglet est temporairement verrouillé (affichage flouté « Bientôt disponible ») le temps de finaliser le module. Aucun autre changement de comportement : caisse, stocks, retours et clôture restent identiques."
  });
}
