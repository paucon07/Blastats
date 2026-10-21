// player search view, profile header, loadouts and tracked matches
import { API, esc, formatTime, avatarUrl } from './util.js';
import { watchLinkHtml } from './stream.js';
import { matchDetailHtml, weaponIconHtml } from './matchdetail.js';
import { paginationHtml } from './pagination.js';
import { renderProfileChart } from './chart.js';
import { renderPlayerWeapons } from './playerweapons.js';
import { attachH2H } from './h2h.js';

const PER_PAGE = 20;
let matchPageState = null;

// look up the name in the search box
export async function searchPlayer() {
  const name = document.getElementById('search-input').value.trim();
  if (!name) return;

  // put the name in the url first, the hashchange handler calls back in here
  const target = '#search/' + encodeURIComponent(name);
  if (location.hash !== target) { location.hash = target; return; }

  const profile = document.getElementById('player-profile');
  const error = document.getElementById('search-error');
  error.innerHTML = '';
  profile.innerHTML = '<div class="loading">Searching...</div>';
  try {
    const r = await fetch(`${API}/players/${encodeURIComponent(name)}`);
    if (!r.ok) {
      profile.innerHTML = '';
      // name comes straight from the url so it has to be escaped
      error.innerHTML = `<div class="error-msg">Player "${esc(name)}" not found in database</div>`;
      return;
    }
    renderProfile(await r.json());
  } catch {
    profile.innerHTML = '<div class="loading">Failed to fetch player</div>';
  }
}

// empty search page
export function clearSearch() {
  document.getElementById('player-profile').innerHTML = '';
  document.getElementById('search-error').innerHTML = '';
  document.getElementById('search-input').value = '';
}

// account age like 12d, 5mo or 2.3y
function accountAgeLabel(iso) {
  const created = new Date(iso);
  if (isNaN(created)) return null;
  const days = (Date.now() - created.getTime()) / 864e5;
  if (days < 0) return null;
  if (days < 60) return `${Math.round(days)}d`;
  const years = days / 365.25;
  if (years < 1) return `${Math.round(days / 30.44)}mo`;
  return `${years.toFixed(1)}y`;
}

// account creation date
function accountStartedLabel(iso) {
  const created = new Date(iso);
  return isNaN(created) ? '' : created.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}

// on small screens shrink a long name until it fits on one line
// wraps instead if it still doesnt fit at the minimum size
export function fitProfileName() {
  const el = document.querySelector('#player-profile .profile-name');
  if (!el) return;
  el.style.fontSize = '';
  el.style.whiteSpace = 'nowrap';
  if (window.innerWidth > 640) { el.style.whiteSpace = ''; return; }
  let size = parseFloat(getComputedStyle(el).fontSize);
  const MIN = 16;
  let guard = 40;
  while (el.scrollWidth > el.clientWidth + 1 && size > MIN && guard-- > 0) {
    size -= 1;
    el.style.fontSize = size + 'px';
  }
  if (el.scrollWidth > el.clientWidth + 1) el.style.whiteSpace = '';
}

// big number in the profile header
const statBlock = (val, label, cls) => `
  <div class="profile-stat">
    <div class="profile-stat-val ${cls}">${val}</div>
    <div class="profile-stat-label">${label}</div>
  </div>`;

// card in the stat grid
const statCard = (label, val, cls = '') => `
  <div class="stat-card ${cls}">
    <div class="stat-label">${label}</div>
    <div class="stat-value">${val}</div>
  </div>`;

