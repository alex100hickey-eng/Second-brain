// Scheduled every minute (netlify.toml). Sends the pushes that are due.
import { runTick } from './lib/due.mjs';

export default async () => {
  const report = await runTick();
  return new Response(JSON.stringify(report), { headers: { 'content-type': 'application/json' } });
};

export const config = { schedule: '* * * * *' };
