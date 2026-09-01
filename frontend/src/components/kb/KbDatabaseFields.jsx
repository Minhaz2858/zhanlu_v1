import { useState } from 'react';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { Button } from '@/components/ui/button';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Wand2, CheckCircle2, XCircle, Play, Loader2 } from 'lucide-react';
import { authFetch } from '@/api/authFetch';

/**
 * Decode a single token extracted from a connection string.
 *
 * Connection strings pasted from SQLAlchemy URLs (or any other URL
 * source) carry URL-encoded values like ``%40`` for ``@`` and ``%2B``
 * for ``+``. The CLI itself does NOT decode them, but the value we
 * hand to the wizard (and the backend) needs the real characters —
 * otherwise we double-encode on the way out (see #kb-password-bug).
 *
 * Falls back to the raw token on malformed escape sequences so a
 * stray ``%ZZ`` doesn't blank the form field.
 */
function decodeToken(s) {
  if (s == null) return s;
  try {
    return decodeURIComponent(s);
  } catch {
    return s;
  }
}

/**
 * Parse a SQLAlchemy-style connection URL into the wizard's
 * database-field shape. Examples that this must handle:
 *
 *   mysql+pymysql://root:0Gg.B7c2%40tcX_jne6FMh@10.10.10.49:3306/aipdp_data_warehouse_prod?charset=utf8mb4
 *   postgresql+psycopg2://user:pw@host:5432/mydb
 *   mongodb+srv://user:pw@cluster.example.com/db
 *   oracle+cx_oracle://user:pw@host:1521/?service_name=orcl
 *
 * The dialect prefix (everything before ``+://``) is mapped to the
 * wizard's ``db_type``. URL-encoded values in userinfo / path are
 * decoded by the shared ``decodeToken`` helper. Returns ``null`` when
 * the input is not a SQLAlchemy URL we recognise — the caller then
 * falls through to the CLI parser.
 *
 * Exports for unit testing — see KbDatabaseFields.test.jsx.
 */
