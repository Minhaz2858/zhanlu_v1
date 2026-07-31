import { useState } from 'react';
import {
  Loader2, Upload, Code, Globe,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Badge } from '@/components/ui/badge';
import { authFetch } from '@/api/authFetch';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from '@/components/ui/dialog';

const API = '/api/marketplace';

export default function PublishDialog({ open, onClose, onPublished }) {
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [skillMd, setSkillMd] = useState('');
  const [category, setCategory] = useState('');
  const [tags, setTags] = useState('');
  const [githubUrl, setGithubUrl] = useState('');
  const [publisherName, setPublisherName] = useState('');
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(e) {
    e.preventDefault();
    setError('');
    if (!name.trim() || !skillMd.trim()) {
      setError('Name and SKILL.md content are required');
      return;
    }
    setSubmitting(true);
    try {
      const payload = {
        name: name.trim(),
        description: description.trim(),
        skill_md: skillMd,
        category: category.trim() || null,
        publisher_name: publisherName.trim() || null,
        github_url: githubUrl.trim() || null,
        tags: tags
          .split(',')
          .map((t) => t.trim())
          .filter(Boolean),
      };
      const res = await authFetch(`${API}/publish`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        const data = await res.json();
        throw new Error(data.detail || 'Failed to publish');
      }
      resetForm();
      onPublished?.();
    } catch (err) {
      setError(err.message);
    } finally {
      setSubmitting(false);
    }
  }

  function resetForm() {
    setName('');
    setDescription('');
    setSkillMd('');
    setCategory('');
    setTags('');
    setGithubUrl('');
    setPublisherName('');
    setError('');
  }

  return (
    <Dialog open={open} onOpenChange={(v) => { if (!v) { onClose(); resetForm(); } }}>
      <DialogContent className="max-h-[90vh] max-w-lg overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Globe className="h-5 w-5" /> Publish to Marketplace
          </DialogTitle>
          <DialogDescription>
            Share your skill with the community. Paste your SKILL.md content below.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit} className="mt-4 space-y-4">
          <div>
            <label className="mb-1 block text-sm font-medium">Skill Name *</label>
            <Input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. image-gen, data-analyzer"
              disabled={submitting}
            />
          </div>

          <div>
            <label className="mb-1 block text-sm font-medium">
              Short Description
            </label>
            <Input
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="What does this skill do?"
              disabled={submitting}
            />
          </div>

          <div>
            <label className="mb-1 block text-sm font-medium">
              SKILL.md Content *
            </label>
            <Textarea
              value={skillMd}
              onChange={(e) => setSkillMd(e.target.value)}
              placeholder="---\nname: my-skill\ndescription: Does something useful\n---\n\n# My Skill\n\nDetailed instructions..."
              rows={10}
              disabled={submitting}
              className="font-mono text-xs"
            />
            <p className="mt-1 text-[11px] text-muted-foreground">
              Must start with YAML frontmatter (--- ... ---). Max 100KB.
            </p>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="mb-1 block text-sm font-medium">Category</label>
              <Input
                value={category}
                onChange={(e) => setCategory(e.target.value)}
                placeholder="e.g. media, data"
                disabled={submitting}
              />
            </div>
            <div>
              <label className="mb-1 block text-sm font-medium">Tags</label>
              <Input
                value={tags}
                onChange={(e) => setTags(e.target.value)}
                placeholder="comma, separated"
                disabled={submitting}
              />
            </div>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="mb-1 block text-sm font-medium">
                GitHub URL
              </label>
              <Input
                value={githubUrl}
                onChange={(e) => setGithubUrl(e.target.value)}
                placeholder="https://github.com/..."
                disabled={submitting}
              />
            </div>
            <div>
              <label className="mb-1 block text-sm font-medium">
                Publisher Name
              </label>
              <Input
                value={publisherName}
                onChange={(e) => setPublisherName(e.target.value)}
                placeholder="Your name or org"
                disabled={submitting}
              />
            </div>
          </div>

          {error && (
            <div className="rounded-md border border-destructive/50 bg-destructive/10 px-3 py-2 text-sm text-destructive">
              {error}
            </div>
          )}

          <div className="flex justify-end gap-3 pt-2">
            <Button
              type="button"
              variant="ghost"
              onClick={() => { onClose(); resetForm(); }}
              disabled={submitting}
            >
              Cancel
            </Button>
            <Button type="submit" disabled={submitting} className="gap-2">
              {submitting ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Upload className="h-4 w-4" />
              )}
              {submitting ? 'Publishing...' : 'Publish'}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
