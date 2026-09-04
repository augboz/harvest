/* Runs dashboard/Code.gs against a fake Spreadsheet to prove the row-shift bug
   is fixed. No Google APIs involved: everything below is a stub. */
const fs = require('fs');
const vm = require('vm');

function makeSheet(name, rows) {
  const data = rows.map(r => r.slice());
  const sheet = {
    _name: name,
    getName: () => name,
    getLastRow: () => data.length,
    getLastColumn: () => data[0].length,
    getDataRange: () => range(1, 1, data.length, data[0].length),
    getRange: (r, c, nr, nc) => range(r, c, nr === undefined ? 1 : nr, nc === undefined ? 1 : nc),
    deleteRow: (r) => { data.splice(r - 1, 1); },
    // 1-based, like the real one: insertColumnAfter(5) opens up column 6
    insertColumnAfter: (c) => data.forEach(r => {
      while (r.length < c) r.push('');
      r.splice(c, 0, '');
    }),
    _data: data
  };
  function range(r, c, nr, nc) {
    return {
      getDisplayValues: () => {
        const out = [];
        for (let i = 0; i < nr; i++) {
          const row = data[r - 1 + i] || [];
          const slice = [];
          for (let k = 0; k < nc; k++) slice.push(row[c - 1 + k] === undefined ? '' : String(row[c - 1 + k]));
          out.push(slice);
        }
        return out;
      },
      getDisplayValue: () => {
        const row = data[r - 1] || [];
        return row[c - 1] === undefined ? '' : String(row[c - 1]);
      },
      // the fixtures hold nothing but strings, so this is the same read
      getValues: () => range(r, c, nr, nc).getDisplayValues(),
      setValue: (v) => { if (!data[r - 1]) data[r - 1] = []; data[r - 1][c - 1] = v; },
      setValues: (vals) => {
        for (let i = 0; i < vals.length; i++) {
          if (!data[r - 1 + i]) data[r - 1 + i] = [];
          for (let k = 0; k < vals[i].length; k++) data[r - 1 + i][c - 1 + k] = vals[i][k];
        }
      },
      createTextFinder: (needle) => {
        const finder = {
          matchCase: () => finder,
          findAll: () => {
            const hits = [];
            for (let i = 0; i < nr; i++) {
              const cell = String((data[r - 1 + i] || [])[c - 1] || '');
              if (cell.toLowerCase().includes(String(needle).toLowerCase())) {
                hits.push({ getRow: () => r + i });
              }
            }
            return hits;
          }
        };
        return finder;
      }
    };
  }
  return sheet;
}

const HEAD = ['Category', 'Company', 'Role', 'Score', 'Location', 'Link', 'Priority',
              'Deadline', 'Opens', 'Duration', 'Status', 'Notes', 'Description',
              'Score reason', 'Source'];
function row(company, role) {
  const m = { Category: 'Software', Company: company, Role: role, Score: '80',
              Location: 'London, UK', Link: 'https://jobs.example/' + company.toLowerCase(),
              Status: 'Not applied', Description: 'desc for ' + company, Source: 'src' };
  return HEAD.map(h => m[h] === undefined ? '' : m[h]);
}

const jobs = makeSheet('Job Bank', [HEAD,
  row('Stripe', 'SWE Intern'), row('Datadog', 'SWE Intern'), row('Renault', 'Software Intern'),
  row('TikTok', 'Backend Intern'), row('Spotify', 'ML Intern')]);
const ARCH_HEAD = ['Added', 'Score', 'Category', 'Company', 'Role', 'Location',
                   'Deadline', 'Link', 'Source', 'Disposition', 'Description', 'Duration'];
const archive = makeSheet('Archive', [ARCH_HEAD]);
const SOON_HEAD = ['Company', 'Programme', 'Expected open date', 'Expected close date',
                   'Status', 'Link', 'Notes'];
const soon = makeSheet('Opening Soon', [SOON_HEAD,
  ['Google', 'SWE Intern 2027', '2026-09-01', '2027-01-31', 'Predicted from last year', 'https://g.co', '']]);
const watch = makeSheet('Watchlist', [['Company', 'ATS', 'Slug / URL', 'Notes'],
  ['Mistral', 'lever', 'mistral', '']]);
const sheets = { 'Job Bank': jobs, 'Archive': archive, 'Opening Soon': soon, 'Watchlist': watch };

