/* Runs the dashboard page in a real DOM. The unit harness only proved the
   inline script PARSES; this proves it RUNS, which is where the tab regrouping
   and the two lazy loaders actually broke. */
const fs = require('fs'), { JSDOM } = require('jsdom');
const html = fs.readFileSync(process.argv[2], 'utf8');
const errors = [];
const dom = new JSDOM(html, { runScripts: 'dangerously', pretendToBeVisual: true,
  beforeParse(w) {
    w.addEventListener('error', e => errors.push('window error: ' + (e.error && e.error.stack || e.message)));
    const calls = [];
    // google.script.run hands back a FRESH chainable every time it is touched;
    // sharing one object let a later call site overwrite an earlier handler,
    // which made this harness lie about which callback it was invoking.
    const chain = () => {
      const o = new Proxy({}, { get: (t, k) => {
        // stored callbacks must read back as themselves, not as the recorder
        if (typeof k === 'string' && k[0] === '_') return t[k];
        if (k === 'withSuccessHandler' || k === 'withFailureHandler')
          return (fn) => { t['_' + k] = fn; return o; };
        if (k === 'then') return undefined;
        return (...args) => { calls.push({ fn: String(k), args, o }); return undefined; };
      }});
      return o;
    };
    w.google = { script: {} };
    Object.defineProperty(w.google.script, 'run', { get: chain });
    w.__calls = calls;
  }});
