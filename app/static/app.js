'use strict';
/* Sonnen-Abgleich - single page UI. No build step: plain JS + ECharts. */

// ---------- small helpers -------------------------------------------------------
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'value' || k === 'checked' || k === 'disabled' || k === 'hidden') el[k] = v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

const nf = d => new Intl.NumberFormat('de-DE', { minimumFractionDigits: d, maximumFractionDigits: d });
const n0 = nf(0), n1 = nf(1);
const num = (v, d = 1) => (v == null || Number.isNaN(v) ? '–' : nf(d).format(v));
const kwh = wh => { const v = wh / 1000; return (Math.abs(v) >= 100 ? n0 : n1).format(v); };
const pct = v => (v == null || !Number.isFinite(v) ? '–' : n0.format(v * 100) + ' %');
const dDE = iso => (iso ? `${iso.slice(8, 10)}.${iso.slice(5, 7)}.${iso.slice(0, 4)}` : '–');
const dShort = iso => `${iso.slice(8, 10)}.${iso.slice(5, 7)}.${iso.slice(2, 4)}`;
const dtDE = iso => (iso ? `${dDE(iso)} ${iso.slice(11, 16)}` : '–');
const dtShort = iso => `${dShort(iso)} ${iso.slice(11, 16)}`;
const MONTHS = ['Jan', 'Feb', 'Mär', 'Apr', 'Mai', 'Jun', 'Jul', 'Aug', 'Sep', 'Okt', 'Nov', 'Dez'];
const WDAYS = ['So', 'Mo', 'Di', 'Mi', 'Do', 'Fr', 'Sa'];
const isoDay = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
const toDate = iso => new Date(+iso.slice(0, 4), +iso.slice(5, 7) - 1, +iso.slice(8, 10));
const addDays = (iso, n) => { const d = toDate(iso); d.setDate(d.getDate() + n); return isoDay(d); };

/** "12.345,6" / "12345.6" / "12345" -> number; '' -> null; garbage -> NaN */
function parseNum(str) {
  let s = String(str ?? '').trim().replace(/\s/g, '');
  if (!s) return null;
  if (s.includes(',')) s = s.replace(/\./g, '').replace(',', '.');
  else if (/^\d{1,3}(\.\d{3})+$/.test(s)) s = s.replace(/\./g, '');
  return /^\d+(\.\d+)?$/.test(s) ? Number(s) : NaN;
}

function toast(msg) {
  const t = $('#toast');
  t.textContent = msg; t.classList.add('show');
  clearTimeout(toast.timer); toast.timer = setTimeout(() => t.classList.remove('show'), 3500);
}

async function api(path, opt = {}) {
  const init = { method: opt.method || 'GET', headers: {} };
  if (opt.json !== undefined) { init.body = JSON.stringify(opt.json); init.headers['Content-Type'] = 'application/json'; }
  if (opt.body !== undefined) { init.body = opt.body; init.headers['Content-Type'] = 'text/csv'; }
  const res = await fetch(path, init);
  if (res.status === 401 && path !== '/api/login') { showLogin(); throw new Error('Bitte anmelden.'); }
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) {
    const d = data && data.detail;
    throw new Error(typeof d === 'string' ? d : Array.isArray(d) ? d.map(x => x.msg).join('; ') : `Fehler ${res.status}`);
  }
  return data;
}

// ---------- state ---------------------------------------------------------------
const S = {
  tab: 'overview', gran: 'month', preset: 'all', from: null, to: null,
  days: [], ov: null, cmp: null, calMetric: 'purchase', detailDay: null, sort: { key: 'key', dir: 1 },
};
let C = {};
function readColors() {
  const cs = getComputedStyle(document.documentElement);
  const v = n => cs.getPropertyValue(n).trim();
  C = {
    surface: v('--surface'), text: v('--text'), text2: v('--text-2'), muted: v('--muted'), gridc: v('--grid'), axis: v('--axis'),
    border: v('--border'), grid: v('--s-grid'), feedin: v('--s-feedin'), battery: v('--s-battery'), pv: v('--s-pv'),
    load: v('--s-load'), meter: v('--s-meter'), est: v('--s-est'), nodata: v('--s-nodata'),
    seqLo: v('--seq-lo'), seqHi: v('--seq-hi'), yr: [v('--yr-0'), v('--yr-1'), v('--yr-2')],
  };
}
const FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif';

// ---------- data ----------------------------------------------------------------
async function loadData() {
  const [ov, t] = await Promise.all([api('/api/overview'), api('/api/daily')]);
  S.ov = ov;
  S.days = t.day.map((day, i) => {
    const d = {
      day, produced: t.produced[i], consumed: t.consumed[i], charged: t.bat_charged[i], discharged: t.bat_discharged[i],
      feedin: t.feedin[i], purchase: t.purchase[i], cov: t.cov[i], base: t.base_w[i], soc: t.soc_max[i],
      xc: t.x_consumed[i], xp: t.x_purchase[i],
    };
    d.fromPv = Math.max(0, d.consumed - d.purchase - d.discharged);      // consumption covered directly by PV
    d.selfUse = Math.max(0, d.produced - d.feedin);                      // production used in the house or stored
    return d;
  });
  if (!S.from && S.days.length) setPreset('all');
}

function setPreset(p) {
  S.preset = p;
  const first = S.days[0]?.day, last = S.days[S.days.length - 1]?.day;
  if (!first) return;
  if (p === 'all') { S.from = first; S.to = last; }
  else if (p === '12m') { S.from = addDays(last, -364); S.to = last; }
  else if (p === '30d') { S.from = addDays(last, -29); S.to = last; }
  else if (/^\d{4}$/.test(p)) { S.from = `${p}-01-01`; S.to = `${p}-12-31`; }
  if (S.from < first) S.from = first;
  if (S.to > last) S.to = last;
}
const inRange = () => S.days.filter(d => d.day >= S.from && d.day <= S.to);
const years = () => [...new Set(S.days.map(d => d.day.slice(0, 4)))];

function mondayOf(iso) { const d = toDate(iso); d.setDate(d.getDate() - ((d.getDay() + 6) % 7)); return isoDay(d); }
function isoWeek(iso) {
  const d = toDate(iso); d.setDate(d.getDate() + 3 - ((d.getDay() + 6) % 7));
  const w1 = new Date(d.getFullYear(), 0, 4);
  return 1 + Math.round(((d - w1) / 864e5 - 3 + ((w1.getDay() + 6) % 7)) / 7);
}
const SUMS = ['produced', 'consumed', 'charged', 'discharged', 'feedin', 'purchase', 'fromPv', 'selfUse'];
function aggregate(days, gran) {
  const map = new Map();
  for (const d of days) {
    const key = gran === 'day' ? d.day : gran === 'week' ? mondayOf(d.day) : gran === 'year' ? d.day.slice(0, 4) : d.day.slice(0, 7);
    let b = map.get(key);
    if (!b) {
      b = { key, n: 0, missing: 0, baseSum: 0, baseN: 0, from: d.day, to: d.day };
      for (const f of SUMS) b[f] = 0;
      if (gran === 'day') { b.label = `${d.day.slice(8, 10)}.${d.day.slice(5, 7)}.`; b.title = `${WDAYS[toDate(d.day).getDay()]}, ${dDE(d.day)}`; }
      else if (gran === 'week') { b.label = `KW ${isoWeek(d.day)}/${key.slice(2, 4)}`; }
      else if (gran === 'year') { b.label = key; b.title = key; }
      else { b.label = `${MONTHS[+key.slice(5, 7) - 1]} ${key.slice(2, 4)}`; b.title = `${MONTHS[+key.slice(5, 7) - 1]} ${key.slice(0, 4)}`; }
      map.set(key, b);
    }
    b.n++; b.to = d.day; b.missing += 1 - d.cov;
    for (const f of SUMS) b[f] += d[f];
    if (d.base != null) { b.baseSum += d.base; b.baseN++; }
  }
  const out = [...map.values()];
  for (const b of out) {
    if (gran === 'week') b.title = `${b.label} (${dDE(b.from)} – ${dDE(b.to)})`;
    b.base = b.baseN ? b.baseSum / b.baseN : null;
    b.autarky = b.consumed > 0 ? 1 - b.purchase / b.consumed : null;
  }
  return out;
}

