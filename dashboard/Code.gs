/**
 * Job Bank dashboard — a web page served from the spreadsheet itself.
 *
 * Writes: setStatus() updates a row's Status; removeJob() DELETES the row so
 * everything below shifts up (no gaps), copies it to Archive for the record, and
 * optionally pulls the best archived role in to replace it.
 *
 * Deleting from the sheet is safe: the pipeline's dedupe also remembers every job
 * in data/state.db, so a removed role is not re-added on the next run.
 */

var SHEET_JOBS = 'Job Bank';
var SHEET_SOON = 'Opening Soon';
var SHEET_ARCHIVE = 'Archive';
var SHEET_WATCH = 'Watchlist';
var STATUSES = ['Not applied', 'Applied', 'OA', 'Interview', 'Offer', 'Rejected'];

// Shown in the footer. When something looks wrong, this answers the first
// question — is the browser even running the build we think it is?
var BUILD_ = '2026-08-25a';

function doGet() {
  return HtmlService.createHtmlOutput(renderPage())
    .setTitle('Job Bank')
    .addMetaTag('viewport', 'width=device-width, initial-scale=1, viewport-fit=cover');
}

/* ------------------------------------------------------------------ data --- */

function sheetOf(name) {
  return SpreadsheetApp.getActiveSpreadsheet().getSheetByName(name);
}

// Each of these is a round trip to Google, and one removal used to ask four
// separate times for the same header row. Memoised per execution — a single
// google.script.run call cannot outlive a schema change.
var HEADER_CACHE_ = {};

function headerOf(sh) {
  var key = sh.getName();
  if (!HEADER_CACHE_[key]) {
    HEADER_CACHE_[key] = sh.getRange(1, 1, 1, Math.max(sh.getLastColumn(), 1))
                           .getDisplayValues()[0];
  }
  return HEADER_CACHE_[key];
}

function rowsOf(name) {
  var sh = sheetOf(name);
  if (!sh || sh.getLastRow() < 2) return [];
  var values = sh.getDataRange().getDisplayValues();
  var head = values[0];
  var out = [];
  for (var i = 1; i < values.length; i++) {
    if (values[i].join('').trim() === '') continue;
    var o = { _row: i + 1 };
    for (var c = 0; c < head.length; c++) o[head[c]] = values[i][c];
    out.push(o);
  }
  return out;
}

function colIndex(sh, header) {
  return headerOf(sh).indexOf(header) + 1;   // 1-based; 0 if absent
}

var STALE_ROW_ = 'That role has moved — refresh the page';

/**
 * Row numbers go stale, and a stale row number is worse than a broken button:
 * removeJob() deletes a row and every row below it shifts up one, while the open
 * page keeps the numbers it was rendered with. Every card below the removed one
 * then points at its neighbour, so the prompt button on one role silently
 * returns a DIFFERENT role's prompt. The nightly pipeline rewriting the sheet
 * under a page left open does the same thing.
 *
 * The Link is stable, so every client call now carries it and the row is
 * verified before anything is read or written — and re-found if it moved.
 * Returns 0 when the role is no longer on the sheet at all.
 */
function resolveRow_(sh, rowNumber, link) {
  var last = sh.getLastRow();
  var inRange = rowNumber >= 2 && rowNumber <= last;
  var want = String(link || '').trim();
  // a page cached from before this fix sends no link: nothing to verify against
  if (!want) return inRange ? rowNumber : 0;
  var col = colIndex(sh, 'Link');
  if (!col) return inRange ? rowNumber : 0;
  if (inRange &&
      String(sh.getRange(rowNumber, col).getDisplayValue()).trim() === want) {
    return rowNumber;
  }
  var links = sh.getRange(2, col, Math.max(last - 1, 1), 1).getDisplayValues();
  for (var i = 0; i < links.length; i++) {
    if (String(links[i][0]).trim() === want) return i + 2;
  }
  return 0;
}

/* --------------------------------------------------------------- actions --- */

function setStatus(rowNumber, value, link) {
  if (STATUSES.indexOf(value) < 0) return { ok: false, error: 'Unknown status' };
  var sh = sheetOf(SHEET_JOBS);
  var col = colIndex(sh, 'Status');
  if (!col) return { ok: false, error: 'No Status column' };
  var row = resolveRow_(sh, rowNumber, link);
  if (!row) return { ok: false, error: STALE_ROW_ };
  sh.getRange(row, col).setValue(value);

  // Moving a role forward is the strongest signal there is, and it costs no
  // typing — without it the scorer only ever learns what gets rejected.
  if (['Applied', 'OA', 'Interview', 'Offer'].indexOf(value) >= 0) {
    var head = headerOf(sh);
    var vals = sh.getRange(row, 1, 1, head.length).getDisplayValues()[0];
    var job = {};
    head.forEach(function (h, i) { job[h] = vals[i]; });
    logFeedback_('pursued', job, 'status -> ' + value);
  }
  return { ok: true, value: value };
}

/* ------------------------------------------------------------ favourites --- */
/**
 * The heart on a card. Hearting a role writes into a Favourite column AND moves
 * its ROW to the top of the Job Bank — "the first rows of the job bank" was the
 * ask, so the spreadsheet itself reorders, not just this page.
 *
 * Why a column as well as a position: the pipeline appends below and rows get
 * dragged about by hand, so position alone cannot say which roles were chosen
 * on purpose. The column is listed in JOB_BANK_COLS (src/jobbank/sheets.py) —
 * ensure_schema() clears any column on the tab it does not know about, so a
 * heart parked in a column Python has never heard of would be wiped by the next
 * run.
 *
 * Unhearting drops the row to just under the last hearted one rather than back
 * to wherever it came from: the old position is recorded nowhere, and guessing
 * at it would shuffle rows the click never mentioned.
 */
var FAV_COL_ = 'Favourite';
var FAV_MARK_ = '♥';

function isFavourite_(j) {
  return String(j[FAV_COL_] || '').trim() !== '';
}

/**
 * The Favourite column, added to the tab the first time a heart is clicked.
 * Inserted straight after Link, where JOB_BANK_COLS puts it, rather than tacked
 * on past the hidden tail — otherwise the first heart leaves a stray column on
 * the far right until the next pipeline run reorders the tab. Sheets carries
 * the colour rules and dropdowns across the shift itself.
 */
function favColumn_(sh) {
  var col = colIndex(sh, FAV_COL_);
  if (col) return col;
  var after = colIndex(sh, 'Link');
  if (after) {
    sh.insertColumnAfter(after);
    col = after + 1;
  } else {
    col = sh.getLastColumn() + 1;
  }
  sh.getRange(1, col).setValue(FAV_COL_);
  delete HEADER_CACHE_[sh.getName()];   // the header just changed under it
  return col;
}

/**
 * Move one row by rotating the values of the block between where it is and
 * where it is going. Deliberately NOT deleteRow + insertRow: the colour rules
 * and the Status dropdown belong to the POSITION, so a re-inserted row lands in
 * a cell with no validation, and a failure between those two calls would lose
 * the role outright. Rotating values leaves every cell's formatting alone.
 */
/**
 * Drag-to-reorder lands here: put the dragged role just above `before`, or
 * failing that just below `after`. Everything resolves through resolveRow_
 * because a drop's row numbers can be stale the moment another row was
 * removed — the Link is what actually identifies each role.
 */
function moveJobRow(rowNumber, link, beforeRow, beforeLink, afterRow, afterLink) {
  var sh = sheetOf(SHEET_JOBS);
  if (!sh) return { ok: false, error: 'No Job Bank sheet' };
  var row = resolveRow_(sh, rowNumber, link);
  if (!row) return { ok: false, error: STALE_ROW_ };
  var target;
  if (beforeRow || beforeLink) {
    var b = resolveRow_(sh, beforeRow, beforeLink);
    if (!b) return { ok: false, error: STALE_ROW_ };
    target = row < b ? b - 1 : b;
  } else {
    var a = resolveRow_(sh, afterRow, afterLink);
    if (!a) return { ok: false, error: STALE_ROW_ };
    target = row < a ? a : a + 1;
  }
  return { ok: true, row: moveRowTo_(sh, row, target) };
}

function moveRowTo_(sh, from, to) {
  if (!to || to === from) return from;
  var width = sh.getLastColumn();
  var top = Math.min(from, to), bottom = Math.max(from, to);
  var block = sh.getRange(top, 1, bottom - top + 1, width).getValues();
  block.splice(to - top, 0, block.splice(from - top, 1)[0]);
  sh.getRange(top, 1, block.length, width).setValues(block);
  return to;
}

/**
 * Toggle the heart. Returns the row the role ended up on; every OTHER row
 * between the old and new position shifted by one, but each client call carries
 * the role's Link and resolveRow_ re-finds it, so the page does not have to be
 * re-rendered to stay correct.
 */
function toggleFavourite(rowNumber, link) {
  var sh = sheetOf(SHEET_JOBS);
  if (!sh) return { ok: false, error: 'No Job Bank sheet' };
  var row = resolveRow_(sh, rowNumber, link);
  if (!row) return { ok: false, error: STALE_ROW_ };
  var col = favColumn_(sh);
  var on = String(sh.getRange(row, col).getDisplayValue()).trim() === '';
  sh.getRange(row, col).setValue(on ? FAV_MARK_ : '');

  var target = 2;   // hearted: straight to the top, above the earlier hearts
  if (!on) {
    // unhearted: the first row that is not spoken for by another heart
    var last = sh.getLastRow();
    var marks = last > 1 ? sh.getRange(2, col, last - 1, 1).getDisplayValues() : [];
    var others = 0;
    for (var i = 0; i < marks.length; i++) {
      if (i + 2 !== row && String(marks[i][0]).trim() !== '') others++;
    }
    target = others + 2;
  }
  return { ok: true, favourite: on, row: moveRowTo_(sh, row, target) };
}

var SHEET_FEEDBACK = 'Feedback';
var FEEDBACK_COLS_ = ['Date', 'Signal', 'Company', 'Role', 'Category', 'Score',
                      'Reason', 'Link'];

/**
 * Record why a role was removed, or that one was pursued.
 *
 * Read back into the scorer as CONTEXT — it nudges how future roles are judged,
 * it does not silently create rules. Hard rules stay in filters*.py where they
 * can be read and edited. Never throws: losing a note must not block a removal.
 */
function logFeedback_(signal, job, reason) {
  try {
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var sh = ss.getSheetByName(SHEET_FEEDBACK);
    if (!sh) {
      sh = ss.insertSheet(SHEET_FEEDBACK);
      sh.getRange(1, 1, 1, FEEDBACK_COLS_.length).setValues([FEEDBACK_COLS_]);
      sh.hideSheet();
    }
    appendRowSafely_(sh, [
      Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'yyyy-MM-dd'),
      signal, job['Company'] || '', job['Role'] || '', job['Category'] || '',
      job['Score'] || '', String(reason || '').slice(0, 300), job['Link'] || ''
    ]);
  } catch (err) {
    // best effort by design
  }
}

/**
 * Delete a row outright. Rows below shift up, so the sheet never grows holes.
 * The role is copied to Archive first so the record survives.
 */
function removeJob(rowNumber, wantReplacement, reason, link) {
  var sh = sheetOf(SHEET_JOBS);
  var row = resolveRow_(sh, rowNumber, link);
  if (!row) return { ok: false, error: STALE_ROW_ };
  var head = headerOf(sh);
  var values = sh.getRange(row, 1, 1, head.length).getDisplayValues()[0];
  var job = {};
  head.forEach(function (h, i) { job[h] = values[i]; });

  // one pass over the bank: the row being removed is excluded from the count
  var openLeft = 0;
  rowsOf(SHEET_JOBS).forEach(function (j) {
    if (j._row !== row && ACTIONED_.indexOf(String(j['Status'] || '').trim()) < 0) {
      openLeft++;
    }
  });

  logFeedback_('removed', job, reason);
  archive_(job, 'removed from dashboard');
  sh.deleteRow(row);

  // wantReplacement is now an explicit choice in the removal dialog, so it is
  // obeyed as asked rather than second-guessed against the refill floor — that
  // floor exists to stop the PIPELINE topping the bank up, not to overrule you.
  // The ceiling still holds: replacing one role cannot take the bank past it.
  var replacement = null;
  if (wantReplacement && openLeft < BANK_CEILING) {
    try {
      replacement = promoteFromArchive_();
    } catch (err) {
      replacement = null;   // the removal already succeeded; don't fail the call
    }
  }
  return {
    ok: true,
    removed: job['Company'],
    // the row actually deleted, so the page can shift the cards below it down
    // one and stay in step with the sheet without a full re-render
    row: row,
    replacement: replacement,
    // rendered here so the page can swap it in without re-rendering everything
    replacementCard: replacement ? card(replacement, 0) : '',
    replacementScore: replacement ? (Number(replacement['Score']) || 0) : 0
  };
}

function archive_(job, disposition) {
  var arch = sheetOf(SHEET_ARCHIVE);
  if (!arch) return;
  var head = headerOf(arch);
  var map = {
    'Added': Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'yyyy-MM-dd'),
    'Score': job['Score'], 'Category': job['Category'], 'Company': job['Company'],
    'Role': job['Role'], 'Location': job['Location'], 'Deadline': job['Deadline'],
    'Link': job['Link'], 'Source': job['Source'], 'Disposition': disposition,
    'Description': job['Description'] || '', 'Duration': job['Duration'] || ''
  };
  appendRowSafely_(arch, head.map(function (h) {
    return map.hasOwnProperty(h) ? map[h] : '';
  }));
}

/** appendRow() can leave a gap when trailing cells hold empty strings. */
function appendRowSafely_(sh, row) {
  var target = sh.getLastRow() + 1;
  sh.getRange(target, 1, 1, row.length).setValues([row]);
  return target;
}

/**
 * Recent slice of the Archive. Reading the whole tab is not viable — it holds
 * thousands of rows (every capped/below-threshold role ever seen) and pulling all
 * of it on each click is what produced "Error code INTERNAL". Only the newest
 * rows matter anyway: older ones are from expired cycles.
 */
var ARCHIVE_SCAN = 800;

function recentArchive_() {
  var arch = sheetOf(SHEET_ARCHIVE);
  if (!arch) return [];
  var last = arch.getLastRow();
  if (last < 2) return [];
  var head = headerOf(arch);
  var start = Math.max(2, last - ARCHIVE_SCAN + 1);
  var values = arch.getRange(start, 1, last - start + 1, head.length).getDisplayValues();
  var out = [];
  for (var i = 0; i < values.length; i++) {
    if (values[i].join('').trim() === '') continue;
    var o = { _row: start + i };
    for (var c = 0; c < head.length; c++) o[head[c]] = values[i][c];
    out.push(o);
  }
  return out;
}

/* ------------------------------------------------------- enrich on entry --- */
/**
 * Archive rows can be old — some predate the pipeline's location normalisation,
 * and none carry Opens/Duration. Rather than rewrite thousands of archived rows,
 * a role is cleaned up at the moment it ENTERS the Job Bank, so every row in the
 * bank has the same shape. Mirrors src/jobbank/enrich.py.
 */
var COUNTRY = {
  'uk': 'UK', 'u.k.': 'UK', 'united kingdom': 'UK', 'gb': 'UK', 'gbr': 'UK',
  'great britain': 'UK', 'england': 'UK', 'scotland': 'UK', 'wales': 'UK',
  'northern ireland': 'UK', 'france': 'France', 'fr': 'France',
  'germany': 'Germany', 'de': 'Germany', 'netherlands': 'Netherlands',
  'nl': 'Netherlands', 'spain': 'Spain', 'es': 'Spain', 'italy': 'Italy',
  'it': 'Italy', 'ireland': 'Ireland', 'ie': 'Ireland',
  'switzerland': 'Switzerland', 'belgium': 'Belgium', 'portugal': 'Portugal',
  'poland': 'Poland', 'sweden': 'Sweden', 'denmark': 'Denmark',
  'austria': 'Austria', 'serbia': 'Serbia', 'usa': 'USA', 'us': 'USA',
  'u.s.': 'USA', 'united states': 'USA', 'united states of america': 'USA',
  'canada': 'Canada', 'india': 'India', 'singapore': 'Singapore',
  'australia': 'Australia', 'israel': 'Israel'
};
// A superset of _CITY_COUNTRY in src/jobbank/enrich.py: the two agree on
// everything Python knows, and this adds the places the filter has to group by
// but the pipeline never has to normalise (chiefly US and engineering sites).
var CITY_COUNTRY = {
  'london': 'UK', 'cambridge': 'UK', 'oxford': 'UK', 'manchester': 'UK',
  'edinburgh': 'UK', 'glasgow': 'UK', 'bristol': 'UK', 'leeds': 'UK',
  'birmingham': 'UK', 'reading': 'UK', 'banbury': 'UK', 'brackley': 'UK',
  'silverstone': 'UK', 'grove': 'UK', 'enstone': 'UK', 'woking': 'UK',
  'milton keynes': 'UK', 'basildon': 'UK', 'harwell': 'UK', 'holborn': 'UK',
  'sheffield': 'UK', 'brighton': 'UK', 'bicester': 'UK', 'northampton': 'UK',
  'guildford': 'UK', 'derby': 'UK', 'coventry': 'UK', 'warwick': 'UK',
  'gaydon': 'UK', 'whitley': 'UK', 'solihull': 'UK', 'filton': 'UK',
  'brough': 'UK', 'barrow': 'UK', 'preston': 'UK', 'warton': 'UK',
  'samlesbury': 'UK', 'farnborough': 'UK', 'stevenage': 'UK', 'crawley': 'UK',
  'bracknell': 'UK', 'basingstoke': 'UK', 'slough': 'UK', 'cheltenham': 'UK',
  'gloucester': 'UK', 'portsmouth': 'UK', 'southampton': 'UK', 'yeovil': 'UK',
  'bridgend': 'UK', 'newcastle': 'UK', 'liverpool': 'UK', 'nottingham': 'UK',
  'leicester': 'UK', 'swindon': 'UK', 'cardiff': 'UK', 'belfast': 'UK',
  'aberdeen': 'UK', 'york': 'UK', 'bath': 'UK', 'exeter': 'UK',
  'plymouth': 'UK', 'goodwood': 'UK', 'norwich': 'UK', 'luton': 'UK',
  'cranfield': 'UK', 'rotherham': 'UK', 'hucknall': 'UK', 'annesley': 'UK',
  'paris': 'France', 'lyon': 'France', 'toulouse': 'France', 'bordeaux': 'France',
  'lille': 'France', 'nantes': 'France', 'grenoble': 'France',
  'marseille': 'France', 'nice': 'France', 'rennes': 'France',
  'sophia antipolis': 'France', 'viry-chatillon': 'France',
  'berlin': 'Germany', 'munich': 'Germany', 'münchen': 'Germany',
  'hamburg': 'Germany', 'frankfurt': 'Germany', 'stuttgart': 'Germany',
  'cologne': 'Germany', 'düsseldorf': 'Germany', 'hinwil': 'Switzerland',
  'amsterdam': 'Netherlands', 'eindhoven': 'Netherlands', 'delft': 'Netherlands',
  'rotterdam': 'Netherlands', 'noordwijk': 'Netherlands', 'the hague': 'Netherlands',
  'dublin': 'Ireland', 'cork': 'Ireland', 'madrid': 'Spain', 'barcelona': 'Spain',
  'valencia': 'Spain', 'seville': 'Spain', 'bilbao': 'Spain',
  'milan': 'Italy', 'rome': 'Italy', 'turin': 'Italy', 'bologna': 'Italy',
  'modena': 'Italy', 'maranello': 'Italy', 'faenza': 'Italy',
  'zurich': 'Switzerland', 'geneva': 'Switzerland', 'lausanne': 'Switzerland',
  'basel': 'Switzerland', 'bern': 'Switzerland',
  'brussels': 'Belgium', 'leuven': 'Belgium', 'lisbon': 'Portugal',
  'porto': 'Portugal', 'stockholm': 'Sweden', 'gothenburg': 'Sweden',
  'copenhagen': 'Denmark', 'oslo': 'Norway', 'helsinki': 'Finland',
  'warsaw': 'Poland', 'krakow': 'Poland', 'prague': 'Czechia',
  'vienna': 'Austria', 'budapest': 'Hungary', 'belgrade': 'Serbia',
  'luxembourg': 'Luxembourg', 'athens': 'Greece',
  'boston': 'USA', 'new york': 'USA', 'seattle': 'USA', 'san francisco': 'USA',
  'austin': 'USA', 'chicago': 'USA', 'san jose': 'USA', 'palo alto': 'USA',
  'mountain view': 'USA', 'sunnyvale': 'USA', 'santa clara': 'USA',
  'cupertino': 'USA', 'menlo park': 'USA', 'redmond': 'USA', 'bellevue': 'USA',
  'los angeles': 'USA', 'san diego': 'USA', 'denver': 'USA', 'atlanta': 'USA',
  'dallas': 'USA', 'houston': 'USA', 'philadelphia': 'USA', 'pittsburgh': 'USA',
  'detroit': 'USA', 'phoenix': 'USA', 'portland': 'USA', 'miami': 'USA',
  'washington': 'USA', 'cambridge ma': 'USA', 'ann arbor': 'USA',
  'toronto': 'Canada', 'vancouver': 'Canada', 'montreal': 'Canada',
  'ottawa': 'Canada', 'waterloo': 'Canada',
  'tokyo': 'Japan', 'osaka': 'Japan', 'seoul': 'South Korea',
  'shanghai': 'China', 'beijing': 'China', 'shenzhen': 'China',
  'hong kong': 'Hong Kong', 'taipei': 'Taiwan',
  'bangalore': 'India', 'bengaluru': 'India', 'hyderabad': 'India',
  'pune': 'India', 'mumbai': 'India', 'chennai': 'India', 'delhi': 'India',
  'sydney': 'Australia', 'melbourne': 'Australia', 'tel aviv': 'Israel',
  'haifa': 'Israel', 'dubai': 'UAE', 'abu dhabi': 'UAE'
};
var US_STATES = ('al ak az ar ca co ct de fl ga hi id il in ia ks ky la me md ma mi mn ms ' +
  'mo mt ne nv nh nj nm ny nc nd oh ok or pa ri sc sd tn tx ut vt va wa wv wi wy dc ' +
  'california texas washington massachusetts illinois colorado florida virginia oregon').split(' ');
var CA_PROV = ['on', 'bc', 'qc', 'ab', 'ns', 'mb', 'sk', 'ontario', 'quebec'];

function normalizeLocation(raw) {
  if (!raw) return '';
  var text = String(raw).replace(/\b(undefined|null|n\/?a)\b/gi, ' ');
  var remote = /\b(remote|anywhere|work from home)\b/i.test(text);
  var parts = text.split(/[,\/|]|\s+[-–—]\s+/)
    .map(function (p) { return p.replace(/^[\s\-–—]+|[\s\-–—]+$/g, ''); })
    .filter(function (p) { return p && !/^(remote|hybrid)$/i.test(p); });

  var country = '';
  for (var i = parts.length - 1; i >= 0; i--) {
    var k = parts[i].toLowerCase().replace(/[.\s]+$/, '');
    if (COUNTRY[k]) { country = COUNTRY[k]; parts = parts.slice(0, i); break; }
    if (US_STATES.indexOf(k) >= 0) { country = 'USA'; parts = parts.slice(0, i); break; }
    if (CA_PROV.indexOf(k) >= 0) { country = 'Canada'; parts = parts.slice(0, i); break; }
  }
  var city = '';
  for (var n = 0; n < parts.length; n++) {
    var key = parts[n].toLowerCase().replace(/[.\s]+$/, '');
    if (COUNTRY[key]) { if (!country) country = COUNTRY[key]; continue; }
    if (US_STATES.indexOf(key) >= 0 || CA_PROV.indexOf(key) >= 0) continue;
    if (/^(?=.*\d)(?=.*[A-Za-z])[A-Za-z0-9 ]{4,9}$/.test(parts[n])) continue;  // postcode
    if (parts[n].length > 1) { city = parts[n]; break; }
  }
  if (city && !country) country = CITY_COUNTRY[city.toLowerCase()] || '';
  var out = city && country ? (city + ', ' + country) : (city || country || '');
  if (remote) return out ? ('Remote, ' + out) : 'Remote';
  return out || String(raw).trim();
}

function durationFrom(text) {
  var t = String(text || '');
  if (/\b(year[\s-]?long|year in industry|placement year|industrial placement|12[\s-]?month|sandwich)\b/i.test(t))
    return '12 months';
  var m = /\b(\d{1,2})[\s-]*month/i.exec(t);
  if (m && +m[1] >= 3 && +m[1] <= 18) return m[1] + ' months';
  if (/\bco[\s-]?op\b/i.test(t)) return 'Co-op (4-8 months)';
  if (/\bsummer\b/i.test(t)) return '~10-12 weeks (summer)';
  if (/\bintern(ship)?\b/i.test(t)) return 'Internship (TBC)';
  if (/\bplacement\b/i.test(t)) return 'Placement (TBC)';
  return '';
}

// Bank sizing, mirroring config/config.yaml. Removing a role used to pull in a
// replacement immediately, every time, so the bank could never get smaller — the
// reason it sat at 31 open roles. Replacing is now something you ask for in the
// removal dialog, and the ceiling is what bounds it.
var BANK_CEILING = 10;
var MAX_PER_COMPANY = 3;
var MIN_PROMOTE_SCORE = 75;

/** Roles still awaiting action — an applied role is your pipeline, not clutter. */
var ACTIONED_ = ['Applied', 'OA', 'Interview', 'Offer', 'Rejected'];

/**
 * How many roles are still awaiting action. Nothing in this file calls it: it
 * exists so tests/dashboard/units.js can assert on the bank-sizing behaviour
 * removeJob depends on (does a removal below the floor refill, does an applied
 * role stop counting) without re-implementing the rule it is checking. Keep it.
 */
function openCount_() {
  return rowsOf(SHEET_JOBS).filter(function (j) {
    return ACTIONED_.indexOf(String(j['Status'] || '').trim()) < 0;
  }).length;
}
/** TikTok files postings under ByteDance too; one employer for the cap. */
function companyKey_(name) {
  var k = String(name || '').toLowerCase().replace(/[^a-z0-9 ]/g, ' ')
    .replace(/\b(group|inc|ltd|limited|llc|corp|corporation|holdings|plc|sa|ag|gmbh)\b/g, ' ')
    .replace(/\s+/g, ' ').trim();
  return (k === 'bytedance' || k === 'tiktok bytedance') ? 'tiktok' : k;
}

/** Highest-scoring recently-archived role not already in the bank, if it clears the bar. */
function promoteFromArchive_() {
  var arch = sheetOf(SHEET_ARCHIVE);
  if (!arch) return null;
  var rows = recentArchive_();
  if (!rows.length) return null;

  var jobsSheet = sheetOf(SHEET_JOBS);
  var head = headerOf(jobsSheet);
  var seen = {};
  var byCompany = {};
  rowsOf(SHEET_JOBS).forEach(function (j) {
    seen[String(j['Company'] + '|' + j['Role']).toLowerCase()] = true;
    var k = companyKey_(j['Company']);
    byCompany[k] = (byCompany[k] || 0) + 1;
  });

  var best = null;
  rows.forEach(function (r) {
    var disp = String(r['Disposition'] || '');
    if (disp.indexOf('promoted') >= 0 || disp.indexOf('removed') >= 0) return;
    if (!r['Company'] || !r['Role'] || !r['Link']) return;
    if (seen[String(r['Company'] + '|' + r['Role']).toLowerCase()]) return;
    if ((byCompany[companyKey_(r['Company'])] || 0) >= MAX_PER_COMPANY) return;
    if (!best || (Number(r['Score']) || 0) > (Number(best['Score']) || 0)) best = r;
  });
  if (!best || (Number(best['Score']) || 0) < MIN_PROMOTE_SCORE) return null;

  // enrich as it enters the bank, so every row has the same shape
  var map = {
    'Category': best['Category'] || 'Other', 'Company': best['Company'],
    'Role': best['Role'], 'Score': best['Score'],
    'Location': normalizeLocation(best['Location'] || ''),
    'Link': best['Link'], 'Priority': '', 'Deadline': best['Deadline'] || '',
    'Opens': best['Added'] || '', 'Duration': durationFrom(best['Role']),
    'Status': 'Not applied', 'Notes': '',
    'Description': best['Description'] || '',
    'Score reason': 'Pulled in from the archive to replace a role you removed.',
    'Source': best['Source'] || ''
  };
  var newRow = appendRowSafely_(jobsSheet, head.map(function (h) {
    return map.hasOwnProperty(h) ? map[h] : '';
  }));

  var dispCol = colIndex(arch, 'Disposition');
  if (dispCol) {
    arch.getRange(best._row, dispCol)
        .setValue(String(best['Disposition'] || '') + ' | promoted');
  }
  // set AFTER the write: _row is not a sheet column and must not reach the row
  map._row = newRow;
  return map;
}

/**
 * Promote ONE specific archived role into the Job Bank — the "+" button on the
 * More roles page. Unlike promoteFromArchive_() this deliberately ignores
 * MIN_PROMOTE_SCORE, the bank ceiling and the per-company cap: those exist to
 * stop the pipeline auto-filling the bank with mediocre roles, but an explicit
 * click is a human saying "I want this one anyway", which should always win.
 */
function promoteToBank(archiveRow, link) {
  var arch = sheetOf(SHEET_ARCHIVE);
  if (!arch) return { ok: false, error: 'No Archive sheet' };
  archiveRow = resolveRow_(arch, archiveRow, link);
  if (!archiveRow) return { ok: false, error: STALE_ROW_ };
  var head = headerOf(arch);
  var vals = arch.getRange(archiveRow, 1, 1, head.length).getDisplayValues()[0];
  var r = {};
  head.forEach(function (h, i) { r[h] = vals[i]; });

  if (!r['Company'] || !r['Role'] || !r['Link']) {
    return { ok: false, error: 'That row is missing company/role/link' };
  }
  if (String(r['Disposition'] || '').indexOf('promoted') >= 0) {
    return { ok: false, error: 'Already added to the Job Bank' };
  }

  var jobsSheet = sheetOf(SHEET_JOBS);
  var jobsHead = headerOf(jobsSheet);
  var key = String(r['Company'] + '|' + r['Role']).toLowerCase();
  var dup = rowsOf(SHEET_JOBS).some(function (j) {
    return String(j['Company'] + '|' + j['Role']).toLowerCase() === key;
  });
  if (dup) return { ok: false, error: 'Already in the Job Bank' };

  // same enrich-on-entry as the automatic path, so every bank row has one shape
  var map = {
    'Category': r['Category'] || 'Other', 'Company': r['Company'],
    'Role': r['Role'], 'Score': r['Score'],
    'Location': normalizeLocation(r['Location'] || ''),
    'Link': r['Link'], 'Priority': '', 'Deadline': r['Deadline'] || '',
    'Opens': r['Added'] || '', 'Duration': durationFrom(r['Role']),
    'Status': 'Not applied', 'Notes': '',
    'Description': r['Description'] || '',
    'Score reason': 'Added by hand from More roles (score ' + (r['Score'] || '?') + ').',
    'Source': r['Source'] || ''
  };
  var newRow = appendRowSafely_(jobsSheet, jobsHead.map(function (h) {
    return map.hasOwnProperty(h) ? map[h] : '';
  }));

  var dispCol = colIndex(arch, 'Disposition');
  if (dispCol) {
    arch.getRange(archiveRow, dispCol)
        .setValue(String(r['Disposition'] || '') + ' | promoted');
  }

  // Hand back a rendered card (and its score) so the Job Bank page can slot the
  // role straight in. Re-rendering the whole page just to show one new row was
  // the reason a reload was needed.
  var added = {_row: newRow};
  Object.keys(map).forEach(function (k) { added[k] = map[k]; });
  return { ok: true, company: r['Company'], role: r['Role'],
           score: Number(map['Score']) || 0, card: card(added, 0) };
}


/* ------------------------------------------------- tailoring prompt --- */
/**
 * Build a ready-to-paste prompt for one role. The full job description is NOT
 * stored by the pipeline (it is summarised, then discarded), so it is fetched
 * live here: always current, and no bloat in the sheet. Falls back to the
 * stored Description if the site blocks a server-side fetch.
 *
 * Shaped by the evidence in research/apply-workflow.md:
 *  - context beats prompt cleverness: the FULL job description and the FULL CV
 *    must both be in the chat before anything is asked
 *  - micro-edits beat rewrites (+24 pts for a retuned summary line vs +7 for a
 *    full document rewrite), so this asks for targeted edits
 *  - LLMs fabricate metrics, so invention is forbidden explicitly
 */
function jdTextFor_(url) {
  if (!url) return '';
  try {
    var res = UrlFetchApp.fetch(url, {
      muteHttpExceptions: true,
      followRedirects: true,
      headers: {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' +
                      '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
      }
    });
    if (res.getResponseCode() !== 200) return '';
    var html = res.getContentText();
    // Furniture goes before the tags do. Stripping tags first leaves the menu
    // TEXT behind, and it is at the top of the document, so every fetched
    // description opened with it: Anthropic's read "Skip to content Careers
    // Home We are looking for a research intern...". The <nav>/<header>/
    // <footer> blocks are where that text lives.
    var body = html
      .replace(/<script[\s\S]*?<\/script>/gi, ' ')
      .replace(/<style[\s\S]*?<\/style>/gi, ' ')
      .replace(/<nav[\s\S]*?<\/nav>/gi, ' ')
      .replace(/<header[\s\S]*?<\/header>/gi, ' ')
      .replace(/<footer[\s\S]*?<\/footer>/gi, ' ')
      .replace(/<[^>]+>/g, ' ')
      .replace(/&nbsp;/gi, ' ')
      .replace(/&amp;/gi, '&')
      .replace(/&lt;/gi, '<')
      .replace(/&gt;/gi, '>')
      .replace(/\s+/g, ' ')
      .trim();
    return body.slice(0, 7000);
  } catch (err) {
    return '';
  }
}

