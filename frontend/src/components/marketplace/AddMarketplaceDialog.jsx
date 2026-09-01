import { useState } from 'react';
import { X, Globe, Loader2, CheckCircle, AlertCircle } from 'lucide-react';
import { useLanguage } from '@/lib/LanguageProvider';
import { addSource, syncSource } from '@/api/marketplace';

export default function AddMarketplaceDialog({ open, onClose, onAdded }) {
  const { t } = useLanguage();
  const [url, setUrl] = useState('');
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [iconEmoji, setIconEmoji] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [addedSource, setAddedSource] = useState(null);

  if (!open) return null;

  const handleSubmit = async () => {
    if (!url.trim()) { setError('Please enter a URL'); return; }
    setLoading(true);
    setError('');
    try {
      const source = await addSource({
        url: url.trim(),
        name: name.trim() || undefined,
        description: description.trim() || undefined,
        icon_emoji: iconEmoji.trim() || undefined,
      });
      setAddedSource(source);
      // Poll sync until it finishes (the router kicks off a background
      // sync on POST, so we just wait for the source to be ready).
      for (let i = 0; i < 10; i++) {
        await new Promise((r) => setTimeout(r, 2000));
        try {
          const syncResult = await syncSource(source.id);
          if (syncResult.status === 'completed') break;
        } catch (e) { break; }
      }
      onAdded?.(source);
    } catch (e) {
      setError(e.message || 'Failed to add source');
    } finally {
      setLoading(false);
    }
  };

  const handleClose = () => {
    setUrl(''); setName(''); setDescription(''); setIconEmoji('');
    setError(''); setAddedSource(null);
    onClose();
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50" onClick={handleClose}>
      <div className="w-full max-w-md rounded-lg border border-border bg-card p-5" onClick={(e) => e.stopPropagation()}>
        <div className="mb-4 flex items-center justify-between">
          <h2 className="flex items-center gap-2 text-sm font-medium text-foreground">
            <Globe className="h-4 w-4 text-muted-foreground" />
            {t.marketplace?.addMarketplace || 'Add Marketplace'}
          </h2>
          <button onClick={handleClose} className="rounded p-1 text-muted-foreground hover:bg-secondary">
            <X className="h-4 w-4" />
          </button>
        </div>

        {addedSource ? (
          <div className="flex flex-col items-center gap-3 py-6">
            <CheckCircle className="h-7 w-7 text-emerald-500" />
            <p className="text-sm text-muted-foreground">Source "{addedSource.name}" added.</p>
            <button
              onClick={handleClose}
              className="rounded-md bg-primary px-3 py-1.5 text-xs text-primary-foreground hover:bg-primary/90"
            >
              {t.marketplace?.done || 'Done'}
            </button>
          </div>
        ) : (
          <div className="space-y-3">
            <div>
              <label className="text-xs text-muted-foreground">URL</label>
              <input
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                placeholder="https://github.com/owner/repo"
                className="mt-1 w-full rounded-md border border-border bg-background px-3 py-1.5 text-xs text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-primary/30"
              />
            </div>
            <div>
              <label className="text-xs text-muted-foreground">Name (optional)</label>
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="My Custom Source"
                className="mt-1 w-full rounded-md border border-border bg-background px-3 py-1.5 text-xs text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-primary/30"
              />
            </div>
            <div>
              <label className="text-xs text-muted-foreground">Description (optional)</label>
              <textarea
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                rows={2}
                className="mt-1 w-full rounded-md border border-border bg-background px-3 py-1.5 text-xs text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-primary/30"
              />
            </div>
            <div>
              <label className="text-xs text-muted-foreground">Icon (optional, 1 char)</label>
              <input
                value={iconEmoji}
                onChange={(e) => setIconEmoji(e.target.value)}
                placeholder="Defaults to first letter of name"
                maxLength={4}
                className="mt-1 w-full rounded-md border border-border bg-background px-3 py-1.5 text-xs text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-primary/30"
              />
            </div>

            {error && (
              <div className="flex items-center gap-2 rounded-md bg-red-500/10 px-3 py-2">
                <AlertCircle className="h-4 w-4 text-red-400" />
                <span className="text-xs text-red-300">{error}</span>
              </div>
            )}

            <div className="flex justify-end gap-2 pt-2">
              <button
                onClick={handleClose}
                className="rounded-md px-3 py-1.5 text-xs text-muted-foreground hover:bg-secondary"
              >
                {t.marketplace?.cancel || 'Cancel'}
              </button>
              <button
                onClick={handleSubmit}
                disabled={loading}
                className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
              >
                {loading && <Loader2 className="h-3 w-3 animate-spin" />}
                {loading ? 'Adding...' : (t.marketplace?.add || 'Add')}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
