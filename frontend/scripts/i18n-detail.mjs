import { scanUntranslatedCjk } from './i18n-audit.mjs';
const files = process.argv.slice(2);
const map = scanUntranslatedCjk();
for (const f of files) {
  const hits = map.get(f);
  console.log('=== ' + f + ' (' + (hits ? hits.length : 0) + ')');
  if (hits) for (const h of hits) console.log('  L' + h.line + ' [' + h.kind + '] ' + h.text);
}
