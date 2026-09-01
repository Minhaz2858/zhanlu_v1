import { createClientFromRequest } from 'npm:@base44/sdk@0.8.31';

Deno.serve(async (req) => {
  try {
    const base44 = createClientFromRequest(req);
    const user = await base44.auth.me();
    if (!user) return Response.json({ error: 'Unauthorized' }, { status: 401 });

    const body = await req.json();
    const url = body.url;
    const kind = body.kind || 'system_skill';
    if (!url) return Response.json({ error: 'URL is required' }, { status: 400 });

    const parsed = parseGithubUrl(url);
    if (!parsed) return Response.json({ error: 'Could not parse GitHub URL' }, { status: 400 });

    // Fetch the full repo tree in one API call
    const treeUrl = `https://api.github.com/repos/${parsed.owner}/${parsed.repo}/git/trees/${parsed.branch}?recursive=1`;
    const treeRes = await fetch(treeUrl, {
      headers: { 'User-Agent': 'minha-skills-collector', 'Accept': 'application/vnd.github+json' }
    });
    if (!treeRes.ok) {
      return Response.json({ error: `GitHub API error: ${treeRes.status} ${treeRes.statusText}` }, { status: 502 });
    }
    const treeData = await treeRes.json();
    const tree = treeData.tree || [];

    // Find SKILL.md blobs within the base path
    const basePath = parsed.path ? parsed.path.replace(/\/$/, '') : '';
    let skillMdBlobs = tree.filter((t) => t.type === 'blob' && t.path.endsWith('/SKILL.md'));
    if (basePath) {
      skillMdBlobs = skillMdBlobs.filter((t) => t.path.startsWith(basePath + '/'));
    }
    // Also include a SKILL.md sitting directly at the base path (single skill)
    const direct = tree.find((t) => t.type === 'blob' && t.path === basePath + '/SKILL.md');
    if (direct && !skillMdBlobs.includes(direct)) skillMdBlobs.push(direct);

    let skillPaths = skillMdBlobs.map((f) => f.path.replace(/\/SKILL\.md$/, ''));

    if (parsed.skillFilter) {
      skillPaths = skillPaths.filter((p) => p === parsed.skillFilter || p.endsWith('/' + parsed.skillFilter));
    }

    if (skillPaths.length === 0) {
      return Response.json({ error: 'No skills (folders with SKILL.md) found at this URL' }, { status: 404 });
    }

    // Collect each skill (cap at 30 to stay within reasonable limits)
    const collected = [];
    const errors = [];
    for (const skillPath of skillPaths.slice(0, 30)) {
      try {
        const skill = await collectSkill(parsed.owner, parsed.repo, parsed.branch, skillPath, tree);
        if (!skill) continue;
        const tool = await base44.entities.Tool.create({
          name: skill.name,
          description: skill.description,
          kind,
          source: 'github',
          github_url: url,
          version: skill.version || '1.0.0',
          license: skill.license || 'MIT',
          platform: 'minimax',
          category: skill.category || 'general',
          sources: skill.sources || [],
          skill_md: skill.skillMd,
          references: skill.references || [],
          status: 'active'
        });
        collected.push({ id: tool.id, name: skill.name });
      } catch (e) {
        errors.push({ path: skillPath, error: e.message });
      }
    }

    return Response.json({ collected: collected.length, skills: collected, errors });
  } catch (error) {
    return Response.json({ error: error.message }, { status: 500 });
  }
});

function parseGithubUrl(input) {
  let raw = (input || '').trim();
  let skillFilter = null;

  // Extract --skill flag
  const flagMatch = raw.match(/--skill\s+(\S+)/);
  if (flagMatch) {
    skillFilter = flagMatch[1];
    raw = raw.replace(/--skill\s+\S+/, '').trim();
  }

  // Strip "npx skills add" prefix
  raw = raw.replace(/npx\s+skills\s+add\s+/i, '').trim();

  // Extract the https URL
  const urlMatch = raw.match(/(https:\/\/github\.com\/[^\s]+)/);
  if (!urlMatch) return null;
  const url = urlMatch[1].replace(/[\/)]+$/, '');

  // github.com/{owner}/{repo}/tree/{branch}/{path}
  const treeMatch = url.match(/github\.com\/([^\/]+)\/([^\/]+)\/tree\/([^\/]+)\/?(.*)/);
  if (treeMatch) {
    return { owner: treeMatch[1], repo: treeMatch[2], branch: treeMatch[3], path: treeMatch[4] || '', skillFilter };
  }

  // github.com/{owner}/{repo}
  const rootMatch = url.match(/github\.com\/([^\/]+)\/([^\/?]+)/);
  if (rootMatch) {
    return { owner: rootMatch[1], repo: rootMatch[2], branch: 'main', path: '', skillFilter };
  }

  return null;
}

