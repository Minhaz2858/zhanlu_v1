import { useState, useCallback, useRef } from 'react';
import {
  Copy, Check, Share2, ThumbsUp, ThumbsDown,
  FileText, Image as ImageIcon, X, Star,
} from 'lucide-react';
import { toast } from 'sonner';
import html2canvas from 'html2canvas';
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

  const handleGenerateDocument = useCallback(() => {
    setGeneratingDoc(true);
    try {
      const text = message.content || '';
      const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.download = `message-${message.id || 'export'}.txt`;
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
  };

  return (
    <TooltipProvider delayDuration={300}>
      <div className="mt-2 flex items-center gap-1">
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
