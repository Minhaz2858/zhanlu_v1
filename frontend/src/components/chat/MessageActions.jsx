import { useState, useCallback } from 'react';
import {
  Copy, Check, Share2, ThumbsUp, ThumbsDown, RotateCcw,
  FileText, Image as ImageIcon, X, Star,
} from 'lucide-react';
import { toast } from 'sonner';
import html2canvas from 'html2canvas';
import {
  Document, Packer, Paragraph, TextRun, HeadingLevel,
  BorderStyle, Table, TableRow, TableCell, WidthType,
} from 'docx';
import {
  Tooltip, TooltipTrigger, TooltipContent, TooltipProvider,
} from '@/components/ui/tooltip';
import {
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent,
  DropdownMenuItem, DropdownMenuSeparator,
} from '@/components/ui/dropdown-menu';
import { useLanguage } from '@/lib/LanguageProvider';

export default function MessageActions({
  message,
  messageRef,
  onFeedback,
  feedbackRating,
  isStreaming,
  showRoleRating,
  onRoleRelevance,
  roleRelevanceRating,
  onRegenerate,
}) {
  const { lang } = useLanguage();
  const [copied, setCopied] = useState(false);
  const [shareOpen, setShareOpen] = useState(false);
  const [generatingImage, setGeneratingImage] = useState(false);
  const [generatingDoc, setGeneratingDoc] = useState(false);
  const [hoverRating, setHoverRating] = useState(0);

  const handleCopy = useCallback(() => {
    const text = message.content || '';
    navigator.clipboard.writeText(text).then(() => {
      setCopied(true);
      toast.success(lang === 'en' ? 'Copied to clipboard' : '已复制到剪贴板');
      setTimeout(() => setCopied(false), 1500);
    }).catch(() => {
      toast.error(lang === 'en' ? 'Copy failed' : '复制失败');
    });
  }, [message.content, lang]);

  const handleShareCopyText = useCallback(() => {
    const text = message.content || '';
    navigator.clipboard.writeText(text).then(() => {
      toast.success(lang === 'en' ? 'Text copied' : '文本已复制');
      setShareOpen(false);
    }).catch(() => {
      toast.error(lang === 'en' ? 'Copy failed' : '复制失败');
    });
  }, [message.content, lang]);

  const handleGenerateImage = useCallback(async () => {
    if (!messageRef?.current) {
      toast.error(lang === 'en' ? 'Message element not found' : '未找到消息元素');
      return;
    }
    setGeneratingImage(true);
    try {
      const canvas = await html2canvas(messageRef.current, {
        backgroundColor: '#0f0f0f',
        scale: 2,
        useCORS: true,
      });
      const link = document.createElement('a');
      link.download = `message-${message.id || 'export'}.png`;
      link.href = canvas.toDataURL('image/png');
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      toast.success(lang === 'en' ? 'Image downloaded' : '图片已下载');
    } catch (e) {
      toast.error(lang === 'en' ? `Image generation failed: ${e.message}` : `图片生成失败: ${e.message}`);
    } finally {
      setGeneratingImage(false);
      setShareOpen(false);
    }
  }, [messageRef, message.id, lang]);

  const handleGenerateDocument = useCallback(async () => {
    setGeneratingDoc(true);
    try {
      const htmlContent = message.content || '';
      const children = htmlToDocxChildren(htmlContent);

      const doc = new Document({
        sections: [{
          properties: {},
          children: children.length > 0
            ? children
            : [new Paragraph({ children: [new TextRun(htmlContent)] })],
        }],
      });

      const blob = await Packer.toBlob(doc);
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.download = `document-${message.id || 'export'}.docx`;
      link.href = url;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      URL.revokeObjectURL(url);
      toast.success(lang === 'en' ? 'Document downloaded' : '文档已下载');
    } catch (e) {
      toast.error(lang === 'en' ? `Document generation failed: ${e.message}` : `文档生成失败: ${e.message}`);
    } finally {
      setGeneratingDoc(false);
      setShareOpen(false);
    }
  }, [message.content, message.id, lang]);

  /** Parse HTML string into an array of docx Paragraph/Table elements */
  const htmlToDocxChildren = useCallback((html) => {
    const parser = new DOMParser();
    const doc = parser.parseFromString(html, 'text/html');
    const body = doc.body;
    const children = [];

    const processNode = (node) => {
      if (node.nodeType === Node.TEXT_NODE) {
        return node.textContent;
      }
      if (node.nodeType !== Node.ELEMENT_NODE) return null;

      const tag = node.tagName.toLowerCase();

      // Headings
      if (/^h[1-6]$/.test(tag)) {
        const level = parseInt(tag[1]) - 1;
        return new Paragraph({
          heading: level > 0 ? HeadingLevel[`HEADING_${level}`] : undefined,
          children: [new TextRun({ text: node.textContent.trim(), bold: true, size: 28 - level * 2 })],
          spacing: { after: 200 },
        });
      }

      // Tables
      if (tag === 'table') {
        const rows = Array.from(node.querySelectorAll('tr'));
        if (rows.length === 0) return null;

        const tableRows = rows.map((row, ri) => {
          const cells = Array.from(row.querySelectorAll('th, td'));
          return new TableRow({
            children: cells.map(cell =>
              new TableCell({
                children: [new Paragraph({
                  children: [new TextRun({
                    text: cell.textContent.trim(),
                    bold: cell.tagName.toLowerCase() === 'th',
                  })],
                })],
                borders: {
                  top: { style: BorderStyle.SINGLE, size: 1 },
                  bottom: { style: BorderStyle.SINGLE, size: 1 },
                  left: { style: BorderStyle.SINGLE, size: 1 },
                  right: { style: BorderStyle.SINGLE, size: 1 },
                },
              })
            ),
          });
        });
        return new Table({ rows: tableRows, width: { size: 100, type: WidthType.PERCENTAGE } });
      }

      // Block-level elements → Paragraph
      if (['p', 'div', 'li', 'blockquote', 'pre', 'section'].includes(tag)) {
        const runs = [];
        for (const child of node.childNodes) {
          if (child.nodeType === Node.TEXT_NODE) {
            runs.push(new TextRun(child.textContent));
          } else if (child.nodeType === Node.ELEMENT_NODE) {
            const childTag = child.tagName.toLowerCase();
            const text = child.textContent;
            if (childTag === 'strong' || childTag === 'b') {
              runs.push(new TextRun({ text, bold: true }));
            } else if (childTag === 'em' || childTag === 'i') {
              runs.push(new TextRun({ text, italics: true }));
            } else if (childTag === 'code') {
              runs.push(new TextRun({ text, font: 'Courier New' }));
            } else if (childTag === 'br') {
              runs.push(new TextRun({ break: 1 }));
            } else if (childTag === 'a' && child.href) {
              runs.push(new TextRun({ text: text || child.href, style: 'Hyperlink' }));
            } else if (childTag === 'ul' || childTag === 'ol') {
              // nested list items handled by recursion below
            } else {
              runs.push(new TextRun(text));
            }
          }
        }
        if (runs.length === 0 && node.textContent.trim()) {
          runs.push(new TextRun(node.textContent.trim()));
        }
        if (runs.length > 0) {
          return new Paragraph({
            children: runs,
            bullet: tag === 'li' ? { level: 0 } : undefined,
            spacing: { after: tag === 'li' ? 120 : 160 },
          });
        }
      }

      // List containers — recurse into children
      if (tag === 'ul' || tag === 'ol') {
        const items = [];
        for (const li of node.children) {
          const result = processNode(li);
          if (result) items.push(result);
        }
        return items;
      }

      // Horizontal rule
      if (tag === 'hr') {
        return new Paragraph({
          border: { bottom: { style: BorderStyle.SINGLE, size: 6 } },
          spacing: { after: 200 },
        });
      }

      return null;
    };

    // Walk top-level body children
    for (const child of body.childNodes) {
      const result = processNode(child);
      if (Array.isArray(result)) {
        children.push(...result);
      } else if (result) {
        children.push(result);
      }
    }

    return children;
  }, []);

  const handleFeedback = useCallback((rating) => {
    if (!onFeedback || !message.id) return;
    onFeedback(message.id, rating);
  }, [onFeedback, message.id]);

  if (isStreaming) return null;

  const t = {
    copy: lang === 'en' ? 'Copy' : '复制',
    share: lang === 'en' ? 'Share' : '分享',
    like: lang === 'en' ? 'Like' : '点赞',
    dislike: lang === 'en' ? 'Dislike' : '点踩',
    copyText: lang === 'en' ? 'Copy Text' : '复制文本',
    generateImage: lang === 'en' ? 'Generate Image' : '生成图片',
    generateDoc: lang === 'en' ? 'Generate Document' : '生成文档',
    cancel: lang === 'en' ? 'Cancel' : '取消',
    regenerate: lang === 'en' ? 'Regenerate' : '重新生成',
  };

  return (
    <TooltipProvider delayDuration={300}>
      <div className="mt-2 flex items-center gap-1">
        {/* Regenerate (assistant messages only — re-run the same user turn) */}
        {onRegenerate && (
          <Tooltip>
            <TooltipTrigger asChild>
              <button
                type="button"
                onClick={() => onRegenerate(message)}
                className="inline-flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground"
                aria-label={t.regenerate}
              >
                <RotateCcw className="h-3.5 w-3.5" />
              </button>
            </TooltipTrigger>
            <TooltipContent side="bottom">
              <p>{t.regenerate}</p>
            </TooltipContent>
          </Tooltip>
        )}

        {/* Copy */}
        <Tooltip>
          <TooltipTrigger asChild>
            <button
              type="button"
              onClick={handleCopy}
              className="inline-flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground"
              aria-label={t.copy}
            >
              {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
            </button>
          </TooltipTrigger>
          <TooltipContent side="bottom">
            <p>{t.copy}</p>
          </TooltipContent>
        </Tooltip>

        {/* Share dropdown */}
        <DropdownMenu open={shareOpen} onOpenChange={setShareOpen}>
          <Tooltip>
            <TooltipTrigger asChild>
              <DropdownMenuTrigger asChild>
                <button
                  type="button"
                  className="inline-flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground"
                  aria-label={t.share}
                >
                  <Share2 className="h-3.5 w-3.5" />
                </button>
              </DropdownMenuTrigger>
            </TooltipTrigger>
            <TooltipContent side="bottom">
              <p>{t.share}</p>
            </TooltipContent>
          </Tooltip>
          <DropdownMenuContent align="start" className="w-52">
            <DropdownMenuItem onClick={handleShareCopyText}>
              <Copy className="mr-2 h-4 w-4" />
              {t.copyText}
            </DropdownMenuItem>
            <DropdownMenuItem onClick={handleGenerateImage} disabled={generatingImage}>
              <ImageIcon className="mr-2 h-4 w-4" />
              {generatingImage ? (lang === 'en' ? 'Generating…' : '生成中…') : t.generateImage}
            </DropdownMenuItem>
            <DropdownMenuItem onClick={handleGenerateDocument} disabled={generatingDoc}>
              <FileText className="mr-2 h-4 w-4" />
              {generatingDoc ? (lang === 'en' ? 'Generating…' : '生成中…') : t.generateDoc}
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem onClick={() => setShareOpen(false)}>
              <X className="mr-2 h-4 w-4" />
              {t.cancel}
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>

        {/* Like */}
        {onFeedback && (
          <>
            <Tooltip>
              <TooltipTrigger asChild>
                <button
                  type="button"
                  onClick={() => handleFeedback(1)}
                  className={`inline-flex h-7 w-7 items-center justify-center rounded-md transition-colors ${
                    feedbackRating === 1
                      ? 'bg-green-50 text-green-600'
                      : 'text-muted-foreground hover:bg-secondary hover:text-green-600'
                  }`}
                  aria-label={t.like}
                >
                  <ThumbsUp className="h-3.5 w-3.5" />
                </button>
              </TooltipTrigger>
              <TooltipContent side="bottom">
                <p>{t.like}</p>
              </TooltipContent>
            </Tooltip>

            {/* Dislike */}
            <Tooltip>
              <TooltipTrigger asChild>
                <button
                  type="button"
                  onClick={() => handleFeedback(-1)}
                  className={`inline-flex h-7 w-7 items-center justify-center rounded-md transition-colors ${
                    feedbackRating === -1
                      ? 'bg-red-50 text-red-600'
                      : 'text-muted-foreground hover:bg-secondary hover:text-red-600'
                  }`}
                  aria-label={t.dislike}
                >
                  <ThumbsDown className="h-3.5 w-3.5" />
                </button>
              </TooltipTrigger>
              <TooltipContent side="bottom">
                <p>{t.dislike}</p>
              </TooltipContent>
            </Tooltip>
          </>
        )}

        {/* Role relevance rating (1-5 stars) — shown on throttled messages */}
        {showRoleRating && onRoleRelevance && (
          <Tooltip>
            <TooltipTrigger asChild>
              <button
                type="button"
                className={`inline-flex h-7 items-center gap-0.5 rounded-md px-1.5 text-xs transition-colors ${
                  roleRelevanceRating
                    ? 'bg-amber-50 font-medium text-amber-600'
                    : 'text-muted-foreground hover:bg-secondary hover:text-amber-500'
                }`}
                aria-label={lang === 'en' ? 'Relevant to your role?' : '与您的角色相关吗？'}
                onMouseEnter={() => setHoverRating(0)}
                onMouseLeave={() => setHoverRating(0)}
              >
                {[1, 2, 3, 4, 5].map((n) => (
                  <Star
                    key={n}
                    onMouseEnter={() => setHoverRating(n)}
                    onClick={() => onRoleRelevance(message.id, n)}
                    className={`h-3.5 w-3.5 cursor-pointer ${
                      n <= (hoverRating || roleRelevanceRating || 0)
                        ? 'fill-amber-400 text-amber-400'
                        : 'text-muted-foreground'
                    }`}
                  />
                ))}
              </button>
            </TooltipTrigger>
            <TooltipContent side="bottom">
              <p>{lang === 'en' ? 'Relevant to your role?' : '与您的角色相关吗？'}</p>
            </TooltipContent>
          </Tooltip>
        )}
      </div>
    </TooltipProvider>
  );
}