const props = {};
const sandbox = {
  console,
  SpreadsheetApp: { getActiveSpreadsheet: () => ({
    getUrl: () => 'https://sheet', getSheetByName: (n) => sheets[n] || null,
    insertSheet: (n) => (sheets[n] = makeSheet(n, [['a']])) }) },
  Utilities: { formatDate: () => '2026-08-17' },
  Session: { getScriptTimeZone: () => 'UTC' },
  PropertiesService: { getScriptProperties: () => ({
    getProperty: (k) => props[k] || null, setProperty: (k, v) => { props[k] = v; } }) },
  // Stubbed job boards: lever answers for "mistral", the others 404 like a bad slug.
  // Each profile wants the OPPOSITE role type, so the board offers both plus a
  // senior role that neither should ever keep.
  UrlFetchApp: {
    fetch: () => { throw new Error('blocked'); },
    fetchAll: (reqs) => reqs.map(req => {
      const lever = req.url.includes('api.lever.co') && req.url.includes('mistral');
      if (!lever) return { getResponseCode: () => 404, getContentText: () => '' };
      return {
        getResponseCode: () => 200,
        getContentText: () => JSON.stringify([
          { text: 'Machine Learning Intern - Summer 2027', hostedUrl: 'https://jobs.lever.co/mistral/1',
            categories: { location: 'Paris, France', commitment: 'Internship' },
            descriptionPlain: 'Work on inference.' },
          { text: 'Senior Staff Engineer, Inference', hostedUrl: 'https://jobs.lever.co/mistral/2',
            categories: { location: 'Paris, France' }, descriptionPlain: 'Lead the team.' },
          { text: 'Research Placement (Year in Industry)', hostedUrl: 'https://jobs.lever.co/mistral/3',
            categories: { location: 'London, UK' }, descriptionPlain: 'A placement year.' },
          { text: 'Graduate Engineer Programme 2027', hostedUrl: 'https://jobs.lever.co/mistral/4',
            categories: { location: 'Derby, UK' }, descriptionPlain: 'Two-year graduate scheme.' }])
      };
    })
  },
  HtmlService: { createHtmlOutput: (h) => ({ setTitle: () => ({ addMetaTag: () => ({}) }) }) }
};
vm.createContext(sandbox);
const SRC = fs.readFileSync(process.argv[2], 'utf8');
vm.runInContext(SRC, sandbox);

const RENAULT = 'https://jobs.example/renault';
let fails = 0;
function check(label, got, want) {
  const ok = got === want;
  if (!ok) fails++;
  console.log((ok ? 'PASS  ' : 'FAIL  ') + label + '  got=' + JSON.stringify(got) +
              (ok ? '' : ' want=' + JSON.stringify(want)));
}

// Renault renders at sheet row 4, TikTok at row 5.
check('prompt before any removal', sandbox.tailoringPrompt(4, 'Job Bank', RENAULT).company, 'Renault');

// Remove Datadog (row 3). Renault shifts to row 3, TikTok to row 4 — but the open
// page still says Renault is row 4, which is now TikTok. This is the reported bug.
const rm = sandbox.removeJob(3, false, 'not for me', 'https://jobs.example/datadog');
check('removeJob reports the row it deleted', rm.row, 3);
check('sheet really shifted (row 4 is now TikTok)',
      jobs._data[3][HEAD.indexOf('Company')], 'TikTok');

// The reported symptom, now guarded: stale row 4 + Renault's link -> Renault.
check('prompt on stale row still returns Renault',
      sandbox.tailoringPrompt(4, 'Job Bank', RENAULT).company, 'Renault');
check('outreach on stale row still returns Renault',
      sandbox.outreachFor(4, 'Job Bank', RENAULT).company, 'Renault');
sandbox.setStatus(4, 'Applied', RENAULT);
check('status written to Renault, not TikTok',
      jobs._data[2][HEAD.indexOf('Status')], 'Applied');
check('TikTok untouched', jobs._data[3][HEAD.indexOf('Status')], 'Not applied');

// A role that is genuinely gone must say so rather than act on a neighbour.
check('removed role reports staleness',
      sandbox.tailoringPrompt(3, 'Job Bank', 'https://jobs.example/datadog').ok, false);

// A pre-fix cached page sends no link: fall back to the row number as before.
check('no-link fallback still works', sandbox.tailoringPrompt(3, 'Job Bank').company, 'Renault');

// Removing by stale row number must delete the right role.
sandbox.removeJob(5, false, '', 'https://jobs.example/tiktok');   // TikTok is at row 4 now
check('stale-row removal deleted TikTok', jobs._data.map(r => r[HEAD.indexOf('Company')]).join(','),
      'Company,Stripe,Renault,Spotify');

