// weapons view, elo filter, usage vs win rate plot, player filter and stats table
import { API, esc, iconUrl, weaponDisplay, activate } from './util.js';

// below these sample sizes a number is shown as -
const WPN_WR_MIN = 5;
const WPN_DMG_MIN = 5;
// weapons used by fewer players than this stay off the plot
const WPN_MIN_PLAYERS = 3;

let weaponMode = 'both';
let weaponSort = 'usage';
let weaponSortDir = 'desc';
let weaponData = null;
let weaponEloBounds = null;
let weaponEloMin = null;
let weaponEloMax = null;
// bumped per fetch so late responses get dropped
let weaponFetchSeq = 0;
let weaponSliderDebounce = null;
// lowercase names left out of the stats
const weaponExclude = new Set();
let weaponPlayersOpen = false;
let weaponPlayerListKey = null;

const loading = text => `<div class="loading">${text}</div>`;

// first visit gets the elo range, later visits refetch with the current filters
export async function loadWeapons() {
  const table = document.getElementById('weapons-table');
  if (weaponEloBounds) {
    renderWeaponControls(weaponEloBounds);
    renderWeaponsBody();
    fetchWeapons();
    return;
  }

  if (table) table.innerHTML = loading('Loading weapon stats...');
  try {
    const r = await fetch(`${API}/weapons`);
    const d = await r.json();
    weaponData = d;
    const [lo, hi] = d.eloRange || [null, null];
    if (lo == null || hi == null) {
      renderWeaponControls(null);
    } else {
      weaponEloBounds = [Math.floor(lo), Math.ceil(hi)];
      [weaponEloMin, weaponEloMax] = weaponEloBounds;
      renderWeaponControls(weaponEloBounds);
    }
    renderWeaponsBody();
  } catch {
    if (table) table.innerHTML = loading('Failed to load weapon stats');
  }
}

// refetch with mode, elo range and excluded players
async function fetchWeapons() {
  const seq = ++weaponFetchSeq;
  try {
    const params = new URLSearchParams({ mode: weaponMode });
    if (weaponEloMin != null) params.set('min_elo', weaponEloMin);
    if (weaponEloMax != null) params.set('max_elo', weaponEloMax);
    if (weaponExclude.size) params.set('exclude', [...weaponExclude].join(','));
    const r = await fetch(`${API}/weapons?${params}`);
    const d = await r.json();
    if (seq !== weaponFetchSeq) return;
    weaponData = d;
    renderWeaponsBody();
  } catch {
    if (seq === weaponFetchSeq) {
      const t = document.getElementById('weapons-table');
      if (t) t.innerHTML = loading('Failed to load weapon stats');
    }
  }
}

// two thumb elo slider
// two range inputs stacked on top of each other with a fill between them
function renderWeaponControls(bounds) {
  const el = document.getElementById('weapon-controls');
  if (!el) return;
  if (!bounds) { el.innerHTML = ''; return; }
  const [lo, hi] = bounds;
  el.innerHTML = `
    <div class="elo-filter">
      <div class="elo-filter-head">
        <span class="elo-filter-label">ELO Range</span>
        <span class="elo-filter-values"><span id="elo-min-label">${weaponEloMin}</span> - <span id="elo-max-label">${weaponEloMax}</span></span>
      </div>
      <div class="elo-slider">
        <div class="elo-slider-track"><div class="elo-slider-fill" id="elo-fill"></div></div>
        <input type="range" id="elo-min" class="elo-range" min="${lo}" max="${hi}" value="${weaponEloMin}" step="1" aria-label="Minimum ELO">
        <input type="range" id="elo-max" class="elo-range" min="${lo}" max="${hi}" value="${weaponEloMax}" step="1" aria-label="Maximum ELO">
      </div>
    </div>`;

  const minI = document.getElementById('elo-min');
  const maxI = document.getElementById('elo-max');
  const span = (hi - lo) || 1;

  // move the fill and labels to match the thumbs
  const paint = () => {
    const a = Math.min(weaponEloMin, weaponEloMax);
    const b = Math.max(weaponEloMin, weaponEloMax);
    const fill = document.getElementById('elo-fill');
    if (fill) {
      fill.style.left = ((a - lo) / span * 100) + '%';
      fill.style.width = ((b - a) / span * 100) + '%';
    }
    document.getElementById('elo-min-label').textContent = a;
    document.getElementById('elo-max-label').textContent = b;
      // fixes issue where sliders get stuck when overlapping
    minI.classList.toggle('on-top', a > lo + span / 2);
  };

  // wait for dragging to settle before fetching
  const schedule = () => {
    clearTimeout(weaponSliderDebounce);
    weaponSliderDebounce = setTimeout(fetchWeapons, 180);
  };

  // thumbs cant cross each other
  minI.addEventListener('input', () => {
    let a = parseInt(minI.value, 10);
    const b = parseInt(maxI.value, 10);
    if (a > b) { a = b; minI.value = a; }
    weaponEloMin = a; paint(); schedule();
  });
  maxI.addEventListener('input', () => {
    const a = parseInt(minI.value, 10);
    let b = parseInt(maxI.value, 10);
    if (b < a) { b = a; maxI.value = b; }
    weaponEloMax = b; paint(); schedule();
  });
  paint();
}

