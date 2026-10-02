/* NetSanctum shared calendar core (planner + vault mini-calendar).
 *
 * Dependency-free vanilla JS. Idempotent: safe to (re-)execute on every
 * htmx navigation into a dashboard. All styling lives in per-template
 * <style> blocks under the `ncal-*` class namespace, so no Tailwind
 * rebuild is needed when this file changes.
 */
(function () {
  'use strict';
  if (window.NetSanctumCalendar && window.NetSanctumCalendar.__v === 1) return;

  var DOW_RU = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'];
  var MONTHS_RU = [
    'Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь',
    'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь',
  ];

  var SPACE_HEX = {
    teal: '#2dd4bf', cyan: '#22d3ee', blue: '#60a5fa', violet: '#a78bfa',
    purple: '#c084fc', pink: '#f472b6', rose: '#fb7185', red: '#f87171',
    orange: '#fb923c', amber: '#fbbf24', yellow: '#facc15', lime: '#a3e635',
    green: '#4ade80', emerald: '#34d399', slate: '#94a3b8', gray: '#9ca3af',
    zinc: '#a1a1aa',
  };
  var FOLDER_PALETTE = ['#60a5fa', '#c084fc', '#f472b6', '#fbbf24', '#4ade80', '#22d3ee'];

  function escapeHtml(value) {
    return String(value == null ? '' : value).replace(/[&<>'"]/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[ch];
    });
  }

  function parseISO(raw) {
    if (!raw) return null;
    var d = new Date(raw);
    return isNaN(d.getTime()) ? null : d;
  }

  function pad2(n) { return (n < 10 ? '0' : '') + n; }

  function dayKey(d) {
    return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate());
  }

  function fromKey(key) {
    var parts = String(key).split('-');
    return new Date(parseInt(parts[0], 10), parseInt(parts[1], 10) - 1, parseInt(parts[2], 10));
  }

  function startOfDay(d) {
    return new Date(d.getFullYear(), d.getMonth(), d.getDate());
  }

  function addDays(d, n) {
    var out = new Date(d.getFullYear(), d.getMonth(), d.getDate());
    out.setDate(out.getDate() + n);
    return out;
  }

  function addMonths(d, n) {
    var out = new Date(d.getFullYear(), d.getMonth() + n, 1);
    return out;
  }

  /** Monday-first 6x7 grid covering the cursor month. Returns 42 Dates. */
  function monthWeeks(cursor) {
    var first = new Date(cursor.getFullYear(), cursor.getMonth(), 1);
    var lead = (first.getDay() + 6) % 7; // Mon=0..Sun=6
    var gridStart = addDays(first, -lead);
    var weeks = [];
    for (var i = 0; i < 42; i++) weeks.push(addDays(gridStart, i));
    return weeks;
  }

  /** Monday..Sunday week containing the cursor date. */
  function weekDays(cursor) {
    var dow = (cursor.getDay() + 6) % 7;
    var monday = addDays(cursor, -dow);
    var days = [];
    for (var i = 0; i < 7; i++) days.push(addDays(monday, i));
    return days;
  }

  function monthTitle(cursor) {
    return MONTHS_RU[cursor.getMonth()] + ' ' + cursor.getFullYear();
  }

  function weekTitle(days) {
    var months = {};
    days.forEach(function (d) { months[d.getMonth()] = true; });
    var names = Object.keys(months).map(function (m) { return MONTHS_RU[parseInt(m, 10)]; });
    var label = names.join(' — ');
    return label + ' ' + days[6].getFullYear();
  }

  function fmtTime(d) {
    if (!d) return '';
    return pad2(d.getHours()) + ':' + pad2(d.getMinutes());
  }

  function fmtDateLong(key) {
    var d = fromKey(key);
    return d.toLocaleDateString('ru-RU', { weekday: 'long', day: 'numeric', month: 'long' });
  }

  function hashInt(str) {
    var h = 0;
    var s = String(str == null ? '' : str);
    for (var i =  0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
    return Math.abs(h);
  }

  /** Resolve a pill color: vault collection color wins, folders hash, inbox gray. */
  function spaceColor(kind, id, name, collectionsById) {
    if ((kind === 'collection') && collectionsById && collectionsById[id]) {
      var c = String(collectionsById[id].color || 'teal').toLowerCase();
      if (SPACE_HEX[c]) return SPACE_HEX[c];
    }
    if (kind === 'folder') {
      return FOLDER_PALETTE[hashInt('folder:' + id + ':' + name) % FOLDER_PALETTE.length];
    }
    if (kind && kind !== 'none') {
      var keys = Object.keys(SPACE_HEX);
      return SPACE_HEX[keys[hashInt(kind + ':' + id + ':' + name) % keys.length]];
    }
    return '#71717a';
  }

  function isAllDayEvent(ev) {
    var s = parseISO(ev.starts_at);
    if (!s) return true;
    if (s.getHours() !== 0 || s.getMinutes() !== 0) return false;
    if (!ev.ends_at) return true;
    var e = parseISO(ev.ends_at);
    if (!e) return true;
    return (e - s) >= 20 * 3600 * 1000;
  }

  function eventDayKeys(ev) {
    // Multi-day events paint a pill in every covered cell (inclusive).
    var s = parseISO(ev.starts_at);
    if (!s) return [];
    var e = parseISO(ev.ends_at) || s;
    if (e < s) e = s;
    var keys = [];
    var cur = startOfDay(s);
    var last = startOfDay(e);
    // An event ending exactly at midnight belongs to the previous day.
    if (+e === +last && +e !== +s && e.getHours() === 0 && e.getMinutes() === 0) {
      last = addDays(last, -1);
    }
    var guard = 0;
    while (cur <= last && guard < 366) {
      keys.push(dayKey(cur));
      cur = addDays(cur, 1);
      guard++;
    }
    return keys;
  }

  function groupByDay(events, tasks) {
    var byDay = {};
    function bucket(key) {
      if (!byDay[key]) byDay[key] = { events: [], tasks: [] };
      return byDay[key];
    }
    (events || []).forEach(function (ev) {
      eventDayKeys(ev).forEach(function (key) { bucket(key).events.push(ev); });
    });
    (tasks || []).forEach(function (t) {
      var d = parseISO(t.due_at);
      if (!d) return;
      bucket(dayKey(d)).tasks.push(t);
    });
    Object.keys(byDay).forEach(function (key) {
      byDay[key].events.sort(function (a, b) {
        return String(a.starts_at) < String(b.starts_at) ? -1 : 1;
      });
      byDay[key].tasks.sort(function (a, b) {
        var da = parseISO(a.due_at), db = parseISO(b.due_at);
        return (da || 0) - (db || 0);
      });
    });
    return byDay;
  }

  function pillTitle(kind, item) {
    var bits = [item.title || ''];
    if (kind === 'event') {
      var s = parseISO(item.starts_at);
      if (s) bits.push(fmtTime(s));
      if (item.location) bits.push(item.location);
    } else {
      var d = parseISO(item.due_at);
      if (d) bits.push('срок ' + fmtTime(d));
    }
    if (item.space_name) bits.push('[' + item.space_name + ']');
    if (item.recurrence && item.recurrence !== 'none') bits.push('↻ ' + item.recurrence);
    return bits.filter(Boolean).join(' · ');
  }

  function pillHTML(kind, item, hex, opts) {
    opts = opts || {};
    var time = '';
    if (kind === 'event') {
      var s = parseISO(item.starts_at);
      if (s && !isAllDayEvent(item) && !opts.hideTime) time = fmtTime(s);
    } else {
      var d = parseISO(item.due_at);
      if (d && !opts.hideTime) time = fmtTime(d);
    }
    var flags = '';
    if (item.recurrence && item.recurrence !== 'none') flags += '<span class="ncal-recur" title="Повторяется">↻</span>';
    if (kind === 'task' && item.status === 'done') flags += '<span title="Выполнено">✓</span>';
    var doneBtn = '';
    if (kind === 'task' && item.status !== 'done' && opts.withComplete) {
      doneBtn = '<button class="ncal-done" data-complete="' + item.id + '" title="Готово">✓</button>';
    }
    var overdue = '';
    if (kind === 'task' && item.status !== 'done' && item.due_at) {
      var due = parseISO(item.due_at);
      if (due && due < new Date()) overdue = ' ncal-overdue';
    }
    var cls = kind === 'event' ? 'ncal-pill ncal-event' : 'ncal-pill ncal-task' + overdue;
    if (kind === 'task' && item.status === 'done') cls += ' ncal-task-done';
    return '<div class="' + cls + '" draggable="true" data-kind="' + kind + '" data-id="' + item.id +
      '" style="--pill:' + escapeHtml(hex) + '" title="' + escapeHtml(pillTitle(kind, item)) + '">' +
      doneBtn +
      (time ? '<span class="ncal-time">' + escapeHtml(time) + '</span>' : '') +
      '<span class="ncal-title">' + escapeHtml(item.title || '') + '</span>' + flags + '</div>';
  }

  /**
   * Render a month grid into `container`.
   * opts: {weeks, byDay, cursor, selectedKey, compact, maxPills,
   *        showEvents, showTasks, collectionsById, withComplete}
   */
  function renderMonth(container, opts) {
    var weeks = opts.weeks;
    var byDay = opts.byDay || {};
    var cursorMonth = opts.cursor.getMonth();
    var todayKey = dayKey(new Date());
    var maxPills = opts.compact ? 2 : (opts.maxPills || 3);
    var html = '<div class="ncal-dowrow">' + DOW_RU.map(function (d, i) {
      return '<div class="ncal-dow' + (i >= 5 ? ' ncal-weekend-head' : '') + '">' + d + '</div>';
    }).join('') + '</div><div class="ncal-grid">';
    weeks.forEach(function (date) {
      var key = dayKey(date);
      var cell = byDay[key] || { events: [], tasks: [] };
      var items = [];
      if (opts.showEvents !== false) {
        cell.events.forEach(function (ev) {
          items.push({ kind: 'event', item: ev, at: parseISO(ev.starts_at) || new Date(0) });
        });
      }
      if (opts.showTasks !== false) {
        cell.tasks.forEach(function (t) {
          items.push({ kind: 'task', item: t, at: parseISO(t.due_at) || new Date(0) });
        });
      }
      items.sort(function (a, b) { return a.at - b.at; });
      var pills = items.slice(0, maxPills).map(function (entry) {
        var it = entry.item;
        var hex = spaceColor(it.space_kind, it.space_id, it.space_name, opts.collectionsById);
        return pillHTML(entry.kind, it, hex, { withComplete: opts.withComplete });
      }).join('');
      var more = '';
      if (items.length > maxPills) {
        more = '<button class="ncal-more" data-more="' + key + '">+ещё ' + (items.length - maxPills) + '</button>';
      }
      var cls = 'ncal-cell';
      if (date.getMonth() !== cursorMonth) cls += ' ncal-outside';
      if (key === todayKey) cls += ' ncal-today';
      if (key === opts.selectedKey) cls += ' ncal-selected';
      var dow = date.getDay();
      if (dow === 0 || dow === 6) cls += ' ncal-weekend';
      html += '<div class="' + cls + '" data-date="' + key + '">' +
        '<div class="ncal-dayrow"><span class="ncal-daynum">' + date.getDate() + '</span>' +
        '<button class="ncal-add" data-add="' + key + '" title="Создать в этот день">+</button></div>' +
        '<div class="ncal-pills">' + pills + '</div>' + more + '</div>';
    });
    container.innerHTML = html + '</div>';
  }

  /** HTML5 drag-and-drop: pills -> day cells. onMove(kind, id, targetKey). */
  function wireMonthDrop(container, onMove) {
    if (container.dataset.ncalDropBound) return;
    container.dataset.ncalDropBound = '1';
    var dragPayload = null;
    container.addEventListener('dragstart', function (e) {
      var pill = e.target.closest ? e.target.closest('.ncal-pill') : null;
      if (!pill || !container.contains(pill)) return;
      dragPayload = { kind: pill.dataset.kind, id: pill.dataset.id };
      try { e.dataTransfer.setData('text/plain', pill.dataset.kind + ':' + pill.dataset.id); } catch (err) {}
      e.dataTransfer.effectAllowed = 'move';
    });
    container.addEventListener('dragend', function () {
      dragPayload = null;
      container.querySelectorAll('.ncal-drop-hint').forEach(function (el) {
        el.classList.remove('ncal-drop-hint');
      });
    });
    container.addEventListener('dragover', function (e) {
      var cell = e.target.closest ? e.target.closest('.ncal-cell') : null;
      if (!cell || !container.contains(cell)) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      cell.classList.add('ncal-drop-hint');
    });
    container.addEventListener('dragleave', function (e) {
      var cell = e.target.closest ? e.target.closest('.ncal-cell') : null;
      if (cell && (!e.relatedTarget || !cell.contains(e.relatedTarget))) {
        cell.classList.remove('ncal-drop-hint');
      }
    });
    container.addEventListener('drop', function (e) {
      var cell = e.target.closest ? e.target.closest('.ncal-cell') : null;
      if (!cell || !container.contains(cell)) return;
      e.preventDefault();
      cell.classList.remove('ncal-drop-hint');
      var payload = dragPayload;
      if (!payload) {
        try {
          var raw = e.dataTransfer.getData('text/plain').split(':');
          payload = { kind: raw[0], id: raw[1] };
        } catch (err) { payload = null; }
      }
      if (payload && payload.id && onMove) onMove(payload.kind, payload.id, cell.dataset.date);
    });
  }

  function isoWithOffset(d) {
    var off = -d.getTimezoneOffset();
    var sign = off >= 0 ? '+' : '-';
    var abs = Math.abs(off);
    return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) +
      'T' + pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':00' +
      sign + pad2(Math.floor(abs / 60)) + ':' + pad2(abs % 60);
  }

  /** Move an item to another day, preserving its wall-clock time. */
  function shiftedToDay(originalISO, targetKey) {
    var src = parseISO(originalISO) || new Date();
    var day = fromKey(targetKey);
    var out = new Date(day.getFullYear(), day.getMonth(), day.getDate(),
      src.getHours(), src.getMinutes(), 0, 0);
    return isoWithOffset(out);
  }

  function rangeOfWeeks(weeks) {
    var first = weeks[0], last = weeks[weeks.length - 1];
    return {
      from: isoWithOffset(new Date(first.getFullYear(), first.getMonth(), first.getDate(), 0, 0, 0)),
      to: isoWithOffset(new Date(last.getFullYear(), last.getMonth(), last.getDate(), 23, 59, 59)),
    };
  }

  function rangeOfDays(days) {
    var first = days[0], last = days[days.length - 1];
    return {
      from: isoWithOffset(new Date(first.getFullYear(), first.getMonth(), first.getDate(), 0, 0, 0)),
      to: isoWithOffset(new Date(last.getFullYear(), last.getMonth(), last.getDate(), 23, 59, 59)),
    };
  }

  function calendarUrl(params) {
    var q = new URLSearchParams();
    if (params.from) q.set('from', params.from);
    if (params.to) q.set('to', params.to);
    if (params.space_kind) q.set('space_kind', params.space_kind);
    if (params.space_id != null) q.set('space_id', params.space_id);
    if (params.include_done_tasks === false) q.set('include_done_tasks', 'false');
    return '/api/planner/calendar?' + q.toString();
  }

  function fetchCalendar(params) {
    return fetch(calendarUrl(params)).then(function (r) {
      if (!r.ok) throw new Error('Календарь: HTTP ' + r.status);
      return r.json();
    });
  }

  function apiCall(url, method, body) {
    return fetch(url, {
      method: method,
      headers: { 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : undefined,
    }).then(function (r) {
      if (!r.ok) return r.json().catch(function () { return {}; }).then(function (payload) {
        throw new Error(payload.detail || ('Ошибка: ' + r.status));
      });
      return r.json();
    });
  }

  function fetchCollections() {
    return fetch('/api/vault/collections').then(function (r) {
      if (!r.ok) throw new Error('Пространства: HTTP ' + r.status);
      return r.json();
    }).catch(function () { return []; });
  }

  function fetchFolders() {
    return fetch('/api/vault/items?node_type=folder&limit=500').then(function (r) {
      if (!r.ok) throw new Error('Папки: HTTP ' + r.status);
      return r.json();
    }).catch(function () { return []; });
  }

  function collectionsMap(collections) {
    var map = {};
    (collections || []).forEach(function (c) { map[c.id] = c; });
    return map;
  }

  window.NetSanctumCalendar = {
    __v: 1,
    DOW_RU: DOW_RU,
    MONTHS_RU: MONTHS_RU,
    SPACE_HEX: SPACE_HEX,
    escapeHtml: escapeHtml,
    parseISO: parseISO,
    dayKey: dayKey,
    fromKey: fromKey,
    startOfDay: startOfDay,
    addDays: addDays,
    addMonths: addMonths,
    monthWeeks: monthWeeks,
    weekDays: weekDays,
    monthTitle: monthTitle,
    weekTitle: weekTitle,
    fmtTime: fmtTime,
    fmtDateLong: fmtDateLong,
    spaceColor: spaceColor,
    isAllDayEvent: isAllDayEvent,
    eventDayKeys: eventDayKeys,
    groupByDay: groupByDay,
    pillHTML: pillHTML,
    renderMonth: renderMonth,
    wireMonthDrop: wireMonthDrop,
    isoWithOffset: isoWithOffset,
    shiftedToDay: shiftedToDay,
    rangeOfWeeks: rangeOfWeeks,
    rangeOfDays: rangeOfDays,
    fetchCalendar: fetchCalendar,
    apiCall: apiCall,
    fetchCollections: fetchCollections,
    fetchFolders: fetchFolders,
    collectionsMap: collectionsMap,
  };
})();
