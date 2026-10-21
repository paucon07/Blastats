// activity view, matches bucketed by weekday and hour
import { API, esc, plural, placeTooltip } from './util.js';

let activityWindow = 'all';
// only count matches from players over 1900 lp
let activityHighLP = true;
let activityCache = null;
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

// fetch match times once then filter by window locally
export async function loadActivityHeatmap(timeWindow) {
  if (timeWindow) activityWindow = timeWindow;
  const container = document.getElementById('activity-heatmap-container');
  if (!container) return;

  if (!activityCache) container.innerHTML = '<div class="loading">Loading activity data...</div>';

  try {
    const minLp = activityHighLP ? 1900 : 0;
    if (!activityCache || activityCache.minLp !== minLp) {
      const r = await fetch(`${API}/activity?min_lp=${minLp}`);
      const d = await r.json();
      activityCache = { timestamps: d.timestamps || [], minLp };
    }

    let timestamps = activityCache.timestamps;
    if (activityWindow === 'week') {
      const cutoff = Date.now() - 7 * 864e5;
      timestamps = timestamps.filter(ts => new Date(ts).getTime() >= cutoff);
    }
    renderHeatmap(timestamps);
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
function renderHeatmap(timestamps) {
  const container = document.getElementById('activity-heatmap-container');
  if (!container) return;

  const buckets = Array.from({ length: 7 }, () => Array(24).fill(0));
  for (const iso of timestamps) {
    const d = new Date(iso);
    if (!isNaN(d)) buckets[d.getDay()][d.getHours()]++;
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
      const tooltip = `${DAY_LABELS[day]} ${String(hour).padStart(2, '0')}:00, ${count} ${plural(count, 'match', 'matches')}`;
      // empty cells keep the default background from css
      const bg = count ? ` style="background:${heatColor(intensity(count))}"` : '';
      cells += `<div class="hm-cell" data-tooltip="${tooltip}"${bg}></div>`;
    });
  });

  const swatches = [0, 0.2, 0.4, 0.6, 0.8, 1]
    .map(t => `<div class="hm-swatch" style="background:${heatColor(t)}"></div>`).join('');
  const windowBtn = (value, label) =>
    `<button class="${activityWindow === value ? 'active' : ''}" data-action="activity-window" data-value="${value}">${label}</button>`;

  container.innerHTML = `
    <div class="section-header compact">
      <div>
        <div class="section-title">Activity Heatmap</div>
        <div class="section-sub">Matches by day &amp; hour, ${esc(tzName)} time</div>
      </div>
      <div class="season-toggle">${windowBtn('all', 'All Time')}${windowBtn('week', 'Past Week')}</div>
    </div>
    <div class="table-wrap heatmap-wrap">
      <div class="heatmap-grid">${cells}</div>
      <div class="hm-legend">
        <span>LESS</span>
        <div class="hm-scale">${swatches}</div>
        <span>MORE</span>
        <span class="hm-peak">PEAK: ${max} ${plural(max, 'match', 'matches')} in one hour</span>
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

export const actions = {
  'activity-window': el => loadActivityHeatmap(el.dataset.value),
  // no button for this yet, give one data-action="activity-toggle-lp" to use it
  'activity-toggle-lp': () => {
    activityHighLP = !activityHighLP;
    activityCache = null;
    loadActivityHeatmap();
  },
};