// --- bank sizing: removals must not be refilled one-for-one ---
// Archive holds three strong candidates; the bank currently holds 3 roles.
const arch = [
  ['2026-08-01', '82', 'Software', 'Figma', 'SWE Intern', 'London', '', 'https://jobs.example/figma', 'src', '', 'd', ''],
  ['2026-08-01', '81', 'Software', 'Stripe', 'Infra Intern', 'London', '', 'https://jobs.example/stripe2', 'src', '', 'd', ''],
  ['2026-08-01', '60', 'Software', 'Weak Co', 'Intern', 'London', '', 'https://jobs.example/weak', 'src', '', 'd', '']];
arch.forEach(r => archive._data.push(r));

// 3 rows, but Renault was marked Applied above, so only 2 count as open.
check('bank is thin (2 open)', sandbox.openCount_(), 2);
const thin = sandbox.removeJob(2, true, '', 'https://jobs.example/stripe');
check('below the floor -> refills', thin.replacement && thin.replacement.Company, 'Figma');

// Now push the bank back above the floor and remove again: no replacement.
[row('A Co', 'Intern'), row('B Co', 'Intern'), row('C Co', 'Intern'), row('D Co', 'Intern')]
  .forEach(r => jobs._data.push(r));
check('bank is healthy again', sandbox.openCount_() >= 5, true);
// Replacing is now an explicit choice in the removal dialog, so it is obeyed as
// asked: the refill floor governs the PIPELINE, not a deliberate click.
const declined = sandbox.removeJob(2, false, '', jobs._data[1][HEAD.indexOf('Link')]);
check('asked NOT to replace -> nothing is added', declined.replacement, null);
const asked = sandbox.removeJob(2, true, '', jobs._data[1][HEAD.indexOf('Link')]);
check('asked to replace -> one is added even above the floor',
      !!(asked.replacement && asked.replacement.Company), true);

// Applied roles are your pipeline, not clutter: they must not hold a slot.
jobs._data.forEach(r => { if (r[HEAD.indexOf('Company')] !== 'Company') r[HEAD.indexOf('Status')] = 'Applied'; });
check('applied roles do not count as open', sandbox.openCount_(), 0);

// The rendered card must carry the link the client sends back.
const html = sandbox.card({ _row: 3, Company: 'Renault', Role: 'Software Intern',
                            Score: '80', Link: RENAULT, Status: 'Not applied' }, 0);
check('card carries data-link', html.indexOf('data-link="' + RENAULT + '"') >= 0, true);

// --- find roles: search, then add what you found ---
const GRAD = SRC.includes('wants GRADUATE SCHEMES');   // a graduate-scheme profile inverts the type
const found = sandbox.searchCompany('Mistral', false, '');
check('search reaches the live job board', found.ok && found.counts.ats > 0, true);
const roles = found.results.map(r => r.role);
check('search keeps the role type this profile wants',
      roles.some(r => (GRAD ? /Graduate Engineer/ : /Machine Learning Intern/).test(r)), true);
check('search drops the role type this profile does NOT want',
      roles.some(r => (GRAD ? /Machine Learning Intern/ : /Graduate Engineer/).test(r)), false);
check('search drops the senior role', roles.some(r => /Senior Staff/.test(r)), false);
check('unconfigured Adzuna is reported, not silently empty', found.adzunaConfigured, false);
check('no AI key -> honest heuristic label', found.results[0].scoredBy, 'heuristic');
check('search renders one card per result',
      (found.html.match(/class="hit"/g) || []).length, found.results.length);

// A company the pipeline has already seen comes back from the sheet, free.
const local = sandbox.searchCompany('Spotify', false, '');
check('search finds roles already on the sheet', local.counts.local > 0, true);

// Adding a hit puts it in the bank and refuses the obvious duplicate. Which role
// tops the list depends on the profile, so take whatever the search ranked first.
const topHit = found.results[0];
const add1 = sandbox.addFoundRole(topHit);
check('adding a search hit works', add1.ok, true);
check('the hit really landed in the bank',
      jobs._data.some(r => String(r[HEAD.indexOf('Role')]) === topHit.role), true);
check('adding the same role twice is refused', sandbox.addFoundRole(topHit).ok, false);

// Manual add: the "I found it myself" path, no link required.
const own = sandbox.addFoundRole({ company: 'European Space Agency',
  role: 'Graduate Trainee 2027', location: 'Noordwijk, Netherlands',
  description: 'Found on their careers page.', source: 'added by hand' });
check('manual add works without a link', own.ok, true);
check('manual add ignores the 10-role ceiling', sandbox.openCount_() > 0, true);
check('manual add is labelled honestly',
      jobs._data.some(r => String(r[HEAD.indexOf('Source')]) === 'added by hand'), true);
check('manual add needs a company and role', sandbox.addFoundRole({ role: 'x' }).ok, false);

// --- opening soon ---
const soonAdd = sandbox.addOpeningSoon({ company: 'European Space Agency',
  programme: 'Graduate Trainee Programme 2027', openDate: '2027-02-01',
  link: 'https://jobs.esa.int', notes: 'Opens around February.', status: 'Added by hand' });
