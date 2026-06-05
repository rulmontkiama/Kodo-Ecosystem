import React from 'react';
import Link from 'next/link';

export default function Hero() {
  return (
    <div className="min-h-screen bg-background flex flex-col items-center justify-center p-8">
      <nav className="fixed top-0 left-0 right-0 w-full z-40 bg-background/80 backdrop-blur-xl border-b border-outline/50 transition-all duration-300">
        <div className="max-w-5xl mx-auto px-6 h-20 flex justify-between items-center">
          <h1 className="text-2xl font-black tracking-widest text-foreground uppercase">KŌDO</h1>
          <div className="space-x-8 text-sm text-surface-variant font-medium">
            <Link href="#services" className="hover:text-primary transition-colors">Services</Link>
            <Link href="#contact" className="text-foreground hover:text-primary transition-colors">Demander une démo</Link>
          </div>
        </div>
      </nav>

      <main className="flex-1 flex flex-col items-center justify-center text-center max-w-3xl mt-20">
        <span className="text-xs font-bold text-primary tracking-widest uppercase bg-primary/10 px-4 py-2 rounded-full inline-block mb-6">
          KŌDO SOLUTIONS
        </span>
        <h2 className="text-5xl md:text-7xl font-black tracking-tighter mb-8 text-foreground leading-[1.1]">
          Le système d&apos;exploitation <br className="hidden md:block" /> de votre commerce.
        </h2>
        <p className="text-lg md:text-xl text-foreground/60 mb-12 max-w-2xl font-medium leading-relaxed">
          POS moderne, réservations intelligentes et présence digitale. 
          Propulsé par l&apos;IA pour les commerçants ambitieux de toute la région.
        </p>
        <Link href="#contact" className="bg-primary hover:bg-primary-dark text-white px-10 py-5 rounded-full font-bold shadow-lg shadow-primary/20 hover:shadow-primary/40 hover:-translate-y-1 transition-all text-lg">
          Lancer mon projet
        </Link>
      </main>
    </div>
  );
}
