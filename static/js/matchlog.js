// match log view, every tracked match newest first
import { API, esc, formatTime } from './util.js';
import { watchLinkHtml } from './stream.js';
import { matchDetailHtml } from './matchdetail.js';
import { paginationHtml } from './pagination.js';

const PER_PAGE = 20;
// current page, total pages and pages already fetched
let state = null;

// fetch page 1 and reset paging
export async function loadMatches() {
  const table = document.getElementById('matches-table');
  const pager = document.getElementById('matches-pagination');
  table.innerHTML = '<div class="loading">Loading matches...</div>';
  if (pager) pager.innerHTML = '';

  const st = { page: 1, totalPages: 1, pages: {} };
  state = st;

  try {
    const r = await fetch(`${API}/matches?page=1&per_page=${PER_PAGE}`);
    if (!r.ok) throw new Error(`status ${r.status}`);
    const d = await r.json();
    // a newer load started while this one was waiting
    if (state !== st) return;
    if (!d.matches?.length) {
      table.innerHTML = '<div class="loading">No matches tracked yet</div>';
      return;
    }
    st.pages[1] = d.matches;
    st.totalPages = d.totalPages || 1;
    render();
  } catch {
    if (state === st) table.innerHTML = '<div class="loading">Failed to load matches</div>';
  }
}

// table row plus hidden detail row for spectated matches
function rowHtml(m, idx) {
  const [p1, p2] = m.players || [];
  const winner = m.winner;
  // score is stored winner first so flip it when p2 won
  const score = p2?.displayName === winner ? m.score.split('-').reverse().join('-') : m.score;
  const canExpand = !!m.spectated;
  const detailId = `detail-log-${idx}`;

  // names only link to the profile when the row itself doesnt expand
  const nameCell = p => {
    const name = p?.displayName;
    const won = name === winner ? ' won' : '';
    const inner = `${esc(name || '?')}<span class="mlog-lp"> ${esc(p?.leaguePoints || 0)} LP</span>`;
    return canExpand
      ? `<td class="mlog-player${won}">${inner}</td>`
      : `<td class="mlog-player clickable${won}" data-action="player" data-name="${esc(name)}">${inner}</td>`;
  };

  const rowAttrs = canExpand
    ? ` class="row-expandable" data-action="toggle-detail" data-target="${detailId}"`
    : '';

  const main = `
    <tr${rowAttrs}>
      <td class="match-time">${canExpand ? '<span class="row-chev">▸</span> ' : ''}${formatTime(m.timestamp)}</td>
      ${nameCell(p1)}
      <td class="mlog-score">${esc(score)}</td>
      ${nameCell(p2)}
      <td class="ta-center">${watchLinkHtml(m, true)}</td>
    </tr>`;
  const detail = canExpand
    ? `<tr class="detail-row" id="${detailId}" hidden><td colspan="5">${matchDetailHtml(m)}</td></tr>`
    : '';
  return main + detail;
}

// draw the current page and pager
function render() {
  const st = state;
  const container = document.getElementById('matches-table');
  const pager = document.getElementById('matches-pagination');
  if (!st || !container) return;

  container.innerHTML = `
    <table>
      <thead><tr>
        <th class="mlog-time-col">Time</th>
        <th class="ta-center">Player 1</th>
        <th class="ta-center mlog-score-col">Score</th>
        <th class="ta-center">Player 2</th>
        <th class="ta-center mlog-replay-col">Replay</th>
      </tr></thead>
      <tbody class="fade-in">${(st.pages[st.page] || []).map(rowHtml).join('')}</tbody>
    </table>`;
  if (pager) pager.innerHTML = paginationHtml(st.page, st.totalPages, 'log-page');
}

// go to page n, fetching it if not cached
async function gotoPage(n) {
  const st = state;
  if (!st || n < 1 || n > st.totalPages || n === st.page) return;

  if (!st.pages[n]) {
    const pager = document.getElementById('matches-pagination');
    try {
      const r = await fetch(`${API}/matches?page=${n}&per_page=${PER_PAGE}`);
      if (!r.ok) throw new Error(`status ${r.status}`);
      const data = await r.json();
      if (state !== st) return;
      st.pages[n] = data.matches || [];
      if (data.totalPages) st.totalPages = data.totalPages;
    } catch {
      if (state === st && pager) {
        pager.insertAdjacentHTML('beforeend', '<span class="page-error">failed to load page</span>');
      }
      return;
    }
  }

  st.page = n;
  render();
  document.getElementById('matches-table')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

export const actions = {
  'refresh-matches': loadMatches,
  'log-page': el => gotoPage(+el.dataset.page),
};