function tailoringPrompt(rowNumber, sheetName, link) {
  var sh = sheetOf(sheetName || SHEET_JOBS);
  var row = resolveRow_(sh, rowNumber, link);
  if (!row) return { ok: false, error: STALE_ROW_ };
  var head = headerOf(sh);
  var vals = sh.getRange(row, 1, 1, head.length).getDisplayValues()[0];
  var j = {};
  head.forEach(function (h, i) { j[h] = vals[i]; });

  var jd = jdTextFor_(j['Link']);
  var jdBlock = jd
    ? 'JOB DESCRIPTION (fetched from the posting):\n"""\n' + jd + '\n"""'
    : 'JOB DESCRIPTION: could not be fetched automatically. Open ' + j['Link'] +
      ', copy the posting text and paste it here.\n\nSummary on file: ' +
      (j['Description'] || '(none)');

  var lines = [
    'I am applying for this internship. My CV is attached. Help me tailor it.',
    '',
    'ROLE: ' + j['Role'],
    'COMPANY: ' + j['Company'],
    'LOCATION: ' + j['Location'],
    j['Duration'] ? 'DURATION: ' + j['Duration'] : '',
    j['Deadline'] ? 'DEADLINE: ' + j['Deadline'] : '',
    'LINK: ' + j['Link'],
    '',
    jdBlock,
    '',
    'ABOUT ME: penultimate-year MEng Computer Science at Imperial College London,',
    'graduating June 2028. French and British dual citizen, so full UK and EU work',
    'rights with no sponsorship needed.',
    '',
    'HOW AUTOMATED SCREENERS SCORE THIS CV (factor into every suggestion below):',
    'HackerRank publishes its own resume-scoring agent, including the exact rubric',
    'for a software intern role (github.com/interviewstreet/hiring-agent). Weights:',
    '- Open source 35/100: contributions to OTHER people\'s projects; 1000+ star',
    '  repos = top band; personal repos alone are CAPPED at 10. My beets and',
    '  mutagen contributions are exactly this signal, so name the repos and what',
    '  the merged changes did — never bury them at the bottom.',
    '- Self projects 30/100: complexity and real-world impact. A project with NO',
    '  working GitHub/demo link loses 30-50% of its score; live demos earn 10-20%',
    '  extra; generic names ("Todo App", "Calculator") take explicit deductions.',
    '- Production experience 25/100 (DraftKings goes here), technical skills 10.',
    '- Bonuses: portfolio site, LinkedIn, technical blog. The agent also FETCHES',
    '  my GitHub profile from the link on the CV and scores what it finds.',
    '- It deliberately ignores university name and grades, so lines that only say',
    '  "Imperial" or "IB 42/45" do nothing for the machine pass — keep them for',
    '  human readers but never at the expense of project or open-source content.',
    '',
    'KNOWN GAPS from actually running that agent on my CV (July 2026, scored',
    '66/100): my beets/mutagen open-source work got 15/35 because the CV has no',
    'PR links and my GitHub profile doesn\'t corroborate it; F1 Telemetry, ARMv8',
    'emulator, LibSpace and the chess AI all took deductions for missing links;',
    'no portfolio URL in the header. If the CV version I attach still has any of',
    'these gaps, call it out in the verdict.',
    '',
    'WHAT I NEED, in this order, and keep it tight:',
    '',
    '1. VERDICT (2 lines). Am I a credible candidate here, and what is the single',
    '   biggest gap between my CV and this posting?',
    '',
    '2. SUMMARY LINE. Rewrite the summary/title line at the top of my CV for this',
    '   specific role, mirroring the posting own job title and vocabulary. Give me',
    '   the exact text to paste.',
    '',
    '3. BULLET EDITS. Pick the 4-6 CV bullets that matter most for THIS role and',
    '   rewrite them to mirror the posting language. Show each as BEFORE then',
    '   AFTER so I can see exactly what changed. Do not rewrite my whole CV.',
    '',
    '4. REORDER. Which projects or experience should move up, and which should be',
    '   cut for this application. One line each.',
    '',
    '5. MISSING KEYWORDS. Terms in the posting that a keyword search would look',
    '   for and my CV does not contain, but ONLY ones I can honestly claim. Flag',
    '   any I cannot claim rather than working them in.',
    '',
    '6. SCREENER AUDIT. Using the HackerRank rubric above: list every project on',
    '   my CV that is missing a working GitHub or live-demo link, any generically',
    '   named project worth renaming to what it actually does, and whether the',
    '   open-source contributions are prominent enough. One line each.',
    '',
    '7. COVER LETTER (~250 words) if one is worth sending here. Direct and',
    '   specific. No "I have always been passionate about". Lead with what I have',
    '   actually built that is relevant to them.',
    '',
    'HARD RULES:',
    '- Invent NOTHING. Every claim must trace to something already on my CV. If a',
    '  requirement is not covered by my real experience, say so plainly instead of',
    '  inventing a metric, a technology or a responsibility.',
    '- Do not stuff keywords. A human recruiter reads this.',
    '- Rephrase toward the role, never change the underlying facts, dates or scope.',
    '- UK/EU CV conventions: no photo, no age, no marital status, one page.'
  ];
  var prompt = lines.filter(function (x) { return x !== ''; }).join('\n');
  return { ok: true, prompt: prompt, fetched: !!jd, company: j['Company'] };
}

/* ------------------------------------------------------- reach-out outreach --- */
/**
 * Everything needed to reach a real human about one role, per
 * research/apply-workflow.md and research/outreach-spec.md:
 *  - LinkedIn is the highest-reply channel (17% vs 6% email); the alumni/engineer
 *    routes matter more than the named recruiter because they can file a referral.
 *  - Email discovery needs a Hunter.io key (Script Properties, never hardcoded) and
 *    is honestly capped — "unknown" is a legitimate result, not a bug.
 *  - Messages are drafted with the Anthropic API when a key is configured, else a
 *    strong per-role template — never a single fixed block of text for every job.
 */

// ATS hosts hold no company-domain signal — the real employer domain lives elsewhere.
var ATS_HOSTS_ = [
  /\.lever\.co$/i, /boards\.greenhouse\.io$/i, /jobs\.ashbyhq\.com$/i,
  /\.myworkdayjobs\.com$/i, /jobs\.smartrecruiters\.com$/i, /apply\.workable\.com$/i,
  /jobs\.icims\.com$/i, /taleo\.net$/i, /avature\.net$/i
];

// Companies running 100+:1 grad schemes, where the ATS is read before any human.
var BIG_SCHEMES_ = /\b(google|amazon|goldman sachs|jpmorgan|j\.?p\.?\s?morgan|microsoft|meta|deloitte|pwc|\bey\b|kpmg|accenture|morgan stanley|barclays|hsbc|citi(bank)?|bank of america|ibm)\b/i;

function isBigScheme_(company) {
  return BIG_SCHEMES_.test(String(company || ''));
}

/** The company's domain, or a best-effort guess when the link is an ATS host. */
function companyDomain_(link, company, source) {
  var m = /^https?:\/\/([^\/]+)/i.exec(String(link || ''));
  var host = m ? m[1].replace(/^www\./i, '') : '';
  var isAts = host && ATS_HOSTS_.some(function (re) { return re.test(host); });
  if (host && !isAts) return { domain: host, guessed: false };

  var base = String(company || source || '').toLowerCase().replace(/[^a-z0-9]+/g, '');
  return base ? { domain: base + '.com', guessed: true } : { domain: '', guessed: false };
}

/**
 * currentCompany= only filters on LinkedIn when it carries a real numeric company
 * ID, which we don't have — passing a plain name there is silently ignored and
 * falls back to a generic keyword search across every recruiter on the site. The
 * reliable fix is to put the company name (and the actual role, not a fixed
 * "Software Engineer") INSIDE the keywords string, since that's full-text matched
 * against a profile's current-company field.
 */
function outreachRoleKeyword_(role) {
  var r = String(role || '')
    .replace(/\b(20\d{2}|intern(ship)?|placement|graduate|co-?op|trainee|programme|program|summer|spring|autumn|winter)\b/gi, '')
    .replace(/\bswe\b/i, 'Software Engineer')
    .replace(/[^a-zA-Z0-9 ]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  return r || 'Software Engineer';
}

/**
 * The name to search LinkedIn for, which is rarely the name on the posting.
 * Profiles say "Snap", not "Snap Inc."; "Renault", not "Renault Group"; and
 * "Sony / PlayStation" is two employers written as one. Quoting the legal name
 * verbatim found nobody for 70 of the 469 companies on the sheet.
 */
var CORP_SUFFIX_RE_ = /[\s,]+(inc|incorporated|llc|ltd|limited|co|corp|corporation|plc|gmbh|sa|nv|ab|ag|group|holdings?|technologies|international)\.?\s*$/i;

function searchName_(company) {
  var n = String(company || '').replace(/["“”]/g, '').trim();
  n = n.replace(/\s*\([^)]*\)\s*$/, '');            // "... Corporation (CSMC)" -> "... Corporation"
  n = n.split(/\s*[\/|]\s*/)[0];                 // "Sony / PlayStation" -> "Sony"
  n = n.split(/\s+[-–—]\s+/)[0];                 // "... - USA" -> "..."
  n = n.split(',')[0];                           // "..., LLC" -> "..."
  for (var i = 0; i < 3; i++) {                  // "Group, LLC" style doubles
    var trimmed = n.replace(CORP_SUFFIX_RE_, '');
    if (trimmed === n) break;
    n = trimmed;
  }
  n = n.trim().replace(/[\s.,\-&+]+$/, '');       // "RRS Group & Co" -> "RRS"
  // never reduce a name to nothing, or to something too short to be meaningful
  return n.length >= 2 ? n : String(company || '').replace(/["“”]/g, '').trim();
}

function linkedinLinks_(company, role) {
  var c = String(company || '').trim();
  // Quoted: unquoted "Epic Games" also matches people at other games companies,
  // so every one of these links returned more than its label promised.
  var q = '"' + searchName_(c) + '"';
  var recruiterKeywords = encodeURIComponent(q +
    ' (University Recruiter OR Early Careers OR Campus Recruiting OR Emerging Talent OR ' +
    'Graduate Recruitment OR Talent Acquisition)');
  var engineerKeywords = encodeURIComponent(q + ' ' + outreachRoleKeyword_(role));
  return {
    linkedinPeople: 'https://www.linkedin.com/search/results/people/?keywords=' + recruiterKeywords,
    linkedinEngineers: 'https://www.linkedin.com/search/results/people/?keywords=' + engineerKeywords,
    // The school's own people tab ignores ?keywords= on load, so that link just
    // opened every Imperial alumnus rather than the ones at this company. A
    // people search with both phrases quoted actually constrains it, and is the
    // same endpoint the two links above already use.
    linkedinAlumni: 'https://www.linkedin.com/search/results/people/?keywords=' +
      encodeURIComponent(q + ' "Imperial College London"')
  };
}

/** Hunter.io Domain Search — 50 free credits/month. Needs HUNTER_API_KEY in Script Properties. */
function hunterDomainSearch_(domain) {
  if (!domain) return null;
  var key = PropertiesService.getScriptProperties().getProperty('HUNTER_API_KEY');
  if (!key) return null;
  try {
    var url = 'https://api.hunter.io/v2/domain-search?domain=' + encodeURIComponent(domain) +
      '&api_key=' + encodeURIComponent(key);
    var res = UrlFetchApp.fetch(url, { muteHttpExceptions: true });
    if (res.getResponseCode() !== 200) return null;
    var data = JSON.parse(res.getContentText());
    var d = data && data.data;
    if (!d) return null;
    var candidates = (d.emails || []).slice(0, 5).map(function (e) {
      return {
        name: ((e.first_name || '') + ' ' + (e.last_name || '')).trim(),
        position: e.position || '', email: e.value || '', confidence: e.confidence
      };
    });
    return { pattern: d.pattern || '', candidates: candidates };
  } catch (err) {
    return null;
  }
}

function firstSentence_(text) {
  var t = String(text || '').trim();
  if (!t) return '';
  var m = /^[^.!?]{20,220}[.!?]/.exec(t);
  return m ? m[0].trim() : t.slice(0, 160).trim();
}

/** Draft note/DM/email with the Anthropic API if a key is configured. */
function draftsFromAi_(job, detailSource, key) {
  try {
    var prompt = [
      'Write three pieces of outreach text for a UK internship application. Return ONLY ' +
      'raw JSON, no prose, no markdown fences.',
      'Schema: {"note": string, "dm": string, "email": {"subject": string, "body": string}}',
      '',
      'CANDIDATE: <name>, <degree and university>, <right-to-work status>.',
      'ROLE: ' + job['Role'],
      'COMPANY: ' + job['Company'],
      'JOB POSTING TEXT (use this to find ONE specific, non-transferable detail about this ' +
      'team or product — a detail that would be false if you swapped in a different ' +
      'company name):',
      '"""' + String(detailSource || '').slice(0, 4000) + '"""',
      '',
      'HARD RULES:',
      '- note: a LinkedIn connection-request note, MAX 200 characters, no ask or pitch — ' +
      'just a warm, specific reason to connect.',
      '- dm: a LinkedIn DM, UNDER 400 characters, sent after they accept the connection. ' +
      'Include the one specific team detail and exactly one low-friction question.',
      '- email.subject: specific and boring, e.g. "SWE Intern 2027 — <Name>, <Course>". ' +
      'Include a req ID only if one appears in the posting text.',
      '- email.body: 120-180 words. One specific non-transferable detail about the team. ' +
      'Exactly one low-friction question. Slightly positive tone, simple language.',
      '- Register: direct and specific, no grovelling.',
      '- Never use: "I\'ve always been passionate about", "fast learner", "team player", ' +
      '"Do you have any opportunities?", company-history recitation, career life-story, ' +
      'asking them to review the CV, asking for a hiring-manager intro, Calendly links, ' +
      '"Dear Sir/Madam".',
      '- Leave "[Name]" as a placeholder for the recipient\'s first name — do not invent one.'
    ].join('\n');

    var res = UrlFetchApp.fetch('https://api.anthropic.com/v1/messages', {
      method: 'post',
      contentType: 'application/json',
      muteHttpExceptions: true,
      headers: { 'x-api-key': key, 'anthropic-version': '2023-06-01' },
      payload: JSON.stringify({
        model: 'claude-sonnet-5',
        max_tokens: 900,
        messages: [{ role: 'user', content: prompt }]
      })
    });
    if (res.getResponseCode() !== 200) return null;
    var data = JSON.parse(res.getContentText());
    var text = data.content && data.content[0] && data.content[0].text;
    if (!text) return null;
    var m = /\{[\s\S]*\}/.exec(text);
    if (!m) return null;
    var parsed = JSON.parse(m[0]);
    if (!parsed.note || !parsed.dm || !parsed.email) return null;
    return {
      note: String(parsed.note).slice(0, 200),
      dm: String(parsed.dm).slice(0, 400),
      email: { subject: String(parsed.email.subject || ''), body: String(parsed.email.body || '') },
      source: 'ai'
    };
  } catch (err) {
    return null;
  }
}

function draftsFromTemplate_(job, detailSource) {
  var role = job['Role'] || 'this role';
  var company = job['Company'] || 'your team';
  var detail = firstSentence_(detailSource) || ('the ' + role + ' work at ' + company);

  var note = ('Hi [Name] — I just applied for the ' + role + ' role at ' + company +
    ' and would love to connect.').slice(0, 200);

  var dm = ('Hi [Name], I applied for the ' + role + ' role at ' + company +
    ' and wanted to reach out directly. ' + detail + ' Quick one — is the team still ' +
    'actively reviewing applications for this cycle? Happy to share more about my ' +
    'background if useful.').slice(0, 400);

  var subject = 'SWE Intern 2027 — <Name>, <Course>';
  var body = [
    'Hi [Name],',
    '',
    'I applied for the ' + role + ' role at ' + company + ' a few days ago and wanted to ' +
    'follow up directly.',
    detail,
    'I\'m a penultimate-year MEng Computing student at Imperial College London (dual ' +
    'French/British citizen — full UK and EU right to work, no sponsorship needed), ' +
    'and this role lines up closely with what I\'ve been building. That combination of ' +
    'genuine interest and hands-on experience is why this specific opening caught my eye ' +
    'rather than the dozens of others I looked at.',
    '',
    'Quick question: is the team still actively reviewing applications for this cycle, or ' +
    'has the shortlist already closed?',
    '',
    'Happy to send more detail on my background if useful — my CV is attached.',
    '',
    'Thanks,',
    'Augustin'
  ].join('\n');

  return { note: note, dm: dm, email: { subject: subject, body: body }, source: 'template' };
}

function generateDrafts_(job, detailSource) {
  var aiKey = PropertiesService.getScriptProperties().getProperty('ANTHROPIC_API_KEY');
  if (aiKey) {
    var ai = draftsFromAi_(job, detailSource, aiKey);
    if (ai) return ai;
  }
  return draftsFromTemplate_(job, detailSource);
}

/**
 * Contact dates are tracked in the script's own storage, NOT a Job Bank column —
 * rows get deleted/reshuffled (see removeJob) and the Bank stays a plain view of
 * live roles. Keyed by company+role so the record survives a row moving.
 */
var OUTREACH_LOG_PROP_ = 'OUTREACH_LOG';

function outreachKey_(company, role) {
  return (String(company || '').trim() + '|' + String(role || '').trim()).toLowerCase();
}

function outreachLog_() {
  var raw = PropertiesService.getScriptProperties().getProperty(OUTREACH_LOG_PROP_);
  if (!raw) return {};
  try { return JSON.parse(raw) || {}; } catch (err) { return {}; }
}

function saveOutreachLog_(log) {
  PropertiesService.getScriptProperties().setProperty(OUTREACH_LOG_PROP_, JSON.stringify(log));
}

function outreachFor(rowNumber, sheetName, link) {
  var sh = sheetOf(sheetName || SHEET_JOBS);
  var row = resolveRow_(sh, rowNumber, link);
  if (!row) return { ok: false, error: STALE_ROW_ };
  var head = headerOf(sh);
  var vals = sh.getRange(row, 1, 1, head.length).getDisplayValues()[0];
  var j = {};
  head.forEach(function (h, i) { j[h] = vals[i]; });

  var jd = jdTextFor_(j['Link']);
  var links = linkedinLinks_(j['Company'] || '', j['Role'] || '');
  var domainInfo = companyDomain_(j['Link'], j['Company'], j['Source']);
  var hunter = domainInfo.domain ? hunterDomainSearch_(domainInfo.domain) : null;
  var drafts = generateDrafts_(j, jd || j['Description'] || '');
  var lastContacted = outreachLog_()[outreachKey_(j['Company'], j['Role'])] || null;

  return {
    ok: true,
    company: j['Company'],
    role: j['Role'],
    linkedinPeople: links.linkedinPeople,
    linkedinEngineers: links.linkedinEngineers,
    linkedinAlumni: links.linkedinAlumni,
    domain: domainInfo.domain,
    domainGuessed: domainInfo.guessed,
    hunterConfigured: !!PropertiesService.getScriptProperties().getProperty('HUNTER_API_KEY'),
    pattern: hunter ? hunter.pattern : '',
    candidates: hunter ? hunter.candidates : [],
    note: drafts.note,
    dm: drafts.dm,
    email: drafts.email,
    bigScheme: isBigScheme_(j['Company']),
    lastContacted: lastContacted
  };
}

/** Record today as the contact date, enforcing "one follow-up, then stop". */
function markContacted(rowNumber, sheetName, link) {
  var sh = sheetOf(sheetName || SHEET_JOBS);
  var row = resolveRow_(sh, rowNumber, link);
  if (!row) return { ok: false, error: STALE_ROW_ };
  var head = headerOf(sh);
  var vals = sh.getRange(row, 1, 1, head.length).getDisplayValues()[0];
  var j = {};
  head.forEach(function (h, i) { j[h] = vals[i]; });

  var key = outreachKey_(j['Company'], j['Role']);
  if (key === '|') return { ok: false, error: 'Missing company/role for this row' };

  var log = outreachLog_();
  var today = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'yyyy-MM-dd');
  log[key] = today;
  saveOutreachLog_(log);
  return { ok: true, date: today };
}

/* ------------------------------------------------------- descriptions --- */
/**
 * Roles arrive with no description more often than you would think: TikTok and
 * the other JS-rendered career sites give the fetcher a title and nothing else,
 * and a role added by hand or pulled out of the archive often has nothing worth
 * keeping either. The card then reads "No description", which is the complaint.
 *
 * The posting itself always has the text, so fetch it the first time the card is
 * opened and write it back to the sheet — a second open, and the next tailoring
 * prompt, both get it for free. Deliberately on demand: doing this for every row
 * on page load would be dozens of fetches for descriptions nobody reads.
 */
function describeRole(rowNumber, sheetName, link) {
  var sh = sheetOf(sheetName || SHEET_JOBS);
  var row = resolveRow_(sh, rowNumber, link);
  if (!row) return { ok: false, error: STALE_ROW_ };
  var head = headerOf(sh);
  var vals = sh.getRange(row, 1, 1, head.length).getDisplayValues()[0];
  var j = {};
  head.forEach(function (h, i) { j[h] = vals[i]; });

  var existing = String(j['Description'] || '').trim();
  if (existing.length > 40) return { ok: true, text: existing, cached: true };

  var jd = jdTextFor_(j['Link']);
  if (!jd) {
    return { ok: false, error: 'That site will not hand over its text — open the posting.' };
  }
  var text = firstSentences_(jd, 600);
  var col = headerOf(sh).indexOf('Description') + 1;
  if (col) sh.getRange(row, col).setValue(text);
  return { ok: true, text: text, cached: false };
}

/** Trim fetched page text to something a card can actually show. */
function firstSentences_(text, limit) {
  var t = String(text || '').replace(/\s+/g, ' ').trim();
  // career pages open with nav junk; start at the first real sentence
  var start = t.search(/[A-Z][^.!?]{40,}/);
  if (start > 0) t = t.slice(start);
  if (t.length <= limit) return t;
  var cut = t.slice(0, limit);
  var stop = cut.lastIndexOf('. ');
  return (stop > limit * 0.5 ? cut.slice(0, stop + 1) : cut) + '…';
}

/* ------------------------------------------------------ find roles: search --- */
/**
 * On-demand company search — the answer to "does Apple have anything right now?"
 * without waiting for the weekly run to happen across it.
 *
 * Four sources, cheapest first:
 *  1. the sheet itself: ~6,900 archived roles the pipeline has already fetched;
 *  2. the company's ATS board (Greenhouse / Lever / Ashby), which is public JSON;
 *  3. Adzuna, the only route to employers on custom career sites (Apple, ESA);
 *  4. nothing — a real answer, and the reason "add your own" exists next to it.
 *
 * Relevance mirrors filters.py rather than importing it: the job here is to hide
 * the 400 senior roles at a big employer, not to re-run the pipeline in a page.
 */
var SEARCH_LIMIT = 30;
var SEARCH_LOCAL_LIMIT = 40;
// This profile wants internships and placements; a graduate-scheme profile
// deliberately looks for the opposite kind of role.
var WANT_RE_ = /\b(intern|interns|internship|placement|year in industry|industrial (year|placement)|apprentice|working student|stage|alternan\w*|praktikum|summer analyst|campus|student)\b/i;
var SENIOR_RE_ = /\b(senior|staff|principal|lead|manager|director|head of|vp|chief|architect|expert|10\+)\b/i;
var GEO_RE_ = /(united kingdom|\buk\b|england|london|scotland|wales|ireland|dublin|france|paris|germany|berlin|munich|netherlands|amsterdam|spain|madrid|barcelona|italy|milan|portugal|lisbon|belgium|brussels|switzerland|zurich|geneva|sweden|stockholm|denmark|copenhagen|poland|warsaw|austria|vienna|europe|emea|remote)/i;

function normUrl_(u) {
  return String(u || '').split('?')[0].replace(/\/+$/, '').toLowerCase();
}

function slugGuess_(name) {
  return String(name || '').toLowerCase().replace(/&/g, 'and').replace(/[^a-z0-9]/g, '');
}

/** The Watchlist tab already knows which ATS a tracked company uses. */
function watchlistEntry_(name) {
  var sh = sheetOf(SHEET_WATCH);
  if (!sh || sh.getLastRow() < 2) return null;
  var vals = sh.getRange(2, 1, sh.getLastRow() - 1, 3).getDisplayValues();
  var key = companyKey_(name);
  for (var i = 0; i < vals.length; i++) {
    if (vals[i][0] && companyKey_(vals[i][0]).indexOf(key) >= 0) {
      return { name: vals[i][0], ats: String(vals[i][1] || '').toLowerCase(),
               slug: String(vals[i][2] || '') };
    }
  }
  return null;
}

/** Roles for this company the pipeline has already seen — instant and free. */
function localMatches_(name) {
  var key = companyKey_(name), out = [];
  rowsOf(SHEET_JOBS).forEach(function (j) {
    if (companyKey_(j['Company']).indexOf(key) >= 0) {
      out.push({ company: j['Company'], role: j['Role'], location: j['Location'],
                 link: j['Link'], deadline: j['Deadline'], duration: j['Duration'],
                 description: j['Description'], score: Number(j['Score']) || 0,
                 reason: 'Already in your Job Bank.', source: 'job bank',
                 inBank: true });
    }
  });
  var arch = sheetOf(SHEET_ARCHIVE);
  if (!arch || arch.getLastRow() < 2) return out;
  var col = colIndex(arch, 'Company');
  if (!col) return out;
  // TextFinder so a 6,900-row tab is never pulled into memory whole
  var hits = arch.getRange(2, col, arch.getLastRow() - 1, 1)
                 .createTextFinder(name).matchCase(false).findAll();
  var head = headerOf(arch);
  for (var i = 0; i < hits.length && i < SEARCH_LOCAL_LIMIT; i++) {
    var vals = arch.getRange(hits[i].getRow(), 1, 1, head.length).getDisplayValues()[0];
    var r = {};
    head.forEach(function (h, k) { r[h] = vals[k]; });
    if (companyKey_(r['Company']).indexOf(key) < 0) continue;
    var disp = String(r['Disposition'] || '');
    out.push({ company: r['Company'], role: r['Role'], location: r['Location'],
               link: r['Link'], deadline: r['Deadline'], duration: r['Duration'],
               description: r['Description'], score: Number(r['Score']) || 0,
               reason: r['Score reason'] || '', source: 'archive',
               archived: true, dismissed: disp.indexOf('removed') >= 0 });
  }
  return out;
}

/** Public ATS boards. All three are tried at once — a wrong guess just 404s. */
function atsSearch_(name) {
  var wl = watchlistEntry_(name);
  var slug = (wl && wl.slug && !/^https?:/i.test(wl.slug)) ? wl.slug : slugGuess_(name);
  if (!slug) return [];
  var kinds = ['greenhouse', 'lever', 'ashby'];
  var urls = [
    'https://boards-api.greenhouse.io/v1/boards/' + slug + '/jobs?content=true',
    'https://api.lever.co/v0/postings/' + slug + '?mode=json',
    'https://api.ashbyhq.com/posting-api/job-board/' + slug
  ];
  var reqs = urls.map(function (u) {
    return { url: u, muteHttpExceptions: true, followRedirects: true };
  });
  var responses;
  try {
    responses = UrlFetchApp.fetchAll(reqs);
  } catch (err) {
    return [];
  }
  var out = [];
  responses.forEach(function (res, i) {
    if (res.getResponseCode() !== 200) return;
    var data;
    try { data = JSON.parse(res.getContentText()); } catch (err) { return; }
    var label = wl ? wl.name : name;
    if (kinds[i] === 'greenhouse' && data.jobs) {
      data.jobs.forEach(function (j) {
        out.push({ company: label, role: j.title || '',
                   location: (j.location || {}).name || '',
                   link: j.absolute_url || '', source: 'greenhouse',
                   description: stripTags_(j.content || '').slice(0, 600) });
      });
    } else if (kinds[i] === 'lever' && data.length) {
      data.forEach(function (j) {
        out.push({ company: label, role: j.text || '',
                   location: (j.categories || {}).location || '',
                   link: j.hostedUrl || '', source: 'lever',
                   duration: (j.categories || {}).commitment || '',
                   description: String(j.descriptionPlain || '').slice(0, 600) });
      });
    } else if (kinds[i] === 'ashby' && data.jobs) {
      data.jobs.forEach(function (j) {
        out.push({ company: label, role: j.title || '', location: j.location || '',
                   link: j.jobUrl || '', source: 'ashby',
                   description: String(j.descriptionPlain || '').slice(0, 600) });
      });
    }
  });
  return out;
}

function stripTags_(html) {
  return String(html || '').replace(/<[^>]+>/g, ' ').replace(/&nbsp;/gi, ' ')
    .replace(/&amp;/gi, '&').replace(/\s+/g, ' ').trim();
}

/**
 * Adzuna. Returns null (not []) when unconfigured, so the page can say "add the
 * keys" rather than "nothing found" — the difference matters for Apple and ESA,
 * who have no public ATS board at all.
 */
var ADZUNA_COUNTRIES = ['gb', 'fr', 'de', 'us'];

function adzunaUrl_(country, what, page, id, key) {
  return { url: 'https://api.adzuna.com/v1/api/jobs/' + country + '/search/' + page +
                '?app_id=' + id + '&app_key=' + key + '&results_per_page=50&what=' + what +
                '&content-type=application/json',
           muteHttpExceptions: true };
}

/**
 * Adzuna's relevance ranking makes a single query unreliable: searching Dyson
 * for "Dyson graduate" returns nothing at all, while "Dyson engineer" returns
 * twelve postings including two graduate roles. Asking once per country
 * therefore misses roles that are plainly there — which is exactly the "they
 * exist but aren't coming up" complaint. Each country is asked three ways
 * instead: the company name alone across two pages, which is the query that
 * actually enumerates an employer, then the name with the role terms.
 */
function adzunaSearch_(name, terms) {
  var props = PropertiesService.getScriptProperties();
  var id = props.getProperty('ADZUNA_APP_ID'), key = props.getProperty('ADZUNA_APP_KEY');
  if (!id || !key) return null;
  var bare = encodeURIComponent(name);
  var withTerms = encodeURIComponent(name + ' ' + (terms || 'intern placement'));
  var reqs = [];
  ADZUNA_COUNTRIES.forEach(function (c) {
    reqs.push(adzunaUrl_(c, bare, 1, id, key));
    reqs.push(adzunaUrl_(c, bare, 2, id, key));
    reqs.push(adzunaUrl_(c, withTerms, 1, id, key));
  });
  var out = [], wanted = companyKey_(name);
  var responses;
  try {
    responses = UrlFetchApp.fetchAll(reqs);
  } catch (err) {
    return [];
  }
  responses.forEach(function (res) {
    if (res.getResponseCode() !== 200) return;
    var data;
    try { data = JSON.parse(res.getContentText()); } catch (err) { return; }
    (data.results || []).forEach(function (j) {
      var co = (j.company || {}).display_name || '';
      // Adzuna matches on free text, so most hits are other employers entirely
      if (companyKey_(co).indexOf(wanted) < 0) return;
      out.push({ company: co, role: j.title || '',
                 location: (j.location || {}).display_name || '',
                 link: j.redirect_url || '', source: 'adzuna',
                 description: String(j.description || '').slice(0, 600) });
    });
  });
  return out;
}

function relevantRole_(r) {
  var t = String(r.role || '');
  if (!t) return false;
  if (SENIOR_RE_.test(t)) return false;
  return WANT_RE_.test(t);
}

/** Honest stand-in for the LLM scorer: labelled as heuristic wherever shown. */
function heuristicScore_(r) {
  var s = 62;
  if (WANT_RE_.test(String(r.role || ''))) s += 10;
  if (/202[6-9]/.test(String(r.role || '') + ' ' + (r.description || ''))) s += 4;
  if (GEO_RE_.test(String(r.location || ''))) s += 6;
  if (SENIOR_RE_.test(String(r.role || ''))) s -= 30;
  return Math.max(0, Math.min(88, s));   // never claims a top-band score
}

/**
 * Rank with the same model the pipeline uses. Returns null when no key is set,
 * which is the signal to fall back to heuristicScore_ and say so in the UI.
 */
function rankWithAi_(results) {
  var key = PropertiesService.getScriptProperties().getProperty('ANTHROPIC_API_KEY');
  if (!key || !results.length) return null;
  var list = results.slice(0, 25).map(function (r, i) {
    return i + '. ' + r.role + ' — ' + (r.location || 'location not stated') +
           (r.duration ? ' — ' + r.duration : '');
  }).join('\n');
  var prompt = [
    'Score each role 0-100 for this candidate. Return ONLY a JSON array of',
    '{"i": <index>, "score": <0-100>, "reason": "<max 12 words>"}.',
    '',
    'CANDIDATE: penultimate-year MEng Computer Science, Imperial College London,',
    'graduating June 2028. French + British dual national (UK and EU work rights,',
    'no sponsorship). Wants 2027 summer internships and year-in-industry',
    'placements in software, ML or data. Interests: motorsport/F1, space, music',
    'tech, AI labs, sports data. Prior: DraftKings; open-source contributions to',
    'beets and mutagen.',
    '',
    'Score high: relevant intern/placement roles he can actually apply to now.',
    'Score low: senior/experienced roles, unrelated functions (sales, HR,',
    'marketing), or roles requiring a degree he does not have.',
    '',
    'ROLES:',
    list
  ].join('\n');
  try {
    var res = UrlFetchApp.fetch('https://api.anthropic.com/v1/messages', {
      method: 'post',
      contentType: 'application/json',
      muteHttpExceptions: true,
      headers: { 'x-api-key': key, 'anthropic-version': '2023-06-01' },
      payload: JSON.stringify({
        model: 'claude-haiku-4-5-20251001',
        max_tokens: 1200,
        messages: [{ role: 'user', content: prompt }]
      })
    });
    if (res.getResponseCode() !== 200) return null;
    var data = JSON.parse(res.getContentText());
    var text = data.content && data.content[0] && data.content[0].text;
    var m = text && /\[[\s\S]*\]/.exec(text);
    if (!m) return null;
    return JSON.parse(m[0]);
  } catch (err) {
    return null;
  }
}

function searchCompany(query, includeAll, terms) {
  var name = String(query || '').trim();
  if (name.length < 2) return { ok: false, error: 'Type a company name first' };

  var out = [], seen = {}, counts = { local: 0, ats: 0, adzuna: 0 };
  function push(r, bucket) {
    if (!r.role || !r.company) return;
    var k = normUrl_(r.link) ||
            (companyKey_(r.company) + '|' + String(r.role).toLowerCase());
    if (seen[k]) return;
    seen[k] = true;
    counts[bucket]++;
    out.push(r);
  }
  localMatches_(name).forEach(function (r) { push(r, 'local'); });
  atsSearch_(name).forEach(function (r) { push(r, 'ats'); });
  var adz = adzunaSearch_(name, terms);
  if (adz) adz.forEach(function (r) { push(r, 'adzuna'); });

  var filtered = includeAll ? out : out.filter(function (r) {
    return r.inBank || r.archived || relevantRole_(r);
  });

  // roles already on the sheet keep their real pipeline score; the rest are
  // ranked now, by the model when there is a key and by keyword when there isn't
  var fresh = filtered.filter(function (r) { return !r.inBank && !r.archived; });
  var ranked = rankWithAi_(fresh);
  if (ranked) {
    ranked.forEach(function (x) {
      var r = fresh[x.i];
      if (!r) return;
      r.score = Math.max(0, Math.min(100, Number(x.score) || 0));
      r.reason = String(x.reason || '');
      r.scoredBy = 'ai';
    });
  }
  fresh.forEach(function (r) {
    if (r.scoredBy) return;
    r.score = heuristicScore_(r);
    r.reason = r.reason || 'Keyword match — no AI key configured, so this is a rough rank.';
    r.scoredBy = 'heuristic';
  });

  filtered.sort(function (a, b) { return (b.score || 0) - (a.score || 0); });
  filtered = capSimilar_(filtered, MAX_SAME_ROLE,
                         function (r) { return r.company; },
                         function (r) { return r.role; });
  var top = filtered.slice(0, SEARCH_LIMIT);
  return {
    ok: true,
    company: name,
    total: out.length,
    counts: counts,
    adzunaConfigured: adz !== null,
    aiUsed: !!ranked,
    // rendered here, in the same order as `results`, so a hit's + button can
    // hand the exact record back rather than re-finding it server-side
    html: top.map(function (r, i) { return searchCard(r, i); }).join(''),
    results: top
  };
}

/* ------------------------------------------------------- add roles by hand --- */
/**
 * Add a role Aug found himself, or picked out of a search. Deliberately ignores
 * the bank ceiling and MIN_SCORE: those exist to stop the PIPELINE flooding the
 * bank, and an explicit "I want this one" should always win. The pipeline simply
 * adds nothing of its own until the bank is back below its refill floor.
 */
function addFoundRole(p) {
  p = p || {};
  var company = String(p.company || '').trim();
  var role = String(p.role || '').trim();
  if (!company || !role) return { ok: false, error: 'Company and role are both needed' };

  var sh = sheetOf(SHEET_JOBS);
  var head = headerOf(sh);
  var link = String(p.link || '').trim();
  var dupe = null;
  rowsOf(SHEET_JOBS).forEach(function (j) {
    if (link && normUrl_(j['Link']) === normUrl_(link)) dupe = j;
    if (companyKey_(j['Company']) === companyKey_(company) &&
        String(j['Role'] || '').toLowerCase() === role.toLowerCase()) dupe = j;
  });
  if (dupe) return { ok: false, error: 'Already in the Job Bank' };

  var score = Number(p.score) || 0;
  if (!score) score = heuristicScore_({ role: role, location: p.location, description: p.description });
  var map = {
    'Category': p.category || 'Other', 'Company': company, 'Role': role,
    'Score': score, 'Location': normalizeLocation(String(p.location || '')),
    'Link': link, 'Priority': '', 'Deadline': String(p.deadline || ''),
    'Opens': '', 'Duration': p.duration || durationFrom(role),
    'Status': 'Not applied', 'Notes': '',
    'Description': String(p.description || '').slice(0, 1200),
    'Score reason': p.reason ||
      (p.source ? 'Added from a ' + p.source + ' search.' : 'Added by hand.'),
    'Source': p.source || 'added by hand'
  };
  var newRow = appendRowSafely_(sh, head.map(function (h) {
    return map.hasOwnProperty(h) ? map[h] : '';
  }));
  var added = { _row: newRow };
  Object.keys(map).forEach(function (k) { added[k] = map[k]; });
  return { ok: true, company: company, role: role, score: score, card: card(added, 0) };
}

/* --------------------------------------------------------- opening soon --- */
/**
 * Programmes that are not open yet — the ESA graduate scheme that reopens in
 * February and must not be missed. The pipeline already flips a row to OPEN NOW
 * once its expected date passes (opening_soon.py) and puts it at the top of the
 * digest email, so anything added here inherits that for free.
 */
function openingSoonRows_() {
  return rowsOf(SHEET_SOON).filter(function (s) {
    return String(s['Status'] || '').toLowerCase().indexOf('dismissed') < 0;
  });
}

function addOpeningSoon(p) {
  p = p || {};
  var company = String(p.company || '').trim();
  var programme = String(p.programme || '').trim();
  if (!company || !programme) {
    return { ok: false, error: 'Company and programme are both needed' };
  }
  var sh = sheetOf(SHEET_SOON);
  if (!sh) return { ok: false, error: 'No Opening Soon tab' };
  var dupe = rowsOf(SHEET_SOON).some(function (s) {
    return companyKey_(s['Company']) === companyKey_(company) &&
           String(s['Programme'] || '').toLowerCase() === programme.toLowerCase();
  });
  if (dupe) return { ok: false, error: 'Already tracked in Opening Soon' };

  var head = headerOf(sh);
  var map = {
    'Company': company, 'Programme': programme,
    'Expected open date': String(p.openDate || ''),   // ISO: the pipeline parses it
    'Expected close date': String(p.closeDate || ''),
    'Status': p.status || 'Added by hand',
    'Link': String(p.link || ''), 'Notes': String(p.notes || '').slice(0, 500)
  };
  var newRow = appendRowSafely_(sh, head.map(function (h) {
    return map.hasOwnProperty(h) ? map[h] : '';
  }));
  var added = { _row: newRow };
  Object.keys(map).forEach(function (k) { added[k] = map[k]; });
  return { ok: true, company: company, programme: programme,
           emailed: !!map['Expected open date'], card: soonRow(added, 0) };
}

/** Nothing is deleted — the tab is a record — so this only marks the row. */
function dismissOpeningSoon(rowNumber, company, programme) {
  var sh = sheetOf(SHEET_SOON);
  if (!sh) return { ok: false, error: 'No Opening Soon tab' };
  var head = headerOf(sh);
  var row = Number(rowNumber);
  if (row < 2 || row > sh.getLastRow()) return { ok: false, error: STALE_ROW_ };
  var vals = sh.getRange(row, 1, 1, head.length).getDisplayValues()[0];
  var r = {};
  head.forEach(function (h, i) { r[h] = vals[i]; });
  // verify before writing: the Opening Soon tab has no stable link column
  if (company && companyKey_(r['Company']) !== companyKey_(company)) {
    return { ok: false, error: STALE_ROW_ };
  }
  var col = colIndex(sh, 'Status');
  if (!col) return { ok: false, error: 'No Status column' };
  sh.getRange(row, col).setValue('Dismissed (was: ' + (r['Status'] || '—') + ')');
  return { ok: true, company: r['Company'] };
}

/**
 * "Not this posting." The X on an OPEN NOW row rejects the specific posting the
 * pipeline matched — the programme itself goes back to the watch pile, and the
 * skip list at the note's tail stops tomorrow's run re-confirming with the
 * same URL. Wholly different from dismissOpeningSoon, which stops tracking.
 */
function skipOpeningSoonPosting(rowNumber, company, url) {
  var sh = sheetOf(SHEET_SOON);
  if (!sh) return { ok: false, error: 'No Opening Soon tab' };
  var head = headerOf(sh);
  var row = Number(rowNumber);
  if (row < 2 || row > sh.getLastRow()) return { ok: false, error: STALE_ROW_ };
  var vals = sh.getRange(row, 1, 1, head.length).getDisplayValues()[0];
  var r = {};
  head.forEach(function (h, i) { r[h] = vals[i]; });
  if (company && companyKey_(r['Company']) !== companyKey_(company)) {
    return { ok: false, error: STALE_ROW_ };
  }
  url = String(url || '').trim();
  if (!url) return { ok: false, error: 'No posting on this row to skip' };
  var prev = String(r['Notes'] || '');
  var m = /\| skip ([^|]+?)\s*$/.exec(prev);
  var list = m ? m[1].split(/\s+/) : [];
  if (list.indexOf(url) < 0) list.push(url);
  var today = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'yyyy-MM-dd');
  var note = 'Not this one: skipped ' + url + ' on ' + today +
             '; still watching for the next posting. | skip ' + list.join(' ');
  sh.getRange(row, colIndex(sh, 'Status')).setValue('Due to open');
  sh.getRange(row, colIndex(sh, 'Notes')).setValue(note);
  r['Status'] = 'Due to open'; r['Notes'] = note; r._row = row;
  return { ok: true, company: r['Company'], card: soonRow(r, 0) };
}

function openingSoon() {
  var rows = openingSoonRows_().sort(function (a, b) {
    var x = parseEu(a['Expected open date']) || parseIso_(a['Expected open date']);
    var y = parseEu(b['Expected open date']) || parseIso_(b['Expected open date']);
    return (x ? x.getTime() : 9e15) - (y ? y.getTime() : 9e15);
  });
  return {
    ok: true,
    count: rows.length,
    html: rows.length
      ? rows.map(function (s, i) { return soonCard(s, i); }).join('')
      : '<p class="empty">Nothing tracked yet — add the first one below.</p>'
  };
}

function parseIso_(s) {
  var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(s || '').trim());
  return m ? new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])) : null;
}

