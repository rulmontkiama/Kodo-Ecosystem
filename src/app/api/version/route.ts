import { NextResponse } from 'next/server';

export async function GET() {
  return NextResponse.json({
    version: "v2.0.7",
    latestVersion: "2.0.7",
    latest_version: "v2.0.7",
    has_update: true,
    releaseDate: "2026-10-10",
    download_url: "https://kodo-solutions-web.vercel.app/Installation_Kodo_POS.dmg",
    downloadUrl: "https://kodo-solutions-web.vercel.app/Installation_Kodo_POS.dmg",
    dmgUrl: "https://kodo-solutions-web.vercel.app/Installation_Kodo_POS.dmg",
    dmg_url: "https://kodo-solutions-web.vercel.app/Installation_Kodo_POS.dmg",
    distPatchUrl: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v2.0.7.zip",
    dist_patch_url: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v2.0.7.zip",
    backendPatch: { version: "2.0.7", url: "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/backend_v2.0.7.zip" },
    changelog: "v2.0.7 :\n• Stocks (Shopify) : l'import du catalogue est refusé tant que des ventes n'ont pas encore été envoyées à Shopify, pour ne plus faire réapparaître en stock des articles déjà vendus. Une copie de sécurité de la base est faite avant chaque import.\n• Boutique en ligne (Shopify) : quand la synchronisation échoue, la cause exacte (jeton refusé, droit manquant, boutique suspendue…) s'affiche dans les Paramètres, au lieu d'un simple échec.\n• Stocks : chaque quantité modifiée par un import ou depuis une fiche produit est désormais consignée (avant / après), pour retrouver ce qui a changé et quand.\n\nv2.0.6 :\n• Stocks (Shopify) : un article décliné en tailles n'est plus dupliqué par taille à l'import ; les doublons déjà créés sont refondus en un seul article, sans perte d'historique de ventes. Relancer l'import du catalogue Shopify pour réparer.\n\nv2.0.5 :\n• Boutique en ligne (Shopify) : échanges plus rapides sur une connexion persistante, file de suivi des ventes en attente, aucun décompte en double après une coupure réseau.\n• Stocks rafraîchis automatiquement après une vente en ligne, avec un bouton « Rafraîchir la synchronisation » dans les Paramètres.\n• Paramètres réorganisés sur une seule page, en sections titrées.\n• Audit de traçabilité NF525 : contrôle des clôtures Z et de la piste d'audit corrigé, sans fausse alerte."
  });
}
