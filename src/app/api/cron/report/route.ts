import { NextResponse } from 'next/server';

// Type pour structurer nos données agrégées
interface KodoReportData {
  totalLeads: number;
  posLeads: number;
  bookingsLeads: number;
  nouveauxCetteSemaine: number;
  pipelineStatus: Record<string, number>;
}

export async function GET(request: Request) {
  try {
    // 1. Vérification de sécurité Vercel Cron
    const authHeader = request.headers.get('authorization');
    if (process.env.NODE_ENV === 'production') {
      if (authHeader !== `Bearer ${process.env.CRON_SECRET}`) {
        return new Response('Unauthorized', { status: 401 });
      }
    }

    const NOTION_TOKEN = process.env.NOTION_TOKEN;
    const NOTION_DATABASE_ID = process.env.NOTION_DATABASE_ID;
    const DISCORD_WEBHOOK_URL = process.env.DISCORD_WEBHOOK_URL; // Ou RESEND_API_KEY pour email

    if (!NOTION_TOKEN || !NOTION_DATABASE_ID) {
      console.warn("⚠️ Clés Notion manquantes. Impossible de générer le rapport réel.");
      return NextResponse.json({ error: "Configuration Notion manquante." }, { status: 500 });
    }

    // 2. Récupération des données depuis Notion (Kōdo Central)
    // On récupère les leads créés dans les 7 derniers jours
    const oneWeekAgo = new Date();
    oneWeekAgo.setDate(oneWeekAgo.getDate() - 7);

    const response = await fetch(`https://api.notion.com/v1/databases/${NOTION_DATABASE_ID}/query`, {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${NOTION_TOKEN}`,
        'Content-Type': 'application/json',
        'Notion-Version': '2022-06-28'
      },
      // Pas de filtre strict ici pour l'exemple, on récupère tout et on filtre en JS (ou filtre Notion possible)
      body: JSON.stringify({})
    });

    if (!response.ok) throw new Error("Erreur lors du fetch Notion");
    
    const data = await response.json();
    const leads = data.results || [];

    // 3. Traitement et Analyse par "L'Agent Ultra"
    const reportData: KodoReportData = {
      totalLeads: leads.length,
      posLeads: 0,
      bookingsLeads: 0,
      nouveauxCetteSemaine: 0,
      pipelineStatus: {}
    };

    for (const lead of leads) {
      const props = lead.properties;
      
      // Branche
      const branche = props['Branche']?.select?.name;
      if (branche === 'POS') reportData.posLeads++;
      if (branche === 'Bookings') reportData.bookingsLeads++;

      // Statut
      const statut = props['Statut']?.status?.name || 'Non défini';
      reportData.pipelineStatus[statut] = (reportData.pipelineStatus[statut] || 0) + 1;

      // Nouveaux leads
      const dateString = props['Date de demande']?.date?.start;
      if (dateString) {
        const leadDate = new Date(dateString);
        if (leadDate >= oneWeekAgo) {
          reportData.nouveauxCetteSemaine++;
        }
      }
    }

    // 4. Rédaction du Rapport Stratégique
    const markdownReport = `
# 📊 Rapport Stratégique Hebdomadaire - Kōdo Solutions
*Généré par l'Agent Ultra le ${new Date().toLocaleDateString('fr-FR')}*

## 🎯 Acquisition (7 derniers jours)
- **Nouveaux Prospects :** ${reportData.nouveauxCetteSemaine}
- **Répartition Globale :** ${reportData.posLeads} POS | ${reportData.bookingsLeads} Bookings

## 📈 Pipeline Actuel
${Object.entries(reportData.pipelineStatus).map(([statut, count]) => `- ${statut} : ${count}`).join('\\n')}

## 💡 Recommandation de l'Agent
${reportData.posLeads > reportData.bookingsLeads 
  ? "Le marché Retail (POS) est actuellement plus réceptif. Je recommande d'intensifier la prospection sur ce segment la semaine prochaine." 
  : "Les salons (Bookings) montrent une forte traction. Assure-toi de mettre en avant la fonctionnalité de 'Tasting Bar' lors de tes démos."}

*En attente de vos instructions pour la semaine prochaine, CEO.*
    `;

    // 5. Envoi du rapport (Exemple Discord Webhook)
    if (DISCORD_WEBHOOK_URL) {
      await fetch(DISCORD_WEBHOOK_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: markdownReport })
      });
    }

    return NextResponse.json({ 
      success: true, 
      message: "Rapport généré et envoyé.",
      reportPreview: markdownReport 
    });

  } catch (error) {
    console.error("Agent Ultra Error:", error);
    return NextResponse.json({ error: "Échec de la génération du rapport" }, { status: 500 });
  }
}
