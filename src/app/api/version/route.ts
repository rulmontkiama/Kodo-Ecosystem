import { NextResponse } from 'next/server';

export async function GET() {
  return NextResponse.json({
    version: "v1.0.73",
    latestVersion: "1.0.73",
    latest_version: "v1.0.73",
    has_update: true,
    releaseDate: "2026-09-20",
    download_url: "https://kodo-solutions-web.vercel.app/Installation_Kodo_POS.dmg",
    downloadUrl: "https://kodo-solutions-web.vercel.app/Installation_Kodo_POS.dmg",
    dmgUrl: "https://kodo-solutions-web.vercel.app/Installation_Kodo_POS.dmg",
    dmg_url: "https://kodo-solutions-web.vercel.app/Installation_Kodo_POS.dmg",
    distPatchUrl: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.73.zip",
    dist_patch_url: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.73.zip",
    backendPatch: { version: "1.0.73", url: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/backend_v1.0.73.zip" },
    changelog: "v1.0.73 :\n• Ventes : correction du refus « Article introuvable en base » et des ventes enregistrées sur un autre article. La taille choisie est respectée et le bon stock est décompté.\n• Import Shopify : réimporter le catalogue conserve les articles, leur TVA, leur marque et leur seuil d'alerte, et importe aussi les catalogues de plus de 250 produits.\n• Réglages : modifier le fond de caisse ou le seuil d'alerte n'efface plus l'adresse, le n° BCE, le n° de TVA ni l'adresse de l'imprimante ; un fond de caisse à 0 € est enregistré.\n• Démarrage : ouvrir l'application une seconde fois n'arrête plus le serveur en cours, et l'interface affichée est toujours la plus récente."
  });
}