// everything under the slider
function renderWeaponsBody() {
  updateWeaponSampleLine();
  renderWeaponPlayers();
  renderWeaponScatter();
  renderWeaponsTable();
}

// collapsible player list with checkboxes to leave players out
function renderWeaponPlayers() {
  const host = document.getElementById('weapon-players');
  if (!host) return;
  const list = weaponData?.playerList || [];
  if (!list.length) { host.innerHTML = ''; weaponPlayerListKey = null; return; }

  // same list as last time so keep it and its scroll position
  const key = list.map(p => p.name + ':' + p.entries).join('|');
  if (key === weaponPlayerListKey && host.firstChild) {
    updateWeaponOmittedCount();
    return;
  }
  weaponPlayerListKey = key;

  const rows = list.map(p => `
    <label class="wpn-player-row">
      <input type="checkbox" ${weaponExclude.has(p.name.toLowerCase()) ? '' : 'checked'} data-change="weapon-player" data-name="${esc(p.name)}">
      <span class="wpn-player-name">${esc(p.name)}</span>
      <span class="wpn-player-entries">${p.entries.toLocaleString()}</span>
    </label>`).join('');

  host.innerHTML = `
    <div class="wpn-players">
      <button class="wpn-players-head" data-action="weapon-players-toggle">
        <span>Players (${list.length})<span id="wpn-omitted" class="wpn-omitted"></span></span>
        <span class="wpn-players-chev">${weaponPlayersOpen ? '▾' : '▸'}</span>
      </button>
      <div class="wpn-players-body"${weaponPlayersOpen ? '' : ' hidden'}>
        <div class="wpn-players-hint">Uncheck a player to omit their matches. A player who uses a particular weapon all the time can skew the data for it.</div>
        <button class="wpn-players-reset" id="wpn-reset" data-action="weapon-players-reset" hidden>Re-enable all</button>
        <div class="wpn-players-list">${rows}</div>
      </div>
    </div>`;
  updateWeaponOmittedCount();
}

// omitted count in the header and the reset button
function updateWeaponOmittedCount() {
  const el = document.getElementById('wpn-omitted');
  if (el) el.textContent = weaponExclude.size ? `,  ${weaponExclude.size} omitted` : '';
  const reset = document.getElementById('wpn-reset');
  if (reset) reset.hidden = !weaponExclude.size;
}

// open or close the player list
function toggleWeaponPlayers() {
  weaponPlayersOpen = !weaponPlayersOpen;
  const body = document.querySelector('.wpn-players-body');
  const chev = document.querySelector('.wpn-players-chev');
  if (body) body.hidden = !weaponPlayersOpen;
  if (chev) chev.textContent = weaponPlayersOpen ? '▾' : '▸';
}

// include or exclude one player and refetch
function toggleWeaponPlayer(name) {
  const key = name.toLowerCase();
  if (weaponExclude.has(key)) weaponExclude.delete(key);
  else weaponExclude.add(key);
  updateWeaponOmittedCount();
  fetchWeapons();
}

// tick every player again
function resetWeaponPlayers() {
  if (!weaponExclude.size) return;
  weaponExclude.clear();
  document.querySelectorAll('.wpn-player-row input[type="checkbox"]').forEach(cb => { cb.checked = true; });
  updateWeaponOmittedCount();
  fetchWeapons();
}

