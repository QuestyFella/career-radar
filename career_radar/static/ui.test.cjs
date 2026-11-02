'use strict';
// UI-only fixtures. These never enter the application database or live interface.
const test = require('node:test');
const assert = require('node:assert/strict');
const { existsSync } = require('node:fs');
const { resolve } = require('node:path');
const {
  safeHttpUrl, buildQuery, formatDeadline, sourcePresentation, normalizeJobList,
  normalizeTime, parseTimeList, normalizeHttpUrl, splitUrlList, normalizeChips, splitChipInput,
  keyHint, formatDuration, summarizeDiscoveryRun, summarizeScoutRun, initials, validateResumeFile,
  looksLikePdf, resumeFilename, sourceOriginLabel, filterSources, errorDetail, cleanProfile,
  candidateStatus, scheduleJobState, parseIntInRange, summarizeExtracted, pdfDate, viewFromQuery
} = require('./app.js');

// The static files live in career_radar/static; the repository root is two levels up.
const REPO_ROOT = resolve(__dirname, '..', '..');
const urlCases = require(resolve(REPO_ROOT, 'tests/fixtures/url_safety_cases.json'));
const PYTHON = resolve(REPO_ROOT, '.venv/bin/python');

for (const fixture of urlCases.cases) {
  test(`shared URL policy: ${fixture.id}`, () => {
    assert.equal(safeHttpUrl(fixture.value), fixture.canonical === null ? null : fixture.value);
    if (fixture.canonical !== null) assert.equal(safeHttpUrl(fixture.canonical), fixture.canonical);
  });
}

test('shared authority/identity examples retain the original valid link', () => {
  for (const group of [urlCases.equivalent, urlCases.distinct]) {
    assert.equal(new Set(group.map((fixture) => fixture.id)).size, group.length);
    for (const fixture of group) {
      for (const value of fixture.urls) assert.equal(safeHttpUrl(value), value, fixture.id);
    }
  }
  assert.equal(new Set(urlCases.cases.map((fixture) => fixture.id)).size, urlCases.cases.length);
});

test('real core date-only deadlines round-trip using their explicit calendar field', { skip: !existsSync(PYTHON) && 'no .venv python in this checkout' }, () => {
  const { execFileSync } = require('node:child_process');
  const raw = { title: 'Junior Software Developer', company: 'UI round-trip fixture', location: 'Toronto, ON', url: 'https://example.com/fixture', deadline: '2026-11-05', deadline_evidence: 'Closing date: 2026-11-05' };
  const script = 'import sys, json\nfrom career_radar import core\nprint(json.dumps(core.normalize_job(json.loads(sys.stdin.read()))))';
  const normalized = JSON.parse(execFileSync(PYTHON, ['-c', script], { cwd: REPO_ROOT, input: JSON.stringify(raw), encoding: 'utf8' }));
  assert.equal(normalized.deadline_date, '2026-11-05');
  assert.equal(formatDeadline(normalized.deadline_date || normalized.deadline, normalized.deadline_kind), 'Nov 5, 2026');
});

test('external links accept only absolute http(s) URLs without credentials or ambiguous whitespace', () => {
  assert.equal(safeHttpUrl('https://example.com/jobs?a=1&b=2'), 'https://example.com/jobs?a=1&b=2');
  assert.equal(safeHttpUrl('http://example.com/job/42'), 'http://example.com/job/42');
  for (const url of ['javascript:alert(1)', 'data:text/html,hi', '//example.com', '/jobs/42', 'https://user:password@example.com', 'https://example.com/\nmalicious', 'https:\\\\example.com', '', null]) {
    assert.equal(safeHttpUrl(url), null, String(url));
  }
});

test('external job links reject browser-normalized private and literal IP hosts', () => {
  for (const url of ['http://１２７.０.０.１/review', 'http://127。0.0.1/review', 'http://127.1/review', 'http://2130706433/review', 'http://localhost/review', 'http://host.internal/review', 'http://[::1]/review', 'http://10.1.2.3/review']) {
    assert.equal(safeHttpUrl(url), null, url);
  }
  assert.equal(safeHttpUrl('https://www.café.ca/jobs'), 'https://www.café.ca/jobs');
});

