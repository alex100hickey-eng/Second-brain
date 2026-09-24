import { timingSafeEqual } from 'node:crypto';

/** Bearer <INTENTION_KEY>. Constant-time compare; false on any shape mismatch. */
export function authorized(req) {
  const want = process.env.INTENTION_KEY || '';
  if (!want) return false;
  const h = req.headers.get('authorization') || '';
  const m = /^Bearer\s+(.+)$/i.exec(h.trim());
  if (!m) return false;
  const a = Buffer.from(m[1]), b = Buffer.from(want);
  return a.length === b.length && timingSafeEqual(a, b);
}
