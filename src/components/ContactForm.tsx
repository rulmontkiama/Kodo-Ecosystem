'use client';
import { useState } from 'react';
import { motion, Variants } from 'framer-motion';
import { Building2, Mail, Send, CheckCircle2 } from 'lucide-react';

export default function ContactForm() {
  const [name, setName] = useState('');
  const [email, setEmail] = useState('');
  const [service, setService] = useState('POS');
  const [status, setStatus] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);

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

  const containerVariants = {
    hidden: { opacity: 0 },
    visible: {
      opacity: 1,
      transition: {
        staggerChildren: 0.1
      }
    }
  };

  const itemVariants: Variants = {
    hidden: { opacity: 0, y: 20 },
    visible: { opacity: 1, y: 0, transition: { type: "spring", stiffness: 300, damping: 24 } }
  };

  return (
    <section id="contact" className="py-32 flex justify-center px-6 relative overflow-hidden">
      {/* Background Glow */}
      <div className="absolute bottom-0 right-0 w-[600px] h-[600px] bg-accent/10 blur-[120px] rounded-full pointer-events-none" />

      <div className="w-full max-w-6xl grid grid-cols-1 lg:grid-cols-2 gap-20 items-center z-10">
        
        <motion.div 
          initial={{ opacity: 0, x: -30 }}
          whileInView={{ opacity: 1, x: 0 }}
          viewport={{ once: true, margin: "-100px" }}
          transition={{ duration: 0.6 }}
          className="space-y-8"
        >
          <div className="inline-flex items-center gap-2 text-xs font-bold text-accent tracking-widest uppercase glass px-4 py-2 rounded-full">
            <Building2 size={14} />
            Contact
          </div>
          <h2 className="text-4xl md:text-5xl lg:text-6xl font-black text-foreground tracking-tight leading-[1.1] text-glow">
            Prêt à transformer <br/><span className="text-accent">votre commerce ?</span>
          </h2>
          <p className="text-xl text-foreground/60 font-medium max-w-md leading-relaxed">
            Laissez-nous vos coordonnées et la branche qui vous intéresse. Notre équipe d&apos;experts vous recontactera avec une proposition sur mesure.
          </p>
          
          <div className="pt-8 flex flex-col gap-4 text-sm font-bold tracking-wider uppercase text-foreground/40">
            <p className="flex items-center gap-3"><CheckCircle2 className="text-accent" size={20}/> Devis personnalisé</p>
            <p className="flex items-center gap-3"><CheckCircle2 className="text-accent" size={20}/> Accompagnement sur mesure</p>
            <p className="flex items-center gap-3"><CheckCircle2 className="text-accent" size={20}/> Migration de vos données incluse</p>
          </div>
        </motion.div>

        <motion.form 
          variants={containerVariants}
          initial="hidden"
          whileInView="visible"
          viewport={{ once: true, margin: "-100px" }}
          onSubmit={handleSubmit} 
          className="w-full glass p-8 sm:p-12 rounded-[2.5rem] shadow-2xl space-y-6"
        >
          <motion.div variants={itemVariants} className="space-y-2">
            <label className="text-[10px] font-black tracking-widest uppercase text-foreground/40 ml-4 flex items-center gap-2">
              <Building2 size={12}/> Nom du commerce
            </label>
            <input 
              type="text" 
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Ex: Café Ciseaux" 
              className="w-full p-5 rounded-2xl border border-white/10 bg-white/5 focus:bg-white/10 focus:border-accent focus:ring-4 focus:ring-accent/20 outline-none transition-all placeholder:text-foreground/20 font-medium text-lg text-white" 
              required 
            />
          </motion.div>
          
          <motion.div variants={itemVariants} className="space-y-2">
            <label className="text-[10px] font-black tracking-widest uppercase text-foreground/40 ml-4 flex items-center gap-2">
              <Mail size={12}/> Email de contact
            </label>
            <input 
              type="email" 
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="hello@exemple.com" 
              className="w-full p-5 rounded-2xl border border-white/10 bg-white/5 focus:bg-white/10 focus:border-accent focus:ring-4 focus:ring-accent/20 outline-none transition-all placeholder:text-foreground/20 font-medium text-lg text-white" 
              required 
            />
          </motion.div>
          
          <motion.div variants={itemVariants} className="space-y-2">
            <label className="text-[10px] font-black tracking-widest uppercase text-foreground/40 ml-4">Service souhaité</label>
            <select 
              value={service}
              onChange={(e) => setService(e.target.value)}
              className="w-full p-5 rounded-2xl border border-white/10 bg-white/5 focus:bg-white/10 focus:border-accent focus:ring-4 focus:ring-accent/20 outline-none transition-all cursor-pointer appearance-none font-medium text-lg text-white"
            >
              <option value="POS" className="bg-[#121212]">Kōdo POS (Retail / Magasins)</option>
              <option value="Bookings" className="bg-[#121212]">Kōdo Bookings (Services / Salons)</option>
            </select>
          </motion.div>
          
          <motion.button 
            variants={itemVariants}
            type="submit" 
            disabled={isSubmitting}
            className={`w-full flex justify-center items-center gap-3 bg-accent text-white py-5 mt-4 rounded-2xl font-black uppercase tracking-widest shadow-xl shadow-accent/20 transition-all text-sm ${isSubmitting ? 'opacity-70 cursor-not-allowed scale-95' : 'hover:bg-blue-600 hover:-translate-y-1 hover:shadow-accent/40'}`}
          >
            {isSubmitting ? 'Envoi en cours...' : 'Lancer mon projet'}
            {!isSubmitting && <Send size={18} />}
          </motion.button>
          
          {status && (
            <motion.p 
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              className="text-sm text-center font-bold text-accent bg-accent/10 py-4 rounded-xl mt-4 border border-accent/20"
            >
              {status}
            </motion.p>
          )}
        </motion.form>

      </div>
    </section>
  );
}
