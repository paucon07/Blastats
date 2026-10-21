// head to head section on the profile
import { API, esc, formatTime, fmtDuration, plural } from './util.js';
import { watchLinkHtml } from './stream.js';
import { matchDetailHtml } from './matchdetail.js';
import { weaponCardsHtml } from './playerweapons.js';
import { attachSuggestions } from './suggest.js';

// hook up the opponent box renderProfile just drew
export function attachH2H(playerName) {
  const input = document.getElementById('h2h-input');
  const box = document.getElementById('h2h-suggestions');
  if (!input || !box) return;
  attachSuggestions(input, box, {
    exclude: name => name.toLowerCase() === playerName.toLowerCase(),
    onSelect: opponent => loadH2H(playerName, opponent),
  });
}

// fetch and draw player vs opponent
async function loadH2H(playerName, opponentName) {
  const container = document.getElementById('h2h-results');
  if (!container) return;
  container.innerHTML = '<div class="loading">Loading head-to-head...</div>';
  try {
    const r = await fetch(`${API}/players/${encodeURIComponent(playerName)}/vs/${encodeURIComponent(opponentName)}`);
    if (!r.ok) {
      container.innerHTML = `<div class="error-msg">Could not load H2H against "${esc(opponentName)}"</div>`;
      return;
    }
    renderH2H(playerName, await r.json());
  } catch {
    container.innerHTML = '<div class="error-msg">Failed to fetch head-to-head</div>';
  }
}

// one match card plus hidden detail if it has rounds
function matchRowHtml(m, idx, playerName) {
  const isWin = m.winner?.toLowerCase() === playerName.toLowerCase();
  const canExpand = !!m.spectated && (m.rounds || []).length > 0;
  const detailId = `h2h-detail-${idx}`;
  const rowAttrs = canExpand ? ` data-action="toggle-detail" data-target="${detailId}"` : '';

  const card = `
    <div class="match-result${canExpand ? ' row-expandable' : ''}"${rowAttrs}>
      <div>
        ${canExpand ? '<span class="row-chev">▸</span> ' : ''}<span class="${isWin ? 'win-badge' : 'loss-badge'}">${isWin ? 'WIN' : 'LOSS'}</span>
      </div>
      <div class="ta-center">
        <div class="match-score">${esc(m.score || '?')}</div>
        <div class="round-winners">${(m.roundWinners || []).map(esc).join(', ')}</div>
      </div>
      <div class="ta-right">
        <div class="match-time">${formatTime(m.timestamp)}</div>
        ${watchLinkHtml(m)}
      </div>
    </div>`;
  const detail = canExpand
    ? `<div class="detail-row" id="${detailId}" hidden>${matchDetailHtml(m)}</div>`
    : '';
  return card + detail;
}

// header, summary, collapsed match list and weapons
function renderH2H(playerName, data) {
  const container = document.getElementById('h2h-results');
  if (!container) return;

  if (!data.totalMatches) {
    container.innerHTML = `<div class="loading">No tracked matches between ${esc(playerName)} and ${esc(data.playerTwo)}</div>`;
    return;
  }

  const n = data.totalMatches;
  const matchRows = (data.matches || []).map((m, i) => matchRowHtml(m, i, playerName)).join('');

  container.innerHTML = `
    <div class="profile-header h2h-header">
      <div>
        <div class="h2h-title">${esc(playerName)} <span class="h2h-vs">VS</span> ${esc(data.playerTwo)}</div>
        <div class="profile-meta">${n} ${plural(n, 'match', 'matches')} tracked</div>
      </div>
      <div class="profile-stats">
        <div class="profile-stat">
          <div class="profile-stat-val green">${esc(data.playerOneWins)}</div>
          <div class="profile-stat-label">Wins</div>
        </div>
        <div class="profile-stat">
          <div class="profile-stat-val red">${esc(data.playerTwoWins)}</div>
          <div class="profile-stat-label">Losses</div>
        </div>
        <div class="profile-stat">
          <div class="profile-stat-val blue">${(data.playerOneWinRate ?? 0).toFixed(1)}%</div>
          <div class="profile-stat-label">Win Rate</div>
        </div>
      </div>
    </div>
    ${summaryHtml(data.summaryStats)}
    <div class="table-wrap" id="h2h-match-list" hidden>${matchRows}</div>
    <button class="btn-ghost btn-block" id="h2h-toggle" data-action="h2h-matches">
      ▼ Show ${n} ${plural(n, 'match', 'matches')}
    </button>
    ${weaponsHtml(playerName, data.weaponStats)}`;
}

// matchup stats from spectated matches, skips any the api didnt send
function summaryHtml(s) {
  if (!s?.spectatedMatches) return '';
  const item = (val, label, cls = '') =>
    `<div class="h2h-sum-item"><div class="h2h-sum-val ${cls}">${val}</div><div class="h2h-sum-label">${label}</div></div>`;

  const items = [];
  if (s.avgRoundTime != null) items.push(item(fmtDuration(s.avgRoundTime), 'Avg round time'));
  if (s.avgHealthDiff != null) {
    const v = s.avgHealthDiff;
    items.push(item(`${v > 0 ? '+' : ''}${v.toFixed(1)}`, 'Avg HP margin / round', v >= 0 ? 'wpn-good' : 'wpn-bad'));
  }
  if (s.roundWinRate != null) items.push(item(`${s.roundWinRate.toFixed(1)}%`, 'Round win rate'));
  if (s.avgRoundsPerMatch != null) items.push(item(esc(s.avgRoundsPerMatch), 'Rounds / match'));
  if (!items.length) return '';

  const n = s.spectatedMatches;
  return `
    <div class="section-sub h2h-caption">Matchup stats,  ${n} spectated ${plural(n, 'match', 'matches')}</div>
    <div class="h2h-summary">${items.join('')}</div>`;
}

// collapsed weapon cards for this matchup
function weaponsHtml(playerName, ws) {
  if (!ws?.sampleMatches) return '';
  const n = ws.sampleMatches;
  const dmgNote = ws.damageMatches < n ? `, damage from ${ws.damageMatches}` : '';
  return `
    <button class="btn-ghost btn-block" id="h2h-weapons-toggle" data-action="h2h-weapons">
      ▼ ${esc(playerName)}'s weapons in this matchup
    </button>
    <div class="h2h-weapons" id="h2h-weapons" hidden>
      <div class="section-sub">${n} spectated ${plural(n, 'match', 'matches')}${dmgNote}</div>
      ${weaponCardsHtml(ws, 'h2hwp')}
    </div>`;
}

export const actions = {
  // show or hide the match list
  'h2h-matches': btn => {
    const list = document.getElementById('h2h-match-list');
    if (!list) return;
    list.hidden = !list.hidden;
    const count = list.querySelectorAll('.match-result').length;
    btn.textContent = `${list.hidden ? '▼ Show' : '▲ Hide'} ${count} ${plural(count, 'match', 'matches')}`;
  },
  // show or hide the weapon cards, keeps the label and flips the arrow
  'h2h-weapons': btn => {
    const box = document.getElementById('h2h-weapons');
    if (!box) return;
    box.hidden = !box.hidden;
    const label = btn.textContent.replace(/[▼▲]/g, '').trim();
    btn.textContent = `${box.hidden ? '▼' : '▲'} ${label}`;
  },
};
