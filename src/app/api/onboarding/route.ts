import { NextResponse } from 'next/server';
import { adminAuth, adminDb } from '@/lib/firebase/admin';

function generateSlug(name: string): string {
  return name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/(^-|-$)/g, '');
}

export async function POST(request: Request) {
  try {
    const body = await request.json();
    const { 
      email, password, nomComplet, nomSalon, 
      horaires, 
      prenomStaff, 
      nomPrestation, prixPrestation, dureePrestation 
    } = body;

    if (!email || !password || !nomComplet || !nomSalon) {
      return NextResponse.json({ error: 'Les informations de base sont requises' }, { status: 400 });
    }

    // 1. Firebase Auth
    const userRecord = await adminAuth.createUser({
      email,
      password,
      displayName: nomComplet,
    });

    const uid = userRecord.uid;
    const slug = generateSlug(nomSalon);

    // 2. Vérification unicité slug
    const existingSalon = await adminDb.collection('salons').where('slug', '==', slug).get();
    let finalSlug = slug;
    if (!existingSalon.empty) {
      finalSlug = `${slug}-${Math.floor(Math.random() * 1000)}`;
    }

    // 3. Batch Firestore
    const batch = adminDb.batch();

    // -- Salon --
    const salonRef = adminDb.collection('salons').doc();
    const salonId = salonRef.id;

    batch.set(salonRef, {
      slug: finalSlug,
      nom: nomSalon,
      owner_uid: uid,
      primaryColor: '#000000',
      horaires: horaires || [], // Ex: ['Lundi', 'Mardi', 'Jeudi']
      createdAt: new Date().toISOString()
    });

    // -- User Profile --
    const userProfileRef = adminDb.collection('users').doc(uid);
    batch.set(userProfileRef, {
      nom_complet: nomComplet,
      role: 'gerant',
      salon_id: salonId,
      createdAt: new Date().toISOString()
    });

    // -- Staff Initial --
    if (prenomStaff) {
      const staffRef = salonRef.collection('staff').doc();
      batch.set(staffRef, {
        nom: prenomStaff,
        createdAt: new Date().toISOString()
      });
    }

    // -- Prestation Initiale --
    if (nomPrestation && prixPrestation && dureePrestation) {
      const serviceRef = salonRef.collection('services').doc();
      batch.set(serviceRef, {
        nom: nomPrestation,
        prix: Number(prixPrestation),
        duree: Number(dureePrestation), // en minutes
        createdAt: new Date().toISOString()
      });
    }

    await batch.commit();

    return NextResponse.json({ 
      success: true, 
      salonId,
      slug: finalSlug 
    });
  } catch (error: any) {
    console.error('Erreur Onboarding API:', error);
    return NextResponse.json({ error: error.message || 'Erreur interne du serveur' }, { status: 500 });
  }
}