check('tracking a future programme works', soonAdd.ok, true);
check('a dated entry promises the email', soonAdd.emailed, true);
check('the date is stored ISO for the pipeline to parse',
      soon._data[soon._data.length - 1][SOON_HEAD.indexOf('Expected open date')], '2027-02-01');
check('duplicate programme is refused', sandbox.addOpeningSoon({
  company: 'European Space Agency', programme: 'Graduate Trainee Programme 2027' }).ok, false);
check('opening soon lists both entries', sandbox.openingSoon().count, 2);
const dis = sandbox.dismissOpeningSoon(soon._data.length, 'European Space Agency');
check('dismiss marks the row', dis.ok, true);
check('dismissed entries drop out of the list', sandbox.openingSoon().count, 1);
check('nothing was deleted from the tab', soon._data.length, 3);
check('dismiss on a mismatched row is refused',
      sandbox.dismissOpeningSoon(2, 'Not The Right Company').ok, false);

// --- status groups (design option B: Act now / In flight / Closed out) ---
check('interview is in flight', sandbox.bucketOf_('Interview'), 'waiting');
check('OA is in flight', sandbox.bucketOf_('OA'), 'waiting');
check('an offer is still in flight', sandbox.bucketOf_('Offer'), 'waiting');
check('blank status counts as act-now', sandbox.bucketOf_(''), 'apply');
check('applied is in flight', sandbox.bucketOf_('Applied'), 'waiting');
check('rejected is closed out', sandbox.bucketOf_('Rejected'), 'closed');
check('an unknown status falls back to act-now', sandbox.bucketOf_('Ghosted'), 'apply');

// put one role in each interesting state and check the ONE table groups them.
// tbodys, not sections: option B is a single list with band rows, and the
// client's regroup() moves a <tr> between tbodys with a plain appendChild.
jobs._data[1][HEAD.indexOf('Status')] = 'Interview';
jobs._data[2][HEAD.indexOf('Status')] = 'Not applied';
const grouped = sandbox.renderPage();
function tbodyOf(html, key) {
  const i = html.indexOf('id="grid-' + key + '"');
  return i < 0 ? '' : html.slice(i, html.indexOf('</tbody>', i));
}
check('the page renders one band per group', sandbox.BUCKETS.length,
      sandbox.BUCKETS.filter(b => grouped.includes('id="grid-' + b.key + '"')).length);
check('the watchlist gets a band of its own',
      grouped.includes('id="grid-watch"'), true);
check('the interview role sits in the in-flight band',
      tbodyOf(grouped, 'waiting').includes(jobs._data[1][HEAD.indexOf('Company')]), true);
check('the not-applied role sits in the act-now band',
      tbodyOf(grouped, 'apply').includes(jobs._data[2][HEAD.indexOf('Company')]), true);
check('an empty band says so instead of a blank shelf',
      tbodyOf(grouped, 'closed').includes('bempty'), true);
check('every band header carries its count',
      (grouped.match(/class="gcnt"/g) || []).length >= 4, true);
check('it is one table, not stacked sections',
      (grouped.match(/class="jbt"/g) || []).length, 1);

// --- OPEN NOW rows carry the found posting, its location and its score ---
const sr = sandbox.soonRow({_row: 9, 'Company': 'Google', 'Programme': 'SWE Intern',
  'Status': 'OPEN NOW', 'Link': 'https://careers.google.com', 'Expected open date': '',
  'Notes': "Confirmed open on 2026-08-21: found 'SWE Intern' at https://g/1 " +
           "| loc London, UK | score 81 | seen 2026-09-01"}, 0);
check('an open-now row links the posting itself', sr.includes('href="https://g/1"'), true);
check('and keeps the careers page as the second button',
      sr.includes('href="https://careers.google.com"'), true);
check('and shows the found location', sr.includes('London, UK'), true);
check('and wears the posting score', />81</.test(sr), true);
check('a live watchlist row reads Not applied, like its bank neighbours',
      sr.includes('Not applied') && !sr.includes('Open now'), true);
// the pre-refresh note format must still yield its URL
const srOld = sandbox.soonRow({_row: 9, 'Company': 'G', 'Programme': 'P',
  'Status': 'OPEN NOW', 'Link': '', 'Expected open date': '',
  'Notes': "Confirmed open on 2026-08-21: found 'T' at https://old/1"}, 0);