// ---------- chart plumbing --------------------------------------------------------
const live = new Set();
function draw(el, option) {
  const chart = echarts.init(el, null, { renderer: 'canvas' });
  chart.setOption(option);
  live.add(chart);
  return chart;
}
function disposeCharts() { for (const c of live) c.dispose(); live.clear(); }
window.addEventListener('resize', () => { for (const c of live) c.resize(); });

const axCat = (data, extra = {}) => ({
  type: 'category', data, axisLine: { lineStyle: { color: C.axis } }, axisTick: { show: false },
  axisLabel: { color: C.muted, hideOverlap: true, fontSize: 11 }, ...extra,
});
const axVal = (name, extra = {}) => ({
  type: 'value', name, nameGap: 12, nameTextStyle: { color: C.muted, align: 'left', fontSize: 11, padding: [0, 0, 0, -28] },
  axisLabel: { color: C.muted, fontSize: 11, formatter: v => n0.format(v) }, splitLine: { lineStyle: { color: C.gridc } }, ...extra,
});
function baseOpt(legend = true) {
  return {
    animation: false, textStyle: { fontFamily: FONT },
    grid: { left: 4, right: 12, top: legend ? (innerWidth < 640 ? 76 : 48) : 30, bottom: 4, containLabel: true },
    legend: legend ? { top: 0, left: 0, icon: 'roundRect', itemWidth: 10, itemHeight: 10, itemGap: 16, textStyle: { color: C.text2, fontSize: 12 } } : undefined,
    tooltip: {
      trigger: 'axis', axisPointer: { type: 'shadow', shadowStyle: { color: C.border } }, confine: true,
      backgroundColor: C.surface, borderColor: C.axis, borderWidth: 1, padding: [8, 10], textStyle: { color: C.text, fontSize: 12, fontFamily: FONT },
    },
  };
}
/** Tooltip body for bucketed charts: every series value, optional total, and a data-gap note. */
function tip(buckets, unit = 'kWh', total = null) {
  return params => {
    const b = buckets[params[0].dataIndex];
    let s = `<div style="font-weight:650;margin-bottom:4px">${b.title || b.label}</div>`, sum = 0;
    for (const p of params) {
      const v = Array.isArray(p.value) ? p.value[1] : p.value;
      if (v == null) continue;
      sum += v;
      s += `<div style="display:flex;justify-content:space-between;gap:18px"><span>${p.marker}${p.seriesName}</span><b>${n1.format(v)} ${unit}</b></div>`;
    }
    if (total) s += `<div style="display:flex;justify-content:space-between;gap:18px;border-top:1px solid ${C.gridc};margin-top:4px;padding-top:4px"><span>${total}</span><b>${n1.format(sum)} ${unit}</b></div>`;
    if (b.missing > 0.05) s += `<div style="color:${C.muted};margin-top:4px">${n1.format(b.missing)} von ${b.n} Tagen ohne sonnen-Daten</div>`;
    return s;
  };
}
const barSeries = (name, color, data, stack, top = false) => ({
  name, type: 'bar', stack, data, barMaxWidth: 24, barCategoryGap: '28%',
  itemStyle: { color, borderRadius: top ? [3, 3, 0, 0] : 0, borderColor: C.surface, borderWidth: data.length <= 70 ? 1 : 0 },
  emphasis: { disabled: true },
});
/** Shade stretches where sonnen recorded nothing. */
function gapAreas(buckets) {
  const areas = []; let start = null;
  buckets.forEach((b, i) => {
    const gap = b.missing / b.n > 0.5;
    if (gap && start == null) start = i;
    if ((!gap || i === buckets.length - 1) && start != null) {
      const end = gap ? i : i - 1;
      areas.push([{ xAxis: buckets[start].label, name: end - start >= 1 || buckets.length < 40 ? 'keine Daten' : '' }, { xAxis: buckets[end].label }]);
      start = null;
    }
  });
  if (!areas.length) return undefined;
  return { silent: true, itemStyle: { color: C.nodata, opacity: 0.55 }, label: { color: C.text2, fontSize: 11, position: 'insideTop' }, data: areas };
}
const zoom = n => (n > 80 ? [{ type: 'inside' }, {
  type: 'slider', height: 18, bottom: 4, borderColor: C.gridc, fillerColor: C.border, handleSize: '100%',
  textStyle: { color: C.muted, fontSize: 10 }, dataBackground: { lineStyle: { color: C.axis }, areaStyle: { color: C.gridc } },
}] : undefined);
const withZoom = (opt, n) => { const z = zoom(n); if (z) { opt.dataZoom = z; opt.grid.bottom = 34; } return opt; };

function card(title, sub, ...body) {
  return h('section', { class: 'card' }, h('header', {}, h('div', {}, h('h2', {}, title), sub && h('p', { class: 'sub' }, sub))), ...body);
}
function tile(label, value, unit, note, color) {
  return h('div', { class: 'tile' },
    h('div', { class: 'k' }, color && h('span', { class: 'dot', style: `background:${color}` }), label),
    h('div', { class: 'v' }, value, unit && h('small', {}, unit)),
    note && h('div', { class: 'n' }, note));
}

// ---------- shared: gap notices -----------------------------------------------------
function gapNotices() {
  return (S.ov.gaps || []).filter(g => g.missing_days >= 0.5).map(g => {
    const e = g.est_kwh;
    return h('div', { class: 'notice' }, h('span', { class: 'ico', 'aria-hidden': 'true' }, '!'), h('div', {},
      h('p', {}, h('strong', {}, `sonnen hat vom ${dDE(g.from)} bis ${dDE(g.to)} nichts aufgezeichnet `),
        `(${num(g.missing_days, 1)} Tage). Was das Haus in dieser Zeit aus dem Netz bezogen hat, fehlt in der sonnen-App – der offizielle Zähler hat es gezählt.`),
      h('p', {}, `Geschätzter nicht erfasster Netzbezug: etwa ${n0.format(e.imp_mid)} kWh, falls nur der Speicher ausgefallen war und die PV-Anlage weiterlief `
        + `(Spanne ${n0.format(e.imp_lo)} – ${n0.format(e.imp_hi)} kWh; der obere Wert gilt, falls auch die PV-Anlage stand). Grundlage: ${g.reference}.`)));
  });
}

