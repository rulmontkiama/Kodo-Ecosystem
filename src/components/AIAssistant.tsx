'use client';
import { useState, useRef, useEffect } from 'react';
import { MessageCircle, X, Send, Bot } from 'lucide-react';
import { motion, AnimatePresence } from 'framer-motion';
import { GoogleGenAI } from '@google/genai';

const API_KEY = process.env.NEXT_PUBLIC_GEMINI_API_KEY;

export default function AIAssistant() {
  const [isOpen, setIsOpen] = useState(false);
  const [messages, setMessages] = useState<{role: 'user'|'model', text: string}[]>([
    { role: 'model', text: `Bonjour ! Je suis l'expert commercial de Kōdo Solutions. Avez-vous des questions sur nos produits Kōdo POS, Bookings ou Web ?` }
  ]);
  const [input, setInput] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  useEffect(() => {
    scrollToBottom();
  }, [messages]);

  const handleSend = async () => {
    if (!input.trim() || !API_KEY) return;

    const userMessage = input;
    setMessages(prev => [...prev, { role: 'user', text: userMessage }]);
    setInput('');
    setIsLoading(true);

    try {
      const ai = new GoogleGenAI({ apiKey: API_KEY });
      const prompt = `Tu es l'assistant commercial virtuel de Kōdo Solutions.
Ton but est de renseigner les prospects sur nos trois produits principaux et de les encourager à remplir le formulaire de contact sur le site.
Nos produits :
1. Kōdo POS : Logiciel de caisse (hors-ligne, certifié NF525, synchronisé sur le cloud, analytics poussés).
2. Kōdo Bookings : Application Web de prise de rendez-vous pour les salons et instituts (PWA, intégration Firebase).
3. Kōdo Web : Création de sites vitrines et E-commerce ultra-performants.
Ton ton doit être professionnel, moderne, rassurant et persuasif. Garde tes réponses courtes.

Message du visiteur: ${userMessage}`;

      const response = await ai.models.generateContent({
        model: 'gemini-2.5-flash',
        contents: prompt,
      });

      setMessages(prev => [...prev, { role: 'model', text: response.text || 'Désolé, je ne peux pas répondre pour le moment.' }]);
    } catch (error) {
      console.error(error);
      setMessages(prev => [...prev, { role: 'model', text: "Erreur de connexion à l'assistant." }]);
    } finally {
      setIsLoading(false);
    }
  };

  if (!API_KEY) return null;

  return (
    <>
      <button
        onClick={() => setIsOpen(true)}
        className="fixed bottom-8 right-8 md:bottom-10 md:right-10 z-50 px-5 py-4 bg-accent text-white rounded-full shadow-[0_0_30px_rgba(255,127,127,0.4)] hover:scale-105 transition-all flex items-center justify-center gap-3 group border border-white/10"
      >
        <MessageCircle size={24} />
        <span className="font-bold tracking-wider text-sm hidden sm:block">Besoin d'aide ?</span>
      </button>

      <AnimatePresence>
        {isOpen && (
          <motion.div
            initial={{ opacity: 0, y: 20, scale: 0.95 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 20, scale: 0.95 }}
            className="fixed bottom-28 right-8 md:bottom-32 md:right-10 w-[350px] sm:w-[400px] h-[500px] glass bg-surface rounded-2xl shadow-2xl flex flex-col z-50 overflow-hidden border border-white/10"
          >
            {/* Header */}
            <div className="p-4 border-b border-white/10 bg-black/40 flex justify-between items-center">
              <div className="flex items-center gap-3">
                <div className="w-8 h-8 rounded-full bg-accent/20 flex items-center justify-center">
                  <Bot size={18} className="text-accent" />
                </div>
                <div>
                  <h3 className="font-bold text-sm text-white">Assistant Kōdo</h3>
                  <p className="text-[10px] text-accent font-bold uppercase tracking-widest">En ligne</p>
                </div>
              </div>
              <button onClick={() => setIsOpen(false)} className="text-white/50 hover:text-white transition-colors">
                <X size={20} />
              </button>
            </div>

            {/* Messages */}
            <div className="flex-1 overflow-y-auto p-4 space-y-4">
              {messages.map((msg, idx) => (
                <div key={idx} className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
                  <div className={`max-w-[80%] p-3 rounded-2xl text-sm ${msg.role === 'user' ? 'bg-accent text-white rounded-tr-sm' : 'bg-white/10 text-white rounded-tl-sm'}`}>
                    {msg.text}
                  </div>
                </div>
              ))}
              {isLoading && (
                <div className="flex justify-start">
                  <div className="bg-white/10 p-3 rounded-2xl rounded-tl-sm flex gap-1">
                    <span className="w-2 h-2 bg-white/50 rounded-full animate-bounce" style={{ animationDelay: '0ms' }} />
                    <span className="w-2 h-2 bg-white/50 rounded-full animate-bounce" style={{ animationDelay: '150ms' }} />
                    <span className="w-2 h-2 bg-white/50 rounded-full animate-bounce" style={{ animationDelay: '300ms' }} />
                  </div>
                </div>
              )}
              <div ref={messagesEndRef} />
            </div>

            {/* Input */}
            <div className="p-4 border-t border-white/10 bg-black/40">
              <div className="flex gap-2">
                <input
                  type="text"
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && handleSend()}
                  placeholder="Posez votre question..."
                  className="flex-1 bg-white/5 border border-white/10 rounded-xl px-4 py-2 text-sm text-white focus:outline-none focus:border-accent"
                />
                <button
                  onClick={handleSend}
                  disabled={isLoading || !input.trim()}
                  className="p-2 bg-accent text-white rounded-xl hover:opacity-90 disabled:opacity-50 transition-opacity flex items-center justify-center"
                >
                  <Send size={18} />
                </button>
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </>
  );
}
