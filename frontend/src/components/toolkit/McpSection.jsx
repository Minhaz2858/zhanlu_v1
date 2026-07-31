import { useState, useEffect } from 'react';
import { Plug, Plus, Loader2, Trash2, Terminal, Link2 } from 'lucide-react';
import { base44 } from '@/api/base44Client';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { authFetch } from '@/api/authFetch';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter, DialogTrigger } from '@/components/ui/dialog';
import { inputCls } from '@/components/agent/AgentParts';

const STATUS_STYLES = {
  connected: 'bg-green-100 text-green-700',
  disconnected: 'bg-gray-100 text-gray-600',
  error: 'bg-red-100 text-red-700',
};
const DOT_STYLES = {
  connected: 'bg-green-500',
  disconnected: 'bg-gray-400',
  error: 'bg-red-500',
};

export default function McpSection({ t }) {
  const [servers, setServers] = useState([]);
  const [loading, setLoading] = useState(true);
  const [showAdd, setShowAdd] = useState(false);
  const [showPaste, setShowPaste] = useState(false);
  const [configText, setConfigText] = useState('');
  const [pasteError, setPasteError] = useState('');
  const [pasteLoading, setPasteLoading] = useState(false);
  const [form, setForm] = useState({ name: '', server_url: '', transport: 'sse', description: '' });

  useEffect(() => { load(); }, []);
  async function load() {
    try {
      const result = await authFetch('/api/mcp/servers');
      if (result.ok) {
        setServers(await result.json());
      } else {
        setServers(await base44.entities.McpServer.list('-updated_date', 200));
      }
    } catch { setServers([]); }
    finally { setLoading(false); }
  }

  async function handleAdd() {
    if (!form.name || !form.server_url) return;
    await base44.entities.McpServer.create({
      ...form,
      status: 'disconnected',
      tools_count: 0,
      resources_count: 0,
    });
    setForm({ name: '', server_url: '', transport: 'sse', description: '' });
    setShowAdd(false);
    load();
  }

  async function handlePasteConfig() {
    setPasteError('');
    setPasteLoading(true);
    try {
      // Parse the pasted config — supports Claude-style MCP config format
      let parsed;
      try {
        parsed = JSON.parse(configText);
        // If top-level mcpServers key, extract first server
        if (parsed.mcpServers && typeof parsed.mcpServers === 'object') {
          const entry = Object.entries(parsed.mcpServers)[0];
          if (entry) {
            const [name, cfg] = entry;
            parsed = { name, ...cfg };
          }
        }
      } catch {
        setPasteError('Invalid JSON config. Paste a valid Claude-style MCP config.');
        setPasteLoading(false);
        return;
      }

      const res = await authFetch('/api/mcp/servers/connect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: parsed.name || 'mcp-server',
          command: parsed.command || parsed.server_url || '',
          args: parsed.args || [],
          env: parsed.env || {},
          description: parsed.description || '',
          transport: parsed.transport || 'stdio',
        }),
      });

      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || 'Failed to connect');
      }

      setConfigText('');
      setShowPaste(false);
      load();
    } catch (err) {
      setPasteError(err.message);
    } finally {
      setPasteLoading(false);
    }
  }

  async function handleDelete(id) {
    try {
      await authFetch(`/api/mcp/servers/${id}`, { method: 'DELETE' });
    } catch {
      try { await base44.entities.McpServer.delete(id); } catch { /* noop */ }
    }
    load();
  }

  return (
    <div>
      <div className="mb-1 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Plug className="h-4 w-4 text-primary" />
          <h2 className="font-display text-lg text-foreground">{t.toolkit.mcpServers}</h2>
          <span className="text-xs text-muted-foreground">({servers.length})</span>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={() => setShowPaste(true)} className="gap-2">
            <Terminal className="h-3.5 w-3.5" /> Paste Config
          </Button>
          <Dialog open={showAdd} onOpenChange={setShowAdd}>
            <DialogTrigger asChild>
              <Button variant="outline" size="sm" className="gap-2">
                <Link2 className="h-3.5 w-3.5" /> {t.toolkit.addMcpServer}
              </Button>
            </DialogTrigger>
            <DialogContent>
              <DialogHeader>
                <DialogTitle>{t.toolkit.addMcpServer}</DialogTitle>
              </DialogHeader>
              <div className="space-y-3 py-2">
                <div>
                  <label className="mb-1 block text-xs text-muted-foreground">{t.toolkit.serverName}</label>
                  <input className={inputCls} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder={t.toolkit.serverNamePh} />
                </div>
                <div>
                  <label className="mb-1 block text-xs text-muted-foreground">{t.toolkit.serverUrl}</label>
                  <input className={inputCls} value={form.server_url} onChange={(e) => setForm({ ...form, server_url: e.target.value })} placeholder="https://…" />
                </div>
                <div>
                  <label className="mb-1 block text-xs text-muted-foreground">{t.toolkit.transport}</label>
                  <select className={inputCls} value={form.transport} onChange={(e) => setForm({ ...form, transport: e.target.value })}>
                    <option value="sse">{t.toolkit.transports.sse}</option>
                    <option value="streamable">{t.toolkit.transports.streamable}</option>
                    <option value="stdio">{t.toolkit.transports.stdio}</option>
                  </select>
                </div>
                <div>
                  <label className="mb-1 block text-xs text-muted-foreground">{t.toolkit.description}</label>
                  <input className={inputCls} value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} placeholder={t.toolkit.descPh} />
                </div>
              </div>
              <DialogFooter>
                <Button variant="outline" onClick={() => setShowAdd(false)}>{t.toolkit.cancel}</Button>
                <Button onClick={handleAdd} disabled={!form.name || !form.server_url}>{t.toolkit.confirm}</Button>
              </DialogFooter>
            </DialogContent>
          </Dialog>
        </div>

        {/* Paste Config Dialog */}
        <Dialog open={showPaste} onOpenChange={setShowPaste}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Connect MCP Server</DialogTitle>
            </DialogHeader>
            <div className="space-y-3 py-2">
              <p className="text-xs text-muted-foreground">
                Paste a Claude-style MCP server config to auto-import tools.
              </p>
              <Textarea
                value={configText}
                onChange={(e) => setConfigText(e.target.value)}
                placeholder={`{\n  "mcpServers": {\n    "github": {\n      "command": "npx",\n      "args": ["-y", "@modelcontextprotocol/server-github"],\n      "env": {\n        "GITHUB_TOKEN": "..."\n      }\n    }\n  }\n}`}
                rows={10}
                className="font-mono text-xs"
              />
              {pasteError && (
                <p className="text-xs text-destructive">{pasteError}</p>
              )}
            </div>
            <DialogFooter>
              <Button variant="outline" onClick={() => { setShowPaste(false); setConfigText(''); setPasteError(''); }}>
                Cancel
              </Button>
              <Button onClick={handlePasteConfig} disabled={!configText.trim() || pasteLoading}>
                {pasteLoading ? <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" /> : null}
                Connect
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </div>
      <p className="mb-4 text-xs text-muted-foreground">{t.toolkit.mcpDesc}</p>
      {loading ? (
        <div className="flex justify-center py-8"><Loader2 className="h-5 w-5 animate-spin text-muted-foreground" /></div>
      ) : servers.length === 0 ? (
        <div className="rounded-xl border border-dashed border-border py-12 text-center text-sm text-muted-foreground">{t.toolkit.mcpEmpty}</div>
      ) : (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
          {servers.map((s) => (
            <div key={s.id} className="group rounded-xl border border-border bg-card p-5 transition-shadow hover:shadow-sm">
              <div className="mb-2 flex items-start justify-between">
                <h3 className="font-display text-base text-foreground">{s.name}</h3>
                <span className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs ${STATUS_STYLES[s.status] || STATUS_STYLES.disconnected}`}>
                  <span className={`h-1.5 w-1.5 rounded-full ${DOT_STYLES[s.status] || DOT_STYLES.disconnected}`} />
                  {t.toolkit.mcpStatuses[s.status] || s.status}
                </span>
              </div>
              <p className="mb-3 text-xs text-muted-foreground">{s.description || '—'}</p>
              <p className="mb-3 truncate font-mono text-xs text-muted-foreground">{s.server_url}</p>
              <div className="flex items-center justify-between text-xs text-muted-foreground">
                <span>{t.toolkit.transport}: {t.toolkit.transports[s.transport] || s.transport}</span>
                <span>{t.toolkit.toolsCount}: {s.tools_count || 0}</span>
              </div>
              <button onClick={() => handleDelete(s.id)} className="mt-3 text-xs text-muted-foreground transition-colors hover:text-destructive">
                <Trash2 className="inline h-3 w-3" /> {t.common.delete}
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}