// ---------- overview ---------------------------------------------------------------
function viewOverview() {
  const days = inRange();
  if (!days.length) return [h('div', { class: 'card empty' }, 'Noch keine Daten. Unter „Daten“ können CSV-Exporte aus dem sonnen-Portal importiert werden.')];
  const t = aggregate(days, 'year').reduce((a, b) => { for (const f of [...SUMS, 'missing', 'n']) a[f] = (a[f] || 0) + b[f]; return a; }, {});
  const months = aggregate(days, 'month');
  const out = [...gapNotices()];

  out.push(h('div', { class: 'tiles' },
    tile('Netzbezug', kwh(t.purchase), 'kWh', `${num(t.purchase / 1000 / Math.max(1, t.n - t.missing), 1)} kWh pro Tag`, C.grid),
    tile('Einspeisung', kwh(t.feedin), 'kWh', null, C.feedin),
    tile('Erzeugung', kwh(t.produced), 'kWh', null, C.pv),
    tile('Verbrauch', kwh(t.consumed), 'kWh', `${num(t.consumed / 1000 / Math.max(1, t.n - t.missing), 1)} kWh pro Tag`, C.load),
    tile('Autarkie', pct(t.consumed ? 1 - t.purchase / t.consumed : null), null, 'Anteil des Verbrauchs ohne Netz'),
    tile('Eigenverbrauch', pct(t.produced ? t.selfUse / t.produced : null), null, 'Anteil der Erzeugung selbst genutzt'),
    tile('Tage ohne Daten', num(t.missing, 1), `von ${t.n}`, t.missing > 0.5 ? 'in allen Summen nicht enthalten' : 'lückenlos')));

  // Grid purchase per month, one bar per year
  const ys = [...new Set(days.map(d => d.day.slice(0, 4)))];
  const allYs = years();
  const yrColor = y => {
    const i = allYs.indexOf(y), n = allYs.length;
    return n <= 3 ? C.yr[3 - n + i] ?? C.yr[i] : C.yr[Math.min(2, Math.round(i * 2 / (n - 1)))];
  };
  const byKey = new Map(months.map(m => [m.key, m]));
  const cmpBuckets = MONTHS.map((m, i) => ({
    label: m, title: m, n: 1, missing: 0,
    note: ys.map(y => byKey.get(`${y}-${String(i + 1).padStart(2, '0')}`)).filter(b => b && b.missing > 0.5).map(b => `${b.title}: ${n1.format(b.missing)} Tage ohne Daten`),
  }));
  const elCmp = h('div', { class: 'chart' });
  const optCmp = baseOpt();
  optCmp.xAxis = axCat(MONTHS); optCmp.yAxis = axVal('kWh');
  optCmp.tooltip.formatter = params => tip(cmpBuckets)(params) + cmpBuckets[params[0].dataIndex].note.map(n => `<div style="color:${C.muted}">${n}</div>`).join('');
  optCmp.series = ys.map(y => ({
    name: y, type: 'bar', barMaxWidth: 18, barGap: '12%', emphasis: { disabled: true },
    itemStyle: { color: yrColor(y), borderRadius: [3, 3, 0, 0] },
    data: MONTHS.map((_, i) => {
      const b = byKey.get(`${y}-${String(i + 1).padStart(2, '0')}`);
      if (!b) return null;
      return b.missing > 0.5 ? { value: b.purchase / 1000, itemStyle: { opacity: 0.4 } } : b.purchase / 1000;
    }),
  }));
  const hasGapMonth = months.some(b => b.missing > 0.5);

  // Where did the consumed energy come from
  const elSrc = h('div', { class: 'chart' });
  const optSrc = baseOpt();
  optSrc.xAxis = axCat(months.map(b => b.label)); optSrc.yAxis = axVal('kWh');
  optSrc.tooltip.formatter = tip(months, 'kWh', 'Verbrauch');
  optSrc.series = [
    barSeries('direkt von der PV', C.pv, months.map(b => b.fromPv / 1000), 's'),
    barSeries('aus dem Speicher', C.battery, months.map(b => b.discharged / 1000), 's'),
    barSeries('aus dem Netz', C.grid, months.map(b => b.purchase / 1000), 's', true),
  ];
  optSrc.series[0].markArea = gapAreas(months);

  // Calendar
  const metrics = { purchase: 'Netzbezug', consumed: 'Verbrauch', produced: 'Erzeugung' };
  const elCal = h('div', { class: 'chart', style: `height:${56 + ys.length * 150}px` });
  const vals = days.filter(d => d.cov >= 0.999).map(d => d[S.calMetric] / 1000).sort((a, b) => a - b);
  const vmax = Math.max(1, Math.ceil(vals[Math.floor(vals.length * 0.97)] || 1));
  const optCal = {
    animation: false, textStyle: { fontFamily: FONT },
    tooltip: {
      confine: true, backgroundColor: C.surface, borderColor: C.axis, textStyle: { color: C.text, fontSize: 12 },
      formatter: p => `<b>${WDAYS[toDate(p.value[0]).getDay()]}, ${dDE(p.value[0])}</b><br>${p.value[2] == null ? 'keine sonnen-Daten' : `${metrics[S.calMetric]}: <b>${n1.format(p.value[2])} kWh</b>`}`,
    },
    visualMap: [{
      type: 'continuous', min: 0, max: vmax, dimension: 1, orient: 'horizontal', right: 8, top: 0, itemWidth: 10, itemHeight: 120,
      seriesIndex: ys.map((_, i) => i), inRange: { color: [C.seqLo, C.seqHi] }, text: [`${vmax}+ kWh`, '0'], textStyle: { color: C.muted, fontSize: 11 }, calculable: false,
    }, { show: false, type: 'continuous', min: 0, max: 1, dimension: 1, seriesIndex: ys.map((_, i) => ys.length + i), inRange: { color: [C.nodata, C.nodata] } }],
    calendar: ys.map((y, i) => ({
      range: y, top: 62 + i * 150, left: 52, right: 8, cellSize: ['auto', 15],
      splitLine: { show: false }, itemStyle: { color: 'transparent', borderColor: C.surface, borderWidth: 2 },
      dayLabel: { firstDay: 1, nameMap: WDAYS, color: C.muted, fontSize: 10 },
      monthLabel: { nameMap: MONTHS, color: C.muted, fontSize: 11 },
      yearLabel: { show: true, color: C.text2, fontSize: 13, fontWeight: 600, margin: 30 },
    })),
    series: ys.map((y, i) => ({
      type: 'heatmap', coordinateSystem: 'calendar', calendarIndex: i,
      data: days.filter(d => d.day.startsWith(y) && d.cov >= 0.5).map(d => [d.day, Math.min(vmax, d[S.calMetric] / 1000), d[S.calMetric] / 1000]),
    })).concat(ys.map((y, i) => ({   // days without sonnen data, drawn in neutral gray
      type: 'heatmap', coordinateSystem: 'calendar', calendarIndex: i,
      data: days.filter(d => d.day.startsWith(y) && d.cov < 0.5).map(d => [d.day, 0, null]),
    }))),
  };
  const metricSeg = h('div', { class: 'seg' }, Object.entries(metrics).map(([k, v]) =>
    h('button', { class: k === S.calMetric ? 'on' : '', onclick: () => { S.calMetric = k; render(); } }, v)));

  out.push(h('div', { class: 'cols' },
    card('Netzbezug pro Monat im Jahresvergleich', 'Von sonnen aufgezeichneter Strombezug aus dem Netz.', elCmp,
      hasGapMonth && h('div', { class: 'legendline' }, h('span', {}, 'Blasse Balken: Monat mit Datenlücke, tatsächlicher Bezug war höher.'))),
    card('Woher kam der verbrauchte Strom?', 'Monatlicher Verbrauch nach Quelle.', elSrc)));
  const calCard = card('Kalender', 'Jeder Tag ein Feld – ungewöhnliche Tage fallen als dunkle Felder auf. Graue Felder: keine sonnen-Daten.', elCal);
  $('header', calCard).append(metricSeg);
  out.push(calCard);
  queueMicrotask(() => { draw(elCmp, optCmp); draw(elSrc, optSrc); draw(elCal, optCal); });
  return out;
}

