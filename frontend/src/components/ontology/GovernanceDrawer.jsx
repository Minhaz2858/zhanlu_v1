import { X } from 'lucide-react';
import ReviewCenter from '@/components/enterprise/ReviewCenter';

/* Phase 7A — governance drawer for the one-page Enterprise Ontology.

   THIN SHELL ONLY: it reuses the EXISTING ReviewCenter component and the
   EXISTING org-admin review-queue / {kind}/{id}/confirm|reject APIs —
   no duplicate review logic, no second queue, no new backend. Administrators
   inspect pending discoveries (with confidence, provenance and source
   references) and confirm or reject them without leaving the ontology page.
   Rule lifecycle (enable/disable) lives in the Business Logic section's
   rules browser; document proposals have no review lifecycle yet and are
   therefore honestly absent from the queue. */

export default function GovernanceDrawer({ onClose }) {
  return (
    <div className="fixed inset-0 z-50 bg-black/30 flex justify-end"
      onClick={onClose}>
      <div className="w-full max-w-xl bg-white h-full shadow-2xl overflow-y-auto"
        onClick={(e) => e.stopPropagation()}>
        <div className="sticky top-0 bg-white px-4 py-3 border-b border-stone-100 flex items-center gap-2">
          <h3 className="text-[13px] font-semibold text-stone-900">
            Knowledge governance
          </h3>
          <span className="text-[10px] text-stone-400">
            admin only · every decision is audited
          </span>
          <button onClick={onClose} aria-label="close"
            className="ml-auto text-stone-400 hover:text-stone-600">
            <X size={15} />
          </button>
        </div>
        <div className="px-4 py-3">
          <ReviewCenter />
        </div>
      </div>
    </div>
  );
}
