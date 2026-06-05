'use client';
import { useState } from 'react';

export default function ContactForm() {
  const [name, setName] = useState('');
  const [email, setEmail] = useState('');
  const [service, setService] = useState('POS');

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsSubmitting(true);
    
    try {
      const response = await fetch('/api/contact', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, email, service })
      });
      
      const data = await response.json();
      
      if (data.error) {
        setStatus(`Erreur : ${data.error}`);
      } else {
        setStatus('Merci ! Votre demande Kōdo a bien été enregistrée. Nous vous contacterons rapidement.');
        setName('');
        setEmail('');
      }
    } catch (error) {
      setStatus('Une erreur réseau est survenue. Veuillez réessayer.');
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <section id="contact" className="py-24 bg-surface-variant/30 flex justify-center px-6">
      <div className="w-full max-w-5xl grid grid-cols-1 md:grid-cols-2 gap-16 items-center">
        
        <div className="space-y-6">
          <h2 className="text-4xl md:text-5xl font-black text-foreground tracking-tight">
            Prêt à transformer <br/> votre commerce ?
          </h2>
          <p className="text-lg text-foreground/70 font-medium max-w-md">
            Laissez-nous vos coordonnées et la branche qui vous intéresse. Notre équipe d&apos;experts vous recontactera avec une proposition sur mesure.
          </p>
        </div>

        <form onSubmit={handleSubmit} className="w-full bg-surface p-8 md:p-10 rounded-[2rem] shadow-xl border border-outline/30 space-y-5">
          <div className="space-y-1">
            <label className="text-xs font-bold tracking-wider uppercase text-foreground/50 ml-2">Nom du commerce</label>
            <input 
              type="text" 
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Ex: Café Ciseaux" 
              className="w-full p-4 rounded-2xl border border-outline bg-surface-variant/20 focus:border-primary focus:ring-1 focus:ring-primary outline-none transition-all placeholder:text-foreground/30" 
              required 
            />
          </div>
          
          <div className="space-y-1">
            <label className="text-xs font-bold tracking-wider uppercase text-foreground/50 ml-2">Email de contact</label>
            <input 
              type="email" 
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="hello@exemple.com" 
              className="w-full p-4 rounded-2xl border border-outline bg-surface-variant/20 focus:border-primary focus:ring-1 focus:ring-primary outline-none transition-all placeholder:text-foreground/30" 
              required 
            />
          </div>
          
          <div className="space-y-1">
            <label className="text-xs font-bold tracking-wider uppercase text-foreground/50 ml-2">Service souhaité</label>
            <select 
              value={service}
              onChange={(e) => setService(e.target.value)}
              className="w-full p-4 rounded-2xl border border-outline bg-surface-variant/20 focus:border-primary focus:ring-1 focus:ring-primary outline-none transition-all text-foreground cursor-pointer appearance-none"
            >
              <option value="POS">Kōdo POS (Retail / Magasins)</option>
              <option value="Bookings">Kōdo Bookings (Services / Salons)</option>
            </select>
          </div>
          
          <button 
            type="submit" 
            disabled={isSubmitting}
            className={`w-full bg-primary text-white py-4 mt-2 rounded-2xl font-bold uppercase tracking-wider shadow-lg transition-all ${isSubmitting ? 'opacity-70 cursor-not-allowed' : 'hover:bg-primary-dark hover:-translate-y-0.5'}`}
          >
            {isSubmitting ? 'Envoi en cours...' : 'Lancer mon projet'}
          </button>
          
          {status && (
            <p className="text-sm text-center font-bold text-primary bg-primary/10 py-3 rounded-xl mt-4 animate-in fade-in slide-in-from-bottom-2">
              {status}
            </p>
          )}
        </form>

      </div>
    </section>
  );
}