// ---------- analysis ---------------------------------------------------------------
function viewAnalysis() {
  const days = inRange();
  if (!days.length) return [h('div', { class: 'card empty' }, 'Keine Daten im gewählten Zeitraum.')];
  const B = aggregate(days, S.gran);
  const labels = B.map(b => b.label);
  const unitTitle = { day: 'Tag', week: 'Woche', month: 'Monat' }[S.gran];
  const out = [];

  const elSrc = h('div', { class: 'chart tall' });
  const o1 = withZoom(baseOpt(), B.length);
  o1.xAxis = axCat(labels); o1.yAxis = axVal('kWh'); o1.tooltip.formatter = tip(B, 'kWh', 'Verbrauch');
  o1.series = [
    barSeries('direkt von der PV', C.pv, B.map(b => b.fromPv / 1000), 's'),
    barSeries('aus dem Speicher', C.battery, B.map(b => b.discharged / 1000), 's'),
    barSeries('aus dem Netz', C.grid, B.map(b => b.purchase / 1000), 's', true),
  ];
  o1.series[0].markArea = gapAreas(B);

  const elUse = h('div', { class: 'chart' });
  const o2 = withZoom(baseOpt(), B.length);
  o2.xAxis = axCat(labels); o2.yAxis = axVal('kWh'); o2.tooltip.formatter = tip(B, 'kWh', 'Erzeugung');
  o2.series = [
    barSeries('direkt verbraucht', C.pv, B.map(b => Math.max(0, b.produced - b.feedin - b.charged) / 1000), 's'),
    barSeries('in den Speicher', C.battery, B.map(b => b.charged / 1000), 's'),
    barSeries('ins Netz eingespeist', C.feedin, B.map(b => b.feedin / 1000), 's', true),
  ];
  o2.series[0].markArea = gapAreas(B);

  const elBase = h('div', { class: 'chart' });
  const o3 = withZoom(baseOpt(false), B.length);
  o3.xAxis = axCat(labels); o3.yAxis = axVal('Watt'); o3.tooltip.formatter = tip(B, 'W'); o3.tooltip.axisPointer = { type: 'line', lineStyle: { color: C.axis } };
  o3.series = [{
    name: 'Grundlast nachts', type: 'line', data: B.map(b => (b.base == null ? null : Math.round(b.base))), connectNulls: false,
    showSymbol: B.length <= 40, symbolSize: 7, lineStyle: { width: 2, color: C.load }, itemStyle: { color: C.load, borderColor: C.surface, borderWidth: 2 },
    markArea: gapAreas(B),
  }];

  out.push(card(`Verbrauch pro ${unitTitle} nach Quelle`, 'Der blaue Anteil ist der Netzbezug – das, was auch der offizielle Zähler zählt.', elSrc));
  out.push(h('div', { class: 'cols' },
    card(`Erzeugung pro ${unitTitle} nach Verwendung`, 'Was mit dem Solarstrom passiert ist.', elUse),
    card('Grundlast in der Nacht', 'Durchschnittliche Leistung des Hauses zwischen 0 und 6 Uhr. Ein dauerhaft höherer Wert deutet auf ein Gerät hin, das durchläuft.', elBase)));

  // Days that stick out
  const odd = days.filter(d => d.xc > 0 || d.xp > 0).sort((a, b) => Math.max(b.xc, b.xp) - Math.max(a.xc, a.xp)).slice(0, 40);
  out.push(card('Auffällige Tage', 'Tage, an denen Verbrauch oder Netzbezug deutlich über dem lagen, was in den vier Wochen drumherum üblich war. Klick auf eine Zeile zeigt den Tagesverlauf.',
    odd.length ? h('div', { class: 'tablewrap scrolly' }, h('table', {},
      h('thead', {}, h('tr', {}, ['Tag', 'Verbrauch', 'üblich', 'mehr als üblich', 'Netzbezug', 'üblich', 'mehr als üblich'].map(x => h('th', {}, x)))),
      h('tbody', {}, odd.map(d => h('tr', { class: 'clickable', onclick: () => openDay(d.day) },
        h('td', {}, `${WDAYS[toDate(d.day).getDay()]}, ${dDE(d.day)}`),
        h('td', {}, kwh(d.consumed) + ' kWh'), h('td', {}, d.xc ? kwh(d.consumed - d.xc) + ' kWh' : '–'),
        h('td', { class: d.xc ? 'pos' : '' }, d.xc ? '+' + kwh(d.xc) + ' kWh' : '–'),
        h('td', {}, kwh(d.purchase) + ' kWh'), h('td', {}, d.xp ? kwh(d.purchase - d.xp) + ' kWh' : '–'),
        h('td', { class: d.xp ? 'pos' : '' }, d.xp ? '+' + kwh(d.xp) + ' kWh' : '–'))))))
      : h('p', { class: 'empty' }, 'Keine auffälligen Tage im gewählten Zeitraum.')));

  // Day profile
  if (!S.detailDay || S.detailDay < S.days[0].day || S.detailDay > S.days[S.days.length - 1].day) S.detailDay = days[days.length - 1].day;
  const elP = h('div', { class: 'chart' }), elSoc = h('div', { class: 'chart short' });
  const dayInput = h('input', { type: 'date', value: S.detailDay, min: S.days[0].day, max: S.days[S.days.length - 1].day, onchange: e => e.target.value && openDay(e.target.value, false) });
  const profile = card('Tagesverlauf', 'Durchschnittliche Leistung je Zeitabschnitt, drei Tage rund um den gewählten Tag.', elP,
    h('h3', { style: 'margin:14px 0 2px' }, 'Ladestand des Speichers'), elSoc);
  profile.id = 'profile';
  $('header', profile).append(h('div', { class: 'row' },
    h('button', { class: 'btn small', onclick: () => openDay(addDays(S.detailDay, -1), false) }, '‹ Tag zurück'),
    h('label', { class: 'field', style: 'width:150px' }, dayInput),
    h('button', { class: 'btn small', onclick: () => openDay(addDays(S.detailDay, 1), false) }, 'Tag vor ›')));
  out.push(profile);

  // Table
  const cols = [
    ['key', 'Zeitraum', b => b.title || b.label], ['consumed', 'Verbrauch', b => kwh(b.consumed)], ['purchase', 'Netzbezug', b => kwh(b.purchase)],
    ['fromPv', 'direkt PV', b => kwh(b.fromPv)], ['discharged', 'aus Speicher', b => kwh(b.discharged)], ['produced', 'Erzeugung', b => kwh(b.produced)],
    ['feedin', 'Einspeisung', b => kwh(b.feedin)], ['charged', 'in Speicher', b => kwh(b.charged)],
    ['eff', 'Speicher-Wirkungsgrad', b => (b.charged > 2000 ? pct(b.discharged / b.charged) : '–')],
    ['autarky', 'Autarkie', b => pct(b.autarky)], ['base', 'Grundlast nachts', b => (b.base == null ? '–' : n0.format(b.base) + ' W')],
    ['missing', 'Tage ohne Daten', b => (b.missing > 0.05 ? n1.format(b.missing) : '')],
  ];
  const sorted = [...B].sort((a, b) => {
    const k = S.sort.key, va = k === 'eff' ? a.discharged / (a.charged || 1) : a[k], vb = k === 'eff' ? b.discharged / (b.charged || 1) : b[k];
    return (va > vb ? 1 : va < vb ? -1 : 0) * S.sort.dir;
  });
  out.push(card(`Tabelle pro ${unitTitle}`, 'Alle Werte in kWh. Spaltenkopf anklicken zum Sortieren.',
    h('div', { class: 'tablewrap scrolly' }, h('table', {},
      h('thead', {}, h('tr', {}, cols.map(([k, t]) => h('th', {
        'data-sort': k, onclick: () => { S.sort = { key: k, dir: S.sort.key === k ? -S.sort.dir : (k === 'key' ? 1 : -1) }; render(); },
      }, t + (S.sort.key === k ? (S.sort.dir > 0 ? ' ▲' : ' ▼') : ''))))),
      h('tbody', {}, sorted.map(b => h('tr', { class: S.gran === 'day' ? 'clickable' : '', onclick: S.gran === 'day' ? () => openDay(b.key) : null },
        cols.map(([, , f]) => h('td', {}, f(b))))))))));

  queueMicrotask(() => { draw(elSrc, o1); draw(elUse, o2); draw(elBase, o3); drawProfile(elP, elSoc); });
  return out;
}

function openDay(day, scroll = true) {
  S.detailDay = day;
  if (S.tab !== 'analysis') S.tab = 'analysis';
  render();
  if (scroll) setTimeout(() => $('#profile')?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 50);
}

async function drawProfile(elP, elSoc) {
  let p;
  try { p = await api(`/api/power?start=${addDays(S.detailDay, -1)}&end=${addDays(S.detailDay, 1)}`); } catch { return; }
  if (!elP.isConnected) return;
  if (!p.t.length) { elP.replaceWith(h('p', { class: 'empty' }, 'Für diese Tage liegen keine Leistungsdaten vor.')); elSoc.remove(); return; }
  const labels = p.t.map(t => `${WDAYS[toDate(t).getDay()]} ${t.slice(8, 10)}.${t.slice(5, 7)}. ${t.slice(11, 16)}`);
  const buckets = p.t.map((t, i) => ({ title: `${WDAYS[toDate(t).getDay()]}, ${dDE(t)} ab ${t.slice(11, 16)} Uhr (${p.dur[i] / 3600} h)`, n: 1, missing: 0 }));
  const line = (name, color, data) => ({ name, type: 'line', step: 'end', data, showSymbol: false, lineStyle: { width: 2, color }, itemStyle: { color } });
  const o = baseOpt();
  o.xAxis = axCat(labels, { boundaryGap: false, axisLabel: { color: C.muted, fontSize: 11, hideOverlap: true, alignMinLabel: 'left', alignMaxLabel: 'right' } }); o.yAxis = axVal('Watt');
  o.tooltip.formatter = tip(buckets, 'W'); o.tooltip.axisPointer = { type: 'line', lineStyle: { color: C.axis } };
  o.series = [line('Verbrauch', C.load, p.consumption), line('Erzeugung', C.pv, p.production), line('Netzbezug', C.grid, p.purchase)];
  draw(elP, o);
  const o2 = baseOpt(false);
  o2.xAxis = axCat(labels, { boundaryGap: false, axisLabel: { color: C.muted, fontSize: 11, hideOverlap: true, alignMinLabel: 'left', alignMaxLabel: 'right' } }); o2.yAxis = axVal('%', { max: 100, min: 0 });
  o2.tooltip.formatter = tip(buckets, '%'); o2.tooltip.axisPointer = { type: 'line', lineStyle: { color: C.axis } };
  o2.series = [{ name: 'Ladestand', type: 'line', data: p.soc, showSymbol: false, lineStyle: { width: 2, color: C.battery }, itemStyle: { color: C.battery }, areaStyle: { color: C.battery, opacity: 0.12 } }];
  draw(elSoc, o2);
}

