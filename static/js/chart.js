// lp and elo history chart on the profile
import { API, esc, formatTime, placeTooltip } from './util.js';

let chartMode = 'lp';
let chartWindow = 'all';
let currentChartPlayer = null;
// cached history for the last player fetched
let chartHistory = null;

const WINDOW_LABELS = { week: 'Past week', month: 'Past month', all: 'All time' };

// round a range to 1, 2, 5 or 10 times a power of ten
function niceNum(range, round) {
  if (!(range > 0) || !isFinite(range)) return 1;
  const exp = Math.floor(Math.log10(range));
  const frac = range / Math.pow(10, exp);
  let nf;
  if (round) nf = frac < 1.5 ? 1 : frac < 3 ? 2 : frac < 7 ? 5 : 10;
  else nf = frac <= 1 ? 1 : frac <= 2 ? 2 : frac <= 5 ? 5 : 10;
  return nf * Math.pow(10, exp);
}

// evenly spaced round numbers covering lo to hi
function niceTicks(lo, hi, count = 5) {
  if (!(hi > lo)) hi = lo + 1;
  const range = niceNum(hi - lo, false);
  const step = Math.max(1, Math.round(niceNum(range / Math.max(1, count - 1), true)));
  const min = Math.floor(lo / step) * step;
  const max = Math.ceil(hi / step) * step;
  const ticks = [];
  for (let v = min; v <= max + step * 0.5; v += step) ticks.push(Math.round(v));
  return { ticks, min, max };
}

