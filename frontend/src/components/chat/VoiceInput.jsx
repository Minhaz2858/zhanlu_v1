import { useState, useRef } from 'react';
import { Mic } from 'lucide-react';
import { useLanguage } from '@/lib/LanguageProvider';

export default function VoiceInput({ onTranscript, disabled, lang }) {
  const { t } = useLanguage();
  const [listening, setListening] = useState(false);
  const recRef = useRef(null);

  function stop() {
    try { recRef.current?.stop(); } catch { /* noop */ }
    setListening(false);
  }

  function toggle() {
    if (listening) { stop(); return; }
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) {
      alert(t.chat.voice.unsupported);
      return;
    }
    const rec = new SR();
    rec.lang = lang === 'en' ? 'en-US' : 'zh-CN';
    rec.interimResults = false;
    rec.continuous = false;
    rec.onresult = (e) => {
      const text = e.results[0]?.[0]?.transcript || '';
      if (text) onTranscript?.(text);
    };
    rec.onerror = () => setListening(false);
    rec.onend = () => setListening(false);
    recRef.current = rec;
    try {
      rec.start();
      setListening(true);
    } catch {
      setListening(false);
    }
  }

  return (
    <button
      type="button"
      onClick={toggle}
      disabled={disabled}
      title={t.chat.voice.title}
      className={`inline-flex h-7 w-7 items-center justify-center rounded-full border transition-colors disabled:opacity-40 ${
        listening
          ? 'border-primary/50 bg-primary/10 text-primary'
          : 'border-border text-muted-foreground hover:border-primary/40 hover:text-foreground'
      }`}
    >
      <Mic className={`h-3.5 w-3.5 ${listening ? 'animate-pulse' : ''}`} />
    </button>
  );
}