/* ---------------------------------------------------------------- render --- */

/**
 * Words, not markup. Some sources hand back the advert as raw HTML (Reed does),
 * and rows written before that was stripped upstream still hold it, so a card
 * read `<p><strong>&#163;50,000 - &#163;60,000 &#43; Hybrid`. The sheet is never
 * rewritten for old rows, so the reader is protected here as well as at source.
 *
 * Entities are decoded first, twice, because some rows were escaped twice: that
 * turns any escaped tag back into a real one so the tag strip can remove it.
 */
function decodeEntities_(s) {
  return String(s)
    .replace(/&#(\d+);/g, function (_, d) { return String.fromCharCode(Number(d)); })
    .replace(/&#x([0-9a-f]+);/gi, function (_, h) { return String.fromCharCode(parseInt(h, 16)); })
    .replace(/&nbsp;/gi, ' ').replace(/&pound;/gi, '£').replace(/&euro;/gi, '€')
    .replace(/&ndash;/gi, '–').replace(/&mdash;/gi, '—')
    .replace(/&rsquo;|&#8217;/gi, '’').replace(/&lsquo;/gi, '‘')
    .replace(/&hellip;/gi, '…').replace(/&bull;/gi, '•')
    .replace(/&quot;/gi, '"').replace(/&apos;/gi, "'")
    .replace(/&lt;/gi, '<').replace(/&gt;/gi, '>')
    // &amp; last: decoding it first would manufacture entities out of text
    .replace(/&amp;/gi, '&');
}

function stripMarkup_(s) {
  var t = String(s || '');
  if (t.indexOf('<') < 0 && t.indexOf('&') < 0) return t;
  for (var i = 0; i < 2 && t.indexOf('&') >= 0; i++) t = decodeEntities_(t);
  t = t.replace(/<[^>]+>/g, ' ');
  // text that begins part-way through a tag has lost its "<", leaving attributes
  t = t.replace(/(?:[a-zA-Z][a-zA-Z0-9_-]*="[^"]*"\s*)+>?/g, ' ');
  return t.replace(/\s+/g, ' ').trim();
}

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/**
 * Dates reach the dashboard in two shapes: dd/mm/yyyy, which is how the sheet
 * displays a real date cell, and yyyy-mm-dd, which is what the pipeline writes
 * when the cell is plain text. Both are read here so a column's formatting can
 * never decide whether a date is understood.
 */
function parseEu(s) {
  var t = String(s || '').trim();
  var m = /^(\d{2})\/(\d{2})\/(\d{4})$/.exec(t);
  if (m) return new Date(+m[3], +m[2] - 1, +m[1]);
  m = /^(\d{4})-(\d{2})-(\d{2})/.exec(t);
  return m ? new Date(+m[1], +m[2] - 1, +m[3]) : null;
}

/**
 * How long ago a role went up, from the Opens column. "Rolling" only ever said
 * that no deadline was set, which is not the thing you want to know when
 * deciding whether a listing is still worth your time.
 *
 * Vague on purpose past a fortnight: the source dates are themselves rough, and
 * "3 weeks ago" is both truer and easier to act on than "19 days ago".
 */
function postedDays_(s) {
  var d = parseEu(s);
  if (!d) return null;
  var t = new Date(); t.setHours(0, 0, 0, 0);
  var days = Math.round((t - d) / 86400000);
  // Opening Soon promotions overwrite Opens with a future open date, and
  // "posted in -14 days" is nonsense — say nothing rather than something wrong.
  return days < 0 ? null : days;
}

function postedAgo_(s) {
  var days = postedDays_(s);
  if (days === null) return '';
  // No "Posted" prefix: location, duration and this have to sit on one row.
  if (days === 0) return 'Today';
  if (days === 1) return 'Yesterday';
  if (days < 7) return days + ' days ago';
  if (days < 14) return '1 week ago';
  if (days < 61) return Math.round(days / 7) + ' weeks ago';
  if (days < 365) {
    var mo = Math.round(days / 30.44);
    return mo + ' month' + (mo === 1 ? '' : 's') + ' ago';
  }
  return '1 year+ ago';
}

function daysUntil(s) {
  var d = parseEu(s);
  if (!d) return null;
  var t = new Date(); t.setHours(0, 0, 0, 0);
  return Math.round((d - t) / 86400000);
}

// Solid, unmistakable category colours.
// Muted earth tones to sit inside the sage/cream scheme; the old set was
// saturated web-primaries that shouted over it.
var CAT = {
  'Motorsport': '#B4653F', 'AI/ML': '#6E6A8A', 'SWE': '#5C7A6B',
  'MusicTech': '#A05C6E', 'SportsData': '#4F7A70', 'Space': '#5E6C8A',
  'BigTech': '#6E7A5E', 'Other': '#8A857A'
};

function scoreBand(n) {
  if (n >= 85) return { bg: '#55703F', label: 'Excellent' };
  if (n >= 75) return { bg: '#6E7A5E', label: 'Strong' };
  if (n >= 68) return { bg: '#B08344', label: 'Good' };
  return { bg: '#9A948A', label: 'Fair' };
}

/**
 * One or two letters standing in for a company logo in list view. Two initials
 * when the name has two useful words ("Epic Games" -> EG), otherwise one, which
 * keeps the badge readable at 34px rather than cramming in a whole word.
 */
function initialsOf_(name) {
  var words = String(name || '?').trim().split(/[\s\-\/&]+/)
                .filter(function (w) { return /[A-Za-z0-9]/.test(w); });
  if (!words.length) return '?';
  if (words.length === 1) return words[0].charAt(0).toUpperCase();
  return (words[0].charAt(0) + words[1].charAt(0)).toUpperCase();
}

function card(j, idx) {
  var score = Number(j['Score']) || 0;
  var band = scoreBand(score);
  var status = String(j['Status'] || '').trim() || 'Not applied';
  var cat = j['Category'] || 'Other';
  var place = locOf_(j['Location']);
  var fav = isFavourite_(j);
  var priority = String(j['Priority'] || '').toUpperCase() === 'PRIORITY';
  var days = daysUntil(j['Deadline']);
  var urgent = days !== null && days >= 0 && days <= 7;
  var expired = days !== null && days < 0;
  var flagged = String(j['Notes'] || '').indexOf(CLOSED_MARK_) >= 0;
  var closed = expired || flagged;
  var age = postedDays_(j['Opens']);
  var isNew = age !== null && age <= 3;

  var pill = closed ? ['p-rej', 'Closed']
    : status === 'Applied' ? ['p-applied', 'Applied']
    : status === 'OA' ? ['p-live', 'OA']
    : status === 'Interview' ? ['p-live', 'Interview']
    : status === 'Offer' ? ['p-offer', 'Offer']
    : status === 'Rejected' ? ['p-rej', 'Rejected']
    : ['p-not', 'Not applied'];
  var rail = closed || status === 'Rejected' ? 'st-closed'
    : status === 'Applied' ? 'st-wait'
    : status === 'OA' || status === 'Interview' ? 'st-live'
    : status === 'Offer' ? 'st-offer' : 'st-act';

  var opts = STATUSES.map(function (v) {
    return '<option value="' + esc(v) + '"' + (v === status ? ' selected' : '') + '>' +
           esc(v) + '</option>';
  }).join('');

  var meta = [];
  if (j['Opens']) meta.push('<span><b>Opened</b> ' + esc(j['Opens']) + '</span>');
  if (j['Duration']) meta.push('<span><b>Duration</b> ' + esc(j['Duration']) + '</span>');
  if (j['Salary']) meta.push('<span><b>Salary</b> ' + esc(j['Salary']) + '</span>');
  if (j['Deadline']) meta.push('<span><b>Closes</b> ' + esc(j['Deadline']) + '</span>');

  // One table row per role: the collapsed line carries only what the design
  // mock keeps (company over role, status, opened date, score); everything
  // else lives in .det, revealed by the same .expanded class the cards used,
  // so every existing handler keeps working on the same class names.
  return '' +
  '<tr draggable="true" class="shell card r' + (fav ? ' is-fav' : '') +
    (priority ? ' is-top' : '') + (closed ? ' is-closed' : '') + '"' +
    ' data-cat="' + esc(cat) + '" data-row="' + j._row + '"' +
    ' data-city="' + esc(place.city) + '" data-country="' + esc(place.country) + '"' +
    ' data-link="' + esc(j['Link']) + '">' +
   '<td class="railc"><span class="railbar ' + rail + '"></span></td>' +
   '<td class="carc"><span class="car">&#9656;</span></td>' +
   '<td class="rolec"><span class="co">' + esc(j['Company']) +
     (priority ? ' <span class="mini mtop">&#9733; top</span>' : '') +
     (fav ? ' <span class="mini mfav">&#10084;</span>' : '') + '</span>' +
    '<span class="prog">' + esc(j['Role']) +
     (isNew ? ' <span class="mini mnew">NEW</span>' : '') + '</span>' +
    '<div class="det">' +
     (j['Description'] ? '<p class="why">' + esc(stripMarkup_(j['Description'])) + '</p>'
       : '<p class="why nodesc" data-fetch="1">No description yet &mdash; fetched ' +
         'the first time you open this row.</p>') +
     (meta.length ? '<div class="dmeta">' + meta.join('') + '</div>' : '') +
     '<div class="actions">' +
      '<a class="cta" href="' + esc(j['Link']) + '" target="_blank" rel="noopener">' +
        'Open posting<span class="dot">&#8599;</span></a>' +
      '<div class="right">' +
       '<label class="sel" data-status="' + esc(status) + '">' +
         '<select data-row="' + j._row + '">' + opts + '</select></label>' +
       '<button class="heart' + (fav ? ' on' : '') + '" data-row="' + j._row + '"' +
         ' aria-pressed="' + (fav ? 'true' : 'false') + '"' +
         ' title="' + (fav ? 'Unpin from the top of the bank'
                           : 'Pin to the top of the bank') + '"' +
         ' aria-label="Favourite">&#10084;</button>' +
       '<button class="prompt" data-row="' + j._row + '" title="Copy a tailoring prompt ' +
         'for Claude" aria-label="Copy tailoring prompt">&#128203;</button>' +
       '<button class="reachout" data-row="' + j._row + '" title="Find the person and ' +
         'draft outreach" aria-label="Reach out">&#9993;</button>' +
       '<button class="killx" data-row="' + j._row + '" title="Remove this role" ' +
         'aria-label="Remove this role">&#10005;</button>' +
      '</div>' +
     '</div>' +
    '</div></td>' +
   '<td class="statc"><span class="jpill ' + pill[0] + '">' + pill[1] + '</span>' +
     (urgent && !closed ? '<span class="jpill p-urgent">' + days + 'd left</span>' : '') +
   '</td>' +
   '<td class="numc locc">' + (j['Location'] ? esc(j['Location']) : '&mdash;') + '</td>' +
   '<td class="scorec"><span class="score" style="background:' + band.bg + '">' +
     score + '</span></td>' +
  '</tr>';
}

/**
 * One Opening Soon programme as a row in the same table. No select, no heart,
 * no prompt: nothing has been applied to. The dismiss button is .soonx ONLY —
 * .killx would also open the Job Bank removal modal.
 */
function soonRow(s, idx) {
  var status = String(s['Status'] || '');
  var live = status.toUpperCase().indexOf('OPEN NOW') >= 0;
  var gone = /^posting gone/i.test(status);
  // The pipeline writes the proof into Notes, refreshed every run:
  //   Confirmed open on D: found 'Title' at URL | loc L | score N | seen D2
  // That URL is the actual posting — the Link column is only the careers page
  // it was tracked from — and loc/score fill cells this tab has no columns for.
  var note = String(s['Notes'] || '');
  var found = / at (https?:\/\/\S+?)(?=\s*\||\s*$)/.exec(note);
  var postUrl = found ? found[1] : '';
  var mloc = /\| loc ([^|]+?)(?=\s*\||\s*$)/.exec(note);
  var soonLoc = mloc ? mloc[1].trim() : '';
  var msc = /\| score (\d+)/.exec(note);
  var soonScore = msc ? Number(msc[1]) : null;
  // a gone row's stored URL is exactly the dead link — hand back the careers page
  var href = gone ? (s['Link'] || '') : (postUrl || s['Link'] || '');
  // A live row sits in the Act now band next to bank rows that all say "Not
  // applied" — Aug: if it's on the bank page, it reads like the rest. The
  // green rail still says where it came from.
  var pill = live ? ['p-not', 'Not applied']
    : gone ? ['p-rej', 'Posting gone']
    : /due/i.test(status) ? ['p-soon', 'Due to open']
    : /^ *announced/i.test(status) ? ['p-soon', 'Announced']
    : ['p-soon', 'Predicted'];
  var meta = [];
  if (s['Expected open date']) {
    meta.push('<span><b>Expected open</b> ' + esc(s['Expected open date']) + '</span>');
  }
  if (s['Expected close date']) {
    meta.push('<span><b>Closes</b> ' + esc(s['Expected close date']) + '</span>');
  }
  meta.push('<span><b>Tracked</b> Opening soon</span>');
  return '' +
  '<tr class="shell card r" data-cat="Other" data-row="' + s._row + '"' +
    ' data-company="' + esc(s['Company']) + '" data-link="' + esc(href) + '">' +
   '<td class="railc"><span class="railbar ' + (live ? 'st-open' : 'st-watch') +
     '"></span></td>' +
   '<td class="carc"><span class="car">&#9656;</span></td>' +
   '<td class="rolec"><span class="co">' + esc(s['Company']) + '</span>' +
    '<span class="prog">' + esc(s['Programme']) + '</span>' +
    '<div class="det">' +
     (s['Notes'] ? '<p class="why">' + esc(stripMarkup_(s['Notes'])) + '</p>'
                 : '<p class="why nodesc">No notes</p>') +
     (meta.length ? '<div class="dmeta">' + meta.join('') + '</div>' : '') +
     '<div class="actions">' +
      (href ? '<a class="cta" href="' + esc(href) + '" target="_blank" ' +
        'rel="noopener">' + (postUrl && !gone ? 'Open the posting'
          : 'Open careers page') +
        '<span class="dot">&#8599;</span></a>' : '') +
      (postUrl && !gone && s['Link'] ? '<a class="jbtn" href="' + esc(s['Link']) +
        '" target="_blank" rel="noopener">Careers page</a>' : '') +
      '<div class="right">' +
       (live && postUrl
         ? '<button class="jbtn soonstop" data-row="' + s._row + '"' +
             ' data-company="' + esc(s['Company']) + '">Stop tracking</button>' +
           '<button class="skipx" data-row="' + s._row + '"' +
             ' data-company="' + esc(s['Company']) + '" data-url="' + esc(postUrl) + '"' +
             ' title="Not this posting — keep watching ' + esc(s['Company']) + '"' +
             ' aria-label="Not this posting">&#10005;</button>'
         : '<button class="soonx" data-row="' + s._row + '"' +
             ' data-company="' + esc(s['Company']) + '" title="Stop tracking this ' +
             'programme" aria-label="Stop tracking">&#10005;</button>') +
      '</div>' +
     '</div>' +
    '</div></td>' +
   '<td class="statc"><span class="jpill ' + pill[0] + '">' + pill[1] + '</span>' +
     (s['Expected open date'] && !live && !gone ? '<span class="soond">' +
       esc(s['Expected open date']) + '</span>' : '') + '</td>' +
   '<td class="numc locc">' + (soonLoc ? esc(soonLoc) : '&mdash;') + '</td>' +
   (soonScore !== null
     ? '<td class="scorec"><span class="score" style="background:' +
       scoreBand(soonScore).bg + '">' + soonScore + '</span></td>'
     : '<td class="numc dim">watch</td>') +
  '</tr>';
}


/**
 * "More roles" — the second page, browsing the top 50 archived roles (below
 * min_score or over the weekly cap) rather than only what made the Job Bank.
 * No status dropdown or remove button: these rows live on the Archive sheet and
 * their row numbers don't correspond to Job Bank rows. Prompt/Reach-out are
 * passed data-sheet="Archive" so the server reads the right sheet; "+" promotes
 * the role into the bank.
 */
function archiveCard(j, idx) {
  var score = Number(j['Score']) || 0;
  var band = scoreBand(score);
  var cat = j['Category'] || 'Other';
  var colour = CAT[cat] || CAT.Other;
  var place = locOf_(j['Location']);

  var meta = [];
  if (j['Location']) meta.push('<span><b>Location</b> ' + esc(j['Location']) + '</span>');
  if (j['Salary']) meta.push('<span><b>Salary</b> ' + esc(j['Salary']) + '</span>');
  if (j['Deadline']) meta.push('<span><b>Closes</b> ' + esc(j['Deadline']) + '</span>');

  return '' +
  '<tr class="shell card r" data-cat="' + esc(cat) + '" data-row="' + j._row + '"' +
    ' data-city="' + esc(place.city) + '" data-country="' + esc(place.country) + '"' +
    ' data-link="' + esc(j['Link']) + '">' +
   '<td class="railc"><span class="railbar" style="background:' + colour + '"></span></td>' +
   '<td class="carc"><span class="car">&#9656;</span></td>' +
   '<td class="rolec"><span class="co">' + esc(j['Company']) + '</span>' +
    '<span class="prog">' + esc(j['Role']) + '</span>' +
    '<div class="det">' +
     (j['Description'] ? '<p class="why">' + esc(stripMarkup_(j['Description'])) + '</p>'
       : '<p class="why nodesc" data-fetch="1">No description</p>') +
     (meta.length ? '<div class="dmeta">' + meta.join('') + '</div>' : '') +
     '<div class="actions">' +
      '<a class="cta" href="' + esc(j['Link']) + '" target="_blank" rel="noopener">' +
        'Open posting<span class="dot">&#8599;</span></a>' +
      '<div class="right">' +
       '<button class="addbank" data-row="' + j._row + '" ' +
         'title="Add this role to the Job Bank" aria-label="Add to Job Bank">+</button>' +
       '<button class="prompt" data-row="' + j._row + '" data-sheet="Archive" ' +
         'title="Copy a tailoring prompt for Claude" aria-label="Copy tailoring prompt">&#128203;</button>' +
       '<button class="reachout" data-row="' + j._row + '" data-sheet="Archive" ' +
         'title="Find the person and draft outreach" aria-label="Reach out">&#9993;</button>' +
       '<button class="killx" data-row="' + j._row + '" data-sheet="Archive" ' +
         'title="Remove this role" aria-label="Remove this role">&#10005;</button>' +
      '</div>' +
     '</div>' +
    '</div></td>' +
   '<td class="statc"><span class="jpill p-cat" style="color:' + colour + '">' +
     esc(cat) + '</span></td>' +
   '<td class="numc">' + (j['Added'] ? esc(j['Added']) : '&mdash;') + '</td>' +
   '<td class="scorec"><span class="score" style="background:' + band.bg + '">' +
     score + '</span></td>' +
  '</tr>';
}


/**
 * The Job Bank is grouped by where a role has actually got to, in the order the
 * week should be spent: an interview on Thursday matters more than a role not
 * started yet, and roles you are waiting to hear about should not sit in the
 * middle of the list you are working through.
 */
var BUCKETS = [
  { key: 'apply', label: 'Act now', hint: 'open, not yet applied',
    blank: 'Nothing waiting — everything here has been actioned.',
    statuses: ['Not applied', ''] },
  { key: 'waiting', label: 'In flight', hint: 'applied &middot; OA &middot; interview &middot; offer',
    blank: 'Nothing sent yet. Mark a role Applied once you have submitted it.',
    statuses: ['Applied', 'OA', 'Interview', 'Offer'] },
  { key: 'closed', label: 'Closed out', hint: 'kept for the record',
    blank: 'Nothing rejected yet.',
    statuses: ['Rejected'] }
];

function bucketOf_(status) {
  var s = String(status || '').trim() || 'Not applied';
  for (var i = 0; i < BUCKETS.length; i++) {
    if (BUCKETS[i].statuses.indexOf(s) >= 0) return BUCKETS[i].key;
  }
  return 'apply';
}

/**
 * One search hit. Deliberately lighter than a Job Bank card: this is a decision
 * surface ("is this worth keeping?"), not somewhere to live with a role.
 */
function searchCard(r, idx) {
  var score = Number(r.score) || 0;
  var band = scoreBand(score);
  var where = [r.location, r.duration].filter(function (x) { return x; }).join(' · ');
  var tag = r.inBank ? '<span class="pill pill-green">In your Job Bank</span>'
    : r.dismissed ? '<span class="pill pill-grey">You removed this before</span>'
    : r.archived ? '<span class="pill pill-grey">Seen by the pipeline</span>'
    : '<span class="pill pill-blue">' + esc(r.source || 'live') + '</span>';
  return '' +
  '<article class="hit" style="--i:' + idx + '">' +
    '<span class="score" style="background:' + band.bg + '">' + score +
      '<em>' + band.label + '</em></span>' +
    '<div class="hitmain">' +
      '<h4>' + (r.link
        ? '<a href="' + esc(r.link) + '" target="_blank" rel="noopener">' + esc(r.role) + '</a>'
        : esc(r.role)) + '</h4>' +
      '<p class="co">' + esc(r.company) + (where ? ' · ' + esc(where) : '') + '</p>' +
      (r.reason ? '<p class="why">' + esc(r.reason) + '</p>' : '') +
      '<div class="pills">' + tag + '</div>' +
    '</div>' +
    (r.inBank ? '<span class="added">&#10003;</span>'
      : '<button class="addbank hitadd" data-i="' + idx + '" ' +
        'title="Add to the Job Bank" aria-label="Add to Job Bank">+</button>') +
  '</article>';
}

var MONTHS_ = ['January', 'February', 'March', 'April', 'May', 'June', 'July',
               'August', 'September', 'October', 'November', 'December'];

/**
 * One tracked programme that has not opened yet.
 *
 * Only a date somebody actually announced is shown as a date. Everything else in
 * this tab is a guess — the seed labels most rows "Predicted from last year",
 * which in practice meant picking a likely month and writing the 1st of it.
 * Rendering that as "Opens in 13 days" states a day nobody ever published.
 *
 * The guessed date still earns its keep: it is when the pipeline starts looking
 * for the real posting. It is simply shown as the month it is, and hedged.
 */
function soonCard(s, idx) {
  var open = String(s['Expected open date'] || '');
  var d = parseIso_(open) || parseEu(open);
  var status = String(s['Status'] || '');
  var live = status.toUpperCase().indexOf('OPEN NOW') >= 0;
  var confirmed = /^ *announced/i.test(status);
  // A date is only shown when something backs it: either the employer announced
  // one, or the Notes record what the estimate rests on (they start "Basis:").
  // Anything else has no date on the card at all -- an invented month dressed up
  // as an estimate is still us making it up.
  var basis = String(s['Notes'] || '').trim();
  var grounded = /^basis:/i.test(basis);
  var days = d ? Math.ceil((d.getTime() - new Date().setHours(0, 0, 0, 0)) / 86400000) : null;
  var month = d ? MONTHS_[d.getMonth()] +
    (d.getFullYear() !== new Date().getFullYear() ? ' ' + d.getFullYear() : '') : '';
  var when = live ? '<span class="pill pill-green">Open now — go and apply</span>'
    : (days === null || !(confirmed || grounded))
      ? '<span class="pill pill-grey">No date yet</span>'
    : days <= 0
      ? (confirmed
          ? '<span class="pill pill-green">Due to open — check it</span>'
          : '<span class="pill pill-est">Expected by now — worth a check</span>')
      : confirmed
        ? (days <= 31
            ? '<span class="pill pill-red">Opens in ' + days + ' day' + (days === 1 ? '' : 's') + '</span>'
            : '<span class="pill pill-grey">Opens ' + esc(open) + '</span>')
        : '<span class="pill pill-est">Expected ' + esc(month) + '</span>';
  return '' +
  '<article class="shell soonshell' + (live ? ' is-top' : '') + '" data-row="' + s._row + '"' +
  ' data-company="' + esc(s['Company']) + '" style="--i:' + idx + '">' +
   '<div class="card">' +
    '<div class="stripe" style="background:' + (live ? '#2F9E44' : '#868E96') + '"></div>' +
    '<button class="killx soonx" data-row="' + s._row + '" ' +
      'data-company="' + esc(s['Company']) + '" title="Stop tracking this" ' +
      'aria-label="Stop tracking">&#10005;</button>' +
    '<div class="inner">' +
      '<div class="row1">' + when +
        (String(s['Status'] || '').indexOf('hand') >= 0
          ? '<span class="tag tag-top">Yours</span>' : '') +
      '</div>' +
      '<div class="titleblock">' +
        '<p class="co">' + esc(s['Company']) + '</p>' +
        '<h3>' + (s['Link']
          ? '<a href="' + esc(s['Link']) + '" target="_blank" rel="noopener">' +
            esc(s['Programme']) + '</a>'
          : esc(s['Programme'])) + '</h3>' +
      '</div>' +
      // just the dates: the raw Status ("Auto-flagged OPEN NOW on ... (was: ...)")
      // is pipeline bookkeeping, and the open/closed state is already the top pill
      ((s['Expected close date'])
        ? '<div class="pills"><span class="pill pill-grey">Closes ' +
          esc(s['Expected close date']) + '</span></div>' : '') +
      (s['Notes'] ? '<p class="why">' + esc(stripMarkup_(s['Notes'])) + '</p>'
        : '<p class="why nodesc">No notes</p>') +
    '</div>' +
   '</div>' +
  '</article>';
}

/**
 * Hide an archived role from More roles. Nothing is deleted — the Archive is the
 * permanent record, and the pipeline's own dedupe reads it — so this only marks
 * the disposition, which moreRoles() already filters on.
 */
function dismissArchived(archiveRow, reason, link) {
  var arch = sheetOf(SHEET_ARCHIVE);
  if (!arch) return { ok: false, error: 'No Archive sheet' };
  var col = colIndex(arch, 'Disposition');
  if (!col) return { ok: false, error: 'No Disposition column' };
  archiveRow = resolveRow_(arch, archiveRow, link);
  if (!archiveRow) return { ok: false, error: STALE_ROW_ };
  var head = headerOf(arch);
  var vals = arch.getRange(archiveRow, 1, 1, head.length).getDisplayValues()[0];
  var r = {};
  head.forEach(function (h, i) { r[h] = vals[i]; });
  var disp = String(r['Disposition'] || '');
  if (disp.indexOf('removed') < 0) {
    arch.getRange(archiveRow, col).setValue(disp + ' | removed');
  }
  logFeedback_('removed', r, reason);
  return { ok: true, removed: r['Company'] };
}

/**
 * TikTok posts the same internship a dozen times over — 24 "software engineer"
 * variants and 10 "backend software engineer" ones were sitting in More roles.
 * Dedupe cannot collapse them because the titles genuinely differ, but seeing
 * more than a few of one role at one company is noise, not choice.
 *
 * The limit is per company AND role family, so one employer may still show 3
 * software engineer roles, 3 machine learning roles and 3 data science ones —
 * those are different jobs. What it stops is the same job listed once per team.
 *
 * Strips the parts that vary between near-identical postings (year, season,
 * bracketed team names, the word "intern") and keeps what is left as the key.
 */
var MAX_SAME_ROLE = 3;

/**
 * The role FAMILY, not the exact title. TikTok posts one internship per team —
 * "Backend Software Engineer Intern (TikTok-Search)", "...(Privacy and
 * Security)", "...- Global E-Commerce" — which are the same job to anyone
 * deciding whether to apply. Matching on the cleaned title kept all 24, because
 * the team names genuinely differ; matching on the family collapses them.
 *
 * Falls back to the de-noised word set for anything unrecognised, so a role this
 * list has never seen is still deduped against its own kind rather than dropped.
 */
var ROLE_FAMILIES_ = [
  [/\b(machine learning|deep learning|nlp|computer vision|llm)\b/, 'ml engineer'],
  [/\b(ml|ai)\b.*\b(engineer|scientist|infra|infrastructure)\b/, 'ml engineer'],
  [/\b(data scien(ce|tist))\b/, 'data scientist'],
  [/\bdata (engineer|engineering)\b/, 'data engineer'],
  [/\b(data|business|quantitative) analyst\b/, 'data analyst'],
  [/\b(software|backend|back end|frontend|front end|fullstack|full stack|mobile|ios|android|web|platform|systems?|test|qa|sde|swe)\b.*\bengineer|engineer.*\b(software|backend|frontend|fullstack)\b|\bsoftware develop/, 'software engineer'],
  [/\b(infrastructure|devops|sre|site reliability|cloud)\b/, 'infrastructure engineer'],
  [/\b(security|privacy)\b.*\bengineer\b/, 'security engineer'],
  [/\bproduct (manager|management|owner)\b/, 'product manager'],
  [/\b(designer|design|ux|ui)\b/, 'designer'],
  [/\bresearch(er|)\b.*\b(scientist|engineer|intern)\b|\bresearch scientist\b/, 'researcher'],
  [/\b(mechanical|mechatronic)\b/, 'mechanical engineer'],
  [/\b(thermofluid|thermal|aerodynamic|aero|fluid|cfd|combustion)\b/, 'thermofluids engineer'],
  [/\b(powertrain|engine|propulsion|motor|turbine|drivetrain)\b/, 'powertrain engineer'],
  [/\b(electrical|electronic|electronics|power)\b.*\bengineer\b/, 'electrical engineer'],
  [/\b(manufactur|production|process)\w*\b/, 'manufacturing engineer'],
  [/\b(consult|strategy|operations|program|project)\w*\b/, 'business'],
  [/\b(marketing|sales|content|communications|brand)\b/, 'marketing']
];
// the words that differ between repostings of one job, not between jobs
var NOISE_RE_ = /\b(20\d\d|summer|winter|spring|fall|autumn|interns?|internships?|placements?|graduate|programme|program|new grad|early careers?|apprentice|trainee|general hire)\b/g;

function roleKey_(title) {
  var raw = String(title || '').toLowerCase().replace(/[^a-z0-9 ]/g, ' ');
  raw = raw.replace(/\s+/g, ' ').trim();
  for (var i = 0; i < ROLE_FAMILIES_.length; i++) {
    if (ROLE_FAMILIES_[i][0].test(raw)) return ROLE_FAMILIES_[i][1];
  }
  var t = raw.replace(NOISE_RE_, ' ');
  var seen = {}, out = [];
  t.split(/\s+/).filter(Boolean).sort().forEach(function (w) {
    if (!seen[w]) { seen[w] = 1; out.push(w); }
  });
  return out.join(' ').slice(0, 60);
}

/** The same title with different punctuation is the same posting, not a choice. */
function titleKey_(title) {
  return String(title || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
}

/** Keep at most `limit` of any one company+role. Expects best-first order. */
function capSimilar_(rows, limit, companyOf, roleOf) {
  var counts = {}, seen = {}, out = [];
  rows.forEach(function (r) {
    var co = companyKey_(companyOf(r));
    var exact = co + '||' + titleKey_(roleOf(r));
    if (seen[exact]) return;            // literally the same role, listed twice
    seen[exact] = 1;
    var k = co + '|' + roleKey_(roleOf(r));
    counts[k] = (counts[k] || 0) + 1;
    if (counts[k] <= (limit || MAX_SAME_ROLE)) out.push(r);
  });
  return out;
}

/** Top 50 archived roles by score, minus ones already removed or promoted. */
function moreRoles() {
  var rows = recentArchive_().filter(function (r) {
    var disp = String(r['Disposition'] || '');
    return disp.indexOf('removed') < 0 && disp.indexOf('promoted') < 0
      && r['Company'] && r['Role'] && r['Link'];
  });
  // Score order, full stop. Whether a description could be scraped says nothing
  // about how good the role is and must never influence ranking.
  rows.sort(function (a, b) { return (Number(b['Score']) || 0) - (Number(a['Score']) || 0); });
  // best-scoring few of each company+role; the rest are the same job reposted
  rows = capSimilar_(rows, MAX_SAME_ROLE,
                     function (r) { return r['Company']; },
                     function (r) { return r['Role']; });
  var top = rows.slice(0, 50);
  return {
    ok: true,
    count: top.length,
    filters: top.length ? filterControls_('moreList') : '',
    html: top.length ?
      '<div class="jbtwrap"><table class="jbt"><thead><tr>' +
      '<th class="railh"></th><th class="carh"></th><th>Role</th><th>Category</th>' +
      '<th>Added</th><th class="scoreh">Score</th></tr></thead>' +
      '<tbody class="bucket">' + top.map(archiveCard).join('') + '</tbody>' +
      '</table></div>' :
      '<p class="empty">Nothing archived yet.</p>'
  };
}

/**
 * Split a free-text location into the two halves the filter groups by: a
 * country, and a city within it.
 *
 * Reads the same COUNTRY / CITY_COUNTRY / US_STATES / CA_PROV tables that
 * normalizeLocation uses, rather than keeping a second opinion about where
 * places are — the first version of this carried its own shorter lists and put
 * San Francisco under "Other" because they didn't mention it.
 */
function partKey_(s) {
  return String(s || '').toLowerCase().replace(/[.\s]+$/, '').trim();
}

/**
 * Levenshtein, abandoned early when the lengths are too far apart to matter.
 * Used only to rescue an obvious misspelling of a place we already know, never
 * to guess between two places that genuinely differ.
 */
function editDist_(a, b) {
  if (Math.abs(a.length - b.length) > 2) return 99;
  var prev = [], cur = [], i, j;
  for (j = 0; j <= b.length; j++) prev[j] = j;
  for (i = 1; i <= a.length; i++) {
    cur = [i];
    for (j = 1; j <= b.length; j++) {
      cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1,
                        prev[j - 1] + (a.charAt(i - 1) === b.charAt(j - 1) ? 0 : 1));
    }
    prev = cur;
  }
  return prev[b.length];
}

/**
 * The key in `map` that `word` was probably trying to be, or ''.
 *
 * Short words are matched exactly and never fuzzily: at four characters or
 * fewer one edit is the distance between two real, different places — "ca" and
 * "ma" are both states, "cork" and "york" are both cities. A tie is treated as
 * no match, because two equally-close answers means we do not know which.
 */
function fuzzyKey_(word, map) {
  var w = partKey_(word);
  if (!w) return '';
  if (Object.prototype.hasOwnProperty.call(map, w)) return w;
  if (w.length <= 4) return '';
  var budget = w.length <= 8 ? 1 : 2;
  var best = '', bestD = 99, ties = 0;
  Object.keys(map).forEach(function (k) {
    var d = editDist_(w, k);
    if (d > budget) return;
    if (d < bestD) { bestD = d; best = k; ties = 1; }
    else if (d === bestD) { ties++; }
  });
  return ties === 1 ? best : '';
}

function titleCase_(s) {
  return String(s).replace(/\b[a-z]/g, function (c) { return c.toUpperCase(); });
}

function locOf_(loc) {
  var raw = String(loc || '').replace(/\s+/g, ' ').trim();
  if (!raw) return { city: '', country: '' };
  var remote = /\b(remote|anywhere|work from home|wfh)\b/i.test(raw);
  var parts = raw.split(/[,;|\/]|\s+[-–—]\s+|\s+or\s+/i)
    .map(function (p) {
      return p.replace(/\(.*$/, '').replace(/^[\s\-–—]+|[\s\-–—]+$/g, '').trim();
    })
    .filter(function (p) { return p && !/^(remote|hybrid|on-?site|flexible)$/i.test(p); });

  // the country is named at the end when it is named at all
  var country = '';
  for (var i = parts.length - 1; i >= 0 && !country; i--) {
    var k = partKey_(parts[i]);
    if (COUNTRY[k]) country = COUNTRY[k];
    else if (US_STATES.indexOf(k) >= 0) country = 'USA';
    else if (CA_PROV.indexOf(k) >= 0) country = 'Canada';
    else { var f = fuzzyKey_(k, COUNTRY); if (f) country = COUNTRY[f]; }
    if (country) parts = parts.slice(0, i);
  }

  var city = '';
  for (var n = 0; n < parts.length && !city; n++) {
    var key = partKey_(parts[n]);
    if (COUNTRY[key] || US_STATES.indexOf(key) >= 0 || CA_PROV.indexOf(key) >= 0) continue;
    if (/^(?=.*\d)(?=.*[A-Za-z])[A-Za-z0-9 ]{4,9}$/.test(parts[n])) continue;  // postcode
    if (/^\d+$/.test(parts[n])) continue;
    if (parts[n].length > 1) city = parts[n];
  }

  // A known city names its own country, and answers to its correct spelling —
  // so "San Fransisco" and "San Francisco" are one entry in the filter, not two.
  if (city) {
    var hit = fuzzyKey_(city, CITY_COUNTRY);
    if (hit) {
      city = titleCase_(hit);
      if (!country) country = CITY_COUNTRY[hit];
    }
  }
  if (!country) country = remote ? 'Remote' : 'Other';
  if (!city) city = remote ? 'Remote' : country;
  return { city: city, country: country };
}

/**
 * The two filter controls, role type and location. Both are filled in
 * client-side from the cards actually on screen, so neither can offer something
 * nothing matches, and the counts follow whichever status tab is open.
 */
function filterControls_(target) {
  function drop(kind, label) {
    return '<div class="fdrop" data-kind="' + kind + '" data-target="' + target + '">' +
      '<button class="fbtn" type="button" aria-expanded="false">' +
        '<span class="flabel">' + label + '</span>' +
        '<span class="fval">All</span><span class="fchev">&#9662;</span></button>' +
      '<div class="fpanel"><div class="fslide">' +
        '<div class="flevel froot"></div><div class="flevel fsub"></div>' +
      '</div><div class="ffoot"><button class="fclear" type="button">Clear</button>' +
      '<span class="fnum"></span></div></div></div>';
  }
  return drop('cat', 'Role type') + drop('loc', 'Location');
}

/* ---------------------------------------------------------- closed listings --- */
/**
 * A posting that has come down is worse than useless: it sits in the bank
 * looking like work still to do. Two ways of spotting one —
 *
 *  1. its deadline has passed, which needs no network at all and is applied
 *     when the card renders;
 *  2. the page itself says so, which is what checkClosed() goes and reads.
 *
 * Only a confident answer is recorded. A site that blocks us, rate-limits us or
 * times out is reported as "could not check" rather than being marked dead —
 * quietly striking a live role off the list would be the worse error.
 */
var CLOSED_MARK_ = '[closed';
var CLOSED_PHRASES_ = [
  'no longer accepting application', 'we are no longer accepting',
  'this job is no longer available', 'this position is no longer available',
  'the position has been filled', 'this role has been filled',
  'applications are closed', 'applications have closed',
  'application deadline has passed', 'this posting has expired',
  'job posting is closed', 'this vacancy has closed', 'vacancy is closed',
  'this job has expired', 'no longer open for application'
];

/** Append a marker to the human-owned Notes column; never overwrite what is there. */
function markClosed_(sh, row, why) {
  var col = colIndex(sh, 'Notes');
  if (!col) return;
  var cur = String(sh.getRange(row, col).getDisplayValue() || '');
  if (cur.indexOf(CLOSED_MARK_) >= 0) return;
  var mark = CLOSED_MARK_ + ' ' + fmtDate_(new Date(), 'yyyy-MM-dd') + ' — ' + why + ']';
  sh.getRange(row, col).setValue(cur ? (cur + ' ' + mark) : mark);
}

function checkClosed(rowNumber, sheetName, link) {
  var sh = sheetOf(sheetName || SHEET_JOBS);
  if (!sh) return { ok: false, error: 'No sheet' };
  var row = resolveRow_(sh, rowNumber, link);
  if (!row) return { ok: false, error: STALE_ROW_ };
  var url = String(link || '').trim();
  if (!url) return { ok: true, state: 'unknown', reason: 'no link to check' };

  var res;
  try {
    res = UrlFetchApp.fetch(url, {
      muteHttpExceptions: true, followRedirects: true,
      headers: {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' +
                      '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
      }
    });
  } catch (err) {
    return { ok: true, state: 'unknown', reason: 'could not be reached' };
  }

  var code = res.getResponseCode();
  if (code === 404 || code === 410) {
    markClosed_(sh, row, 'page gone (' + code + ')');
    return { ok: true, state: 'closed', reason: 'the page is gone (' + code + ')' };
  }
  // 403 and 429 mean the site turned US away, which says nothing about the role
  if (code !== 200) return { ok: true, state: 'unknown', reason: 'site answered ' + code };

  var text = String(res.getContentText() || '')
    .replace(/<script[\s\S]*?<\/script>/gi, ' ')
    .replace(/<style[\s\S]*?<\/style>/gi, ' ')
    .replace(/<[^>]+>/g, ' ')
    .replace(/\s+/g, ' ')
    .toLowerCase();
  for (var i = 0; i < CLOSED_PHRASES_.length; i++) {
    if (text.indexOf(CLOSED_PHRASES_[i]) >= 0) {
      markClosed_(sh, row, 'posting says "' + CLOSED_PHRASES_[i] + '"');
      return { ok: true, state: 'closed', reason: 'the posting says it is closed' };
    }
  }
  return { ok: true, state: 'open' };
}

/* ------------------------------------------------- more roles: hard refresh --- */
/**
 * "Go and look again": re-runs the live search over every company on the
 * Watchlist, scores what comes back, and files anything genuinely new into the
 * Archive so More roles can surface it.
 *
 * Split into one call per company on purpose. Apps Script stops a single
 * google.script.run well before a twenty-company sweep could finish, so the
 * client drives the loop — which also means the progress bar is reporting work
 * that has actually happened rather than animating a guess.
 *
 * This reaches what the DASHBOARD can reach: each company's Greenhouse/Lever/
 * Ashby board, plus Adzuna. It is not the nightly pipeline and does not see
 * Trackr, JobSpy or Gradcracker.
 */
function refreshMorePlan() {
  var seen = {}, names = [];
  rowsOf(SHEET_WATCH).forEach(function (r) {
    var c = String(r['Company'] || '').trim();
    if (!c) return;
    var k = companyKey_(c);
    if (seen[k]) return;
    seen[k] = 1;
    names.push(c);
  });
  if (!names.length) {
    return { ok: false, error: 'Your Watchlist has no companies on it, so there is ' +
                               'nothing to re-search.' };
  }
  var props = PropertiesService.getScriptProperties();
  return {
    ok: true,
    companies: names,
    aiConfigured: !!props.getProperty('ANTHROPIC_API_KEY'),
    adzunaConfigured: !!(props.getProperty('ADZUNA_APP_ID') &&
                         props.getProperty('ADZUNA_APP_KEY'))
  };
}

/** Every link already in the Archive, so the sweep never files the same role twice. */
function archiveLinkSet_() {
  var arch = sheetOf(SHEET_ARCHIVE);
  var out = {};
  if (!arch) return out;
  var last = arch.getLastRow();
  var col = colIndex(arch, 'Link');
  if (!col || last < 2) return out;
  arch.getRange(2, col, last - 1, 1).getDisplayValues().forEach(function (r) {
    var k = normUrl_(r[0]);
    if (k) out[k] = 1;
  });
  return out;
}

/**
 * One company. Returns what it found so the page can keep a running count.
 * Never throws: a careers page that is down must not end the whole sweep.
 */
function refreshMoreOne(company) {
  var name = String(company || '').trim();
  if (!name) return { ok: false, error: 'No company given' };
  try {
    var res = searchCompany(name, false, '');
    if (!res || !res.ok) {
      return { ok: false, company: name, error: (res && res.error) || 'Search failed' };
    }
    var known = archiveLinkSet_();
    var added = 0;
    (res.results || []).forEach(function (r) {
      if (r.inBank || r.archived || r.dismissed) return;
      var k = normUrl_(r.link);
      if (!k || known[k]) return;
      known[k] = 1;
      archive_({
        'Score': Math.round(Number(r.score) || 0),
        'Category': r.category || 'Other',
        'Company': r.company, 'Role': r.role,
        'Location': normalizeLocation(String(r.location || '')),
        'Deadline': r.deadline || '', 'Duration': r.duration || '',
        'Link': r.link, 'Source': r.source || 'hard refresh',
        'Description': String(r.description || '').slice(0, 1200)
      }, 'found by refresh');
      added++;
    });
    return { ok: true, company: name, found: (res.results || []).length, added: added };
  } catch (err) {
    return { ok: false, company: name, error: String(err && err.message || err) };
  }
}

/* ------------------------------------------------------------- statistics --- */
/**
 * Everything here is derived from the Feedback sheet, which has recorded a dated
 * row every time a role moved forward ('pursued', reason "status -> Applied")
 * or was removed. That is the only history the system keeps: the Job Bank holds
 * a role's status but not when it changed, so nothing here can predate the day
 * feedback logging started. The page says so rather than drawing a flat line
 * and letting it read as "you did nothing".
 */
var STAGE_OF_ = { 'Applied': 'applied', 'OA': 'oa', 'Interview': 'interview',
                  'Offer': 'offer' };
// How far through a role has got. Used to throw out history a role has since
// walked back — see reconciliation in statsData below.
var STAGE_RANK_ = { '': 0, 'Not applied': 0, 'Applied': 1, 'OA': 2,
                    'Interview': 3, 'Offer': 4 };

function weekStart_(d) {
  var x = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  x.setDate(x.getDate() - ((x.getDay() + 6) % 7));   // Monday
  return x;
}

function fmtDate_(d, pattern) {
  return Utilities.formatDate(d, Session.getScriptTimeZone(), pattern);
}

/** Feedback dates are written as yyyy-MM-dd; anything else is not a date. */
function parseStatsDate_(s) {
  var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(s || '').trim());
  return m ? new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])) : null;
}

/** The status a 'pursued' row records, e.g. "status -> OA" -> "OA". */
function stageLabelOf_(r) {
  var m = /status\s*->\s*(.+)$/i.exec(String(r['Reason'] || ''));
  return m ? m[1].trim() : '';
}

/**
 * days: 30 / 365 / 0 for everything. Buckets are weekly for a short range and
 * monthly for a long one — 52 grouped week-bars in a year view is a smear, not
 * a chart.
 */
function statsData(days) {
  days = Number(days) || 0;
  var rows = sheetOf(SHEET_FEEDBACK) ? rowsOf(SHEET_FEEDBACK) : [];
  var today = new Date(); today.setHours(0, 0, 0, 0);
  var cutoff = null;
  if (days > 0) {
    cutoff = new Date(today.getTime());
    cutoff.setDate(cutoff.getDate() - days);
  }

  var monthly = !(days > 0 && days <= 93);
  function bucketOfDate(d) { return monthly ? new Date(d.getFullYear(), d.getMonth(), 1)
                                            : weekStart_(d); }

  // Feedback is an append-only event log, which is right for the scorer and
  // wrong for a count: setting a role to OA and putting it straight back left
  // "Tests 1" on a page where no test ever happened. So the log is reconciled
  // against where each role actually stands before anything is counted.
  var current = {};
  rowsOf(SHEET_JOBS).forEach(function (j) {
    var id = String(j['Link'] || '').trim() ||
             (String(j['Company'] || '') + '|' + String(j['Role'] || ''));
    var st = String(j['Status'] || '').trim();
    current[id] = { rank: STAGE_RANK_.hasOwnProperty(st) ? STAGE_RANK_[st] : 0,
                    rejected: st === 'Rejected' };
  });

  // ...and where a role that has LEFT the bank finally got to, which is the
  // half the bank cannot answer. Checking the bank alone let a walked-back
  // stage come back the moment the row was deleted: a role set to OA, put
  // straight back to Applied the same day and removed a day later still
  // reported a test, because once the row was gone there was nothing left to
  // check the claim against. The log knows — it recorded the walk-back.
  //
  // The LAST stage logged, not the highest: the highest is the claim being
  // tested. Only stage events count, so a later "removed" row cannot stand in
  // for one, and Rejected is never logged as a stage at all.
  var lastLogged = {};
  rows.forEach(function (r) {
    if (String(r['Signal'] || '') === 'removed') return;
    var label = stageLabelOf_(r);
    if (!STAGE_OF_[label]) return;
    var id = String(r['Link'] || '').trim() ||
             (String(r['Company'] || '') + '|' + String(r['Role'] || ''));
    lastLogged[id] = STAGE_RANK_[label] || 0;
  });

  var buckets = {}, earliest = null, totals = { applied: 0, oa: 0, interview: 0,
                                                offer: 0, removed: 0 };
  var everSeen = false, counted = {};

  rows.forEach(function (r) {
    var d = parseStatsDate_(r['Date']);
    if (!d) return;
    everSeen = true;
    if (!earliest || d < earliest) earliest = d;
    if (cutoff && d < cutoff) return;
    var key = fmtDate_(bucketOfDate(d), 'yyyy-MM-dd');
    if (!buckets[key]) {
      buckets[key] = { applied: 0, oa: 0, interview: 0, offer: 0, removed: 0 };
    }
    if (String(r['Signal'] || '') === 'removed') {
      buckets[key].removed++; totals.removed++;
      return;
    }
    var label = stageLabelOf_(r);
    var stage = STAGE_OF_[label];
    if (!stage) return;

    var id = String(r['Link'] || '').trim() ||
             (String(r['Company'] || '') + '|' + String(r['Role'] || ''));
    // One role reaching one stage is one event, however many times the dropdown
    // was nudged through it.
    if (counted[id + '|' + stage]) return;

    // A role that now sits BELOW this stage never really got here — the status
    // was undone. "Now" means its Status if it is still in the bank, and the
    // last stage the log recorded if it has been removed. Rejected is an
    // ending rather than an undoing (you can be rejected after a real
    // interview), so a rejected role keeps everything it reached.
    var now = current[id];
    var rank = now ? now.rank
                   : (lastLogged.hasOwnProperty(id) ? lastLogged[id] : null);
    if (rank !== null && !(now && now.rejected) &&
        rank < (STAGE_RANK_[label] || 0)) return;

    counted[id + '|' + stage] = 1;
    buckets[key][stage]++; totals[stage]++;
  });

  // Empty periods must appear as zero, not be skipped — a chart that silently
  // drops quiet weeks makes a gap look like steady activity.
  var start = cutoff || (earliest ? bucketOfDate(earliest) : today);
  var cursor = bucketOfDate(start), out = [];
  var guard = 0;
  while (cursor <= today && guard++ < 400) {
    var k = fmtDate_(cursor, 'yyyy-MM-dd');
    var b = buckets[k] || { applied: 0, oa: 0, interview: 0, offer: 0, removed: 0 };
    out.push({ key: k, label: fmtDate_(cursor, monthly ? 'MMM' : 'd MMM'),
               applied: b.applied, oa: b.oa, interview: b.interview,
               offer: b.offer, removed: b.removed });
    if (monthly) cursor = new Date(cursor.getFullYear(), cursor.getMonth() + 1, 1);
    else { cursor = new Date(cursor.getTime()); cursor.setDate(cursor.getDate() + 7); }
  }

  // Where things stand right now, which the history cannot tell you: a role
  // applied for in March and still waiting is one row in the bank, not an event.
  var funnel = {};
  BUCKETS.forEach(function (b) { funnel[b.key] = 0; });
  rowsOf(SHEET_JOBS).forEach(function (j) { funnel[bucketOf_(j['Status'])]++; });

  return {
    ok: true,
    bucket: monthly ? 'month' : 'week',
    buckets: out,
    totals: totals,
    funnel: BUCKETS.map(function (b) {
      return { key: b.key, label: b.label.replace(/&amp;/g, '&'), n: funnel[b.key] };
    }),
    hasHistory: everSeen,
    since: earliest ? fmtDate_(earliest, 'd MMM yyyy') : ''
  };
}

// Inline SVGs so the sidebar reads at a glance — and still reads when it is
// collapsed to icons only. stroke:currentColor picks up the tab's own colour.
/* ---------------------------------------------------------------- sally --- */
var SHEET_RUNLOG_ = 'Run Log';
var SHEET_STATS_ = 'Stats';

/**
 * The dials Sally exposes. Whitelist + hard ranges: these cells feed straight
 * into the pipeline's config override (read at the start of every run), so a
 * typo here must be rejected at the door rather than discovered in CI.
 */
var SALLY_PARAMS_ = {
  min_score:      { int: true, min: 1,  max: 100,
    label: 'Minimum score for the bank',
    help: 'A role below this is archived, not shown. Applies from the next run.' },
  weekly_cap:     { int: true, min: 1,  max: 50,
    label: 'Max new roles per week',
    help: 'Rate limit on bank additions in any 7-day window.' },
  llm_top_n:      { int: true, min: 5,  max: 200,
    label: 'AI reviews per run',
    help: 'How many roles get full AI scoring each run — the main token dial.' },
  run_every_days: { int: true, min: 1,  max: 14,
    label: 'Check every N days',
    help: '1 = daily. Higher skips whole runs: zero tokens on the days between.' },
  digest_to:      { email: true,
    label: 'Email digest to',
    help: 'Weekly summary + urgent-deadline alerts. Blank = no emails.' }
};

// Sally's own scraping happens in GitHub Actions, so "run now" means asking
// GitHub to dispatch the workflow. Needs a fine-grained PAT with Actions: write
// stored as GITHUB_TOKEN in Script Properties — the dashboard never holds it.
var SALLY_REPO_ = 'your-user/your-private-repo';
var SALLY_WORKFLOW_ = 'weekly.yml';

function sallyGhToken_() {
  return PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
}

/**
 * Diagnostic for the Run now path. Run this from the editor and read the log.
 *
 * Deliberately NOT wrapped in try/catch: a missing OAuth scope has to surface
 * in the execution log rather than be swallowed into {ok:false}, which is what
 * made sallyRunStatus look like it had succeeded. Never logs the token itself,
 * only whether one is there and how long it is.
 */
function sallyDiagnose() {
  var props = PropertiesService.getScriptProperties();
  var tok = props.getProperty('GITHUB_TOKEN');
  Logger.log('script property keys: ' + (props.getKeys().join(', ') || '(none)'));
  Logger.log('GITHUB_TOKEN: ' + (tok ? 'present, ' + tok.length + ' chars, starts "' +
             tok.slice(0, 4) + '"' : 'MISSING'));
  // Unauthenticated call first: if the scope is missing this throws here, and
  // that is the answer.
  var ping = UrlFetchApp.fetch('https://api.github.com/rate_limit',
                               { muteHttpExceptions: true });
  Logger.log('UrlFetchApp works: HTTP ' + ping.getResponseCode());
  if (!tok) return;
  var look = UrlFetchApp.fetch(
    'https://api.github.com/repos/' + SALLY_REPO_ + '/actions/workflows/' +
    SALLY_WORKFLOW_,
    { muteHttpExceptions: true,
      headers: { Authorization: 'Bearer ' + tok,
                 Accept: 'application/vnd.github+json' } });
  Logger.log('workflow lookup: HTTP ' + look.getResponseCode() + ' ' +
             String(look.getContentText()).slice(0, 200));
  Logger.log('token scopes (classic PATs only): ' +
             (look.getAllHeaders()['x-oauth-scopes'] || '(not reported)'));
}

function sallyRunNow() {
  var tok = sallyGhToken_();
  if (!tok) {
    return { ok: false, needsToken: true,
             error: 'No GITHUB_TOKEN set in Script Properties.' };
  }
  try {
    var res = UrlFetchApp.fetch(
      'https://api.github.com/repos/' + SALLY_REPO_ + '/actions/workflows/' +
      SALLY_WORKFLOW_ + '/dispatches',
      { method: 'post', contentType: 'application/json', muteHttpExceptions: true,
        headers: { Authorization: 'Bearer ' + tok,
                   Accept: 'application/vnd.github+json',
                   'X-GitHub-Api-Version': '2022-11-28' },
        payload: JSON.stringify({ ref: 'main' }) });
    var code = res.getResponseCode();
    if (code === 204) return { ok: true };
    if (code === 401 || code === 403) {
      return { ok: false, error: 'GitHub rejected the token (' + code +
               '). It needs Actions: write on ' + SALLY_REPO_ + '.' };
    }
    return { ok: false, error: 'GitHub said ' + code + ': ' +
             String(res.getContentText()).slice(0, 140) };
  } catch (err) {
    return { ok: false, error: String(err) };
  }
}

/** Status of the most recent workflow run, for polling after a dispatch. */
function sallyRunStatus() {
  var tok = sallyGhToken_();
  if (!tok) return { ok: false };
  try {
    var res = UrlFetchApp.fetch(
      'https://api.github.com/repos/' + SALLY_REPO_ + '/actions/workflows/' +
      SALLY_WORKFLOW_ + '/runs?per_page=1',
      { muteHttpExceptions: true,
        headers: { Authorization: 'Bearer ' + tok,
                   Accept: 'application/vnd.github+json' } });
    if (res.getResponseCode() !== 200) return { ok: false };
    var r = (JSON.parse(res.getContentText()).workflow_runs || [])[0];
    if (!r) return { ok: false };
    return { ok: true, status: r.status, conclusion: r.conclusion,
             url: r.html_url, started: r.run_started_at };
  } catch (err) {
    return { ok: false };
  }
}

/** Everything the board shows, from the Run Log + Stats tabs. */
function sallyBoard() {
  var out = { ok: true, sources: [], runs: [], config: {}, params: SALLY_PARAMS_,
              totals: { fetched: 0, added: 0, scored: 0, archived: 0 },
              lastRun: null, cost: null, nextRun: '' };

  var rl = sheetOf(SHEET_RUNLOG_);
  var runs = [];
  if (rl && rl.getLastRow() > 1) {
    var head = headerOf(rl);
    var start = Math.max(2, rl.getLastRow() - 59);   // ~2 months of daily runs
    var vals = rl.getRange(start, 1, rl.getLastRow() - start + 1, head.length)
                 .getDisplayValues();
    vals.forEach(function (v) {
      var r = {};
      head.forEach(function (h, i) { r[h] = v[i]; });
      if (!r['Run ID']) return;
      var per = {}, fails = {};
      try { per = JSON.parse(r['Fetched/source'] || '{}') || {}; } catch (e) {}
      try { fails = JSON.parse(r['Failed sources'] || '{}') || {}; } catch (e) {}
      runs.push({ id: r['Run ID'], started: r['Started'] || '',
                  per: per, fails: fails,
                  fresh: Number(r['After dedupe']) || 0,
                  scored: Number(r['Scored']) || 0,
                  added: Number(r['Added']) || 0,
                  archived: Number(r['Archived']) || 0 });
    });
  }

  var src = {};
  runs.forEach(function (run) {
    Object.keys(run.per).forEach(function (name) {
      if (!src[name]) src[name] = { name: name, total: 0, fails: 0, last: 0,
                                    lastRun: '', lastFound: '' };
      var n = Number(run.per[name]) || 0;
      src[name].total += n;
      src[name].last = n;                    // runs arrive oldest-first
      src[name].lastRun = run.started;
      if (n > 0) src[name].lastFound = run.started;
      if (run.fails[name]) src[name].fails++;
    });
    out.totals.added += run.added;
    out.totals.scored += run.scored;
    out.totals.archived += run.archived;
    Object.keys(run.per).forEach(function (k) {
      out.totals.fetched += Number(run.per[k]) || 0;
    });
  });
  out.sources = Object.keys(src).map(function (k) { return src[k]; })
    .sort(function (a, b) { return b.last - a.last || b.total - a.total; });
  out.runs = runs.slice(-12).reverse();
  out.lastRun = runs.length ? runs[runs.length - 1] : null;

  var st = sheetOf(SHEET_STATS_);
  if (st && st.getLastRow() > 1) {
    var v = st.getDataRange().getDisplayValues();
    var hi = -1;
    for (var i = 0; i < v.length; i++) {
      if (v[i][0] === 'Run ID') { hi = i; break; }
    }
    if (hi >= 0) {
      var costIdx = v[hi].indexOf('LLM cost est ($)');
      var nextIdx = v[hi].indexOf('Next scheduled run');
      var c = 0;
      for (var j = hi + 1; j < v.length; j++) {
        if (!v[j][0]) continue;
        if (costIdx >= 0) c += Number(v[j][costIdx]) || 0;
        if (nextIdx >= 0 && v[j][nextIdx]) out.nextRun = v[j][nextIdx];
      }
      out.cost = Math.round(c * 100) / 100;
    }
    st.getRange(2, 1, Math.min(10, st.getLastRow() - 1), 2).getDisplayValues()
      .forEach(function (row) {
        if (row[0] && SALLY_PARAMS_[row[0]]) out.config[row[0]] = row[1];
      });
  }
  return out;
}

/** Write one dial to the Stats CONFIG block. Growing the block inserts a row
 *  just above the blank line that separates config from the run table. */
function sallySave(key, value) {
  var spec = SALLY_PARAMS_[key];
  if (!spec) return { ok: false, error: 'Unknown parameter' };
  var val = String(value == null ? '' : value).trim();
  if (spec.int) {
    if (!/^-?\d+$/.test(val)) return { ok: false, error: 'Needs a whole number' };
    var n = parseInt(val, 10);
    if (n < spec.min || n > spec.max) {
      return { ok: false, error: 'Allowed range: ' + spec.min + '\u2013' + spec.max };
    }
    val = String(n);
  } else if (spec.email && val && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(val)) {
    return { ok: false, error: 'Not an email address' };
  }
  var st = sheetOf(SHEET_STATS_);
  if (!st) return { ok: false, error: 'No Stats sheet' };
  var rows = st.getRange(2, 1, 12, 1).getDisplayValues();
  var target = -1, firstBlank = -1;
  for (var i = 0; i < rows.length; i++) {
    var a = rows[i][0];
    if (a === key) { target = 2 + i; break; }
    if (a === 'Run ID') break;
    if (!a && firstBlank < 0) firstBlank = 2 + i;
  }
  if (target < 0) {
    var at = firstBlank > 0 ? firstBlank : 2;
    st.insertRowsBefore(at, 1);
    st.getRange(at, 1).setValue(key);
    target = at;
  }
  st.getRange(target, 2).setValue(val);
  return { ok: true, key: key, value: val };
}

var ICONS_ = (function () {
  function svg(inner) {
    return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
      'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" ' +
      'aria-hidden="true">' + inner + '</svg>';
  }
  return {
    bank: svg('<rect x="3" y="7" width="18" height="13" rx="2"/>' +
              '<path d="M8 7V5a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>'),
    more: svg('<path d="m12 3-10 5 10 5 10-5-10-5z"/><path d="m2 13 10 5 10-5"/>'),
    find: svg('<circle cx="11" cy="11" r="7"/><path d="m20 20-4.35-4.35"/>'),
    soon: svg('<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>'),
    stats: svg('<path d="M4 20V10"/><path d="M10 20V4"/><path d="M16 20v-7"/>' +
               '<path d="M22 20H2"/>'),
    sally: svg('<rect x="6" y="6" width="12" height="12" rx="2"/>' +
               '<path d="M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4"/>')
  };
})();

function renderPage() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheetUrl = ss.getUrl();

  // SHEET order is the display order. Hearts already sit physically on top
  // (toggleFavourite moves the row) and drag-to-reorder writes the sheet the
  // same way — so what you arrange is what every reload shows. The sheets were
  // normalised once (fav-first, then score) when this shipped, so the starting
  // point matches what the score sort used to show.
  var jobs = rowsOf(SHEET_JOBS);
  var urgent = jobs.filter(function (j) {
    var d = daysUntil(j['Deadline']); return d !== null && d >= 0 && d <= 7;
  });
  function bucket(key) {
    return jobs.filter(function (j) { return bucketOf_(j['Status']) === key; });
  }
  var shown = false, opened = false;   // "first non-empty tab" flags, used below
  // The bank page carries the watchlist too, Trackr-style: programmes that are
  // not open yet sit one tab over from the roles that are. Dismissed rows stay
  // hidden here the same way the Soon page hides them.
  var soonRows = rowsOf(SHEET_SOON).filter(function (s) {
    return String(s['Status'] || '').toLowerCase().indexOf('dismissed') < 0;
  });
  var soonOpen = soonRows.filter(function (s) {
    return String(s['Status'] || '').toUpperCase().indexOf('OPEN NOW') >= 0; });
  var soonWait = soonRows.filter(function (s) {
    return String(s['Status'] || '').toUpperCase().indexOf('OPEN NOW') < 0; });


  var today = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'EEEE d MMMM');

  return '' +
