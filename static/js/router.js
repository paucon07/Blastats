// hash routes: #leaderboard #matches #weapons #activity #search/<name>
import { loadMatches } from './matchlog.js';
import { loadWeapons } from './weapons.js';
import { loadActivityHeatmap } from './activity.js';
import { searchPlayer, clearSearch } from './profile.js';

// setting the hash fires hashchange which calls handleRoute
// same hash wont fire it so call it directly
function navigate(hash) {
  if (location.hash === hash) handleRoute();
  else location.hash = hash;
}

export const showView = name => navigate('#' + name);
export const goToPlayer = name => navigate('#search/' + encodeURIComponent(name));

// show the view in the hash and load its data
export function handleRoute() {
  const [view, ...rest] = location.hash.slice(1).split('/');
  const arg = rest.length ? decodeURIComponent(rest.join('/')) : null;
  const viewName = view || 'leaderboard';

  // unknown view goes to the leaderboard
  const target = document.getElementById('view-' + viewName);
  if (!target) { location.hash = 'leaderboard'; return; }

  document.querySelectorAll('.view').forEach(v => v.classList.toggle('active', v === target));
  document.querySelectorAll('nav button').forEach(b => b.classList.toggle('active', b.dataset.view === viewName));
  document.getElementById('chart-tooltip')?.remove();

  if (viewName === 'matches') loadMatches();
  else if (viewName === 'weapons') loadWeapons();
  else if (viewName === 'activity') loadActivityHeatmap();
  else if (viewName === 'search') {
    if (arg) {
      // anyone can send a link with any hash so treat arg as untrusted
      document.getElementById('search-input').value = arg;
      searchPlayer();
    } else {
      clearSearch();
    }
  }
}