test('job filters use the exact API keys and preserve arbitrary dynamic categories', () => {
  const query = buildQuery({ q: ' firmware & test ', level: 'co_op', category: 'Research & simulation', location: 'Ottawa, ON', status: 'saved', sort: 'deadline', priorityOnly: true, includeClosed: true, deadlineDays: '7' }, 3);
  assert.equal(query.get('q'), 'firmware & test');
  assert.equal(query.get('level'), 'co_op');
  assert.equal(query.get('category'), 'Research & simulation');
  assert.equal(query.get('location'), 'Ottawa, ON');
  assert.equal(query.get('status'), 'saved');
  assert.equal(query.get('sort'), 'deadline');
  assert.equal(query.get('priority_only'), 'true');
  assert.equal(query.get('include_closed'), 'true');
  assert.equal(query.get('deadline_days'), '7');
  assert.equal(query.get('page'), '3');
  assert.equal(query.get('page_size'), '30');
  assert.equal(new URLSearchParams(query.toString()).get('q'), 'firmware & test');
});

test('default filters are explicit and invalid enums cannot become API parameters', () => {
  const query = buildQuery({ sort: 'untrusted', level: 'senior', status: 'unknown' }, -2);
  assert.equal(query.get('sort'), 'priority');
  assert.equal(query.get('page'), '1');
  assert.equal(query.get('priority_only'), 'false');
  assert.equal(query.get('include_closed'), 'false');
  assert.equal(query.has('level'), false);
  assert.equal(query.has('status'), false);
  assert.equal(query.has('deadline_days'), false);
});

test('unknown deadlines are explicitly Not published, never rolling or invented', () => {
  assert.equal(formatDeadline(null), 'Not published');
  assert.equal(formatDeadline(undefined), 'Not published');
  assert.equal(formatDeadline('invalid'), 'Not published');
  assert.equal(formatDeadline('2026-02-30', 'date'), 'Not published');
});

test('date-only deadlines never shift calendar dates in a western timezone', () => {
  assert.equal(formatDeadline('2026-11-06', 'date', 'America/Toronto'), 'Nov 6, 2026');
  assert.equal(formatDeadline('2026-11-06T00:00:00Z', 'date', 'America/Toronto'), 'Nov 6, 2026');
  assert.match(formatDeadline('2026-11-06T19:30:00Z', 'datetime', 'America/Toronto'), /Nov 6, 2026/);
  assert.match(formatDeadline('2026-11-06T19:30:00Z', 'datetime', 'America/Toronto'), /2:30/);
});

test('source health keeps limited, unavailable, pending and unknown distinct', () => {
  assert.equal(sourcePresentation('ok').label, 'Working');
  assert.equal(sourcePresentation('partial').label, 'Limited');
  assert.equal(sourcePresentation('error').label, 'Unavailable');
  assert.equal(sourcePresentation('blocked').label, 'Restricted');
  assert.equal(sourcePresentation('pending').label, 'Not checked');
  assert.equal(sourcePresentation('unexpected').label, 'Unknown');
  assert.notEqual(sourcePresentation('partial').tone, sourcePresentation('ok').tone);
});

test('job pagination requires genuine API data and validates the list envelope', () => {
  assert.deepEqual(normalizeJobList({ items: [], total: 0, page: 1, page_size: 30, pages: 0 }), { items: [], total: 0, page: 1, pageSize: 30, pages: 0 });
  assert.throws(() => normalizeJobList({}), /invalid/i);
  assert.throws(() => normalizeJobList({ items: [], total: 'unknown', page: 1, page_size: 30, pages: 1 }), /invalid/i);
  assert.throws(() => normalizeJobList({ items: [{ title: 'UI-only fixture missing ID' }], total: 1, page: 1, page_size: 30, pages: 1 }), /invalid/i);
});

// --- Profile, sources, discovery and settings helpers -----------------------------------

