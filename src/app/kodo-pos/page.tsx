'use client';

import { useState } from 'react';

export default function KodoPosPage() {
  const [formData, setFormData] = useState({
    nom: '',
    email: '',
    salon: ''
  });
  const [loading, setLoading] = useState(false);
  const [success, setSuccess] = useState(false);
  const [downloadUrl, setDownloadUrl] = useState('');
  const [error, setError] = useState('');

  const handleChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    setFormData({ ...formData, [e.target.name]: e.target.value });
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError('');

    try {
      const response = await fetch('/api/pos-capture', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(formData)
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.error || 'Erreur lors de la soumission');
      }

      setDownloadUrl(data.downloadUrl);
      setSuccess(true);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  if (success) {
    return (
      <div className="min-h-screen bg-[#FAFAFA] flex items-center justify-center p-6 selection:bg-gray-200">
        <div className="w-full max-w-md bg-white border border-gray-100 shadow-[0_8px_30px_rgb(0,0,0,0.04)] rounded-[32px] p-12 text-center animate-in fade-in zoom-in-95 duration-500">
          <div className="w-20 h-20 bg-[#F9F9F9] rounded-full mx-auto mb-6 flex items-center justify-center text-4xl">
            ✨
          </div>
          <h1 className="text-[28px] font-semibold tracking-tight mb-3 text-black">C'est prêt !</h1>
          <p className="text-gray-500 font-medium mb-8">
            Bienvenue dans Kōdo POS. Vous pouvez télécharger l'application macOS ci-dessous.
          </p>
          <a
            href={downloadUrl}
            download
            className="w-full py-4 px-4 bg-black text-white font-medium rounded-2xl hover:bg-gray-800 transition-all shadow-sm flex justify-center items-center gap-2"
          >
            <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
            </svg>
            Télécharger pour Mac
          </a>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-[#FAFAFA] text-gray-900 flex items-center justify-center p-6 selection:bg-gray-200">
      <div className="w-full max-w-md bg-white border border-gray-100 shadow-[0_8px_30px_rgb(0,0,0,0.04)] rounded-[32px] p-10">
        
        <div className="mb-10 text-center">
          <div className="w-16 h-16 bg-black text-white rounded-[24px] mx-auto mb-6 flex items-center justify-center font-bold text-2xl shadow-sm">
            K
          </div>
          <h1 className="text-[28px] font-semibold tracking-tight mb-2 text-black">Obtenir Kōdo POS</h1>
          <p className="text-gray-500 text-sm font-medium">L'application de caisse ultra-minimaliste pour macOS.</p>
        </div>

        {error && (
          <div className="mb-6 p-4 bg-red-50 text-red-600 rounded-2xl text-sm text-center border border-red-100">
            {error}
          </div>
        )}

        <form onSubmit={handleSubmit} className="space-y-5">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1.5 ml-1">Prénom & Nom</label>
            <input
              type="text"
              name="nom"
              required
              value={formData.nom}
              onChange={handleChange}
              className="w-full px-4 py-3.5 bg-[#F9F9F9] border border-gray-200 rounded-2xl focus:outline-none focus:ring-2 focus:ring-black/5 focus:border-gray-400 transition-all placeholder-gray-400"
              placeholder="Jean Dupont"
            />
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1.5 ml-1">Email pro</label>
            <input
              type="email"
              name="email"
              required
              value={formData.email}
              onChange={handleChange}
              className="w-full px-4 py-3.5 bg-[#F9F9F9] border border-gray-200 rounded-2xl focus:outline-none focus:ring-2 focus:ring-black/5 focus:border-gray-400 transition-all placeholder-gray-400"
              placeholder="jean@salon.com"
            />
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1.5 ml-1">Nom du Salon</label>
            <input
              type="text"
              name="salon"
              required
              value={formData.salon}
              onChange={handleChange}
              className="w-full px-4 py-3.5 bg-[#F9F9F9] border border-gray-200 rounded-2xl focus:outline-none focus:ring-2 focus:ring-black/5 focus:border-gray-400 transition-all placeholder-gray-400"
              placeholder="L'Adresse"
            />
          </div>

          <button
            type="submit"
            disabled={loading}
            className="w-full py-4 px-4 bg-black text-white font-medium rounded-2xl hover:bg-gray-800 transition-all shadow-sm flex justify-center items-center mt-8"
          >
            {loading ? (
              <span className="flex items-center gap-2">
                <svg className="animate-spin h-4 w-4 text-white" viewBox="0 0 24 24"><circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none"></circle><path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path></svg>
                Préparation...
              </span>
            ) : (
              'Télécharger l\'application'
            )}
          </button>
        </form>
      </div>
    </div>
  );
}
