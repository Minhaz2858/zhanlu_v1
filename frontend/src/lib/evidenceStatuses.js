/**
 * Canonical evidence statuses for the enterprise ontology.
 *
 * SOURCE OF TRUTH: backend/app/services/ontology/evidence.py
 * (EVIDENCE_STATUSES). Keep this list identical — the EvidenceBadge and
 * any ontology UI must only ever render these five values:
 *
 *   verified    — human-authorized bind (emerald)
 *   computed    — deterministic registered logic, e.g. metric backing (sky)
 *   inferred    — machine-proposed with recorded evidence (amber)
 *   proposed    — awaiting evidence or confirmation (stone)
 *   unavailable — supporting evidence vanished, e.g. schema drift (red)
 */
export const EVIDENCE_STATUSES = [
  'verified',
  'computed',
  'inferred',
  'proposed',
  'unavailable',
];

export const EVIDENCE_BADGE_STYLE = {
  verified: 'border-emerald-200 bg-emerald-50 text-emerald-700',
  computed: 'border-sky-200 bg-sky-50 text-sky-700',
  inferred: 'border-amber-200 bg-amber-50 text-amber-700',
  proposed: 'border-stone-200 bg-stone-50 text-stone-400',
  unavailable: 'border-red-200 bg-red-50 text-red-700',
};

export function isEvidenceStatus(v) {
  return EVIDENCE_STATUSES.includes(v);
}