check('the old one-line note still links its posting', srOld.includes('href="https://old/1"'), true);
const gr = sandbox.soonRow({_row: 9, 'Company': 'G', 'Programme': 'P',
  'Status': 'Posting gone (was open)', 'Link': 'https://careers.g', 'Expected open date': '',
  'Notes': "Not seen since 2026-08-25; the posting has probably closed. " +
           "Last known: 'T' at https://dead/1"}, 0);
check('a gone row hands back the careers page, not the dead link',
      gr.includes('href="https://careers.g"') && !gr.includes('cta" href="https://dead/1"'), true);
check('and says the posting is gone', gr.includes('Posting gone'), true);
check('a gone row is not live', gr.includes('Open now'), false);
// on a LIVE row the X means "not this posting", never "stop tracking Google":
// Aug X-ed the PhD posting and lost the whole Google tracker with it
check('a live row X-es the posting, not the programme',
      sr.includes('class="skipx"') && sr.includes('data-url="https://g/1"'), true);
check('stop-tracking survives as its own labelled button',
      sr.includes('soonstop') && sr.includes('Stop tracking'), true);
check('the old one-line note still gets the posting-level X',
      srOld.includes('class="skipx"'), true);
check('a not-yet-open row still X-es the tracker itself',
      sandbox.soonRow({_row: 9, 'Company': 'G', 'Programme': 'P',
        'Status': 'Due to open', 'Link': '', 'Expected open date': '',
        'Notes': ''}, 0).includes('class="soonx"'), true);
check('no watchlist X is ever the bank-removal killx', sr.includes('killx'), false);
// the server skip: posting rejected, programme back to the watch pile
const soonSheet = sandbox.SpreadsheetApp.getActiveSpreadsheet()
  .getSheetByName('Opening Soon');
if (soonSheet) {
  const skipRes = sandbox.skipOpeningSoonPosting(2,
    soonSheet._data[1][0], 'https://reject/1');
  check('skipping needs the company to match what the row says',
        sandbox.skipOpeningSoonPosting(2, 'Wrong Co', 'https://x').ok, false);
  check('a skip goes back to the watch pile', skipRes.ok &&
        soonSheet._data[1][soonSheet._data[0].indexOf('Status')], 'Due to open');
  check('and the note carries the skip list for the pipeline',
        String(soonSheet._data[1][soonSheet._data[0].indexOf('Notes')])
          .includes('| skip https://reject/1'), true);
  const again = sandbox.skipOpeningSoonPosting(2,
    soonSheet._data[1][0], 'https://reject/2');
  check('a second skip keeps the first URL too', again.ok &&
        String(soonSheet._data[1][soonSheet._data[0].indexOf('Notes')])
          .includes('https://reject/1') &&
        String(soonSheet._data[1][soonSheet._data[0].indexOf('Notes')])
          .includes('https://reject/2'), true);
}

// The page's inline client script must parse.
const page = grouped;
const script = page.slice(page.indexOf('<script>') + 8, page.lastIndexOf('</script>'));
try { new vm.Script(script); check('inline client script parses', true, true); }
catch (e) { check('inline client script parses: ' + e.message, false, true); }
check('client sends the link with tailoringPrompt',
      /tailoringPrompt\(Number\(b\.dataset\.row\),b\.dataset\.sheet\|\|"Job Bank",linkOf\(b\)\)/.test(script), true);

check('the page sends the link with toggleFavourite',
      /toggleFavourite\(Number\(b\.dataset\.row\),linkOf\(b\)\)/.test(script), true);

// --- descriptions that were never fetched ---
// TikTok-style rows arrive with a title and nothing else; the card should be
// able to go and get the text rather than reading "No description" forever.
const DESC = HEAD.indexOf('Description');
const noDesc = row('Anthropic', 'Research Intern');
noDesc[DESC] = '';
jobs._data.push(noDesc);
const dRow = jobs._data.length, dLink = noDesc[HEAD.indexOf('Link')];
sandbox.UrlFetchApp.fetch = () => ({
  getResponseCode: () => 200,
  getContentText: () => '<html><nav>Skip to content Careers Home</nav><body>' +
    '<p>We are looking for a research intern to work on interpretability, ' +
    'joining the team for twelve weeks over the summer of 2027 in London.</p></body></html>'
});
const got = sandbox.describeRole(dRow, 'Job Bank', dLink);
check('a missing description is fetched from the posting', got.ok, true);
check('the fetched text is real prose, not nav junk',
      got.text.startsWith('We are looking for a research intern'), true);
check('it is written back to the sheet so it is fetched once',
      jobs._data[dRow - 1][DESC].startsWith('We are looking for'), true);
check('a second open reuses the stored text',
      sandbox.describeRole(dRow, 'Job Bank', dLink).cached, true);