'<!doctype html><html lang="en"><head><meta charset="utf-8">' +
'<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">' +
'<link rel="preconnect" href="https://fonts.googleapis.com">' +
'<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>' +
'<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700&display=swap" rel="stylesheet">' +
'<style>' +
':root{' +
' --bg:#F7F5EF; --card:#FFFFFF; --ink:#2E2E28; --text:#4A4A42; --soft:#8A857A;' +
' --line:#E5E0D3; --blue:#6B7355; --green:#5F7A54; --red:#B4553F; --amber:#C08A3E;' +
' --sidebar:#6E7A5E; --sidebar-ink:#F4F2EA; --sand:#EFEBE0;' +
' --title:#1F1F1B; --company:#B4553F;' +
' --spring:cubic-bezier(0.32,0.72,0,1);' +
// Owner colour: one accent per profile dashboard, so a glance at the header
// tells you whose bank you're looking at (layouts are otherwise identical).
' --accent:#6B7355;' +
'}' +
'*{box-sizing:border-box}' +
'body{margin:0;background:var(--bg);color:var(--text);' +
' font:16px/1.6 "Outfit",system-ui,sans-serif;-webkit-font-smoothing:antialiased}' +
// the bottom padding lives on .page, not .wrap: padding on the wrap sits BELOW
// the sidebar, so the page scrolled 80px past where the sage column ended
'.wrap{max-width:1560px;margin:0 auto;padding:0;display:flex;gap:0;align-items:flex-start}' +
'.rail{position:sticky;top:0;align-self:stretch;min-height:100vh;width:232px;flex:0 0 232px;' +
' background:var(--sidebar);color:var(--sidebar-ink);padding:26px 16px 22px;' +
' display:flex;flex-direction:column;gap:6px}' +
'.railhead{display:flex;align-items:flex-start;justify-content:space-between;gap:8px}' +
'.railtitle{flex:1 1 auto;font-size:19px;font-weight:600;letter-spacing:-.01em;padding:0 10px 4px;line-height:1.25}' +
'.railtoggle{flex:0 0 auto;width:26px;height:26px;margin:2px 4px 0 0;border:0;border-radius:8px;' +
' cursor:pointer;color:rgba(244,242,234,.75);background:rgba(255,255,255,.08);padding:0;' +
' display:grid;place-items:center;font-size:14px;line-height:1;' +
' transition:background-color 160ms var(--spring),color 160ms var(--spring)}' +
'.railtoggle:hover{background:rgba(255,255,255,.16);color:#fff}' +
'.railsub{font-size:12px;color:rgba(244,242,234,.62);padding:0 10px 18px;font-weight:400}' +
'.pagetab{font:inherit;font-size:14px;font-weight:500;color:rgba(244,242,234,.82);' +
' background:transparent;border:0;border-radius:11px;padding:11px 12px;cursor:pointer;' +
' display:flex;align-items:center;gap:11px;text-align:left;width:100%;' +
' transition:background-color 160ms var(--spring),color 160ms var(--spring)}' +
'.pagetab .ic{width:17px;height:17px;flex:0 0 auto;opacity:.9}' +
'.pagetab .ic svg{width:17px;height:17px;display:block}' +
// collapsed rail: icons only, so the list gets the width back
'.wrap.railmin .rail{width:64px;flex:0 0 64px;padding:22px 9px;align-items:center}' +
'.wrap.railmin .railtitle,.wrap.railmin .railsub,.wrap.railmin .railfoot,' +
'.wrap.railmin .pagetab .lbl{display:none}' +
'.wrap.railmin .railhead{justify-content:center;padding-bottom:14px}' +
'.wrap.railmin .railtoggle{margin:0}' +
'.wrap.railmin .pagetab{width:46px;justify-content:center;padding:11px 0}' +
'.pagetab:hover{background:rgba(255,255,255,.09);color:#fff}' +
'.pagetab.on{background:rgba(255,255,255,.16);color:#fff;font-weight:600}' +
'.railfoot{margin-top:auto;padding:16px 12px 0;border-top:1px solid rgba(255,255,255,.14);' +
' font-size:12.5px;line-height:1.5;color:rgba(244,242,234,.66)}' +
'.railfoot a{color:rgba(244,242,234,.9);text-decoration:none;font-weight:500}' +
'.page{flex:1 1 auto;min-width:0;padding:30px 26px 80px}' +
'@media(max-width:900px){' +
' .wrap{display:block}' +
' .rail{position:static;width:auto;min-height:0;flex-direction:row;flex-wrap:wrap;' +
'  padding:14px 12px;gap:6px;align-items:center}' +
' .railtitle,.railsub,.railfoot,.railtoggle{display:none}' +
// collapsing makes no sense when the rail is a horizontal chip row
' .wrap.railmin .rail{width:auto;flex:none;padding:14px 12px}' +
' .wrap.railmin .pagetab{width:auto;justify-content:flex-start;padding:9px 13px}' +
' .wrap.railmin .pagetab .lbl{display:inline}' +
' .pagetab{width:auto;padding:9px 13px}' +
' .page{padding:18px 14px 60px}' +
'}' +