async function collectSkill(owner, repo, branch, skillPath, tree) {
  const skillMdBlob = tree.find((t) => t.type === 'blob' && t.path === skillPath + '/SKILL.md');
  if (!skillMdBlob) return null;

  let name = skillPath.split('/').pop();
  let description = '';
  let version = '1.0.0';
  let license = 'MIT';
  let category = 'general';
  let sources = [];
  let skillMd = '';
  let references = [];

  // Fetch SKILL.md
  const skillMdContent = await fetchRaw(owner, repo, branch, skillPath + '/SKILL.md');
  const parsed = parseSkillMd(skillMdContent);
  name = parsed.name || name;
  description = parsed.description || description;
  version = parsed.version || version;
  license = parsed.license || license;
  category = parsed.category || category;
  sources = parsed.sources || sources;
  skillMd = parsed.body;

  // Fetch _meta.json if present
  const metaBlob = tree.find((t) => t.type === 'blob' && t.path === skillPath + '/_meta.json');
  if (metaBlob) {
    const metaContent = await fetchRaw(owner, repo, branch, skillPath + '/_meta.json');
    try {
      const meta = JSON.parse(metaContent);
      if (meta.version) version = String(meta.version);
      if (meta.name) name = meta.name;
    } catch (_e) { /* ignore malformed meta */ }
  }

  // Fetch _references/ files
  const refsDir = skillPath + '/_references/';
  const refBlobs = tree.filter((t) => t.type === 'blob' && t.path.startsWith(refsDir));
  for (const ref of refBlobs) {
    const refName = ref.path.split('/').pop();
    const content = await fetchRaw(owner, repo, branch, ref.path);
    references.push({ name: refName, content });
  }

  return { name, description, version, license, category, sources, skillMd, references };
}

async function fetchRaw(owner, repo, branch, path) {
  const url = `https://raw.githubusercontent.com/${owner}/${repo}/${branch}/${path}`;
  try {
    const res = await fetch(url);
    if (!res.ok) return '';
    return await res.text();
  } catch (_e) {
    return '';
  }
}

function parseSkillMd(content) {
  const result = { name: '', description: '', version: '', license: '', category: '', sources: [], body: '' };
  if (!content) return result;

  const fmMatch = content.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n?([\s\S]*)$/);
  if (fmMatch) {
    const frontmatter = fmMatch[1];
    result.body = fmMatch[2];

    const nameMatch = frontmatter.match(/^name:\s*(.+)$/m);
    if (nameMatch) result.name = nameMatch[1].trim().replace(/^["']|["']$/g, '');

    const descMatch = frontmatter.match(/^description:\s*["']?([\s\S]*?)["']?\s*$/m);
    if (descMatch) result.description = descMatch[1].trim();

    const licMatch = frontmatter.match(/^license:\s*(.+)$/m);
    if (licMatch) result.license = licMatch[1].trim().replace(/^["']|["']$/g, '');

    const verMatch = frontmatter.match(/version:\s*["']?(.+?)["']?\s*$/m);
    if (verMatch) result.version = verMatch[1].trim();

    const catMatch = frontmatter.match(/category:\s*(.+)$/m);
    if (catMatch) result.category = catMatch[1].trim().replace(/^["']|["']$/g, '');

    const srcSection = frontmatter.match(/sources:\s*\n((?:\s*-\s+.+\n?)+)/);
    if (srcSection) {
      result.sources = srcSection[1]
        .split('\n')
        .map((l) => l.replace(/^\s*-\s*/, '').trim())
        .filter(Boolean)
        .map((s) => s.replace(/^["']|["']$/g, ''));
    }
  } else {
    result.body = content;
  }

  return result;
}