// sample size line under the title
function updateWeaponSampleLine() {
  const sub = document.getElementById('weapons-sub');
  if (!sub) return;
  const d = weaponData || {};
  const loadouts = d.sampleLoadouts || 0;
  if (!loadouts) {
    sub.textContent = d.totalSpectatedMatches ? 'No loadouts in this ELO range' : 'No weapon data yet';
    return;
  }
  const modeWord = weaponMode === 'opponent' ? 'opponent in range' : 'both players in range';
  sub.textContent = `${(d.sampleMatches || 0).toLocaleString()} matches, ${loadouts.toLocaleString()} loadouts, ${modeWord}`;
}

const THIN = '<span class="wpn-thin" title="Not enough data">-</span>';

// win rate excluding mirror matches
function weaponWinCell(w) {
  if (w.winRateNonMirror == null || (w.winRateNonMirrorSample || 0) < WPN_WR_MIN) return THIN;
  const cls = w.winRateNonMirror >= 50 ? 'wpn-good' : 'wpn-bad';
  return `<span class="${cls}">${w.winRateNonMirror.toFixed(1)}%</span>`;
}

function weaponDmgCell(w) {
  if (w.avgDmgPerRound == null || (w.dmgRounds || 0) < WPN_DMG_MIN) return THIN;
  return esc(w.avgDmgPerRound);
}

// stats table sorted by the chosen column
function renderWeaponsTable() {
  const wrap = document.getElementById('weapons-table');
  if (!wrap) return;
  const weapons = (weaponData?.weapons || []).slice();
  if (!weapons.length) {
    wrap.innerHTML = loading('No weapons match this filter');
    return;
  }

  // missing values always sort last
  const dir = weaponSortDir === 'asc' ? 1 : -1;
  const key = weaponSort;
  weapons.sort((a, b) => {
    if (key === 'weapon') return weaponDisplay(a.weapon).localeCompare(weaponDisplay(b.weapon)) * dir;
    const av = a[key], bv = b[key];
    if (av == null && bv == null) return 0;
    if (av == null) return 1;
    if (bv == null) return -1;
    return (av - bv) * dir;
  });

  const th = (k, label) => {
    const arrow = weaponSort === k ? `<span class="wpn-arrow">${weaponSortDir === 'desc' ? '▾' : '▴'}</span>` : '';
    return `<th class="wpn-sortable" data-action="sort-weapons" data-key="${k}">${label} ${arrow}</th>`;
  };

  const rows = weapons.map(w => {
    const disp = weaponDisplay(w.weapon);
    return `<tr>
      <td><span class="wpn-cell"><img src="${iconUrl(disp)}" alt=""><span>${esc(disp)}</span></span></td>
      <td>${w.usage != null ? w.usage.toFixed(1) + '%' : '-'}</td>
      <td>${weaponWinCell(w)}</td>
      <td>${weaponDmgCell(w)}</td>
      <td class="wpn-thin">${(w.loadouts || 0).toLocaleString()}</td>
      <td class="wpn-thin">${(w.players || 0).toLocaleString()}</td>
    </tr>`;
  }).join('');

  wrap.innerHTML = `
    <table class="wpn-table">
      <thead><tr>
        ${th('weapon', 'Weapon')}
        ${th('usage', 'Usage')}
        ${th('winRateNonMirror', 'Win %')}
        ${th('avgDmgPerRound', 'Dmg / Round')}
        ${th('loadouts', 'Games')}
        ${th('players', 'Players')}
      </tr></thead>
      <tbody class="fade-in">${rows}</tbody>
    </table>`;
}

// same column flips direction, new column starts descending
// except weapon name which starts a to z
function sortWeapons(key) {
  if (weaponSort === key) {
    weaponSortDir = weaponSortDir === 'desc' ? 'asc' : 'desc';
  } else {
    weaponSort = key;
    weaponSortDir = key === 'weapon' ? 'asc' : 'desc';
  }
  renderWeaponsTable();
}