/* header */
'.top{background:transparent;padding:0 0 4px;color:var(--ink);' +
' display:flex;justify-content:space-between;align-items:flex-start;gap:20px;flex-wrap:wrap}' +
'.top h1{margin:0;font-size:26px;font-weight:600;letter-spacing:-.02em;color:var(--ink)}' +
'.top .sub{color:var(--soft);font-size:13.5px;margin-top:5px;font-weight:400}' +
'.linkbtn{background:var(--card);color:var(--text);text-decoration:none;font-size:13.5px;font-weight:500;' +
' border:1px solid var(--line);' +
' padding:11px 18px;border-radius:999px;display:inline-flex;align-items:center;gap:10px;' +
' transition:transform 220ms var(--spring)}' +
'.linkbtn .dot{width:22px;height:22px;border-radius:50%;background:rgba(0,0,0,.08);' +
' display:grid;place-items:center;font-size:12px;transition:transform 220ms var(--spring)}' +
'.linkbtn:active{transform:scale(.97)}' +

/* stats */
'.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:16px 0 8px}' +
'.stat{background:var(--card);border-radius:16px;padding:18px 16px;text-align:center;' +
' border:2px solid var(--line)}' +
'.stat b{display:block;font-size:32px;font-weight:800;line-height:1;color:var(--ink)}' +
'.stat span{font-size:12px;font-weight:600;color:var(--soft);margin-top:6px;display:block}' +
'.stat.red{border-color:#FFC9C9;background:#FFF5F5}.stat.red b{color:var(--red)}' +
'.stat.green{border-color:#B2F2BB;background:#F4FCF5}.stat.green b{color:var(--green)}' +
'.stat.blue{border-color:#A5D8FF;background:#F1F9FF}.stat.blue b{color:var(--blue)}' +

/* alert */
'.alert{background:#FFF5F5;border:2px solid #FFC9C9;border-radius:16px;padding:18px 20px;' +
' margin-top:20px}' +
'.alert h2{margin:0 0 10px;font-size:15px;font-weight:800;color:var(--red)}' +
'.alert ul{margin:0;padding-left:20px}' +
'.alert li{margin:6px 0;font-size:15px}' +
'.alert a{color:var(--red);font-weight:600}' +

/* filters */
'.sub2{color:var(--soft);font-size:14px;margin:20px 0 4px}' +
'.searchbox{width:100%;max-width:420px;margin:24px 0 0;display:block;' +
' font:inherit;font-size:14px;color:var(--text);background:var(--card);' +
' border:2px solid var(--line);border-radius:999px;padding:10px 18px;outline:none;' +
' transition:border-color 200ms var(--spring)}' +
'.searchbox:focus{border-color:var(--accent)}' +
'.filters{display:flex;gap:10px;flex-wrap:wrap;margin:14px 0 18px;align-items:center}' +

/* filter dropdowns: role type + location, side by side */
'.fdrop{position:relative}' +
'.fbtn{font:inherit;font-size:12.5px;font-weight:600;color:var(--text);background:var(--card);' +
' border:1.5px solid var(--line);border-radius:999px;padding:7px 13px;cursor:pointer;' +
' display:inline-flex;align-items:center;gap:7px;max-width:340px;' +
' transition:border-color 200ms var(--spring),background-color 200ms var(--spring)}' +
'.fbtn:hover{border-color:#ADB5BD}' +
'.fdrop.on .fbtn{border-color:var(--accent)}' +
'.flabel{color:var(--soft);font-weight:600;flex:0 0 auto}' +
// the summary is the part that can get long, so it is the part that truncates
'.fval{font-weight:700;color:var(--ink);min-width:0;overflow:hidden;text-overflow:ellipsis;' +
' white-space:nowrap}' +
'.fdrop.set .fval{color:var(--accent)}' +
'.fchev{font-size:9px;color:var(--soft);flex:0 0 auto;transition:transform 200ms var(--spring)}' +
'.fdrop.open .fchev{transform:rotate(180deg)}' +
'.fpanel{position:absolute;top:calc(100% + 7px);left:0;z-index:60;width:288px;' +
' background:var(--card);border:1.5px solid var(--line);border-radius:15px;' +
' box-shadow:0 14px 34px rgba(0,0,0,.13);overflow:hidden;display:none}' +
'.fdrop.open .fpanel{display:block}' +
// two levels side by side; picking a country slides the strip left to the cities
'.fslide{display:flex;width:200%;transition:transform 280ms var(--spring)}' +
'.fdrop.sub .fslide{transform:translateX(-50%)}' +
'.flevel{width:50%;flex:0 0 50%;max-height:302px;overflow-y:auto;padding:7px}' +
'.frow{display:flex;align-items:center;gap:9px;padding:8px 9px;border-radius:9px;' +
' cursor:pointer;font-size:13px;font-weight:600;color:var(--text)}' +
'.frow:hover{background:#F4F2EA}' +
'.frow input{width:15px;height:15px;flex:0 0 auto;accent-color:var(--accent);cursor:pointer;margin:0}' +
'.fname{flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}' +
'.fcount{font-size:11.5px;font-weight:700;color:var(--soft);flex:0 0 auto}' +
'.fdrill{font:inherit;font-size:14px;line-height:1;color:var(--soft);background:transparent;' +
' border:0;cursor:pointer;padding:2px 3px;flex:0 0 auto;border-radius:6px}' +
'.fdrill:hover{background:rgba(0,0,0,.07);color:var(--ink)}' +
'.fback{font:inherit;font-size:12.5px;font-weight:700;color:var(--text);background:transparent;' +
' border:0;cursor:pointer;padding:8px 9px;width:100%;text-align:left;border-radius:9px;' +
' display:flex;align-items:center;gap:8px}' +
'.fback:hover{background:#F4F2EA}' +
'.ffoot{display:flex;align-items:center;justify-content:space-between;gap:8px;' +
' padding:9px 13px;border-top:1px solid var(--line);background:#FBFAF6}' +
'.fclear{font:inherit;font-size:12px;font-weight:700;color:var(--soft);background:transparent;' +
' border:0;cursor:pointer;padding:0}' +
'.fclear:hover{color:var(--red)}' +
'.fnum{font-size:11.5px;font-weight:600;color:var(--soft)}' +
'.fempty{padding:14px 10px;font-size:12.5px;color:var(--soft)}' +

/* card: outer shell + inner core */
// align-items:start (not stretch) so expanding one card doesn't drag every
// other card in its row taller — each sizes to its own content.
// minmax(0,1fr), not 1fr: a bare 1fr floors at min-content, so a single long
// token in one card (a stray ==== rule, an un-spaced URL) stretches that whole
// column and squashes the other two.
'.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;align-items:start}' +
'.shell,.card,.inner{min-width:0}' +
'.shell h3,.shell .co,.why,.rank{overflow-wrap:anywhere}' +
'@media (max-width:1240px){.grid{grid-template-columns:repeat(2,1fr)}}' +
'@media (max-width:680px){.grid{grid-template-columns:1fr}}' +
'.shell{background:var(--card);border-radius:20px;padding:5px;display:flex;' +
' border:2px solid var(--line);' +
' opacity:0;transform:translateY(14px);animation:in 620ms var(--spring) forwards;' +
' animation-delay:calc(var(--i) * 45ms);' +
' transition:opacity 240ms var(--spring),transform 240ms var(--spring),' +
' border-color 240ms var(--spring)}' +
'@keyframes in{to{opacity:1;transform:none}}' +
'.shell.is-top{border-color:#FFD43B;background:#FFFBEB}' +
'.shell.leaving{opacity:0;transform:translateY(-8px) scale(.98)}' +
'.card{position:relative;background:var(--card);border-radius:17px;overflow:hidden;' +
' flex:1;display:flex;flex-direction:column;min-width:0;cursor:pointer}' +
'.stripe{display:none}' +
'.inner{padding:16px 16px 15px 20px;display:flex;flex-direction:column;height:100%}' +
'.row1{display:flex;align-items:center;gap:9px;flex-wrap:wrap;margin-bottom:12px}' +
'.shell.expanded .row1{padding-right:38px}' +
'.tag{font-size:11px;font-weight:700;color:#fff;padding:4px 10px;border-radius:999px;' +
' letter-spacing:.01em}' +
'.tag-top{background:#F59F00;color:#3B2F00}' +
'.score{margin-left:auto;color:#fff;font-weight:800;font-size:15px;padding:3px 9px;' +
' border-radius:12px;display:inline-flex;align-items:baseline;gap:8px}' +
'.score em{font-style:normal;font-size:10px;font-weight:700;opacity:.85}' +
// The heart sits in the top row rather than the actions strip, because the
// actions strip only exists on an expanded card and this is the one control
// you want to hit while skimming.
'.heart{margin-left:2px;font-size:15px;line-height:1;color:#CFC9BC;background:transparent;' +
' border:0;padding:0;cursor:pointer;flex:none;width:28px;height:28px;border-radius:50%;' +
' display:inline-flex;align-items:center;justify-content:center;' +
' transition:transform 200ms var(--spring),color 200ms var(--spring),' +
' background-color 200ms var(--spring)}' +
'.heart:hover{color:var(--red);background:#FBEDEA}' +
'.heart.on{color:var(--red)}' +
'.heart:active{transform:scale(.82)}' +
'.heart.busy{opacity:.45;pointer-events:none}' +
// after .shell.is-top, so a hearted top pick reads as hearted first
'.shell.is-fav{border-color:#E8B4A8;background:#FDF6F4}' +
/* list view -------------------------------------------------------------
   One row per role, same markup as a card. The card grid stays the default;
   this is what you switch to when you want to scan forty roles rather than
   read six. Expanding a row still opens the description and the actions, so
   nothing is only reachable from the card view. */
'.avatar{display:none}' +
'.newbadge{display:none}' +
'.grid.rows{grid-template-columns:1fr;gap:8px}' +
'.rows .shell{border-radius:13px;padding:3px;animation-delay:calc(var(--i) * 18ms)}' +
'.rows .card{border-radius:11px}' +
// the row itself: avatar | title+pills | score/heart, everything centred
'.rows .inner{display:grid;grid-template-columns:auto minmax(0,1fr) auto;' +
' align-items:center;gap:14px;padding:11px 14px}' +
'.rows .avatar{display:grid;place-items:center;width:34px;height:34px;flex:none;' +
' border-radius:9px;background:var(--sand);color:var(--soft);' +
' font-size:12.5px;font-weight:700;letter-spacing:.02em}' +
// title block carries the role, the company and the pills in two tight lines
'.rows .titleblock{grid-column:2;margin:0;padding:0;border:0;min-width:0}' +
'.rows .titleblock::before{display:none}' +
// In a list the employer is what you scan down, so it takes the headline and
// the role becomes the subline under it — the reverse of the card, where one
// role fills the whole tile and the title should dominate.
// `.rows .shell h3`, not `.rows h3`: the base `.shell h3` rule has the same
// specificity and sits later in the sheet, so it was winning and the role
// kept rendering larger than the company it sits under.
'.rows .shell h3{order:2;margin:0;font-size:12.8px;font-weight:500;' +
' color:var(--text);' +
' line-height:1.35;display:flex;align-items:center;gap:7px;flex-wrap:wrap;' +
' -webkit-line-clamp:none}' +
// .co carries a bottom hairline in card view, which in a row reads as a rule
// drawn across the middle of the listing
'.rows .co{order:1;margin:0;padding:0;border:0;font-size:15.5px;font-weight:700;' +
' color:var(--cat,var(--title));letter-spacing:-.012em;line-height:1.3;min-width:0;' +
' overflow:hidden;text-overflow:ellipsis;white-space:nowrap}' +
'.rows .pills{grid-column:2;margin:0;gap:6px;font-size:11px;flex-wrap:wrap}' +
'.rows .pills .pill{font-size:11px;padding:2px 8px}' +
// company sits with the pills rather than above the title: in a list the role
// is what you are scanning, and the company is one more fact about it
'.rows .titleblock{display:flex;flex-direction:column;gap:3px}' +
'.rows .row1{grid-column:3;grid-row:1 / span 2;margin:0;flex-wrap:nowrap;gap:7px}' +
'.rows .tag{display:none}' +
'.rows .tag-top{display:inline-block;font-size:10px;padding:3px 8px}' +
'.rows .score{margin-left:0;font-size:13.5px;padding:2px 8px}' +
'.rows .score em{display:none}' +
'.rows .why,.rows .morehint{display:none}' +
'.rows .stripe{display:none}' +
// expanded rows behave like cards again, so the description and the action
// strip are never stranded in a view you are not using
'.rows .shell.expanded .inner{grid-template-columns:auto minmax(0,1fr) auto}' +
'.rows .shell.expanded .why{display:block;grid-column:2;margin-top:6px}' +
'.rows .shell.expanded .actions{display:flex;grid-column:1 / -1;margin-top:4px}' +
'.rows .newbadge{display:inline-block;font-size:9.5px;font-weight:800;' +
' letter-spacing:.04em;color:var(--blue);background:#EAF3FE;border:1px solid #A5D8FF;' +
' border-radius:999px;padding:2px 7px;flex:none}' +
'@media(max-width:680px){' +
' .rows .inner{grid-template-columns:auto minmax(0,1fr);gap:10px}' +
' .rows .row1{grid-column:1 / -1;grid-row:auto;margin-top:4px}' +
'}' +
/* the view switch */
'.viewtog{display:inline-flex;background:var(--card);border:1.5px solid var(--line);' +
' border-radius:999px;padding:2px;gap:2px;margin-left:auto;flex:none}' +
'.viewtog button{font:inherit;font-size:12px;font-weight:600;color:var(--soft);' +
' background:transparent;border:0;border-radius:999px;padding:6px 13px;cursor:pointer;' +
' transition:background-color 180ms var(--spring),color 180ms var(--spring)}' +
'.viewtog button.on{background:var(--accent);color:#fff}' +
// Title and company were two bold lines stacked with nothing between them, so
// they blurred into one block. Three things separate them now: the company sits
// ABOVE the title rather than under it, a terracotta bar and a hairline mark the
// group out, and the company carries the one warm colour on the card while the
// title keeps the darkest ink. Same terracotta on every role, so it reads as
// "this is the company" rather than encoding anything.
'.titleblock{border-left:5px solid var(--company);padding-left:14px;margin:0 0 11px}' +
'.co{margin:0 0 8px;padding-bottom:7px;border-bottom:1px solid var(--line);' +
' font-size:14px;font-weight:800;color:var(--company);letter-spacing:.005em;' +
' line-height:1.3}' +
'.shell h3{margin:0;font-size:19.5px;line-height:1.24;font-weight:700;' +
' letter-spacing:-.022em;color:var(--title);' +
' display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}' +
'.shell h3 a{color:inherit;text-decoration:none}' +
'.pills{display:flex;flex-wrap:wrap;gap:6px}' +
'.pill{font-size:12px;font-weight:600;padding:4px 9px;border-radius:7px;white-space:nowrap;' +
' overflow:hidden;text-overflow:ellipsis;min-width:0;flex:0 1 auto}' +
'.pill-grey{background:#F2EFE6;color:#5F5A50}' +
'.pill-red{background:#F5E2DC;color:#A24A34}' +
'.pill-green{background:#E3EBDB;color:#4A6238}' +
'.pill-blue{background:#EBEEE5;color:#5A6650}' +
'.pill-dead{background:#4A4A42;color:#F4F2EA}' +
// pay is the one pill worth picking out of the row at a glance
'.pill-pay{background:#E7EFE4;color:#3F6B45;font-weight:700}' +
// still open after six months: not dead, but old enough to check first
'.pill-stale{background:#F6ECD9;color:#8A6524}' +
// an estimate, drawn as one: dashed edge, no fill, so it never reads with
// the same authority as a date an employer actually published
'.pill-est{background:transparent;color:var(--soft);border:1px dashed #C9C2B0;font-style:italic}' +
// a dead listing stays readable but stops competing with live ones for attention
'.shell.is-closed{opacity:.58}' +
'.shell.is-closed:hover,.shell.is-closed.expanded{opacity:1}' +
'.shell.is-closed h3 a{text-decoration:line-through;text-decoration-thickness:1px}' +

/* find roles: search hits are a decision list, so they are rows not cards */
'.hits{display:flex;flex-direction:column;gap:9px;margin:16px 0 0}' +
'.hit{display:flex;align-items:flex-start;gap:12px;background:var(--card);' +
' border:1px solid var(--line);border-radius:12px;padding:13px 14px;' +
' animation:rise .28s cubic-bezier(.2,.7,.3,1) backwards;animation-delay:calc(var(--i)*22ms)}' +
'.hitmain{flex:1 1 auto;min-width:0}' +
'.hit h4{margin:0 0 4px;font-size:16px;font-weight:800;line-height:1.3;' +
' letter-spacing:-.015em;color:var(--title)}' +
// search hits are compact decision rows, so they take the colour but not the
// bar and rule that give the cards their structure
'.hit .co{margin:0 0 7px;padding:0;border:0;font-size:12.5px;letter-spacing:.005em}' +
'.hit h4 a{color:inherit;text-decoration:none}' +
'.hit h4 a:hover{text-decoration:underline}' +
'.hit .why{margin:0 0 8px;-webkit-line-clamp:2}' +
'.hit .score{position:static;margin-top:2px}' +
'.added{color:#2B8A3E;font-size:19px;font-weight:800;flex:0 0 auto;padding:6px 4px}' +
'.searchrow{display:flex;gap:9px;flex-wrap:wrap;align-items:center;margin:18px 0 0}' +
'.searchrow .searchbox{margin:0;flex:1 1 240px;max-width:none}' +
'.gobtn{font:inherit;font-size:14px;font-weight:700;color:#fff;background:var(--accent);' +
' border:0;border-radius:10px;padding:11px 20px;cursor:pointer}' +
'.gobtn:active{transform:scale(.97)}' +
'.gobtn[disabled]{opacity:.5;cursor:default}' +
'.togg{display:flex;align-items:center;gap:7px;font-size:13px;color:var(--soft);' +
' font-weight:600;cursor:pointer;user-select:none}' +

/* add-your-own forms */
'.panel{background:var(--card);border:1px solid var(--line);border-radius:14px;' +
' padding:16px 16px 18px;margin:18px 0 0}' +
'.panel h4{margin:0 0 4px;font-size:15px;font-weight:800}' +
'.panel p.hint{margin:0 0 14px;font-size:13px;color:var(--soft);line-height:1.5}' +
'.fgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:11px}' +
'.f{display:flex;flex-direction:column;gap:5px;min-width:0}' +
'.f label{font-size:12px;font-weight:700;color:var(--soft)}' +
'.f input,.f textarea{font:inherit;font-size:14px;color:var(--ink);background:#fff;' +
' border:1.5px solid var(--line);border-radius:9px;padding:9px 11px;width:100%;' +
' box-sizing:border-box}' +
'.f input:focus,.f textarea:focus{outline:0;border-color:var(--accent)}' +
'.f textarea{min-height:64px;resize:vertical}' +
'.f.wide{grid-column:1/-1}' +
'.frow{display:flex;gap:9px;align-items:center;margin:14px 0 0;flex-wrap:wrap}' +
'.soonshell .card{min-height:0}' +
'.why{margin:10px 0 0;font-size:12.5px;line-height:1.5;color:var(--text);flex:0 0 auto;' +
' max-height:calc(1.5em * 2 + 12px);' +
' background:#F8F9FA;border-left:3px solid var(--line);padding:9px 11px;border-radius:7px;' +
' display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}' +
'.shell.expanded .why{-webkit-line-clamp:unset;max-height:none;overflow:visible}' +
'.nodesc{color:var(--soft);font-style:italic}' +
'.whyopts{display:flex;flex-wrap:wrap;gap:8px;margin:4px 0 14px}' +
'.whyopt{font:inherit;font-size:13px;font-weight:600;color:var(--text);background:#fff;' +
' border:2px solid var(--line);border-radius:999px;padding:8px 14px;cursor:pointer;' +
' transition:transform 200ms var(--spring),border-color 200ms var(--spring),' +
' background-color 200ms var(--spring)}' +
'.whyopt.on{background:var(--accent);border-color:var(--accent);color:#fff}' +
'.whyopt:active{transform:scale(.97)}' +
'#whyModal textarea{width:100%;height:80px;border:2px solid var(--line);border-radius:12px;' +
' padding:12px;resize:vertical;color:var(--text);font:13px/1.5 inherit}' +
'.morehint{margin-top:9px;font-size:11.5px;font-weight:700;color:var(--soft);' +
' display:flex;align-items:center;gap:4px}' +
'.chev{display:inline-block;transition:transform 200ms var(--spring)}' +
'.shell.expanded .morehint{display:none}' +
'.shell.expanded .chev{transform:rotate(180deg)}' +
'.rank b{color:var(--text)}' +
'.killx{display:none}' +
'.shell.expanded .killx{display:flex}' +
'.actions{display:none;align-items:center;gap:7px;margin-top:auto;padding-top:12px;' +
' flex-wrap:nowrap}' +
'.shell.expanded .actions{display:flex}' +
'.cta{background:var(--ink);color:#fff;text-decoration:none;font-size:12.5px;font-weight:700;' +
' padding:7px 7px 7px 13px;border-radius:999px;display:inline-flex;align-items:center;gap:7px;' +
' flex:none;white-space:nowrap;' +
' transition:transform 220ms var(--spring)}' +
'.cta .dot{width:22px;height:22px;border-radius:50%;background:rgba(255,255,255,.16);' +
' display:grid;place-items:center;font-size:13px;transition:transform 220ms var(--spring)}' +
'.cta:active{transform:scale(.97)}' +
'.right{margin-left:auto;display:flex;align-items:center;gap:6px;flex:none}' +
'.sel{position:relative;display:inline-flex}' +
'.sel:after{content:"";position:absolute;right:13px;top:50%;width:6px;height:6px;' +
' border-right:2px solid currentColor;border-bottom:2px solid currentColor;' +
' transform:translateY(-70%) rotate(45deg);pointer-events:none;opacity:.6}' +
'.sel select{font:inherit;font-size:12px;font-weight:700;padding:7px 25px 7px 10px;' +
' border-radius:999px;border:2px solid var(--line);background:#F8F9FA;color:var(--text);' +
' cursor:pointer;-webkit-appearance:none;appearance:none;' +
' transition:border-color 200ms var(--spring),background-color 200ms var(--spring)}' +
'.sel[data-status="Applied"] select{background:#E7F5FF;border-color:#A5D8FF;color:#1864AB}' +
'.sel[data-status="OA"] select{background:#FFF9DB;border-color:#FFE066;color:#9C6600}' +
'.sel[data-status="Interview"] select{background:#FFF4E6;border-color:#FFC078;color:#C25100}' +
'.sel[data-status="Offer"] select{background:#D3F9D8;border-color:#8CE99A;color:#2B8A3E}' +
'.sel[data-status="Rejected"] select{background:#FFE3E3;border-color:#FFA8A8;color:#C92A2A}' +
'.prompt{font-size:15px;line-height:1;color:var(--blue);background:#EAF3FE;' +
' border:2px solid #A5D8FF;border-radius:50%;padding:0;cursor:pointer;flex:none;' +
' width:32px;height:32px;display:inline-flex;align-items:center;justify-content:center;' +
' transition:transform 200ms var(--spring),background-color 200ms var(--spring)}' +
'.prompt:active{transform:scale(.9)}' +
'.reachout{font-size:15px;line-height:1;color:#2B8A3E;background:#EBFBEE;' +
' border:2px solid #8CE99A;border-radius:50%;padding:0;cursor:pointer;flex:none;' +
' width:32px;height:32px;display:inline-flex;align-items:center;justify-content:center;' +
' transition:transform 200ms var(--spring),background-color 200ms var(--spring)}' +
'.reachout:active{transform:scale(.9)}' +
'.addbank{font-size:19px;font-weight:800;line-height:1;color:#5F3DC4;background:#F3F0FF;' +
' border:2px solid #B197FC;border-radius:50%;padding:0;cursor:pointer;flex:none;' +
' width:32px;height:32px;display:inline-flex;align-items:center;justify-content:center;' +
' transition:transform 200ms var(--spring),background-color 200ms var(--spring)}' +
'.addbank:active{transform:scale(.9)}' +
'.addbank.done{color:#2B8A3E;background:#EBFBEE;border-color:#8CE99A}' +
'.killx{position:absolute;top:10px;right:10px;z-index:3;width:26px;height:26px;' +
' border-radius:50%;align-items:center;justify-content:center;padding:0;' +
' font-size:12px;font-weight:800;line-height:1;color:var(--soft);' +
' background:rgba(255,255,255,.92);border:2px solid var(--line);cursor:pointer;' +
' transition:transform 200ms var(--spring),color 200ms var(--spring),' +
' border-color 200ms var(--spring),background-color 200ms var(--spring)}' +
'.killx:active{transform:scale(.9)}' +
'.osec{margin-top:16px}' +
'.osec h4{margin:0 0 6px;font-size:12.5px;font-weight:800;color:var(--ink);display:flex;' +
' justify-content:space-between;align-items:center;gap:8px}' +
'.ocount{font-size:11px;font-weight:600;color:var(--soft)}' +
'.osec textarea{width:100%;border:2px solid var(--line);border-radius:12px;padding:12px;' +
' resize:vertical;color:var(--text);font:13px/1.5 ui-monospace,Menlo,Consolas,monospace;height:80px}' +
'.olinks{display:flex;flex-direction:column;gap:6px}' +
'.olinks a{color:var(--blue);font-weight:600;font-size:13.5px;text-decoration:none}' +
'.olinks a:hover{text-decoration:underline}' +
'.copybtn{font:inherit;font-size:11.5px;font-weight:700;color:var(--blue);background:#EAF3FE;' +
' border:2px solid #A5D8FF;border-radius:999px;padding:6px 11px;cursor:pointer;margin-top:6px}' +
'.copybtn:active{transform:scale(.97)}' +
'.warn{background:#FFF9DB;border:2px solid #FFE066;border-radius:12px;padding:10px 14px;' +
' font-size:13px;color:#9C6600;margin-top:10px}' +
'.emailrow{display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:13.5px}' +
'.emailrow b{color:var(--ink)}' +
'.candlist{margin:0;font-size:12.5px;color:var(--text)}' +
'.candlist div{display:flex;justify-content:space-between;gap:8px;padding:5px 0;' +
' border-top:1px solid var(--line)}' +
'.candlist div:first-child{border-top:0}' +
'.modal{position:fixed;inset:0;background:rgba(17,20,24,.55);display:none;z-index:200;' +
' padding:20px;overflow:auto}' +
'.modal.show{display:block}' +
'.sheetbox{max-width:780px;margin:36px auto;background:#fff;border-radius:18px;padding:22px}' +
'.sheetbox h3{margin:0 0 4px;font-size:19px;font-weight:800;color:var(--ink)}' +
'.sheetbox p{margin:0 0 14px;font-size:13.5px;color:var(--soft)}' +
'.sheetbox textarea{width:100%;height:330px;border:2px solid var(--line);border-radius:12px;' +
' padding:14px;resize:vertical;color:var(--text);' +
' font:12.5px/1.55 ui-monospace,Menlo,Consolas,monospace}' +
'.modalbtns{display:flex;gap:10px;margin-top:14px}' +
'.kill{font:inherit;font-size:12px;font-weight:700;color:var(--soft);background:transparent;' +
' border:2px solid var(--line);border-radius:999px;padding:7px 11px;cursor:pointer;' +
' flex:none;white-space:nowrap;' +
' transition:transform 200ms var(--spring),color 200ms var(--spring),' +
' border-color 200ms var(--spring),background-color 200ms var(--spring)}' +
'.kill:active{transform:scale(.97)}' +

/* more roles: hard refresh */
'.rfx{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:18px 0 0}' +
'.rfxconfirm{background:#FFF9DB;border:2px solid #FFE066;border-radius:14px;' +
' padding:13px 16px;font-size:13px;color:#9C6600;font-weight:600}' +
'.rfxrun{margin:18px 0 0;background:var(--card);border:2px solid var(--line);' +
' border-radius:14px;padding:14px 16px}' +
'.rfxbar{height:8px;border-radius:999px;background:#EFEBE0;overflow:hidden}' +
'.rfxbar i{display:block;height:100%;width:0;border-radius:999px;' +
' background:var(--accent);transition:width 320ms var(--spring)}' +
'.rfxfoot{display:flex;align-items:center;justify-content:space-between;gap:10px;' +
' margin-top:10px}' +

/* statistics */
'.ranges{display:flex;gap:8px;flex-wrap:wrap;margin:16px 0 4px}' +
'.rbtn{font:inherit;font-size:13px;font-weight:700;color:var(--soft);background:var(--card);' +
' border:1.5px solid var(--line);border-radius:999px;padding:8px 15px;cursor:pointer;' +
' transition:background-color 180ms var(--spring),color 180ms var(--spring),' +
' border-color 180ms var(--spring)}' +
'.rbtn:hover{border-color:#ADB5BD}' +
'.rbtn.on{background:var(--ink);border-color:var(--ink);color:#fff}' +
'.chartcard{background:var(--card);border:2px solid var(--line);border-radius:18px;' +
' padding:18px 18px 12px;margin:16px 0 0}' +
'.chartcard h4{margin:0 0 2px;font-size:15px;font-weight:800;color:var(--title)}' +
'.chartcard .hint{margin:0 0 14px;font-size:12.5px;color:var(--soft)}' +
// the chart scrolls inside its own box rather than stretching the page sideways
'.chartscroll{overflow-x:auto;overflow-y:hidden}' +
'.chartsvg{display:block;min-width:520px;width:100%;height:auto}' +
'.chartsvg .grid{stroke:var(--line);stroke-width:1}' +
'.chartsvg .axis{fill:var(--soft);font-size:11px;font-weight:600}' +
'.chartsvg .bar{rx:3}' +
'.legend{display:flex;gap:16px;flex-wrap:wrap;margin:12px 0 4px;font-size:12.5px;' +
' font-weight:600;color:var(--text)}' +
'.legend i{width:11px;height:11px;border-radius:3px;display:inline-block;margin-right:6px}' +
'.funnel{display:grid;grid-template-columns:repeat(auto-fit,minmax(132px,1fr));gap:11px;' +
' margin:16px 0 0}' +
'.fcell{background:var(--card);border:2px solid var(--line);border-radius:15px;' +
' padding:15px 14px}' +
'.fcell b{display:block;font-size:27px;font-weight:800;line-height:1;color:var(--title)}' +
'.fcell span{display:block;margin-top:6px;font-size:12px;font-weight:600;color:var(--soft)}' +

/* opening soon */
'h2.sec{font-size:22px;font-weight:800;color:var(--ink);margin:44px 0 14px;letter-spacing:-.01em}' +

/* status tabs: one list at a time, with the size of every other list visible */
'.btabs{display:flex;gap:7px;flex-wrap:wrap;margin:20px 0 22px}' +
'.btab{font:inherit;font-size:13.5px;font-weight:700;color:var(--soft);background:var(--card);' +
' border:1.5px solid var(--line);border-radius:999px;padding:9px 15px;cursor:pointer;' +
' display:flex;align-items:center;gap:8px;transition:background .12s,border-color .12s,color .12s}' +
'.btab b{font-size:12px;font-weight:800;background:#F1F3F5;color:var(--soft);' +
' border-radius:999px;padding:1px 8px;min-width:18px;text-align:center}' +
'.btab.hollow{opacity:.5}' +
'.btab.on{background:var(--ink);border-color:var(--ink);color:#fff;opacity:1}' +
'.btab.on b{background:rgba(255,255,255,.22);color:#fff}' +
'.btab.live.on{background:#55703F;border-color:#55703F}' +
'.btab.offer.on{background:#B08344;border-color:#B08344}' +
'.btab.apply.on{background:var(--accent);border-color:var(--accent)}' +
'.btab:active{transform:scale(.97)}' +
'.bhint{margin:16px 0 12px;font-size:13px;color:var(--soft);font-weight:600}' +
'.bempty{padding:34px 16px}' +
// a card that just changed status flashes so it can be found in its new tab
'.shell.moved .card{box-shadow:0 0 0 2px var(--accent),0 8px 22px rgba(0,0,0,.10)}' +
'.shell.moved{animation:rise .3s cubic-bezier(.2,.7,.3,1)}' +
'.tablewrap{background:var(--card);border:2px solid var(--line);border-radius:18px;' +
' overflow:hidden}' +
'table{width:100%;border-collapse:collapse;font-size:15px}' +
'th{text-align:left;background:#F1F3F5;color:var(--soft);font-size:12px;font-weight:700;' +
' padding:12px 16px;text-transform:uppercase;letter-spacing:.04em}' +
'td{padding:13px 16px;border-top:2px solid #F1F3F5;vertical-align:middle}' +
'td.b{font-weight:700;color:var(--ink)}' +
'td a{color:var(--blue);font-weight:600}' +
'tr.live td{background:#F4FCF5}' +
'.nw{white-space:nowrap;font-weight:600}' +
'.empty{color:var(--soft);padding:28px 16px;text-align:center}' +
'footer{margin-top:40px;text-align:center;font-size:13px;color:var(--soft);font-weight:500}' +

/* toast */
'.toast{position:fixed;left:50%;bottom:26px;transform:translate(-50%,16px);opacity:0;' +
' background:var(--ink);color:#fff;font-size:15px;font-weight:600;padding:14px 20px;' +
' border-radius:14px;display:flex;align-items:center;gap:16px;pointer-events:none;z-index:100;' +
' transition:opacity 240ms var(--spring),transform 240ms var(--spring)}' +
'.toast.show{opacity:1;transform:translate(-50%,0);pointer-events:auto}' +
'.toast button{font:inherit;font-size:14px;font-weight:700;color:#FFD43B;background:none;' +
' border:0;cursor:pointer;padding:0}' +