// whole profile page for player p
function renderProfile(p) {
  const stats = p.stats || {};
  const elo = stats.ELO_RATING;
  const mmr = stats.CALCULATED_MATCHMAKING_VALUE;
  const roundsWon = stats.ROUNDS_WON;
  const totalLP = stats.LEAGUE_POINTS_TOTAL;
  const streak = p.currentStreak || 0;
  const isWinStreak = p.streakType === 'win';
  const bestWinStreak = p.bestWinStreak || 0;
  const bestLossStreak = p.bestLossStreak || 0;
  const startedLabel = p.accountCreated ? accountStartedLabel(p.accountCreated) : null;
  const ageAgo = p.accountCreated ? accountAgeLabel(p.accountCreated) : null;
  const name = esc(p.displayName);
  const hasMatches = (p.matchesTracked || 0) > 0;

  document.getElementById('player-profile').innerHTML = `
    <div class="fade-in">
      <div class="profile-header">
        <div class="profile-id">
          ${p.lastAvatar ? `<img class="profile-avatar" src="${avatarUrl(p.lastAvatar)}" alt="">` : ''}
          <div>
            <div class="profile-name">${name}</div>
            <div class="profile-meta">
              ${p.currentSeasonPosition ? `<span class="tag">Rank #${esc(p.currentSeasonPosition)}</span>` : ''}
              ${startedLabel ? `<span class="tag"${ageAgo ? ` title="${ageAgo} ago"` : ''}>Started: ${startedLabel}</span>` : ''}
            </div>
          </div>
        </div>
        <div class="profile-stats">
          ${statBlock((p.currentSeasonLP || 0).toLocaleString(), 'Season LP', 'blue')}
          ${elo ? statBlock(elo.toLocaleString(), 'ELO', 'gold') : ''}
          ${mmr ? statBlock(esc(mmr), 'MMR', 'red') : ''}
          ${statBlock(`${esc(p.winRate || 0)}%`, 'Win Rate', 'green')}
          ${bestWinStreak ? statBlock(bestWinStreak, 'Top Win Streak', 'green') : ''}
          ${bestLossStreak ? statBlock(bestLossStreak, 'Top Loss Streak', 'red') : ''}
        </div>
      </div>
      <div class="stat-grid" id="stat-grid">
        ${statCard('Tracked Matches', p.matchesTracked || 0)}
        ${statCard('Wins', p.wins || 0, 'green')}
        ${statCard('Losses', p.losses || 0, 'red')}
        ${streak > 0 ? statCard(isWinStreak ? 'Win Streak' : 'Lose Streak', streak, isWinStreak ? 'green' : 'red') : ''}
        ${roundsWon ? statCard('Total Rounds Won', roundsWon.toLocaleString(), 'gold') : ''}
        ${totalLP ? statCard('Lifetime LP', totalLP.toLocaleString()) : ''}
      </div>
      ${loadoutsHtml(p)}
      <div id="lp-chart-container"></div>
      <div id="player-weapons"></div>
      <div class="section-header compact spaced">
        <div class="section-title">Head-to-Head</div>
      </div>
      <div class="search-wrap h2h-search">
        <input class="search-input" id="h2h-input" type="text" placeholder="ENTER OPPONENT NAME" autocomplete="off">
        <div id="h2h-suggestions" class="suggestions" hidden></div>
      </div>
      <div id="h2h-results"></div>
      ${hasMatches ? `
        <div class="section-header compact spaced">
          <div class="section-title">Tracked Matches</div>
        </div>
        <div class="table-wrap" id="match-list"></div>
        <div class="pagination" id="match-pagination"></div>` : `
        <div class="table-wrap">
          <div class="loading">No tracked matches for this player yet</div>
        </div>`}
    </div>`;

  attachH2H(p.displayName);

  // first page comes with the profile, later pages are fetched
  if (hasMatches) {
    matchPageState = {
      name: p.displayName,
      page: 1,
      totalPages: Math.max(1, Math.ceil(p.matchesTracked / PER_PAGE)),
      pages: { 1: p.recentMatches || [] },
    };
    renderMatchPage();
  } else {
    matchPageState = null;
  }

  loadTournaments(p.displayName);
  renderProfileChart(p);
  renderPlayerWeapons(p);
  fitProfileName();
}

// earnings card and placement tags, added once they arrive
function loadTournaments(profileName) {
  fetch(`${API}/players/${encodeURIComponent(profileName)}/tournaments`)
    .then(r => r.json())
    .then(data => {
      // user left the page or opened someone else meanwhile
      if (!document.getElementById('view-search')?.classList.contains('active')) return;
      if (document.querySelector('.profile-name')?.textContent !== profileName) return;

      const earned = data.totalPrizeMoney > 0 ? `$${data.totalPrizeMoney.toFixed(2)}` : '$0.00';
      document.getElementById('stat-grid')?.insertAdjacentHTML('beforeend', statCard('Tournament Earnings', earned, 'gold'));

      const meta = document.querySelector('.profile-meta');
      if (!data.results?.length || !meta) return;
      // best placement first
      const tags = data.results.slice().sort((a, b) => a.place - b.place).map(t => {
        const medal = t.place === 1 ? '🥇' : t.place === 2 ? '🥈' : t.place === 3 ? '🥉' : `#${esc(t.place)}`;
        const cls = t.place === 1 ? '' : t.place === 2 ? 'silver' : t.place === 3 ? 'bronze' : 'other';
        return `<span class="tournament-tag ${cls}">${medal} ${esc(t.name)}</span>`;
      }).join('');
      meta.insertAdjacentHTML('beforeend', `<div class="tournament-tags">${tags}</div>`);
    })
    .catch(() => {});
}

