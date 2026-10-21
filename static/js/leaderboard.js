// leaderboard view, header stats and season countdown
import { API, esc, formatTime, activate } from './util.js';

// season lp above this counts as active
const ACTIVE_LP = 1900;

let filter = 'lp';
let season = 'current';
let activeOnly = true;
let streakMode = 'highest';
let streakSide = 'win';

// api status light and tracked match count
export async function loadStatus() {
  try {
    const r = await fetch(`${API}/status`);
    const d = await r.json();
    document.getElementById('status-text').textContent = 'LIVE';
    document.getElementById('gs-matches').textContent = d.matchesTracked.toLocaleString();
  } catch {
    document.getElementById('status-text').textContent = 'API OFFLINE';
  }
}

// switch season lp between current and previous
export function loadLeaderboard(s, btn) {
  if (btn) activate(btn);
  season = s;
  renderLeaderboard();
}

// season lp straight from the leaderboard endpoint
async function fetchSeasonLp(lbUpdated) {
  const r = await fetch(`${API}/leaderboard?season=${season}`);
  const d = await r.json();
  lbUpdated.textContent += `Season ${d.version || '?'}` + (d.lastUpdated ? `, updated ${formatTime(d.lastUpdated)}` : '');
  return (d.leaderboard || []).map(p => ({ displayName: p.displayName, value: p.value, valueLabel: 'LP' }));
}

// tournament earnings with season lp alongside
async function fetchEarnings() {
  const [earningsRes, playersRes] = await Promise.all([
    fetch(`${API}/leaderboard/earnings`),
    fetch(`${API}/players?limit=500`),
  ]);
  const earningsData = await earningsRes.json();
  const playersData = await playersRes.json();

  // name to season lp
  const lpMap = {};
  for (const p of playersData.players || []) lpMap[p.displayName] = p.currentSeasonLP || null;

  return (earningsData.leaderboard || [])
    .filter(p => !activeOnly || (lpMap[p.displayName] || 0) > ACTIVE_LP)
    .map(p => ({
      displayName: p.displayName,
      value: p.totalEarnings,
      valueLabel: 'Earnings',
      formattedValue: `$${p.totalEarnings.toFixed(2)}`,
      secondaryValue: lpMap[p.displayName] ?? null,
    }));
}

// top 100 by best or current win or loss streak
async function fetchStreaks(sideWord) {
  const r = await fetch(`${API}/players?limit=500&streaks=1`);
  const d = await r.json();
  const useHighest = streakMode === 'highest';
  const wantWin = streakSide === 'win';
  return (d.players || [])
    .map(p => ({
      displayName: p.displayName,
      // current streak only counts if its the side we want
      value: useHighest
        ? (wantWin ? p.bestWinStreak : p.bestLossStreak) || 0
        : (p.streakType === streakSide ? p.currentStreak || 0 : 0),
      currentSeasonLP: p.currentSeasonLP || null,
    }))
    .filter(p => p.value > 0)
    .filter(p => !activeOnly || (p.currentSeasonLP || 0) > ACTIVE_LP)
    .sort((a, b) => b.value - a.value)
    .slice(0, 100)
    .map(p => ({
      displayName: p.displayName,
      value: p.value,
      valueLabel: `${sideWord} Streak`,
      secondaryValue: p.currentSeasonLP,
    }));
}

// filters that just sort players by one stat
const STAT_FILTERS = {
  elo: { stat: 'ELO_RATING', label: 'ELO' },
  total_lp: { stat: 'LEAGUE_POINTS_TOTAL', label: 'Total LP' },
  rounds: { stat: 'ROUNDS_WON', label: 'Rounds Won' },
};

// top 100 by the current stat filter
async function fetchStat() {
  const { stat, label } = STAT_FILTERS[filter];
  const r = await fetch(`${API}/players?limit=500`);
  const d = await r.json();
  return (d.players || [])
    .filter(p => p.stats?.[stat])
    .filter(p => !activeOnly || (p.currentSeasonLP || 0) > ACTIVE_LP)
    .sort((a, b) => b.stats[stat] - a.stats[stat])
    .slice(0, 100)
    .map(p => ({ displayName: p.displayName, value: p.stats[stat], valueLabel: label, secondaryValue: p.currentSeasonLP || null }));
}