'#pageSally .slywrap{--sb:#0A0E16;--sp:#10161F;--sl:#1E2836;--st:#8B98AC;' +
' --sh:#E8EDF4;--sa:#F59F00;background:var(--sb);border-radius:20px;' +
' padding:26px 26px 30px;color:var(--st);font-size:13.5px;' +
' border:1px solid #131A26}' +
'#pageSally .slyactions{display:flex;flex-direction:column;align-items:flex-end;gap:7px}' +
'#pageSally .slyrun{font:800 11px ui-monospace,Menlo,Consolas,monospace;' +
' letter-spacing:.12em;color:#0A0E16;background:var(--sa);border:0;border-radius:9px;' +
' padding:9px 15px;cursor:pointer}' +
'#pageSally .slyrun:disabled{opacity:.45;cursor:default}' +
'#pageSally .slyrun:active{transform:scale(.96)}' +
'#pageSally .slyeta{font-size:10px;color:#48566B;text-align:right;letter-spacing:.04em}' +
'#pageSally .slyrunmsg{font-size:11px;color:#5B6B82;text-align:right;max-width:260px}' +
'#pageSally .slyrunmsg.slyok{color:#3FB950}#pageSally .slyrunmsg.slybad{color:#F85149}' +
'#pageSally .slyid{display:flex;align-items:center;gap:14px}' +
'#pageSally .slyava{width:58px;height:58px;flex:none;' +
' filter:drop-shadow(0 0 7px rgba(245,159,0,.45))}' +
'#pageSally .slyring{animation:slyspin 7s linear infinite;' +
' transform-origin:32px 32px}' +
'#pageSally .slyeye{animation:slyblink 4.5s ease-in-out infinite}' +
'@keyframes slyspin{to{transform:rotate(360deg)}}' +
'@keyframes slyblink{0%,92%,100%{ry:3.1}94%,98%{ry:.6}}' +
'#pageSally .slycharts{display:grid;grid-template-columns:1fr 1.35fr;gap:12px;' +
' margin-top:12px}' +
'#pageSally .slychart h3{margin:0 0 12px}' +
'#pageSally .slylegend{display:flex;flex-direction:column;gap:7px;font-size:11.5px}' +
'#pageSally .slylegend li{display:flex;align-items:center;gap:8px;list-style:none}' +
'#pageSally .slyswatch{width:9px;height:9px;border-radius:2px;flex:none}' +
'#pageSally .slylegend b{color:var(--sh);margin-left:auto;font-family:ui-monospace,' +
' Menlo,Consolas,monospace;font-weight:600}' +
'#pageSally .slybars{display:flex;align-items:flex-end;gap:5px;height:96px}' +
'#pageSally .slybar{flex:1;background:#243043;border-radius:3px 3px 0 0;' +
' min-height:2px;position:relative;transition:background-color 200ms}' +
'#pageSally .slybar.hi{background:#F59F00}' +
'#pageSally .slybarwrap{display:flex;flex-direction:column;align-items:center;' +
' flex:1;height:100%;justify-content:flex-end;gap:6px}' +
'#pageSally .slybarwrap span{font-size:9.5px;color:#48566B;' +
' font-family:ui-monospace,Menlo,Consolas,monospace}' +
'#pageSally .slymore{background:none;border:0;color:var(--sa);cursor:pointer;' +
' font:600 11.5px inherit;padding:8px 0 0;text-align:left}' +
'#pageSally .slytoggle{width:100%;display:flex;justify-content:space-between;' +
' align-items:center;background:var(--sp);border:1px solid var(--sl);' +
' border-radius:14px;padding:14px 18px;cursor:pointer;color:var(--sh);' +
' font:700 11px ui-monospace,Menlo,Consolas,monospace;text-transform:uppercase;' +
' letter-spacing:.14em;margin-top:12px}' +
'#pageSally .slytoggle .slychev{transition:transform 200ms;color:#5B6B82}' +
'#pageSally .slytoggle.on .slychev{transform:rotate(180deg)}' +
'#pageSally .slydials{display:none;margin-top:12px}' +
'#pageSally .slydials.on{display:block}' +
'#pageSally .slyhead{display:flex;justify-content:space-between;' +
' align-items:flex-start;gap:14px;flex-wrap:wrap;margin-bottom:18px}' +
'#pageSally h2{margin:0;font:800 24px/1 ui-monospace,Menlo,Consolas,monospace;' +
' color:var(--sh);letter-spacing:.24em}' +
'#pageSally .slysub{margin-top:7px;font-size:11.5px;text-transform:uppercase;' +
' letter-spacing:.14em;color:#5B6B82}' +
'#pageSally .slydot{display:inline-block;width:8px;height:8px;border-radius:50%;' +
' background:#3FB950;margin-right:8px;box-shadow:0 0 8px #3FB950;' +
' animation:slypulse 2.2s infinite}' +
'@keyframes slypulse{50%{opacity:.35}}' +
'#pageSally .slynext{font:600 11.5px/1.6 ui-monospace,Menlo,Consolas,monospace;' +
' color:#5B6B82;text-align:right}' +
'#pageSally .slyload,#pageSally .slyempty{color:#5B6B82;font-style:italic}' +
'#pageSally .slycards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}' +
'#pageSally .slycard{background:var(--sp);border:1px solid var(--sl);' +
' border-radius:14px;padding:15px 16px}' +
'#pageSally .slycard b{display:block;font:700 26px/1.1 ui-monospace,Menlo,' +
' Consolas,monospace;color:var(--sh)}' +
'#pageSally .slycard b em{font-style:normal;font-size:13px;color:var(--sa)}' +
'#pageSally .slycard span{display:block;margin-top:6px;font-size:10.5px;' +
' text-transform:uppercase;letter-spacing:.1em;color:#5B6B82}' +
'#pageSally .slycols{display:grid;grid-template-columns:1.55fr 1fr;gap:12px;' +
' margin-top:12px;align-items:start}' +
'#pageSally .slypanel{background:var(--sp);border:1px solid var(--sl);' +
' border-radius:14px;padding:16px 18px}' +
'#pageSally .slypanel h3{margin:0 0 10px;font-size:11px;font-weight:800;' +
' text-transform:uppercase;letter-spacing:.14em;color:#5B6B82}' +
'#pageSally table{width:100%;border-collapse:collapse}' +
'#pageSally th{text-align:left;font-size:10px;text-transform:uppercase;' +
' letter-spacing:.1em;color:#48566B;padding:6px 8px;border-bottom:1px solid var(--sl)}' +
'#pageSally td{padding:7px 8px;border-top:1px solid #141C29;font-size:12.5px;' +
' color:var(--st);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;' +
' max-width:180px}' +
'#pageSally .slynum{font-family:ui-monospace,Menlo,Consolas,monospace;' +
' color:var(--sh)}' +
'#pageSally .slyok{color:#3FB950}#pageSally .slybad{color:#F85149}' +
'#pageSally .slydim{color:#48566B}' +
'#pageSally .slyparam{padding:11px 0;border-top:1px solid #141C29}' +
'#pageSally .slyparam:first-of-type{border-top:0}' +
'#pageSally .slyparam label{display:block;font-size:12.5px;font-weight:700;' +
' color:var(--sh)}' +
'#pageSally .slyparam small{display:block;margin:3px 0 8px;font-size:11.5px;' +
' color:#5B6B82;line-height:1.5}' +
'#pageSally .slyrow{display:flex;gap:8px}' +
'#pageSally .slyrow input{flex:1;min-width:0;background:#0C1220;' +
' border:1px solid var(--sl);border-radius:9px;padding:8px 11px;' +
' color:var(--sh);font:600 13px ui-monospace,Menlo,Consolas,monospace;outline:none}' +
'#pageSally .slyrow input:focus{border-color:var(--sa)}' +
'#pageSally .slysave{font:700 12px ui-monospace,Menlo,Consolas,monospace;' +
' color:#0A0E16;background:var(--sa);border:0;border-radius:9px;' +
' padding:8px 14px;cursor:pointer;flex:none}' +
'#pageSally .slysave:active{transform:scale(.96)}' +
'#pageSally .slymsg{font-size:11.5px;margin-top:6px;min-height:14px}' +
'@media (max-width:900px){#pageSally .slycards{grid-template-columns:1fr 1fr}' +
' #pageSally .slycols{grid-template-columns:1fr}}' +

'@media (hover:hover) and (pointer:fine){' +
' .linkbtn:hover .dot{transform:translate(2px,-2px)}' +
' .cta:hover .dot{transform:translate(2px,-2px)}' +
' .shell:hover{border-color:#ADB5BD}' +
' .shell h3 a:hover{color:var(--blue)}' +
' .kill:hover{color:var(--red);border-color:#FFC9C9;background:#FFF5F5}' +
' .killx:hover{color:var(--red);border-color:#FFC9C9;background:#FFF5F5}' +
' .prompt:hover{background-color:#DDEBFC}' +
' .reachout:hover{background-color:#DDF7DE}' +
' .addbank:hover{background-color:#E5DBFF}' +
' .sel select:hover{border-color:#ADB5BD}' +
'}' +
'@media (max-width:640px){' +
' .top{padding:20px}.top h1{font-size:24px}' +
' .stats{grid-template-columns:repeat(2,1fr)}.inner{padding:17px 16px 16px 20px}' +
' .shell h3{font-size:18px}.actions{flex-wrap:wrap}.pills{flex-wrap:wrap}' +
' .right{margin-left:0;width:100%}.sel{flex:1}.sel select{width:100%}' +
'}' +
'@media (prefers-reduced-motion:reduce){' +
' *{animation-duration:1ms!important;animation-delay:0ms!important;transition-duration:1ms!important}' +
' .shell{opacity:1;transform:none}' +
'}' +
/* --- the dense table (design options B + C) --- */
'.jbtwrap{overflow-x:auto;border:2px solid var(--line);border-radius:14px;' +
' background:var(--card);margin-top:12px}' +
'.jbt td.locc{max-width:190px;overflow:hidden;text-overflow:ellipsis}' +
'.jbt .soond{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:10.5px;' +
' color:var(--soft);margin-left:7px;white-space:nowrap}' +
'.jbt tr.shell.dragging{opacity:.45}' +
'.jbt tr.shell.dragging td{background:var(--sand)}' +
'.jbt{width:100%;border-collapse:collapse;font-size:13.5px;min-width:640px}' +
'.jbt th{text-align:left;font-size:10.5px;font-weight:700;letter-spacing:.09em;' +
' text-transform:uppercase;color:var(--soft);padding:11px 14px;' +
' border-bottom:2px solid var(--line);background:var(--sand);white-space:nowrap}' +
'.jbt td{padding:12px 14px;border-bottom:1px solid var(--line);vertical-align:middle}' +
'.jbt tbody:last-child tr:last-child td{border-bottom:0}' +
'.jbt tr.grp td{background:var(--sand);padding:9px 14px;' +
' font-family:ui-monospace,Menlo,Consolas,monospace;font-size:10.5px;font-weight:700;' +
' letter-spacing:.1em;text-transform:uppercase;color:var(--text);' +
' border-top:2px solid var(--line);border-bottom:1px solid var(--line)}' +
'.jbt .ghint{font-weight:400;color:var(--soft);text-transform:none;letter-spacing:.02em}' +
'.jbt .gcnt{font-weight:400;color:var(--soft);margin-left:6px}' +
'.jbt tr.bempty td{color:var(--soft);font-size:12.5px;padding:13px 14px}' +
// tr.shell keeps the class every handler looks for; the card CSS it inherits
// (flex, radius, entrance animation) has to be flattened back to a table row
'.jbt tr.shell{display:table-row;background:transparent;border:0;border-radius:0;' +
' padding:0;opacity:1;transform:none;animation:none;box-shadow:none;overflow:visible;cursor:pointer}' +
'.jbt tr.shell:hover td{background:var(--sand)}' +
'.jbt tr.shell.is-top td{background:#FFFBEB}' +
'.jbt tr.shell.is-closed{opacity:.55}' +
'.jbt tr.shell.leaving{opacity:0}' +
'.jbt tr.shell.moved td{background:var(--sand)}' +
'.jbt td.railc{width:3px;padding:0 0 0 4px}' +
'.jbt .railbar{display:block;width:3px;height:34px;border-radius:2px;background:var(--line)}' +
'.jbt .st-act{background:var(--amber)}.jbt .st-open{background:var(--green)}' +
'.jbt .st-watch{background:var(--amber)}.jbt .st-wait{background:var(--accent)}' +
'.jbt .st-live{background:var(--blue)}.jbt .st-offer{background:var(--green)}' +
'.jbt .st-closed{background:var(--red)}' +
'.jbt td.carc{width:18px;padding-right:2px}' +
'.jbt .car{display:inline-block;color:var(--soft);font-size:11px;' +
' transition:transform 150ms var(--spring)}' +
'.jbt tr.shell.expanded .car{transform:rotate(90deg)}' +
'.jbt td.rolec{min-width:220px}' +
'.jbt .co{display:block;margin:0;padding:0;border:0;font-size:13.5px;font-weight:700;' +
' color:var(--title);line-height:1.3;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}' +
'.jbt .prog{display:block;font-size:12.3px;color:var(--soft);line-height:1.35;' +
' white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:52ch}' +
'.jbt .mini{display:inline-block;font-size:9px;font-weight:800;letter-spacing:.04em;' +
' border-radius:999px;padding:1px 6px;vertical-align:1px}' +
'.jbt .mnew{color:var(--blue);background:#EAF3FE;border:1px solid #A5D8FF}' +
'.jbt .mtop{color:#8a6d00;background:#FFF9DB;border:1px solid #FFE066}' +
'.jbt .mfav{color:var(--red);background:transparent;border:0;padding:0}' +
'.jbt .jpill{display:inline-block;font-family:ui-monospace,Menlo,Consolas,monospace;' +
' font-size:10px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;' +
' padding:3px 8px;border-radius:20px;white-space:nowrap}' +
'.jbt .jpill+.jpill{margin-left:5px}' +
'.jbt .p-not{background:var(--sand);color:var(--soft)}' +
'.jbt .p-applied{background:#EAF3FE;color:#1971C2}' +
'.jbt .p-live{background:#EDF2FF;color:#3B5BDB}' +
'.jbt .p-offer,.jbt .p-open{background:#E4F3EA;color:#177245}' +
'.jbt .p-rej{background:#FBE9E7;color:#A62B21}' +
'.jbt .p-soon{background:#FBF0DE;color:#9A5B08}' +
'.jbt .p-urgent{background:#FBE9E7;color:#A62B21}' +
'.jbt .p-cat{background:var(--sand)}' +
'.jbt td.numc{font-family:ui-monospace,Menlo,Consolas,monospace;' +
' font-variant-numeric:tabular-nums;font-size:12px;color:var(--text);white-space:nowrap}' +
'.jbt td.numc.dim,.jbt .dim{color:var(--soft)}' +
'.jbt td.statc,.jbt td.scorec{white-space:nowrap}' +
'.jbt .score{margin:0;font-size:12px;font-weight:800;padding:2px 8px;border-radius:7px;color:#fff}' +
'.jbt .score em{display:none}' +
// the expansion: same .expanded class and .why/.actions rules the cards used,
// so fetchDesc, the status select and every button keep working untouched
'.jbt .det{display:none}' +
'.jbt tr.shell.expanded .det{display:block;margin:12px 0 6px}' +
'.jbt tr.shell.expanded .prog{white-space:normal;overflow:visible}' +
'.jbt .det .why{margin:0 0 8px;max-width:80ch}' +
'.jbt .dmeta{display:flex;flex-wrap:wrap;gap:7px 22px;margin:0 0 12px;' +
' font-family:ui-monospace,Menlo,Consolas,monospace;font-size:11px;color:var(--soft)}' +
'.jbt .dmeta b{color:var(--text);font-weight:600}' +
'.jbt .det .actions{padding-top:2px;border-top:0}' +
'.jbt .killx{position:static;display:none;width:26px;height:26px}' +
'.jbt tr.shell.expanded .killx{display:flex}' +
// the watchlist X wears the same face as the job X; only the class differs,
// because .killx also opens the bank removal modal
'.jbt .soonx{display:none;align-items:center;justify-content:center;' +
' width:26px;height:26px;font-size:13px;line-height:1;color:var(--soft);' +
' background:var(--card);border:1.5px solid var(--line);border-radius:8px;' +
' cursor:pointer;padding:0}' +
'.jbt tr.shell.expanded .soonx{display:flex}' +
'.jbt .soonx:hover{color:var(--red);border-color:#FFC9C9;background:#FFF5F5}' +
'.jbt .soonx:active{transform:scale(.9)}' +
'.jbt .skipx{display:none;align-items:center;justify-content:center;' +
' width:26px;height:26px;font-size:13px;line-height:1;color:var(--soft);' +
' background:var(--card);border:1.5px solid var(--line);border-radius:8px;' +
' cursor:pointer;padding:0}' +
'.jbt tr.shell.expanded .skipx{display:flex}' +
'.jbt .skipx:hover{color:var(--red);border-color:#FFC9C9;background:#FFF5F5}' +
'.jbt .skipx:active{transform:scale(.9)}' +
'.jbt .jbtn{font:inherit;font-size:12px;font-weight:600;color:var(--text);' +
' background:var(--card);border:1.5px solid var(--line);border-radius:8px;' +
' padding:5px 11px;cursor:pointer}' +
'.jbt .jbtn:hover{border-color:var(--soft)}' +
'@media(max-width:680px){.jbt th:nth-child(5),.jbt td.numc{display:none}}' +
'</style></head><body><div class="wrap">' +

'<nav class="rail">' +
 '<div class="railhead">' +
  '<div class="railtitle">Aug&#8217;s Job Bank</div>' +
  '<button class="railtoggle" id="railToggle" title="Collapse the sidebar" ' +
    'aria-label="Collapse the sidebar">&#171;</button>' +
 '</div>' +
 '<div class="railsub">2027 internships &amp; placements</div>' +
 '<button class="pagetab on" data-page="bank"><span class="ic">' + ICONS_.bank +
   '</span><span class="lbl">Job Bank</span></button>' +
 '<button class="pagetab" data-page="more"><span class="ic">' + ICONS_.more +
   '</span><span class="lbl">More roles</span></button>' +
 '<button class="pagetab" data-page="find"><span class="ic">' + ICONS_.find +
   '</span><span class="lbl">Find roles</span></button>' +
 '<button class="pagetab" data-page="stats"><span class="ic">' + ICONS_.stats +
   '</span><span class="lbl">Statistics</span></button>' +
 '<button class="pagetab" data-page="sally"><span class="ic">' + ICONS_.sally +
   '</span><span class="lbl">Sally</span></button>' +
 '<div class="railfoot">' + esc(today) + '<br>' +
   '<a href="' + esc(sheetUrl) + '" target="_blank" rel="noopener">Open the spreadsheet &#8599;</a><br><br>' +
   'Apply early. Apply often.</div>' +
'</nav>' +

'<div class="page">' +

'<div id="pageBank">' +

(urgent.length ?
'<div class="alert"><h2>&#9200; Closing within 7 days</h2><ul>' +
 urgent.map(function (j) {
   return '<li><a href="' + esc(j['Link']) + '" target="_blank" rel="noopener">' +
     esc(j['Company']) + ' — ' + esc(j['Role']) + '</a> · ' + esc(j['Deadline']) + '</li>';
 }).join('') + '</ul></div>' : '') +

'<div class="rfx" id="ckIdle">' +
  '<button class="kill" id="ckGo">Check for closed postings</button>' +
  '<span class="ocount">Opens every posting in the tab you are on and marks the ' +
    'ones that have come down.</span>' +
'</div>' +
'<div class="rfxrun" id="ckRun" style="display:none">' +
  '<div class="rfxbar"><i id="ckFill"></i></div>' +
  '<div class="rfxfoot"><span class="ocount" id="ckMsg"></span>' +
    '<button class="kill" id="ckStop">Stop</button></div>' +
'</div>' +
'<input class="searchbox" type="search" data-target="list" ' +
  'placeholder="Search company, role or description&hellip;">' +
'<div class="filters" data-target="list">' + filterControls_('list') + '</div>' +
// Grouped by where each role has got to, most time-critical first: an interview
// next week matters more than a role you have not started. Sections hide
// themselves when empty, and a status change moves its card across live.
'<main id="list">' +
 (jobs.length || soonRows.length ?
  // ONE table, grouped by band rows — design option B's grouping with option
  // C's dense two-line rows. Groups are tbodys so the client's regroup() can
  // move a row between them with a plain appendChild.
  // Slice tabs: the table stays whole and grouped; a tab just narrows the view
  // to one band. "All" is the default and puts everything back.
  '<div class="btabs" id="btabs">' +
   '<button class="btab all on" data-b="all">All<b>' +
     (jobs.length + soonRows.length) + '</b></button>' +
   '<button class="btab apply" data-b="apply">Act now<b>' +
     (bucket('apply').length + soonOpen.length) + '</b></button>' +
   '<button class="btab watch" data-b="watch">Watchlist<b>' +
     soonWait.length + '</b></button>' +
   '<button class="btab waiting" data-b="waiting">In flight<b>' +
     bucket('waiting').length + '</b></button>' +
   '<button class="btab closed" data-b="closed">Closed out<b>' +
     bucket('closed').length + '</b></button>' +
  '</div>' +
  '<div class="jbtwrap"><table class="jbt"><thead><tr>' +
   '<th class="railh"></th><th class="carh"></th><th>Role</th><th>Status</th>' +
   '<th>Location</th><th class="scoreh">Score</th></tr></thead>' +
  (function () {
    function tbody(key, label, hint, blank, rowsHtml, n) {
      return '<tbody class="bucket" id="grid-' + key + '">' +
       '<tr class="grp"><td colspan="6">' + label +
         (hint ? '<span class="ghint"> &mdash; ' + hint + '</span>' : '') +
         '<b class="gcnt">&middot; ' + n + '</b></td></tr>' +
       '<tr class="bempty"' + (n ? ' style="display:none"' : '') +
         '><td colspan="6">' + blank + '</td></tr>' +
       rowsHtml + '</tbody>';
    }
    // Watchlist rows that are OPEN NOW belong in Act now — they are the rows
    // most worth a click today, which is the whole point of the top band.
    var act = bucket('apply');
    return tbody('apply', BUCKETS[0].label, BUCKETS[0].hint, BUCKETS[0].blank,
        soonOpen.map(soonRow).join('') + act.map(card).join(''),
        act.length + soonOpen.length) +
      tbody('watch', 'Watchlist', 'not open yet',
        'Nothing tracked yet — add programmes on the Opening soon page.',
        soonWait.map(soonRow).join(''), soonWait.length) +
      tbody('waiting', BUCKETS[1].label, BUCKETS[1].hint, BUCKETS[1].blank,
        bucket('waiting').map(card).join(''), bucket('waiting').length) +
      tbody('closed', BUCKETS[2].label, BUCKETS[2].hint, BUCKETS[2].blank,
        bucket('closed').map(card).join(''), bucket('closed').length);
  })() + '</table></div>' :
 '<p class="empty">Nothing in the bank yet — the pipeline runs each morning at 07:00 UTC.</p>') +
'</main>' +

'</div>' +

'<div id="pageMore" style="display:none">' +
 '<p class="sub2">The top 50 roles that were fetched and passed hard filters but scored ' +
   'below the bar, or that the Job Bank had no room for. Hit + to pull one in anyway.</p>' +
 '<div class="rfx" id="rfxIdle">' +
   '<button class="gobtn" id="rfxGo">Hard refresh</button>' +
   '<span class="ocount">Re-searches every company on your Watchlist and files ' +
     'anything new. Takes a few minutes.</span>' +
 '</div>' +
 '<div class="rfx rfxconfirm" id="rfxAsk" style="display:none">' +
   '<span>This searches each company one at a time and can run for several ' +
     'minutes. You can keep using the page, but leaving it stops the sweep.</span>' +
   '<button class="gobtn" id="rfxYes">Start</button>' +
   '<button class="kill" id="rfxNo">Cancel</button>' +
 '</div>' +
 '<div class="rfxrun" id="rfxRun" style="display:none">' +
   '<div class="rfxbar"><i id="rfxFill"></i></div>' +
   '<div class="rfxfoot"><span class="ocount" id="rfxMsg"></span>' +
     '<button class="kill" id="rfxStop">Stop</button></div>' +
 '</div>' +
 '<input class="searchbox" type="search" data-target="moreList" ' +
   'placeholder="Search company, role or description&hellip;">' +
 '<div class="filters" id="moreFilters" data-target="moreList"></div>' +
 '<main id="moreList">' +
   '<p class="empty">Loading&hellip;</p>' +
 '</main>' +
'</div>' +

'<div id="pageFind" style="display:none">' +
 '<p class="sub2">Search one company on demand — your archive first, then their ' +
   'Greenhouse/Lever/Ashby board, then Adzuna. Found nothing? Add it yourself below.</p>' +
 '<div class="searchrow">' +
   '<input class="searchbox" id="findco" type="search" ' +
     'placeholder="Company name — e.g. Apple, ESA, Mistral&hellip;">' +
   '<input class="searchbox" id="findterms" type="search" style="flex:1 1 160px" ' +
     'placeholder="Role words (optional)">' +
   '<button class="gobtn" id="findgo">Search</button>' +
 '</div>' +
 '<label class="togg" style="margin:11px 0 0">' +
   '<input type="checkbox" id="findall"> Show every role they have, not just ones that fit me' +
 '</label>' +
 '<p id="findnote" class="sub2" style="margin:14px 0 0"></p>' +
 '<div class="hits" id="findhits"></div>' +

 '<div class="panel" id="ownRole">' +
   '<h4>Add a role you found yourself</h4>' +
   '<p class="hint">For anything the search cannot reach — a LinkedIn post, a PDF, ' +
     'a careers page with no feed. It goes straight into the Job Bank and ignores ' +
     'the 10-role ceiling, because you asked for it by name. Not open yet? Tick ' +
     'the box below and it sits on the bank page&#8217;s Watchlist band instead.</p>' +
   '<div class="fgrid">' +
     '<div class="f"><label for="orCompany">Company *</label>' +
       '<input id="orCompany" placeholder="Apple"></div>' +
     '<div class="f"><label for="orRole">Role *</label>' +
       '<input id="orRole" placeholder="Software Engineering Intern 2027"></div>' +
     '<div class="f wide"><label for="orLink">Link</label>' +
       '<input id="orLink" placeholder="https://&hellip;"></div>' +
     '<div class="f"><label for="orLoc">Location</label>' +
       '<input id="orLoc" placeholder="London, UK"></div>' +
     '<div class="f"><label for="orDeadline">Deadline</label>' +
       '<input id="orDeadline" placeholder="31/01/2027"></div>' +
     '<div class="f"><label for="orDuration">Duration</label>' +
       '<input id="orDuration" placeholder="12 weeks"></div>' +
     '<div class="f wide"><label for="orDesc">What the role is</label>' +
       '<textarea id="orDesc" placeholder="Paste the bits worth keeping&hellip;"></textarea></div>' +
   '</div>' +
   '<label class="togg" style="margin:11px 0 0">' +
     '<input type="checkbox" id="orSoon"> Not open yet &mdash; track it as Opening soon' +
   '</label>' +
   '<div class="fgrid" id="orOpenWrap" style="display:none;margin-top:8px">' +
     '<div class="f"><label for="orOpen">Expected to open</label>' +
       '<input id="orOpen" type="date"></div>' +
   '</div>' +
   '<div class="frow"><button class="gobtn" id="orGo">Add to Job Bank</button>' +
     '<span class="ocount" id="orMsg"></span></div>' +
 '</div>' +
'</div>' +

'<div id="pageStats" style="display:none">' +
 '<p class="sub2">Every time you move a role forward it is recorded with a date, ' +
   'and that record is what this page draws.</p>' +
 '<div class="ranges" id="statRanges">' +
   '<button class="rbtn on" data-days="30">Past month</button>' +
   '<button class="rbtn" data-days="365">Past year</button>' +
   '<button class="rbtn" data-days="0">All time</button>' +
 '</div>' +
 '<div id="statBody"><p class="empty">Loading&hellip;</p></div>' +
'</div>' +

'<div id="pageSally" style="display:none"><div class="slywrap">' +
 '<div class="slyhead">' +
  '<div class="slyid">' +
  '<svg class="slyava" viewBox="0 0 64 64" aria-hidden="true">' +
    '<circle cx="32" cy="32" r="30" fill="none" stroke="#1E2836" stroke-width="2"/>' +
    '<circle cx="32" cy="32" r="30" fill="none" stroke="#F59F00" stroke-width="2" ' +
      'stroke-dasharray="47 141" stroke-linecap="round" class="slyring"/>' +
    '<circle cx="32" cy="32" r="22" fill="#0C1220"/>' +
    '<path d="M20 24c2-6 22-6 24 0" stroke="#2A3648" stroke-width="1.6" fill="none" ' +
      'stroke-linecap="round"/>' +
    '<circle cx="24.5" cy="30" r="3.1" fill="#F59F00" class="slyeye"/>' +
    '<circle cx="39.5" cy="30" r="3.1" fill="#F59F00" class="slyeye"/>' +
    '<path d="M24 40c3 3 13 3 16 0" stroke="#F59F00" stroke-width="1.6" fill="none" ' +
      'stroke-linecap="round" opacity=".85"/>' +
  '</svg>' +
  '<div><h2>SALLY</h2>' +
  '<div class="slysub"><span class="slydot"></span>autonomous recruiter &middot; ' +
    'always on</div></div></div>' +
  '<div class="slyactions">' +
    '<button class="slyrun" id="slyRunBtn">&#9656; RUN NOW</button>' +
    '<div class="slyeta">takes about 4&ndash;7 min</div>' +
    '<div class="slynext" id="slyNext"></div>' +
    '<div class="slyrunmsg" id="slyRunMsg"></div>' +
  '</div>' +
 '</div>' +
 '<div id="slyBody"><p class="slyload">Waking Sally&hellip;</p></div>' +
'</div></div>' +

'<footer>Scored 1&ndash;100 for fit and attainability &middot; build ' + BUILD_ + ' &middot; refresh for the latest</footer>' +
'</div>' +
'</div>' +

'<div class="modal" id="modal"><div class="sheetbox">' +
 '<h3 id="mtitle">Tailoring prompt</h3>' +
 '<p id="msub">Copy this, then paste it into Claude with your CV attached.</p>' +
 '<textarea id="mtext" readonly></textarea>' +
 '<div class="modalbtns">' +
   '<button class="cta" id="mcopy" style="border:0;cursor:pointer">Copy to clipboard</button>' +
   '<button class="kill" id="mclose">Close</button>' +
 '</div></div></div>' +

'<div class="modal" id="whyModal"><div class="sheetbox" style="max-width:520px">' +
 '<h3>Why are you removing this?</h3>' +
 '<p id="whySub">Optional — it tunes what gets scored highly next time.</p>' +
 '<div class="whyopts">' +
   '<button class="whyopt" data-r="Wrong field / discipline">Wrong field</button>' +
   '<button class="whyopt" data-r="Too senior or too much experience needed">Too senior</button>' +
   '<button class="whyopt" data-r="Location does not work">Location</button>' +
   '<button class="whyopt" data-r="Not interested in this company">Company</button>' +
   '<button class="whyopt" data-r="Unpaid or poor package">Package</button>' +
   '<button class="whyopt" data-r="Wrong timing / cycle">Timing</button>' +
   '<button class="whyopt" data-r="Already applied or heard back">Already applied</button>' +
 '</div>' +
 '<textarea id="whyText" placeholder="Anything else worth knowing (optional)"></textarea>' +
 // Sometimes the point of removing a role is to make the list shorter, so this
 // asks rather than assuming, and asks again each time.
 '<label class="togg" id="whyRepWrap" style="margin:12px 0 0">' +
   '<input type="checkbox" id="whyRep"> Pull in a replacement from More roles' +
 '</label>' +
 '<div class="modalbtns">' +
   '<button class="cta" id="whyGo" style="border:0;cursor:pointer">Remove</button>' +
   '<button class="kill" id="whySkip">Remove without a reason</button>' +
   '<button class="kill" id="whyCancel">Cancel</button>' +
 '</div></div></div>' +

'<div class="modal" id="outreachModal"><div class="sheetbox" style="max-width:660px">' +
 '<h3 id="otitle">Reach out</h3>' +
 '<p id="osub">7-10 business days after applying &middot; Tue-Thu mid-morning UK &middot; ' +
   'one follow-up, then stop.</p>' +
 '<div id="obigscheme" class="warn" style="display:none">This is a high-volume grad ' +
   'scheme (140:1 applications) &mdash; outreach here is close to a no-op, but costs ' +
   'nothing to try.</div>' +

 '<div class="osec"><h4>Find the person</h4><div class="olinks" id="olinks"></div></div>' +

 '<div class="osec"><h4>Likely email <span class="ocount" id="odomain"></span></h4>' +
   '<div id="oemailbox"></div></div>' +

 '<div class="osec"><h4>Connection note <span class="ocount" id="onoteCount"></span></h4>' +
   '<textarea id="onote" readonly></textarea>' +
   '<button class="copybtn" data-copy="onote">Copy note</button>' +
   '<div class="warn" style="margin-top:8px">Free LinkedIn allows only 3 notes/month &mdash; ' +
     'spend them on roles that matter.</div>' +
 '</div>' +

 '<div class="osec"><h4>LinkedIn DM <span class="ocount" id="odmCount"></span></h4>' +
   '<textarea id="odm" readonly></textarea>' +
   '<button class="copybtn" data-copy="odm">Copy DM</button>' +
 '</div>' +

 '<div class="osec"><h4>Email <span class="ocount" id="obodyCount"></span></h4>' +
   '<div class="emailrow"><b>Subject:</b> <span id="osubject"></span> ' +
     '<button class="copybtn" data-copy="osubject">Copy subject</button></div>' +
   '<textarea id="obody" readonly style="height:180px;margin-top:8px"></textarea>' +
   '<button class="copybtn" data-copy="obody">Copy email body</button>' +
 '</div>' +

 '<div class="modalbtns">' +
   '<span class="ocount" id="olast" style="margin-right:auto"></span>' +
   '<button class="cta" id="omark" style="border:0;cursor:pointer">Mark as contacted today</button>' +
   '<button class="kill" id="ocloseBtn">Close</button>' +
 '</div></div></div>' +

'<div class="toast" id="toast"><span id="tmsg"></span></div>' +

'<script>' +
'(function(){' +
'var toast=document.getElementById("toast"),tmsg=document.getElementById("tmsg"),timer=null;' +
'function say(t){tmsg.textContent=t;toast.classList.add("show");clearTimeout(timer);' +
' timer=setTimeout(function(){toast.classList.remove("show");},3500);}' +

// Every server call is sent the role's Link alongside its row number: rows move
// (a removal shifts everything below it up one) and a stale number reads the
// wrong role's data rather than failing, which is how the prompt button ended up
// handing back a different company's prompt.
'function linkOf(el){var s=el.closest(".shell");return (s&&s.dataset.link)||"";}' +
// after a Job Bank row is deleted, renumber the cards below it in place — a full
// re-render would lose scroll position, expanded cards and the search filter
'function shiftRows(removed){' +
' var list=document.getElementById("list");if(!list)return;' +
' list.querySelectorAll("[data-row]").forEach(function(el){' +
'  var r=Number(el.dataset.row);if(r>removed)el.dataset.row=String(r-1);});' +
'}' +

'document.addEventListener("change",function(e){' +
' var sel=e.target.closest(".sel select");if(!sel)return;' +
' var wrap=sel.parentNode,row=Number(sel.dataset.row),value=sel.value;' +
' var prev=wrap.dataset.status;wrap.dataset.status=value;sel.disabled=true;' +
' google.script.run.withSuccessHandler(function(res){sel.disabled=false;' +
'   if(res&&res.ok){say("Marked as "+value);regroup(sel.closest(".shell"),value);}' +
'   else{wrap.dataset.status=prev;sel.value=prev;say((res&&res.error)||"Could not save that");}' +
' }).withFailureHandler(function(){sel.disabled=false;wrap.dataset.status=prev;' +
'   sel.value=prev;say("Could not save — refresh and try again");' +
' }).setStatus(row,value,linkOf(sel));' +
'});' +

'document.addEventListener("click",function(e){' +
' var btn=e.target.closest(".killx");if(!btn)return;' +
' var shell=btn.closest(".shell");if(!shell)return;' +
' openWhy(btn,shell);' +
'});' +

/* ---- drag a row to reorder it inside its band ---- */
// Constrained to the row's own tbody: order across bands is what the status
// dropdown is for. The drop is written back by moving the sheet row, and the
// page renders the bank in sheet order, so the arrangement survives reloads.
// Watchlist rows are not draggable — their order is the tracker's business.
'var dragRow=null,dragWas=null;' +
'document.addEventListener("dragstart",function(e){' +
' var tr=e.target.closest("#list tr.shell");' +
' if(!tr||tr.dataset.company){if(e.preventDefault)e.preventDefault();return;}' +
' dragRow=tr;dragWas=tr.nextElementSibling;' +
' tr.classList.add("dragging");' +
' if(e.dataTransfer){e.dataTransfer.effectAllowed="move";' +
'  try{e.dataTransfer.setData("text/plain","");}catch(err){}}' +
'});' +
'document.addEventListener("dragover",function(e){' +
' if(!dragRow)return;' +
' var tr=e.target.closest("tr.shell");' +
' if(!tr||tr===dragRow||tr.parentNode!==dragRow.parentNode)return;' +
' e.preventDefault();' +
' if(e.dataTransfer)e.dataTransfer.dropEffect="move";' +
' var r=tr.getBoundingClientRect();' +
' var before=(e.clientY-r.top)<r.height/2;' +
' tr.parentNode.insertBefore(dragRow,before?tr:tr.nextSibling);' +
'});' +
'document.addEventListener("drop",function(e){if(dragRow)e.preventDefault();});' +
'document.addEventListener("dragend",function(){' +
' if(!dragRow)return;' +
' var tr=dragRow;dragRow=null;tr.classList.remove("dragging");' +
' if(tr.nextElementSibling===dragWas)return;' +
' var sib=tr.nextElementSibling;' +
' while(sib&&!sib.classList.contains("shell"))sib=sib.nextElementSibling;' +
' var prev=tr.previousElementSibling;' +
' while(prev&&!prev.classList.contains("shell"))prev=prev.previousElementSibling;' +
' if(!sib&&!prev)return;' +
' google.script.run.withSuccessHandler(function(res){' +
'  if(res&&res.ok){tr.dataset.row=String(res.row);say("Order saved");}' +
'  else say((res&&res.error)||"Could not save the order — it will reset on refresh");' +
' }).withFailureHandler(function(){' +
'  say("Could not save the order — it will reset on refresh");' +
' }).moveJobRow(Number(tr.dataset.row),tr.dataset.link||"",' +
'  sib?Number(sib.dataset.row):0,sib?(sib.dataset.link||""):"",' +
'  prev?Number(prev.dataset.row):0,prev?(prev.dataset.link||""):"");' +
'});' +