test('schedule times accept 24-hour HH:MM only and normalize browser time values', () => {
  assert.equal(normalizeTime('08:00'), '08:00');
  assert.equal(normalizeTime(' 8:05 '), '08:05');
  assert.equal(normalizeTime('18:30:00'), '18:30');
  assert.equal(normalizeTime('23:59'), '23:59');
  assert.equal(normalizeTime('00:00'), '00:00');
  for (const value of ['24:00', '12:60', '7:5', '8', '08:00 PM', 'noon', '', null, undefined, 800]) {
    assert.equal(normalizeTime(value), null, String(value));
  }
});

test('time lists are sorted, deduplicated and report invalid entries and the 12-time limit', () => {
  assert.deepEqual(parseTimeList(['18:00', '8:00', '08:00', '']), { times: ['08:00', '18:00'], invalid: [], tooMany: false });
  assert.deepEqual(parseTimeList('18:00, 7:30 25:00'), { times: ['07:30', '18:00'], invalid: ['25:00'], tooMany: false });
  const many = Array.from({ length: 13 }, (_, hour) => `${String(hour).padStart(2, '0')}:00`);
  const result = parseTimeList(many);
  assert.equal(result.tooMany, true);
  assert.equal(result.times.length, 12);
  assert.deepEqual(parseTimeList(null), { times: [], invalid: [], tooMany: false });
});

test('pasted board URL lists keep up to 10 unique http(s) addresses and report the rest', () => {
  const pasted = 'https://jobs.lever.co/acme\n  https://jobs.lever.co/acme/ , job-boards.greenhouse.io/example.\n<https://jobs.ashbyhq.com/demo>\nftp://example.com/x javascript:alert(1) hello';
  const result = splitUrlList(pasted);
  assert.deepEqual(result.urls, ['https://jobs.lever.co/acme', 'https://job-boards.greenhouse.io/example', 'https://jobs.ashbyhq.com/demo']);
  assert.deepEqual(result.invalid, ['ftp://example.com/x', 'javascript:alert(1)', 'hello']);
  assert.equal(result.extra, 0);
  const lots = Array.from({ length: 12 }, (_, index) => `https://jobs.lever.co/company${index}`).join('\n');
  const capped = splitUrlList(lots);
  assert.equal(capped.urls.length, 10);
  assert.equal(capped.extra, 2);
  assert.deepEqual(splitUrlList(''), { urls: [], invalid: [], extra: 0 });
  assert.equal(normalizeHttpUrl('github.com/example'), 'https://github.com/example');
  assert.equal(normalizeHttpUrl('http://localhost:8080/board'), null);
  assert.equal(normalizeHttpUrl('mailto:me@example.com'), null);
});

test('chip lists trim, collapse spaces, dedupe case-insensitively and respect limits', () => {
  assert.deepEqual(normalizeChips(['  Python ', 'python', 'C++', '', '  ', 'Machine   learning', 42, null]), ['Python', 'C++', 'Machine learning']);
  assert.deepEqual(normalizeChips(['a', 'b', 'c', 'd'], { max: 2 }), ['a', 'b']);
  assert.deepEqual(normalizeChips(['abcdefghij'], { maxLength: 4 }), ['abcd']);
  assert.deepEqual(normalizeChips('Python'), []);
  assert.deepEqual(splitChipInput('Python, C++; Rust\nGo'), ['Python', 'C++', 'Rust', 'Go']);
  assert.deepEqual(splitChipInput('Toronto, ON; Remote', false), ['Toronto, ON', 'Remote']);
  assert.deepEqual(splitChipInput(' , ;'), []);
});

test('provider key hints never reveal more than the server hint', () => {
  assert.equal(keyHint({ set: true, source: 'file', hint: '…a1b2' }, { key_env: 'OPENAI_API_KEY' }), 'Key saved (…a1b2)');
  assert.equal(keyHint({ set: true, source: 'file', hint: 'a1b2' }, {}), 'Key saved (…a1b2)');
  assert.equal(keyHint({ set: true, source: 'file', hint: 'set' }, {}), 'Key saved');
  assert.equal(keyHint({ set: true, source: 'env', hint: '…zzzz' }, { key_env: 'OPENAI_API_KEY' }), 'From environment variable OPENAI_API_KEY');
  assert.equal(keyHint({ set: true, source: 'env' }, {}), 'From an environment variable');
  assert.equal(keyHint({ set: false }, { id: 'openai' }), 'Not set');
  assert.equal(keyHint(undefined, { id: 'openai-compatible' }), 'Not set (optional for this provider)');
});

