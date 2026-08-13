import type { Metadata } from "next";
import { Outfit } from "next/font/google";
import "./globals.css";
import AIAssistant from "@/components/AIAssistant";

const outfit = Outfit({
  variable: "--font-outfit",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Kōdo Solutions | Logiciel de Caisse (POS) & Création de Sites Web",
  description: "Propulsez votre commerce avec Kodo Solutions. Caisse enregistreuse tactile, réservation en ligne et création de sites web professionnels sur-mesure pour salons et restaurants en Belgique.",
  keywords: ["Kodo Solutions", "Logiciel POS", "Caisse enregistreuse", "Réservation en ligne", "Création site web", "Kōdo Solutions", "Belgique", "Waimes", "Malmedy", "Coiffeur", "Restaurant"],
  openGraph: {
    title: "Kōdo Solutions | Technologie pour Commerçants",
    description: "La solution tout-en-un pour gérer et développer votre commerce : POS, Bookings et Web.",
    url: "https://kodo-solutions.com",
    siteName: "Kodo Solutions",
    locale: "fr_BE",
    type: "website",
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="fr"
      className={`${outfit.variable} font-sans h-full antialiased`}
    >
      <body className="min-h-full flex flex-col bg-background text-foreground">
        {children}
        <AIAssistant />
      </body>
    </html>
  );
}
