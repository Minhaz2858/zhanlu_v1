import { createClientFromRequest } from 'npm:@base44/sdk@0.8.31';
import { Document, Packer, Paragraph, TextRun, HeadingLevel, Table, TableRow, TableCell, WidthType } from 'npm:docx@8.5.0';

function parseInlineRuns(text) {
  // Split on **bold** markers
  const parts = [];
  const regex = /\*\*(.+?)\*\*/g;
  let last = 0;
  let m;
  while ((m = regex.exec(text)) !== null) {
    if (m.index > last) parts.push({ text: text.slice(last, m.index), bold: false });
    parts.push({ text: m[1], bold: true });
    last = regex.lastIndex;
  }
  if (last < text.length) parts.push({ text: text.slice(last), bold: false });
  if (parts.length === 0) parts.push({ text, bold: false });
  return parts.map((p) => new TextRun({ text: p.text, bold: p.bold }));
}

function mdToChildren(md) {
  const lines = (md || '').split('\n');
  const children = [];
  let tableRows = [];
  let inTable = false;

  function flushTable() {
    if (tableRows.length < 2) {
      // Not enough rows for a table — render as text
      tableRows.forEach((r) => children.push(new Paragraph({ children: parseInlineRuns(r) })));
    } else {
      const rows = tableRows.map((row, ri) => {
        const cells = row.split('|').map((c) => c.trim()).filter((c, i, arr) => c !== '' || (i > 0 && i < arr.length - 1));
        return new TableRow({
          children: cells.map((cell) => new TableCell({
            children: [new Paragraph({ children: parseInlineRuns(cell) })],
          })),
          tableHeader: ri === 0,
        });
      });
      children.push(new Table({ rows, width: { size: 100, type: WidthType.PERCENTAGE } }));
    }
    tableRows = [];
    inTable = false;
  }

  for (const line of lines) {
    const tr = line.trim();
    if (!tr) { if (inTable) flushTable(); continue; }
    // Markdown table row
    if (tr.startsWith('|') && tr.endsWith('|')) {
      // Skip separator rows like |---|---|
      if (/^\|[\s\-:|]+\|$/.test(tr)) { inTable = true; continue; }
      inTable = true;
      tableRows.push(tr);
      continue;
    }
    if (inTable) flushTable();
    if (tr.startsWith('### ')) children.push(new Paragraph({ heading: HeadingLevel.HEADING_3, children: parseInlineRuns(tr.slice(4)) }));
    else if (tr.startsWith('## ')) children.push(new Paragraph({ heading: HeadingLevel.HEADING_2, children: parseInlineRuns(tr.slice(3)) }));
    else if (tr.startsWith('# ')) children.push(new Paragraph({ heading: HeadingLevel.HEADING_1, children: parseInlineRuns(tr.slice(2)) }));
    else if (tr.startsWith('- ') || tr.startsWith('* ')) children.push(new Paragraph({ bullet: { level: 0 }, children: parseInlineRuns(tr.slice(2)) }));
    else if (/^\d+\.\s/.test(tr)) children.push(new Paragraph({ numbering: { reference: 'default-numbering', level: 0 }, children: parseInlineRuns(tr.replace(/^\d+\.\s/, '')) }));
    else children.push(new Paragraph({ children: parseInlineRuns(tr) }));
  }
  if (inTable) flushTable();
  return children;
}

Deno.serve(async (req) => {
  try {
    const base44 = createClientFromRequest(req);
    const user = await base44.auth.me();
    if (!user) return Response.json({ error: 'Unauthorized' }, { status: 401 });

    const { title, markdown, sessionId, project } = await req.json();
    if (!title) return Response.json({ error: 'Title is required' }, { status: 400 });

    const safeTitle = String(title).replace(/[\\/:*?"<>|]/g, '_').slice(0, 80);
    const doc = new Document({
      numbering: {
        config: [{
          reference: 'default-numbering',
          levels: [{ level: 0, format: 'decimal', text: '%1.', alignment: 'start' }],
        }],
      },
      sections: [{
        children: [
          new Paragraph({ heading: HeadingLevel.TITLE, children: [new TextRun({ text: title, bold: true })] }),
          ...mdToChildren(markdown),
        ],
      }],
    });

    const blob = await Packer.toBlob(doc);
    const file = new File([blob], `${safeTitle}.docx`, { type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' });
    const uploadRes = await base44.integrations.Core.UploadFile({ file });
    const file_url = uploadRes?.file_url || uploadRes?.data?.file_url;
    if (!file_url) throw new Error('File upload failed');

    // Also register as a UserFile so it appears in session files
    if (sessionId) {
      try {
        await base44.entities.UserFile.create({
          name: `${safeTitle}.docx`,
          file_type: 'docx',
          size: blob.size,
          file_url,
          source: 'ai_generated',
          resource_kind: 'report',
          session_id: sessionId,
          project: project || 'global',
        });
      } catch { /* UserFile creation is best-effort */ }
    }

    return Response.json({ file_url, file_name: `${safeTitle}.docx` });
  } catch (error) {
    return Response.json({ error: error.message }, { status: 500 });
  }
});