test('durations read naturally', () => {
  assert.equal(formatDuration(4.25), '4.3 s');
  assert.equal(formatDuration(42), '42 s');
  assert.equal(formatDuration(270), '4 min 30 s');
  assert.equal(formatDuration(600), '10 min');
  assert.equal(formatDuration(3720), '1 h 2 min');
  assert.equal(formatDuration(-1), '');
  assert.equal(formatDuration('soon'), '');
});

test('discovery run summaries count probes, additions, drops and outcomes', () => {
  const summary = summarizeDiscoveryRun({
    ok: true, skipped: false, finished_at: '2026-11-01T09:30:00+00:00', duration_seconds: 270, probed: 120,
    not_probed_time_budget: 4, by_status: { verified: 3, no_canada_tech: 80, error: 0, not_found: 37 },
    added_count: 3, added: [{ name: 'Acme', type: 'greenhouse', canada_tech: 5, matched_now: 2 }, { name: 'Beta', type: 'lever', canada_tech: 1 }, { name: '' }],
    dropped: ['Old Co'], discovered_total: 990
  });
  assert.equal(summary.headline, 'Checked 120 candidates in 4 min 30 s: 3 added, 1 dropped.');
  assert.deepEqual(summary.added.map((item) => item.name), ['Acme', 'Beta']);
  assert.equal(summary.added[0].canadaTech, 5);
  assert.deepEqual(summary.dropped, ['Old Co']);
  assert.deepEqual(summary.byStatus, [['no_canada_tech', 80], ['not_found', 37], ['verified', 3]]);
  assert.equal(summary.notProbed, 4);
  assert.equal(summary.finishedAt, '2026-11-01T09:30:00+00:00');
  const skipped = summarizeDiscoveryRun({ ok: true, skipped: true, reason: 'Another discovery run is in progress.' });
  assert.equal(skipped.skipped, true);
  assert.equal(skipped.headline, 'Skipped: Another discovery run is in progress.');
  assert.equal(summarizeDiscoveryRun({ probed: 1, added_count: 0, dropped: [] }).headline, 'Checked 1 candidate: 0 added, 0 dropped.');
  assert.equal(summarizeDiscoveryRun(null), null);
});

test('scout summaries list themes, leads and errors', () => {
  const summary = summarizeScoutRun({ finished_at: '2026-11-01T06:30:00Z', themes: ['Aerospace in Quebec', 'Fintech in Toronto'], lead_count: 5, suggestions: 9, errors: ['Fintech in Toronto: timeout'] });
  assert.equal(summary.headline, '2 themes: 9 suggestions, 5 board leads to verify.');
  assert.deepEqual(summary.themes, ['Aerospace in Quebec', 'Fintech in Toronto']);
  assert.deepEqual(summary.errors, ['Fintech in Toronto: timeout']);
  assert.equal(summarizeScoutRun(undefined), null);
});

test('owner initials come from the profile name only', () => {
  assert.equal(initials('Ada Lovelace'), 'AL');
  assert.equal(initials('  grace  brewster murray hopper '), 'GH');
  assert.equal(initials('Zoë'), 'Z');
  assert.equal(initials('(Sam) O’Neil'), 'SO');
  assert.equal(initials(''), '');
  assert.equal(initials(null), '');
});