// short date for the x axis
function chartDateLabel(iso) {
  const d = new Date(iso);
  return isNaN(d) ? '' : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

// at most maxCount evenly spread indices so long histories dont draw thousands of points
function sampleIndices(n, maxCount) {
  if (n <= maxCount) return Array.from({ length: n }, (_, i) => i);
  const out = [];
  const step = (n - 1) / (maxCount - 1);
  let last = -1;
  for (let k = 0; k < maxCount; k++) {
    const i = Math.round(k * step);
    if (i !== last) { out.push(i); last = i; }
  }
  return out;
}

// fetch history once per player
function ensureChartHistory(p) {
  if (chartHistory?.name === p.displayName) return Promise.resolve(chartHistory);
  return fetch(`${API}/players/${encodeURIComponent(p.displayName)}/lp-history`)
    .then(r => r.json())
    .then(d => (chartHistory = { name: p.displayName, entries: d.history || [] }))
    .catch(() => (chartHistory = { name: p.displayName, entries: [] }));
}

// value, change since last dot and date for a hovered dot
function showChartTooltip(event, dot) {
  const tt = document.getElementById('chart-tooltip');
  if (!tt) return;
  document.querySelectorAll('#lp-chart-container .chart-dot.active').forEach(d => d.classList.remove('active'));
  dot.classList.add('active');

  const unit = chartMode === 'elo' ? 'ELO' : 'LP';
  let deltaHtml = '';
  const d = parseInt(dot.dataset.delta);
  if (!isNaN(d) && d !== 0) {
    const cls = d > 0 ? 'tooltip-win' : 'tooltip-loss';
    const sign = d > 0 ? '+' : '-';
    deltaHtml = `<div class="${cls}">${sign}${Math.abs(d).toLocaleString()} ${unit}</div>`;
  }

  tt.innerHTML = `
    <div class="tooltip-lp">${parseInt(dot.dataset.lp).toLocaleString()} ${unit}</div>
    ${deltaHtml}
    <div class="tooltip-date">${esc(dot.dataset.date)}</div>`;
  tt.style.display = 'block';
  placeTooltip(tt, event, 14, -10);
}

export function hideChartTooltip() {
  const tt = document.getElementById('chart-tooltip');
  if (tt) tt.style.display = 'none';
  document.querySelectorAll('#lp-chart-container .chart-dot.active').forEach(d => d.classList.remove('active'));
}

// draw the chart for player p in the current mode and window
export function renderProfileChart(p) {
  currentChartPlayer = p;
  const container = document.getElementById('lp-chart-container');
  if (!container) return;

  const isElo = chartMode === 'elo';
  const unit = isElo ? 'ELO' : 'LP';

  const toggle = (action, value, label, on) =>
    `<button class="${on ? 'active' : ''}" data-action="${action}" data-value="${value}">${label}</button>`;
  const header = subtitle => `
    <div class="section-header compact spaced">
      <div>
        <div class="section-title">${isElo ? 'ELO History' : 'LP History'}</div>
        <div class="section-sub">${subtitle}</div>
      </div>
      <div class="chart-controls">
        <div class="season-toggle chart-mode-toggle">
          ${toggle('chart-mode', 'lp', 'LP', !isElo)}${toggle('chart-mode', 'elo', 'ELO', isElo)}
        </div>
        <div class="season-toggle chart-window-toggle">
          ${toggle('chart-window', 'week', 'Week', chartWindow === 'week')}
          ${toggle('chart-window', 'month', 'Month', chartWindow === 'month')}
          ${toggle('chart-window', 'all', 'All', chartWindow === 'all')}
        </div>
      </div>
    </div>`;
  const message = text => `<div class="table-wrap chart-wrap empty"><div class="loading">${text}</div></div>`;

  // fetch then come back here
  if (chartHistory?.name !== p.displayName) {
    container.innerHTML = header('Loading...') + message('Loading history...');
    ensureChartHistory(p).then(() => { if (currentChartPlayer === p) renderProfileChart(p); });
    return;
  }

  const valKey = isElo ? 'elo' : 'lp';
  const now = Date.now();
  const cutoff = chartWindow === 'week' ? now - 7 * 864e5
               : chartWindow === 'month' ? now - 30 * 864e5
               : -Infinity;

  const points = (chartHistory.entries || [])
    .filter(e => e && e[valKey] != null && e.timestamp)
    .map(e => ({ value: e[valKey], iso: e.timestamp, ts: new Date(e.timestamp).getTime() }))
    .filter(e => !isNaN(e.ts) && e.ts >= cutoff)
    .sort((a, b) => a.ts - b.ts);

  document.getElementById('chart-tooltip')?.remove();

  if (points.length < 2) {
    container.innerHTML = header(`${WINDOW_LABELS[chartWindow]}, not enough data`) +
      message(`Not enough ${unit} data in this range`);
    return;
  }

  // net change over the window for the subtitle
  const net = points[points.length - 1].value - points[0].value;
  const netCls = net > 0 ? 'tooltip-win' : net < 0 ? 'tooltip-loss' : '';
  const netStr = net === 0 ? 'no change'
    : `${net > 0 ? '▲' : '▼'} ${Math.abs(net).toLocaleString()} ${unit}`;
  const subtitle = `${WINDOW_LABELS[chartWindow]}, <span class="${netCls}">${netStr}</span>`;

  const vals = points.map(pt => pt.value);
  let lo = Math.min(...vals), hi = Math.max(...vals);
  // flat line still needs some height
  if (lo === hi) { lo -= 5; hi += 5; }
  const { ticks, min: dmin, max: dmax } = niceTicks(lo, hi, 5);
  const span = (dmax - dmin) || 1;

  // points are spaced by match not by time
  const xPct = i => (i / (points.length - 1)) * 100;
  const yPct = v => (1 - (v - dmin) / span) * 100;

  const poly = sampleIndices(points.length, 1500)
    .map(i => `${xPct(i).toFixed(2)},${yPct(points[i].value).toFixed(2)}`).join(' ');

  const gridlines = ticks.map(t =>
    `<line x1="0" y1="${yPct(t).toFixed(2)}" x2="100" y2="${yPct(t).toFixed(2)}"
       stroke="var(--border)" stroke-width="1" vector-effect="non-scaling-stroke"/>`).join('');

  const yLabels = ticks.map(t => {
    const y = yPct(t);
    if (y < -1 || y > 101) return '';
    return `<div class="chart-ylabel" style="top:${y.toFixed(2)}%">${t.toLocaleString()}</div>`;
  }).join('');

  // hoverable dots only when there are few enough to tell apart
  const dotMax = window.matchMedia('(max-width: 768px)').matches ? 45 : 110;
  const dotIdx = points.length <= dotMax ? points.map((_, i) => i) : [];
  const dots = dotIdx.map((idx, k) => {
    const pt = points[idx];
    const delta = k > 0 ? pt.value - points[dotIdx[k - 1]].value : '';
    const dir = delta === '' || delta === 0 ? '' : (delta > 0 ? 'up' : 'down');
    const dateIso = idx > 0 ? points[idx - 1].iso : pt.iso;
    return `<div class="chart-dot ${dir}" style="left:${xPct(idx).toFixed(2)}%;top:${yPct(pt.value).toFixed(2)}%"
      data-lp="${pt.value}" data-delta="${delta}" data-date="${esc(formatTime(dateIso))}"></div>`;
  }).join('');

  const midIdx = Math.floor((points.length - 1) / 2);

  const tip = document.createElement('div');
  tip.className = 'chart-tooltip';
  tip.id = 'chart-tooltip';
  document.body.appendChild(tip);

  container.innerHTML = header(subtitle) + `
    <div class="table-wrap chart-wrap">
      <div class="chart-plot">
        <div class="chart-yaxis">${yLabels}</div>
        <div class="chart-area">
          <svg class="chart-svg" viewBox="0 0 100 100" preserveAspectRatio="none">
            ${gridlines}
            <polygon points="0,100 ${poly} 100,100" fill="rgba(0,212,255,0.07)" stroke="none"/>
            <polyline points="${poly}" fill="none" stroke="var(--accent)" stroke-width="1.5"
              vector-effect="non-scaling-stroke" stroke-linejoin="round"/>
          </svg>
          ${dots}
        </div>
      </div>
      <div class="chart-xaxis">
        <span>${chartDateLabel(points[0].iso)}</span>
        <span class="chart-date-mid">${chartDateLabel(points[midIdx].iso)}</span>
        <span>${chartDateLabel(points[points.length - 1].iso)}</span>
      </div>
    </div>`;

  // one set of listeners on the plot instead of one per dot
  const area = container.querySelector('.chart-area');
  const dotFrom = e => e.target.closest('.chart-dot');
  area.addEventListener('mouseover', e => { const d = dotFrom(e); if (d) showChartTooltip(e, d); });
  area.addEventListener('mouseout', e => { if (dotFrom(e)) hideChartTooltip(); });
  area.addEventListener('click', e => { const d = dotFrom(e); if (d) showChartTooltip(e, d); });
  area.addEventListener('touchstart', e => { const d = dotFrom(e); if (d) showChartTooltip(e, d); }, { passive: true });
}

const rerender = () => { if (currentChartPlayer) renderProfileChart(currentChartPlayer); };

export const actions = {
  'chart-mode': el => { chartMode = el.dataset.value; rerender(); },
  'chart-window': el => { chartWindow = el.dataset.value; rerender(); },
};