// ---------- meter comparison ---------------------------------------------------------
const VERDICT = {
  ok: ['ok', 'stimmt überein'], gap: ['gap', 'durch Datenlücke erklärbar'], gap_unexplained: ['bad', 'trotz Datenlücke unklar'],
  high: ['bad', 'Zähler zählt mehr'], low: ['bad', 'Zähler zählt weniger'],
};
const nowLocal = () => { const d = new Date(); return { date: isoDay(d), time: `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}` }; };

function numField(label, name, value, hint, required, expect) {
  const echo = h('span', { class: 'hint' }, hint || '');
  const input = h('input', {
    name, inputmode: 'decimal', autocomplete: 'off', value: value ?? '', required: required || null, placeholder: 'z. B. 12345,6',
    oninput: e => {
      const v = parseNum(e.target.value), x = expect ? expect() : null;
      echo.textContent = v == null ? (hint || '') : Number.isNaN(v) ? 'Bitte eine Zahl eingeben'
        : `= ${nf(1).format(v)} kWh` + (x == null ? '' : ` · erwartet ${nf(1).format(x)} · Abweichung ${v - x > 0 ? '+' : ''}${nf(1).format(v - x)} kWh`);
    },
  });
  return h('label', { class: 'field' }, label, input, echo);
}
const textField = (label, name, value, { wide, hint, ...attrs } = {}) => h('label', { class: 'field' + (wide ? ' wide' : '') }, label, h('input', { name, value: value ?? '', ...attrs }), h('span', { class: 'hint' }, hint || ''));
function readNum(form, name, required, what) {
  const v = parseNum(form.elements[name].value);
  if (Number.isNaN(v)) throw new Error('Bitte Zahlen in der Form 12345,6 eingeben.');
  if (v == null && required) throw new Error(what ? `Bitte ${what} eintragen.` : 'Bitte den Zählerstand eintragen.');
  return v;
}

function dialog(title, sub, fields, submitLabel, onSubmit) {
  const dlg = $('#dlg'), body = $('#dlg-body');
  const err = h('p', { class: 'err' });
  body.replaceChildren(h('h2', {}, title), sub && h('p', { class: 'sub' }, sub), h('div', { class: 'form' }, fields), err,
    h('div', { class: 'actions' },
      h('button', { class: 'btn', type: 'button', onclick: () => dlg.close() }, 'Abbrechen'),
      h('button', { class: 'btn primary', type: 'submit' }, submitLabel)));
  body.onsubmit = async e => {
    e.preventDefault();
    try { await onSubmit(body); dlg.close(); await refreshMeter(); } catch (ex) { err.textContent = ex.message; }
  };
  dlg.showModal();
}
async function refreshMeter() { S.cmp = await api('/api/comparison'); render(); }

function dlgNewMeter() {
  const n = nowLocal();
  dialog('Zähler anlegen', 'Die Zählernummer steht auf dem Zähler und auf der Stromrechnung. Einbaudatum und Anfangsstand nur eintragen, wenn sie bekannt sind (z. B. aus dem Einbauprotokoll).', [
    textField('Zählernummer', 'number', '', { required: true }),
    textField('Bezeichnung (optional)', 'label', '', { placeholder: 'z. B. Zweirichtungszähler' }),
    textField('Einbaudatum (optional)', 'date', '', { type: 'date', max: n.date }),
    textField('Uhrzeit', 'time', '12:00', { type: 'time' }),
    numField('Anfangsstand Bezug 1.8.0 (optional)', 'imp', ''), numField('Anfangsstand Einspeisung 2.8.0 (optional)', 'exp', ''),
  ], 'Zähler anlegen', async f => {
    const date = f.elements.date.value;
    await api('/api/meters', { method: 'POST', json: {
      number: f.elements.number.value, label: f.elements.label.value || null,
      installed_at: date ? `${date}T${f.elements.time.value || '12:00'}` : null,
      start_import_kwh: readNum(f, 'imp'), start_export_kwh: readNum(f, 'exp'),
    } });
    toast('Zähler angelegt.');
  });
}
function dlgEditMeter(m) {
  dialog(`Zähler ${m.number} bearbeiten`, null, [
    textField('Zählernummer', 'number', m.number, { required: true }), textField('Bezeichnung', 'label', m.label || ''),
    textField('Einbaudatum', 'date', m.installed_local ? m.installed_local.slice(0, 10) : '', { type: 'date' }),
    textField('Uhrzeit', 'time', m.installed_local ? m.installed_local.slice(11, 16) : '12:00', { type: 'time' }),
    textField('Notiz', 'note', m.note || '', { wide: true }),
  ], 'Speichern', async f => {
    const date = f.elements.date.value;
    await api(`/api/meters/${m.id}`, { method: 'PUT', json: {
      number: f.elements.number.value, label: f.elements.label.value || null, note: f.elements.note.value || null,
      installed_at: date ? `${date}T${f.elements.time.value || '12:00'}` : null,
    } });
  });
}
function dlgEditReading(m, r) {
  dialog('Ablesung bearbeiten', `Zähler ${m.number}`, [
    textField('Datum', 'date', r.taken_local.slice(0, 10), { type: 'date', required: true }),
    textField('Uhrzeit', 'time', r.taken_local.slice(11, 16), { type: 'time', required: true }),
    numField('1.8.0 – Bezug (kWh)', 'imp', nf(1).format(r.import_kwh), '', true),
    numField('2.8.0 – Einspeisung (kWh)', 'exp', r.export_kwh == null ? '' : nf(1).format(r.export_kwh)),
    textField('Notiz', 'note', r.note || '', { wide: true }),
  ], 'Speichern', async f => {
    await api(`/api/readings/${r.id}`, { method: 'PUT', json: {
      meter_id: m.id, taken_at: `${f.elements.date.value}T${f.elements.time.value}`,
      import_kwh: readNum(f, 'imp', true), export_kwh: readNum(f, 'exp'), note: f.elements.note.value || null,
    } });
  });
}
function dlgMeterChange(active) {
  const n = nowLocal();
  const unknown = h('input', { type: 'checkbox', name: 'unknown', onchange: e => {
    for (const k of ['oimp', 'oexp']) { const el = e.target.form.elements[k]; el.disabled = e.target.checked; if (e.target.checked) { el.value = ''; el.dispatchEvent(new Event('input')); } }
  } });
  dialog('Zählerwechsel erfassen', 'Wenn der Netzbetreiber den Zähler tauscht: Die Endstände des alten und die Anfangsstände des neuen Zählers stehen auf dem Wechselprotokoll. Alle vier Werte werden gebraucht.', [
    h('label', { class: 'field' }, 'Alter Zähler', h('select', { name: 'old' }, active.map(m => h('option', { value: m.id }, m.number))), h('span', { class: 'hint' })),
    textField('Neue Zählernummer', 'number', '', { required: true }),
    textField('Datum des Wechsels', 'date', n.date, { type: 'date', required: true, max: n.date }),
    textField('Uhrzeit', 'time', '12:00', { type: 'time', required: true }),
    numField('Endstand alt – 1.8.0 Bezug', 'oimp', ''), numField('Endstand alt – 2.8.0 Einspeisung', 'oexp', ''),
    h('label', { class: 'check wide' }, unknown, 'Endstand des alten Zählers unbekannt (z. B. Protokoll nicht mehr vorhanden). Die Zeit von der letzten Ablesung bis zum Wechsel kann dann nicht abgeglichen werden.'),
    numField('Anfangsstand neu – 1.8.0 Bezug', 'nimp', '0'), numField('Anfangsstand neu – 2.8.0 Einspeisung', 'nexp', '0'),
    textField('Notiz', 'note', '', { wide: true, placeholder: 'z. B. Grund des Wechsels' }),
  ], 'Wechsel speichern', async f => {
    const none = f.elements.unknown.checked;
    await api('/api/meter-change', { method: 'POST', json: {
      old_meter_id: +f.elements.old.value, changed_at: `${f.elements.date.value}T${f.elements.time.value}`,
      old_final_unknown: none,
      old_final_import_kwh: none ? null : readNum(f, 'oimp', true, 'den Endstand 1.8.0 des alten Zählers (oder „Endstand unbekannt“ ankreuzen)'),
      old_final_export_kwh: none ? null : readNum(f, 'oexp', true, 'den Endstand 2.8.0 des alten Zählers (oder „Endstand unbekannt“ ankreuzen)'),
      new_number: f.elements.number.value,
      new_start_import_kwh: readNum(f, 'nimp', true, 'den Anfangsstand 1.8.0 des neuen Zählers'),
      new_start_export_kwh: readNum(f, 'nexp', true, 'den Anfangsstand 2.8.0 des neuen Zählers'),
      note: f.elements.note.value || null,
    } });
    toast('Zählerwechsel gespeichert.');
  });
}