export function parseSqlalchemyUrl(raw) {
  const str = (raw || '').trim();
  if (!str) return null;

  // Match: scheme://[user[:password]@]host[:port]/path[?query]
  // scheme = ALPHA *( ALPHA / DIGIT / + / - / . )
  const m = str.match(
    /^([a-zA-Z][a-zA-Z0-9+.-]*):\/\/(?:([^:@/?#]+)(?::([^@]*))?@)?([^:/?#]+)(?::(\d+))?\/?([^?#]*)/
  );
  if (!m) return null;

  const [, scheme, user, password, host, port, rawPath] = m;
  const dialect = scheme.toLowerCase().split('+')[0];

  const dialectToDbType = {
    mysql: 'mysql',
    postgresql: 'postgresql',
    postgres: 'postgresql',
    mongodb: 'mongodb',
    mongo: 'mongodb',
    mssql: 'sqlserver',
    sqlserver: 'sqlserver',
  };
  const db_type = dialectToDbType[dialect];
  if (!db_type) return null;

  const result = { db_type };
  if (host) result.host = decodeToken(host);
  if (port) result.port = Number(port);
  if (user) result.username = decodeToken(user);
  if (password) result.password = decodeToken(password);
  const dbName = rawPath.split('?')[0].replace(/^\//, '');
  if (dbName) result.database_name = decodeToken(dbName);

  return Object.keys(result).length > 1 ? result : null;
}

/**
 * Parse a CLI-style connection string (mysql/psql/mongosh/sqlcmd)
 * OR a SQLAlchemy URL into the wizard's database-field shape.
 *
 * Exports for unit testing — see KbDatabaseFields.test.jsx.
 */
export function parseConnString(raw) {
  const str = (raw || '').trim();
  if (!str) return null;

  // SQLAlchemy URLs always carry a scheme prefix. Try that branch
  // first so URLs like ``mysql+pymysql://...`` don't fall through
  // and confuse the CLI flag parser with their ``+pymysql`` digests.
  if (/^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//.test(str)) {
    const sa = parseSqlalchemyUrl(str);
    if (sa) return sa;
  }

  const client = (str.match(/^(\S+)/)?.[1] || '').toLowerCase();
  let db_type = '';
  if (client === 'mysql') db_type = 'mysql';
  else if (['psql', 'postgres', 'pgsql'].includes(client)) db_type = 'postgresql';
  else if (['mongosh', 'mongo'].includes(client)) db_type = 'mongodb';
  else if (client === 'sqlcmd') db_type = 'sqlserver';
  const isPg = db_type === 'postgresql';

  const result = {};
  if (db_type) result.db_type = db_type;

  // --key=value style (--host=x, --password=x, --user=x, --port=x, --database=x)
  const eqRe = /--([a-z-]+)=("[^"]*"|'[^']*'|\S+)/gi;
  let m;
  while ((m = eqRe.exec(str))) {
    const key = m[1].toLowerCase();
    const val = decodeToken(m[2].replace(/^["']|["']$/g, ''));
    if (key === 'host') result.host = val;
    else if (key === 'port') result.port = val;
    else if (key === 'user' || key === 'username') result.username = val;
    else if (key === 'password') result.password = val;
    else if (key === 'database' || key === 'dbname' || key === 'db') result.database_name = val;
  }

  // -h VALUE (space-separated). Strip surrounding quotes if the
  // user wrapped the value (e.g. ``-h "my host"``).
  const hostM = str.match(/(?:^|\s)-h\s+(\S+)/);
  if (hostM && !result.host) {
    result.host = decodeToken(hostM[1].replace(/^["']|["']$/g, ''));
  }

  // -P VALUE (MySQL port, capital) or -p VALUE (PG port, lowercase)
  const portM = isPg
    ? str.match(/(?:^|\s)-p\s+(\d+)/)
    : str.match(/(?:^|\s)-P\s+(\d+)/);
  if (portM && !result.port) result.port = portM[1];

  // -u / -U VALUE
  const userM = str.match(/(?:^|\s)-[uU]\s+(\S+)/);
  if (userM && !result.username) result.username = decodeToken(userM[1]);

  // -pVALUE (MySQL attached password, e.g. -pSynexia@123)
  const pwM = str.match(/(?:^|\s)-p(?=\S)(\S+)/);
  if (pwM && !isPg && !result.password) result.password = decodeToken(pwM[1]);

  // -d VALUE (PG database name)
  const dbM = str.match(/(?:^|\s)-d\s+(\S+)/);
  if (dbM && !result.database_name) result.database_name = decodeToken(dbM[1]);

  // Positional database name (bare token at the end, e.g. db_zhanlu_no2)
  const cleaned = str
    .replace(/^\S+/, '')
    .replace(/--[a-z-]+=("[^"]*"|'[^']*'|\S+)/gi, '')
    .replace(/(?:^|\s)-[hPuUd]\s+\S+/g, '')
    .replace(/(?:^|\s)-p(?=\S)\S+/g, '')
    .replace(/(?:^|\s)-[a-zA-Z](?=\s|$)/g, ' ')
    .trim();
  const positional = cleaned.match(/(\S+)/);
  if (positional && !result.database_name) {
    result.database_name = decodeToken(positional[1].replace(/^["']|["']$/g, ''));
  }

  if (result.port) result.port = Number(result.port);
  if (!result.host && !result.db_type) return null;
  return result;
}

export default function KbDatabaseFields({ value, onChange, t }) {
  const DB_TYPES = Object.keys(t?.kb?.dbTypes || {});
  const set = (k, v) => onChange({ [k]: v });
  const isApi = value?.db_type === 'api';
  const [connStr, setConnStr] = useState('');
  const [parsed, setParsed] = useState(false);
  // Pre-save connectivity test: runs the wizard's form fields against the
  // backend's ``POST /api/apps/{app_id}/knowledge_bases/test_connection``
  // endpoint so the user can see green/red before clicking Save. Does
  // NOT block save — the user can still proceed if the test fails.
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState(null); // {ok: bool, info: string} | null

  function handleConnInput(raw) {
    setConnStr(raw);
    const result = parseConnString(raw);
    if (result && Object.keys(result).length > 0) {
      onChange(result);
      setParsed(true);
    } else if (!raw.trim()) {
      setParsed(false);
    }
  }

  const canTest = !isApi && !!value?.host && !!value?.username;

  async function handleTest() {
    if (!canTest) {
      setTestResult({ ok: false, info: t.kb.testMissingFields });
      return;
    }
    setTesting(true);
    setTestResult(null);
    try {
      const appId =
        (typeof window !== 'undefined' &&
          window.localStorage.getItem('base44_app_id')) ||
        'default-app';
      const res = await authFetch(
        `/api/apps/${appId}/knowledge_bases/test_connection`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            db_type: value.db_type,
            host: value.host,
            port: value.port || undefined,
            database_name: value.database_name,
            username: value.username,
            password: value.password,
          }),
        }
      );
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        setTestResult({ ok: false, info: data.detail || `HTTP ${res.status}` });
        return;
      }
      setTestResult({ ok: !!data.ok, info: data.info || '' });
    } catch (e) {
      setTestResult({ ok: false, info: e?.message || 'Network error' });
    } finally {
      setTesting(false);
    }
  }

  return (
    <div className="space-y-3">
      <div>
        <Label className="mb-1.5 block text-xs">{t.kb.dbType}</Label>
        <Select value={value?.db_type || ''} onValueChange={(v) => set('db_type', v)}>
          <SelectTrigger><SelectValue placeholder={t.kb.dbType} /></SelectTrigger>
          <SelectContent>
            {DB_TYPES.map((k) => <SelectItem key={k} value={k}>{t.kb.dbTypes[k]}</SelectItem>)}
          </SelectContent>
        </Select>
      </div>
      {isApi ? (
        <div>
          <Label className="mb-1.5 block text-xs">{t.kb.apiUrl}</Label>
          <Input value={value.api_url || ''} onChange={(e) => set('api_url', e.target.value)} placeholder={t.kb.apiUrlPh} />
        </div>
      ) : (
        <>
          <div>
            <Label className="mb-1.5 flex items-center gap-1.5 text-xs">
              <Wand2 className="h-3 w-3" /> {t.kb.connString}
            </Label>
            <Textarea
              value={connStr}
              onChange={(e) => handleConnInput(e.target.value)}
              placeholder={t.kb.connStringPh}
              rows={2}
              className="resize-none font-mono text-xs"
            />
            <p className="mt-1 flex items-center gap-1 text-[11px] text-muted-foreground">
              {parsed
                ? <><CheckCircle2 className="h-3 w-3 text-primary" /> {t.kb.connStringParsed}</>
                : t.kb.connStringHint}
            </p>
          </div>
          <div className="grid grid-cols-3 gap-3">
            <div className="col-span-2">
              <Label className="mb-1.5 block text-xs">{t.kb.host}</Label>
              <Input value={value.host || ''} onChange={(e) => set('host', e.target.value)} placeholder={t.kb.hostPh} />
            </div>
            <div>
              <Label className="mb-1.5 block text-xs">{t.kb.port}</Label>
              <Input type="number" value={value.port ?? ''} onChange={(e) => set('port', e.target.value ? Number(e.target.value) : '')} placeholder="5432" />
            </div>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <Label className="mb-1.5 block text-xs">{t.kb.database}</Label>
              <Input value={value.database_name || ''} onChange={(e) => set('database_name', e.target.value)} />
            </div>
            <div>
              <Label className="mb-1.5 block text-xs">{t.kb.username}</Label>
              <Input value={value.username || ''} onChange={(e) => set('username', e.target.value)} />
            </div>
          </div>
          <div>
            <Label className="mb-1.5 block text-xs">{t.kb.password}</Label>
            <Input type="password" value={value.password || ''} onChange={(e) => set('password', e.target.value)} />
          </div>
          {!isApi && (
            <div>
              <div className="flex items-center gap-2">
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={handleTest}
                  disabled={testing || !canTest}
                  className="h-7 gap-1.5 px-2.5 text-xs"
                >
                  {testing ? (
                    <Loader2 className="h-3 w-3 animate-spin" />
                  ) : (
                    <Play className="h-3 w-3" />
                  )}
                  {testing ? t.kb.testing : t.kb.testConn}
                </Button>
              </div>
              {testResult && (
                <p
                  className={`mt-1.5 flex items-start gap-1.5 text-[11px] leading-snug ${
                    testResult.ok
                      ? 'text-green-600 dark:text-green-400'
                      : 'text-destructive'
                  }`}
                >
                  {testResult.ok ? (
                    <CheckCircle2 className="mt-0.5 h-3 w-3 shrink-0" />
                  ) : (
                    <XCircle className="mt-0.5 h-3 w-3 shrink-0" />
                  )}
                  <span className="break-all">
                    {(testResult.ok ? t.kb.testOk : t.kb.testFail).replace(
                      '{info}',
                      testResult.info
                    )}
                  </span>
                </p>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}