// The heart writes to the sheet AND reorders it, which is a couple of seconds of
// Google round trips. The card moves NOW and the sheet catches up; if the write
// fails the heart and the card go back exactly where they were, with the reason.
'function frontOf(grid,el){if(!grid)return;' +
' var h=grid.querySelector("tr.bempty")||grid.querySelector("tr.grp");' +
' grid.insertBefore(el,h?h.nextSibling:grid.firstChild);}' +
'function paintHeart(b,shell,on){b.classList.toggle("on",on);' +
' shell.classList.toggle("is-fav",on);b.setAttribute("aria-pressed",on?"true":"false");' +
' b.title=on?"Unpin from the top of the bank":"Pin to the top of the bank";}' +
'document.addEventListener("click",function(e){' +
' var b=e.target.closest(".heart");if(!b)return;' +
' var shell=b.closest(".shell");if(!shell)return;' +
' var on=!b.classList.contains("on");' +
// where it was, so a refused write can put it back rather than leaving it
// somewhere the sheet never agreed to
' var par=shell.parentNode,prev=shell.previousElementSibling;' +
' function undo(msg){b.classList.remove("busy");paintHeart(b,shell,!on);' +
'  if(par)par.insertBefore(shell,prev?prev.nextSibling:par.firstChild);say(msg);}' +
' b.classList.add("busy");paintHeart(b,shell,on);' +
// unhearting hands the card back to the score order the rest of the list is in
// (placeByScore already keeps it below anything still hearted)
' if(on)frontOf(par,shell);else placeByScore(par,shell);' +
' google.script.run.withSuccessHandler(function(res){' +
'   if(res&&res.ok){b.classList.remove("busy");shell.dataset.row=String(res.row);' +
'    say(on?"Hearted — moved to the top of the bank":"Unhearted");}' +
'   else undo((res&&res.error)||"Could not save that");' +
' }).withFailureHandler(function(){' +
'   undo("Could not save — refresh and try again");' +
' }).toggleFavourite(Number(b.dataset.row),linkOf(b));' +
'});' +

// The prompt is deliberately skippable: a forced text box on every removal is
// how you end up not removing things, which is worse than having no feedback.
'var whyModal=document.getElementById("whyModal"),whyText=document.getElementById("whyText"),' +
'whyBtn=null,whyShell=null,whyPicked=[];' +
'function openWhy(btn,shell){' +
' whyBtn=btn;whyShell=shell;whyPicked=[];whyText.value="";' +
' document.getElementById("whyRepWrap").style.display=' +
'  (btn.dataset.sheet==="Archive")?"none":"";' +
// unticked every time: a sticky tick would silently refill later removals,
// which is the surprise this option exists to remove
' document.getElementById("whyRep").checked=false;' +
' document.querySelectorAll(".whyopt").forEach(function(o){o.classList.remove("on")});' +
' whyModal.classList.add("show");' +
'}' +
'function closeWhy(){whyModal.classList.remove("show");}' +
'document.getElementById("whyCancel").addEventListener("click",closeWhy);' +
'whyModal.addEventListener("click",function(e){if(e.target===whyModal)closeWhy();});' +
'document.addEventListener("click",function(e){' +
' var o=e.target.closest(".whyopt");if(!o)return;' +
' o.classList.toggle("on");' +
' var r=o.dataset.r,i=whyPicked.indexOf(r);' +
' if(i<0)whyPicked.push(r);else whyPicked.splice(i,1);' +
'});' +
'document.getElementById("whySkip").addEventListener("click",function(){doRemove("");});' +
'document.getElementById("whyGo").addEventListener("click",function(){' +
' var extra=whyText.value.trim();' +
' doRemove(whyPicked.concat(extra?[extra]:[]).join("; "));' +
'});' +

// Removing writes to three tabs and re-reads the bank, which is a few seconds of
// Google API round trips. Waiting for all of that before the card moves is what
// made this feel broken, so the card goes NOW and the sheet catches up. If the
// write fails the card comes back exactly where it was, with the reason.
'function doRemove(reason){' +
' closeWhy();' +
' var btn=whyBtn,shell=whyShell;' +
' if(!btn||!shell)return;' +
' var fromArchive=btn.dataset.sheet==="Archive";' +
' var wantRep=document.getElementById("whyRep").checked;' +
' var parent=shell.parentNode,next=shell.nextSibling;' +
' var listId=fromArchive?"moreList":null;' +
' shell.classList.add("leaving");' +
' setTimeout(function(){' +
'  shell.remove();' +
'  if(listId){var ml=document.getElementById(listId);' +
'   if(ml&&!ml.querySelectorAll(".shell").length){' +
'    ml.className="empty";ml.textContent="Nothing left here — all caught up.";}' +
'  } else {recountAll();}' +
'  eqhAll();' +
' },200);' +
' say(wantRep?"Removed — finding a replacement…":"Removed");' +
' var undo=function(msg){' +
'  if(parent)parent.insertBefore(shell,next);' +
'  shell.classList.remove("leaving");' +
'  if(!listId)recountAll();' +
'  eqhAll();say(msg);' +
' };' +
' var done=function(res){' +
'  if(!res||!res.ok){undo((res&&res.error)||"Could not remove — put it back");return;}' +
'  if(!fromArchive){' +
'   if(res.row)shiftRows(res.row);' +
'   if(res.replacementCard){' +
'    insertBankCard(res.replacementCard,res.replacementScore||0);' +
'    say("Removed. "+res.replacement.Company+" added in its place.");}' +
'   else if(wantRep){say("Removed — nothing in More roles was good enough to add.");}' +
'  }' +
' };' +
' var fail=function(){undo("Could not remove — put it back, try again");};' +
' if(fromArchive){' +
'  google.script.run.withSuccessHandler(done).withFailureHandler(fail)' +
'   .dismissArchived(Number(btn.dataset.row),reason,linkOf(btn));' +
' } else {' +
'  google.script.run.withSuccessHandler(done).withFailureHandler(fail)' +
'   .removeJob(Number(btn.dataset.row),wantRep,reason,linkOf(btn));' +
' }' +
'}' +

'document.addEventListener("click",function(e){' +
' if(e.target.closest("a,button,select"))return;' +
' var card=e.target.closest(".card");if(!card)return;' +
' var shell=card.closest(".shell");if(!shell)return;' +
' shell.classList.toggle("expanded");' +
' if(!shell.classList.contains("expanded")){eqhAll();return;}' +
' shell.style.minHeight="";' +
' fetchDesc(shell);' +
'});' +

// A card with no description fetches it the first time it is opened, and the
// server writes it back to the sheet so it is only ever fetched once.
// Only a card that ASKS to be filled in, via data-fetch, is ever fetched for.
// "nodesc" is styling and nothing more: Opening Soon reuses it for "No notes",
// and when it doubled as the trigger those cards fetched too — sending an
// Opening Soon row number against the Job Bank and rendering a completely
// different company's description onto the programme.
'function fetchDesc(shell){' +
' var el=shell.querySelector(".why[data-fetch]");' +
' if(!el||el.dataset.tried)return;' +
// No link means resolveRow_ has nothing to verify the row against, and a row
// number alone is exactly what reads back the wrong role. Say so instead.
' if(!(shell.dataset.link||"").trim()){' +
'  el.dataset.tried="1";el.textContent="No link on this role — add one on the sheet.";return;}' +
' el.dataset.tried="1";el.textContent="Reading the posting…";' +
' google.script.run.withSuccessHandler(function(res){' +
'  if(res&&res.ok){el.textContent=res.text;el.classList.remove("nodesc");}' +
'  else{el.textContent=(res&&res.error)||"Could not read that posting.";}' +
'  eqhAll();' +
' }).withFailureHandler(function(){' +
'  el.textContent="Could not read that posting.";' +
' }).describeRole(Number(shell.dataset.row),' +
'   shell.closest("#moreList")?"Archive":"Job Bank",shell.dataset.link||"");' +
'}' +

'var modal=document.getElementById("modal"),mtext=document.getElementById("mtext"),' +
'mtitle=document.getElementById("mtitle"),msub=document.getElementById("msub");' +
'document.getElementById("mclose").addEventListener("click",function(){' +
' modal.classList.remove("show");});' +
'modal.addEventListener("click",function(e){if(e.target===modal)modal.classList.remove("show");});' +
'document.getElementById("mcopy").addEventListener("click",function(){' +
' mtext.removeAttribute("readonly");mtext.focus();mtext.select();' +
' try{document.execCommand("copy");say("Prompt copied - paste into Claude with your CV");}' +
' catch(err){say("Select the text and copy manually");}' +
' mtext.setAttribute("readonly","readonly");});' +

'document.addEventListener("click",function(e){' +
' var b=e.target.closest(".prompt");if(!b)return;' +
' b.disabled=true;say("Reading the posting...");' +
' google.script.run.withSuccessHandler(function(res){' +
'   b.disabled=false;' +
'   if(!res||!res.ok){say((res&&res.error)||"Could not build the prompt");return;}' +
'   mtitle.textContent="Tailoring prompt - "+res.company;' +
'   msub.textContent=res.fetched?"Full job description included. Paste into Claude with your CV attached."' +
'     :"The site blocked an automatic read - paste the posting text where indicated.";' +
'   mtext.value=res.prompt;modal.classList.add("show");' +
'   toast.classList.remove("show");' +
' }).withFailureHandler(function(){b.disabled=false;say("Could not build the prompt");' +
' }).tailoringPrompt(Number(b.dataset.row),b.dataset.sheet||"Job Bank",linkOf(b));' +
'});' +

'var oModal=document.getElementById("outreachModal"),' +
'oTitle=document.getElementById("otitle"),oBig=document.getElementById("obigscheme"),' +
'oLinks=document.getElementById("olinks"),oDomain=document.getElementById("odomain"),' +
'oEmailBox=document.getElementById("oemailbox"),oNote=document.getElementById("onote"),' +
'oNoteCount=document.getElementById("onoteCount"),oDm=document.getElementById("odm"),' +
'oDmCount=document.getElementById("odmCount"),oSubject=document.getElementById("osubject"),' +
'oBody=document.getElementById("obody"),oBodyCount=document.getElementById("obodyCount"),' +
'oMark=document.getElementById("omark"),oLast=document.getElementById("olast"),' +
'oCurrentRow=null,oCurrentSheet="Job Bank",oCurrentLink="";' +

'document.getElementById("ocloseBtn").addEventListener("click",function(){' +
' oModal.classList.remove("show");});' +
'oModal.addEventListener("click",function(e){if(e.target===oModal)oModal.classList.remove("show");});' +

'function wordCount(s){return (String(s||"").trim().match(/\\S+/g)||[]).length;}' +

'document.addEventListener("click",function(e){' +
' var b=e.target.closest(".reachout");if(!b)return;' +
' b.disabled=true;say("Building outreach...");oCurrentRow=Number(b.dataset.row);' +
' oCurrentSheet=b.dataset.sheet||"Job Bank";oCurrentLink=linkOf(b);' +
' google.script.run.withSuccessHandler(function(res){' +
'   b.disabled=false;' +
'   if(!res||!res.ok){say((res&&res.error)||"Could not build outreach");return;}' +
'   oTitle.textContent="Reach out - "+res.company;' +
'   oBig.style.display=res.bigScheme?"":"none";' +
'   oLinks.innerHTML=' +
'     "<a href=\\""+res.linkedinPeople+"\\" target=\\"_blank\\" rel=\\"noopener\\">Recruiters at "+res.company+" &#8599;</a>"+' +
'     "<a href=\\""+res.linkedinEngineers+"\\" target=\\"_blank\\" rel=\\"noopener\\">Engineers at "+res.company+" &#8599;</a>"+' +
'     "<a href=\\""+res.linkedinAlumni+"\\" target=\\"_blank\\" rel=\\"noopener\\">Imperial alumni at "+res.company+" &#8599;</a>";' +
'   if(!res.hunterConfigured){' +
'     oDomain.textContent="";' +
'     oEmailBox.innerHTML="<div class=\\"warn\\">Add HUNTER_API_KEY in Script Properties to resolve email addresses.</div>";' +
'   } else if(res.candidates&&res.candidates.length){' +
'     oDomain.textContent=res.domain+(res.domainGuessed?" (guessed)":"")+(res.pattern?" \\u00b7 pattern "+res.pattern:"");' +
'     oEmailBox.innerHTML="<div class=\\"candlist\\">"+res.candidates.map(function(c){' +
'       return "<div><span>"+(c.name||c.position||"?")+"</span><b>"+c.email+"</b></div>";' +
'     }).join("")+"</div>";' +
'   } else {' +
'     oDomain.textContent=res.domain?res.domain+(res.domainGuessed?" (guessed)":""):"";' +
'     oEmailBox.innerHTML="<div class=\\"warn\\">No confident match - unknown is a legitimate outcome here (catch-all domains, Microsoft 365 corporates).</div>";' +
'   }' +
'   oNote.value=res.note;oNoteCount.textContent=res.note.length+"/200 chars";' +
'   oDm.value=res.dm;oDmCount.textContent=res.dm.length+"/400 chars";' +
'   oSubject.textContent=res.email.subject;' +
'   oBody.value=res.email.body;oBodyCount.textContent=wordCount(res.email.body)+" words (aim 120-180)";' +
'   oLast.textContent=res.lastContacted?("Last contacted "+res.lastContacted):"";' +
'   oModal.classList.add("show");toast.classList.remove("show");' +
' }).withFailureHandler(function(){b.disabled=false;say("Could not build outreach");' +
' }).outreachFor(oCurrentRow,oCurrentSheet,oCurrentLink);' +
'});' +

'document.addEventListener("click",function(e){' +
' var b=e.target.closest(".copybtn");if(!b)return;' +
' var id=b.dataset.copy,el=document.getElementById(id);' +
' var text=(el.tagName==="TEXTAREA")?el.value:el.textContent;' +
' var tmp=document.createElement("textarea");tmp.value=text;tmp.style.position="fixed";' +
' tmp.style.opacity="0";document.body.appendChild(tmp);tmp.focus();tmp.select();' +
' try{document.execCommand("copy");say("Copied to clipboard");}' +
' catch(err){say("Select the text and copy manually");}' +
' document.body.removeChild(tmp);' +
'});' +

'document.getElementById("omark").addEventListener("click",function(){' +
' if(!oCurrentRow)return;oMark.disabled=true;' +
' google.script.run.withSuccessHandler(function(res){' +
'   oMark.disabled=false;' +
'   if(res&&res.ok){oLast.textContent="Last contacted "+res.date;say("Marked as contacted today");}' +
'   else{say((res&&res.error)||"Could not save");}' +
' }).withFailureHandler(function(){oMark.disabled=false;say("Could not save");' +
' }).markContacted(oCurrentRow,oCurrentSheet,oCurrentLink);' +
'});' +

/* ---- filters: role type, and location by country then city ---- */
// Selections live here rather than in the DOM, because the panels are rebuilt
// whenever the open tab changes and anything held in the markup would be lost
// with them. An empty set means "no filter", not "nothing matches".
'var FSTATE={};' +
'function fstate(t){if(!FSTATE[t])FSTATE[t]={cat:{},loc:{},sub:null};return FSTATE[t];}' +
'function nkeys(o){return Object.keys(o).length;}' +
'function cityOf(c){return (c.dataset.city||"").trim()||(c.dataset.country||"Other");}' +
'function ckey(country,city){return country+"||"+city;}' +

// Counts describe the tab you are looking at. Counting the whole bank while one
// group is on screen is how "AI/ML 7" ended up sitting above five cards.
'function fscope(target){' +
' var grid=document.getElementById(target);if(!grid)return null;' +
' if(target!=="list")return grid;' +
// counts describe what the slice tabs are showing: one band narrowed-to, or
// the whole table under All. (The old sec-* sections no longer exist.)
' var vis=[],n=0;' +
' BKEYS.forEach(function(k){var g=gridOf(k);' +
'  if(g&&g.style.display!=="none"){vis.push(g);n++;}});' +
' return n===1?vis[0]:grid;' +
'}' +

'function fscan(target){' +
' var scope=fscope(target);if(!scope)return null;' +
' var st=fstate(target),anyCat=nkeys(st.cat),anyLoc=nkeys(st.loc);' +
' var cats={},countries={},total=0;' +
' scope.querySelectorAll(".shell").forEach(function(c){' +
'  total++;' +
'  var cat=c.dataset.cat||"Other";' +
'  var co=(c.dataset.country||"").trim()||"Other",ci=cityOf(c);' +
// Each list counts only what the OTHER filter still allows, so no row is ever a
// dead end: "Space 1" next to a UK-only location filter promised a role it would
// then hide. Picking anything on offer always leaves something on screen.
'  if(!anyLoc||st.loc[ckey(co,ci)])cats[cat]=(cats[cat]||0)+1;' +
'  if(!anyCat||st.cat[cat]){' +
'   if(!countries[co])countries[co]={n:0,cities:{}};' +
'   countries[co].n++;' +
'   countries[co].cities[ci]=(countries[co].cities[ci]||0)+1;' +
'  }' +
' });' +
// Something already ticked has to stay on the list even once its count reaches
// zero, or there would be no way left to untick it.
' Object.keys(st.cat).forEach(function(k){if(!(k in cats))cats[k]=0;});' +
' Object.keys(st.loc).forEach(function(k){' +
'  var p=k.split("||"),co=p[0],ci=p[1];' +
'  if(!countries[co])countries[co]={n:0,cities:{}};' +
'  if(!(ci in countries[co].cities))countries[co].cities[ci]=0;' +
' });' +
' return {cats:cats,countries:countries,total:total};' +
'}' +

'function applyView(target){' +
' var grid=document.getElementById(target);if(!grid)return;' +
' var st=fstate(target),anyCat=nkeys(st.cat),anyLoc=nkeys(st.loc);' +
' var sb=document.querySelector(".searchbox[data-target=\'"+target+"\']");' +
' var q=sb?sb.value.trim().toLowerCase():"";' +
' grid.querySelectorAll(".shell").forEach(function(c){' +
'  var okCat=!anyCat||!!st.cat[c.dataset.cat||"Other"];' +
'  var co=(c.dataset.country||"").trim()||"Other";' +
'  var okLoc=!anyLoc||!!st.loc[ckey(co,cityOf(c))];' +
'  var okText=!q||c.textContent.toLowerCase().indexOf(q)>=0;' +
'  c.style.display=(okCat&&okLoc&&okText)?"":"none";});' +
// which tab is open is decided by showBucket, never by the filter — a search
// that silently swapped tabs under you would be worse than one that finds nothing
' eqhAll();' +
'}' +

'document.addEventListener("input",function(e){' +
' var sb=e.target.closest(".searchbox");if(!sb)return;' +
' applyView(sb.dataset.target);' +
'});' +

// A country fully ticked reads as the country, not as a list of its cities —
// "United Kingdom" beats "London, Bristol, Derby +4" for the same meaning.
'function fsummary(target,scan){' +
' var st=fstate(target),out=[];' +
' Object.keys(scan.countries).forEach(function(co){' +
'  var cities=Object.keys(scan.countries[co].cities);' +
'  var picked=cities.filter(function(ci){return st.loc[ckey(co,ci)];});' +
'  if(!picked.length)return;' +
'  if(picked.length===cities.length)out.push(co);' +
'  else picked.forEach(function(ci){out.push(ci);});' +
' });' +
' return out;' +
'}' +
'function ftext(names){' +
' if(!names.length)return "All";' +
' if(names.length<=2)return names.join(", ");' +
' return names.slice(0,2).join(", ")+" +"+(names.length-2);' +
'}' +

'function row(opts){' +
' var el=document.createElement(opts.drill?"div":"label");' +
' el.className="frow";' +
' var cb=document.createElement("input");cb.type="checkbox";cb.checked=!!opts.on;' +
' if(opts.mixed)cb.indeterminate=true;' +
' cb.addEventListener("change",function(){opts.toggle(cb.checked);});' +
' var nm=document.createElement("span");nm.className="fname";nm.textContent=opts.name;' +
' var ct=document.createElement("b");ct.className="fcount";ct.textContent=opts.count;' +
' el.appendChild(cb);el.appendChild(nm);el.appendChild(ct);' +
' if(opts.drill){' +
'  var d=document.createElement("button");d.type="button";d.className="fdrill";' +
'  d.innerHTML="&#8250;";d.title="Choose cities in "+opts.name;' +
'  d.addEventListener("click",function(ev){ev.stopPropagation();opts.drill();});' +
'  el.appendChild(d);' +
'  nm.style.cursor="pointer";' +
'  nm.addEventListener("click",function(){opts.drill();});' +
' }' +
' return el;' +
'}' +

'function buildFilters(target){' +
' var scan=fscan(target);if(!scan)return;' +
' var st=fstate(target);' +
' document.querySelectorAll(".fdrop[data-target=\'"+target+"\']").forEach(function(dd){' +
'  var kind=dd.dataset.kind;' +
'  var root=dd.querySelector(".froot"),sub=dd.querySelector(".fsub");' +
'  root.innerHTML="";sub.innerHTML="";' +
'  if(kind==="cat"){' +
'   var cats=Object.keys(scan.cats).sort(function(a,b){' +
'    return scan.cats[b]-scan.cats[a]||a.localeCompare(b);});' +
'   if(!cats.length)root.innerHTML="<p class=\\"fempty\\">Nothing to filter here.</p>";' +
'   cats.forEach(function(k){' +
'    root.appendChild(row({name:k,count:scan.cats[k],on:!!st.cat[k],' +
'     toggle:function(v){if(v)st.cat[k]=1;else delete st.cat[k];' +
'      applyView(target);buildFilters(target);}}));' +
'   });' +
'   var names=Object.keys(st.cat);' +
'   dd.querySelector(".fval").textContent=ftext(names);' +
'   dd.classList.toggle("set",!!names.length);' +
'   dd.querySelector(".fnum").textContent=names.length?names.length+" selected":"";' +
'   return;' +
'  }' +
'  var cos=Object.keys(scan.countries).sort(function(a,b){' +
'   return scan.countries[b].n-scan.countries[a].n||a.localeCompare(b);});' +
'  if(!cos.length)root.innerHTML="<p class=\\"fempty\\">Nothing to filter here.</p>";' +
'  cos.forEach(function(co){' +
'   var cities=Object.keys(scan.countries[co].cities);' +
'   var on=cities.filter(function(ci){return st.loc[ckey(co,ci)];}).length;' +
'   root.appendChild(row({name:co,count:scan.countries[co].n,' +
'    on:on===cities.length&&on>0,mixed:on>0&&on<cities.length,' +
'    toggle:function(v){' +
'     cities.forEach(function(ci){' +
'      if(v)st.loc[ckey(co,ci)]=1;else delete st.loc[ckey(co,ci)];});' +
'     applyView(target);buildFilters(target);},' +
'    drill:function(){st.sub=co;dd.classList.add("sub");buildFilters(target);}}));' +
'  });' +
'  if(st.sub&&scan.countries[st.sub]){' +
'   var co2=st.sub,cs=scan.countries[co2].cities;' +
'   var back=document.createElement("button");back.type="button";back.className="fback";' +
'   back.innerHTML="&#8249; <span>"+co2+"</span>";' +
'   back.addEventListener("click",function(){st.sub=null;dd.classList.remove("sub");' +
'    buildFilters(target);});' +
'   sub.appendChild(back);' +
'   Object.keys(cs).sort(function(a,b){return cs[b]-cs[a]||a.localeCompare(b);})' +
'    .forEach(function(ci){' +
'     sub.appendChild(row({name:ci,count:cs[ci],on:!!st.loc[ckey(co2,ci)],' +
'      toggle:function(v){if(v)st.loc[ckey(co2,ci)]=1;else delete st.loc[ckey(co2,ci)];' +
'       applyView(target);buildFilters(target);}}));' +
'    });' +
'  } else {dd.classList.remove("sub");}' +
'  var picked=fsummary(target,scan);' +
'  dd.querySelector(".fval").textContent=ftext(picked);' +
'  dd.classList.toggle("set",!!picked.length);' +
'  var n=nkeys(st.loc);' +
'  dd.querySelector(".fnum").textContent=n?n+" selected":"";' +
' });' +
'}' +

// Capture, not bubble. Every control inside a panel re-renders the panel, which
// detaches the node that was clicked — and a detached node's closest(".fpanel")
// is null, so by the bubble phase a click on Back or a checkbox looks exactly
// like a click on the page behind it, and the panel shut itself. In the capture
// phase the tree is still intact and the question has a truthful answer.
'document.addEventListener("click",function(e){' +
' var btn=e.target.closest(".fbtn");' +
' var inPanel=e.target.closest(".fpanel");' +
' document.querySelectorAll(".fdrop.open").forEach(function(d){' +
'  if(btn&&d.contains(btn))return;' +
'  if(inPanel&&d.contains(inPanel))return;' +
'  d.classList.remove("open");d.querySelector(".fbtn").setAttribute("aria-expanded","false");' +
' });' +
' if(!btn)return;' +
' var dd=btn.closest(".fdrop");' +
' var open=!dd.classList.contains("open");' +
' dd.classList.toggle("open",open);' +
' btn.setAttribute("aria-expanded",open?"true":"false");' +
// the panel is EMPTY until something builds it. Nothing does that on page load
// any more (the old first-tab boot went with the tab sections), so build it at
// the moment of opening — which is also what keeps its counts honest.
' if(open)buildFilters(dd.dataset.target);' +
'},true);' +
'document.addEventListener("keydown",function(e){' +
' if(e.key!=="Escape")return;' +
' document.querySelectorAll(".fdrop.open").forEach(function(d){d.classList.remove("open");});' +
'});' +
'document.addEventListener("click",function(e){' +
' var b=e.target.closest(".fclear");if(!b)return;' +
' var dd=b.closest(".fdrop"),t=dd.dataset.target,st=fstate(t);' +
' if(dd.dataset.kind==="cat")st.cat={};else{st.loc={};st.sub=null;}' +
' applyView(t);buildFilters(t);' +
'});' +

'document.addEventListener("click",function(e){' +
' var b=e.target.closest(".addbank");if(!b)return;' +
' b.disabled=true;say("Adding to the Job Bank...");' +
' google.script.run.withSuccessHandler(function(res){' +
'   if(res&&res.ok){' +
'    say("Added "+res.role+" at "+res.company+" to the Job Bank");' +
'    bumpOpen(1);' +
'    insertBankCard(res.card,res.score||0);' +
'    var sh=b.closest(".shell");' +
'    if(sh){sh.classList.add("leaving");' +
'      setTimeout(function(){' +
'        sh.remove();' +
'        var ml=document.getElementById("moreList");' +
'        if(ml&&!ml.querySelectorAll(".shell").length){' +
'          ml.className="empty";' +
'          ml.textContent="Nothing left here — all caught up.";}' +
'      },220);}' +
'   } else {b.disabled=false;say((res&&res.error)||"Could not add");}' +
' }).withFailureHandler(function(){b.disabled=false;say("Could not add");' +
' }).promoteToBank(Number(b.dataset.row),linkOf(b));' +
'});' +

'var pageBank=document.getElementById("pageBank"),pageMore=document.getElementById("pageMore"),' +
'moreList=document.getElementById("moreList"),moreLoaded=false;' +
'var sallyLoaded=false;' +
'document.querySelectorAll(".pagetab").forEach(function(b){' +
' b.addEventListener("click",function(){' +
'  document.querySelectorAll(".pagetab").forEach(function(x){x.classList.remove("on")});' +
'  b.classList.add("on");var p=b.dataset.page;' +
'  pageBank.style.display=(p==="bank")?"":"none";' +
'  pageMore.style.display=(p==="more")?"":"none";' +
'  document.getElementById("pageFind").style.display=(p==="find")?"":"none";' +
'  document.getElementById("pageStats").style.display=(p==="stats")?"":"none";' +
'  document.getElementById("pageSally").style.display=(p==="sally")?"":"none";' +
'  if(p==="more"&&!moreLoaded){moreLoaded=true;loadMore();}' +
'  if(p==="stats"&&!statsLoaded){statsLoaded=true;loadStats();}' +
'  if(p==="sally"&&!sallyLoaded){sallyLoaded=true;loadSally();}' +
'  if(p==="find")document.getElementById("findco").focus();' +
' });});' +

// Collapse the sidebar to icons. Remembered across visits where the Apps Script
// sandbox allows storage; where it does not, the toggle still works per page.
'var wrapEl=document.querySelector(".wrap"),railT=document.getElementById("railToggle");' +
'function setRail(min){' +
' wrapEl.classList.toggle("railmin",min);' +
' railT.innerHTML=min?"&#187;":"&#171;";' +
' var t=min?"Expand the sidebar":"Collapse the sidebar";' +
' railT.title=t;railT.setAttribute("aria-label",t);' +
' clearTimeout(window.__eqhT);window.__eqhT=setTimeout(eqhAll,180);' +
'}' +
'railT.addEventListener("click",function(){' +
' var min=!wrapEl.classList.contains("railmin");setRail(min);' +
' try{localStorage.setItem("railmin",min?"1":"0");}catch(err){}' +
'});' +
'try{if(localStorage.getItem("railmin")==="1")setRail(true);}catch(err){}' +

/* ---- find roles: search one company on demand ---- */
// The hits are kept client-side so the + can hand the whole record back; a
// second server round-trip to re-find the role would just be a chance to get a
// different answer.
'var lastHits=[],findhits=document.getElementById("findhits"),' +
'findnote=document.getElementById("findnote"),findgo=document.getElementById("findgo");' +

'function runFind(){' +
' var co=document.getElementById("findco").value.trim();' +
' if(co.length<2){say("Type a company name first");return;}' +
' var terms=document.getElementById("findterms").value.trim();' +
' var all=document.getElementById("findall").checked;' +
' findgo.disabled=true;findnote.textContent="Searching "+co+" — archive, their job board, then Adzuna…";' +
' findhits.innerHTML="";' +
' google.script.run.withSuccessHandler(function(res){' +
'  findgo.disabled=false;' +
'  if(!res||!res.ok){findnote.textContent=(res&&res.error)||"Search failed.";return;}' +
'  lastHits=res.results||[];' +
'  var bits=[];' +
'  bits.push(lastHits.length+" role"+(lastHits.length===1?"":"s")+" worth your time");' +
'  bits.push(res.counts.local+" from your sheet, "+res.counts.ats+" from their job board, "' +
'   +res.counts.adzuna+" from Adzuna");' +
'  if(!res.adzunaConfigured)bits.push("Adzuna is off — add ADZUNA_APP_ID and " +' +
'   "ADZUNA_APP_KEY in Script Properties to reach employers with no public " +' +
'   "job board (Apple, ESA)");' +
'  if(!res.aiUsed&&lastHits.length)bits.push("ranked by keyword, not the model " +' +
'   "— add ANTHROPIC_API_KEY in Script Properties for real scores");' +
'  findnote.innerHTML=bits.join(" · ");' +
'  if(!lastHits.length){' +
'   findhits.innerHTML="<p class=\\"empty\\">Nothing found for "+co+". If you know " +' +
'    "of one, add it yourself below — that is what the form is for.</p>";' +
'   document.getElementById("orCompany").value=co;return;}' +
'  findhits.innerHTML=res.html||"";' +
' }).withFailureHandler(function(){' +
'  findgo.disabled=false;findnote.textContent="Search failed — try again.";' +
' }).searchCompany(co,all,terms);' +
'}' +
'findgo.addEventListener("click",runFind);' +
'document.getElementById("findco").addEventListener("keydown",function(e){' +
' if(e.key==="Enter")runFind();});' +
'document.getElementById("findterms").addEventListener("keydown",function(e){' +
' if(e.key==="Enter")runFind();});' +

'document.addEventListener("click",function(e){' +
' var b=e.target.closest(".hitadd");if(!b)return;' +
' var hit=lastHits[Number(b.dataset.i)];if(!hit)return;' +
' b.disabled=true;say("Adding "+hit.company+"…");' +
' google.script.run.withSuccessHandler(function(res){' +
'  if(res&&res.ok){' +
'   b.classList.add("done");b.textContent="\\u2713";' +
'   insertBankCard(res.card,res.score||0);bumpOpen(1);' +
'   say("Added "+res.role+" at "+res.company+" to the Job Bank");' +
'  } else {b.disabled=false;say((res&&res.error)||"Could not add");}' +
' }).withFailureHandler(function(){b.disabled=false;say("Could not add");' +
' }).addFoundRole(hit);' +
'});' +

'document.getElementById("orGo").addEventListener("click",function(){' +
' var btn=this,msg=document.getElementById("orMsg");' +
' var p={company:document.getElementById("orCompany").value.trim(),' +
'        role:document.getElementById("orRole").value.trim(),' +
'        link:document.getElementById("orLink").value.trim(),' +
'        location:document.getElementById("orLoc").value.trim(),' +
'        deadline:document.getElementById("orDeadline").value.trim(),' +
'        duration:document.getElementById("orDuration").value.trim(),' +
'        description:document.getElementById("orDesc").value.trim(),' +
'        source:"added by hand"};' +
' if(!p.company||!p.role){msg.textContent="Company and role are both needed";return;}' +
' function clearForm(){' +
'  ["orCompany","orRole","orLink","orLoc","orDeadline","orDuration","orDesc","orOpen"]' +
'   .forEach(function(id){var el=document.getElementById(id);if(el)el.value="";});' +
' }' +
// the checkbox turns "add this role" into "watch for this role": same form,
// different sheet, and the new row lands in the bank page's Watchlist band
' if(document.getElementById("orSoon").checked){' +
'  btn.disabled=true;msg.textContent="Saving…";' +
'  google.script.run.withSuccessHandler(function(res){' +
'   btn.disabled=false;' +
'   if(!res||!res.ok){msg.textContent=(res&&res.error)||"Could not save";return;}' +
'   msg.textContent=res.emailed?"Tracked — you will be emailed when it opens"' +
'    :"Tracked. Add an expected date if you want the email too.";' +
'   var g=gridOf("watch");' +
'   if(g&&res.card){var tmp=document.createElement("tbody");tmp.innerHTML=res.card;' +
'    if(tmp.firstElementChild)g.appendChild(tmp.firstElementChild);}' +
'   recountAll();clearForm();' +
'  }).withFailureHandler(function(){btn.disabled=false;msg.textContent="Could not save";' +
'  }).addOpeningSoon({company:p.company,programme:p.role,link:p.link,' +
'   openDate:document.getElementById("orOpen").value,closeDate:p.deadline,' +
'   notes:p.description,status:"Added by hand"});' +
'  return;' +
' }' +
' btn.disabled=true;msg.textContent="Adding…";' +
' google.script.run.withSuccessHandler(function(res){' +
'  btn.disabled=false;' +
'  if(!res||!res.ok){msg.textContent=(res&&res.error)||"Could not add";return;}' +
'  msg.textContent="Added — it is in your Job Bank";' +
'  insertBankCard(res.card,res.score||0);bumpOpen(1);clearForm();' +
' }).withFailureHandler(function(){btn.disabled=false;msg.textContent="Could not add";' +
' }).addFoundRole(p);' +
'});' +
'document.getElementById("orSoon").addEventListener("change",function(){' +
' document.getElementById("orOpenWrap").style.display=this.checked?"":"none";' +
' document.getElementById("orGo").textContent=' +
'  this.checked?"Track it":"Add to Job Bank";' +
'});' +

