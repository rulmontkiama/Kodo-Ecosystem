'use client';

import { motion } from 'framer-motion';
import { ArrowLeft, CheckCircle2, CalendarDays, Smartphone, Clock, Users } from 'lucide-react';
import Link from 'next/link';
import Image from 'next/image';


export default function KodoBookingsPage() {
  return (
    <div className="min-h-screen flex flex-col relative z-10">
      
      {/* Background Glows */}
      <div className="fixed top-20 left-10 w-[500px] h-[500px] bg-accent/5 blur-[120px] rounded-full pointer-events-none -z-10" />
      <div className="fixed bottom-20 right-10 w-[600px] h-[600px] bg-accent/5 blur-[120px] rounded-full pointer-events-none -z-10" />

      <main className="flex-1 pt-32 pb-20 px-6">
        <div className="max-w-4xl mx-auto">
          
          <Link href="/#services" className="inline-flex items-center gap-2 text-foreground/50 hover:text-accent transition-colors font-semibold tracking-wider text-sm uppercase mb-12">
            <ArrowLeft size={16} /> Retour aux services
          </Link>

          {/* Hero Section */}
          <div className="mb-20 grid grid-cols-1 lg:grid-cols-2 gap-12 items-center">
            <motion.div 
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.6 }}
            >
              <div className="w-20 h-20 rounded-3xl bg-white/5 border border-white/10 flex items-center justify-center shadow-2xl shadow-accent/10 mb-8">
                <CalendarDays size={40} className="text-accent" />
              </div>
              <h1 className="text-4xl md:text-6xl font-black text-foreground tracking-tight mb-6 text-glow">
                Kōdo <span className="text-accent">Bookings</span>
              </h1>
              <p className="text-xl text-foreground/70 font-medium leading-relaxed mb-8">
                La plateforme de réservation ultime. Remplissez votre carnet d'adresses sans effort et offrez une expérience fluide à vos clients.
              </p>
              <Link href="/#contact" className="inline-block bg-accent text-accent-foreground font-black px-8 py-4 rounded-full tracking-widest uppercase text-sm hover:scale-105 hover:shadow-[0_0_40px_rgba(var(--accent),0.4)] transition-all">
                Ouvrir un agenda
              </Link>
            </motion.div>

            <motion.div
              initial={{ opacity: 0, scale: 0.9 }}
              animate={{ opacity: 1, scale: 1 }}
              transition={{ duration: 0.8, delay: 0.2 }}
              className="relative"
            >
              <div className="absolute inset-0 bg-accent/20 blur-[100px] rounded-full" />
              <div className="relative rounded-[2.5rem] overflow-hidden border border-white/10 shadow-2xl">
                <Image 
                  src="/images/bookings_mockup.png" 
                  alt="Kōdo Bookings Mobile App 3D Mockup" 
                  width={800} 
                  height={800}
                  className="w-full h-auto object-cover transform hover:scale-105 transition-transform duration-700"
                  priority
                />
              </div>
            </motion.div>
          </div>

          {/* Features Grid */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-6 mb-20">
            <motion.div 
              initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.1 }}
              className="glass p-8 rounded-[2rem] border border-white/5 hover:border-accent/30 transition-colors"
            >
              <Smartphone className="w-10 h-10 text-accent mb-6" />
              <h3 className="text-xl font-bold mb-3 text-foreground tracking-tight">Prise de RDV 24/7</h3>
              <p className="text-foreground/60 leading-relaxed text-sm">
                Vos clients peuvent réserver de jour comme de nuit depuis leur téléphone ou ordinateur, sans que vous n'ayez à répondre au téléphone.
              </p>
            </motion.div>

            <motion.div 
              initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.2 }}
              className="glass p-8 rounded-[2rem] border border-white/5 hover:border-accent/30 transition-colors"
            >
              <Clock className="w-10 h-10 text-accent mb-6" />
              <h3 className="text-xl font-bold mb-3 text-foreground tracking-tight">Rappels SMS & Emails</h3>
              <p className="text-foreground/60 leading-relaxed text-sm">
                Éradiquez les "no-shows". Des rappels automatiques sont envoyés à vos clients pour confirmer leur présence.
              </p>
            </motion.div>

            <motion.div 
              initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.3 }}
              className="glass p-8 rounded-[2rem] border border-white/5 hover:border-accent/30 transition-colors"
            >
              <Users className="w-10 h-10 text-accent mb-6" />
              <h3 className="text-xl font-bold mb-3 text-foreground tracking-tight">Plannings d'Équipe</h3>
              <p className="text-foreground/60 leading-relaxed text-sm">
                Gérez les emplois du temps de tous vos collaborateurs. Assignez des spécialités et définissez les horaires d'ouverture facilement.
              </p>
            </motion.div>

            <motion.div 
              initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.4 }}
              className="glass p-8 rounded-[2rem] border border-white/5 hover:border-accent/30 transition-colors"
            >
              <CheckCircle2 className="w-10 h-10 text-accent mb-6" />
              <h3 className="text-xl font-bold mb-3 text-foreground tracking-tight">Intégration Parfaite</h3>
              <p className="text-foreground/60 leading-relaxed text-sm">
                Connecté nativement avec Kōdo POS. Les rendez-vous pris en ligne s'affichent directement sur votre caisse.
              </p>
            </motion.div>
          </div>

          {/* Benefits Section */}
          <motion.div 
            initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.5 }}
            className="bg-accent/10 rounded-[2.5rem] p-10 md:p-14 border border-accent/20 relative overflow-hidden"
          >
            <div className="absolute top-0 right-0 w-64 h-64 bg-accent/20 blur-[80px] rounded-full pointer-events-none" />
            
            <h2 className="text-3xl font-black mb-8 text-foreground tracking-tight relative z-10">
              Transformez votre salon
            </h2>
            
            <div className="grid grid-cols-1 md:grid-cols-2 gap-6 relative z-10">
              <ul className="space-y-4">
                <li className="flex items-start gap-4">
                  <CheckCircle2 size={24} className="text-accent shrink-0 mt-0.5" />
                  <span className="text-foreground/80 font-medium">-70% de rendez-vous non honorés.</span>
                </li>
                <li className="flex items-start gap-4">
                  <CheckCircle2 size={24} className="text-accent shrink-0 mt-0.5" />
                  <span className="text-foreground/80 font-medium">+30% de nouveaux clients grâce à l'accessibilité web.</span>
                </li>
              </ul>
              <ul className="space-y-4">
                <li className="flex items-start gap-4">
                  <CheckCircle2 size={24} className="text-accent shrink-0 mt-0.5" />
                  <span className="text-foreground/80 font-medium">Réduction du stress lié aux appels téléphoniques incessants.</span>
                </li>
                <li className="flex items-start gap-4">
                  <CheckCircle2 size={24} className="text-accent shrink-0 mt-0.5" />
                  <span className="text-foreground/80 font-medium">Base de données clients enrichie et sécurisée.</span>
                </li>
              </ul>
            </div>
          </motion.div>

        </div>
      </main>
      
      <footer className="bg-foreground text-background py-8 text-center mt-auto">
        <p className="text-sm font-medium opacity-80">
          &copy; {new Date().getFullYear()} Kōdo Solutions. Tous droits réservés.
        </p>
      </footer>
    </div>
  );
}