test('resume files must be non-empty PDFs up to 8 MB', () => {
  assert.equal(validateResumeFile({ name: 'cv.pdf', type: 'application/pdf', size: 120_000 }).ok, true);
  assert.equal(validateResumeFile({ name: 'cv.PDF', type: '', size: 120_000 }).ok, true);
  assert.match(validateResumeFile({ name: 'cv.docx', type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', size: 1000 }).message, /not a PDF/);
  assert.match(validateResumeFile({ name: 'cv.pdf', type: 'application/msword', size: 1000 }).message, /not a PDF/);
  assert.match(validateResumeFile({ name: 'big.pdf', type: 'application/pdf', size: 9 * 1024 * 1024 }).message, /8 MB/);
  assert.equal(validateResumeFile({ name: 'edge.pdf', type: 'application/pdf', size: 8 * 1024 * 1024 }).ok, true);
  assert.match(validateResumeFile({ name: 'empty.pdf', type: 'application/pdf', size: 0 }).message, /empty/);
  assert.equal(validateResumeFile(null).ok, false);
  const encoder = new TextEncoder();
  assert.equal(looksLikePdf(encoder.encode('%PDF-1.7\n...')), true);
  assert.equal(looksLikePdf(encoder.encode('\n\n  %PDF-1.4')), true);
  assert.equal(looksLikePdf(encoder.encode('PK\u0003\u0004 not a pdf')), false);
  assert.equal(looksLikePdf(null), false);
  assert.equal(resumeFilename('C:\\Users\\me\\My CV.pdf'), 'My CV.pdf');
  assert.equal(resumeFilename('/tmp/a/b/resume.pdf'), 'resume.pdf');
  assert.equal(resumeFilename(''), 'resume.pdf');
  assert.equal(resumeFilename(`${'x'.repeat(300)}.pdf`).length, 120);
});

test('source origins and filters match the Sources view controls', () => {
  assert.equal(sourceOriginLabel('builtin'), 'Built-in');
  assert.equal(sourceOriginLabel('discovered'), 'Discovered');
  assert.equal(sourceOriginLabel('user'), 'Added by you');
  assert.equal(sourceOriginLabel('other'), 'Other');
  const items = [
    { id: 'greenhouse-acme', name: 'Acme Robotics', type: 'greenhouse', url: 'https://job-boards.greenhouse.io/acme', origin: 'builtin', enabled: true, status: 'ok' },
    { id: 'lever-beta', name: 'Beta', type: 'lever', url: 'https://jobs.lever.co/beta', origin: 'discovered', enabled: true, status: 'error' },
    { id: 'user-linkedin-firmware', name: 'LinkedIn public — firmware', type: 'linkedin', url: 'https://www.linkedin.com/jobs', keywords: 'firmware intern', origin: 'user', enabled: true, status: 'pending' },
    { id: 'ashby-gamma', name: 'Gamma', type: 'ashby', url: 'https://jobs.ashbyhq.com/gamma', origin: 'discovered', enabled: false, status: 'disabled' }
  ];
  const ids = (filters) => filterSources(items, filters).map((item) => item.id);
  assert.deepEqual(ids({}), items.map((item) => item.id));
  assert.deepEqual(ids({ health: 'attention' }), ['lever-beta']);
  assert.deepEqual(ids({ health: 'ok' }), ['greenhouse-acme']);
  assert.deepEqual(ids({ health: 'pending' }), ['user-linkedin-firmware']);
  assert.deepEqual(ids({ health: 'disabled' }), ['ashby-gamma']);
  assert.deepEqual(ids({ origin: 'discovered' }), ['lever-beta', 'ashby-gamma']);
  assert.deepEqual(ids({ type: 'lever' }), ['lever-beta']);
  assert.deepEqual(ids({ text: 'FIRMWARE' }), ['user-linkedin-firmware']);
  assert.deepEqual(ids({ text: 'acme robotics' }), ['greenhouse-acme']);
  assert.deepEqual(ids({ text: 'greenhouse', origin: 'discovered' }), []);
  assert.deepEqual(filterSources(null, {}), []);
});

test('API error details become one readable sentence', () => {
  assert.equal(errorDetail({ detail: 'Already monitored as “Acme”.' }, 409), 'Already monitored as “Acme”.');
  assert.equal(errorDetail({ detail: [{ loc: ['body', 'schedule', 'collect_times'], msg: 'invalid time' }] }, 422), 'schedule → collect_times: invalid time');
  assert.equal(errorDetail({ detail: { message: 'Nope' } }, 400), 'Nope');
  assert.equal(errorDetail({ message: 'Busy' }, 503), 'Busy');
  assert.equal(errorDetail(null, 500), 'The request failed (500).');
});

test('profile payloads are cleaned without inventing data', () => {
  const cleaned = cleanProfile({
    name: '  Ada   Lovelace ', email: ' ada@example.com ', graduation: '', headline: 42,
    education: [{ school: ' Waterloo ', degree: 'BASc', field: '', graduation: '2027-04' }, { school: ' ', degree: '', field: '', graduation: '' }],
    experience: [{ title: 'Firmware intern', company: 'Acme', start: '2025-05', end: '2025-08', extra: 'x' }, 'bad'],
    skills: ['Python', 'python', ' C++ '], interests: ['embedded'], preferred_locations: ['Toronto, ON'], exclude_keywords: [],
    links: ['https://github.com/example'], levels: ['co_op', 'senior', 'internship', 'co_op'], source: 'resume', updated_at: '2026-11-01T12:00:00+00:00'
  });
  assert.deepEqual(cleaned, {
    name: 'Ada Lovelace', email: 'ada@example.com', location: '', headline: '', graduation: '',
    education: [{ school: 'Waterloo', degree: 'BASc', field: '', graduation: '2027-04' }],
    experience: [{ title: 'Firmware intern', company: 'Acme', start: '2025-05', end: '2025-08' }],
    skills: ['Python', 'C++'], interests: ['embedded'], preferred_locations: ['Toronto, ON'], exclude_keywords: [],
    links: ['https://github.com/example'], levels: ['internship', 'co_op']
  });
  assert.equal(Object.hasOwn(cleaned, 'source'), false);
  assert.deepEqual(cleanProfile(null).skills, []);
});

test('board check outcomes explain themselves and only offer sensible additions', () => {
  assert.equal(candidateStatus('verified').canAdd, true);
  assert.equal(candidateStatus('verified').tone, 'ok');
  assert.equal(candidateStatus('no_canada_tech').canAdd, true);
  assert.equal(candidateStatus('error').canAdd, true);
  for (const status of ['not_found', 'excluded', 'already_monitored', 'surprise']) assert.equal(candidateStatus(status).canAdd, false, status);
  assert.match(candidateStatus('not_found').text, /No public job board/);
  assert.equal(candidateStatus('surprise').label, 'Surprise');
});

test('scheduled job states distinguish running, never run, OK and failed', () => {
  assert.deepEqual(scheduleJobState({ running: true }), { tone: 'running', label: 'Running now' });
  assert.deepEqual(scheduleJobState({ last_run: null }), { tone: 'pending', label: 'Not run yet' });
  assert.deepEqual(scheduleJobState({ last_run: '2026-11-01T13:00:00Z', last_ok: true }), { tone: 'ok', label: 'Last run OK' });
  assert.deepEqual(scheduleJobState({ last_run: '2026-11-01T13:00:00Z', last_ok: false }), { tone: 'error', label: 'Last run failed' });
  assert.equal(scheduleJobState(null).tone, 'unknown');
});

test('whole-number settings stay inside their ranges', () => {
  assert.equal(parseIntInRange('40', 5, 180), 40);
  assert.equal(parseIntInRange(' 6 ', 1, 6), 6);
  for (const value of ['', '0', '7', '2.5', 'ten', null, undefined]) assert.equal(parseIntInRange(value, 1, 6), null, String(value));
});

test('resume extraction and PDF dates are summarized for people, not machines', () => {
  assert.equal(summarizeExtracted({ name: 'Ada', email: '', skills: ['Python', 'C'], education: [{ school: 'Waterloo' }], experience: [], links: ['https://x.example'] }), 'name, 2 skills, 1 education entry, 1 link');
  assert.equal(summarizeExtracted({}), '');
  assert.equal(summarizeExtracted(null), '');
  assert.equal(pdfDate('D:20240105120000Z'), '2024-01-05');
  assert.equal(pdfDate('D:2024'), '2024-01-01');
  assert.equal(pdfDate('2024-03-09T10:00:00Z'), '2024-03-09');
  assert.equal(pdfDate('D:20241305'), null);
  assert.equal(pdfDate('yesterday'), null);
});

test('view URLs accept only known views', () => {
  assert.equal(viewFromQuery('?view=settings'), 'settings');
  assert.equal(viewFromQuery('?view=sources&job=4'), 'sources');
  assert.equal(viewFromQuery('?view=__proto__'), 'feed');
  assert.equal(viewFromQuery(''), 'feed');
});