// usage on x, win rate on y, dot size is sample size
function renderWeaponScatter() {
  const host = document.getElementById('weapon-scatter');
  if (!host) return;
  const pts = (weaponData?.weapons || []).filter(w =>
    w.winRateNonMirror != null &&
    (w.winRateNonMirrorSample || 0) >= WPN_WR_MIN &&
    (w.players || 0) >= WPN_MIN_PLAYERS &&
    w.usage != null);
  if (pts.length < 3) {
    host.innerHTML = '<div class="weapon-scatter"><div class="ws-empty">Not enough non-mirror data in this range for a usage / win-rate plot</div></div>';
    return;
  }

  const W = 720, H = 320;
  const pad = { l: 46, r: 18, t: 18, b: 40 };
  // y always covers at least 40 to 60, in steps of 5
  const wrVals = pts.map(p => p.winRateNonMirror);
  const ylo = Math.min(40, Math.floor((Math.min(...wrVals) - 4) / 5) * 5);
  const yhi = Math.max(60, Math.ceil((Math.max(...wrVals) + 4) / 5) * 5);
  const xhi = Math.max(1, Math.max(...pts.map(p => p.usage)) * 1.1);
  const maxSample = Math.max(...pts.map(p => p.winRateNonMirrorSample));

  const px = u => pad.l + (u / xhi) * (W - pad.l - pad.r);
  const py = v => pad.t + (1 - (v - ylo) / (yhi - ylo)) * (H - pad.t - pad.b);
  const rOf = s => 3 + Math.min(9, Math.sqrt(s / maxSample) * 9);

  const yticks = [];
  for (let v = ylo; v <= yhi + 0.001; v += (yhi - ylo) / 4) yticks.push(Math.round(v));
  const xticks = [];
  for (let u = 0; u <= xhi + 0.001; u += xhi / 4) xticks.push(u);

  // dashed 50% line, added separately if no tick lands on it
  const hline = (cls, v) =>
    `<line class="${cls}" x1="${pad.l}" y1="${py(v).toFixed(1)}" x2="${W - pad.r}" y2="${py(v).toFixed(1)}"/>`;
  const grid = yticks.map(v => hline(Math.abs(v - 50) < 0.01 ? 'ws-mid' : 'ws-grid', v)).join('');
  const midLine = yticks.some(v => Math.abs(v - 50) < 0.01) ? '' : hline('ws-mid', 50);

  const ylabels = yticks.map(v =>
    `<text class="ws-axis" x="${pad.l - 8}" y="${(py(v) + 3).toFixed(1)}" text-anchor="end">${v}</text>`).join('');
  const xlabels = xticks.map(u =>
    `<text class="ws-axis" x="${px(u).toFixed(1)}" y="${H - pad.b + 18}" text-anchor="middle">${u.toFixed(u < 10 ? 1 : 0)}%</text>`).join('');

  const dots = pts.map(p => {
    const cls = p.winRateNonMirror >= 50 ? 'ws-dot-good' : 'ws-dot-bad';
    return `<circle class="ws-dot ${cls}" cx="${px(p.usage).toFixed(1)}" cy="${py(p.winRateNonMirror).toFixed(1)}" r="${rOf(p.winRateNonMirrorSample).toFixed(1)}">
      <title>${esc(weaponDisplay(p.weapon))}, ${p.usage}% usage, ${p.winRateNonMirror}% win (${p.winRateNonMirrorSample} games)</title>
    </circle>`;
  }).join('');

  const midY = (pad.t + (H - pad.b)) / 2;
  host.innerHTML = `
    <div class="weapon-scatter">
      <div class="ws-title">Usage vs Win Rate <span class="ws-hint">dot size = sample</span></div>
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" role="img">
        ${grid}${midLine}
        <line class="ws-grid" x1="${pad.l}" y1="${pad.t}" x2="${pad.l}" y2="${H - pad.b}"/>
        <line class="ws-grid" x1="${pad.l}" y1="${H - pad.b}" x2="${W - pad.r}" y2="${H - pad.b}"/>
        ${ylabels}${xlabels}
        <text class="ws-axis" x="${(pad.l + (W - pad.r)) / 2}" y="${H - 4}" text-anchor="middle">USAGE %</text>
        <text class="ws-axis" x="14" y="${midY}" text-anchor="middle" transform="rotate(-90 14 ${midY})">WIN %</text>
        ${dots}
      </svg>
    </div>`;
}

export const actions = {
  'weapon-mode': el => {
    if (el.dataset.value === weaponMode) return;
    weaponMode = el.dataset.value;
    activate(el);
    const t = document.getElementById('weapons-table');
    if (t) t.innerHTML = loading('Loading weapon stats...');
    fetchWeapons();
  },
  'sort-weapons': el => sortWeapons(el.dataset.key),
  'weapon-players-toggle': toggleWeaponPlayers,
  'weapon-players-reset': resetWeaponPlayers,
  'weapon-player': el => toggleWeaponPlayer(el.dataset.name),
};
