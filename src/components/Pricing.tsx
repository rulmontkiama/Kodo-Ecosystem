'use client';
import { motion, Variants } from 'framer-motion';
import { CheckCircle2, Star } from 'lucide-react';

export default function Pricing() {
  const containerVariants = {
    hidden: { opacity: 0 },
    visible: {
      opacity: 1,
      transition: { staggerChildren: 0.2 }
    }
  };

  const itemVariants: Variants = {
    hidden: { opacity: 0, y: 30 },
    visible: { opacity: 1, y: 0, transition: { type: "spring", stiffness: 300, damping: 24 } }
  };

  return (
    <section id="pricing" className="py-32 flex flex-col items-center px-6 relative z-10 bg-black/50">
      <div className="text-center space-y-6 mb-20 max-w-3xl">
        <h2 className="text-4xl md:text-5xl font-black text-foreground tracking-tight leading-tight">
          Des tarifs simples, <span className="text-accent">sans surprise.</span>
        </h2>
        <p className="text-xl text-foreground/60 font-medium">
          Investissez dans la technologie qui fera grandir votre commerce. Aucun frais caché.
        </p>
      </div>

      <motion.div 
        variants={containerVariants}
        initial="hidden"
        whileInView="visible"
        viewport={{ once: true, margin: "-100px" }}
        className="grid grid-cols-1 md:grid-cols-3 gap-8 w-full max-w-6xl"
      >
        {/* Kodo POS */}
        <motion.div variants={itemVariants} className="glass p-8 rounded-3xl border border-white/10 flex flex-col hover:-translate-y-2 transition-transform duration-300">
          <h3 className="text-2xl font-black tracking-widest uppercase mb-2">Kōdo POS</h3>
          <p className="text-foreground/60 text-sm mb-6 h-10">La caisse intelligente pour les commerces physiques.</p>
          <div className="text-5xl font-black text-white mb-8 flex items-end gap-1">
            49€ <span className="text-lg text-foreground/40 font-bold">/mois</span>
          </div>
          <ul className="space-y-4 mb-8 flex-1">
            <li className="flex items-center gap-3 text-sm font-medium"><CheckCircle2 size={18} className="text-accent" /> Logiciel certifié NF525</li>
            <li className="flex items-center gap-3 text-sm font-medium"><CheckCircle2 size={18} className="text-accent" /> Mode hors-ligne intégré</li>
            <li className="flex items-center gap-3 text-sm font-medium"><CheckCircle2 size={18} className="text-accent" /> Gestion des stocks</li>
            <li className="flex items-center gap-3 text-sm font-medium"><CheckCircle2 size={18} className="text-accent" /> Analytics avancés</li>
          </ul>
          <a href="#contact" className="w-full text-center py-4 rounded-xl border border-white/20 hover:bg-white/10 transition-colors font-bold tracking-widest uppercase text-sm">
            Choisir Kōdo POS
          </a>
        </motion.div>

        {/* Kodo Bookings */}
        <motion.div variants={itemVariants} className="glass p-8 rounded-3xl border-2 border-accent relative flex flex-col hover:-translate-y-2 transition-transform duration-300 bg-accent/5 shadow-[0_0_40px_rgba(255,127,127,0.15)]">
          <div className="absolute -top-4 left-1/2 -translate-x-1/2 bg-accent text-white text-xs font-black uppercase tracking-widest px-4 py-1.5 rounded-full flex items-center gap-1">
            <Star size={12} className="fill-white" /> Le plus demandé
          </div>
          <h3 className="text-2xl font-black tracking-widest uppercase mb-2 text-accent">Kōdo Bookings</h3>
          <p className="text-foreground/60 text-sm mb-6 h-10">L'app de réservation parfaite pour les salons.</p>
          <div className="text-5xl font-black text-white mb-8 flex items-end gap-1">
            49€ <span className="text-lg text-foreground/40 font-bold">/mois</span>
          </div>
          <ul className="space-y-4 mb-8 flex-1">
            <li className="flex items-center gap-3 text-sm font-medium"><CheckCircle2 size={18} className="text-accent" /> Prise de RDV 24/7</li>
            <li className="flex items-center gap-3 text-sm font-medium"><CheckCircle2 size={18} className="text-accent" /> PWA (Application Mobile)</li>
            <li className="flex items-center gap-3 text-sm font-medium"><CheckCircle2 size={18} className="text-accent" /> Synchronisation avec Kōdo POS</li>
            <li className="flex items-center gap-3 text-sm font-medium"><CheckCircle2 size={18} className="text-accent" /> Rappels SMS/Email</li>
          </ul>
          <a href="#contact" className="w-full text-center py-4 rounded-xl bg-accent text-white hover:opacity-90 transition-opacity font-black tracking-widest uppercase text-sm shadow-xl shadow-accent/20">
            Choisir Kōdo Bookings
          </a>
        </motion.div>

        {/* Kodo Web */}
        <motion.div variants={itemVariants} className="glass p-8 rounded-3xl border border-white/10 flex flex-col hover:-translate-y-2 transition-transform duration-300">
          <h3 className="text-2xl font-black tracking-widest uppercase mb-2">Kōdo Web</h3>
          <p className="text-foreground/60 text-sm mb-6 h-10">Création de sites vitrines et E-commerce sur mesure.</p>
          <div className="text-5xl font-black text-white mb-8 flex items-end gap-1">
            Sur devis
          </div>
          <ul className="space-y-4 mb-8 flex-1">
            <li className="flex items-center gap-3 text-sm font-medium"><CheckCircle2 size={18} className="text-accent" /> Design Premium sur mesure</li>
            <li className="flex items-center gap-3 text-sm font-medium"><CheckCircle2 size={18} className="text-accent" /> Optimisation SEO (Google)</li>
            <li className="flex items-center gap-3 text-sm font-medium"><CheckCircle2 size={18} className="text-accent" /> Performance ultra-rapide</li>
            <li className="flex items-center gap-3 text-sm font-medium"><CheckCircle2 size={18} className="text-accent" /> Maintenance incluse</li>
          </ul>
          <a href="#contact" className="w-full text-center py-4 rounded-xl border border-white/20 hover:bg-white/10 transition-colors font-bold tracking-widest uppercase text-sm">
            Demander un devis
          </a>
        </motion.div>
      </motion.div>
    </section>
  );
}