// up to 4 different loadouts from recent spectated matches
function loadoutsHtml(p) {
  const seen = new Set();
  const matches = (p.recentMatches || [])
    .filter(m => {
      if (!m.weapons?.length) return false;
      // same weapons in any order count as one loadout
      const key = m.weapons.map(w => `${w.name}|${w.attachment}`).sort().join(',');
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    })
    .slice(0, 4);
  if (!matches.length) return '';

  const cards = matches.map(m => `
    <div class="loadout-card">
      <div class="loadout-date">${formatTime(m.timestamp)}</div>
      <div class="weapon-grid">${m.weapons.map(w => weaponIconHtml(w, 'lg')).join('')}</div>
    </div>`).join('');

  // collapsed only matters on mobile, see styles.css
  return `
    <div class="loadout-section collapsed" id="loadout-section">
      <div class="section-header compact spaced loadout-header" data-action="toggle-loadouts">
        <div class="section-title">Recent Loadouts</div>
        <div class="section-sub">From spectated matches</div>
        <span class="loadout-toggle-icon">▾</span>
      </div>
      <div class="loadout-grid">${cards}</div>
    </div>`;
}

// one tracked match row plus hidden detail if spectated
function matchRowHtml(m, idx) {
  const isWin = m.result === 'win';
  const canExpand = !!m.spectated;
  const detailId = `detail-prof-${idx}`;
  const opp = esc(m.opponent || '?');
  // opponent only links to their profile when the row doesnt expand
  const oppHtml = canExpand
    ? `<span class="opp-name">${opp}</span>`
    : `<span class="opp-name clickable" data-action="player" data-name="${opp}">${opp}</span>`;
  const rowAttrs = canExpand ? ` data-action="toggle-detail" data-target="${detailId}"` : '';

  const row = `
    <div class="match-result${canExpand ? ' row-expandable' : ''}"${rowAttrs}>
      <div>
        <span class="${isWin ? 'win-badge' : 'loss-badge'}">${isWin ? 'WIN' : 'LOSS'}</span>
        ${oppHtml}
        <span class="opp-lp">${esc(m.opponentLP || 0)} LP</span>
      </div>
      <div class="ta-center">
        <div class="match-score">${esc(m.score || '?')}</div>
        <div class="round-winners">${(m.roundWinners || []).map(esc).join(', ')}</div>
      </div>
      <div class="ta-right">
        <div class="match-time">${formatTime(m.timestamp)}${canExpand ? ' <span class="row-chev">▸</span>' : ''}</div>
        ${watchLinkHtml(m)}
      </div>
    </div>`;
  const detail = canExpand
    ? `<div class="detail-row" id="${detailId}" hidden>${matchDetailHtml(m)}</div>`
    : '';
  return row + detail;
}

// draw the current page of tracked matches
function renderMatchPage() {
  const st = matchPageState;
  const list = document.getElementById('match-list');
  const pager = document.getElementById('match-pagination');
  if (!st || !list) return;
  const rows = st.pages[st.page] || [];
  list.innerHTML = rows.length
    ? rows.map(matchRowHtml).join('')
    : '<div class="loading">No matches on this page</div>';
  if (pager) pager.innerHTML = paginationHtml(st.page, st.totalPages, 'profile-page');
}

// go to page n, fetching it if not cached
async function gotoMatchPage(n) {
  const st = matchPageState;
  if (!st || n < 1 || n > st.totalPages || n === st.page) return;

  if (!st.pages[n]) {
    const pager = document.getElementById('match-pagination');
    try {
      const r = await fetch(`${API}/players/${encodeURIComponent(st.name)}/matches?page=${n}`);
      if (!r.ok) throw new Error(`status ${r.status}`);
      const data = await r.json();
      // another profile was opened while this was loading
      if (matchPageState !== st) return;
      st.pages[n] = data.matches || [];
      if (data.totalPages) st.totalPages = data.totalPages;
    } catch {
      if (matchPageState === st && pager) {
        pager.insertAdjacentHTML('beforeend', '<span class="page-error">failed to load page</span>');
      }
      return;
    }
  }

  st.page = n;
  renderMatchPage();
  document.getElementById('match-list')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

export const actions = {
  'profile-page': el => gotoMatchPage(+el.dataset.page),
  // loadouts only collapse on mobile
  'toggle-loadouts': () => {
    if (window.matchMedia('(max-width: 768px)').matches) {
      document.getElementById('loadout-section')?.classList.toggle('collapsed');
    }
  },
};
