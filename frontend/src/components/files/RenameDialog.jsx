import { useState, useEffect } from 'react';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';

export default function RenameDialog({ open, item, onConfirm, onCancel, t }) {
  const [name, setName] = useState('');

  useEffect(() => {
    if (open) setName(item?.name || '');
  }, [open, item]);

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onCancel()}>
      <DialogContent className="max-w-sm">
        <DialogHeader>
          <DialogTitle>{t.myFiles.renameTitle}</DialogTitle>
        </DialogHeader>
        <Input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder={t.myFiles.renamePh}
          autoFocus
          onKeyDown={(e) => e.key === 'Enter' && name.trim() && onConfirm(name)}
        />
        <DialogFooter>
          <Button variant="outline" onClick={onCancel}>{t.myFiles.cancel}</Button>
          <Button onClick={() => onConfirm(name)} disabled={!name.trim()}>{t.myFiles.confirm}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}