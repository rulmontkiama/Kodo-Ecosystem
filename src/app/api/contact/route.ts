import { NextResponse } from 'next/server';

export async function POST(request: Request) {
  try {
    const body = await request.json();
    const { name, email, service } = body;

    if (!name || !email || !service) {
      return NextResponse.json({ error: 'Champs manquants' }, { status: 400 });
    }

    const NOTION_TOKEN = process.env.NOTION_TOKEN;
    const NOTION_DATABASE_ID = process.env.NOTION_DATABASE_ID;

    // Si les clés ne sont pas configurées, on simule un succès pour le développement
    if (!NOTION_TOKEN || !NOTION_DATABASE_ID) {
      console.warn("⚠️ Clés Notion manquantes. Simulation d'enregistrement réussie.");
      return NextResponse.json({ success: true, simulated: true });
    }

    const response = await fetch('https://api.notion.com/v1/pages', {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${NOTION_TOKEN}`,
        'Content-Type': 'application/json',
        'Notion-Version': '2022-06-28'
      },
      body: JSON.stringify({
        parent: { database_id: NOTION_DATABASE_ID },
        properties: {
          // Ajuste ces noms de colonnes en fonction de ta base "Kōdo Central"
          "Nom du commerce": {
            title: [
              { text: { content: name } }
            ]
          },
          "Email": {
            email: email
          },
          "Branche": {
            select: { name: service }
          },
          "Statut": {
            status: { name: "Client potentiel" }
          },
          "Source": {
            select: { name: "Inbound (Kōdo Web)" }
          },
          "Date de demande": {
            date: { start: new Date().toISOString() }
          }
        }
      })
    });

    if (!response.ok) {
      const errorText = await response.text();
      console.error("Erreur API Notion:", errorText);
      return NextResponse.json({ error: 'Erreur lors de la communication avec Notion' }, { status: 500 });
    }

    return NextResponse.json({ success: true });
  } catch (error) {
    console.error("Erreur serveur API:", error);
    return NextResponse.json({ error: 'Erreur interne du serveur' }, { status: 500 });
  }
}