function viewMeter() {
  const cmp = S.cmp, out = [];
  if (!cmp.meters.length) {
    out.push(card('Offiziellen Zähler anlegen', 'Damit Ablesungen dem richtigen Gerät zugeordnet werden, zuerst den Zähler des Netzbetreibers mit seiner Zählernummer anlegen. Wird der Zähler später getauscht, wird das als Zählerwechsel erfasst – so werden nie Stände verschiedener Geräte miteinander verrechnet.',
      h('button', { class: 'btn primary', onclick: dlgNewMeter }, 'Zähler anlegen')));
    return out;
  }
  const active = cmp.meters.filter(m => !m.removed_at);
  const n = nowLocal();

  // entry form
  const err = h('p', { class: 'err wide' });
  const hint = h('p', { class: 'sub wide' });
  const sel = h('select', { name: 'meter', onchange: showHint }, (active.length ? active : cmp.meters).map(m => h('option', { value: m.id }, `${m.number}${m.label ? ' – ' + m.label : ''}`)));
  const meterOf = () => cmp.meters.find(x => x.id === +sel.value);
  function showHint() {
    const e = meterOf()?.expected;
    hint.textContent = !e ? '' : `Laut sonnen müsste der Zähler jetzt bei etwa ${nf(1).format(e.import_kwh)} kWh Bezug (1.8.0)`
      + (e.export_kwh != null ? ` und ${nf(1).format(e.export_kwh)} kWh Einspeisung (2.8.0)` : '') + ` stehen (sonnen-Datenstand ${dtDE(e.as_of)}`
      + (e.missing_days > 0.1 ? `; dazu kommen geschätzt ${n0.format(e.est_import[0])} – ${n0.format(e.est_import[2])} kWh Bezug aus ${num(e.missing_days, 1)} Tagen ohne Daten` : '') + ').';
  }
  // The expected value only applies to a reading taken now, not to one that is entered for a past date.
  const expectNow = key => () => { const e = meterOf()?.expected; return e && form.elements.date.value === n.date ? e[key] : null; };
  const form = h('form', { class: 'form', onsubmit: async e => {
    e.preventDefault(); err.textContent = '';
    const f = e.target;
    try {
      const exp = readNum(f, 'exp'), m = meterOf(), lastR = m.readings[m.readings.length - 1];
      if (exp == null && lastR && lastR.export_kwh != null
        && !confirm('Ohne den Wert 2.8.0 kann die Einspeisung für diesen Zeitraum nicht abgeglichen werden. Trotzdem speichern?')) return;
      await api('/api/readings', { method: 'POST', json: {
        meter_id: m.id, taken_at: `${f.elements.date.value}T${f.elements.time.value}`,
        import_kwh: readNum(f, 'imp', true), export_kwh: exp, note: f.elements.note.value || null,
      } });
      toast('Ablesung gespeichert.'); await refreshMeter();
    } catch (ex) { err.textContent = ex.message; }
  } },
    h('label', { class: 'field' }, 'Zähler (Nummer)', sel, h('span', { class: 'hint' })),
    textField('Datum', 'date', n.date, { type: 'date', required: true, max: n.date }),
    textField('Uhrzeit', 'time', n.time, { type: 'time', required: true }),
    numField('1.8.0 – Bezug (kWh)', 'imp', '', 'Anzeige 1.8.0 am Zähler', true, expectNow('import_kwh')),
    numField('2.8.0 – Einspeisung (kWh)', 'exp', '', 'Anzeige 2.8.0 am Zähler', false, expectNow('export_kwh')),
    textField('Notiz', 'note', '', { placeholder: 'optional' }),
    hint, err,
    h('div', { class: 'row wide' },
      h('button', { class: 'btn primary', type: 'submit' }, 'Ablesung speichern'),
      h('button', { class: 'btn', type: 'button', onclick: () => dlgMeterChange(active), disabled: !active.length }, 'Zählerwechsel erfassen'),
      h('button', { class: 'btn', type: 'button', onclick: dlgNewMeter }, 'Weiteren Zähler anlegen')));
  out.push(card('Zählerstand eintragen', 'Der Zähler zeigt abwechselnd zwei Werte: 1.8.0 ist der Bezug aus dem Netz, 2.8.0 die Einspeisung ins Netz. Bitte immer beide mit Datum und Uhrzeit eintragen.', form));
  showHint();

  // result: both registers side by side
  const I = cmp.intervals, t = cmp.total;
  const sgn = v => (v == null ? '–' : (v > 0 ? '+' : '') + n1.format(v));
  const period = i => `${dtShort(i.from)} – ${dtShort(i.to)}`;
  const badge = v => (v ? h('span', { class: 'badge ' + VERDICT[v][0] }, VERDICT[v][1]) : '');
  const devClass = (v, diff) => (!v || v === 'ok' || v === 'gap' ? '' : diff > 0 ? 'pos' : 'neg');
  if (!I.length) {
    out.push(h('div', { class: 'notice info' }, h('span', { class: 'ico' }, 'i'), h('p', {}, 'Für einen Abgleich werden mindestens zwei Ablesungen desselben Zählers gebraucht. Auch ältere Stände, z. B. von Rechnungen oder Fotos, können mit ihrem Datum nachgetragen werden.')));
  } else {
    const X = I.filter(i => i.meter_export != null), te = t.export;
    const stat = (k, v, unit, note) => h('div', { class: 'stat' }, h('div', { class: 'k' }, k), h('div', { class: 'v' }, v, unit && h('small', {}, unit)), note && h('div', { class: 'n' }, note));
    const side = (title, sub, rows, meterKey, sonnenKey, estOf, sum) => {
      if (!rows.length) return card(title, sub, h('p', { class: 'empty' }, 'Dafür werden zwei Ablesungen mit diesem Wert gebraucht.'));
      const B = rows.map(i => ({ label: `${dShort(i.from)}–\n${dShort(i.to)}`, title: `${dtDE(i.from)} – ${dtDE(i.to)} · Zähler ${i.meter_number}`, n: Math.max(1, i.days), missing: i.missing_days }));
      const el = h('div', { class: 'chart' });
      const o = baseOpt();
      o.xAxis = axCat(B.map(b => b.label), { axisLabel: { color: C.muted, fontSize: 11, interval: 0, hideOverlap: true } }); o.yAxis = axVal('kWh');
      o.tooltip.formatter = tip(B);
      o.series = [
        barSeries('offizieller Zähler', C.meter, rows.map(i => i[meterKey]), 'm', true),
        barSeries('sonnen aufgezeichnet', C.grid, rows.map(i => i[sonnenKey]), 's'),
        barSeries('Schätzung für Zeit ohne Daten', C.est, rows.map(i => (i.missing_days > 0.02 ? estOf(i) : 0)), 's', true),
      ];
      queueMicrotask(() => draw(el, o));
      return card(title, sub, h('div', { class: 'stats' },
        stat('Zähler', n1.format(sum.meter), 'kWh', `in ${n0.format(sum.days)} Tagen`),
        stat('laut sonnen erwartet', n1.format(sum.sonnen), 'kWh'),
        stat('Abweichung', sgn(sum.diff), 'kWh', sum.meter > 0 ? `${sgn(100 * sum.diff / sum.meter)} % des Zählerwerts` : null),
        sum.missing > 0.1 && stat('ohne sonnen-Daten', n1.format(sum.missing), 'Tage', `erklärt etwa ${sum.est}`)), el);
    };
    out.push(h('div', { class: 'cols' },
      side('1.8.0 – Bezug aus dem Netz', 'Zählerstand gegen das, was sonnen als Netzbezug aufgezeichnet hat.', I, 'meter_import', 'sonnen_import', i => i.est_import[1],
        { meter: t.meter_import, sonnen: t.sonnen_import, diff: t.diff_import, days: t.days, missing: t.missing_days, est: `${n0.format(t.est_import[0])} – ${n0.format(t.est_import[2])} kWh` }),
      side('2.8.0 – Einspeisung ins Netz', 'Zählerstand gegen das, was sonnen als Einspeisung aufgezeichnet hat.', X, 'meter_export', 'sonnen_export', i => i.est_export[1],
        te ? { meter: te.meter, sonnen: te.sonnen, diff: te.diff, days: te.days, missing: te.missing_days, est: `0 – ${n0.format(te.est[1])} kWh` } : null)));
    out.push(h('div', { class: 'legendline', style: 'margin-top:-6px' }, h('span', {}, 'Grau: geschätzte Energie in Zeiten, in denen sonnen nichts aufgezeichnet hat (Annahme: Speicher aus, PV-Anlage lief).')));

    out.push(card('Abweichung je Ablesezeitraum', 'Positive Abweichung: Der Zähler hat mehr gezählt, als sonnen aufgezeichnet hat. Alle Werte in kWh.',
      h('div', { class: 'tablewrap' }, h('table', {},
        h('thead', {},
          h('tr', {}, h('th', { rowspan: 2 }, 'Zeitraum'), h('th', { rowspan: 2 }, 'Tage'), h('th', { colspan: 4, class: 'grp' }, '1.8.0 – Bezug'), h('th', { colspan: 4, class: 'grp' }, '2.8.0 – Einspeisung'), h('th', { rowspan: 2 }, 'Tage ohne Daten')),
          h('tr', {}, ['Zähler', 'sonnen', 'Abweichung', 'Bewertung', 'Zähler', 'sonnen', 'Abweichung', 'Bewertung'].map((x, i) => h('th', { class: (i % 4 === 3 ? 'l' : '') + (i % 4 === 0 ? ' grp' : '') }, x)))),
        h('tbody', {}, I.map(i => h('tr', {},
          h('td', { title: `Zähler ${i.meter_number}` }, period(i)), h('td', {}, n1.format(i.days)),
          h('td', { class: 'grp' }, n1.format(i.meter_import)), h('td', {}, n1.format(i.sonnen_import) + (i.approx ? ' ≈' : '')),
          h('td', { class: devClass(i.verdict, i.diff_import) }, sgn(i.diff_import) + (i.diff_pct == null ? '' : ` (${sgn(i.diff_pct)} %)`)), h('td', { class: 'l' }, badge(i.verdict)),
          h('td', { class: 'grp' }, i.meter_export == null ? '–' : n1.format(i.meter_export)), h('td', {}, n1.format(i.sonnen_export)),
          h('td', { class: devClass(i.verdict_export, i.diff_export) }, sgn(i.diff_export)), h('td', { class: 'l' }, badge(i.verdict_export)),
          h('td', {}, i.missing_days > 0.02 ? n1.format(i.missing_days) : '')))),
        h('tfoot', {}, h('tr', {}, h('td', {}, 'Summe'), h('td', {}, n1.format(t.days)),
          h('td', { class: 'grp' }, n1.format(t.meter_import)), h('td', {}, n1.format(t.sonnen_import)), h('td', {}, sgn(t.diff_import)), h('td', {}),
          h('td', { class: 'grp' }, te ? n1.format(te.meter) : '–'), h('td', {}, te ? n1.format(te.sonnen) : '–'), h('td', {}, te ? sgn(te.diff) : '–'), h('td', {}),
          h('td', {}, t.missing_days > 0.02 ? n1.format(t.missing_days) : ''))))),
      h('div', { class: 'legendline' }, h('span', {}, 'Toleranz für „stimmt überein“: 3 % bzw. mindestens 2 kWh.'),
        t.missing_days > 0.02 && h('span', {}, `In den Tagen ohne sonnen-Daten sind geschätzt ${n0.format(t.est_import[0])} – ${n0.format(t.est_import[2])} kWh Bezug nicht aufgezeichnet worden.`),
        I.some(i => i.approx) && h('span', {}, '≈: Für Teile dieses Zeitraums liegen nur Tageswerte vor; angebrochene Tage sind anteilig gerechnet.'))));
  }
  for (const b of cmp.breaks) {
    out.push(h('div', { class: 'notice info' }, h('span', { class: 'ico' }, 'i'), h('p', {}, `Zwischen ${dtDE(b.from)} (Zähler ${b.old_meter}) und ${dtDE(b.to)} (Zähler ${b.new_meter}) ist kein Abgleich möglich, weil der Endstand des alten Zählers fehlt.`)));
  }

  // meters and their readings, each with what sonnen expected for that moment
  const KIND = { install: 'Anfangsstand', removal: 'Endstand', reading: '' };
  const byReading = new Map(I.map(i => [i.reading_id, i]));
  const kw = v => (v == null ? '–' : nf(1).format(v));
  for (const m of [...cmp.meters].reverse()) {
    const c = card(`Zähler ${m.number}`, [m.label, m.installed_local && `eingebaut ${dtDE(m.installed_local)}`, m.removed_local ? `ausgebaut ${dtDE(m.removed_local)}` : 'in Betrieb', m.note].filter(Boolean).join(' · '),
      m.readings.length ? h('div', { class: 'tablewrap' }, h('table', {},
        h('thead', {},
          h('tr', {}, h('th', { rowspan: 2 }, 'Abgelesen am'), h('th', { colspan: 3, class: 'grp' }, '1.8.0 – Bezug (kWh)'), h('th', { colspan: 3, class: 'grp' }, '2.8.0 – Einspeisung (kWh)'), h('th', { rowspan: 2, class: 'l' }, 'Notiz'), h('th', { rowspan: 2 })),
          h('tr', {}, ['Zählerstand', 'erwartet', 'Abweichung', 'Zählerstand', 'erwartet', 'Abweichung'].map((x, i) => h('th', { class: i % 3 === 0 ? 'grp' : '' }, x)))),
        h('tbody', {}, [...m.readings].reverse().map(r => {
          const i = byReading.get(r.id);
          return h('tr', {},
            h('td', {}, dtDE(r.taken_local)),
            h('td', { class: 'grp' }, kw(r.import_kwh)), h('td', {}, i ? kw(i.expected_import) : '–'),
            h('td', { class: i ? devClass(i.verdict, i.diff_import) : '', title: i ? VERDICT[i.verdict][1] : '' }, i ? sgn(i.diff_import) : '–'),
            h('td', { class: 'grp' }, kw(r.export_kwh)), h('td', {}, i ? kw(i.expected_export) : '–'),
            h('td', { class: i ? devClass(i.verdict_export, i.diff_export) : '', title: i && i.verdict_export ? VERDICT[i.verdict_export][1] : '' }, i ? sgn(i.diff_export) : '–'),
            h('td', { class: 'l wrap' }, [KIND[r.kind], r.note].filter(Boolean).filter((x, k, a) => a.indexOf(x) === k).join(' · ')),
            h('td', {}, h('div', { class: 'row', style: 'justify-content:flex-end;flex-wrap:nowrap' },
              h('button', { class: 'btn small', onclick: () => dlgEditReading(m, r) }, 'Ändern'),
              h('button', { class: 'btn small danger', onclick: async () => {
                if (!confirm(`Ablesung vom ${dtDE(r.taken_local)} löschen?`)) return;
                try { await api(`/api/readings/${r.id}`, { method: 'DELETE' }); await refreshMeter(); } catch (ex) { toast(ex.message); }
              } }, 'Löschen'))));
        }))),
      h('div', { class: 'legendline' }, h('span', {}, '„Erwartet“ = vorherige Ablesung plus das, was sonnen seitdem aufgezeichnet hat. Abweichung = Zählerstand minus erwartet.')))
        : h('p', { class: 'empty' }, 'Noch keine Ablesung für diesen Zähler.'));
    $('header', c).append(h('div', { class: 'row' },
      h('button', { class: 'btn small', onclick: () => dlgEditMeter(m) }, 'Zähler bearbeiten'),
      h('button', { class: 'btn small danger', onclick: async () => {
        if (!confirm(`Zähler ${m.number} mit allen ${m.readings.length} Ablesungen löschen?`)) return;
        try { await api(`/api/meters/${m.id}`, { method: 'DELETE' }); await refreshMeter(); } catch (ex) { toast(ex.message); }
      } }, 'Zähler löschen')));
    out.push(c);
  }
  return out;
}

