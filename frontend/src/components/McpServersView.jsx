import { useCallback, useEffect, useState } from 'react';
import { toast } from 'react-hot-toast';
import { PlugZap, RefreshCw, Trash2, Plus, Loader2, ExternalLink, Server, ShieldCheck } from 'lucide-react';
import { useTranslate } from '@/lib/useTranslate';
import { useLanguage } from '@/lib/LanguageProvider';
import {
  listMcpServers, getMcpMarketplace, connectMcpServer,
  deleteMcpServer, probeMcpServer, oauthStart, oauthCallback,
} from '@/api/mcp';

export default function McpServersView() {
  const [servers, setServers] = useState([]);
  const [marketplace, setMarketplace] = useState([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(null);
  const [showAdd, setShowAdd] = useState(false);
  const [form, setForm] = useState({ name: '', transport: 'http', base_url: '', command: '', args: '' });
  const { lang } = useLanguage();
  const translate = useTranslate([...servers.map((s) => s.name), ...marketplace.map((m) => m.name)], lang);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [srv, cat] = await Promise.all([
        listMcpServers().catch(() => []),
        getMcpMarketplace().catch(() => ({ servers: [] })),
      ]);
      setServers(Array.isArray(srv) ? srv : []);
      setMarketplace(Array.isArray(cat.servers) ? cat.servers : []);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  // Handle OAuth redirect back to /my-space?tab=mcp&oauth=callback&server=...
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (params.get('oauth') !== 'callback') return;
    const serverId = params.get('server');
    const code = params.get('code');
    const state = params.get('state');
    if (!serverId || !code || !state) return;
    (async () => {
      try {
        await oauthCallback(serverId, { code, state });
        toast.success('MCP server authorized');
      } catch (e) {
        toast.error(e.message || 'OAuth callback failed');
      } finally {
        window.history.replaceState({}, '', window.location.pathname);
        load();
      }
    })();
  }, [load]);

  async function handleConnect(entry) {
    setBusy(entry.id);
    try {
      const payload = {
        name: entry.name,
        description: entry.description,
        transport: entry.transport,
        auth_type: entry.auth_type || 'none',
      };
      if (entry.transport === 'http') payload.base_url = entry.base_url;
      else { payload.command = entry.command || 'npx'; payload.args = entry.args || []; }
      await connectMcpServer(payload);
      toast.success(`Connected to ${entry.name}`);
      load();
      if (entry.auth_type === 'oauth') {
        // Kick off the OAuth flow: start -> open provider window.
        const { auth_url } = await oauthStart((await listMcpServers()).find((s) => s.name === entry.name)?.id, {
          client_id: entry.oauth_client_id || '',
        }).catch(() => ({}));
        if (auth_url) window.open(auth_url, '_blank', 'noopener');
      }
    } catch (e) {
      toast.error(e.message || 'Connect failed');
    } finally {
      setBusy(null);
    }
  }

  async function handleRemove(server) {
    if (!window.confirm(`Disconnect MCP server "${server.name}"?`)) return;
    try {
      await deleteMcpServer(server.id);
      load();
    } catch (e) {
      toast.error(e.message || 'Disconnect failed');
    }
  }

  async function handleProbe(server) {
    setBusy(server.id);
    try {
      await probeMcpServer(server.id);
      load();
    } catch (e) {
      toast.error(e.message || 'Probe failed');
    } finally {
      setBusy(null);
    }
  }

  async function handleManualAdd() {
    if (!form.name || (form.transport === 'http' && !form.base_url) || (form.transport !== 'http' && !form.command)) {
      toast.error('Name and endpoint are required');
      return;
    }
    setBusy('manual');
    try {
      const payload = { name: form.name, transport: form.transport, auth_type: 'none' };
      if (form.transport === 'http') payload.base_url = form.base_url;
      else payload.args = form.args ? form.args.split(/\s+/) : [];
      await connectMcpServer(payload);
      toast.success('MCP server connected');
      setShowAdd(false);
      setForm({ name: '', transport: 'http', base_url: '', command: '', args: '' });
      load();
    } catch (e) {
      toast.error(e.message || 'Connect failed');
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="space-y-8">
      {/* Connected servers */}
      <section>
        <div className="mb-3 flex items-center justify-between">
          <h2 className="flex items-center gap-2 font-display text-sm font-semibold">
            <Server className="h-4 w-4 text-primary" />
            {lang === 'en' ? 'Connected Servers' : '已连接服务器'}
          </h2>
          <button
            onClick={() => setShowAdd((v) => !v)}
            className="inline-flex items-center gap-1 rounded-md border border-border px-2.5 py-1 text-xs transition-colors hover:bg-secondary"
          >
            <Plus className="h-3 w-3" /> {lang === 'en' ? 'Add Server' : '添加服务器'}
          </button>
        </div>

        {showAdd && (
          <div className="mb-4 rounded-xl border border-border bg-card p-4">
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
              <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })}
                placeholder="Name" className="rounded-md border border-border bg-background px-3 py-1.5 text-sm" />
              <select value={form.transport} onChange={(e) => setForm({ ...form, transport: e.target.value })}
                className="rounded-md border border-border bg-background px-3 py-1.5 text-sm">
                <option value="http">HTTP (streamable)</option>
                <option value="stdio">stdio (command)</option>
              </select>
              {form.transport === 'http' ? (
                <input value={form.base_url} onChange={(e) => setForm({ ...form, base_url: e.target.value })}
                  placeholder="https://example.com/mcp" className="rounded-md border border-border bg-background px-3 py-1.5 text-sm md:col-span-2" />
              ) : (
                <>
                  <input value={form.command} onChange={(e) => setForm({ ...form, command: e.target.value })}
                    placeholder="npx" className="rounded-md border border-border bg-background px-3 py-1.5 text-sm" />
                  <input value={form.args} onChange={(e) => setForm({ ...form, args: e.target.value })}
                    placeholder="-y @modelcontextprotocol/server-github" className="rounded-md border border-border bg-background px-3 py-1.5 text-sm" />
                </>
              )}
            </div>
            <button onClick={handleManualAdd} disabled={busy === 'manual'}
              className="mt-3 inline-flex items-center gap-1 rounded-md bg-primary px-3 py-1.5 text-xs text-primary-foreground transition-opacity disabled:opacity-50">
              {busy === 'manual' ? <Loader2 className="h-3 w-3 animate-spin" /> : <Plus className="h-3 w-3" />}
              {lang === 'en' ? 'Connect' : '连接'}
            </button>
          </div>
        )}

        {loading ? (
          <div className="flex justify-center py-10"><Loader2 className="h-5 w-5 animate-spin text-muted-foreground" /></div>
        ) : servers.length === 0 ? (
          <p className="rounded-xl border border-dashed border-border py-8 text-center text-sm text-muted-foreground">
            {lang === 'en' ? 'No MCP servers connected yet. Pick one from the marketplace below.' : '尚未连接 MCP 服务器，请从下方市场选择。'}
          </p>
        ) : (
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
            {servers.map((server) => (
              <div key={server.id} className="rounded-xl border border-border bg-card p-4">
                <div className="mb-1.5 flex items-center gap-2">
                  <PlugZap className="h-4 w-4 shrink-0 text-primary" />
                  <h3 className="min-w-0 flex-1 truncate font-display text-sm">{translate(server.name)}</h3>
                  <span className={`inline-flex shrink-0 items-center rounded-full px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide ${
                    server.status === 'connected' ? 'bg-[hsl(var(--chart-2))]/10 text-[hsl(var(--chart-2))]' : 'bg-muted text-muted-foreground'}`}>
                    {server.status}
                  </span>
                </div>
                <div className="mb-2 flex flex-wrap items-center gap-1.5 text-[10px] text-muted-foreground">
                  <span className="rounded-full bg-muted px-2 py-0.5">{server.transport}</span>
                  <span className="inline-flex items-center gap-1 rounded-full bg-muted px-2 py-0.5">
                    <ShieldCheck className="h-2.5 w-2.5" /> {server.auth_type || 'none'}
                  </span>
                  <span className="rounded-full bg-muted px-2 py-0.5">{server.tools_count || 0} tools</span>
                </div>
                <div className="flex items-center gap-2">
                  <button onClick={() => handleProbe(server)} disabled={busy === server.id}
                    className="inline-flex items-center gap-1 rounded-md border border-border px-2.5 py-1 text-xs transition-colors hover:bg-secondary disabled:opacity-50">
                    {busy === server.id ? <Loader2 className="h-3 w-3 animate-spin" /> : <RefreshCw className="h-3 w-3" />}
                    {lang === 'en' ? 'Probe' : '探测'}
                  </button>
                  <button onClick={() => handleRemove(server)}
                    className="ml-auto inline-flex items-center gap-1 rounded-md border border-border px-2.5 py-1 text-xs text-muted-foreground transition-colors hover:text-destructive">
                    <Trash2 className="h-3 w-3" /> {lang === 'en' ? 'Disconnect' : '断开'}
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      {/* Marketplace */}
      <section>
        <h2 className="mb-3 flex items-center gap-2 font-display text-sm font-semibold">
          <ExternalLink className="h-4 w-4 text-primary" />
          {lang === 'en' ? 'Marketplace' : '服务器市场'}
        </h2>
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2 lg:grid-cols-3">
          {marketplace.map((entry) => (
            <div key={entry.id} className="flex flex-col rounded-xl border border-border bg-card p-4">
              <div className="mb-1 flex items-center gap-2">
                <PlugZap className="h-4 w-4 shrink-0 text-primary" />
                <h3 className="min-w-0 flex-1 truncate font-display text-sm">{entry.name}</h3>
              </div>
              <p className="mb-2 line-clamp-2 min-h-[2rem] flex-1 text-xs text-muted-foreground">{entry.description}</p>
              <div className="mb-3 flex flex-wrap gap-1.5 text-[10px] text-muted-foreground">
                <span className="rounded-full bg-muted px-2 py-0.5">{entry.transport}</span>
                <span className="rounded-full bg-muted px-2 py-0.5">{entry.auth_type}</span>
                <span className="rounded-full bg-muted px-2 py-0.5">{entry.category}</span>
              </div>
              <button onClick={() => handleConnect(entry)} disabled={busy === entry.id}
                className="inline-flex items-center justify-center gap-1 rounded-md bg-primary px-3 py-1.5 text-xs text-primary-foreground transition-opacity disabled:opacity-50">
                {busy === entry.id ? <Loader2 className="h-3 w-3 animate-spin" /> : <Plus className="h-3 w-3" />}
                {lang === 'en' ? 'Connect' : '连接'}
              </button>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}