// a site that blocks the fetch must say so rather than silently doing nothing
const blocked = row('Dyson', 'Graduate Engineer');
blocked[DESC] = '';
jobs._data.push(blocked);
sandbox.UrlFetchApp.fetch = () => ({ getResponseCode: () => 403, getContentText: () => '' });
const bad = sandbox.describeRole(jobs._data.length, 'Job Bank', blocked[HEAD.indexOf('Link')]);
check('an unreadable posting reports why', bad.ok, false);
check('and does not blank the row', jobs._data[jobs._data.length - 1][DESC], '');

// --- the whole list is visible at once: no tabs, nothing pre-hidden ---
// Option B is ONE list. If a tbody rendered with display:none the group would
// silently vanish, which is exactly the failure tabs used to paper over.
check('the slice-tab strip is back, with All first',
      /class="btab all on" data-b="all"/.test(grouped), true);
check('a slice tab exists for every band', ['apply','watch','waiting','closed']
      .every(k => grouped.includes('data-b="' + k + '"')), true);
check('no group starts hidden',
      /<tbody[^>]*display:none/.test(grouped), false);

// --- one job, reposted per team, is still one job ---
// TikTok lists the same internship for every team it has; the titles genuinely
// differ, so dedupe cannot collapse them and 44 of the 50 More roles slots were
// TikTok reposting itself.
check('team names do not make a new role',
      sandbox.roleKey_('Backend Software Engineer Intern (TikTok-Search) - 2027 Summer'),
      sandbox.roleKey_('Software Engineer Intern - Global E-Commerce - 2027 Fall'));
check('ML and software are different families',
      sandbox.roleKey_('Machine Learning Engineer Intern - Search Ads') ===
      sandbox.roleKey_('Backend Software Engineer Intern'), false);
check('data science is its own family',
      sandbox.roleKey_('Data Science Intern - TikTok Product'), 'data scientist');
check('year and season are noise',
      sandbox.roleKey_('Software Engineer Intern 2027 Summer'),
      sandbox.roleKey_('Software Engineer Intern 2026 Winter'));
check('an unrecognised title still keys to itself',
      sandbox.roleKey_('Puppetry Intern 2027') === sandbox.roleKey_('Puppetry Intern 2026'), true);
check('and does not collide with a different unknown',
      sandbox.roleKey_('Puppetry Intern') === sandbox.roleKey_('Falconry Intern'), false);

const many = [];
for (let i = 0; i < 9; i++) many.push({ c: 'TikTok', r: `Software Engineer Intern (Team ${i}) - 2027` });
many.push({ c: 'Figma', r: 'Software Engineer Intern' });
const capped = sandbox.capSimilar_(many, 3, x => x.c, x => x.r);
check('nine repostings become three', capped.filter(x => x.c === 'TikTok').length, 3);
check('and another employer is untouched', capped.filter(x => x.c === 'Figma').length, 1);
check('the ones kept are the first (best-scoring) ones',
      capped[0].r.includes('Team 0'), true);

// The cap is per company AND role: different roles at one employer are different
// jobs, so 3 software engineer + 3 machine learning at TikTok is correct.
const mixed = [
  { c: 'TikTok', r: 'Backend Software Engineer Intern (Search) - 2027' },
  { c: 'TikTok', r: 'Software Engineer Intern (Ads) - 2027' },
  { c: 'TikTok', r: 'Fullstack Software Engineer Intern (E-Commerce) - 2027' },
  { c: 'TikTok', r: 'Software Engineer Intern (Media Engine) - 2027' },
  { c: 'TikTok', r: 'Machine Learning Engineer Intern - Search Ads' },
  { c: 'TikTok', r: 'Machine Learning Engineer Intern - Brand Ads' },
  { c: 'TikTok', r: 'Data Science Intern - TikTok Product' }];
const out = sandbox.capSimilar_(mixed, 3, x => x.c, x => x.r);
check('three software engineer roles survive',
      out.filter(x => sandbox.roleKey_(x.r) === 'software engineer').length, 3);
check('and the machine learning ones are counted separately',
      out.filter(x => sandbox.roleKey_(x.r) === 'ml engineer').length, 2);
check('as is data science', out.filter(x => sandbox.roleKey_(x.r) === 'data scientist').length, 1);
check('so one employer can legitimately show more than 3 in total', out.length, 6);

check('the same title punctuated differently is one posting',
      sandbox.capSimilar_([{ c: 'TikTok', r: 'Data Scientist Intern (VOD Data) - 2027 Summer' },
                           { c: 'TikTok', r: 'Data Scientist Intern - VOD Data - 2027 Summer' }],
                          3, x => x.c, x => x.r).length, 1);

