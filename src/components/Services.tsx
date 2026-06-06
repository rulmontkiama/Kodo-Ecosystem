'use client';

import { motion, Variants } from 'framer-motion';
import { Store, CalendarDays, CheckCircle2, ArrowRight, Code2 } from 'lucide-react';
import Link from 'next/link';

export default function Services() {
  const containerVariants: Variants = {
    hidden: { opacity: 0 },
    visible: {
      opacity: 1,
      transition: { staggerChildren: 0.2 }
    }
  };

  const cardVariants: Variants = {
    hidden: { opacity: 0, y: 30 },
    visible: { 
      opacity: 1, 
      y: 0, 
      transition: { type: "spring", stiffness: 100, damping: 20 } 
    }
  };

  return (
    <section id="services" className="py-32 px-6 relative z-10 overflow-hidden">
      {/* Background glow top right */}
      <div className="absolute top-0 right-0 w-[500px] h-[500px] bg-accent/5 blur-[120px] rounded-full pointer-events-none" />

      <div className="max-w-7xl mx-auto">
        <motion.div 
          initial={{ opacity: 0, y: 20 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true, margin: "-100px" }}
          transition={{ duration: 0.6 }}
          className="text-center mb-24"
        >
          <h2 className="text-4xl md:text-5xl font-black text-foreground tracking-tight mb-6 text-glow">
            Nos solutions <span className="text-accent">métiers</span>
          </h2>
          <p className="text-lg text-foreground/60 max-w-2xl mx-auto font-medium">
            Des outils spécialisés et des créations sur-mesure pour propulser votre activité vers l'excellence digitale.
          </p>
        </motion.div>

        <motion.div 
          variants={containerVariants}
          initial="hidden"
          whileInView="visible"
          viewport={{ once: true, margin: "-100px" }}
          className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-8"
        >
          {/* Card: Kodo POS */}
          <motion.div 
            variants={cardVariants}
            className="group relative glass p-10 rounded-[2.5rem] border border-white/5 hover:border-accent/30 transition-all duration-500 overflow-hidden bg-gradient-to-br from-white/5 to-transparent hover:shadow-2xl hover:shadow-accent/10"
          >
            {/* Card internal glow on hover */}
            <div className="absolute -inset-x-20 -top-20 h-40 bg-accent/20 blur-3xl rounded-full opacity-0 group-hover:opacity-100 transition-opacity duration-500 pointer-events-none" />
            
            <div className="relative z-10">
              <div className="w-16 h-16 rounded-2xl bg-white/5 border border-white/10 flex items-center justify-center mb-8 group-hover:scale-110 group-hover:bg-accent/10 transition-all duration-500">
                <Store size={32} className="text-foreground group-hover:text-accent transition-colors duration-500" />
              </div>
              
              <h3 className="text-2xl font-black text-foreground mb-4 tracking-tight">Kōdo POS</h3>
              <p className="text-foreground/60 leading-relaxed mb-8 font-medium text-sm">
                Le système de caisse intelligent qui révolutionne le retail. Gestion des stocks en temps réel, analytics prédictifs et encaissements unifiés.
              </p>

              <ul className="space-y-4 mb-10 text-sm font-bold tracking-wider text-foreground/70">
                <li className="flex items-center gap-3"><CheckCircle2 size={18} className="text-accent" /> Encaissement multi-canal</li>
                <li className="flex items-center gap-3"><CheckCircle2 size={18} className="text-accent" /> Gestion d'inventaire IA</li>
                <li className="flex items-center gap-3"><CheckCircle2 size={18} className="text-accent" /> Fidélisation client</li>
              </ul>

              <Link href="#contact" className="inline-flex items-center gap-2 text-accent font-black uppercase tracking-widest text-sm hover:gap-4 transition-all mt-auto">
                Découvrir le POS <ArrowRight size={16} />
              </Link>
            </div>
          </motion.div>

          {/* Card: Kodo Bookings */}
          <motion.div 
            variants={cardVariants}
            className="group relative glass p-10 rounded-[2.5rem] border border-white/5 hover:border-accent/30 transition-all duration-500 overflow-hidden bg-gradient-to-br from-white/5 to-transparent hover:shadow-2xl hover:shadow-accent/10 flex flex-col"
          >
            <div className="absolute -inset-x-20 -top-20 h-40 bg-accent/20 blur-3xl rounded-full opacity-0 group-hover:opacity-100 transition-opacity duration-500 pointer-events-none" />
            
            <div className="relative z-10 flex flex-col h-full">
              <div className="w-16 h-16 rounded-2xl bg-white/5 border border-white/10 flex items-center justify-center mb-8 group-hover:scale-110 group-hover:bg-accent/10 transition-all duration-500 shrink-0">
                <CalendarDays size={32} className="text-foreground group-hover:text-accent transition-colors duration-500" />
              </div>
              
              <h3 className="text-2xl font-black text-foreground mb-4 tracking-tight">Kōdo Bookings</h3>
              <p className="text-foreground/60 leading-relaxed mb-8 font-medium text-sm">
                La plateforme de réservation sur-mesure pour les salons et services. Maximisez votre remplissage et éliminez les rendez-vous manqués.
              </p>

              <ul className="space-y-4 mb-10 text-sm font-bold tracking-wider text-foreground/70 flex-grow">
                <li className="flex items-center gap-3"><CheckCircle2 size={18} className="text-accent shrink-0" /> Prise de RDV 24/7</li>
                <li className="flex items-center gap-3"><CheckCircle2 size={18} className="text-accent shrink-0" /> Rappels SMS</li>
                <li className="flex items-center gap-3"><CheckCircle2 size={18} className="text-accent shrink-0" /> Plannings collaborateurs</li>
              </ul>

              <Link href="#contact" className="inline-flex items-center gap-2 text-accent font-black uppercase tracking-widest text-sm hover:gap-4 transition-all">
                Voir Bookings <ArrowRight size={16} />
              </Link>
            </div>
          </motion.div>

          {/* Card: Kodo Web */}
          <motion.div 
            variants={cardVariants}
            className="group relative glass p-10 rounded-[2.5rem] border border-white/5 hover:border-accent/30 transition-all duration-500 overflow-hidden bg-gradient-to-br from-white/5 to-transparent hover:shadow-2xl hover:shadow-accent/10 flex flex-col"
          >
            <div className="absolute -inset-x-20 -top-20 h-40 bg-accent/20 blur-3xl rounded-full opacity-0 group-hover:opacity-100 transition-opacity duration-500 pointer-events-none" />
            
            <div className="relative z-10 flex flex-col h-full">
              <div className="w-16 h-16 rounded-2xl bg-white/5 border border-white/10 flex items-center justify-center mb-8 group-hover:scale-110 group-hover:bg-accent/10 transition-all duration-500 shrink-0">
                <Code2 size={32} className="text-foreground group-hover:text-accent transition-colors duration-500" />
              </div>
              
              <h3 className="text-2xl font-black text-foreground mb-4 tracking-tight">Kōdo Web</h3>
              <p className="text-foreground/60 leading-relaxed mb-8 font-medium text-sm">
                Création de sites web vitrines et e-commerce ultra-performants. Un design premium sur-mesure pour sublimer votre image de marque.
              </p>

              <ul className="space-y-4 mb-10 text-sm font-bold tracking-wider text-foreground/70 flex-grow">
                <li className="flex items-center gap-3"><CheckCircle2 size={18} className="text-accent shrink-0" /> Design unique et moderne</li>
                <li className="flex items-center gap-3"><CheckCircle2 size={18} className="text-accent shrink-0" /> Optimisation SEO avancée</li>
                <li className="flex items-center gap-3"><CheckCircle2 size={18} className="text-accent shrink-0" /> Hébergement cloud Vercel</li>
              </ul>

              <Link href="#contact" className="inline-flex items-center gap-2 text-accent font-black uppercase tracking-widest text-sm hover:gap-4 transition-all">
                Créer mon site <ArrowRight size={16} />
              </Link>
            </div>
          </motion.div>

        </motion.div>
      </div>
    </section>
  );
}
