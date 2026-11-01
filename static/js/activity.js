// activity view, ranked games bucketed by weekday and hour
import { API, esc, plural, placeTooltip } from './util.js';

let activityWindow = 'all';
// slider bounds from the first reply then the chosen lp range
let lpBounds = null;
let lpMin = null;
let lpMax = null;
let lpDebounce = null;
// replies by window and lp range so switching back is instant
const activityCache = new Map();
// only the newest request gets drawn when the slider moves quickly
let activitySeq = 0;
let activeCell = null;

const DAY_LABELS = ['SUN', 'MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT'];

// colour ramp from dark blue to bright cyan
const HEAT_STOPS = [
  { t: 0.00, c: [8, 40, 72] },
  { t: 0.30, c: [0, 110, 170] },
  { t: 0.58, c: [0, 175, 228] },
  { t: 0.80, c: [0, 212, 255] },
  { t: 1.00, c: [140, 238, 255] },
];

// colour at t between 0 and 1 on the ramp
function heatColor(t) {
  t = Math.max(0, Math.min(1, t));
  for (let i = 1; i < HEAT_STOPS.length; i++) {
    const s1 = HEAT_STOPS[i];
    if (t <= s1.t) {
      const s0 = HEAT_STOPS[i - 1];
      const u = (t - s0.t) / (s1.t - s0.t);
      const [r, g, b] = s0.c.map((v, k) => Math.round(v + (s1.c[k] - v) * u));
      return `rgb(${r},${g},${b})`;
    }
  }
  return `rgb(${HEAT_STOPS[HEAT_STOPS.length - 1].c.join(',')})`;
}

// fetch hourly counts for the window and bracket then draw them
export async function loadActivityHeatmap(timeWindow) {
  if (timeWindow) activityWindow = timeWindow;
  const container = document.getElementById('activity-heatmap-container');
  if (!container) return;

  const key = `${activityWindow}|${lpMin}|${lpMax}`;
  if (!activityCache.size) container.innerHTML = '<div class="loading">Loading activity data...</div>';
  const seq = ++activitySeq;

  try {
    if (!activityCache.has(key)) {
      const params = new URLSearchParams({ window: activityWindow });
      if (lpMin != null) params.set('min_lp', lpMin);
      if (lpMax != null) params.set('max_lp', lpMax);
      const r = await fetch(`${API}/activity?${params}`);
      const d = await r.json();
      // too many slider positions to keep them all
      if (activityCache.size > 50) activityCache.clear();
      activityCache.set(key, d);
    }
    if (seq !== activitySeq) return;
    const d = activityCache.get(key);
    // first reply sets the slider from 0 to the top rated player
    // rounded up to the slider step so the far right really is the top
    if (!lpBounds) {
      const [lo, hi] = d.lpRange || [0, 0];
      lpBounds = [lo, Math.ceil(hi / 10) * 10];
      [lpMin, lpMax] = lpBounds;
    }
    renderHeatmap(d);
  } catch {
    container.innerHTML = '<div class="loading">Failed to load activity data</div>';
  }
}

// tooltip for a cell, also enlarges the cell
function showHeatmapTooltip(event, cell) {
  hideHeatmapTooltip();
  activeCell = cell;
  cell.classList.add('active');

  const tt = document.createElement('div');
  tt.id = 'heatmap-tooltip';
  tt.className = 'heatmap-tooltip';
  tt.textContent = cell.dataset.tooltip;
  document.body.appendChild(tt);
  placeTooltip(tt, event, 12);
}

// remove the tooltip and shrink the cell back
export function hideHeatmapTooltip(cell) {
  document.getElementById('heatmap-tooltip')?.remove();
  const target = cell || activeCell;
  target?.classList.remove('active');
  if (target === activeCell) activeCell = null;
}