const w = dom.window;
setTimeout(() => {
  let fails = 0;
  const check = (label, got, want) => {
    const ok = got === want; if (!ok) fails++;
    console.log((ok ? 'PASS  ' : 'FAIL  ') + label + '  got=' + JSON.stringify(got) +
                (ok ? '' : ' want=' + JSON.stringify(want)));
  };
  check('the page script runs without throwing', errors.join(' | ') || 'none', 'none');
  if (errors.length) { errors.forEach(e => console.log('\n' + e)); process.exit(1); }

  const doc = w.document;
  // Option B is ONE grouped table: counts live in each band's header, every
  // group is on screen at once, and there are no tabs left to click.
  const count = k => Number((doc.querySelector(`#grid-${k} .gcnt`) || {})
    .textContent.replace(/[^0-9]/g, ''));
  console.log('bands:', ['apply','watch','waiting','closed']
    .map(k => k + '=' + count(k)).join(' '));
  // slice tabs: All is on by default and every band starts visible
  check('the All tab is on by default',
        doc.querySelector('.btab[data-b="all"]').classList.contains('on'), true);
  check('every band is visible at once',
        [...doc.querySelectorAll('tbody.bucket')]
          .every(b => b.style.display !== 'none'), true);
  // clicking a slice narrows the one table to that band; All restores it
  doc.querySelector('.btab[data-b="waiting"]').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  check('a slice tab hides the other bands',
        doc.getElementById('grid-apply').style.display, 'none');
  check('and keeps its own visible',
        doc.getElementById('grid-waiting').style.display, '');
  doc.querySelector('.btab[data-b="all"]').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  check('All brings every band back',
        [...doc.querySelectorAll('tbody.bucket')]
          .every(b => b.style.display !== 'none'), true);
  check('it is one single table',
        doc.querySelectorAll('#list table.jbt').length, 1);

  // the reported bug: change a status and the card should move tabs
  const card = doc.querySelector('#grid-apply .shell');
  const co = card.querySelector('.co').textContent.trim();
  const before = { apply: count('apply'), waiting: count('waiting') };
  check('rows are table rows, not cards', card.tagName, 'TR');
  const sel = card.querySelector('.sel select');
  sel.value = 'Applied';
  sel.dispatchEvent(new w.Event('change', { bubbles: true }));
  const call = w.__calls.find(c => c.fn === 'setStatus');
  check('the status change reaches the server', !!call, true);
  if (call) call.o._withSuccessHandler({ ok: true, value: 'Applied' });
  check(`${co} moved into the Applied list`,
        !!doc.querySelector('#grid-waiting .shell'), true);
  check('it left the Act now band', doc.querySelectorAll('#grid-apply .shell').length,
        before.apply - 1);
  check('the Act now count went down', count('apply'), before.apply - 1);
  check('the In flight count went up', count('waiting'), before.waiting + 1);
  // the stat cards were removed in the design pass: they repeated these very
  // counts directly above the tabs, so the tabs are now the only place they live
  check('no stat card duplicates the tab counts', doc.getElementById('statOpen'), null);

  // --- the heart moves the row before the sheet has answered ---
  const grid = doc.getElementById('grid-waiting');
  const last = grid.querySelectorAll('.shell')[grid.querySelectorAll('.shell').length - 1];
  check('there is more than one card to reorder', grid.querySelectorAll('.shell').length > 1, true);
  const heart = last.querySelector('.heart');
  check('every row carries a heart', !!heart, true);
  check('the heart lives in the expansion actions',
        !!heart.closest('.det .actions'), true);
  heart.dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  check('the row jumps to the front of its band, under the header',
        grid.querySelectorAll('.shell')[0], last);
  check('and reads as hearted', last.classList.contains('is-fav'), true);
  check('the heart is pressed', heart.getAttribute('aria-pressed'), 'true');
  const fav = w.__calls.find(c => c.fn === 'toggleFavourite');
  check('the heart reaches the server', !!fav, true);
  check('with the row and the link', fav && fav.args.length, 2);
  // clicking a heart must not also expand the card underneath it
  check('the card did not expand', last.classList.contains('expanded'), false);

  // a refused write puts the card and the heart back where they were
  if (fav) fav.o._withFailureHandler(new Error('nope'));
  check('a failed write un-hearts it', last.classList.contains('is-fav'), false);
  check('and the heart is no longer pressed', heart.getAttribute('aria-pressed'), 'false');
  check('and the row goes back to where it was',
        [...grid.querySelectorAll('.shell')].pop(), last);

  // --- the list view (cards retired 2026-09-01) ---
  // The dense list is the ONLY view now: no toggle to wire, so what can break
  // is a grid rendering without the rows class and quietly reverting to cards.
  check('the view switch is gone', doc.getElementById('viewtog'), null);
  check('the bank renders as one dense table',
        !!doc.querySelector('#list table.jbt'), true);
  // a row still carries everything the card did — the filters, heart and status
  // dropdown all read these same nodes
  const row = doc.querySelector('#list .shell');
  check('a row shows company over role, option C style',
        row.querySelector('.co') && !!row.querySelector('.prog'), true);
  check('a row still has its status dropdown', !!row.querySelector('.sel select'), true);
  check('a row still has its heart', !!row.querySelector('.heart'), true);
  check('a row still carries its link for the server calls', !!row.dataset.link, true);

  // --- filter dropdowns build the moment they open ---
  // Nothing populates them on page load any more, so an open that doesn't
  // build renders an empty panel — which read as "initial lag".
  const catDrop = doc.querySelector('.fdrop[data-kind="cat"][data-target="list"]');
  catDrop.querySelector('.fbtn').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  check('opening Role type fills the panel',
        catDrop.querySelectorAll('.froot .frow, .froot label, .froot .fempty, .froot *').length > 0, true);
  catDrop.querySelector('.fbtn').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));

  // --- drag a row to reorder it inside its band ---
  const dGrid = doc.getElementById('grid-waiting');
  const dRows = [...dGrid.querySelectorAll('tr.shell')];
  if (dRows.length > 1) {
    const a = dRows[0], b2 = dRows[1];
    a.dispatchEvent(new w.Event('dragstart', { bubbles: true }));
    check('dragging marks the row', a.classList.contains('dragging'), true);
    // jsdom rects are all zeros, so "after the target" is the branch taken
    b2.dispatchEvent(new w.Event('dragover', { bubbles: true }));
    a.dispatchEvent(new w.Event('dragend', { bubbles: true }));
    check('the row moved below its neighbour',
          [...dGrid.querySelectorAll('tr.shell')][0], b2);
    const mv = w.__calls.find(c => c.fn === 'moveJobRow');
    check('the new order reaches the sheet', !!mv, true);
    check('with the neighbour link for stale-row safety',
          !!(mv && (mv.args[3] || mv.args[5])), true);
    if (mv) mv.o._withSuccessHandler({ ok: true, row: 9 });
    check('the dragged row takes its confirmed sheet row', a.dataset.row, '9');
  }
  // watchlist rows must NOT drag — their order is the tracker's business
  const wRow = doc.querySelector('#grid-watch tr.shell');
  if (wRow) {
    wRow.dispatchEvent(new w.Event('dragstart', { bubbles: true }));
    check('watchlist rows refuse the drag', wRow.classList.contains('dragging'), false);
  }

  // --- the Watchlist band folds Opening Soon into the same table ---
  const wband = doc.getElementById('grid-watch');
  check('the Watchlist band is in the table', !!wband, true);
  check('it sits second, between Act now and In flight',
        [...doc.querySelectorAll('tbody.bucket')].indexOf(wband), 1);
  check('its band header names it',
        wband.querySelector('tr.grp td').textContent.includes('Watchlist'), true);

  // --- the search box must actually filter ---
  // Guarding a real regression: a second function once shadowed applyView()
  // and search silently stopped filtering. Nothing here noticed, because
  // nothing here typed in the search box.
  const wgrid = doc.getElementById('grid-waiting');
  const shells = [...wgrid.querySelectorAll('.shell')];
  const firstCo = shells[0].querySelector('.co').textContent.trim();
  const sbox = doc.querySelector('.searchbox[data-target="list"]');
  sbox.value = firstCo;
  sbox.dispatchEvent(new w.Event('input', { bubbles: true }));
  const shown = shells.filter(s => s.style.display !== 'none');
  check('searching narrows the list', shown.length < shells.length || shells.length === 1, true);
  check('and keeps the role that matches',
        shown.some(s => s.querySelector('.co').textContent.trim() === firstCo), true);
  sbox.value = '';
  sbox.dispatchEvent(new w.Event('input', { bubbles: true }));
  check('clearing the box brings everything back',
        shells.every(s => s.style.display !== 'none'), true);

  // a search hides rows, never band headers — the group labels always stand
  sbox.value = 'zzzznotarole';
  sbox.dispatchEvent(new w.Event('input', { bubbles: true }));
  check('a fruitless search leaves the band headers standing',
        [...doc.querySelectorAll('#list tr.grp')]
          .every(g => g.style.display !== 'none'), true);
  sbox.value = '';
  sbox.dispatchEvent(new w.Event('input', { bubbles: true }));

  // the two lazy pages must actually ask the server for their content
  doc.querySelector('.pagetab[data-page="more"]').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  const moreCall = w.__calls.find(c => c.fn === 'moreRoles');
  check('More roles asks the server', !!moreCall, true);

  // --- the list view reaches More roles too ---
  // That page replaces its whole filter bar on load (mf.innerHTML = res.filters)
  // and setListView runs after; the grid must come back in rows, not cards.
  if (moreCall) moreCall.o._withSuccessHandler({
    ok: true,
    html: '<div class="jbtwrap"><table class="jbt"><tbody class="bucket">' +
          '<tr class="shell card r" data-cat="SWE" data-row="2" data-link="https://x.test/a">' +
          '<td class="railc"></td><td class="carc"></td>' +
          '<td class="rolec"><span class="co">A Co</span><span class="prog">Intern</span></td>' +
          '<td class="statc"></td><td class="numc">&mdash;</td>' +
          '<td class="scorec"><span class="score">81</span></td>' +
          '</tr></tbody></table></div>',
    // empty rather than a hand-rolled .fdrop: buildFilters walks real filter
    // markup and a half-built stub crashes it, which says nothing about the view
    filters: ''
  });
  const more = doc.getElementById('moreList');
  check('More roles renders as the same dense table',
        !!more.querySelector('table.jbt'), true);
  check('and no view switch came back with the new filter bar',
        doc.querySelector('#moreFilters .viewtog'), null);
  check('its rows are table rows like the bank rows',
        more.querySelector('.shell') && more.querySelector('.shell').tagName, 'TR');
  // the Opening soon page is gone; tracking lives on the Find roles form now
  check('the Opening soon page tab is gone',
        doc.querySelector('.pagetab[data-page="soon"]'), null);
  doc.querySelector('.pagetab[data-page="find"]').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  const orSoon = doc.getElementById('orSoon');
  check('Find roles has the not-open-yet checkbox', !!orSoon, true);
  check('the expected-open date starts hidden',
        doc.getElementById('orOpenWrap').style.display, 'none');
  orSoon.checked = true;
  orSoon.dispatchEvent(new w.Event('change', { bubbles: true }));
  check('ticking it reveals the expected-open date',
        doc.getElementById('orOpenWrap').style.display, '');
  check('and relabels the button', doc.getElementById('orGo').textContent, 'Track it');
  doc.getElementById('orCompany').value = 'ESA';
  doc.getElementById('orRole').value = 'Graduate Trainee 2027';
  doc.getElementById('orGo').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  const soonAdd = w.__calls.find(c => c.fn === 'addOpeningSoon');
  check('the tracked add goes to Opening Soon, not the bank', !!soonAdd, true);
  check('as a programme named after the role field',
        soonAdd && soonAdd.args[0].programme, 'Graduate Trainee 2027');

  console.log(fails ? `\n${fails} FAILURE(S)` : '\nall DOM checks passed');
  process.exit(fails ? 1 : 0);
}, 400);
