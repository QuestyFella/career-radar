/* Career Radar. Same-origin API only; external listing content is always plain text. */
(function () {
  'use strict';

  const LEVELS = Object.freeze({ internship: 'Internship', co_op: 'Co-op', new_grad: 'New grad', entry_level: 'Entry level' });
  const STATUSES = new Set(['new', 'saved', 'applied', 'dismissed']);
  const SORTS = new Set(['priority', 'newest', 'deadline']);
  const DEFAULT_TIMEZONE = 'America/Toronto';
  const numberFormat = new Intl.NumberFormat('en-CA');

  function safeHttpUrl(value) {
    if (typeof value !== 'string' || /[\u0000-\u0020\u007f\\\ud800-\udfff]/u.test(value)) return null;
    // Reject repairs before WHATWG parsing: empty userinfo, encoded authorities,
    // extra slashes and empty/non-ASCII ports must not become valid job links.
    const authority = value.match(/^https?:\/\/([^/?#]+)/i);
    if (!authority || /[@%]/.test(authority[1]) || !/^(?:\[[^\[\]]+\]|[^:\[\]]+)(?::[0-9]+)?$/.test(authority[1])) return null;
    try {
      const parsed = new URL(value);
      if (!['https:', 'http:'].includes(parsed.protocol) || !parsed.hostname || parsed.username || parsed.password || !['', '80', '443'].includes(parsed.port)) return null;
      const host = parsed.hostname.toLowerCase().replace(/\.$/, '');
      // Apply the job-link policy to the browser's actual ASCII/IDNA hostname,
      // after Unicode/numeric aliases have been resolved. Shared Python/Node
      // cases live in tests/fixtures/url_safety_cases.json.
      if (!host.includes('.') || host.length > 253 || /(?:^|\.)(?:localhost|localdomain|local|internal|lan|home|home\.arpa)$/.test(host)) return null;
      const labels = host.split('.');
      if (!labels.every((label) => /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(label)) || /^(?:[0-9]+|0x[0-9a-f]*)$/i.test(labels[labels.length - 1])) return null;
      return value;
    } catch (_) { return null; }
  }

  function buildQuery(filters = {}, page = 1) {
    const query = new URLSearchParams();
    for (const key of ['q', 'category', 'location']) {
      if (typeof filters[key] === 'string' && filters[key].trim()) query.set(key, filters[key].trim());
    }
    if (Object.hasOwn(LEVELS, filters.level)) query.set('level', filters.level);
    if (STATUSES.has(filters.status)) query.set('status', filters.status);
    query.set('sort', SORTS.has(filters.sort) ? filters.sort : 'priority');
    query.set('priority_only', filters.priorityOnly === true ? 'true' : 'false');
    query.set('include_closed', filters.includeClosed === true ? 'true' : 'false');
    const days = Number(filters.deadlineDays);
    if (Number.isInteger(days) && days > 0 && days <= 366) query.set('deadline_days', String(days));
    query.set('page', String(Number.isInteger(page) && page > 0 ? page : 1));
    query.set('page_size', '30');
    return query;
  }

  function parseDate(value) {
    if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}(?:$|[T ])/.test(value)) return null;
    const [year, month, day] = value.slice(0, 10).split('-').map(Number);
    const calendar = new Date(`${value.slice(0, 10)}T12:00:00Z`);
    if (!Number.isFinite(calendar.getTime()) || calendar.getUTCFullYear() !== year || calendar.getUTCMonth() !== month - 1 || calendar.getUTCDate() !== day) return null;
    const parsed = new Date(value);
    return Number.isFinite(parsed.getTime()) ? parsed : null;
  }

  function formatDeadline(value, kind, timeZone = DEFAULT_TIMEZONE) {
    const date = parseDate(value);
    if (!date) return 'Not published';
    const options = { month: 'short', day: 'numeric', year: 'numeric' };
    if (kind === 'date' || value.length === 10) {
      // A calendar deadline is not a UTC midnight instant. Never shift its day.
      return new Intl.DateTimeFormat('en-US', { ...options, timeZone: 'UTC' }).format(new Date(`${value.slice(0, 10)}T12:00:00Z`));
    }
    if (!/(?:Z|[+-]\d{2}:?\d{2})$/i.test(value)) {
      const unzoned = new Date(`${value.replace(' ', 'T')}Z`);
      if (!Number.isFinite(unzoned.getTime())) return 'Not published';
      return `${new Intl.DateTimeFormat('en-US', { ...options, hour: 'numeric', minute: '2-digit', timeZone: 'UTC' }).format(unzoned)} (time zone not supplied)`;
    }
    try {
      return new Intl.DateTimeFormat('en-US', { ...options, hour: 'numeric', minute: '2-digit', timeZone, timeZoneName: 'short' }).format(date);
    } catch (_) {
      return new Intl.DateTimeFormat('en-US', { ...options, hour: 'numeric', minute: '2-digit', timeZone: DEFAULT_TIMEZONE, timeZoneName: 'short' }).format(date);
    }
  }

  function sourcePresentation(status) {
    const states = {
      ok: { label: 'Working', tone: 'ok' },
      partial: { label: 'Limited', tone: 'warning' },
      blocked: { label: 'Restricted', tone: 'warning' },
      error: { label: 'Unavailable', tone: 'error' },
      disabled: { label: 'Disabled', tone: 'pending' },
      pending: { label: 'Not checked', tone: 'pending' }
    };
    return Object.hasOwn(states, status) ? states[status] : { label: 'Unknown', tone: 'unknown' };
  }

  function validJob(job) {
    return job && typeof job === 'object' && ((typeof job.id === 'string' && job.id.length > 0) || (typeof job.id === 'number' && Number.isFinite(job.id)));
  }

  function normalizeJobList(data) {
    if (!data || !Array.isArray(data.items) || !data.items.every(validJob) || !Number.isInteger(data.total) || data.total < 0 || !Number.isInteger(data.page) || data.page < 1 || !Number.isInteger(data.page_size) || data.page_size < 1 || !Number.isInteger(data.pages) || data.pages < 0 || data.items.length > data.page_size || data.items.length > data.total) {
      throw new Error('The server returned an invalid job list. Please retry.');
    }
    return { items: data.items, total: data.total, page: data.page, pageSize: data.page_size, pages: data.pages };
  }

  // --- Helpers for the profile, sources, discovery and settings views ----------------------

  const VIEWS = Object.freeze(['feed', 'saved', 'applied', 'dismissed', 'sources', 'profile', 'discovery', 'settings']);
  const LEVEL_ORDER = Object.freeze(Object.keys(LEVELS));
  const MAX_RESUME_BYTES = 8 * 1024 * 1024;
  const MAX_BOARD_URLS = 10;
  const MAX_COLLECT_TIMES = 12;
  const MAX_PROFILE_RECORDS = 10;
  const PROFILE_LIST_CAPS = Object.freeze({ skills: 80, interests: 25, preferred_locations: 20, exclude_keywords: 40, links: 10 });
  const EDUCATION_KEYS = Object.freeze(['school', 'degree', 'field', 'graduation']);
  const EXPERIENCE_KEYS = Object.freeze(['title', 'company', 'start', 'end']);
  const ORIGIN_LABELS = Object.freeze({ builtin: 'Built-in', discovered: 'Discovered', user: 'Added by you' });

  function wholeNumber(value) { return Number.isInteger(value) && value >= 0 ? value : 0; }

  // One trimmed line of text; anything that is not a string becomes ''.
  function plainText(value, cap = 200) {
    if (typeof value !== 'string') return '';
    return value.replace(/[\u0000-\u001f\u007f]/g, ' ').replace(/\s+/g, ' ').trim().slice(0, cap).trim();
  }

  function stringList(value, cap = 50, itemCap = 300) {
    return Array.isArray(value) ? value.map((item) => plainText(item, itemCap)).filter(Boolean).slice(0, cap) : [];
  }

  function pluralWord(value, singular, pluralForm = `${singular}s`) {
    return `${numberFormat.format(value)} ${value === 1 ? singular : pluralForm}`;
  }

  // 24-hour "HH:MM" (browser time inputs may add seconds); anything else is null.
  function normalizeTime(value) {
    if (typeof value !== 'string') return null;
    const match = value.trim().match(/^(\d{1,2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?$/);
    if (!match) return null;
    const hour = Number(match[1]);
    const minute = Number(match[2]);
    if (hour > 23 || minute > 59 || (match[3] !== undefined && Number(match[3]) > 59)) return null;
    return `${String(hour).padStart(2, '0')}:${match[2]}`;
  }

  function parseTimeList(value, max = MAX_COLLECT_TIMES) {
    const parts = Array.isArray(value) ? value : (typeof value === 'string' ? value.split(/[\s,;]+/) : []);
    const times = new Set();
    const invalid = [];
    for (const part of parts) {
      if (part === null || part === undefined) continue;
      const text = String(part).trim();
      if (!text) continue;
      const time = normalizeTime(text);
      if (time) times.add(time);
      else invalid.push(text);
    }
    const sorted = [...times].sort();
    return { times: sorted.slice(0, max), invalid, tooMany: sorted.length > max };
  }

  // A pasted address becomes an absolute public http(s) URL ("jobs.lever.co/x" gains https://).
  function normalizeHttpUrl(value) {
    if (typeof value !== 'string') return null;
    let text = value.trim().replace(/^<([^<>]*)>$/, '$1').replace(/^["']+|["']+$/g, '').trim().replace(/[.,;]+$/, '');
    if (!text || /\s/.test(text) || text.startsWith('//')) return null;
    if (!/^[a-z][a-z0-9+.-]*:/i.test(text)) text = `https://${text}`;
    if (!/^https?:\/\//i.test(text)) return null;
    return safeHttpUrl(text);
  }

  function splitUrlList(text, max = MAX_BOARD_URLS) {
    const result = { urls: [], invalid: [], extra: 0 };
    if (typeof text !== 'string') return result;
    const seen = new Set();
    for (const token of text.split(/[\s,]+/)) {
      const raw = token.trim();
      if (!raw) continue;
      const url = normalizeHttpUrl(raw);
      if (!url) { result.invalid.push(raw); continue; }
      const key = url.replace(/^https?:\/\//i, '').replace(/\/+$/, '').toLowerCase();
      if (seen.has(key)) continue;
      seen.add(key);
      if (result.urls.length >= max) result.extra += 1;
      else result.urls.push(url);
    }
    return result;
  }

  // Chip lists: trimmed, single-spaced, case-insensitively unique, capped.
  function normalizeChips(values, { max = 80, maxLength = 200 } = {}) {
    if (!Array.isArray(values)) return [];
    const seen = new Set();
    const out = [];
    for (const value of values) {
      const text = plainText(value, maxLength);
      const key = text.toLowerCase();
      if (!text || seen.has(key)) continue;
      seen.add(key);
      out.push(text);
      if (out.length >= max) break;
    }
    return out;
  }

  function splitChipInput(text, commas = true) {
    if (typeof text !== 'string') return [];
    return text.split(commas ? /[,;\n]+/ : /[;\n]+/).map((part) => part.trim()).filter(Boolean);
  }

  // Keys are write-only: the UI only ever repeats the server's short hint.
  function keyHint(secret, provider) {
    const spec = provider && typeof provider === 'object' ? provider : {};
    const optional = spec.auth === 'optional_key' || spec.id === 'openai-compatible';
    if (!secret || typeof secret !== 'object' || secret.set !== true) return optional ? 'Not set (optional for this provider)' : 'Not set';
    if (secret.source === 'env') return typeof spec.key_env === 'string' && /^[A-Za-z0-9_]+$/.test(spec.key_env) ? `From environment variable ${spec.key_env}` : 'From an environment variable';
    const hint = typeof secret.hint === 'string' ? secret.hint.trim() : '';
    const tail = hint.replace(/^…/, '');
    if (!hint || hint === 'set' || !/^\S{1,8}$/.test(tail)) return 'Key saved';
    return `Key saved (…${tail})`;
  }

  function formatDuration(seconds) {
    if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0) return '';
    if (seconds < 10) return `${Math.round(seconds * 10) / 10} s`;
    const total = Math.round(seconds);
    if (total < 60) return `${total} s`;
    if (total < 3600) {
      const minutes = Math.floor(total / 60);
      const rest = total % 60;
      return rest ? `${minutes} min ${rest} s` : `${minutes} min`;
    }
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    return minutes ? `${hours} h ${minutes} min` : `${hours} h`;
  }

  function statusCounts(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return [];
    return Object.entries(value)
      .filter(([key, total]) => key && key !== 'null' && key !== 'undefined' && Number.isInteger(total) && total > 0)
      .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  }

  function summarizeDiscoveryRun(run) {
    if (!run || typeof run !== 'object') return null;
    const finishedAt = typeof run.finished_at === 'string' && run.finished_at ? run.finished_at : null;
    if (run.skipped === true) {
      const reason = plainText(run.reason, 300) || 'another discovery run was in progress.';
      return { skipped: true, ok: run.ok !== false, headline: `Skipped: ${reason}`, added: [], addedCount: 0, dropped: [], byStatus: [], notProbed: 0, finishedAt, discoveredTotal: null };
    }
    const probed = wholeNumber(run.probed);
    const added = Array.isArray(run.added) ? run.added
      .filter((item) => item && typeof item === 'object' && plainText(item.name))
      .map((item) => ({ name: plainText(item.name), type: typeof item.type === 'string' ? item.type : '', canadaTech: wholeNumber(item.canada_tech), matchedNow: wholeNumber(item.matched_now) })) : [];
    const dropped = stringList(run.dropped, 500);
    const addedCount = Number.isInteger(run.added_count) && run.added_count >= 0 ? run.added_count : added.length;
    const duration = formatDuration(run.duration_seconds);
    const headline = `Checked ${pluralWord(probed, 'candidate')}${duration ? ` in ${duration}` : ''}: ${numberFormat.format(addedCount)} added, ${numberFormat.format(dropped.length)} dropped.`;
    return { skipped: false, ok: run.ok !== false, headline, added, addedCount, dropped, byStatus: statusCounts(run.by_status), notProbed: wholeNumber(run.not_probed_time_budget), finishedAt, discoveredTotal: Number.isInteger(run.discovered_total) ? run.discovered_total : null };
  }

  function summarizeScoutRun(run) {
    if (!run || typeof run !== 'object') return null;
    const themes = stringList(run.themes, 12);
    const errors = stringList(run.errors, 12);
    const leads = wholeNumber(run.lead_count);
    const suggestions = wholeNumber(run.suggestions);
    return {
      headline: `${pluralWord(themes.length, 'theme')}: ${pluralWord(suggestions, 'suggestion')}, ${pluralWord(leads, 'board lead')} to verify.`,
      themes, errors, leads, suggestions, finishedAt: typeof run.finished_at === 'string' && run.finished_at ? run.finished_at : null
    };
  }

  // Owner avatar initials: first and last word of the profile name only.
  function initials(name) {
    if (typeof name !== 'string') return '';
    const words = name.split(/\s+/).map((word) => word.replace(/[^\p{L}\p{N}]/gu, '')).filter(Boolean);
    if (!words.length) return '';
    const first = [...words[0]][0];
    const last = words.length > 1 ? [...words[words.length - 1]][0] : '';
    return `${first}${last}`.toLocaleUpperCase('en-CA');
  }

  function validateResumeFile(file) {
    if (!file || typeof file !== 'object') return { ok: false, message: 'Choose your resume as a PDF file.' };
    const name = typeof file.name === 'string' ? file.name : '';
    const type = typeof file.type === 'string' ? file.type.trim().toLowerCase() : '';
    const pdfType = type === 'application/pdf' || type === 'application/x-pdf';
    if (type ? !pdfType : !/\.pdf$/i.test(name.trim())) return { ok: false, message: `“${plainText(name, 80) || 'This file'}” is not a PDF. Export or print your resume to PDF and try again.` };
    if (typeof file.size !== 'number' || !Number.isFinite(file.size) || file.size <= 0) return { ok: false, message: 'This file is empty. Choose your resume PDF.' };
    if (file.size > MAX_RESUME_BYTES) return { ok: false, message: 'This PDF is larger than 8 MB. Save a smaller copy (for example without images) and try again.' };
    return { ok: true, message: '' };
  }

  // "%PDF-" magic bytes, allowing a byte-order mark or leading whitespace.
  function looksLikePdf(bytes) {
    if (!bytes || typeof bytes.length !== 'number') return false;
    let index = bytes[0] === 0xef && bytes[1] === 0xbb && bytes[2] === 0xbf ? 3 : 0;
    while (index < bytes.length && index < 1024 && [0, 9, 10, 12, 13, 32].includes(bytes[index])) index += 1;
    return [0x25, 0x50, 0x44, 0x46, 0x2d].every((code, offset) => bytes[index + offset] === code);
  }

  function resumeFilename(value) {
    let name = String(value || '').split(/[\\/]/).pop() || '';
    name = name.replace(/[\u0000-\u001f\u007f]/g, '').replace(/\s+/g, ' ').replace(/^[ .]+|[ .]+$/g, '');
    if (name.length > 120) {
      const dot = name.lastIndexOf('.');
      const ext = dot > 0 ? name.slice(dot + 1) : '';
      name = ext && ext.length <= 8 ? `${name.slice(0, 119 - ext.length)}.${ext}` : name.slice(0, 120);
    }
    return name || 'resume.pdf';
  }

  function sourceOriginLabel(origin) {
    return Object.hasOwn(ORIGIN_LABELS, origin) ? ORIGIN_LABELS[origin] : 'Other';
  }

  // Enabled sources whose last check was not clean. Disabled and unchecked ones are not alarms.
  function sourceNeedsAttention(source) {
    return Boolean(source) && !['ok', 'pending', 'disabled'].includes(source.status);
  }

  function filterSources(items, filters = {}) {
    if (!Array.isArray(items)) return [];
    const health = typeof filters.health === 'string' && filters.health ? filters.health : 'all';
    const origin = typeof filters.origin === 'string' ? filters.origin : '';
    const type = typeof filters.type === 'string' ? filters.type : '';
    const terms = typeof filters.text === 'string' ? filters.text.toLowerCase().split(/\s+/).filter(Boolean) : [];
    return items.filter((source) => {
      if (!source || typeof source !== 'object') return false;
      if (health !== 'all' && (health === 'attention' ? !sourceNeedsAttention(source) : source.status !== health)) return false;
      if (origin && source.origin !== origin) return false;
      if (type && source.type !== type) return false;
      if (terms.length) {
        const haystack = [source.name, source.url, source.id, source.keywords, source.type].filter((part) => typeof part === 'string').join(' ').toLowerCase();
        if (!terms.every((term) => haystack.includes(term))) return false;
      }
      return true;
    });
  }

  // FastAPI errors: {"detail": "text"}, or a validation list [{loc, msg}].
  function errorDetail(data, status) {
    const fallback = `The request failed (${status}).`;
    if (!data || typeof data !== 'object') return fallback;
    const { detail } = data;
    if (typeof detail === 'string' && detail.trim()) return detail.trim().slice(0, 400);
    if (Array.isArray(detail)) {
      const parts = detail.map((item) => {
        if (typeof item === 'string') return item;
        if (!item || typeof item !== 'object' || typeof item.msg !== 'string') return '';
        const loc = Array.isArray(item.loc) ? item.loc.filter((part, index) => !(index === 0 && ['body', 'query', 'path'].includes(part))).map(String) : [];
        return loc.length ? `${loc.join(' → ')}: ${item.msg}` : item.msg;
      }).filter(Boolean);
      if (parts.length) return parts.slice(0, 3).join('; ').slice(0, 400);
    }
    if (detail && typeof detail === 'object' && typeof detail.message === 'string' && detail.message.trim()) return detail.message.trim().slice(0, 400);
    if (typeof data.message === 'string' && data.message.trim()) return data.message.trim().slice(0, 400);
    return fallback;
  }

  function cleanRecords(value, keys) {
    if (!Array.isArray(value)) return [];
    const out = [];
    for (const item of value) {
      if (!item || typeof item !== 'object' || Array.isArray(item)) continue;
      const record = Object.fromEntries(keys.map((key) => [key, plainText(item[key])]));
      if (keys.some((key) => record[key])) out.push(record);
      if (out.length >= MAX_PROFILE_RECORDS) break;
    }
    return out;
  }

  // The PUT /api/profile body: only known fields, cleaned, nothing invented.
  function cleanProfile(profile) {
    const data = profile && typeof profile === 'object' ? profile : {};
    const wanted = new Set(Array.isArray(data.levels) ? data.levels : []);
    const links = (Array.isArray(data.links) ? data.links : []).map((link) => normalizeHttpUrl(link)).filter(Boolean);
    return {
      name: plainText(data.name), email: plainText(data.email), location: plainText(data.location),
      headline: plainText(data.headline, 300), graduation: plainText(data.graduation),
      education: cleanRecords(data.education, EDUCATION_KEYS), experience: cleanRecords(data.experience, EXPERIENCE_KEYS),
      skills: normalizeChips(data.skills, { max: PROFILE_LIST_CAPS.skills }),
      interests: normalizeChips(data.interests, { max: PROFILE_LIST_CAPS.interests }),
      preferred_locations: normalizeChips(data.preferred_locations, { max: PROFILE_LIST_CAPS.preferred_locations }),
      exclude_keywords: normalizeChips(data.exclude_keywords, { max: PROFILE_LIST_CAPS.exclude_keywords }),
      links: normalizeChips(links, { max: PROFILE_LIST_CAPS.links, maxLength: 300 }),
      levels: LEVEL_ORDER.filter((level) => wanted.has(level))
    };
  }

  const CANDIDATE_STATES = Object.freeze({
    verified: { label: 'Verified', tone: 'ok', canAdd: true, text: 'Lists current Canadian technical roles. Ready to add.' },
    no_canada_tech: { label: 'No Canadian tech roles now', tone: 'warning', canAdd: true, text: 'The board works, but it has no current Canadian technical postings. You can still add it; it is checked on every collection.' },
    error: { label: 'Check failed', tone: 'error', canAdd: true, text: 'The board could not be read just now. It may be temporarily unavailable. You can add it anyway; it is retried on every collection.' },
    not_found: { label: 'Not found', tone: 'error', canAdd: false, text: 'No public job board was found at this address. Compare the company name in the URL with the employer’s careers page.' },
    excluded: { label: 'Excluded', tone: 'pending', canAdd: false, text: 'This looks like a job aggregator, a crowd-work marketplace or a test board, so Career Radar does not monitor it.' },
    already_monitored: { label: 'Already monitored', tone: 'pending', canAdd: false, text: 'This board is already one of your sources.' }
  });

  function candidateStatus(status) {
    if (Object.hasOwn(CANDIDATE_STATES, status)) return { ...CANDIDATE_STATES[status] };
    const text = typeof status === 'string' ? status.replace(/[_-]+/g, ' ').trim() : '';
    return { label: text ? text.charAt(0).toUpperCase() + text.slice(1) : 'Unknown', tone: 'unknown', canAdd: false, text: '' };
  }

  function scheduleJobState(job) {
    if (!job || typeof job !== 'object') return { tone: 'unknown', label: 'Status unavailable' };
    if (job.running === true) return { tone: 'running', label: 'Running now' };
    if (!job.last_run) return { tone: 'pending', label: 'Not run yet' };
    if (job.last_ok === true) return { tone: 'ok', label: 'Last run OK' };
    if (job.last_ok === false) return { tone: 'error', label: 'Last run failed' };
    return { tone: 'unknown', label: 'Last run finished' };
  }

  function parseIntInRange(value, min, max) {
    const text = typeof value === 'number' ? String(value) : (typeof value === 'string' ? value.trim() : '');
    if (!/^\d+$/.test(text)) return null;
    const number = Number(text);
    return Number.isSafeInteger(number) && number >= min && number <= max ? number : null;
  }

  function summarizeExtracted(extracted) {
    if (!extracted || typeof extracted !== 'object') return '';
    const parts = [];
    for (const [key, text] of [['name', 'name'], ['email', 'email'], ['location', 'location'], ['headline', 'headline'], ['graduation', 'graduation date']]) {
      if (plainText(extracted[key])) parts.push(text);
    }
    for (const [key, singular, pluralForm] of [['skills', 'skill', 'skills'], ['interests', 'interest', 'interests'], ['education', 'education entry', 'education entries'], ['experience', 'experience entry', 'experience entries'], ['links', 'link', 'links']]) {
      const size = Array.isArray(extracted[key]) ? extracted[key].length : 0;
      if (size) parts.push(pluralWord(size, singular, pluralForm));
    }
    return parts.join(', ');
  }

  // PDF metadata dates ("D:20240105…") or ISO dates -> "YYYY-MM-DD"; impossible dates -> null.
  function pdfDate(value) {
    if (typeof value !== 'string') return null;
    const text = value.trim();
    let match = text.match(/^D:(\d{4})(\d{2})?(\d{2})?/);
    if (!match) match = text.match(/^(\d{4})-(\d{2})-(\d{2})(?:$|[T ])/);
    if (!match) return null;
    const [, year, month = '01', day = '01'] = match;
    const date = new Date(Date.UTC(Number(year), Number(month) - 1, Number(day)));
    if (date.getUTCFullYear() !== Number(year) || date.getUTCMonth() !== Number(month) - 1 || date.getUTCDate() !== Number(day)) return null;
    return `${year}-${month}-${day}`;
  }

  function viewFromQuery(search) {
    let view = null;
    try { view = new URLSearchParams(typeof search === 'string' ? search : '').get('view'); } catch (_) { view = null; }
    return VIEWS.includes(view) ? view : 'feed';
  }

  const helpers = {
    safeHttpUrl, buildQuery, formatDeadline, sourcePresentation, normalizeJobList,
    normalizeTime, parseTimeList, normalizeHttpUrl, splitUrlList, normalizeChips, splitChipInput,
    keyHint, formatDuration, summarizeDiscoveryRun, summarizeScoutRun, initials, validateResumeFile,
    looksLikePdf, resumeFilename, sourceOriginLabel, filterSources, errorDetail, cleanProfile,
    candidateStatus, scheduleJobState, parseIntInRange, summarizeExtracted, pdfDate, viewFromQuery
  };

  // The same small helpers are exercised by node --test career_radar/static/ui.test.cjs.
  if (typeof module !== 'undefined' && module.exports) module.exports = helpers;
  if (typeof document === 'undefined') return;
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', startApp, { once: true });
  else startApp();

  function startApp() {
    const $ = (id) => document.getElementById(id);
    const JOB_VIEWS = new Set(['feed', 'saved', 'applied', 'dismissed']);
    const VIEW_SECTIONS = Object.freeze({ sources: 'sources-view', profile: 'profile-view', discovery: 'discovery-view', settings: 'settings-view' });
    const VIEW_HEADINGS = Object.freeze({ sources: 'sources-heading', profile: 'profile-heading', discovery: 'discovery-heading', settings: 'settings-heading' });
    const SOURCE_PAGE_SIZE = 100;
    const state = {
      view: 'feed', page: 1, pageSize: 30, pages: 0, total: null, items: [],
      filters: { q: '', level: '', category: '', location: '', status: '', sort: 'priority', priorityOnly: false, includeClosed: false, deadlineDays: '' },
      stats: null, statsError: '', sources: null, sourcesRefresh: null, sourceTypes: [], sourceLimit: SOURCE_PAGE_SIZE, sourcesShown: 0, sourcesFiltered: 0,
      collectionRunning: false, refreshPending: false, exportPending: false,
      jobsSequence: 0, statsSequence: 0, sourcesSequence: 0, detailSequence: 0, profileSequence: 0, discoverySequence: 0, settingsSequence: 0,
      jobsController: null, statsController: null, sourcesController: null, detailController: null,
      listSignature: '', categoriesSignature: '', locationsSignature: '', sourcesSignature: '', sourceTypesSignature: '',
      detailId: null, currentJob: null, detailOpener: null,
      pendingJobs: new Set(), pendingSources: new Set(), pollTimer: null, pollPromise: null, pollCount: 0,
      searchTimer: null, sourceSearchTimer: null, toastTimer: null,
      candidates: [], checkingBoards: false, addingSearch: false,
      profile: null, resume: null, lastUpload: null, profileLoaded: false, profileDirty: false, profileBusy: false, uploading: false, chips: {}, recordCounter: 0,
      discovery: null, discoveryTimer: null, discoveryBusy: false,
      settings: null, llmDirty: false, scheduleDirty: false, llmBusy: false, modelsBusy: false, modelsProvider: null, scheduleBusy: false,
      codexLogin: null, codexTimer: null, codexBusy: false, timeCounter: 0,
      confirmResolve: null, confirmOpener: null
    };
    const viewCopy = {
      feed: { title: 'Opportunities', subtitle: 'Find your next move, without the noise.', result: 'All opportunities' },
      saved: { title: 'Saved roles', subtitle: 'A shortlist worth coming back to.', result: 'Your shortlist' },
      applied: { title: 'Applied', subtitle: 'The next step is in motion. Keep your progress in view.', result: 'Your applications' },
      dismissed: { title: 'Dismissed', subtitle: 'Not this time. You can restore a role whenever you like.', result: 'Dismissed roles' },
      sources: { title: 'Sources', subtitle: 'Know what is working, what is limited, and add the boards you care about.' },
      profile: { title: 'Profile', subtitle: 'Tell your radar what you are looking for. Ranking follows your profile.' },
      discovery: { title: 'Discovery', subtitle: 'New employer boards, verified before they join your radar.' },
      settings: { title: 'Settings', subtitle: 'Language model, sign-in and the automatic schedule.' }
    };

    // --- Shared DOM, formatting and request helpers ------------------------------------------

    function element(tag, className, text) {
      const node = document.createElement(tag);
      if (className) node.className = className;
      if (text !== undefined && text !== null) node.textContent = String(text);
      return node;
    }

    function icon(name) {
      const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      svg.classList.add('icon');
      svg.setAttribute('aria-hidden', 'true');
      const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
      use.setAttribute('href', `#i-${name}`);
      svg.append(use);
      return svg;
    }

    function displayText(value, fallback = '') {
      return typeof value === 'string' && value.trim() ? value : fallback;
    }

    function label(value) {
      const text = displayText(value).replace(/[_-]+/g, ' ');
      return text ? text.charAt(0).toUpperCase() + text.slice(1) : '';
    }

    function strings(value) {
      return Array.isArray(value) ? [...new Set(value.filter((part) => typeof part === 'string' && part.trim()))] : [];
    }

    function count(value) {
      return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? numberFormat.format(value) : '—';
    }

    const compactFormat = new Intl.NumberFormat('en-CA', { notation: 'compact', maximumFractionDigits: 1 });
    function navCount(value) {
      return typeof value === 'number' && Number.isFinite(value) && value >= 10000 ? compactFormat.format(value) : count(value);
    }

    function plural(value, word) { return `${count(value)} ${word}${value === 1 ? '' : 's'}`; }
    function timezone() { return displayText(state.stats && state.stats.timezone, displayText(state.settings && state.settings.timezone, DEFAULT_TIMEZONE)); }
    function dateLabel(value, fallback = 'Not published') {
      const date = parseDate(value);
      if (!date) return fallback;
      if (value.length === 10) return formatDeadline(value, 'date', timezone());
      return new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', year: 'numeric', timeZone: timezone() }).format(date);
    }
    function timestamp(value, fallback = 'Not recorded') { return parseDate(value) ? formatDeadline(value, value.length === 10 ? 'date' : 'datetime', timezone()) : fallback; }

    function relativeTime(value) {
      const date = parseDate(value);
      if (!date) return 'not recorded';
      const diff = Date.now() - date.getTime();
      if (diff < 0) return timestamp(value);
      if (diff < 60_000) return 'just now';
      if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m ago`;
      if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h ago`;
      if (diff < 604_800_000) return `${Math.floor(diff / 86_400_000)}d ago`;
      return dateLabel(value, 'not recorded');
    }

    function monogram(company) {
      const words = displayText(company).trim().split(/\s+/).filter(Boolean);
      if (!words.length) return '—';
      return (words.length > 1 ? [...words[0]][0] + [...words[1]][0] : [...words[0]].slice(0, 2).join('')).toUpperCase();
    }

    function externalLink(text, value, className = '') {
      const url = safeHttpUrl(value);
      if (!url) return element('span', className, text);
      const link = element('a', className, text);
      link.href = url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.referrerPolicy = 'no-referrer';
      link.append(icon('external'));
      return link;
    }

    function errorText(error) {
      return error && typeof error.message === 'string' ? error.message.slice(0, 350) : 'Something went wrong. Please try again.';
    }

    function isJobView(view = state.view) { return JOB_VIEWS.has(view); }

    function setStatus(id, text, isError = false) {
      const node = $(id);
      node.textContent = text;
      node.classList.toggle('is-error', Boolean(isError && text));
    }

    // Busy buttons keep focus (aria-disabled instead of disabled); handlers check state flags.
    function setBusy(button, busy, busyText) {
      const target = button.querySelector('span') || button;
      if (busy) {
        if (busyText && button.dataset.idleText === undefined) {
          button.dataset.idleText = target.textContent;
          target.textContent = busyText;
        }
        button.setAttribute('aria-disabled', 'true');
        button.setAttribute('aria-busy', 'true');
      } else {
        if (button.dataset.idleText !== undefined) {
          target.textContent = button.dataset.idleText;
          delete button.dataset.idleText;
        }
        button.removeAttribute('aria-disabled');
        button.removeAttribute('aria-busy');
      }
    }

    function factList(target, rows) {
      target.replaceChildren(...rows.map(([term, value]) => {
        const row = element('div');
        row.append(element('dt', '', term), element('dd', '', value));
        return row;
      }));
    }

    function countBadge(text, total, className) {
      const badge = element('span', `badge ${className}`, text);
      badge.append(element('strong', '', total));
      return badge;
    }

    function requestError(message, status = 0) {
      const error = new Error(message);
      error.status = status;
      return error;
    }

    async function request(path, options = {}) {
      const controller = new AbortController();
      let timedOut = false;
      const abort = () => controller.abort();
      if (options.signal) {
        if (options.signal.aborted) controller.abort();
        options.signal.addEventListener('abort', abort, { once: true });
      }
      const timeout = window.setTimeout(() => { timedOut = true; controller.abort(); }, options.timeout || 25_000);
      const method = options.method || 'GET';
      try {
        const headers = { Accept: 'application/json' };
        // Every write is same-origin JSON (even an empty DELETE); the server rejects anything else.
        if (method !== 'GET') headers['Content-Type'] = 'application/json';
        const response = await fetch(path, {
          method, headers, credentials: 'same-origin', cache: 'no-store',
          body: options.body !== undefined ? JSON.stringify(options.body) : undefined, signal: controller.signal
        });
        let data;
        try { data = await response.json(); }
        catch (_) { throw requestError(response.ok ? 'The server did not return valid data. Please retry.' : `The request failed (${response.status}). Please retry.`, response.status); }
        if (!response.ok) throw requestError(errorDetail(data, response.status), response.status);
        return data;
      } catch (error) {
        if (timedOut) throw requestError('The server took too long to respond. Please retry.');
        if (error instanceof TypeError) throw requestError('Cannot reach Career Radar. Check the connection and try again.');
        throw error;
      } finally {
        window.clearTimeout(timeout);
        if (options.signal) options.signal.removeEventListener('abort', abort);
      }
    }

    // The resume is the one non-JSON write: the raw PDF, with upload progress.
    function uploadPdf(path, file, onProgress) {
      return new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open('POST', path);
        xhr.timeout = 240_000;
        xhr.setRequestHeader('Content-Type', 'application/pdf');
        xhr.setRequestHeader('Accept', 'application/json');
        xhr.upload.addEventListener('progress', (event) => { if (event.lengthComputable && event.total > 0) onProgress(event.loaded / event.total, false); });
        xhr.upload.addEventListener('load', () => onProgress(1, true));
        xhr.addEventListener('load', () => {
          let data = null;
          try { data = JSON.parse(xhr.responseText); } catch (_) { data = null; }
          if (xhr.status >= 200 && xhr.status < 300) {
            if (data && typeof data === 'object') resolve(data);
            else reject(requestError('The server did not return valid data. Please retry.', xhr.status));
          } else reject(requestError(errorDetail(data, xhr.status), xhr.status));
        });
        xhr.addEventListener('error', () => reject(requestError('Cannot reach Career Radar. Check the connection and try again.')));
        xhr.addEventListener('timeout', () => reject(requestError('Reading the resume took too long. Please retry, or choose “Don’t send my resume to an LLM”.')));
        xhr.addEventListener('abort', () => reject(requestError('The upload was cancelled.')));
        xhr.send(file);
      });
    }

    function announce(message) { $('announcer').textContent = message; }

    function toast(message, isError = false) {
      const node = $('toast');
      const destination = $('job-dialog').open ? $('job-dialog') : document.body;
      if (node.parentElement !== destination) destination.append(node);
      window.clearTimeout(state.toastTimer);
      node.textContent = message;
      node.classList.toggle('is-error', isError);
      node.setAttribute('role', isError ? 'alert' : 'status');
      node.hidden = false;
      state.toastTimer = window.setTimeout(() => { node.hidden = true; }, isError ? 10_000 : 5_000);
    }

    // An accessible confirmation dialog; resolves true only for the confirm button.
    function confirmAction({ title, message, confirm = 'Confirm', danger = false }) {
      const dialog = $('confirm-dialog');
      if (state.confirmResolve) settleConfirm(false);
      if (typeof dialog.showModal !== 'function') return Promise.resolve(window.confirm(`${title}\n\n${message}`));
      $('confirm-title').textContent = title;
      $('confirm-message').textContent = message;
      $('confirm-ok').textContent = confirm;
      $('confirm-ok').classList.toggle('button-danger', danger);
      state.confirmOpener = document.activeElement;
      return new Promise((resolve) => {
        state.confirmResolve = resolve;
        dialog.showModal();
        $('confirm-cancel').focus();
      });
    }

    function settleConfirm(value) {
      const resolve = state.confirmResolve;
      const opener = state.confirmOpener;
      state.confirmResolve = null;
      state.confirmOpener = null;
      if ($('confirm-dialog').open) $('confirm-dialog').close();
      if (opener && opener.isConnected && opener !== document.body && typeof opener.focus === 'function') opener.focus({ preventScroll: true });
      if (resolve) resolve(value);
    }

    // --- Views ---------------------------------------------------------------------------------

    function activeFilters() {
      const f = state.filters;
      return Boolean(f.q || f.level || f.category || f.location || f.priorityOnly || f.deadlineDays || f.includeClosed);
    }

    function readFilters() {
      const selectedLevel = document.querySelector('input[name="level"]:checked');
      state.filters = {
        q: $('search-input').value.trim(), level: selectedLevel ? selectedLevel.value : '',
        category: $('category-filter').value, location: $('location-filter').value.trim(),
        status: ['saved', 'applied', 'dismissed'].includes(state.view) ? state.view : '',
        sort: $('sort-filter').value, priorityOnly: $('priority-filter').checked && !$('priority-filter').disabled,
        includeClosed: $('include-closed').checked, deadlineDays: $('deadline-filter').value
      };
      $('reset-filters').hidden = !activeFilters() && state.filters.sort === 'priority';
    }

    function applyFilters() {
      window.clearTimeout(state.searchTimer);
      readFilters();
      state.page = 1;
      void loadJobs();
    }

    function clearFilters() {
      $('search-input').value = '';
      document.querySelector('input[name="level"][value=""]').checked = true;
      $('category-filter').value = '';
      $('location-filter').value = '';
      $('deadline-filter').value = '';
      $('priority-filter').checked = false;
      $('include-closed').checked = false;
      $('sort-filter').value = 'priority';
      applyFilters();
    }

    function setViewUrl(view, push) {
      const url = new URL(window.location.href);
      if (view === 'feed') url.searchParams.delete('view');
      else url.searchParams.set('view', view);
      const next = `${url.pathname}${url.search}${url.hash}`;
      if (next === `${window.location.pathname}${window.location.search}${window.location.hash}`) return;
      if (push) window.history.pushState(null, '', next);
      else window.history.replaceState(null, '', next);
    }

    function changeView(view, { push = true, focus = false, initial = false } = {}) {
      if (!Object.hasOwn(viewCopy, view)) return;
      const previous = state.view;
      state.view = view;
      state.page = 1;
      const copy = viewCopy[view];
      $('page-title').replaceChildren(document.createTextNode(copy.title), element('span', 'title-dot', '.'));
      $('page-subtitle').textContent = copy.subtitle;
      if (!(initial && view === 'feed')) document.title = `${copy.title} — Career Radar`;
      document.querySelectorAll('.nav-item[data-view]').forEach((button) => {
        const active = button.dataset.view === view;
        button.classList.toggle('is-active', active);
        if (active) button.setAttribute('aria-current', 'page');
        else button.removeAttribute('aria-current');
      });
      const jobs = isJobView(view);
      $('jobs-view').hidden = !jobs;
      for (const [name, id] of Object.entries(VIEW_SECTIONS)) $(id).hidden = name !== view;
      $('summary-strip').hidden = !(jobs || view === 'sources');
      setViewUrl(view, push && !initial);
      if (!jobs && state.jobsController) state.jobsController.abort();
      if (jobs) {
        $('results-heading').textContent = copy.result;
        readFilters();
        void loadJobs();
      } else if (view === 'sources') void loadSources({ quiet: Boolean(state.sources) });
      else if (view === 'profile') void loadProfile();
      else if (view === 'discovery') void loadDiscovery();
      else if (view === 'settings') {
        if (state.settings) applySettings(state.settings);
        void loadSettings();
      }
      if (focus) (jobs ? $('results-heading') : $(VIEW_HEADINGS[view])).focus();
      else if (!initial && !jobs && previous !== view) announce(`${copy.title}. ${copy.subtitle}`);
    }

    // --- Jobs feed -----------------------------------------------------------------------------

    async function loadJobs({ quiet = false } = {}) {
      if (!isJobView()) return;
      const sequence = ++state.jobsSequence;
      if (state.jobsController) state.jobsController.abort();
      state.jobsController = new AbortController();
      $('job-list').setAttribute('aria-busy', 'true');
      if (!quiet) {
        $('jobs-loading').hidden = false;
        $('job-list').hidden = true;
        $('jobs-empty').hidden = true;
        $('jobs-error').hidden = true;
        $('pagination').hidden = true;
        $('result-count').textContent = 'Loading…';
      }
      try {
        const data = normalizeJobList(await request(`/api/jobs?${buildQuery(state.filters, state.page)}`, { signal: state.jobsController.signal }));
        if (sequence !== state.jobsSequence || !isJobView()) return;
        if (data.pages > 0 && state.page > data.pages) {
          state.page = data.pages;
          return loadJobs({ quiet });
        }
        state.items = data.items;
        state.total = data.total;
        state.page = data.page;
        state.pageSize = data.pageSize;
        state.pages = data.pages;
        const signature = JSON.stringify(data);
        if (state.listSignature !== signature) {
          renderJobs();
          state.listSignature = signature;
        }
        $('job-list').hidden = data.items.length === 0;
        $('jobs-empty').hidden = data.items.length > 0;
        if (!data.items.length) renderEmpty();
        $('result-count').textContent = plural(data.total, 'role');
        $('jobs-error').hidden = true;
        renderPagination();
        if (!quiet) announce(`${plural(data.total, 'role')} found. ${data.items.length ? `Page ${data.page} of ${data.pages}.` : ''}`);
      } catch (error) {
        if (error.name === 'AbortError' || sequence !== state.jobsSequence) return;
        const hasVisibleResults = quiet && state.total !== null;
        $('jobs-error-text').textContent = `${hasVisibleResults ? 'Could not update. Showing the last loaded results. ' : 'Could not load opportunities. '}${errorText(error)}`;
        $('jobs-error').hidden = false;
        if (!hasVisibleResults) {
          $('job-list').hidden = true;
          $('jobs-empty').hidden = true;
          $('pagination').hidden = true;
          $('result-count').textContent = 'Unavailable';
        }
      } finally {
        if (sequence === state.jobsSequence) {
          $('jobs-loading').hidden = true;
          $('job-list').setAttribute('aria-busy', 'false');
        }
      }
    }

    function addJobTags(target, job, all = false) {
      target.replaceChildren();
      if (job.level) target.append(element('span', 'badge badge-level', LEVELS[job.level] || label(job.level)));
      if (job.category) target.append(element('span', 'badge', label(job.category)));
      if (job.active === false) target.append(element('span', 'badge badge-warning', 'Closed / no longer seen'));
      const tags = strings(job.tags).filter((tag) => tag !== job.category && tag !== job.level);
      for (const tag of (all ? tags : tags.slice(0, 1))) target.append(element('span', 'badge', tag));
    }

    function renderJobs() {
      const active = document.activeElement;
      const activeRow = active && active.closest('[data-job-id]');
      const restore = activeRow && $('job-list').contains(activeRow) ? { id: activeRow.dataset.jobId, action: active.dataset.action } : null;
      const fragment = document.createDocumentFragment();
      for (const job of state.items) {
        const li = element('li');
        const row = element('article', `job-row${job.active === false ? ' is-closed' : ''}`);
        row.dataset.jobId = String(job.id);
        const initialsNode = element('span', 'company-monogram', monogram(job.company));
        initialsNode.setAttribute('aria-hidden', 'true');
        const main = element('div', 'job-main');
        const heading = element('h3', 'job-title');
        const open = element('button', 'job-open', displayText(job.title, 'Untitled role'));
        open.type = 'button';
        open.dataset.action = 'open';
        open.setAttribute('aria-label', `${displayText(job.title, 'Untitled role')} at ${displayText(job.company, 'employer not published')}. View details.`);
        heading.append(open);
        const meta = element('div', 'job-meta');
        meta.append(element('span', 'company-name', displayText(job.company, 'Employer not published')), element('span', 'meta-separator', '·'), element('span', '', displayText(job.location, 'Location not published')));
        const tags = element('div', 'job-tags');
        addJobTags(tags, job);
        main.append(heading, meta, tags);
        const reasons = strings(job.priority_reasons);
        // Lead with the most specific reason (interest, then skills), not the generic base reason.
        const specific = reasons.filter((reason) => reason !== 'Canadian early-career opportunity');
        const rank = (reason) => (/interest/i.test(reason) ? 0 : /skill/i.test(reason) ? 1 : 2);
        const lead = [...specific].sort((a, b) => rank(a) - rank(b))[0] || reasons[0];
        if (typeof job.priority_score === 'number' && Number.isFinite(job.priority_score)) {
          const match = element('div', 'job-match-note');
          match.append(element('strong', '', `${count(job.priority_score)} match score`));
          if (lead) {
            const reason = element('span', 'job-match-reason', `· ${lead}`);
            reason.title = reasons.join(' · ');
            match.append(reason);
          }
          main.append(match);
        }
        const timing = element('div', 'job-timing');
        const deadline = formatDeadline(job.deadline_date || job.deadline, job.deadline_kind, timezone());
        timing.append(element('span', 'timing-label', 'Deadline'), element('span', `deadline-value${deadline === 'Not published' ? ' deadline-unknown' : ''}`, deadline));
        const posted = parseDate(job.posted_at) ? `Posted ${dateLabel(job.posted_at)}` : (parseDate(job.first_seen) ? `Found ${dateLabel(job.first_seen)}` : 'Posted date not published');
        timing.append(element('span', 'posted-value', posted));
        const save = element('button', 'save-job');
        save.type = 'button';
        save.dataset.action = job.status === 'applied' ? 'open' : 'save';
        const statusLabel = job.status === 'applied' ? 'Applied' : job.status === 'saved' ? 'Saved' : job.status === 'dismissed' ? 'Restore' : 'Save';
        save.append(icon(job.status === 'applied' ? 'check' : job.status === 'dismissed' ? 'refresh' : 'bookmark'), element('span', '', statusLabel));
        if (job.status !== 'applied') save.setAttribute('aria-pressed', String(job.status === 'saved'));
        save.setAttribute('aria-label', `${job.status === 'saved' ? 'Unsave' : statusLabel} ${displayText(job.title, 'role')}${job.status === 'applied' ? '. View application status' : ''}`);
        save.title = job.status === 'applied' ? 'Applied. Open details to change status.' : `${statusLabel} role`;
        save.disabled = state.pendingJobs.has(String(job.id));
        row.append(initialsNode, main, timing, save);
        li.append(row);
        fragment.append(li);
      }
      $('job-list').replaceChildren(fragment);
      if (restore) {
        const row = [...$('job-list').querySelectorAll('[data-job-id]')].find((item) => item.dataset.jobId === restore.id);
        const control = row && [...row.querySelectorAll('[data-action]')].find((item) => item.dataset.action === restore.action && !item.disabled);
        (control || $('results-heading')).focus({ preventScroll: true });
      }
    }

    function renderPagination() {
      $('pagination').hidden = !state.items.length;
      const start = (state.page - 1) * state.pageSize + 1;
      const end = Math.min(state.total, start + state.items.length - 1);
      $('page-range').textContent = `Showing ${count(start)}–${count(end)} of ${plural(state.total, 'role')}`;
      $('page-number').textContent = `Page ${state.page} of ${Math.max(1, state.pages)}`;
      $('previous-page').disabled = state.page <= 1;
      $('next-page').disabled = state.page >= state.pages;
    }

    function renderEmpty() {
      let title = 'No matching roles';
      let text = 'Try a broader search, a different location, or clear a filter.';
      let action = 'Clear filters';
      let kind = 'reset';
      if (!activeFilters()) {
        kind = 'browse';
        action = 'Browse opportunities';
        if (state.view === 'saved') { title = 'Make room for a good next step.'; text = 'Save a role that catches your eye. Your shortlist will be right here.'; }
        else if (state.view === 'applied') { title = 'Your progress starts here.'; text = 'After submitting an application, mark the role applied to keep track. Opening a listing never changes its status.'; }
        else if (state.view === 'dismissed') { title = 'Nothing dismissed.'; text = 'Roles you dismiss will appear here, so you can revisit or restore them.'; }
        else { title = state.collectionRunning ? 'Your radar is searching.' : 'No opportunities collected yet.'; text = 'Real Canadian roles will appear after a successful source check. Sources shows which boards are available.'; action = state.collectionRunning ? 'Refreshing sources…' : 'Refresh sources'; kind = 'refresh'; }
      }
      $('empty-title').textContent = title;
      $('empty-text').textContent = text;
      $('empty-action').textContent = action;
      $('empty-action').dataset.kind = kind;
      $('empty-action').disabled = kind === 'refresh' && (state.collectionRunning || state.refreshPending);
    }

    // --- Overview, ranking note and owner block ------------------------------------------------

    async function loadStats() {
      const sequence = ++state.statsSequence;
      if (state.statsController) state.statsController.abort();
      state.statsController = new AbortController();
      try {
        const data = await request('/api/stats', { signal: state.statsController.signal });
        if (sequence !== state.statsSequence) return;
        if (!data || typeof data.total !== 'number' || !Array.isArray(data.categories)) throw new Error('The server returned an invalid overview.');
        const wasRunning = state.collectionRunning;
        state.stats = data;
        state.statsError = '';
        state.collectionRunning = data.collection_running === true;
        renderStats();
        // A finished collection re-checked every source; refresh their health quietly.
        if (wasRunning && !state.collectionRunning && state.sources) void loadSources({ quiet: true });
      } catch (error) {
        if (error.name === 'AbortError' || sequence !== state.statsSequence) return;
        state.statsError = errorText(error);
        renderFreshness();
      }
    }

    function renderStats() {
      const data = state.stats;
      const fields = { 'stat-total': 'total', 'stat-new': 'new_today', 'stat-priority': 'priority', 'stat-closing': 'closing_soon', 'nav-total': 'total', 'nav-saved': 'saved', 'nav-applied': 'applied' };
      for (const [id, key] of Object.entries(fields)) {
        const nav = id.startsWith('nav-');
        $(id).textContent = nav ? navCount(data[key]) : count(data[key]);
        if (nav) $(id).title = count(data[key]);
      }
      const categories = data.categories.filter((category) => category && typeof category.name === 'string');
      const categorySignature = JSON.stringify(categories);
      if (categorySignature !== state.categoriesSignature && document.activeElement !== $('category-filter')) {
        const select = $('category-filter');
        const selected = state.filters.category;
        const options = [new Option('All categories', '')];
        for (const category of categories) options.push(new Option(`${label(category.name)} (${count(category.count)})`, category.name));
        if (selected && !categories.some((category) => category.name === selected)) options.push(new Option(`${label(selected)} (current filter)`, selected));
        select.replaceChildren(...options);
        select.value = selected;
        state.categoriesSignature = categorySignature;
      }
      const locations = Array.isArray(data.location_counts) ? data.location_counts.filter((location) => location && typeof location.name === 'string') : [];
      const locationSignature = JSON.stringify(locations);
      if (state.locationsSignature !== locationSignature) {
        const fragment = document.createDocumentFragment();
        for (const location of locations) {
          const option = element('option');
          option.value = location.name;
          option.label = plural(location.count, 'role');
          fragment.append(option);
        }
        $('location-options').replaceChildren(fragment);
        state.locationsSignature = locationSignature;
      }
      renderPreference();
      renderFreshness();
      if (!$('jobs-empty').hidden) renderEmpty();
    }

    // The ranking note and Priority toggle follow the profile; nothing personal is hard-coded.
    function renderPreference() {
      const data = state.stats;
      if (!data) return;
      const profileSet = data.profile_set === true;
      const terms = strings(data.priority_terms);
      let text = 'Upload your resume or fill in your profile to personalize ranking.';
      if (profileSet && terms.length) text = `Ranking boosts: ${terms.slice(0, 8).join(', ')}${terms.length > 8 ? `, and ${count(terms.length - 8)} more` : ''}`;
      else if (profileSet) text = 'Ranking follows your profile. Add interests to choose what counts as a Priority match.';
      $('preference-text').textContent = text;
      $('preference-link').textContent = profileSet ? 'Edit profile' : 'Set up your profile';
      $('nav-profile').className = `health-dot ${profileSet ? 'ok' : 'attention'}`;
      const filter = $('priority-filter');
      filter.disabled = !profileSet;
      $('priority-toggle').classList.toggle('is-disabled', !profileSet);
      $('priority-toggle').title = profileSet ? 'Show only roles that strongly match your profile.' : 'Set up your profile to use Priority matches.';
      $('priority-hint').textContent = profileSet ? (terms.length ? `Priority matches use your interests: ${terms.join(', ')}.` : 'Shows only roles that strongly match your profile.') : 'Unavailable until you set up your profile.';
      if (!profileSet && filter.checked) {
        filter.checked = false;
        if (isJobView()) applyFilters();
        else readFilters();
      }
    }

    function renderOwner() {
      const name = plainText(state.profile && state.profile.name, 80);
      const letters = initials(name);
      $('owner-avatar').replaceChildren(letters ? document.createTextNode(letters) : icon('user'));
      $('owner-name').textContent = name || 'Your workspace';
      $('owner-caption').textContent = name ? 'Your profile' : 'Self-hosted';
      $('owner-button').title = name ? 'Open your profile' : 'Set up your profile';
    }

    function renderFreshness() {
      const data = state.stats;
      const failed = Boolean(state.statsError);
      $('connection-dot').className = `status-dot ${failed ? 'error' : state.collectionRunning ? 'running' : data ? 'ok' : ''}`;
      $('connection-label').textContent = failed ? 'Connection interrupted' : state.collectionRunning ? 'Refreshing sources' : data ? 'Live view' : 'Connecting…';
      $('status-warning').hidden = !failed;
      $('status-warning-text').textContent = `Overview unavailable${data ? '; totals show the last loaded snapshot' : ''}. ${state.statsError}`;
      if (data) {
        $('freshness-text').textContent = parseDate(data.last_refresh) ? `Sources refreshed ${relativeTime(data.last_refresh)}` : 'No completed source refresh yet';
        $('freshness-text').title = timestamp(data.last_refresh, 'No completed source refresh recorded');
        $('source-summary').textContent = `${count(data.sources_ok)} / ${count(data.sources_total)} sources responding`;
        $('source-summary').title = 'Includes working and limited checks. Open Sources for coverage details.';
        updateHealthDot();
        const next = parseDate(data.next_refresh) ? timestamp(data.next_refresh) : displayText(data.next_refresh);
        $('next-refresh').textContent = next ? `Next check: ${next}` : '';
        $('next-refresh').title = `Collection schedule · ${timezone()}`;
      } else if (failed) {
        $('freshness-text').textContent = 'Source freshness unavailable';
      }
      updateRefreshButton();
    }

    function updateRefreshButton() {
      const running = state.refreshPending || state.collectionRunning;
      $('refresh-button').disabled = running;
      $('refresh-button').querySelector('span').textContent = running ? 'Refreshing…' : 'Refresh sources';
      $('refresh-button').setAttribute('aria-busy', String(running));
      $('collection-note').hidden = !running;
      if ($('empty-action').dataset.kind === 'refresh') {
        $('empty-action').disabled = running;
        $('empty-action').textContent = running ? 'Refreshing sources…' : 'Refresh sources';
      }
    }

    function updateHealthDot() {
      if (!state.sources) return;
      const enabled = state.sources.filter((source) => source && source.enabled !== false);
      $('nav-health').className = `health-dot ${enabled.length ? (enabled.some(sourceNeedsAttention) ? 'attention' : 'ok') : ''}`;
    }
    // --- Sources -------------------------------------------------------------------------------

    function sourceTypeLabel(type) {
      const known = state.sourceTypes.find((item) => item && item.type === type);
      return known && typeof known.label === 'string' && known.label ? known.label : (label(type) || 'Source type not supplied');
    }

    async function loadSources({ quiet = false } = {}) {
      const sequence = ++state.sourcesSequence;
      if (state.sourcesController) state.sourcesController.abort();
      state.sourcesController = new AbortController();
      if (!quiet && !state.sources) $('sources-loading').hidden = false;
      try {
        const data = await request('/api/sources', { signal: state.sourcesController.signal });
        if (sequence !== state.sourcesSequence) return;
        if (!data || !Array.isArray(data.items)) throw new Error('The server returned an invalid source list.');
        state.sources = data.items.filter((item) => item && typeof item === 'object' && typeof item.id === 'string');
        state.sourcesRefresh = data.last_refresh;
        if (Array.isArray(data.types)) state.sourceTypes = data.types;
        const signature = JSON.stringify(data);
        if (signature !== state.sourcesSignature) {
          state.sourcesSignature = signature;
          renderSources();
        } else updateSourceFreshness();
        $('sources-error').hidden = true;
      } catch (error) {
        if (error.name === 'AbortError' || sequence !== state.sourcesSequence) return;
        $('sources-error-text').textContent = `Could not load sources.${state.sources ? ' Showing the last loaded list.' : ''} ${errorText(error)}`;
        $('sources-error').hidden = false;
        if (!state.sources) $('sources-checked').textContent = 'Source status is unavailable';
      } finally {
        if (sequence === state.sourcesSequence) $('sources-loading').hidden = true;
      }
    }

    function renderSourceCounts() {
      const counts = { ok: 0, partial: 0, blocked: 0, error: 0, disabled: 0, pending: 0, unknown: 0 };
      for (const source of state.sources) counts[Object.hasOwn(counts, source.status) ? source.status : 'unknown'] += 1;
      const badges = [];
      for (const [status, total] of Object.entries(counts)) {
        if (!total && status !== 'ok') continue;
        const presentation = sourcePresentation(status);
        badges.push(countBadge(presentation.label, count(total), `badge-${presentation.tone}`));
      }
      $('source-counts').replaceChildren(...badges);
      const origins = {};
      for (const source of state.sources) origins[source.origin] = (origins[source.origin] || 0) + 1;
      $('source-origin-counts').replaceChildren(...Object.keys(ORIGIN_LABELS).filter((origin) => origins[origin])
        .map((origin) => countBadge(sourceOriginLabel(origin), count(origins[origin]), 'badge-origin')));
    }

    function renderSourceTypeOptions() {
      const byType = new Map();
      for (const source of state.sources) {
        if (typeof source.type === 'string' && source.type) byType.set(source.type, (byType.get(source.type) || 0) + 1);
      }
      const entries = [...byType.entries()].map(([type, total]) => [type, sourceTypeLabel(type), total]).sort((a, b) => a[1].localeCompare(b[1]));
      const signature = JSON.stringify(entries);
      if (signature === state.sourceTypesSignature) return;
      state.sourceTypesSignature = signature;
      const select = $('source-type-filter');
      const selected = select.value;
      select.replaceChildren(new Option('All types', ''), ...entries.map(([type, text, total]) => new Option(`${text} (${count(total)})`, type)));
      select.value = entries.some(([type]) => type === selected) ? selected : '';
    }

    function sourceFilters() {
      return { health: $('source-filter').value, origin: $('source-origin-filter').value, type: $('source-type-filter').value, text: $('source-search').value };
    }

    function actionButton(text, className, action, ariaLabel, disabled = false) {
      const button = element('button', className, text);
      button.type = 'button';
      button.dataset.action = action;
      button.disabled = disabled;
      if (ariaLabel) button.setAttribute('aria-label', ariaLabel);
      return button;
    }

    function sourceRow(source) {
      const enabled = source.enabled !== false;
      const name = displayText(source.name, 'Unnamed source');
      const row = element('li', `source-row${enabled ? '' : ' is-disabled'}`);
      row.dataset.sourceId = source.id;
      const info = element('div', 'source-info');
      const heading = element('h3', 'source-name');
      heading.append(externalLink(name, source.url));
      const meta = element('span', 'source-type', `${sourceTypeLabel(source.type)} · ${sourceOriginLabel(source.origin)}`);
      if (displayText(source.keywords)) meta.textContent += ` · “${source.keywords}”`;
      if (displayText(source.coverage)) meta.title = source.coverage;
      info.append(heading, meta);
      const presentation = sourcePresentation(source.status);
      const badge = element('span', `badge source-badge badge-${presentation.tone}`, presentation.label);
      const metrics = element('dl', 'source-metrics');
      for (const [title, value] of [['Matched', source.matched_count], ['Scanned', source.raw_count]]) {
        const cell = element('div');
        cell.append(element('dt', '', title), element('dd', '', count(value)));
        metrics.append(cell);
      }
      const checked = element('div', 'source-checked', 'Last checked');
      checked.dataset.checked = typeof source.last_checked === 'string' ? source.last_checked : '';
      checked.append(element('strong', '', parseDate(source.last_checked) ? relativeTime(source.last_checked) : 'Not yet'));
      checked.title = timestamp(source.last_checked, 'No check recorded');
      const pending = state.pendingSources.has(source.id);
      const actions = element('div', 'source-actions');
      actions.append(actionButton(enabled ? 'Disable' : 'Enable', 'button button-secondary button-small', 'toggle', `${enabled ? 'Disable' : 'Enable'} ${name}`, pending));
      if (source.removable === true) actions.append(actionButton('Remove', 'button button-quiet button-small', 'remove', `Remove ${name}`, pending));
      row.append(info, badge, metrics, checked, actions);
      let explanation = enabled ? displayText(source.error) : displayText(source.notes, 'Disabled. It is skipped during collection.');
      if (!explanation && source.status === 'partial') explanation = 'This source has limited coverage. Not every listing may be included.';
      if (!explanation && source.status === 'blocked') explanation = 'This source restricts automated access.';
      if (!explanation && source.status === 'error') explanation = 'The last check did not complete successfully.';
      if (!explanation && source.status === 'pending') explanation = 'No completed check yet. Coverage is not confirmed.';
      if (explanation) row.append(element('p', 'source-error', explanation));
      return row;
    }

    function renderSources() {
      if (!state.sources) return;
      const focused = document.activeElement;
      const focusedRow = focused && focused.closest ? focused.closest('.source-row') : null;
      const restore = focusedRow && $('source-list').contains(focusedRow) ? { id: focusedRow.dataset.sourceId, action: focused.dataset.action } : null;
      renderSourceCounts();
      renderSourceTypeOptions();
      const filtered = filterSources(state.sources, sourceFilters());
      const shown = filtered.slice(0, state.sourceLimit);
      state.sourcesFiltered = filtered.length;
      state.sourcesShown = shown.length;
      const fragment = document.createDocumentFragment();
      for (const source of shown) fragment.append(sourceRow(source));
      $('source-list').replaceChildren(fragment);
      const rest = filtered.length - shown.length;
      $('source-more').hidden = rest <= 0;
      $('source-more').textContent = `Show ${count(Math.min(SOURCE_PAGE_SIZE, rest))} more (${count(rest)} not shown)`;
      if (restore) {
        const row = [...$('source-list').children].find((item) => item.dataset.sourceId === restore.id);
        const control = row && ([...row.querySelectorAll('[data-action]')].find((item) => item.dataset.action === restore.action && !item.disabled) || row.querySelector('a'));
        (control || $('source-filter')).focus({ preventScroll: true });
      }
      $('sources-empty').hidden = filtered.length > 0;
      $('sources-empty-title').textContent = state.sources.length ? 'No sources in this view' : 'No sources yet';
      $('sources-empty-text').textContent = state.sources.length ? 'Clear the search or choose other filters to see more sources.' : 'Add a board below, or run discovery to find employer boards.';
      updateSourceFreshness();
    }

    function updateSourceFreshness() {
      if (!state.sources) return;
      $('source-list').querySelectorAll('.source-checked').forEach((node) => {
        node.querySelector('strong').textContent = parseDate(node.dataset.checked) ? relativeTime(node.dataset.checked) : 'Not yet';
      });
      const total = state.sources.length;
      const scope = state.sourcesFiltered === total ? plural(total, 'source') : `${count(state.sourcesFiltered)} of ${plural(total, 'source')}`;
      $('sources-checked').textContent = `${scope} · ${parseDate(state.sourcesRefresh) ? `Collection ${relativeTime(state.sourcesRefresh)}` : 'No completed collection yet'}`;
      updateHealthDot();
    }

    function replaceSource(updated) {
      if (!updated || typeof updated !== 'object' || typeof updated.id !== 'string') return;
      if (!state.sources) state.sources = [];
      const index = state.sources.findIndex((item) => item.id === updated.id);
      if (index >= 0) state.sources[index] = updated;
      else state.sources.push(updated);
      state.sourcesSignature = '';
    }

    async function toggleSource(source) {
      if (state.pendingSources.has(source.id)) return;
      const enabled = source.enabled === false;
      const name = displayText(source.name, 'Source');
      state.pendingSources.add(source.id);
      renderSources();
      try {
        const data = await request(`/api/sources/${encodeURIComponent(source.id)}`, { method: 'PATCH', body: { enabled } });
        if (!data || !data.source || data.source.enabled !== enabled) throw new Error('The change could not be confirmed. Reload the list and retry.');
        replaceSource(data.source);
        toast(enabled ? `${name} enabled. It is checked on the next collection.` : `${name} disabled. It is skipped during collection.`);
      } catch (error) {
        toast(`Could not ${enabled ? 'enable' : 'disable'} ${name}. ${errorText(error)}`, true);
      } finally {
        state.pendingSources.delete(source.id);
        renderSources();
      }
    }

    async function removeSource(source) {
      if (state.pendingSources.has(source.id)) return;
      const name = displayText(source.name, 'this source');
      const confirmed = await confirmAction({ title: `Remove ${name}?`, message: 'Career Radar stops checking this board, and discovery will not add it back.', confirm: 'Remove source', danger: true });
      if (!confirmed) return;
      state.pendingSources.add(source.id);
      renderSources();
      try {
        await request(`/api/sources/${encodeURIComponent(source.id)}`, { method: 'DELETE' });
        state.sources = state.sources.filter((item) => item.id !== source.id);
        state.sourcesSignature = '';
        toast(`${name} removed.`);
      } catch (error) {
        toast(`Could not remove ${name}. ${errorText(error)}`, true);
      } finally {
        state.pendingSources.delete(source.id);
        renderSources();
      }
    }
    // --- Adding boards and keyword searches ----------------------------------------------------

    function renderCandidates() {
      const fragment = document.createDocumentFragment();
      for (const candidate of state.candidates) {
        const status = candidate.added
          ? { label: 'Added', tone: 'ok', canAdd: false, text: 'Added to your sources. It is checked on every collection.' }
          : candidateStatus(candidate.status);
        const name = displayText(candidate.name, 'Unnamed board');
        const item = element('li', 'candidate');
        item.dataset.key = candidate.key;
        const head = element('div', 'candidate-head');
        const title = element('h4', 'candidate-name');
        title.append(externalLink(name, candidate.url));
        head.append(title, element('span', `badge badge-${status.tone}`, status.label));
        item.append(head);
        if (candidate.status !== 'already_monitored' && !candidate.added) {
          item.append(element('p', 'candidate-meta', `${sourceTypeLabel(candidate.type)} · ${plural(candidate.raw, 'listing')} · ${count(candidate.canada)} in Canada · ${count(candidate.matched)} match your radar now`));
        } else item.append(element('p', 'candidate-meta', sourceTypeLabel(candidate.type)));
        if (status.text) item.append(element('p', 'candidate-text', status.text));
        const titles = strings(candidate.sample_titles).slice(0, 5);
        if (titles.length && !candidate.added) {
          const list = element('ul', 'candidate-samples');
          for (const text of titles) list.append(element('li', '', text));
          item.append(element('p', 'candidate-samples-label', 'Example roles:'), list);
        }
        if (displayText(candidate.error) && !candidate.added) item.append(element('p', 'candidate-error', candidate.error));
        if (status.canAdd) item.append(actionButton('Add board', 'button button-primary button-small', 'add', `Add ${name}`, state.pendingSources.has(`candidate:${candidate.key}`)));
        fragment.append(item);
      }
      $('board-candidates').replaceChildren(fragment);
    }

    // Invalid pasted text, shown per line ("not a url" stays one entry, not three words).
    function invalidLines(text) {
      const out = [];
      for (const line of String(text || '').split(/\n+/)) {
        const parsed = splitUrlList(line);
        if (!parsed.invalid.length) continue;
        out.push(parsed.urls.length ? parsed.invalid.join(' ') : line.trim());
      }
      return out;
    }

    async function checkBoards() {
      if (state.checkingBoards) return;
      const parsed = splitUrlList($('board-urls').value);
      if (!parsed.urls.length) {
        setStatus('board-check-status', parsed.invalid.length ? 'These are not web addresses. Paste full board links, one per line.' : 'Paste at least one careers-board address.', true);
        $('board-urls').focus();
        return;
      }
      state.checkingBoards = true;
      setBusy($('board-check'), true, 'Checking…');
      setStatus('board-check-status', `Checking ${plural(parsed.urls.length, 'board')}. This can take up to a minute.`);
      try {
        const data = await request('/api/sources/check', { method: 'POST', body: { urls: parsed.urls }, timeout: 150_000 });
        if (!data || !Array.isArray(data.candidates)) throw new Error('The server returned an invalid check result.');
        state.candidates = data.candidates.filter((item) => item && typeof item === 'object' && typeof item.key === 'string');
        renderCandidates();
        const unsupported = [...invalidLines($('board-urls').value), ...strings(data.unsupported)];
        $('board-unsupported-list').replaceChildren(...unsupported.map((text) => element('li', '', text)));
        $('board-unsupported').hidden = !unsupported.length;
        const ready = state.candidates.filter((item) => candidateStatus(item.status).canAdd).length;
        const message = `${plural(state.candidates.length, 'board')} checked${ready ? `, ${count(ready)} can be added` : ''}.${parsed.extra ? ` ${count(parsed.extra)} more skipped: up to ${MAX_BOARD_URLS} per check.` : ''}`;
        setStatus('board-check-status', message);
        announce(message);
      } catch (error) {
        setStatus('board-check-status', `Check failed. ${errorText(error)}`, true);
      } finally {
        state.checkingBoards = false;
        setBusy($('board-check'), false);
      }
    }

    async function addCandidate(candidate) {
      const pendingKey = `candidate:${candidate.key}`;
      if (state.pendingSources.has(pendingKey)) return;
      state.pendingSources.add(pendingKey);
      renderCandidates();
      try {
        const data = await request('/api/sources', { method: 'POST', body: { url: candidate.url, name: displayText(candidate.name) || undefined } });
        if (!data || !data.source || typeof data.source.id !== 'string') throw new Error('The server did not confirm the new source.');
        candidate.added = true;
        replaceSource(data.source);
        renderSources();
        toast(`${displayText(data.source.name, 'Board')} added. It is checked on the next collection.`);
      } catch (error) {
        if (error.status === 409) candidate.status = 'already_monitored';
        toast(`Could not add the board. ${errorText(error)}`, true);
      } finally {
        state.pendingSources.delete(pendingKey);
        renderCandidates();
      }
    }

    async function addSearchSource(event) {
      event.preventDefault();
      if (state.addingSearch) return;
      const type = $('search-source-type').value;
      const keywords = plainText($('search-source-keywords').value, 200);
      const name = plainText($('search-source-name').value, 200);
      if (keywords.length < 2 || keywords.length > 80) {
        setStatus('search-source-status', 'Keywords must be 2–80 characters.', true);
        $('search-source-keywords').focus();
        return;
      }
      state.addingSearch = true;
      setBusy($('search-source-submit'), true, 'Adding…');
      setStatus('search-source-status', '');
      try {
        const data = await request('/api/sources', { method: 'POST', body: { type, keywords, name: name || undefined } });
        if (!data || !data.source || typeof data.source.id !== 'string') throw new Error('The server did not confirm the new search.');
        replaceSource(data.source);
        renderSources();
        $('search-source-keywords').value = '';
        $('search-source-name').value = '';
        setStatus('search-source-status', `Added “${displayText(data.source.name, keywords)}”. It runs on the next collection.`);
      } catch (error) {
        setStatus('search-source-status', errorText(error), true);
      } finally {
        state.addingSearch = false;
        setBusy($('search-source-submit'), false);
      }
    }
    // --- Profile and resume ---------------------------------------------------------------------

    const CHIP_FIELDS = Object.freeze({ skills: 'pf-skills', interests: 'pf-interests', preferred_locations: 'pf-locations', exclude_keywords: 'pf-exclude', links: 'pf-links' });
    const RECORD_FIELDS = Object.freeze({
      education: [['school', 'School'], ['degree', 'Degree'], ['field', 'Field of study'], ['graduation', 'Graduation']],
      experience: [['title', 'Title'], ['company', 'Company'], ['start', 'Start'], ['end', 'End']]
    });
    const PROFILE_SOURCES = Object.freeze({ manual: 'Entered by you', resume: 'From your resume', 'resume+llm': 'From your resume, read with your LLM' });
    const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

    function markProfileDirty() {
      state.profileDirty = true;
      setStatus('profile-status', 'Unsaved changes');
    }

    function renderChips(key) {
      const prefix = CHIP_FIELDS[key];
      const values = state.chips[key] || [];
      $(`${prefix}-list`).replaceChildren(...values.map((value, index) => {
        const item = element('li', 'chip');
        item.append(element('span', 'chip-text', value));
        const remove = element('button', 'chip-remove');
        remove.type = 'button';
        remove.dataset.chipKey = key;
        remove.dataset.chipIndex = String(index);
        remove.setAttribute('aria-label', `Remove ${value}`);
        remove.append(icon('close'));
        item.append(remove);
        return item;
      }));
      $(`${prefix}-list`).hidden = !values.length;
      $(`${prefix}-add`).disabled = values.length >= PROFILE_LIST_CAPS[key];
    }

    // Returns false when some typed text could not be added (it stays in the box to correct).
    function addChips(key) {
      const prefix = CHIP_FIELDS[key];
      const input = $(`${prefix}-input`);
      const parts = splitChipInput(input.value, key !== 'preferred_locations');
      if (!parts.length) { input.value = ''; return true; }
      const current = state.chips[key] || [];
      const cap = PROFILE_LIST_CAPS[key];
      const invalid = [];
      const additions = [];
      for (const part of parts) {
        const value = key === 'links' ? normalizeHttpUrl(part) : plainText(part);
        if (value) additions.push(value);
        else invalid.push(part);
      }
      const next = normalizeChips([...current, ...additions], { max: cap, maxLength: key === 'links' ? 300 : 200 });
      const overflow = normalizeChips([...current, ...additions], { max: cap + additions.length }).length > next.length;
      state.chips[key] = next;
      input.value = invalid.join(key === 'preferred_locations' ? '; ' : ', ');
      renderChips(key);
      let message = '';
      if (invalid.length) message = key === 'links' ? `Not a public web address: ${invalid.join(', ')}` : 'Some entries could not be added.';
      else if (overflow) message = `Limit reached: up to ${count(cap)}.`;
      setStatus(`${prefix}-message`, message, Boolean(message));
      if (next.length !== current.length) markProfileDirty();
      return invalid.length === 0;
    }

    function recordRow(kind, record = {}) {
      const row = element('li', 'record-row');
      const grid = element('div', 'record-grid');
      const number = ++state.recordCounter;
      for (const [key, text] of RECORD_FIELDS[kind]) {
        const field = element('div', 'field');
        const id = `pf-${kind}-${number}-${key}`;
        const caption = element('label', '', text);
        caption.htmlFor = id;
        const input = element('input');
        input.type = 'text';
        input.id = id;
        input.dataset.key = key;
        input.maxLength = 200;
        input.autocomplete = 'off';
        input.value = plainText(record[key]);
        field.append(caption, input);
        grid.append(field);
      }
      const remove = actionButton('Remove', 'button button-quiet button-small record-remove', 'remove-record', `Remove this ${kind} entry`);
      row.append(grid, remove);
      return row;
    }

    function updateRecordState(kind) {
      const total = $(`pf-${kind}-list`).children.length;
      $(`pf-${kind}-empty`).hidden = total > 0;
      $(`pf-${kind}-add`).disabled = total >= MAX_PROFILE_RECORDS;
    }

    function renderRecords(kind, records) {
      $(`pf-${kind}-list`).replaceChildren(...records.map((record) => recordRow(kind, record)));
      updateRecordState(kind);
    }

    function readRecords(kind) {
      return [...$(`pf-${kind}-list`).children].map((row) => Object.fromEntries([...row.querySelectorAll('input[data-key]')].map((input) => [input.dataset.key, input.value])));
    }

    function fillProfileForm() {
      const profile = state.profile || {};
      for (const key of ['name', 'email', 'location', 'headline', 'graduation']) $(`pf-${key}`).value = plainText(profile[key], key === 'headline' ? 300 : 200);
      const levels = new Set(Array.isArray(profile.levels) ? profile.levels : []);
      document.querySelectorAll('input[name="pf-level"]').forEach((input) => { input.checked = levels.has(input.value); });
      for (const key of Object.keys(CHIP_FIELDS)) {
        state.chips[key] = normalizeChips(profile[key], { max: PROFILE_LIST_CAPS[key], maxLength: key === 'links' ? 300 : 200 });
        $(`${CHIP_FIELDS[key]}-input`).value = '';
        setStatus(`${CHIP_FIELDS[key]}-message`, '');
        renderChips(key);
      }
      renderRecords('education', cleanRecords(profile.education, EDUCATION_KEYS));
      renderRecords('experience', cleanRecords(profile.experience, EXPERIENCE_KEYS));
      const source = Object.hasOwn(PROFILE_SOURCES, profile.source) ? PROFILE_SOURCES[profile.source] : PROFILE_SOURCES.manual;
      $('profile-meta').textContent = parseDate(profile.updated_at) ? `${source} · saved ${relativeTime(profile.updated_at)}` : 'Not saved yet';
      state.profileDirty = false;
      setStatus('profile-status', '');
    }

    function collectProfile() {
      const read = (id) => $(id).value;
      return cleanProfile({
        name: read('pf-name'), email: read('pf-email'), location: read('pf-location'), headline: read('pf-headline'), graduation: read('pf-graduation'),
        education: readRecords('education'), experience: readRecords('experience'),
        skills: state.chips.skills, interests: state.chips.interests, preferred_locations: state.chips.preferred_locations,
        exclude_keywords: state.chips.exclude_keywords, links: state.chips.links,
        levels: [...document.querySelectorAll('input[name="pf-level"]:checked')].map((input) => input.value)
      });
    }

    function renderResumeHint() {
      const settings = state.settings;
      let text = '';
      if (settings && settings.llm) {
        const provider = (Array.isArray(settings.providers) ? settings.providers : []).find((item) => item && item.id === settings.llm.provider);
        text = settings.llm.configured === true && provider
          ? ` Your LLM: ${provider.label}${plainText(settings.llm.model) ? ` (${plainText(settings.llm.model)})` : ''}.`
          : ' No LLM is set up, so resumes are read on this server.';
      }
      $('resume-llm-hint').textContent = text;
    }

    function renderResume() {
      const resume = state.resume;
      $('resume-result').hidden = !resume;
      $('resume-none').hidden = Boolean(resume);
      if (!resume) {
        $('resume-extracted-block').hidden = true;
        $('resume-warnings-block').hidden = true;
        return;
      }
      const pdf = resume.pdf && typeof resume.pdf === 'object' ? resume.pdf : {};
      const rows = [['File', plainText(resume.filename, 120) || 'resume.pdf'], ['Uploaded', timestamp(resume.uploaded_at)], ['Pages', count(resume.pages)],
        ['Text read', plural(resume.chars, 'character')], ['Read by', resume.method === 'llm' ? 'Your LLM' : 'This server (no LLM)']];
      if (plainText(pdf.title)) rows.push(['PDF title', plainText(pdf.title)]);
      if (plainText(pdf.author)) rows.push(['PDF author', plainText(pdf.author)]);
      const created = pdfDate(pdf.created);
      if (created) rows.push(['PDF created', formatDeadline(created, 'date')]);
      const maker = plainText(pdf.creator) || plainText(pdf.producer);
      if (maker) rows.push(['Made with', maker]);
      factList($('resume-facts'), rows);
      const found = state.lastUpload ? summarizeExtracted(state.lastUpload.extracted) : '';
      $('resume-extracted-block').hidden = !state.lastUpload;
      $('resume-extracted').textContent = found ? `Found ${found}.` : 'No profile details were found in this upload.';
      const warnings = stringList(resume.warnings, 20);
      $('resume-warnings').replaceChildren(...warnings.map((text) => element('li', '', text)));
      $('resume-warnings-block').hidden = !warnings.length;
    }

    async function loadProfile({ quiet = false } = {}) {
      const sequence = ++state.profileSequence;
      if (!quiet && !state.profileLoaded) {
        $('profile-loading').hidden = false;
        $('profile-form').hidden = true;
      }
      if (!quiet && !state.settings) void loadSettings({ quiet: true });
      try {
        const data = await request('/api/profile');
        if (sequence !== state.profileSequence) return;
        if (!data || !data.profile || typeof data.profile !== 'object') throw new Error('The server returned an invalid profile.');
        state.resume = data.resume && typeof data.resume === 'object' ? data.resume : null;
        state.profileLoaded = true;
        if (!state.profileDirty) {
          state.profile = data.profile;
          fillProfileForm();
        }
        renderResume();
        renderOwner();
        $('profile-error').hidden = true;
        $('profile-form').hidden = false;
      } catch (error) {
        if (sequence !== state.profileSequence || quiet) return;
        $('profile-error-text').textContent = `Could not load your profile. ${errorText(error)}`;
        $('profile-error').hidden = false;
      } finally {
        if (sequence === state.profileSequence) $('profile-loading').hidden = true;
      }
    }

    function showResumeError(message) {
      $('resume-error-text').textContent = message;
      $('resume-error').hidden = !message;
    }

    function setResumeProgress(fraction, uploaded, mode) {
      $('resume-progress').value = Math.round(Math.max(0, Math.min(1, fraction)) * 100);
      const withLlm = mode === 'on' || (mode === 'auto' && state.settings && state.settings.llm && state.settings.llm.configured === true);
      $('resume-status-text').textContent = uploaded ? (withLlm ? 'Reading your resume with your LLM. This can take a minute…' : 'Reading your resume…') : `Uploading… ${Math.round(fraction * 100)}%`;
    }

    async function handleResumeFile(file) {
      if (state.uploading) return;
      showResumeError('');
      const check = validateResumeFile(file);
      if (!check.ok) { showResumeError(check.message); return; }
      let head = null;
      try { head = new Uint8Array(await file.slice(0, 1029).arrayBuffer()); } catch (_) { head = null; }
      if (!head || !looksLikePdf(head)) {
        showResumeError(`“${plainText(file.name, 80) || 'This file'}” is not a readable PDF. Export or print your resume to PDF and try again.`);
        return;
      }
      if (state.profileDirty && !(await confirmAction({ title: 'Discard unsaved profile changes?', message: 'Reading a resume updates your saved profile and reloads the form, so unsaved edits are lost.', confirm: 'Read resume anyway', danger: true }))) return;
      const choice = document.querySelector('input[name="resume-llm"]:checked');
      const mode = choice ? choice.value : 'auto';
      state.uploading = true;
      setBusy($('resume-choose'), true, 'Reading…');
      $('resume-drop').classList.add('is-busy');
      $('resume-status').hidden = false;
      setResumeProgress(0, false, mode);
      try {
        const query = new URLSearchParams({ llm: mode, filename: resumeFilename(file.name) });
        const result = await uploadPdf(`/api/profile/resume?${query}`, file, (fraction, uploaded) => setResumeProgress(fraction, uploaded, mode));
        if (!result.profile || typeof result.profile !== 'object') throw new Error('The server did not return your updated profile.');
        state.profile = result.profile;
        state.resume = result.resume && typeof result.resume === 'object' ? result.resume : null;
        state.lastUpload = { extracted: result.extracted, warnings: stringList(result.warnings, 20) };
        fillProfileForm();
        renderResume();
        renderOwner();
        state.listSignature = '';
        void loadStats();
        const found = summarizeExtracted(result.extracted);
        toast(found ? `Resume read. Found ${found}. Review your profile below.` : 'Resume stored, but no profile details were found. Fill in your profile below.');
        $('resume-result-heading').focus();
      } catch (error) {
        showResumeError(errorText(error));
      } finally {
        state.uploading = false;
        setBusy($('resume-choose'), false);
        $('resume-drop').classList.remove('is-busy');
        $('resume-status').hidden = true;
      }
    }

    async function saveProfile(event) {
      event.preventDefault();
      if (state.profileBusy) return;
      const email = plainText($('pf-email').value);
      if (email && !EMAIL_PATTERN.test(email)) {
        setStatus('profile-status', 'Enter a valid email address, or leave it empty.', true);
        $('pf-email').focus();
        return;
      }
      const results = Object.keys(CHIP_FIELDS).map((key) => [key, addChips(key)]);
      const failed = results.find(([, ok]) => !ok);
      if (failed) {
        setStatus('profile-status', 'Fix the highlighted entry before saving.', true);
        $(`${CHIP_FIELDS[failed[0]]}-input`).focus();
        return;
      }
      state.profileBusy = true;
      setBusy($('profile-save'), true, 'Saving…');
      try {
        const data = await request('/api/profile', { method: 'PUT', body: collectProfile() });
        if (!data || !data.profile || typeof data.profile !== 'object') throw new Error('The server did not confirm the saved profile.');
        state.profile = data.profile;
        fillProfileForm();
        renderOwner();
        state.listSignature = '';
        void loadStats();
        setStatus('profile-status', 'Saved. Ranking now follows your profile.');
        toast('Profile saved.');
      } catch (error) {
        setStatus('profile-status', `Not saved. ${errorText(error)}`, true);
      } finally {
        state.profileBusy = false;
        setBusy($('profile-save'), false);
      }
    }

    async function deleteResume() {
      if (state.profileBusy) return;
      const confirmed = await confirmAction({ title: 'Delete the stored resume?', message: 'The stored PDF details and text are removed from this server. Your profile stays as it is.', confirm: 'Delete resume', danger: true });
      if (!confirmed) return;
      state.profileBusy = true;
      try {
        await request('/api/profile/resume', { method: 'DELETE' });
        state.resume = null;
        state.lastUpload = null;
        renderResume();
        toast('Stored resume deleted. Your profile is unchanged.');
      } catch (error) {
        toast(`Could not delete the resume. ${errorText(error)}`, true);
      } finally {
        state.profileBusy = false;
      }
    }

    async function resetProfile() {
      if (state.profileBusy) return;
      const confirmed = await confirmAction({ title: 'Reset your profile?', message: 'This deletes your profile and the stored resume. Ranking goes back to neutral until you set it up again.', confirm: 'Reset profile', danger: true });
      if (!confirmed) return;
      state.profileBusy = true;
      try {
        const data = await request('/api/profile', { method: 'DELETE' });
        state.profile = data && data.profile && typeof data.profile === 'object' ? data.profile : {};
        state.resume = null;
        state.lastUpload = null;
        fillProfileForm();
        renderResume();
        renderOwner();
        state.listSignature = '';
        void loadStats();
        toast('Profile reset.');
      } catch (error) {
        toast(`Could not reset the profile. ${errorText(error)}`, true);
      } finally {
        state.profileBusy = false;
      }
    }
    function setJobUrl(id) {
      const url = new URL(window.location.href);
      if (id !== null && id !== undefined) url.searchParams.set('job', String(id));
      else url.searchParams.delete('job');
      window.history.replaceState(null, '', `${url.pathname}${url.search}${url.hash}`);
    }

    async function openDetail(id, { syncUrl = true } = {}) {
      const jobId = String(id);
      const sequence = ++state.detailSequence;
      if (state.detailController) state.detailController.abort();
      state.detailController = new AbortController();
      const dialog = $('job-dialog');
      if (!dialog.open) state.detailOpener = document.activeElement;
      state.detailId = jobId;
      state.currentJob = null;
      $('detail-content').hidden = true;
      $('detail-actions').hidden = true;
      $('detail-error').hidden = true;
      $('detail-share').hidden = true;
      $('detail-loading').hidden = false;
      $('copy-link').disabled = true;
      dialog.setAttribute('aria-labelledby', 'detail-title');
      $('detail-title').textContent = 'Job details';
      if (syncUrl) setJobUrl(jobId);
      if (!dialog.open) dialog.showModal();
      document.body.classList.add('dialog-open');
      dialog.scrollTop = 0;
      try {
        const job = await request(`/api/jobs/${encodeURIComponent(jobId)}`, { signal: state.detailController.signal });
        if (sequence !== state.detailSequence || !dialog.open) return;
        if (!validJob(job) || String(job.id) !== jobId) throw new Error('The server did not return this role. It may no longer be available.');
        renderDetail(job);
        $('detail-title').focus({ preventScroll: true });
      } catch (error) {
        if (error.name === 'AbortError' || sequence !== state.detailSequence) return;
        $('detail-error-text').textContent = errorText(error);
        $('detail-error').hidden = false;
        dialog.setAttribute('aria-labelledby', 'detail-error-title');
      } finally {
        if (sequence === state.detailSequence) $('detail-loading').hidden = true;
      }
    }

    function fillList(target, values, fallback) {
      const items = strings(values);
      target.replaceChildren(...(items.length ? items : [fallback]).map((text) => element('li', '', text)));
    }

    function renderDetail(job) {
      state.currentJob = job;
      $('detail-content').hidden = false;
      $('detail-actions').hidden = false;
      $('detail-error').hidden = true;
      $('copy-link').disabled = false;
      $('detail-title').textContent = displayText(job.title, 'Untitled role');
      $('detail-company').textContent = displayText(job.company, 'Employer not published');
      $('detail-monogram').textContent = monogram(job.company);
      $('detail-location').textContent = displayText(job.location, 'Location not published');
      $('detail-score').textContent = count(job.priority_score);
      $('detail-status').textContent = STATUSES.has(job.status) ? label(job.status) : 'Status not supplied';
      $('detail-status').className = `badge badge-${STATUSES.has(job.status) ? job.status : 'unknown'}`;
      addJobTags($('detail-tags'), job, true);
      fillList($('detail-reasons'), job.priority_reasons, 'No match notes supplied.');
      fillList($('detail-eligibility'), job.eligibility_notes, 'No additional eligibility notes supplied by the source.');
      $('detail-deadline').textContent = formatDeadline(job.deadline_date || job.deadline, job.deadline_kind, timezone());
      $('detail-posted').textContent = parseDate(job.posted_at) ? timestamp(job.posted_at) : 'Not published';
      $('detail-salary').textContent = displayText(job.salary, 'Not published');
      $('detail-active').textContent = job.active === false ? 'Closed / no longer returned by source' : job.active === true ? 'Open at last check' : 'Not confirmed';
      $('detail-description').textContent = displayText(job.description, 'No description supplied. Open the original listing for the full responsibilities and requirements.');
      const evidence = displayText(job.deadline_evidence);
      $('deadline-evidence-section').hidden = !evidence;
      $('detail-evidence').textContent = evidence;
      const evidenceUrl = safeHttpUrl(job.deadline_source_url);
      $('deadline-source').hidden = !evidenceUrl;
      if (evidenceUrl) $('deadline-source').href = evidenceUrl;
      else $('deadline-source').removeAttribute('href');
      const verification = { direct: 'Direct employer listing', curated: 'Curated listing', public_listing: 'Public job listing' };
      $('detail-verification').textContent = Object.hasOwn(verification, job.verification) ? verification[job.verification] : 'Verification not supplied';
      const sources = Array.isArray(job.sources) ? job.sources : [];
      const sourceNodes = sources.map((source) => {
        const li = element('li');
        li.append(externalLink(displayText(source.name, 'Original listing'), source.url));
        return li;
      });
      $('detail-sources').replaceChildren(...(sourceNodes.length ? sourceNodes : [element('li', '', 'Source information not supplied.')]));
      $('detail-first-seen').textContent = timestamp(job.first_seen);
      $('detail-last-seen').textContent = timestamp(job.last_seen);
      const applyUrl = safeHttpUrl(job.url);
      $('apply-link').hidden = !applyUrl;
      $('apply-unavailable').hidden = Boolean(applyUrl);
      if (applyUrl) $('apply-link').href = applyUrl;
      else $('apply-link').removeAttribute('href');
      // There is intentionally no status-changing handler on this external link.
      updateDetailActions();
    }

    function updateDetailActions() {
      const job = state.currentJob;
      if (!job) return;
      const pending = state.pendingJobs.has(String(job.id));
      const save = $('detail-save');
      save.disabled = pending;
      save.querySelector('span').textContent = pending ? 'Saving…' : job.status === 'saved' ? 'Unsave role' : ['applied', 'dismissed'].includes(job.status) ? 'Move to saved' : 'Save role';
      save.setAttribute('aria-pressed', String(job.status === 'saved'));
      $('detail-applied').disabled = pending;
      $('detail-applied').querySelector('span').textContent = job.status === 'applied' ? 'Undo applied' : 'Mark applied';
      $('detail-applied').setAttribute('aria-pressed', String(job.status === 'applied'));
      $('detail-dismiss').disabled = pending;
      $('detail-dismiss').querySelector('span').textContent = job.status === 'dismissed' ? 'Restore role' : 'Dismiss role';
      $('detail-dismiss').setAttribute('aria-pressed', String(job.status === 'dismissed'));
    }

    function syncPendingButtons() {
      $('job-list').querySelectorAll('[data-job-id]').forEach((row) => {
        const button = row.querySelector('.save-job');
        if (button) button.disabled = state.pendingJobs.has(row.dataset.jobId);
      });
      updateDetailActions();
    }

    async function patchStatus(id, status) {
      const jobId = String(id);
      if (!STATUSES.has(status) || state.pendingJobs.has(jobId)) return;
      const focused = document.activeElement;
      const restoreFocus = focused && (focused.matches('.save-job') || ['detail-save', 'detail-applied', 'detail-dismiss'].includes(focused.id));
      state.pendingJobs.add(jobId);
      syncPendingButtons();
      try {
        await request(`/api/jobs/${encodeURIComponent(jobId)}`, { method: 'PATCH', body: { status } });
        // Read the exact target back before claiming a state change succeeded.
        const verified = await request(`/api/jobs/${encodeURIComponent(jobId)}`);
        if (!validJob(verified) || String(verified.id) !== jobId || verified.status !== status) throw new Error('The new status could not be confirmed. Reopen the role before retrying.');
        if (state.detailId === jobId && $('job-dialog').open) renderDetail(verified);
        await Promise.all([loadJobs({ quiet: true }), loadStats()]);
        const messages = { new: 'Role returned to your opportunities.', saved: 'Role saved to your shortlist.', applied: 'Marked applied. Your progress is saved.', dismissed: 'Role dismissed. You can restore it from Dismissed.' };
        toast(messages[status]);
      } catch (error) {
        toast(`Status change could not be confirmed. ${errorText(error)}`, true);
      } finally {
        state.pendingJobs.delete(jobId);
        syncPendingButtons();
        if (restoreFocus && document.activeElement === document.body) {
          const row = [...$('job-list').querySelectorAll('[data-job-id]')].find((item) => item.dataset.jobId === jobId);
          const fallback = row ? row.querySelector('.save-job') : $('results-heading');
          (focused.isConnected ? focused : fallback).focus({ preventScroll: true });
        }
      }
    }

    function finalizeDetailClose() {
      // Native close events may wait for a rendering frame in background tabs.
      // Clean up immediately, and never let an old close event erase a reopened role.
      if ($('job-dialog').open || state.detailId === null) return;
      const closedId = state.detailId;
      if (state.detailController) state.detailController.abort();
      state.detailSequence += 1;
      state.detailId = null;
      state.currentJob = null;
      document.body.classList.remove('dialog-open');
      setJobUrl(null);
      if ($('toast').parentElement !== document.body) document.body.append($('toast'));
      const row = [...$('job-list').querySelectorAll('[data-job-id]')].find((item) => item.dataset.jobId === closedId);
      const opener = state.detailOpener && state.detailOpener.isConnected && state.detailOpener !== document.body ? state.detailOpener : null;
      const fallback = row ? row.querySelector('.job-open') : document.querySelector('.nav-item.is-active');
      (opener || fallback).focus({ preventScroll: true });
      state.detailOpener = null;
    }

    function closeDetail() {
      if ($('job-dialog').open) $('job-dialog').close();
      finalizeDetailClose();
    }

    async function copyJobLink() {
      if (state.detailId === null) return;
      const url = new URL(window.location.href);
      url.searchParams.set('job', state.detailId);
      try {
        if (!navigator.clipboard || !navigator.clipboard.writeText) throw new Error('Clipboard unavailable');
        await navigator.clipboard.writeText(url.href);
        toast('Link copied.');
      } catch (_) {
        $('detail-share').hidden = false;
        $('share-url').value = url.href;
        $('share-url').focus();
        $('share-url').select();
        announce('Copy the selected link.');
      }
    }

    async function refreshSources() {
      if (state.refreshPending || state.collectionRunning) return;
      state.refreshPending = true;
      updateRefreshButton();
      try {
        const result = await request('/api/refresh', { method: 'POST', body: {} });
        if (!result || typeof result.started !== 'boolean') throw new Error('The server did not confirm the refresh request.');
        if (result.started) state.collectionRunning = true;
        // Re-read collection status. A POST acknowledgement alone is not freshness.
        await loadStats();
        toast(displayText(result.message, result.started ? 'Source refresh requested. Progress is shown above.' : 'No new refresh started. Check the collection status.'));
        schedulePoll(2500);
      } catch (error) {
        toast(`Could not start a source refresh. ${errorText(error)}`, true);
        void loadStats();
      } finally {
        state.refreshPending = false;
        updateRefreshButton();
      }
    }

    async function exportCsv() {
      if (state.exportPending) return;
      state.exportPending = true;
      $('export-button').disabled = true;
      $('export-button').querySelector('span').textContent = 'Exporting…';
      const controller = new AbortController();
      const timeout = window.setTimeout(() => controller.abort(), 30_000);
      try {
        const response = await fetch('/api/export.csv', { method: 'GET', credentials: 'same-origin', cache: 'no-store', headers: { Accept: 'text/csv' }, signal: controller.signal });
        if (!response.ok) throw new Error(`Export failed (${response.status}). Please retry.`);
        if (!(response.headers.get('content-type') || '').toLowerCase().includes('csv')) throw new Error('The server did not return a CSV export.');
        const url = URL.createObjectURL(await response.blob());
        const link = element('a');
        link.href = url;
        link.download = 'career-radar.csv';
        document.body.append(link);
        link.click();
        link.remove();
        window.setTimeout(() => URL.revokeObjectURL(url), 10_000);
        toast('CSV export ready. Current filters do not limit this export.');
      } catch (error) {
        toast(error.name === 'AbortError' ? 'The CSV export timed out. Please retry.' : errorText(error), true);
      } finally {
        window.clearTimeout(timeout);
        state.exportPending = false;
        $('export-button').disabled = false;
        $('export-button').querySelector('span').textContent = 'Export CSV';
      }
    }
    // --- Discovery -----------------------------------------------------------------------------

    function discoveryRunBlock(run) {
      const summary = summarizeDiscoveryRun(run);
      if (!summary) return [element('p', 'muted', 'No discovery run recorded yet.')];
      const nodes = [element('p', `run-headline${summary.ok ? '' : ' is-error'}`, summary.headline)];
      if (summary.finishedAt) nodes.push(element('p', 'muted', `Finished ${relativeTime(summary.finishedAt)} · ${timestamp(summary.finishedAt)}`));
      if (summary.added.length) {
        const list = element('ul', 'run-list');
        for (const item of summary.added.slice(0, 25)) {
          list.append(element('li', '', `${item.name} · ${sourceTypeLabel(item.type)} · ${count(item.canadaTech)} Canadian tech role${item.canadaTech === 1 ? '' : 's'}`));
        }
        nodes.push(element('h4', 'subheading', 'Added'), list);
        if (summary.addedCount > summary.added.length) nodes.push(element('p', 'muted', `And ${count(summary.addedCount - summary.added.length)} more.`));
      }
      if (summary.dropped.length) {
        nodes.push(element('h4', 'subheading', 'Dropped (no longer working)'), element('p', 'muted', summary.dropped.slice(0, 20).join(', ') + (summary.dropped.length > 20 ? ` and ${count(summary.dropped.length - 20)} more` : '')));
      }
      if (summary.byStatus.length) {
        const badges = element('div', 'source-counts');
        for (const [status, total] of summary.byStatus) badges.append(countBadge(candidateStatus(status).label, count(total), `badge-${candidateStatus(status).tone}`));
        nodes.push(element('h4', 'subheading', 'Checked candidates'), badges);
      }
      if (summary.notProbed) nodes.push(element('p', 'muted', `${plural(summary.notProbed, 'candidate')} left for the next run (time budget reached).`));
      return nodes;
    }

    function scoutRunBlock(run) {
      const summary = summarizeScoutRun(run);
      if (!summary) return [element('p', 'muted', 'No scout run recorded yet.')];
      const nodes = [element('p', 'run-headline', summary.headline)];
      if (summary.finishedAt) nodes.push(element('p', 'muted', `Finished ${relativeTime(summary.finishedAt)} · ${timestamp(summary.finishedAt)}`));
      if (summary.themes.length) {
        const list = element('ul', 'run-list');
        for (const theme of summary.themes) list.append(element('li', '', theme));
        nodes.push(element('h4', 'subheading', 'Themes'), list);
      }
      if (summary.errors.length) {
        const list = element('ul', 'warning-list');
        for (const text of summary.errors) list.append(element('li', '', text));
        nodes.push(element('h4', 'subheading', 'Problems'), list);
      }
      return nodes;
    }

    function renderDiscovery() {
      const data = state.discovery;
      if (!data) return;
      $('disc-discovered').textContent = count(data.discovered_sources);
      $('disc-curated').textContent = count(data.curated_sources);
      $('disc-checked').textContent = count(data.candidates_checked);
      $('disc-by-status').replaceChildren(...statusCounts(data.by_status).map(([status, total]) => countBadge(candidateStatus(status).label, count(total), `badge-${candidateStatus(status).tone}`)));
      const scout = data.scout && typeof data.scout === 'object' ? data.scout : {};
      const discoveryRunning = data.running === true;
      const scoutRunning = scout.running === true;
      $('discovery-running').hidden = !(discoveryRunning || scoutRunning);
      $('discovery-running-text').textContent = scoutRunning ? 'The LLM scout is looking for employer boards. Suggestions are verified before they are added.' : 'Discovery is running. Results appear here when it finishes.';
      setBusy($('discovery-run'), discoveryRunning, 'Running…');
      const available = scout.available === true;
      setBusy($('scout-run'), scoutRunning, 'Scouting…');
      $('scout-run').disabled = !available && !scoutRunning;
      $('scout-settings-link').hidden = available;
      $('scout-hint').textContent = available ? (scout.enabled ? 'The scout also runs on its schedule.' : 'The scout only runs when you start it, unless you schedule it in Settings.') : 'The scout needs an LLM.';
      $('scout-state').textContent = available ? (scout.enabled ? 'Scheduled, and available to run now.' : 'Available. Not scheduled.') : 'Not available until an LLM is set up in Settings.';
      $('disc-last').replaceChildren(...discoveryRunBlock(data.last_run));
      $('scout-last').replaceChildren(...scoutRunBlock(scout.last_run));
    }

    function scheduleDiscoveryPoll() {
      window.clearTimeout(state.discoveryTimer);
      const data = state.discovery;
      const running = data && (data.running === true || (data.scout && data.scout.running === true));
      if (running && state.view === 'discovery') state.discoveryTimer = window.setTimeout(() => { void loadDiscovery({ quiet: true }); }, 5000);
    }

    async function loadDiscovery({ quiet = false } = {}) {
      const sequence = ++state.discoverySequence;
      if (!quiet && !state.discovery) $('discovery-loading').hidden = false;
      try {
        const data = await request('/api/discovery');
        if (sequence !== state.discoverySequence) return;
        if (!data || typeof data !== 'object') throw new Error('The server returned an invalid discovery status.');
        const wasRunning = state.discovery && (state.discovery.running === true || (state.discovery.scout && state.discovery.scout.running === true));
        state.discovery = data;
        renderDiscovery();
        $('discovery-error').hidden = true;
        const running = data.running === true || (data.scout && data.scout.running === true);
        if (wasRunning && !running) {
          announce('Discovery finished.');
          state.sourcesSignature = '';
          void loadSources({ quiet: true });
        }
      } catch (error) {
        if (sequence !== state.discoverySequence) return;
        $('discovery-error-text').textContent = `Could not load discovery status. ${errorText(error)}`;
        $('discovery-error').hidden = false;
      } finally {
        if (sequence === state.discoverySequence) {
          $('discovery-loading').hidden = true;
          scheduleDiscoveryPoll();
        }
      }
    }

    async function startDiscoveryJob(path, button) {
      if (state.discoveryBusy || button.getAttribute('aria-disabled') === 'true' || button.disabled) return;
      state.discoveryBusy = true;
      setBusy(button, true, 'Starting…');
      try {
        const result = await request(path, { method: 'POST', body: {} });
        if (!result || typeof result.started !== 'boolean') throw new Error('The server did not confirm the request.');
        toast(displayText(result.message, result.started ? 'Started.' : 'Already running.'));
      } catch (error) {
        toast(errorText(error), true);
      } finally {
        state.discoveryBusy = false;
        setBusy(button, false);
        await loadDiscovery({ quiet: true });
      }
    }
    // --- Settings: LLM provider, keys, ChatGPT sign-in, schedule -------------------------------

    function providerList() {
      return state.settings && Array.isArray(state.settings.providers) ? state.settings.providers.filter((item) => item && typeof item.id === 'string') : [];
    }

    function providerSpec(id) { return providerList().find((item) => item.id === id) || null; }

    function selectedProvider() { return providerSpec($('llm-provider').value); }

    function renderProviderOptions() {
      const select = $('llm-provider');
      const selected = state.llmDirty ? select.value : plainText(state.settings.llm && state.settings.llm.provider, 40);
      select.replaceChildren(new Option('None — don’t use an LLM', ''), ...providerList().map((item) => new Option(plainText(item.label, 80) || item.id, item.id)));
      select.value = providerSpec(selected) ? selected : '';
    }

    // Show only the fields the chosen provider needs.
    function renderProviderFields() {
      const spec = selectedProvider();
      const settings = state.settings;
      const secrets = settings.secrets && typeof settings.secrets === 'object' ? settings.secrets : {};
      $('llm-model-field').hidden = !spec;
      $('llm-base-url-field').hidden = !(spec && spec.needs_base_url === true);
      $('llm-key-field').hidden = !(spec && spec.auth !== 'oauth');
      $('llm-codex-field').hidden = !(spec && spec.auth === 'oauth');
      $('llm-provider-note').textContent = spec ? plainText(spec.notes, 300) : 'Career Radar works without an LLM. Resumes are then read on this server and the scout is off.';
      if (spec) {
        $('llm-model').placeholder = plainText(spec.default_model) ? `Default: ${plainText(spec.default_model)}` : 'Model name';
        if (spec.id !== state.modelsProvider) {
          $('llm-model-options').replaceChildren();
          setStatus('llm-models-status', '');
        }
      }
      if (spec && spec.auth !== 'oauth') {
        const secret = secrets[spec.id];
        $('llm-key-label').textContent = `${plainText(spec.label, 80) || 'Provider'} API key${spec.auth === 'optional_key' ? ' (optional)' : ''}`;
        $('llm-key-status').textContent = keyHint(secret, spec);
        const fromEnv = secret && secret.source === 'env';
        $('llm-key').disabled = Boolean(fromEnv);
        $('llm-key').placeholder = fromEnv ? 'Set by the server environment' : (secret && secret.set ? 'Enter a new key to replace it' : 'Paste your API key');
        $('llm-key-save').disabled = Boolean(fromEnv);
        $('llm-key-clear').hidden = !(secret && secret.set && secret.source === 'file');
        const docs = safeHttpUrl(spec.docs_url);
        $('llm-key-docs').hidden = !docs;
        if (docs) {
          $('llm-key-docs').href = docs;
          $('llm-key-docs').target = '_blank';
          $('llm-key-docs').rel = 'noopener noreferrer';
        } else $('llm-key-docs').removeAttribute('href');
      }
      renderCodex();
    }

    function renderLlmBadge() {
      const settings = state.settings;
      const configured = settings.llm && settings.llm.configured === true;
      const spec = providerSpec(settings.llm && settings.llm.provider);
      $('llm-configured').textContent = configured ? 'Ready' : spec ? 'Needs setup' : 'Off';
      $('llm-configured').className = `badge badge-${configured ? 'ok' : spec ? 'warning' : 'pending'}`;
    }

    function applySettings(data) {
      state.settings = data;
      const version = plainText(data.version, 40);
      $('settings-version').textContent = `Career Radar${version ? ` ${version}` : ''} · open source, self-hosted`;
      if (!state.llmDirty) {
        renderProviderOptions();
        $('llm-model').value = plainText(data.llm && data.llm.model, 200);
        $('llm-base-url').value = plainText(data.llm && data.llm.base_url, 300);
      }
      renderProviderFields();
      renderLlmBadge();
      if (!state.scheduleDirty) fillSchedule(data.schedule || {});
      renderScheduleStatus(data.schedule_status);
      $('schedule-timezone').textContent = plainText(data.timezone, 80) || DEFAULT_TIMEZONE;
      renderResumeHint();
    }

    async function loadSettings({ quiet = false } = {}) {
      const sequence = ++state.settingsSequence;
      if (!quiet && !state.settings) $('settings-loading').hidden = false;
      try {
        const data = await request('/api/settings');
        if (sequence !== state.settingsSequence) return;
        if (!data || typeof data !== 'object' || !data.llm || !data.schedule) throw new Error('The server returned invalid settings.');
        applySettings(data);
        $('settings-content').hidden = false;
        $('settings-error').hidden = true;
      } catch (error) {
        if (sequence !== state.settingsSequence || quiet) return;
        $('settings-error-text').textContent = `Could not load settings. ${errorText(error)}`;
        $('settings-error').hidden = false;
      } finally {
        if (sequence === state.settingsSequence) $('settings-loading').hidden = true;
      }
    }

    function llmPayload() {
      const spec = selectedProvider();
      return { provider: spec ? spec.id : '', model: plainText($('llm-model').value, 200), base_url: spec && spec.needs_base_url ? plainText($('llm-base-url').value, 300) : '' };
    }

    async function saveLlm({ quietSuccess = false } = {}) {
      const body = llmPayload();
      const spec = selectedProvider();
      if (spec && spec.needs_base_url && !normalizeHttpUrlAllowLocal(body.base_url)) {
        setStatus('llm-form-status', 'Enter the server’s base URL, starting with http:// or https://.', true);
        $('llm-base-url').focus();
        return false;
      }
      const data = await request('/api/settings', { method: 'PUT', body: { llm: body } });
      state.llmDirty = false;
      applySettings(data);
      if (!quietSuccess) setStatus('llm-form-status', data.llm && data.llm.configured ? 'Saved. Use Test connection to check it works.' : (spec ? 'Saved, but this provider still needs a key or sign-in.' : 'Saved. Career Radar will not use an LLM.'));
      void loadStats();
      return true;
    }

    // Local servers (Ollama, LM Studio) are fine for the base URL; only the scheme is checked here.
    function normalizeHttpUrlAllowLocal(value) {
      return typeof value === 'string' && /^https?:\/\/[^\s/]+/i.test(value.trim()) ? value.trim() : null;
    }

    async function submitLlm(event) {
      event.preventDefault();
      if (state.llmBusy) return;
      state.llmBusy = true;
      setBusy($('llm-save'), true, 'Saving…');
      try { await saveLlm(); }
      catch (error) { setStatus('llm-form-status', `Not saved. ${errorText(error)}`, true); }
      finally { state.llmBusy = false; setBusy($('llm-save'), false); }
    }

    async function saveKey(clear = false) {
      const spec = selectedProvider();
      if (!spec || spec.auth === 'oauth' || state.llmBusy) return;
      const key = $('llm-key').value.trim();
      if (!clear && !key) { setStatus('llm-form-status', 'Paste the API key first.', true); $('llm-key').focus(); return; }
      state.llmBusy = true;
      const button = clear ? $('llm-key-clear') : $('llm-key-save');
      setBusy(button, true, clear ? 'Clearing…' : 'Saving…');
      try {
        const data = await request('/api/settings/secrets', { method: 'PUT', body: { provider: spec.id, api_key: clear ? null : key } });
        $('llm-key').value = '';
        applySettings(data);
        setStatus('llm-form-status', clear ? 'Key removed from this server.' : 'Key saved. It is never shown again; only its last characters.');
      } catch (error) {
        setStatus('llm-form-status', `${clear ? 'Key not removed' : 'Key not saved'}. ${errorText(error)}`, true);
      } finally {
        state.llmBusy = false;
        setBusy(button, false);
      }
    }

    async function loadModels() {
      const spec = selectedProvider();
      if (!spec || state.modelsBusy) return;
      state.modelsBusy = true;
      setBusy($('llm-load-models'), true, 'Loading…');
      setStatus('llm-models-status', 'Loading models…');
      try {
        const query = new URLSearchParams({ provider: spec.id });
        if (spec.needs_base_url && plainText($('llm-base-url').value)) query.set('base_url', plainText($('llm-base-url').value, 300));
        const data = await request(`/api/llm/models?${query}`, { timeout: 45_000 });
        const items = data && Array.isArray(data.items) ? data.items.filter((item) => item && typeof item.id === 'string' && item.id) : [];
        state.modelsProvider = spec.id;
        $('llm-model-options').replaceChildren(...items.slice(0, 500).map((item) => {
          const option = element('option');
          option.value = item.id;
          if (plainText(item.label) && item.label !== item.id) option.label = plainText(item.label);
          return option;
        }));
        if (data && data.error) setStatus('llm-models-status', `Could not list models: ${plainText(data.error, 300)}`, true);
        else setStatus('llm-models-status', items.length ? `${plural(items.length, 'model')} available. Start typing to pick one.` : 'No models were listed. Type a model name.');
      } catch (error) {
        setStatus('llm-models-status', `Could not list models. ${errorText(error)}`, true);
      } finally {
        state.modelsBusy = false;
        setBusy($('llm-load-models'), false);
      }
    }

    async function testLlm() {
      if (state.llmBusy) return;
      state.llmBusy = true;
      setBusy($('llm-test'), true, 'Testing…');
      $('llm-test-result').hidden = true;
      try {
        if (state.llmDirty && !(await saveLlm({ quietSuccess: true }))) return;
        setStatus('llm-form-status', 'Sending a short test request…');
        const result = await request('/api/llm/test', { method: 'POST', body: {}, timeout: 130_000 });
        const box = $('llm-test-result');
        box.className = `test-result ${result && result.ok ? 'is-ok' : 'is-error'}`;
        const lines = result && result.ok
          ? [`Connected to ${plainText(result.model) || 'the model'} in ${formatDuration((result.latency_ms || 0) / 1000) || 'under a second'}.`, `Reply: “${plainText(result.reply, 200)}”`]
          : [`Test failed: ${plainText(result && result.error, 300) || 'no reply.'}`];
        box.replaceChildren(...lines.map((text) => element('p', '', text)));
        box.hidden = false;
        setStatus('llm-form-status', '');
      } catch (error) {
        setStatus('llm-form-status', `Test failed. ${errorText(error)}`, true);
      } finally {
        state.llmBusy = false;
        setBusy($('llm-test'), false);
      }
    }

    function renderCodex() {
      const settings = state.settings;
      if (!settings) return;
      const codex = settings.codex && typeof settings.codex === 'object' ? settings.codex : {};
      const login = state.codexLogin && typeof state.codexLogin === 'object' ? state.codexLogin : { status: 'idle' };
      const signedIn = codex.logged_in === true;
      const pending = login.status === 'pending';
      let text = signedIn ? `Signed in${plainText(codex.account) ? ` as ${plainText(codex.account)}` : ''}.` : 'Not signed in.';
      if (!signedIn && displayText(codex.error)) text += ` ${plainText(codex.error, 200)}`;
      $('llm-codex-status').textContent = text;
      $('llm-codex-login').hidden = signedIn && !pending;
      $('llm-codex-logout').hidden = !signedIn;
      $('llm-codex-code').hidden = !pending;
      if (pending) {
        $('llm-codex-user-code').textContent = plainText(login.user_code, 40);
        const link = safeHttpUrl(login.verification_url);
        if (link) {
          $('llm-codex-link').href = link;
          $('llm-codex-link').target = '_blank';
          $('llm-codex-link').rel = 'noopener noreferrer';
        }
        $('llm-codex-url').textContent = link ? link : '';
        $('llm-codex-poll').textContent = parseDate(login.expires_at) ? `Waiting for you to approve. The code expires ${timestamp(login.expires_at)}.` : 'Waiting for you to approve…';
      } else if (login.status === 'error') {
        $('llm-codex-poll').textContent = '';
        $('llm-codex-status').textContent = `Sign-in failed: ${plainText(login.error, 300) || 'unknown error'}.`;
      }
    }

    function scheduleCodexPoll() {
      window.clearTimeout(state.codexTimer);
      if (state.codexLogin && state.codexLogin.status === 'pending') state.codexTimer = window.setTimeout(() => { void pollCodex(); }, 3000);
    }

    async function pollCodex() {
      try {
        const login = await request('/api/llm/codex/login');
        state.codexLogin = login;
        if (login && login.status === 'complete') {
          state.codexLogin = null;
          await loadSettings({ quiet: true });
          toast('Signed in with ChatGPT.');
          void loadStats();
        }
        renderCodex();
      } catch (error) {
        $('llm-codex-poll').textContent = `Could not check the sign-in. ${errorText(error)} Retrying…`;
      } finally {
        scheduleCodexPoll();
      }
    }

    async function startCodexLogin() {
      if (state.codexBusy) return;
      state.codexBusy = true;
      setBusy($('llm-codex-login'), true, 'Starting…');
      try {
        if ($('llm-provider').value === 'codex' && state.settings.llm.provider !== 'codex') await saveLlm({ quietSuccess: true });
        state.codexLogin = await request('/api/llm/codex/login', { method: 'POST', body: {}, timeout: 45_000 });
        renderCodex();
        if (state.codexLogin && state.codexLogin.status === 'pending') $('llm-codex-copy').focus();
        else if (state.codexLogin && state.codexLogin.status === 'error') toast(`Could not start the sign-in. ${plainText(state.codexLogin.error, 300)}`, true);
        scheduleCodexPoll();
      } catch (error) {
        toast(`Could not start the sign-in. ${errorText(error)}`, true);
      } finally {
        state.codexBusy = false;
        setBusy($('llm-codex-login'), false);
      }
    }

    async function codexLogout() {
      if (state.codexBusy) return;
      const confirmed = await confirmAction({ title: 'Sign out of ChatGPT?', message: 'Career Radar deletes its saved sign-in. Resume reading and the scout stop using ChatGPT until you sign in again.', confirm: 'Sign out', danger: true });
      if (!confirmed) return;
      state.codexBusy = true;
      try {
        const data = await request('/api/llm/codex', { method: 'DELETE' });
        state.codexLogin = null;
        applySettings(data);
        toast('Signed out of ChatGPT.');
      } catch (error) {
        toast(`Could not sign out. ${errorText(error)}`, true);
      } finally {
        state.codexBusy = false;
      }
    }

    async function copyCodexCode() {
      const code = $('llm-codex-user-code').textContent;
      try {
        if (!navigator.clipboard || !navigator.clipboard.writeText) throw new Error('Clipboard unavailable');
        await navigator.clipboard.writeText(code);
        toast('Code copied.');
      } catch (_) {
        const range = document.createRange();
        range.selectNodeContents($('llm-codex-user-code'));
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
        announce('Copy the selected code.');
      }
    }
    // --- Schedule ------------------------------------------------------------------------------

    const SCHEDULE_JOBS = Object.freeze({ collect: 'Source collection', discovery: 'Board discovery', scout: 'LLM scout' });

    function timeRow(value = '') {
      const row = element('li', 'time-row');
      const number = ++state.timeCounter;
      const input = element('input');
      input.type = 'time';
      input.id = `sch-collect-${number}`;
      input.required = true;
      input.value = normalizeTime(value) || '';
      const caption = element('label', 'sr-only', `Collection time ${number}`);
      caption.htmlFor = input.id;
      row.append(caption, input, actionButton('Remove', 'button button-quiet button-small', 'remove-time', 'Remove this collection time'));
      return row;
    }

    function updateTimeState() {
      const total = $('sch-collect-list').children.length;
      $('sch-collect-empty').hidden = total > 0;
      $('sch-collect-add').disabled = total >= MAX_COLLECT_TIMES;
      [...$('sch-collect-list').children].forEach((row, index) => {
        const caption = row.querySelector('label');
        if (caption) caption.textContent = `Collection time ${index + 1}`;
      });
    }

    function fillSchedule(schedule) {
      $('sch-enabled').checked = schedule.enabled !== false;
      const times = parseTimeList(Array.isArray(schedule.collect_times) ? schedule.collect_times : []).times;
      $('sch-collect-list').replaceChildren(...times.map((time) => timeRow(time)));
      updateTimeState();
      $('sch-discovery-time').value = normalizeTime(schedule.discovery_time) || '';
      $('sch-scout-enabled').checked = schedule.scout_enabled === true;
      $('sch-scout-time').value = normalizeTime(schedule.scout_time) || '';
      $('sch-collect-budget').value = Number.isInteger(schedule.collection_budget_minutes) ? String(schedule.collection_budget_minutes) : '';
      $('sch-discovery-budget').value = Number.isInteger(schedule.discovery_budget_minutes) ? String(schedule.discovery_budget_minutes) : '';
      $('sch-scout-themes').value = Number.isInteger(schedule.scout_themes_per_run) ? String(schedule.scout_themes_per_run) : '';
      state.scheduleDirty = false;
      updateScheduleControls();
    }

    function updateScheduleControls() {
      const auto = $('sch-enabled').checked;
      const llmReady = Boolean(state.settings && state.settings.llm && state.settings.llm.configured === true);
      $('sch-scout-enabled').disabled = !auto;
      const scout = auto && $('sch-scout-enabled').checked;
      $('sch-scout-time').disabled = !scout;
      $('sch-scout-themes').disabled = !scout;
      $('sch-scout-themes-help').textContent = scout && !llmReady
        ? 'The scout only runs once an LLM is set up above.'
        : '1–6. More themes find more employers but use more LLM requests.';
    }

    function readSchedule() {
      const raw = [...$('sch-collect-list').querySelectorAll('input[type="time"]')].map((input) => input.value);
      if (raw.some((value) => !String(value).trim())) return { error: 'Fill in or remove the empty collection time.', focus: [...$('sch-collect-list').querySelectorAll('input')].find((input) => !input.value) };
      const times = parseTimeList(raw);
      if (times.invalid.length) return { error: `Not a valid time: ${times.invalid.join(', ')}. Use 24-hour HH:MM.` };
      if (times.tooMany) return { error: `Up to ${MAX_COLLECT_TIMES} collection times.` };
      const discovery = normalizeTime($('sch-discovery-time').value);
      if (!discovery) return { error: 'Enter a discovery time.', focus: $('sch-discovery-time') };
      const collectBudget = parseIntInRange($('sch-collect-budget').value, 5, 180);
      if (collectBudget === null) return { error: 'Collection budget: a whole number of minutes from 5 to 180.', focus: $('sch-collect-budget') };
      const discoveryBudget = parseIntInRange($('sch-discovery-budget').value, 2, 120);
      if (discoveryBudget === null) return { error: 'Discovery budget: a whole number of minutes from 2 to 120.', focus: $('sch-discovery-budget') };
      const schedule = {
        enabled: $('sch-enabled').checked, collect_times: times.times, discovery_time: discovery,
        collection_budget_minutes: collectBudget, discovery_budget_minutes: discoveryBudget, scout_enabled: $('sch-scout-enabled').checked
      };
      const scoutTime = normalizeTime($('sch-scout-time').value);
      if (scoutTime) schedule.scout_time = scoutTime;
      else if (schedule.scout_enabled) return { error: 'Enter a scout time.', focus: $('sch-scout-time') };
      const themes = parseIntInRange($('sch-scout-themes').value, 1, 6);
      if (themes !== null) schedule.scout_themes_per_run = themes;
      else if (schedule.scout_enabled) return { error: 'Themes per scout run: a whole number from 1 to 6.', focus: $('sch-scout-themes') };
      return { schedule };
    }

    async function saveSchedule(event) {
      event.preventDefault();
      if (state.scheduleBusy) return;
      const result = readSchedule();
      if (result.error) {
        setStatus('schedule-form-status', result.error, true);
        if (result.focus) result.focus.focus();
        return;
      }
      state.scheduleBusy = true;
      setBusy($('schedule-save'), true, 'Saving…');
      try {
        const data = await request('/api/settings', { method: 'PUT', body: { schedule: result.schedule } });
        state.scheduleDirty = false;
        applySettings(data);
        setStatus('schedule-form-status', result.schedule.enabled ? 'Saved. The next runs are listed below.' : 'Saved. Automatic runs are off; use Refresh and Run now instead.');
        void loadStats();
      } catch (error) {
        setStatus('schedule-form-status', `Not saved. ${errorText(error)}`, true);
      } finally {
        state.scheduleBusy = false;
        setBusy($('schedule-save'), false);
      }
    }

    function renderScheduleStatus(status) {
      const jobs = status && status.jobs && typeof status.jobs === 'object' ? status.jobs : {};
      $('schedule-runner').textContent = !status ? 'The scheduler is not available.' : status.running === true ? 'The built-in scheduler is running.' : 'The built-in scheduler is stopped (CAREER_RADAR_SCHEDULER=0 or a test setup). Runs only start by hand.';
      const items = Object.entries(SCHEDULE_JOBS).map(([name, title]) => {
        const job = jobs[name];
        const item = element('li', 'status-item');
        const head = element('div', 'status-head');
        const presentation = scheduleJobState(job);
        head.append(element('strong', '', title), element('span', `badge badge-${presentation.tone === 'running' ? 'warning' : presentation.tone}`, presentation.label));
        item.append(head);
        const lines = [];
        if (job && parseDate(job.next_run)) lines.push(`Next: ${timestamp(job.next_run)}`);
        else lines.push(job && Array.isArray(job.times) && job.times.length ? 'Next: not scheduled' : 'Not scheduled');
        if (job && parseDate(job.last_run)) lines.push(`Last: ${timestamp(job.last_run)}`);
        item.append(element('p', 'muted', lines.join(' · ')));
        if (job && displayText(job.last_summary)) item.append(element('p', 'status-summary', plainText(job.last_summary, 500)));
        return item;
      });
      $('schedule-status-list').replaceChildren(...items);
    }
    // --- Polling -------------------------------------------------------------------------------

    function schedulePoll(delay = state.collectionRunning ? 5000 : 60_000) {
      window.clearTimeout(state.pollTimer);
      state.pollTimer = window.setTimeout(poll, delay);
    }

    async function poll() {
      if (document.hidden) { schedulePoll(60_000); return; }
      if (state.pollPromise) return state.pollPromise;
      const tasks = [loadStats()];
      if (isJobView()) tasks.push(loadJobs({ quiet: true }));
      if (state.view === 'sources' || state.pollCount % 5 === 0) tasks.push(loadSources({ quiet: true }));
      if (state.view === 'discovery') tasks.push(loadDiscovery({ quiet: true }));
      if (state.view === 'settings' && !state.llmDirty && !state.scheduleDirty) tasks.push(loadSettings({ quiet: true }));
      state.pollCount += 1;
      state.pollPromise = Promise.all(tasks);
      try { await state.pollPromise; }
      finally { state.pollPromise = null; schedulePoll(); }
    }

    // --- Events --------------------------------------------------------------------------------

    function leaveGuard(view) {
      if (state.view === 'profile' && view !== 'profile' && state.profileDirty) {
        return confirmAction({ title: 'Leave without saving?', message: 'Your profile has unsaved changes.', confirm: 'Discard changes', danger: true }).then((ok) => {
          if (ok) { state.profileDirty = false; state.profile = state.profile || {}; fillProfileForm(); }
          return ok;
        });
      }
      return Promise.resolve(true);
    }

    document.querySelectorAll('[data-view]').forEach((button) => button.addEventListener('click', async () => {
      const view = button.dataset.view;
      if (view === state.view) return;
      if (await leaveGuard(view)) changeView(view, { focus: !button.classList.contains('nav-item') });
    }));
    $('search-form').addEventListener('submit', (event) => { event.preventDefault(); applyFilters(); });
    for (const id of ['search-input', 'location-filter']) {
      $(id).addEventListener('input', (event) => {
        if (event.isComposing) return;
        window.clearTimeout(state.searchTimer);
        state.searchTimer = window.setTimeout(applyFilters, 350);
      });
    }
    document.querySelectorAll('input[name="level"]').forEach((input) => input.addEventListener('change', applyFilters));
    for (const id of ['category-filter', 'deadline-filter', 'priority-filter', 'include-closed', 'sort-filter']) $(id).addEventListener('change', applyFilters);
    $('reset-filters').addEventListener('click', clearFilters);
    $('jobs-retry').addEventListener('click', () => { void loadJobs(); });
    $('stats-retry').addEventListener('click', () => { void loadStats(); });
    $('refresh-button').addEventListener('click', () => { void refreshSources(); });
    $('export-button').addEventListener('click', () => { void exportCsv(); });
    $('empty-action').addEventListener('click', () => {
      const kind = $('empty-action').dataset.kind;
      if (kind === 'reset') clearFilters();
      else if (kind === 'refresh') void refreshSources();
      else changeView('feed');
    });
    for (const [id, delta] of [['previous-page', -1], ['next-page', 1]]) $(id).addEventListener('click', async () => {
      if (state.page + delta < 1 || state.page + delta > state.pages) return;
      state.page += delta;
      await loadJobs();
      $('results-heading').focus({ preventScroll: true });
    });
    $('job-list').addEventListener('click', (event) => {
      const action = event.target.closest('button[data-action]');
      if (!action || action.disabled) return;
      const row = action.closest('[data-job-id]');
      const job = row && state.items.find((item) => String(item.id) === row.dataset.jobId);
      if (!job) return;
      if (action.dataset.action === 'open') void openDetail(job.id);
      else void patchStatus(job.id, ['saved', 'dismissed'].includes(job.status) ? 'new' : 'saved');
    });
    $('close-detail').addEventListener('click', closeDetail);
    $('job-dialog').addEventListener('click', (event) => {
      if (event.target !== $('job-dialog')) return;
      const rect = $('job-dialog').getBoundingClientRect();
      if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) closeDetail();
    });
    $('job-dialog').addEventListener('cancel', (event) => { event.preventDefault(); closeDetail(); });
    $('job-dialog').addEventListener('close', finalizeDetailClose);
    $('detail-retry').addEventListener('click', () => { if (state.detailId !== null) void openDetail(state.detailId); });
    $('copy-link').addEventListener('click', () => { void copyJobLink(); });
    $('detail-save').addEventListener('click', () => { if (state.currentJob) void patchStatus(state.currentJob.id, state.currentJob.status === 'saved' ? 'new' : 'saved'); });
    $('detail-applied').addEventListener('click', () => { if (state.currentJob) void patchStatus(state.currentJob.id, state.currentJob.status === 'applied' ? 'new' : 'applied'); });
    $('detail-dismiss').addEventListener('click', () => { if (state.currentJob) void patchStatus(state.currentJob.id, state.currentJob.status === 'dismissed' ? 'new' : 'dismissed'); });

    // Confirmation dialog
    $('confirm-ok').addEventListener('click', () => settleConfirm(true));
    $('confirm-cancel').addEventListener('click', () => settleConfirm(false));
    $('confirm-dialog').addEventListener('cancel', (event) => { event.preventDefault(); settleConfirm(false); });
    $('confirm-dialog').addEventListener('close', () => { if (state.confirmResolve) settleConfirm(false); });

    // Sources
    $('sources-retry').addEventListener('click', () => { void loadSources(); });
    for (const id of ['source-filter', 'source-origin-filter', 'source-type-filter']) $(id).addEventListener('change', () => { state.sourceLimit = SOURCE_PAGE_SIZE; renderSources(); });
    $('source-search').addEventListener('input', (event) => {
      if (event.isComposing) return;
      window.clearTimeout(state.sourceSearchTimer);
      state.sourceSearchTimer = window.setTimeout(() => { state.sourceLimit = SOURCE_PAGE_SIZE; renderSources(); }, 200);
    });
    $('source-more').addEventListener('click', () => {
      const firstNew = state.sourcesShown;
      state.sourceLimit += SOURCE_PAGE_SIZE;
      renderSources();
      const row = $('source-list').children[firstNew];
      const link = row && row.querySelector('a, button');
      if (link) link.focus();
    });
    $('source-list').addEventListener('click', (event) => {
      const action = event.target.closest('button[data-action]');
      if (!action || action.disabled) return;
      const row = action.closest('[data-source-id]');
      const source = row && state.sources && state.sources.find((item) => item.id === row.dataset.sourceId);
      if (!source) return;
      if (action.dataset.action === 'toggle') void toggleSource(source);
      else if (action.dataset.action === 'remove') void removeSource(source);
    });
    $('board-check').addEventListener('click', () => { void checkBoards(); });
    $('board-urls').addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); void checkBoards(); }
    });
    $('board-candidates').addEventListener('click', (event) => {
      const action = event.target.closest('button[data-action="add"]');
      if (!action || action.disabled) return;
      const item = action.closest('[data-key]');
      const candidate = item && state.candidates.find((entry) => entry.key === item.dataset.key);
      if (candidate) void addCandidate(candidate);
    });
    $('search-source-form').addEventListener('submit', (event) => { void addSearchSource(event); });

    // Profile
    $('profile-retry').addEventListener('click', () => { void loadProfile(); });
    $('profile-form').addEventListener('submit', (event) => { void saveProfile(event); });
    $('profile-form').addEventListener('input', (event) => {
      if (event.target.matches('.chip-entry input')) return;
      markProfileDirty();
    });
    $('profile-form').addEventListener('change', (event) => { if (event.target.name === 'pf-level') markProfileDirty(); });
    for (const [key, prefix] of Object.entries(CHIP_FIELDS)) {
      $(`${prefix}-add`).addEventListener('click', () => { addChips(key); $(`${prefix}-input`).focus(); });
      $(`${prefix}-input`).addEventListener('keydown', (event) => {
        const separator = event.key === 'Enter' || (event.key === ',' && key !== 'preferred_locations') || event.key === ';';
        if (!separator || event.isComposing) return;
        event.preventDefault();
        addChips(key);
      });
      $(`${prefix}-input`).addEventListener('blur', () => { if ($(`${prefix}-input`).value.trim()) addChips(key); });
      $(`${prefix}-list`).addEventListener('click', (event) => {
        const remove = event.target.closest('button[data-chip-key]');
        if (!remove) return;
        const index = Number(remove.dataset.chipIndex);
        const removed = state.chips[key][index];
        state.chips[key] = state.chips[key].filter((_, position) => position !== index);
        renderChips(key);
        markProfileDirty();
        announce(`Removed ${removed}.`);
        const next = $(`${prefix}-list`).querySelectorAll('button[data-chip-key]')[Math.min(index, state.chips[key].length - 1)];
        (next || $(`${prefix}-input`)).focus();
      });
    }
    for (const kind of ['education', 'experience']) {
      $(`pf-${kind}-add`).addEventListener('click', () => {
        if ($(`pf-${kind}-list`).children.length >= MAX_PROFILE_RECORDS) return;
        const row = recordRow(kind);
        $(`pf-${kind}-list`).append(row);
        updateRecordState(kind);
        markProfileDirty();
        row.querySelector('input').focus();
      });
      $(`pf-${kind}-list`).addEventListener('click', (event) => {
        const remove = event.target.closest('button[data-action="remove-record"]');
        if (!remove) return;
        const row = remove.closest('li');
        const next = row.nextElementSibling || row.previousElementSibling;
        row.remove();
        updateRecordState(kind);
        markProfileDirty();
        ((next && next.querySelector('input')) || $(`pf-${kind}-add`)).focus();
      });
    }
    $('profile-reset').addEventListener('click', () => { void resetProfile(); });
    $('resume-delete').addEventListener('click', () => { void deleteResume(); });
    $('resume-choose').addEventListener('click', () => { if (!state.uploading) $('resume-file').click(); });
    $('resume-file').addEventListener('change', () => {
      const file = $('resume-file').files && $('resume-file').files[0];
      $('resume-file').value = '';
      if (file) void handleResumeFile(file);
    });
    const drop = $('resume-drop');
    for (const type of ['dragenter', 'dragover']) drop.addEventListener(type, (event) => {
      if (!event.dataTransfer || ![...event.dataTransfer.types].includes('Files')) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = state.uploading ? 'none' : 'copy';
      drop.classList.add('is-dragging');
    });
    for (const type of ['dragleave', 'dragend']) drop.addEventListener(type, (event) => { if (!drop.contains(event.relatedTarget)) drop.classList.remove('is-dragging'); });
    drop.addEventListener('drop', (event) => {
      event.preventDefault();
      drop.classList.remove('is-dragging');
      const files = event.dataTransfer ? [...event.dataTransfer.files] : [];
      if (files.length > 1) showResumeError('Drop one PDF at a time.');
      else if (files[0]) void handleResumeFile(files[0]);
    });
    // A file dropped outside the drop zone must not navigate away from the app.
    window.addEventListener('dragover', (event) => { if (event.dataTransfer && [...event.dataTransfer.types].includes('Files')) event.preventDefault(); });
    window.addEventListener('drop', (event) => { if (!drop.contains(event.target)) event.preventDefault(); });
    window.addEventListener('beforeunload', (event) => {
      if (!state.profileDirty && !state.llmDirty && !state.scheduleDirty && !state.uploading) return;
      event.preventDefault();
      event.returnValue = '';
    });

    // Discovery
    $('discovery-retry').addEventListener('click', () => { void loadDiscovery(); });
    $('discovery-run').addEventListener('click', () => { void startDiscoveryJob('/api/discovery/run', $('discovery-run')); });
    $('scout-run').addEventListener('click', () => { void startDiscoveryJob('/api/discovery/scout', $('scout-run')); });

    // Settings
    $('settings-retry').addEventListener('click', () => { void loadSettings(); });
    $('llm-form').addEventListener('submit', (event) => { void submitLlm(event); });
    $('llm-provider').addEventListener('change', () => {
      state.llmDirty = true;
      const spec = selectedProvider();
      $('llm-model').value = '';
      if (spec && spec.needs_base_url && !$('llm-base-url').value) $('llm-base-url').value = '';
      $('llm-test-result').hidden = true;
      setStatus('llm-form-status', 'Unsaved changes');
      renderProviderFields();
    });
    for (const id of ['llm-model', 'llm-base-url']) $(id).addEventListener('input', () => { state.llmDirty = true; setStatus('llm-form-status', 'Unsaved changes'); });
    $('llm-load-models').addEventListener('click', () => { void loadModels(); });
    $('llm-test').addEventListener('click', () => { void testLlm(); });
    $('llm-key-save').addEventListener('click', () => { void saveKey(false); });
    $('llm-key-clear').addEventListener('click', () => { void saveKey(true); });
    $('llm-key').addEventListener('keydown', (event) => { if (event.key === 'Enter') { event.preventDefault(); void saveKey(false); } });
    $('llm-codex-login').addEventListener('click', () => { void startCodexLogin(); });
    $('llm-codex-logout').addEventListener('click', () => { void codexLogout(); });
    $('llm-codex-copy').addEventListener('click', () => { void copyCodexCode(); });
    $('schedule-form').addEventListener('submit', (event) => { void saveSchedule(event); });
    $('schedule-form').addEventListener('input', () => { state.scheduleDirty = true; setStatus('schedule-form-status', 'Unsaved changes'); });
    $('schedule-form').addEventListener('change', () => { state.scheduleDirty = true; updateScheduleControls(); });
    $('sch-collect-add').addEventListener('click', () => {
      if ($('sch-collect-list').children.length >= MAX_COLLECT_TIMES) return;
      const row = timeRow('');
      $('sch-collect-list').append(row);
      updateTimeState();
      state.scheduleDirty = true;
      row.querySelector('input').focus();
    });
    $('sch-collect-list').addEventListener('click', (event) => {
      const remove = event.target.closest('button[data-action="remove-time"]');
      if (!remove) return;
      const row = remove.closest('li');
      const next = row.nextElementSibling || row.previousElementSibling;
      row.remove();
      updateTimeState();
      state.scheduleDirty = true;
      setStatus('schedule-form-status', 'Unsaved changes');
      ((next && next.querySelector('input')) || $('sch-collect-add')).focus();
    });

    // Global
    document.addEventListener('keydown', (event) => {
      if (event.key !== '/' || event.ctrlKey || event.metaKey || event.altKey || $('job-dialog').open || $('confirm-dialog').open) return;
      const target = event.target;
      if (target && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName))) return;
      event.preventDefault();
      if (!isJobView()) changeView('feed');
      $('search-input').focus();
    });
    document.addEventListener('visibilitychange', () => { if (!document.hidden) void poll(); });
    window.addEventListener('online', () => { void poll(); });
    window.addEventListener('popstate', () => {
      const view = viewFromQuery(window.location.search);
      if (view !== state.view) changeView(view, { push: false });
      const id = new URLSearchParams(window.location.search).get('job');
      if (id) void openDetail(id, { syncUrl: false });
      else if ($('job-dialog').open) closeDetail();
    });

    // --- Start ---------------------------------------------------------------------------------

    renderOwner();
    const initialView = viewFromQuery(window.location.search);
    const initialJob = new URLSearchParams(window.location.search).get('job');
    changeView(initialView, { push: false, initial: true });
    const boot = [loadStats()];
    if (initialView !== 'profile') boot.push(loadProfile({ quiet: true }));
    if (initialView !== 'sources') boot.push(loadSources({ quiet: true }));
    if (initialView !== 'settings') boot.push(loadSettings({ quiet: true }));
    void Promise.all(boot).finally(() => schedulePoll());
    if (initialJob) void openDetail(initialJob, { syncUrl: false });
  }
})();