// 7 by 24 grid in the viewers local time
// each utc hour from the api lands on its local day and hour
function renderHeatmap(data) {
  const container = document.getElementById('activity-heatmap-container');
  if (!container) return;

  const buckets = Array.from({ length: 7 }, () => Array(24).fill(0));
  for (const [hour, count] of data.hours || []) {
    const d = new Date(`${hour}:00:00Z`);
    if (!isNaN(d)) buckets[d.getDay()][d.getHours()] += count;
  }
  const max = Math.max(0, ...buckets.flat());
  // sqrt so quiet hours still show some colour
  const intensity = count => Math.max(0.06, Math.sqrt(count / max));
  const tzName = Intl.DateTimeFormat().resolvedOptions().timeZone.replace(/_/g, ' ');

  // hour labels every 3 hours, blank corner first
  let cells = '<div></div>';
  for (let h = 0; h < 24; h++) {
    cells += `<div class="hm-hour">${h % 3 === 0 ? String(h).padStart(2, '0') + ':00' : ''}</div>`;
  }
  buckets.forEach((hours, day) => {
    cells += `<div class="hm-day">${DAY_LABELS[day]}</div>`;
    hours.forEach((count, hour) => {
      const tooltip = `${DAY_LABELS[day]} ${String(hour).padStart(2, '0')}:00, ${count} ${plural(count, 'game', 'games')}`;
      // empty cells keep the default background from css
      const bg = count ? ` style="background:${heatColor(intensity(count))}"` : '';
      cells += `<div class="hm-cell" data-tooltip="${tooltip}"${bg}></div>`;
    });
  });

  const swatches = [0, 0.2, 0.4, 0.6, 0.8, 1]
    .map(t => `<div class="hm-swatch" style="background:${heatColor(t)}"></div>`).join('');
  const windowBtn = (value, label) =>
    `<button class="${activityWindow === value ? 'active' : ''}" data-action="activity-window" data-value="${value}">${label}</button>`;

  const totals = `${data.total.toLocaleString()} ${plural(data.total, 'game', 'games')}, ` +
    `${data.uniquePlayers.toLocaleString()} ${plural(data.uniquePlayers, 'player', 'players')}, ${tzName} time`;

  // the slider is drawn once and left alone so redrawing never interrupts a drag
  if (!document.getElementById('activity-grid')) {
    container.innerHTML = `<div id="activity-head"></div>${lpSliderHtml()}<div id="activity-grid"></div>`;
    bindLpSlider();
  }

  document.getElementById('activity-head').innerHTML = `
    <div class="section-header compact">
      <div>
        <div class="section-title">Activity Heatmap</div>
        <div class="section-sub">${esc(totals)}</div>
      </div>
      <div class="season-toggle">${windowBtn('all', 'All Time')}${windowBtn('month', 'Past Month')}${windowBtn('week', 'Past Week')}</div>
    </div>`;
  document.getElementById('activity-grid').innerHTML = `
    <div class="table-wrap heatmap-wrap">
      <div class="heatmap-grid">${cells}</div>
      <div class="hm-legend">
        <span>LESS</span>
        <div class="hm-scale">${swatches}</div>
        <span>MORE</span>
        <span class="hm-peak">PEAK: ${max} ${plural(max, 'game', 'games')}</span>
      </div>
    </div>`;

  // one set of listeners on the grid instead of one per cell
  const grid = container.querySelector('.heatmap-grid');
  const cellFrom = e => e.target.closest('.hm-cell');
  grid.addEventListener('mouseover', e => { const c = cellFrom(e); if (c) showHeatmapTooltip(e, c); });
  // on touch screens the tooltip stays until you tap elsewhere
  grid.addEventListener('mouseout', e => {
    const c = cellFrom(e);
    if (c && !('ontouchstart' in window)) hideHeatmapTooltip(c);
  });
  grid.addEventListener('touchstart', e => {
    const c = cellFrom(e);
    if (!c) return;
    showHeatmapTooltip(e, c);
    e.preventDefault();
  }, { passive: false });
}

// counts games where both players are inside the range
function lpSliderHtml() {
  if (!lpBounds || lpBounds[1] <= lpBounds[0]) return '';
  const [lo, hi] = lpBounds;
  return `
    <div class="elo-filter">
      <div class="elo-filter-head">
        <span class="elo-filter-label">LP Range</span>
        <span class="elo-filter-values"><span id="act-min-label">${lpMin}</span> - <span id="act-max-label">${lpMax}</span></span>
      </div>
      <div class="elo-slider">
        <div class="elo-slider-track"><div class="elo-slider-fill" id="act-fill"></div></div>
        <input type="range" id="act-min" class="elo-range" min="${lo}" max="${hi}" value="${lpMin}" step="10" aria-label="Minimum LP">
        <input type="range" id="act-max" class="elo-range" min="${lo}" max="${hi}" value="${lpMax}" step="10" aria-label="Maximum LP">
      </div>
    </div>`;
}

// thumbs move the fill straight away and refetch once dragging settles
function bindLpSlider() {
  const minI = document.getElementById('act-min');
  const maxI = document.getElementById('act-max');
  if (!minI || !maxI) return;
  const [lo, hi] = lpBounds;
  const span = (hi - lo) || 1;

  const paint = () => {
    const fill = document.getElementById('act-fill');
    fill.style.left = ((lpMin - lo) / span * 100) + '%';
    fill.style.width = ((lpMax - lpMin) / span * 100) + '%';
    document.getElementById('act-min-label').textContent = lpMin;
    document.getElementById('act-max-label').textContent = lpMax;
    // keeps the lower thumb grabbable when both sit at the top end
    minI.classList.toggle('on-top', lpMin > lo + span / 2);
  };
  const schedule = () => {
    clearTimeout(lpDebounce);
    lpDebounce = setTimeout(() => loadActivityHeatmap(), 180);
  };

  // thumbs cant cross each other
  minI.addEventListener('input', () => {
    lpMin = Math.min(parseInt(minI.value, 10), lpMax);
    minI.value = lpMin; paint(); schedule();
  });
  maxI.addEventListener('input', () => {
    lpMax = Math.max(parseInt(maxI.value, 10), lpMin);
    maxI.value = lpMax; paint(); schedule();
  });
  // pressing the bare track used to select the panel text
  // grabbing a thumb after that dragged the selection and blocked the slider
  document.querySelector('#act-min').closest('.elo-filter')
    .addEventListener('dragstart', e => e.preventDefault());
  paint();
}

export const actions = {
  'activity-window': el => loadActivityHeatmap(el.dataset.value),
};
