'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';

const JOURS = ['Lundi', 'Mardi', 'Mercredi', 'Jeudi', 'Vendredi', 'Samedi', 'Dimanche'];

export default function OnboardingPage() {
  const router = useRouter();
  const [step, setStep] = useState(1);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const [formData, setFormData] = useState({
    nomComplet: '',
    email: '',
    password: '',
    nomSalon: '',
    horaires: ['Mardi', 'Mercredi', 'Jeudi', 'Vendredi', 'Samedi'], // Jours ouverts par défaut
    prenomStaff: '',
    nomPrestation: '',
    prixPrestation: '',
    dureePrestation: '30'
  });

  const handleChange = (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => {
    setFormData({ ...formData, [e.target.name]: e.target.value });
  };

  const toggleJour = (jour: string) => {
    setFormData(prev => ({
      ...prev,
      horaires: prev.horaires.includes(jour)
        ? prev.horaires.filter(j => j !== jour)
        : [...prev.horaires, jour]
    }));
  };

  const nextStep = () => setStep(s => s + 1);
  const prevStep = () => setStep(s => s - 1);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (step < 3) {
      nextStep();
      return;
    }

    setLoading(true);
    setError('');

    try {
      const response = await fetch('/api/onboarding', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(formData)
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.error || 'Erreur lors de la création de votre espace');
      }

      router.push(`/reservation/${data.slug}`);
    } catch (err: any) {
      setError(err.message);
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen bg-[#FAFAFA] text-gray-900 flex items-center justify-center p-6 selection:bg-gray-200">
      <div className="w-full max-w-xl bg-white border border-gray-100 shadow-[0_8px_30px_rgb(0,0,0,0.04)] rounded-[32px] p-10 relative overflow-hidden">
        
        {/* Progress Bar */}
        <div className="absolute top-0 left-0 w-full h-1.5 bg-gray-100">
          <div 
            className="h-full bg-black transition-all duration-500 ease-out" 
            style={{ width: `${(step / 3) * 100}%` }}
          />
        </div>

        <div className="mb-10 mt-4 text-center">
          <h1 className="text-[28px] font-semibold tracking-tight mb-2 text-black">
            {step === 1 && "Créons votre Salon"}
            {step === 2 && "Vos Horaires"}
            {step === 3 && "Votre Carte"}
          </h1>
          <p className="text-gray-500 text-sm font-medium">
            Étape {step} sur 3
          </p>
        </div>

        {error && (
          <div className="mb-6 p-4 bg-red-50 text-red-600 rounded-2xl text-sm text-center border border-red-100">
            {error}
          </div>
        )}

        <form onSubmit={handleSubmit} className="space-y-6">
          
          {/* ETAPE 1 : Infos de base */}
          {step === 1 && (
            <div className="space-y-5 animate-in fade-in slide-in-from-right-4 duration-500">
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1.5 ml-1">Nom du Salon</label>
                <input type="text" name="nomSalon" required value={formData.nomSalon} onChange={handleChange} className="w-full px-4 py-3.5 bg-[#F9F9F9] border border-gray-200 rounded-2xl focus:outline-none focus:ring-2 focus:ring-black/5 focus:border-gray-400 transition-all placeholder-gray-400" placeholder="L'Adresse" />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1.5 ml-1">Nom complet (Gérant)</label>
                <input type="text" name="nomComplet" required value={formData.nomComplet} onChange={handleChange} className="w-full px-4 py-3.5 bg-[#F9F9F9] border border-gray-200 rounded-2xl focus:outline-none focus:ring-2 focus:ring-black/5 focus:border-gray-400 transition-all placeholder-gray-400" placeholder="Jean Dupont" />
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1.5 ml-1">Email</label>
                  <input type="email" name="email" required value={formData.email} onChange={handleChange} className="w-full px-4 py-3.5 bg-[#F9F9F9] border border-gray-200 rounded-2xl focus:outline-none focus:ring-2 focus:ring-black/5 focus:border-gray-400 transition-all placeholder-gray-400" placeholder="jean@salon.com" />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1.5 ml-1">Mot de passe</label>
                  <input type="password" name="password" required value={formData.password} onChange={handleChange} className="w-full px-4 py-3.5 bg-[#F9F9F9] border border-gray-200 rounded-2xl focus:outline-none focus:ring-2 focus:ring-black/5 focus:border-gray-400 transition-all placeholder-gray-400" placeholder="••••••••" />
                </div>
              </div>
            </div>
          )}

          {/* ETAPE 2 : Horaires */}
          {step === 2 && (
            <div className="space-y-5 animate-in fade-in slide-in-from-right-4 duration-500">
              <label className="block text-sm font-medium text-gray-700 mb-1.5 ml-1 text-center">Sélectionnez vos jours d'ouverture</label>
              <div className="flex flex-wrap gap-3 justify-center">
                {JOURS.map(jour => {
                  const isActive = formData.horaires.includes(jour);
                  return (
                    <button
                      key={jour}
                      type="button"
                      onClick={() => toggleJour(jour)}
                      className={`px-4 py-3 rounded-2xl text-sm font-medium transition-all ${isActive ? 'bg-black text-white shadow-md' : 'bg-[#F9F9F9] text-gray-500 border border-gray-200 hover:border-gray-300'}`}
                    >
                      {jour}
                    </button>
                  );
                })}
              </div>
            </div>
          )}

          {/* ETAPE 3 : Staff et Prestations */}
          {step === 3 && (
            <div className="space-y-6 animate-in fade-in slide-in-from-right-4 duration-500">
              <div className="p-5 bg-[#F9F9F9] border border-gray-100 rounded-3xl space-y-4">
                <h3 className="font-semibold text-sm text-gray-900 ml-1">Premier collaborateur</h3>
                <div>
                  <input type="text" name="prenomStaff" value={formData.prenomStaff} onChange={handleChange} className="w-full px-4 py-3 bg-white border border-gray-200 rounded-2xl focus:outline-none focus:ring-2 focus:ring-black/5 transition-all placeholder-gray-400" placeholder="Prénom (ex: Alice)" />
                </div>
              </div>

              <div className="p-5 bg-[#F9F9F9] border border-gray-100 rounded-3xl space-y-4">
                <h3 className="font-semibold text-sm text-gray-900 ml-1">Première prestation</h3>
                <div>
                  <input type="text" name="nomPrestation" value={formData.nomPrestation} onChange={handleChange} className="w-full px-4 py-3 bg-white border border-gray-200 rounded-2xl focus:outline-none focus:ring-2 focus:ring-black/5 transition-all mb-3 placeholder-gray-400" placeholder="Nom (ex: Coupe Homme)" />
                </div>
                <div className="grid grid-cols-2 gap-3">
                  <div className="relative">
                    <input type="number" name="prixPrestation" value={formData.prixPrestation} onChange={handleChange} className="w-full px-4 py-3 bg-white border border-gray-200 rounded-2xl focus:outline-none focus:ring-2 focus:ring-black/5 transition-all pr-8" placeholder="Prix" />
                    <span className="absolute right-4 top-3.5 text-gray-400">€</span>
                  </div>
                  <div className="relative">
                    <select name="dureePrestation" value={formData.dureePrestation} onChange={handleChange} className="w-full px-4 py-3 bg-white border border-gray-200 rounded-2xl focus:outline-none focus:ring-2 focus:ring-black/5 transition-all appearance-none text-gray-700">
                      <option value="15">15 min</option>
                      <option value="30">30 min</option>
                      <option value="45">45 min</option>
                      <option value="60">1h</option>
                      <option value="90">1h30</option>
                    </select>
                  </div>
                </div>
              </div>
            </div>
          )}

          {/* Navigation */}
          <div className="flex gap-3 pt-4">
            {step > 1 && (
              <button type="button" onClick={prevStep} disabled={loading} className="w-1/3 py-4 px-4 bg-gray-100 text-gray-900 font-medium rounded-2xl hover:bg-gray-200 transition-all">
                Retour
              </button>
            )}
            <button
              type="submit"
              disabled={loading}
              className={`py-4 px-4 bg-black text-white font-medium rounded-2xl hover:bg-gray-800 transition-all shadow-sm flex justify-center items-center ${step === 1 ? 'w-full' : 'flex-1'}`}
            >
              {loading ? (
                <span className="flex items-center gap-2">
                  <svg className="animate-spin h-4 w-4 text-white" viewBox="0 0 24 24"><circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none"></circle><path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path></svg>
                  Génération...
                </span>
              ) : step === 3 ? (
                'Lancer mon SaaS'
              ) : (
                'Continuer'
              )}
            </button>
          </div>

        </form>
      </div>
    </div>
  );
}
