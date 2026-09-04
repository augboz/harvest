/* Run the dashboard's server functions against the REAL sheet contents.
   The unit harness uses tidy fixtures; this uses the actual rows, which is where
   "More roles" and "Opening soon" are reportedly hanging. */
const fs = require('fs'), vm = require('vm');
const real = JSON.parse(fs.readFileSync('/tmp/real.json', 'utf8'));
function makeSheet(name, data) {
  function range(r, c, nr, nc) {
    return {
      getDisplayValues: () => { const o = []; for (let i = 0; i < nr; i++) {
        const row = data[r - 1 + i] || [], s = [];
        for (let k = 0; k < nc; k++) s.push(row[c - 1 + k] === undefined ? '' : String(row[c - 1 + k]));
        o.push(s); } return o; },
      getDisplayValue: () => String((data[r - 1] || [])[c - 1] || ''),
      setValue() {}, setValues() {},
      createTextFinder: () => ({ matchCase() { return this; }, findAll: () => [] })
    };
  }
  return { getName: () => name, getLastRow: () => data.length,
           getLastColumn: () => (data[0] || []).length,
           getDataRange: () => range(1, 1, data.length, (data[0] || []).length),
           getRange: (r, c, nr, nc) => range(r, c, nr === undefined ? 1 : nr, nc === undefined ? 1 : nc),
           deleteRow() {}, _data: data };
}
const sheets = {};
for (const [k, v] of Object.entries(real)) sheets[k] = makeSheet(k, v);
const sandbox = { console,
  SpreadsheetApp: { getActiveSpreadsheet: () => ({ getUrl: () => 'https://sheet',
    getSheetByName: n => sheets[n] || null, insertSheet: n => sheets[n] }) },
  Utilities: { formatDate: () => '2026-08-18' }, Session: { getScriptTimeZone: () => 'UTC' },
  PropertiesService: { getScriptProperties: () => ({ getProperty: () => null, setProperty() {} }) },
  UrlFetchApp: { fetch() { throw new Error('no net'); }, fetchAll: () => [] },
  HtmlService: { createHtmlOutput: () => ({ setTitle: () => ({ addMetaTag: () => ({}) }) }) } };
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), sandbox);

let bad = 0;
function attempt(label, fn) {
  const t = Date.now();
  try { const r = fn(); console.log(`OK    ${label}  (${Date.now() - t}ms) ${r}`); }
  catch (e) { bad++; console.log(`THROW ${label}: ${e.message}\n      ${(e.stack || '').split('\n')[1] || ''}`); }
}
attempt('renderPage()', () => sandbox.renderPage().length + ' bytes');
attempt('moreRoles()', () => { const r = sandbox.moreRoles(); return `${r.count} rows, ${r.html.length} bytes`; });
attempt('openingSoon()', () => { const r = sandbox.openingSoon(); return `${r.count} rows, ${r.html.length} bytes`; });
attempt('recentArchive_()', () => sandbox.recentArchive_().length + ' rows');
process.exit(bad ? 1 : 0);
