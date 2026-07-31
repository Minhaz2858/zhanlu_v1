import { createClientFromRequest } from 'npm:@base44/sdk@0.8.31';
import pptxgen from 'npm:pptxgenjs@3.12.0';

Deno.serve(async (req) => {
  try {
    const base44 = createClientFromRequest(req);
    const user = await base44.auth.me();
    if (!user) return Response.json({ error: 'Unauthorized' }, { status: 401 });

    const { title, slides } = await req.json();
    if (!title) return Response.json({ error: 'Title is required' }, { status: 400 });
    if (!Array.isArray(slides) || slides.length === 0) return Response.json({ error: 'Slides array is required' }, { status: 400 });

    const safeTitle = String(title).replace(/[\\/:*?"<>|]/g, '_').slice(0, 80);
    const pptx = new pptxgen();
    pptx.defineLayout({ name: 'WIDE', width: 13.333, height: 7.5 });
    pptx.layout = 'WIDE';

    for (const slide of slides) {
      const s = pptx.addSlide();
      const slideTitle = String(slide.title || '').trim();
      if (slideTitle) {
        s.addText(slideTitle, {
          x: 0.6, y: 0.4, w: 12.1, h: 1,
          fontSize: 32, bold: true, color: 'C05621', fontFace: 'Georgia',
        });
      }
      const bullets = Array.isArray(slide.bullets) ? slide.bullets : [];
      if (bullets.length > 0) {
        const textObjs = bullets.map((b) => ({
          text: String(b),
          options: { bullet: true, breakLine: true, fontSize: 20, color: '2D2A26' },
        }));
        s.addText(textObjs, { x: 0.6, y: 1.6, w: 12.1, h: 5.3, valign: 'top' });
      }
    }

    const arrayBuffer = await pptx.write({ outputType: 'arraybuffer' });
    const file = new File([arrayBuffer], `${safeTitle}.pptx`, {
      type: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    });
    const uploadRes = await base44.integrations.Core.UploadFile({ file });
    const file_url = uploadRes?.file_url || uploadRes?.data?.file_url;
    if (!file_url) throw new Error('File upload failed');

    return Response.json({ file_url, file_name: `${safeTitle}.pptx` });
  } catch (error) {
    return Response.json({ error: error.message }, { status: 500 });
  }
});