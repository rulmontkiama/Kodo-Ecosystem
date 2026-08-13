import Hero from '@/components/Hero';
import Services from '@/components/Services';
import Pricing from '@/components/Pricing';
import ContactForm from '@/components/ContactForm';

export default function Home() {
  return (
    <div className="flex flex-col min-h-screen">
      <Hero />
      <Services />
      <Pricing />
      <ContactForm />
      
      <footer className="bg-foreground text-background py-8 text-center relative overflow-hidden">
        <p className="text-sm font-medium opacity-80">
          &copy; {new Date().getFullYear()} Kōdo Solutions. Tous droits réservés.
        </p>
        {/* Balise SEO cachée visuellement mais lue par Google pour le mot-clé sans accent */}
        <p className="sr-only">
          Kodo Solutions propose des logiciels de caisse et la création de site web en Belgique.
        </p>
      </footer>
    </div>
  );
}
