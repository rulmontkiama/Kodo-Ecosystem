import { adminDb } from '@/lib/firebase/admin';
import { notFound } from 'next/navigation';

export default async function ReservationPage({ params }: { params: { slug: string } }) {
  // 1. Fetch the salon by slug
  const salonsSnapshot = await adminDb.collection('salons').where('slug', '==', params.slug).get();
  
  if (salonsSnapshot.empty) {
    notFound();
  }

  const salonDoc = salonsSnapshot.docs[0];
  const salon = salonDoc.data();
  const salonId = salonDoc.id;

  // 2. Fetch services and staff
  const servicesSnapshot = await adminDb.collection(`salons/${salonId}/services`).get();
  const services = servicesSnapshot.docs.map(doc => ({ id: doc.id, ...doc.data() }));

  const staffSnapshot = await adminDb.collection(`salons/${salonId}/staff`).get();
  const staffList = staffSnapshot.docs.map(doc => ({ id: doc.id, ...doc.data() }));

  return (
    <div className="min-h-screen bg-[#FAFAFA] text-gray-900 pb-20">
      {/* Header du Salon */}
      <header className="bg-white border-b border-gray-100 pt-16 pb-12 px-6">
        <div className="max-w-3xl mx-auto text-center">
          <div className="w-20 h-20 bg-gray-100 rounded-[28px] mx-auto mb-6 flex items-center justify-center text-3xl shadow-sm">
            ✂️
          </div>
          <h1 className="text-4xl font-semibold tracking-tight text-black mb-3">{salon.nom}</h1>
          <p className="text-gray-500 font-medium">Réservez votre prestation en quelques clics.</p>
        </div>
      </header>

      <main className="max-w-3xl mx-auto px-6 py-12 space-y-12">
        
        {/* Horaires Section */}
        <section className="bg-white p-8 rounded-[32px] border border-gray-100 shadow-[0_8px_30px_rgb(0,0,0,0.03)]">
          <h2 className="text-xl font-semibold mb-6">Heures d'ouverture</h2>
          <div className="flex flex-wrap gap-3">
            {salon.horaires?.map((jour: string) => (
              <span key={jour} className="px-4 py-2.5 bg-[#F9F9F9] rounded-2xl text-sm font-medium text-gray-700">
                {jour}
              </span>
            ))}
            {(!salon.horaires || salon.horaires.length === 0) && (
              <p className="text-sm text-gray-400">Horaires non définis.</p>
            )}
          </div>
        </section>

        {/* Prestations Section */}
        <section>
          <h2 className="text-xl font-semibold mb-6 ml-2">Nos Prestations</h2>
          <div className="grid gap-4">
            {services.map((service: any) => (
              <div key={service.id} className="bg-white p-5 rounded-[28px] border border-gray-100 shadow-[0_4px_20px_rgb(0,0,0,0.02)] flex justify-between items-center group hover:border-gray-300 transition-all cursor-pointer">
                <div>
                  <h3 className="font-medium text-lg text-black">{service.nom}</h3>
                  <p className="text-gray-500 text-sm mt-1">{service.duree} min</p>
                </div>
                <div className="flex items-center gap-5">
                  <span className="font-semibold">{service.prix} €</span>
                  <button className="w-10 h-10 bg-[#F9F9F9] rounded-full flex items-center justify-center text-black group-hover:bg-black group-hover:text-white transition-all">
                    +
                  </button>
                </div>
              </div>
            ))}
            {services.length === 0 && (
              <p className="text-sm text-gray-400 ml-2">Aucune prestation disponible pour le moment.</p>
            )}
          </div>
        </section>

        {/* Staff Section */}
        <section>
          <h2 className="text-xl font-semibold mb-6 ml-2">Notre Équipe</h2>
          <div className="flex gap-4 overflow-x-auto pb-4 hide-scrollbar">
            {staffList.map((staff: any) => (
              <div key={staff.id} className="min-w-[120px] bg-white p-5 rounded-[28px] border border-gray-100 shadow-[0_4px_20px_rgb(0,0,0,0.02)] text-center cursor-pointer hover:border-gray-300 transition-all">
                <div className="w-14 h-14 bg-gray-100 rounded-full mx-auto mb-3 flex items-center justify-center font-semibold text-gray-600">
                  {staff.nom.charAt(0)}
                </div>
                <p className="font-medium text-sm text-black">{staff.nom}</p>
              </div>
            ))}
            {staffList.length === 0 && (
              <p className="text-sm text-gray-400 ml-2">Aucun collaborateur renseigné.</p>
            )}
          </div>
        </section>

      </main>
    </div>
  );
}