// one leaderboard row, top 3 get medal colours
function rowHtml(p, i) {
  const pos = i + 1;
  const rankClass = pos <= 3 ? `rank-${pos}` : '';
  // season lp column only exists outside the season lp filter
  const secondary = p.secondaryValue !== undefined
    ? `<td class="lb-secondary">${p.secondaryValue ? p.secondaryValue.toLocaleString() + ' LP' : '-'}</td>`
    : '<td></td>';
  return `
    <tr data-action="player" data-name="${esc(p.displayName)}">
      <td class="rank-num ${rankClass}">${pos}</td>
      <td class="player-name">${esc(p.displayName)}</td>
      <td class="lp-value">${esc(p.formattedValue ?? p.value?.toLocaleString())}</td>
      ${secondary}
    </tr>`;
}

// redraw for the current filter and toggles
async function renderLeaderboard() {
  const isLp = filter === 'lp';
  const isStreak = filter === 'streak';
  document.getElementById('season-toggle-lp').hidden = !isLp;
  document.getElementById('season-toggle-activity').hidden = isLp;
  document.getElementById('streak-mode-toggle').hidden = !isStreak;
  document.getElementById('streak-side-toggle').hidden = !isStreak;

  const table = document.getElementById('lb-table');
  table.innerHTML = '<div class="loading">Loading...</div>';

  const sideWord = streakSide === 'win' ? 'Win' : 'Loss';
  const sortLabels = {
    elo: 'ELO',
    total_lp: 'Total LP',
    rounds: 'Rounds Won',
    earnings: 'Tournament Earnings',
    streak: `${streakMode === 'highest' ? 'Highest' : 'Current'} ${sideWord} Streak`,
  };
  const lbUpdated = document.getElementById('lb-updated');
  // season lp fills this in itself once it knows the season number
  lbUpdated.textContent = isLp ? '' : `Sorted by ${sortLabels[filter] || ''}`;

  try {
    const players =
      isLp ? await fetchSeasonLp(lbUpdated)
      : filter === 'earnings' ? await fetchEarnings()
      : isStreak ? await fetchStreaks(sideWord)
      : await fetchStat();

    // top value card above the table
    if (players.length > 0) {
      const filterLabels = {
        lp: 'Top LP', elo: 'Top ELO', total_lp: 'Top Total LP',
        rounds: 'Most Rounds Won',
        streak: streakMode === 'highest' ? `Longest ${sideWord} Streak` : `Top Current ${sideWord} Streak`,
        earnings: 'Top Earnings',
      };
      document.getElementById('gs-top-label').textContent = filterLabels[filter] || 'Top Value';
      document.getElementById('gs-top-lp').textContent = players[0].value?.toLocaleString() || '-';
    }

    table.innerHTML = `
      <table>
        <thead><tr>
          <th>#</th><th>Player</th>
          <th>${esc(players[0]?.valueLabel || 'Value')}</th>
          <th>${isLp ? '' : 'Season LP'}</th>
        </tr></thead>
        <tbody class="fade-in">${players.map(rowHtml).join('')}</tbody>
      </table>`;
  } catch {
    table.innerHTML = '<div class="loading">Failed to load leaderboard</div>';
  }
}

// time left until the season ends
// seasons end at 23:00 utc on the last day of the month
function updateCountdown() {
  const now = new Date();
  let target = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + 1, 0, 23, 0, 0));
  // already past it so count to next month
  if (now >= target) {
    target = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + 2, 0, 23, 0, 0));
  }

  const diff = target - now;
  const days = Math.floor(diff / 864e5);
  const hours = Math.floor((diff % 864e5) / 36e5);
  const mins = Math.floor((diff % 36e5) / 6e4);
  const secs = Math.floor((diff % 6e4) / 1000);

  const el = document.getElementById('season-countdown');
  if (el) el.textContent = `${days}d ${hours}h ${mins}m ${secs}s`;
}

// tick the countdown every second
export function startCountdown() {
  updateCountdown();
  setInterval(updateCountdown, 1000);
}

export const actions = {
  'lb-filter': el => { filter = el.dataset.value; activate(el); renderLeaderboard(); },
  'lb-season': el => loadLeaderboard(el.dataset.season, el),
  'lb-active': el => { activeOnly = el.dataset.value === 'true'; activate(el); renderLeaderboard(); },
  'lb-streak-mode': el => { streakMode = el.dataset.value; activate(el); renderLeaderboard(); },
  'lb-streak-side': el => { streakSide = el.dataset.value; activate(el); renderLeaderboard(); },
};