// ---------- data / sync --------------------------------------------------------------
function viewData() {
  const ov = S.ov, sy = ov.sync, out = [];
  const last = sy.runs.find(r => r.trigger !== 'import');
  const status = sy.running ? 'Synchronisation läuft …'
    : !sy.configured ? 'Keine Zugangsdaten für das sonnen-Portal hinterlegt – es werden nur importierte CSV-Dateien verwendet.'
      : !sy.enabled ? 'Automatischer Download ist abgeschaltet (SYNC_ENABLED=0).'
        : `Automatischer Download täglich um ${sy.time} Uhr.` + (last ? ` Letzter Lauf: ${dtDE(localIso(last.finished_at || last.started_at))} – ${last.ok ? 'erfolgreich' : 'fehlgeschlagen'}.` : ' Noch kein Lauf.');
  const btn = h('button', { class: 'btn primary', disabled: sy.running, onclick: async () => {
    try { await api('/api/sync', { method: 'POST' }); toast('Synchronisation gestartet.'); pollSync(); } catch (ex) { toast(ex.message); }
  } }, 'Jetzt synchronisieren');
  out.push(card('Download aus dem sonnen-Portal', status, h('div', { class: 'row' }, btn),
    sy.runs.length ? h('div', { class: 'tablewrap scrolly', style: 'margin-top:12px' }, h('table', {},
      h('thead', {}, h('tr', {}, ['Zeit', 'Auslöser', 'Ergebnis', 'Meldung'].map(x => h('th', { class: 'l' }, x)))),
      h('tbody', {}, sy.runs.map(r => h('tr', {}, h('td', {}, dtDE(localIso(r.started_at))), h('td', { class: 'l' }, r.trigger || ''),
        h('td', { class: 'l' }, h('span', { class: 'badge ' + (r.ok == null ? '' : r.ok ? 'ok' : 'bad') }, r.ok == null ? 'läuft' : r.ok ? 'in Ordnung' : 'Fehler')),
        h('td', { class: 'l wrap' }, r.message || '')))))) : null));

  const drop = h('div', { class: 'drop' }, 'CSV-Dateien aus dem sonnen-Portal hierher ziehen oder ',
    h('label', { class: 'btn small', style: 'display:inline-block' }, 'Dateien wählen', h('input', { type: 'file', accept: '.csv,text/csv', multiple: true, hidden: true, onchange: e => upload(e.target.files) })));
  drop.addEventListener('dragover', e => { e.preventDefault(); drop.classList.add('over'); });
  drop.addEventListener('dragleave', () => drop.classList.remove('over'));
  drop.addEventListener('drop', e => { e.preventDefault(); drop.classList.remove('over'); upload(e.dataTransfer.files); });
  out.push(card('CSV-Export importieren', 'Im sonnen-Portal unter Analyse → Download: „Energiedaten in Wattstunden“ (statistics_…csv) und „Leistungsdaten in Watt“ (measurements_…csv). Bereits vorhandene Tage werden aktualisiert, nichts wird doppelt gezählt.', drop));

  out.push(card('Datenbestand', null, h('div', { class: 'tiles' },
    tile('Zeitraum', n0.format(S.days.length), 'Tage', ov.first_day ? `${dDE(ov.first_day)} – ${dDE(ov.last_day)}, Datenstand ${dtDE(ov.data_end)}` : null),
    tile('Tageswerte', n0.format(ov.counts.daily), 'Tage'),
    tile('Leistungswerte', n0.format(ov.counts.power), 'Abschnitte', '3-Stunden- bzw. Stundenmittel'),
    tile('Stundenwerte Energie', n0.format(ov.counts.energy_fine), null, ov.hourly_from ? `ab ${dDE(ov.hourly_from)}` : 'kommen mit dem täglichen Download')),
    ov.gaps.length ? h('div', { class: 'tablewrap', style: 'margin-top:14px' }, h('table', {},
      h('thead', {}, h('tr', {}, ['Zeit ohne sonnen-Daten', 'fehlende Tage', 'geschätzter Netzbezug (kWh)', 'Schätzgrundlage'].map((x, i) => h('th', { class: i === 3 ? 'l' : '' }, x)))),
      h('tbody', {}, ov.gaps.map(g => h('tr', {}, h('td', {}, g.from === g.to ? dDE(g.from) : `${dDE(g.from)} – ${dDE(g.to)}`), h('td', {}, num(g.missing_days, 1)),
        h('td', {}, `${n0.format(g.est_kwh.imp_lo)} – ${n0.format(g.est_kwh.imp_hi)}`), h('td', { class: 'l' }, g.reference)))))) : null));
  return out;
}
const localIso = utc => { const d = new Date(utc); return `${isoDay(d)}T${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`; };

