import { NextResponse } from 'next/server';

export async function GET() {
  return NextResponse.json({
    version: "v2.0.5",
    latestVersion: "2.0.5",
    latest_version: "v2.0.5",
    has_update: true,
    releaseDate: "2026-10-01",
    download_url: "https://kodo-solutions-web.vercel.app/Installation_Kodo_POS.dmg",
    downloadUrl: "https://kodo-solutions-web.vercel.app/Installation_Kodo_POS.dmg",
    dmgUrl: "https://kodo-solutions-web.vercel.app/Installation_Kodo_POS.dmg",
    dmg_url: "https://kodo-solutions-web.vercel.app/Installation_Kodo_POS.dmg",
    distPatchUrl: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v2.0.5.zip",
    dist_patch_url: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v2.0.5.zip",
    backendPatch: { version: "2.0.5", url: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/backend_v2.0.5.zip" },
    changelog: "v2.0.5 :\n• Boutique en ligne (Shopify) : échanges plus rapides sur une connexion persistante, file de suivi des ventes en attente, aucun décompte en double après une coupure réseau.\n• Stocks rafraîchis automatiquement après une vente en ligne, avec un bouton « Rafraîchir la synchronisation » dans les Paramètres.\n• Paramètres réorganisés sur une seule page, en sections titrées.\n• Audit de traçabilité NF525 : contrôle des clôtures Z et de la piste d'audit corrigé, sans fausse alerte."
  });
}