// "Machine Learning Eningeer Intern - Brand Ads" was filed under marketing: the
// rule wanted the word "engineer" and the posting misspells it.
check('a misspelt engineer is still machine learning',
      sandbox.roleKey_('Machine Learning Eningeer Intern - Brand Ads - 2027 Summer'), 'ml engineer');
check('but AI product operations is not an ML engineer',
      sandbox.roleKey_('AI Product Operations Intern (Platform Responsibility)') === 'ml engineer', false);

// The choice must survive to the server call, and be hidden where it makes no
// sense (More roles rows live on the Archive and have nothing to replace).
check('the removal dialog offers the choice', grouped.includes('id="whyRep"'), true);
check('and the page sends whatever was chosen',
      /\.removeJob\(Number\(btn\.dataset\.row\),wantRep,reason,linkOf\(btn\)\)/.test(grouped), true);

// --- outreach links must constrain to what their label promises ---
// "Imperial alumni at Epic Games" opened the school's alumni page with a
// ?keywords= param that LinkedIn ignores on load, so it listed every Imperial
// alumnus regardless of employer.
const li = sandbox.linkedinLinks_('Epic Games', 'Software Engineer Intern');
const alumni = decodeURIComponent(li.linkedinAlumni);
check('the alumni link is a people search, not the school page',
      alumni.includes('/search/results/people/'), true);
check('it constrains to the company', alumni.includes('"Epic Games"'), true);
check('and to Imperial', alumni.includes('"Imperial College London"'), true);
check('the company is quoted so it cannot match other games companies',
      decodeURIComponent(li.linkedinEngineers).includes('"Epic Games"'), true);
check('recruiters are constrained too',
      decodeURIComponent(li.linkedinPeople).includes('"Epic Games"'), true);
// The point is that the embedded quote is gone, so it cannot close the search's
// own quoting early. "Co" is then dropped as a corporate suffix, same as
// "RRS Group & Co" -> "RRS" — this expected "Bobs Co" from before that rule
// existed, and never ran to say so.
check('a company name containing a quote cannot break the search',
      decodeURIComponent(sandbox.linkedinLinks_('Bob"s Co', 'Intern').linkedinAlumni)
        .includes('"Bobs"'), true);

// The name on a posting is rarely the name on a LinkedIn profile: quoting the
// legal name verbatim found nobody for 70 of the 469 companies on the sheet.
[['Snap Inc.', 'Snap'], ['Renault Group', 'Renault'], ['Sony / PlayStation', 'Sony'],
 ['Ormat Technologies, Inc.', 'Ormat'], ['Conifer Life Sciences Group, LLC', 'Conifer Life Sciences'],
 ['Doctors Without Borders/Médecins Sans Frontières - USA', 'Doctors Without Borders'],
 ['Epic Games', 'Epic Games'], ['Mercedes-AMG F1', 'Mercedes-AMG F1'], ['TikTok', 'TikTok']
].forEach(([given, want]) =>
  check(`search name: ${given.slice(0, 34)}`, sandbox.searchName_(given), want));
check('a name that is only a suffix is left alone', sandbox.searchName_('Co'), 'Co');
check('the normalised name is what gets searched',
      decodeURIComponent(sandbox.linkedinLinks_('Snap Inc.', 'Intern').linkedinAlumni)
        .includes('"Snap" "Imperial College London"'), true);

// --- the heart: pin a role to the FIRST ROWS of the sheet, not just the page ---
// Two rows of its own, at the bottom of the tab: the point of the test is that
// they end up at the top, so they must start as far from it as possible.
jobs._data.push(row('HeartOne', 'Intern'));
jobs._data.push(row('HeartTwo', 'Intern'));
const SECOND = jobs._data[jobs._data.length - 1][HEAD.indexOf('Link')];
const FIRST = jobs._data[jobs._data.length - 2][HEAD.indexOf('Link')];
check('the fixtures carry the links the test asserts on', !!(FIRST && SECOND), true);

const t1 = sandbox.toggleFavourite(jobs._data.length - 1, FIRST);
check('hearting reports the role as hearted', t1.favourite, true);
check('and puts it on the first row of the bank', t1.row, 2);
check('the sheet really moved it', jobs._data[1][HEAD.indexOf('Link')], FIRST);
const favCol = jobs._data[0].indexOf('Favourite');
check('a Favourite column was added to the tab', favCol >= 0, true);
// where JOB_BANK_COLS puts it, so the first heart does not leave a stray column
// out past the hidden tail until the next pipeline run tidies up
check('and it sits straight after Link', favCol, HEAD.indexOf('Link') + 1);
check('the heart is written into it', jobs._data[1][favCol] !== '', true);

sandbox.toggleFavourite(jobs._data.length, SECOND);
check('a second heart goes above the first', jobs._data[1][HEAD.indexOf('Link')], SECOND);
check('and the first stays directly under it', jobs._data[2][HEAD.indexOf('Link')], FIRST);