async function upload(files) {
  let ok = 0;
  for (const f of files) {
    try {
      const r = await api(`/api/import?name=${encodeURIComponent(f.name)}`, { method: 'POST', body: await f.text() });
      ok++; toast(`${f.name}: ${r.written} Zeilen übernommen`);
    } catch (ex) { toast(`${f.name}: ${ex.message}`); }
  }
  if (ok) { await loadData(); S.cmp = null; render(); }
}
async function pollSync() {
  for (let i = 0; i < 150; i++) {
    await new Promise(r => setTimeout(r, 2000));
    const ov = await api('/api/overview');
    if (!ov.sync.running) break;
  }
  await loadData(); S.cmp = null; render();
}

// ---------- shell ---------------------------------------------------------------------
function renderFilters() {
  const show = S.tab === 'overview' || S.tab === 'analysis';
  $('#filters').hidden = !show || !S.days.length;
  if (!show || !S.days.length) return;
  const presets = [['all', 'Alles'], ...years().map(y => [y, y]), ['12m', '12 Monate'], ['30d', '30 Tage']];
  $('#presets').replaceChildren(...presets.map(([k, label]) => h('button', { class: S.preset === k ? 'on' : '', onclick: () => { setPreset(k); render(); } }, label)));
  const first = S.days[0].day, last = S.days[S.days.length - 1].day;
  for (const [id, key] of [['#f-from', 'from'], ['#f-to', 'to']]) {
    const el = $(id); el.min = first; el.max = last; el.value = S[key];
    el.onchange = () => { if (!el.value) return; S[key] = el.value; if (S.from > S.to) S[key === 'from' ? 'to' : 'from'] = el.value; S.preset = 'custom'; render(); };
  }
  $('#gran').hidden = S.tab !== 'analysis';
  $$('#gran button').forEach(b => { b.classList.toggle('on', b.dataset.g === S.gran); b.onclick = () => { S.gran = b.dataset.g; S.sort = { key: 'key', dir: 1 }; render(); }; });
}

async function render() {
  readColors();
  $$('#tabs button').forEach(b => b.classList.toggle('on', b.dataset.tab === S.tab));
  renderFilters();
  if (S.tab === 'meter' && !S.cmp) S.cmp = await api('/api/comparison');
  const keep = window.scrollY;
  disposeCharts();
  const view = { overview: viewOverview, analysis: viewAnalysis, meter: viewMeter, data: viewData }[S.tab]();
  $('#view').replaceChildren(...view.filter(Boolean));
  window.scrollTo(0, keep);
}

function showLogin() {
  $('#filters').hidden = true; $('#tabs').hidden = true;
  const err = h('p', { class: 'err' });
  $('#view').replaceChildren(h('form', { class: 'card login', onsubmit: async e => {
    e.preventDefault();
    try { await api('/api/login', { method: 'POST', json: { password: e.target.elements.pw.value } }); $('#tabs').hidden = false; await start(); }
    catch (ex) { err.textContent = ex.message; }
  } }, h('h2', { style: 'margin-bottom:12px' }, 'Anmelden'),
    h('label', { class: 'field' }, 'Passwort', h('input', { type: 'password', name: 'pw', autocomplete: 'current-password', required: true, autofocus: true })),
    err, h('div', { class: 'row', style: 'margin-top:12px' }, h('button', { class: 'btn primary' }, 'Anmelden'))));
}

async function start() {
  const s = await api('/api/session');
  $('#site-name').textContent = s.site_name || '';
  if (s.auth_required && !s.authenticated) return showLogin();
  await loadData();
  await render();
}

$('#tabs').addEventListener('click', e => {
  const b = e.target.closest('button[data-tab]');
  if (!b) return;
  S.tab = b.dataset.tab; history.replaceState(null, '', '#' + S.tab); window.scrollTo(0, 0); render();
});
if (['overview', 'analysis', 'meter', 'data'].includes(location.hash.slice(1))) S.tab = location.hash.slice(1);
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => render());
start().catch(ex => { if (ex.message !== 'Bitte anmelden.') $('#view').replaceChildren(h('div', { class: 'card err' }, 'Fehler beim Laden: ' + ex.message)); });
