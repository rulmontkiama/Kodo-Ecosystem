"use client";

import React from 'react';
import Link from 'next/link';
import { motion } from 'framer-motion';
import { ArrowRight, Sparkles } from 'lucide-react';

export default function Hero() {
  return (
    <div className="min-h-screen bg-background flex flex-col items-center justify-center p-8 relative overflow-hidden bg-grid-pattern">
      {/* Background decoration */}
      <div className="absolute top-0 left-1/2 -translate-x-1/2 w-[800px] h-[500px] bg-accent/20 blur-[120px] rounded-full pointer-events-none" />
      
      <nav className="fixed top-0 left-0 right-0 w-full z-40 glass border-b border-white/5 transition-all duration-300">
        <div className="max-w-5xl mx-auto px-6 h-20 flex justify-between items-center">
          <motion.div 
            initial={{ opacity: 0, x: -20 }}
            animate={{ opacity: 1, x: 0 }}
            transition={{ duration: 0.5 }}
          >
            <Link href="/" className="flex items-center gap-3">
              <img src="/logo-light.png" alt="KŌDO Logo" className="h-8 w-auto drop-shadow-md" />
              <span className="text-xl font-black tracking-widest text-foreground uppercase mt-1">KŌDO</span>
            </Link>
          </motion.div>
          <motion.div 
            initial={{ opacity: 0, x: 20 }}
            animate={{ opacity: 1, x: 0 }}
            transition={{ duration: 0.5 }}
            className="space-x-8 text-sm text-foreground/70 font-medium flex items-center"
          >
            <Link href="#services" className="hover:text-white transition-colors">Services</Link>
            <Link href="#contact" className="text-foreground hover:text-accent transition-colors">Demander une démo</Link>
          </motion.div>
        </div>
      </nav>

      <main className="flex-1 flex flex-col items-center justify-center text-center max-w-4xl mt-20 z-10">
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.5, delay: 0.1 }}
          className="flex items-center gap-2 text-xs font-bold text-accent tracking-widest uppercase glass px-5 py-2.5 rounded-full mb-8 shadow-sm"
        >
          <Sparkles size={14} className="text-accent" />
          KŌDO SOLUTIONS
        </motion.div>
        
        <motion.h2 
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.5, delay: 0.2 }}
          className="text-5xl md:text-7xl lg:text-8xl font-black tracking-tighter mb-8 text-foreground leading-[1.05] text-glow"
        >
          Le système d&apos;exploitation <br className="hidden md:block" /> de votre commerce.
        </motion.h2>
        
        <motion.p 
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.5, delay: 0.3 }}
          className="text-lg md:text-2xl text-foreground/60 mb-12 max-w-2xl font-medium leading-relaxed"
        >
          POS moderne, réservations intelligentes et présence digitale. 
          Propulsé par l&apos;IA pour les commerçants ambitieux.
        </motion.p>
        
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.5, delay: 0.4 }}
          className="flex flex-col sm:flex-row items-center gap-4"
        >
          <Link href="/onboarding" className="group flex items-center gap-3 bg-white hover:bg-gray-200 text-black px-10 py-5 rounded-full font-bold shadow-2xl shadow-white/10 hover:shadow-white/20 hover:-translate-y-1 transition-all text-lg">
            Créer mon espace SaaS
            <ArrowRight size={20} className="group-hover:translate-x-1 transition-transform" />
          </Link>
          <Link href="/kodo-pos" className="group flex items-center gap-3 bg-transparent border border-white/20 hover:bg-white/5 text-white px-10 py-5 rounded-full font-bold hover:-translate-y-1 transition-all text-lg">
            Obtenir Kōdo POS
          </Link>
        </motion.div>
      </main>
    </div>
  );
}
