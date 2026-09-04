/* Does the same-role cap actually thin out More roles on the real Archive? */
const fs = require('fs'), vm = require('vm');
const real = JSON.parse(fs.readFileSync('/tmp/real.json', 'utf8'));
function makeSheet(name, data) {
  function range(r, c, nr, nc) {
    return { getDisplayValues: () => { const o = []; for (let i = 0; i < nr; i++) {
        const row = data[r - 1 + i] || [], s = [];
        for (let k = 0; k < nc; k++) s.push(row[c - 1 + k] === undefined ? '' : String(row[c - 1 + k]));
        o.push(s); } return o; },
      getDisplayValue: () => String((data[r - 1] || [])[c - 1] || ''),
      setValue() {}, setValues() {},
      createTextFinder: () => ({ matchCase() { return this; }, findAll: () => [] }) };
  }
  return { getName: () => name, getLastRow: () => data.length,
    getLastColumn: () => (data[0] || []).length,
    getDataRange: () => range(1, 1, data.length, (data[0] || []).length),
    getRange: (r, c, nr, nc) => range(r, c, nr === undefined ? 1 : nr, nc === undefined ? 1 : nc),
    deleteRow() {}, _data: data };
}
const sheets = {};
for (const [k, v] of Object.entries(real)) sheets[k] = makeSheet(k, v);
const sb = { console,
  SpreadsheetApp: { getActiveSpreadsheet: () => ({ getUrl: () => '#',
    getSheetByName: n => sheets[n] || null, insertSheet: n => sheets[n] }) },
  Utilities: { formatDate: () => 'x' }, Session: { getScriptTimeZone: () => 'UTC' },
  PropertiesService: { getScriptProperties: () => ({ getProperty: () => null, setProperty() {} }) },
  UrlFetchApp: { fetch() { throw new Error('x'); }, fetchAll: () => [] },
  HtmlService: { createHtmlOutput: () => ({ setTitle: () => ({ addMetaTag: () => ({}) }) }) } };
vm.createContext(sb);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), sb);

const res = sb.moreRoles();
const titles = [...res.html.matchAll(/<h3><a[^>]*>([^<]+)</g)].map(m => m[1]);
const cos = [...res.html.matchAll(/<p class="co">([^<]+)</g)].map(m => m[1]);
const groups = {}, byCo = {};
titles.forEach((t, i) => {
  const k = sb.companyKey_(cos[i]) + '|' + sb.roleKey_(t);
  groups[k] = (groups[k] || 0) + 1;
  byCo[cos[i]] = (byCo[cos[i]] || 0) + 1;
});
const over = Object.entries(groups).filter(([, n]) => n > 3);
const overCo = Object.entries(byCo).filter(([, n]) => n > 5);
console.log('More roles shows ' + titles.length + ' roles from ' +
            Object.keys(byCo).length + ' employers');
console.log('company+role groups over 3: ' + over.length);
console.log('employers over 5: ' + overCo.length);
Object.entries(byCo).sort((a, b) => b[1] - a[1]).slice(0, 8)
  .forEach(([c, n]) => console.log('   ' + n + 'x ' + c));
process.exit(over.length || overCo.length ? 1 : 0);