// Unhearting cannot put a role back where it came from — nothing records that —
// so it drops to the first row no other heart is holding.
const t3 = sandbox.toggleFavourite(2, SECOND);
check('unhearting clears the heart', t3.favourite, false);
check('and drops it under the roles still hearted', t3.row, 3);
check('the remaining heart holds the first row', jobs._data[1][HEAD.indexOf('Link')], FIRST);
check('the unhearted role sits right below it', jobs._data[2][HEAD.indexOf('Link')], SECOND);
check('with its Favourite cell emptied', String(jobs._data[2][favCol] || ''), '');

// A role only moves when its own heart is clicked: a stale row number plus the
// role's link must still act on that role.
check('a heart on a role that has gone reports staleness',
      sandbox.toggleFavourite(2, 'https://jobs.example/not-here').ok, false);

// Ordering: hearted beats higher-scoring, on the page as well as on the sheet.
// Read the header off the sheet, not off HEAD: adding Favourite shifted every
// column after Link one to the right.
const col = (name) => jobs._data[0].indexOf(name);
jobs._data[1][col('Score')] = '10';
jobs._data[1][col('Status')] = 'Applied';
jobs._data[2][col('Score')] = '99';
jobs._data[2][col('Status')] = 'Applied';
const withFav = sandbox.renderPage();
const waiting = tbodyOf(withFav, 'waiting');
check('the hearted role is the first card in its group',
      waiting.indexOf('data-link="' + FIRST + '"'), waiting.indexOf('data-link'));
check('and renders as hearted', withFav.includes('class="heart on"'), true);
check('an unhearted role still gets an empty heart to click',
      withFav.includes('class="heart"'), true);


// --- statistics: a stage that was walked back is not a stage that happened ---
// The real sequence off the live sheet: Applied, nudged to OA, put straight back
// to Applied the same day, removed the next day. The page reported "Tests 1" on
// a bank where no test had ever happened — checking the role's CURRENT status
// caught the walk-back only while the row still existed, and this row was gone.
const FEED_HEAD = ['Date', 'Signal', 'Company', 'Role', 'Category', 'Score',
                   'Reason', 'Link'];
const RLINK = 'https://fr.indeed.com/viewjob?jk=80e791637dc31407';
function feed(date, signal, reason, link, company) {
  const m = { Date: date, Signal: signal, Company: company || 'Renault Group',
              Role: 'Data Analyst', Reason: reason, Link: link };
  return FEED_HEAD.map(h => m[h] === undefined ? '' : m[h]);
}
sheets['Feedback'] = makeSheet('Feedback', [FEED_HEAD,
  feed('2026-08-17', 'pursued', 'status -> Applied', RLINK),
  feed('2026-08-18', 'pursued', 'status -> OA', RLINK),
  feed('2026-08-18', 'pursued', 'status -> Applied', RLINK),
  feed('2026-08-19', 'removed', 'Already applied or heard back', RLINK)]);

const st = sandbox.statsData(0);
check('a walked-back OA on a removed role is not a test', st.totals.oa, 0);
check('the application it really was still counts', st.totals.applied, 1);
check('and the removal is still recorded', st.totals.removed, 1);

// A role that genuinely progressed and was THEN removed must keep its history:
// it did happen, and forgetting it would undercount just as badly.
const GLINK = 'https://jobs.example/real-oa';
sheets['Feedback'] = makeSheet('Feedback', [FEED_HEAD,
  feed('2026-08-17', 'pursued', 'status -> Applied', GLINK, 'Real Co'),
  feed('2026-08-18', 'pursued', 'status -> OA', GLINK, 'Real Co'),
  feed('2026-08-19', 'removed', 'no longer interested', GLINK, 'Real Co')]);
const st2 = sandbox.statsData(0);
check('a real OA on a removed role is still a test', st2.totals.oa, 1);

// Reaching OA and moving ON to Interview is not a walk-back.
sheets['Feedback'] = makeSheet('Feedback', [FEED_HEAD,
  feed('2026-08-17', 'pursued', 'status -> Applied', GLINK, 'Real Co'),
  feed('2026-08-18', 'pursued', 'status -> OA', GLINK, 'Real Co'),
  feed('2026-08-19', 'pursued', 'status -> Interview', GLINK, 'Real Co')]);
const st3 = sandbox.statsData(0);
check('progressing past a test keeps the test', st3.totals.oa, 1);
check('and counts the interview', st3.totals.interview, 1);


console.log(fails ? '\n' + fails + ' FAILURE(S)' : '\nall checks passed');
process.exit(fails ? 1 : 0);