/* ---- opening soon ---- */
// Both of these used to be able to sit on "Loading…" forever: if the call never
// came back, neither handler ever ran and nothing said so. Now a slow call gives
// up visibly and clicking the tab again retries.
'function lazyLoad(o){' +
' var box=document.getElementById(o.into);if(!box)return;' +
' var done=false;' +
' var giveUp=setTimeout(function(){' +
'  if(done)return;done=true;o.reset();' +
'  box.innerHTML="<p class=\\"empty\\">That took too long. Tap the tab again to retry.</p>";' +
' },25000);' +
' google.script.run.withSuccessHandler(function(res){' +
'  if(done)return;done=true;clearTimeout(giveUp);' +
'  if(res&&res.ok){box.innerHTML=res.html;o.after&&o.after(res);}' +
'  else{o.reset();' +
'   box.innerHTML="<p class=\\"empty\\">"+((res&&res.error)||"Could not load")+' +
'    " — tap the tab again to retry.</p>";}' +
'  eqh("#"+o.into);' +
' }).withFailureHandler(function(err){' +
'  if(done)return;done=true;clearTimeout(giveUp);o.reset();' +
'  box.innerHTML="<p class=\\"empty\\">Could not load ("+' +
'   ((err&&err.message)||"no reason given")+") — tap the tab again to retry.</p>";' +
' })[o.call]();' +
'}' +
'function loadMore(){' +
' lazyLoad({into:"moreList",call:"moreRoles",' +
'  reset:function(){moreLoaded=false;},' +
'  after:function(res){var mf=document.getElementById("moreFilters");' +
'   if(mf){mf.innerHTML=(res.filters||"")+viewTogHtml();}' +
'   buildFilters("moreList");setListView(curView);}});' +
'}' +

/* ---- closed listings: check the tab you are on ---- */
// Same shape as the hard refresh: one posting per server call, so the bar
// measures real progress and a slow site cannot take the whole sweep down.
'var ckQ=[],ckI=0,ckDead=0,ckUnknown=0,ckOn=false;' +
'var ckIdle=document.getElementById("ckIdle"),ckRun=document.getElementById("ckRun"),' +
'ckFill=document.getElementById("ckFill"),ckMsg=document.getElementById("ckMsg");' +

'function ckMark(shell,reason){' +
' shell.classList.add("is-closed");' +
' var pills=shell.querySelector(".pills");' +
' if(pills&&!pills.querySelector(".pill-dead")){' +
'  var p=document.createElement("span");p.className="pill pill-dead";' +
'  p.textContent="Closed";p.title=reason||"";pills.insertBefore(p,pills.firstChild);}' +
'}' +
'document.getElementById("ckStop").addEventListener("click",function(){' +
' ckOn=false;ckMsg.textContent="Stopping…";' +
'});' +
'document.getElementById("ckGo").addEventListener("click",function(){' +
// only what is actually on screen: the open tab, minus anything filtered out
' var sec=null;' +
' BKEYS.forEach(function(k){var s=document.getElementById("sec-"+k);' +
'  if(s&&s.style.display!=="none")sec=s;});' +
' if(!sec){say("Nothing to check");return;}' +
' ckQ=[].slice.call(sec.querySelectorAll(".shell")).filter(function(s){' +
'  return s.style.display!=="none"&&(s.dataset.link||"").trim()&&' +
'         !s.classList.contains("is-closed");});' +
' if(!ckQ.length){say("Nothing here left to check");return;}' +
' ckI=0;ckDead=0;ckUnknown=0;ckOn=true;' +
' ckIdle.style.display="none";ckRun.style.display="";' +
' ckStep();' +
'});' +

'function ckStep(){' +
' if(!ckOn||ckI>=ckQ.length){ckFinish();return;}' +
' var shell=ckQ[ckI];' +
' var co=(shell.querySelector(".co")||{}).textContent||"this role";' +
' ckFill.style.width=Math.round((ckI/ckQ.length)*100)+"%";' +
' ckMsg.textContent="Checking "+co+" — "+(ckI+1)+" of "+ckQ.length+' +
'  (ckDead?" · "+ckDead+" closed":"");' +
' google.script.run.withSuccessHandler(function(r){' +
'  if(r&&r.ok&&r.state==="closed"){ckDead++;ckMark(shell,r.reason);}' +
'  else if(!r||!r.ok||r.state==="unknown")ckUnknown++;' +
'  ckI++;ckStep();' +
' }).withFailureHandler(function(){ckUnknown++;ckI++;ckStep();' +
' }).checkClosed(Number(shell.dataset.row),"Job Bank",shell.dataset.link||"");' +
'}' +

'function ckFinish(){' +
' ckOn=false;ckFill.style.width="100%";' +
' ckIdle.style.display="";ckRun.style.display="none";' +
' var bits=["Checked "+ckI+" of "+ckQ.length];' +
' bits.push(ckDead?(ckDead+" closed"):"none closed");' +
' if(ckUnknown)bits.push(ckUnknown+" could not be checked");' +
' say(bits.join(" · "));eqhAll();' +
'}' +

/* ---- more roles: hard refresh ---- */
// One company per server call, driven from here. A company whose board is down
// is counted and stepped over rather than ending the sweep — twenty companies
// should not be abandoned because the third one 500s.
'var rfxQ=[],rfxI=0,rfxAdded=0,rfxFailed=0,rfxOn=false;' +
'var rfxIdle=document.getElementById("rfxIdle"),rfxAsk=document.getElementById("rfxAsk"),' +
'rfxRun=document.getElementById("rfxRun"),rfxFill=document.getElementById("rfxFill"),' +
'rfxMsg=document.getElementById("rfxMsg");' +

'function rfxShow(which){' +
' rfxIdle.style.display=(which==="idle")?"":"none";' +
' rfxAsk.style.display=(which==="ask")?"":"none";' +
' rfxRun.style.display=(which==="run")?"":"none";' +
'}' +
'function rfxProgress(done,total,text){' +
' rfxFill.style.width=Math.round((done/Math.max(total,1))*100)+"%";' +
' rfxMsg.textContent=text;' +
'}' +
'document.getElementById("rfxGo").addEventListener("click",function(){rfxShow("ask");});' +
'document.getElementById("rfxNo").addEventListener("click",function(){rfxShow("idle");});' +
'document.getElementById("rfxStop").addEventListener("click",function(){' +
' rfxOn=false;rfxMsg.textContent="Stopping after this company…";' +
'});' +
'document.getElementById("rfxYes").addEventListener("click",function(){' +
' rfxShow("run");rfxProgress(0,1,"Reading your Watchlist…");' +
' google.script.run.withSuccessHandler(function(res){' +
'  if(!res||!res.ok){rfxShow("idle");say((res&&res.error)||"Could not start");return;}' +
'  rfxQ=res.companies;rfxI=0;rfxAdded=0;rfxFailed=0;rfxOn=true;' +
'  if(!res.adzunaConfigured)say("Adzuna is off — searching company boards only");' +
'  rfxStep();' +
' }).withFailureHandler(function(){rfxShow("idle");say("Could not start the refresh");' +
' }).refreshMorePlan();' +
'});' +

'function rfxStep(){' +
' if(!rfxOn||rfxI>=rfxQ.length){rfxFinish();return;}' +
' var co=rfxQ[rfxI];' +
' rfxProgress(rfxI,rfxQ.length,"Searching "+co+" — "+(rfxI+1)+" of "+rfxQ.length+' +
'  (rfxAdded?" · "+rfxAdded+" new so far":""));' +
' google.script.run.withSuccessHandler(function(r){' +
'  if(r&&r.ok)rfxAdded+=r.added||0;else rfxFailed++;' +
'  rfxI++;rfxStep();' +
' }).withFailureHandler(function(){rfxFailed++;rfxI++;rfxStep();' +
' }).refreshMoreOne(co);' +
'}' +

'function rfxFinish(){' +
' var stopped=rfxI<rfxQ.length;' +
' rfxOn=false;' +
' rfxProgress(1,1,"Rebuilding the list…");' +
// reload through the normal path so the cards, filters and counts all rebuild
' moreLoaded=true;loadMore();' +
' rfxShow("idle");' +
' var bits=[(stopped?"Stopped after ":"Searched ")+rfxI+" compan"+(rfxI===1?"y":"ies")];' +
' bits.push(rfxAdded?(rfxAdded+" new role"+(rfxAdded===1?"":"s")+" filed"):"nothing new");' +
' if(rfxFailed)bits.push(rfxFailed+" could not be reached");' +
' say(bits.join(" · "));' +
'}' +

/* ---- statistics ---- */
'var STAT_SERIES=[["applied","Applied","#6E7A5E"],["oa","Tests","#B08344"],' +
'["interview","Interviews","#55703F"],["offer","Offers","#B4653F"]];' +
'var statsLoaded=false,statDays=30;' +
'function esc2(s){return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;")' +
' .replace(/>/g,"&gt;");}' +

// Hand-drawn SVG rather than a chart library: the page is served from Apps
// Script with no external requests, and four series of counts does not need one.
'function drawChart(d){' +
' var bs=d.buckets||[];' +
' if(!bs.length)return "<p class=\\"empty\\">Nothing recorded in this period.</p>";' +
' var W=Math.max(520,bs.length*74),H=250,L=38,R=10,T=14,B=34;' +
' var max=0;' +
' bs.forEach(function(b){STAT_SERIES.forEach(function(s){if(b[s[0]]>max)max=b[s[0]];});});' +
' if(max<4)max=4;' +
// round the axis up to something a person would choose
' var step=Math.ceil(max/4),top=step*4;' +
' var iw=W-L-R,ih=H-T-B,bw=iw/bs.length,gw=Math.min(bw*0.7,52),sw=gw/STAT_SERIES.length;' +
' var p=[];' +
' for(var g=0;g<=4;g++){' +
'  var v=step*g,y=T+ih-(v/top)*ih;' +
'  p.push("<line class=\\"grid\\" x1=\\""+L+"\\" y1=\\""+y+"\\" x2=\\""+(W-R)+"\\" y2=\\""+y+"\\"/>");' +
'  p.push("<text class=\\"axis\\" x=\\""+(L-7)+"\\" y=\\""+(y+4)+"\\" text-anchor=\\"end\\">"+v+"</text>");' +
' }' +
' bs.forEach(function(b,i){' +
'  var x0=L+i*bw+(bw-gw)/2;' +
'  STAT_SERIES.forEach(function(s,k){' +
'   var v=b[s[0]]||0;if(!v)return;' +
'   var h=(v/top)*ih,x=x0+k*sw,y=T+ih-h;' +
'   p.push("<rect class=\\"bar\\" x=\\""+x+"\\" y=\\""+y+"\\" width=\\""+(sw-2)+"\\" height=\\""+h+' +
'    "\\" fill=\\""+s[2]+"\\"><title>"+esc2(b.label+" — "+v+" "+s[1].toLowerCase())+"</title></rect>");' +
'  });' +
'  p.push("<text class=\\"axis\\" x=\\""+(L+i*bw+bw/2)+"\\" y=\\""+(H-12)+' +
'   "\\" text-anchor=\\"middle\\">"+esc2(b.label)+"</text>");' +
' });' +
' return "<div class=\\"chartscroll\\"><svg class=\\"chartsvg\\" viewBox=\\"0 0 "+W+" "+H+' +
'  "\\" preserveAspectRatio=\\"xMinYMin meet\\" role=\\"img\\">"+p.join("")+"</svg></div>";' +
'}' +

'function renderStats(d){' +
' var box=document.getElementById("statBody");' +
' if(!d||!d.ok){box.innerHTML="<p class=\\"empty\\">Could not load your statistics.</p>";return;}' +
' var t=d.totals,per=(d.bucket==="month")?"month":"week";' +
' var html="";' +
' if(!d.hasHistory){' +
'  html+="<div class=\\"warn\\">Nothing has been recorded yet. This fills in as you " +' +
'   "move roles to Applied, OA, Interview or Offer — it cannot show anything from " +' +
'   "before that tracking existed.</div>";' +
' } else if(d.since){' +
'  html+="<p class=\\"sub2\\" style=\\"margin:12px 0 0\\">Recorded since "+esc2(d.since)+".</p>";' +
' }' +
' html+="<div class=\\"funnel\\">"+STAT_SERIES.map(function(s){' +
'  return "<div class=\\"fcell\\"><b>"+(t[s[0]]||0)+"</b><span>"+s[1]+"</span></div>";' +
' }).join("")+"<div class=\\"fcell\\"><b>"+(t.removed||0)+"</b><span>Removed</span></div></div>";' +
' html+="<div class=\\"chartcard\\"><h4>Activity per "+per+"</h4>" +' +
'  "<p class=\\"hint\\">Counted on the day you moved each role forward.</p>" +' +
'  drawChart(d) +' +
'  "<div class=\\"legend\\">"+STAT_SERIES.map(function(s){' +
'   return "<span><i style=\\"background:"+s[2]+"\\"></i>"+s[1]+"</span>";' +
'  }).join("")+"</div></div>";' +
' html+="<h2 class=\\"sec\\" style=\\"margin:32px 0 0;font-size:17px\\">Where things stand now</h2>";' +
' html+="<div class=\\"funnel\\">"+(d.funnel||[]).map(function(f){' +
'  return "<div class=\\"fcell\\"><b>"+f.n+"</b><span>"+esc2(f.label)+"</span></div>";' +
' }).join("")+"</div>";' +
' box.innerHTML=html;' +
'}' +

'function slyEsc(s){return String(s==null?"":s).replace(/&/g,"&amp;")' +
'.replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/\\"/g,"&quot;");}' +
'function slyAgo(iso){' +
' if(!iso)return "\u2014";' +
' var t=Date.parse(iso);if(isNaN(t))return slyEsc(String(iso).slice(0,16));' +
' var m=Math.round((Date.now()-t)/60000);' +
' if(m<60)return m+"m ago";if(m<2880)return Math.round(m/60)+"h ago";' +
' return Math.round(m/1440)+"d ago";' +
'}' +
'function loadSally(){' +
' var box=document.getElementById("slyBody");' +
' box.innerHTML="<p class=\\"slyload\\">Waking Sally&hellip;</p>";' +
' google.script.run.withSuccessHandler(renderSally)' +
'  .withFailureHandler(function(){' +
'    box.innerHTML="<p class=\\"slyempty\\">Sally\u2019s log could not be read.</p>";})' +
'  .sallyBoard();' +
'}' +
'var SLY_PAL_=["#F59F00","#3FB950","#58A6FF","#BC8CFF","#F778BA","#39C5CF"];' +
'function slyDonut(segs){' +
' var total=0;segs.forEach(function(s){total+=s.v;});' +
' if(!total)return "<p class=\\"slyempty\\">Nothing scraped yet.</p>";' +
' var r=42,c=2*Math.PI*r,off=0,paths="";' +
' segs.forEach(function(s,i){' +
'  var frac=s.v/total,len=frac*c;' +
'  paths+="<circle cx=\\"52\\" cy=\\"52\\" r=\\""+r+"\\" fill=\\"none\\" "+' +
'   "stroke=\\""+s.c+"\\" stroke-width=\\"15\\" stroke-dasharray=\\""+len+" "+(c-len)+"\\" "+' +
'   "stroke-dashoffset=\\""+(-off)+"\\" transform=\\"rotate(-90 52 52)\\"/>";' +
'  off+=len;' +
' });' +
' var svg="<svg viewBox=\\"0 0 104 104\\" width=\\"128\\" height=\\"128\\">"+paths+' +
'  "<circle cx=\\"52\\" cy=\\"52\\" r=\\"32\\" fill=\\"#0A0E16\\"/>"+' +
'  "<text x=\\"52\\" y=\\"48\\" text-anchor=\\"middle\\" fill=\\"#E8EDF4\\" "+' +
'  "font-size=\\"17\\" font-weight=\\"800\\" font-family=\\"ui-monospace,monospace\\">"+total.toLocaleString()+"</text>"+' +
'  "<text x=\\"52\\" y=\\"62\\" text-anchor=\\"middle\\" fill=\\"#5B6B82\\" "+' +
'  "font-size=\\"8.5\\" letter-spacing=\\".08em\\">FOUND</text></svg>";' +
' var legend="<ul class=\\"slylegend\\">"+segs.map(function(s){' +
'  return "<li><span class=\\"slyswatch\\" style=\\"background:"+s.c+"\\"></span>"+' +
'   slyEsc(s.n)+"<b>"+s.v.toLocaleString()+"</b></li>";' +
' }).join("")+"</ul>";' +
' return "<div style=\\"display:flex;gap:16px;align-items:center;flex-wrap:wrap\\">"+svg+legend+"</div>";' +
'}' +
'function slyBars(runs){' +
' if(!runs.length)return "<p class=\\"slyempty\\">No runs logged yet.</p>";' +
' var ordered=runs.slice().reverse();' +   // oldest -> newest, left to right
' var max=Math.max.apply(null,ordered.map(function(r){return r.added;}).concat([1]));' +
' var h="<div class=\\"slybars\\">";' +
' ordered.forEach(function(r,i){' +
'  var pct=Math.round((r.added/max)*100),isLast=(i===ordered.length-1);' +
'  var day=String(r.started).slice(5,10);' +
'  h+="<div class=\\"slybarwrap\\" title=\\""+slyEsc(r.started.slice(0,16))+" \\u00b7 "+r.added+" banked\\">"+' +
'   "<div class=\\"slybar"+(isLast?" hi":"")+"\\" style=\\"height:"+Math.max(pct,3)+"%\\"></div>"+' +
'   "<span>"+day+"</span></div>";' +
' });' +
' return h+"</div>";' +
'}' +
'var sallyDialsOpen=false,sallyAllSources=false,sallyData=null;' +
'function renderSally(d){' +
' sallyData=d;' +
' var box=document.getElementById("slyBody");' +
' if(!d||!d.ok){box.innerHTML="<p class=\\"slyempty\\">No data.</p>";return;}' +
' var nx=document.getElementById("slyNext");' +
' nx.innerHTML=(d.lastRun?("LAST RUN "+slyAgo(d.lastRun.started)+"<br>"):"")+' +
'   (d.nextRun?("NEXT "+slyEsc(d.nextRun)):"");' +
' var h="";' +
' h+="<div class=\\"slycards\\">"+' +
'  "<div class=\\"slycard\\"><b>"+(d.lastRun?d.lastRun.added:0)+"</b>"+' +
'    "<span>banked last run</span></div>"+' +
'  "<div class=\\"slycard\\"><b>"+d.totals.added+"</b>"+' +
'    "<span>banked \u00b7 60-run window</span></div>"+' +
'  "<div class=\\"slycard\\"><b>"+d.totals.fetched.toLocaleString()+"</b>"+' +
'    "<span>postings scanned</span></div>"+' +
'  "<div class=\\"slycard\\"><b>"+(d.cost==null?"\u2014":"$"+d.cost.toFixed(2))+"</b>"+' +
'    "<span>AI spend, all time</span></div>"+' +
' "</div>";' +
' var byLast=d.sources.slice().sort(function(a,b){return b.total-a.total;});' +
' var top=byLast.slice(0,5),rest=byLast.slice(5);' +
' var segs=top.map(function(s,i){return{n:s.name,v:s.total,c:SLY_PAL_[i%SLY_PAL_.length]};});' +
' var otherTotal=rest.reduce(function(a,s){return a+s.total;},0);' +
' if(otherTotal)segs.push({n:rest.length+" more sources",v:otherTotal,c:"#2A3648"});' +
' h+="<div class=\\"slycharts\\">";' +
' h+="<div class=\\"slypanel slychart\\"><h3>Where roles come from</h3>"+slyDonut(segs)+"</div>";' +
' h+="<div class=\\"slypanel slychart\\"><h3>Banked per run</h3>"+slyBars(d.runs||[])+"</div>";' +
' h+="</div>";' +
' h+="<div class=\\"slycols\\"><div>";' +
' h+="<div class=\\"slypanel\\"><h3>Sources \u00b7 "+d.sources.length+" watched</h3>"+' +
'  "<table id=\\"slySrcTable\\"><tr><th>source</th><th>last run</th><th>60-run total</th>"+' +
'  "<th>fails</th><th>last found</th></tr>";' +
' var shown=sallyAllSources?byLast:top;' +
' shown.forEach(function(s){' +
'  h+="<tr><td>"+slyEsc(s.name)+"</td>"+' +
'   "<td class=\\"slynum\\">"+s.last+"</td>"+' +
'   "<td class=\\"slynum slydim\\">"+s.total.toLocaleString()+"</td>"+' +
'   "<td class=\\""+(s.fails?"slybad":"slydim")+"\\">"+(s.fails||"\u2014")+"</td>"+' +
'   "<td class=\\"slydim\\">"+slyAgo(s.lastFound)+"</td></tr>";' +
' });' +
' h+="</table>";' +
' if(byLast.length>5){' +
'  h+="<button class=\\"slymore\\" id=\\"slySrcMore\\">"+' +
'   (sallyAllSources?"Show top 5 only":"+ "+(byLast.length-5)+" more sources \u2014 show all")+' +
'   "</button>";' +
' }' +
' h+="</div>";' +
' h+="<div class=\\"slypanel\\" style=\\"margin-top:12px\\"><h3>Recent runs</h3>"+' +
'  "<table><tr><th>started</th><th>new</th><th>scored</th><th>banked</th>"+' +
'  "<th>archived</th></tr>";' +
' (d.runs||[]).forEach(function(r){' +
'  h+="<tr><td class=\\"slydim\\">"+slyEsc(String(r.started).slice(0,16).replace("T"," "))+"</td>"+' +
'   "<td class=\\"slynum\\">"+r.fresh+"</td><td class=\\"slynum\\">"+r.scored+"</td>"+' +
'   "<td class=\\"slynum "+(r.added?"slyok":"slydim")+"\\">"+r.added+"</td>"+' +
'   "<td class=\\"slynum slydim\\">"+r.archived+"</td></tr>";' +
' });' +
' h+="</table></div></div>";' +
' h+="<button class=\\"slytoggle"+(sallyDialsOpen?" on":"")+"\\" id=\\"slyDialsBtn\\">"+' +
'  "<span>See Sally\u2019s dials</span><span class=\\"slychev\\">\u25be</span></button>";' +
' h+="<div class=\\"slydials"+(sallyDialsOpen?" on":"")+"\\" id=\\"slyDials\\">"+' +
'  "<div class=\\"slypanel\\">";' +
' Object.keys(d.params).forEach(function(k){' +
'  var p=d.params[k],cur=d.config[k]||"";' +
'  h+="<div class=\\"slyparam\\"><label>"+slyEsc(p.label)+"</label>"+' +
'   "<small>"+slyEsc(p.help)+"</small>"+' +
'   "<div class=\\"slyrow\\"><input id=\\"sly_"+k+"\\" value=\\""+slyEsc(cur)+"\\" "+' +
'   "placeholder=\\"not set yet\\">"+' +
'   "<button class=\\"slysave\\" data-k=\\""+k+"\\">SET</button></div>"+' +
'   "<div class=\\"slymsg\\" id=\\"slymsg_"+k+"\\"></div></div>";' +
' });' +
' h+="<small style=\\"display:block;margin-top:10px;color:#48566B\\">"+' +
'  "Changes are written to the sheet\u2019s CONFIG cells and picked up at the "+' +
'  "start of Sally\u2019s next run.</small></div></div>";' +
' box.innerHTML=h;' +
// Bound directly rather than by delegation: these live inside a panel that is
// re-rendered, and assigning onclick each time cannot accumulate duplicate
// handlers the way addEventListener would. Also sets display outright so the
// open/closed state never depends on a class landing.
' var db=document.getElementById("slyDialsBtn");' +
' if(db)db.onclick=function(){' +
'  sallyDialsOpen=!sallyDialsOpen;' +
'  var panel=document.getElementById("slyDials");' +
'  if(panel)panel.style.display=sallyDialsOpen?"block":"none";' +
'  db.className="slytoggle"+(sallyDialsOpen?" on":"");' +
'  if(sallyDialsOpen&&panel&&panel.scrollIntoView)' +
'   panel.scrollIntoView({behavior:"smooth",block:"nearest"});' +
' };' +
' var panel0=document.getElementById("slyDials");' +
' if(panel0)panel0.style.display=sallyDialsOpen?"block":"none";' +
' var sm=document.getElementById("slySrcMore");' +
' if(sm)sm.onclick=function(){' +
'  sallyAllSources=!sallyAllSources;' +
'  if(sallyData)renderSally(sallyData);' +
' };' +
'}' +

'function slyRunPoll(n){' +
' if(n>40)return;' +
' setTimeout(function(){' +
'  google.script.run.withSuccessHandler(function(st){' +
'   var b=document.getElementById("slyRunBtn"),m=document.getElementById("slyRunMsg");' +
'   if(!b||!m)return;' +
'   if(st&&st.ok&&st.status==="completed"){' +
'    b.disabled=false;' +
'    m.className="slyrunmsg "+(st.conclusion==="success"?"slyok":"slybad");' +
'    m.textContent="Run "+(st.conclusion||"finished")+" · refreshing…";' +
'    sallyLoaded=true;loadSally();return;' +
'   }' +
'   var el=window.__slyT0?Math.round((Date.now()-window.__slyT0)/60000):0;' +
'   m.textContent="Sally is working ("+((st&&st.status)||"queued")+") · "' +
'    +el+" min elapsed of ~4-7";' +
'   slyRunPoll(n+1);' +
'  }).withFailureHandler(function(){slyRunPoll(n+1);}).sallyRunStatus();' +
' },15000);' +
'}' +
'var slyRunBtn0=document.getElementById("slyRunBtn");' +
'if(slyRunBtn0)slyRunBtn0.addEventListener("click",function(){' +
' var b=slyRunBtn0,m=document.getElementById("slyRunMsg");' +
' b.disabled=true;m.className="slyrunmsg";m.textContent="Asking Sally to start…";' +
' google.script.run.withSuccessHandler(function(res){' +
'  if(res&&res.ok){' +
'   var t0=Date.now();' +
'   m.textContent="Sally is scraping now · usually 4-7 min";' +
'   window.__slyT0=t0;' +
'   slyRunPoll(0);' +
'  } else {' +
'   b.disabled=false;m.className="slyrunmsg slybad";' +
'   m.textContent=(res&&res.needsToken)' +
'    ?"Add a GITHUB_TOKEN (Actions: write) in Script Properties to run on demand."' +
'    :((res&&res.error)||"Could not start a run");' +
'  }' +
' }).withFailureHandler(function(){' +
'  b.disabled=false;m.className="slyrunmsg slybad";' +
'  m.textContent="Could not start a run";' +
' }).sallyRunNow();' +
'});' +

'document.addEventListener("click",function(e){' +
' var b=e.target.closest(".slysave");if(!b)return;' +
' var k=b.dataset.k,inp=document.getElementById("sly_"+k),' +
'  msg=document.getElementById("slymsg_"+k);' +
' b.disabled=true;msg.textContent="";msg.className="slymsg";' +
' google.script.run.withSuccessHandler(function(res){' +
'  b.disabled=false;' +
'  if(res&&res.ok){msg.textContent="Saved \u2014 applies from the next run";' +
'   msg.className="slymsg slyok";}' +
'  else{msg.textContent=(res&&res.error)||"Could not save";' +
'   msg.className="slymsg slybad";}' +
' }).withFailureHandler(function(){b.disabled=false;' +
'  msg.textContent="Could not save";msg.className="slymsg slybad";' +
' }).sallySave(k,inp.value);' +
'});' +

'function loadStats(){' +
' var box=document.getElementById("statBody");' +
' box.innerHTML="<p class=\\"empty\\">Loading&hellip;</p>";' +
' google.script.run.withSuccessHandler(renderStats)' +
'  .withFailureHandler(function(){' +
'    box.innerHTML="<p class=\\"empty\\">Could not load your statistics.</p>";})' +
'  .statsData(statDays);' +
'}' +
'document.getElementById("statRanges").addEventListener("click",function(e){' +
' var b=e.target.closest(".rbtn");if(!b)return;' +
' this.querySelectorAll(".rbtn").forEach(function(x){x.classList.remove("on")});' +
' b.classList.add("on");statDays=Number(b.dataset.days);loadStats();' +
'});' +

/* ---- "not this posting": reject the match, keep watching the programme ---- */
'document.addEventListener("click",function(e){' +
' var b=e.target.closest(".skipx");if(!b)return;' +
' var shell=b.closest(".shell");if(!shell)return;' +
' b.disabled=true;' +
' google.script.run.withSuccessHandler(function(res){' +
'  if(!res||!res.ok){b.disabled=false;say((res&&res.error)||"Could not update");return;}' +
'  var g=gridOf("watch");' +
'  shell.classList.add("leaving");' +
'  setTimeout(function(){' +
'   shell.remove();' +
'   if(g&&res.card){var tmp=document.createElement("tbody");tmp.innerHTML=res.card;' +
'    if(tmp.firstElementChild)g.appendChild(tmp.firstElementChild);}' +
'   recountAll();' +
'  },220);' +
'  say("Skipped that posting — still watching "+res.company);' +
' }).withFailureHandler(function(){b.disabled=false;say("Could not update");' +
' }).skipOpeningSoonPosting(Number(b.dataset.row),b.dataset.company,b.dataset.url);' +
'});' +

'document.addEventListener("click",function(e){' +
' var b=e.target.closest(".soonx,.soonstop");if(!b)return;' +
' var shell=b.closest(".shell");if(!shell)return;' +
' b.disabled=true;' +
' google.script.run.withSuccessHandler(function(res){' +
'  if(!res||!res.ok){b.disabled=false;say((res&&res.error)||"Could not update");return;}' +
'  shell.classList.add("leaving");' +
'  setTimeout(function(){shell.remove();' +
'   var l=document.getElementById("soonList");' +
'   if(l&&!l.querySelectorAll(".shell").length){' +
'    l.className="empty";l.textContent="Nothing tracked yet — add the first one below.";}' +
'   recountAll();eqhAll();},220);' +
'  say("Stopped tracking "+res.company);' +
' }).withFailureHandler(function(){b.disabled=false;say("Could not update");' +
' }).dismissOpeningSoon(Number(b.dataset.row),b.dataset.company);' +
'});' +
/* ---- status tabs ---- */
// Mirrors BUCKETS server-side. Counts are read back off the DOM rather than
// nudged by a delta, so they cannot drift out of step with the cards.
'var BUCKET_OF={"OA":"waiting","Interview":"waiting","Offer":"waiting",' +
'"Not applied":"apply","":"apply","Applied":"waiting","Rejected":"closed"};' +
'var BKEYS=["apply","watch","waiting","closed"];' +

'function gridOf(key){return document.getElementById("grid-"+key);}' +
// counts live in each group's band row and are read back off the DOM, never
// nudged by a delta, so they cannot drift out of step with the rows
'function recount(key){' +
' var g=gridOf(key);if(!g)return;' +
' var n=g.querySelectorAll(".shell").length;' +
' var c=g.querySelector(".gcnt");if(c)c.textContent="\u00B7 "+n;' +
' var tb=document.querySelector(".btab[data-b="+key+"] b");' +
' if(tb)tb.textContent=n;' +
' var ph=g.querySelector(".bempty");if(ph)ph.style.display=n?"none":"";' +
'}' +
// a slice tab narrows the ONE table to one band; All puts everything back
'function showBucket(key){' +
' BKEYS.forEach(function(k){var g=gridOf(k);' +
'  if(g)g.style.display=(key==="all"||k===key)?"":"none";});' +
' document.querySelectorAll(".btab").forEach(function(t){' +
'  t.classList.toggle("on",t.dataset.b===key);});' +
' buildFilters("list");' +
'}' +
'document.addEventListener("click",function(e){' +
' var t=e.target.closest(".btab");if(!t)return;showBucket(t.dataset.b);});' +
// the header stats are per-status, finer than the groups, so they scan the
// status dropdowns rather than the groups
'function statScan(){' +
' var live=0,open=0,app=0;' +
' document.querySelectorAll("#list .sel").forEach(function(w){' +
'  var s=w.dataset.status||"";' +
'  if(s==="OA"||s==="Interview")live++;' +
'  else if(s==="Applied")app++;' +
'  else if(s!=="Rejected"&&s!=="Offer")open++;});' +
' [["statLive",live],["statOpen",open],["statApplied",app]].forEach(function(p){' +
'  var el=document.getElementById(p[0]);if(el)el.textContent=p[1];});' +
'}' +
'function recountAll(){BKEYS.forEach(recount);statScan();' +
' var ab=document.querySelector(".btab[data-b=all] b");' +
' if(ab)ab.textContent=document.querySelectorAll("#list .shell").length;}' +

// Card grid or one-per-row list. Only a class on each grid changes — every
// filter, the search box, the heart and the status dropdown all act on the same
// .shell elements either way, so nothing needs teaching about the new view.
// The dense list IS the view now (2026-09-01: cards retired for Trackr-style
// rows). viewTogHtml and setListView keep their names because More roles calls
// both after rebuilding its filter bar; there is simply nothing to choose.
'var curView="rows";' +
'function viewTogHtml(){return "";}' +
'function setListView(){' +
' document.querySelectorAll("#list .grid,#moreList").forEach(function(g){' +
'  g.classList.add("rows");});' +
' eqhAll();' +
'}' +
'setListView();' +
// the whole grouped list is on the page at once now — nothing to pre-open
'recountAll();' +

'function placeByScore(grid,el){' +
// hearted roles hold the top of the grid whatever the scores below them say
' if(el.classList.contains("is-fav")){frontOf(grid,el);return;}' +
' var score=parseInt((el.querySelector(".score")||{}).textContent,10)||0;' +
' var kids=grid.querySelectorAll(".shell");' +
' for(var k=0;k<kids.length;k++){' +
'  if(kids[k]===el||kids[k].classList.contains("is-fav"))continue;' +
'  var sc=kids[k].querySelector(".score");' +
'  if(score>(sc?parseInt(sc.textContent,10)||0:0)){grid.insertBefore(el,kids[k]);return;}' +
' }' +
' grid.appendChild(el);' +
'}' +
'function regroup(shell,status){' +
' if(!shell)return;' +
' var from=shell.closest(".bucket");' +
' var key=BUCKET_OF[status]||"apply";' +
' var fromKey=from?from.id.replace("grid-",""):null;' +
// Rejected has no tab: it is not something to work through any more, so the
// card leaves the list rather than sitting in a group that no longer exists.
' var grid=gridOf(key);' +
' if(!grid){' +
'  shell.classList.add("leaving");' +
'  setTimeout(function(){shell.remove();if(fromKey)recount(fromKey);eqhAll();},200);' +
'  say("Marked "+status+" — removed from the list");return;' +
' }' +
' if(fromKey===key){recount(key);return;}' +
' placeByScore(grid,shell);' +
' shell.classList.add("moved");' +
' setTimeout(function(){shell.classList.remove("moved");},900);' +
' if(fromKey)recount(fromKey);' +
' recount(key);buildFilters("list");eqhAll();' +
' var g2=gridOf(key),lb=g2?g2.querySelector("tr.grp td"):null;' +
' say("Moved to "+(lb?lb.childNodes[0].textContent.trim():key));' +
'}' +

// a newly added role has never been applied to, so it lands in "To apply"
'function insertBankCard(html,score){' +
' var grid=gridOf("apply");if(!grid||!html)return;' +
' var tmp=document.createElement("tbody");tmp.innerHTML=html;' +
' var el=tmp.firstElementChild;if(!el)return;' +
' var list=document.getElementById("list");' +
' var ph=list?list.querySelector(".empty:not(.bempty)"):null;if(ph)ph.remove();' +
' placeByScore(grid,el);recount("apply");eqhAll();' +
'}' +
'function bumpOpen(){recountAll();}' +

'function eqh(sel){' +
' var box=document.querySelector(sel);if(!box)return;' +
' var all=Array.prototype.slice.call(box.querySelectorAll(".shell"));' +
' all.forEach(function(s){s.style.minHeight="";});' +
// Equal heights is a CARD-GRID idea: three tiles per row should line up. In list
// view every row is already one line tall, and padding them all to the tallest
// (an expanded row, a two-line title) left a column of half-empty rows.
' var vis=all.filter(function(s){' +
'  return s.tagName!=="TR"&&s.offsetParent!==null&&' +
'   !s.classList.contains("expanded")&&' +
'   !(s.parentNode&&s.parentNode.classList&&' +
'     s.parentNode.classList.contains("rows"));});' +
' var max=0;' +
' vis.forEach(function(s){if(s.offsetHeight>max)max=s.offsetHeight;});' +
' if(max)vis.forEach(function(s){s.style.minHeight=max+"px";});' +
'}' +
'function eqhAll(){eqh("#list");eqh("#moreList");}' +
'eqhAll();' +
'window.addEventListener("resize",function(){' +
' clearTimeout(window.__eqhT);window.__eqhT=setTimeout(eqhAll,150);});' +
'})();' +
'</script></body></html>';
}
