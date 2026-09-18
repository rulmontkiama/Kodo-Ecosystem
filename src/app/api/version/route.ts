import { NextResponse } from 'next/server';

export async function GET() {
  return NextResponse.json({
    version: "v1.0.72",
    latestVersion: "1.0.72",
    latest_version: "v1.0.72",
    has_update: true,
    releaseDate: "2026-09-18",
    download_url: "https://kodo-solutions-web.vercel.app/Installation_Kodo_POS.dmg",
    downloadUrl: "https://kodo-solutions-web.vercel.app/Installation_Kodo_POS.dmg",
    dmgUrl: "https://kodo-solutions-web.vercel.app/Installation_Kodo_POS.dmg",
    dmg_url: "https://kodo-solutions-web.vercel.app/Installation_Kodo_POS.dmg",
    distPatchUrl: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.72.zip",
    dist_patch_url: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.72.zip",
    backendPatch: { version: "1.0.72", url: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/backend_v1.0.72.zip" },
    changelog: "v1.0.72 :\n• Clôture Z : si plusieurs jours n'ont jamais été clôturés, elle se fait désormais jour par jour, du plus ancien au plus récent, avec la vraie date affichée.\n• Clôture Z : les paiements par QR Code ne sont plus comptés à tort en « Carte bancaire », le rendu de monnaie déduit deux fois par d'anciennes versions est régularisé (ligne explicative), et un avertissement s'affiche si les règlements ne totalisent pas le chiffre d'affaires.\n• Clôture Z : si elle échoue, un message clair s'affiche et la fenêtre reste ouverte (avant : aucune alerte)."
  });
}
