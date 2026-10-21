// entry point, sets up event handling and the first loads
import { handleRoute, showView, goToPlayer } from './router.js';
import { loadStatus, loadLeaderboard, startCountdown, actions as leaderboardActions } from './leaderboard.js';
import { actions as matchLogActions } from './matchlog.js';
import { actions as detailActions } from './matchdetail.js';
import { searchPlayer, fitProfileName, actions as profileActions } from './profile.js';
import { actions as h2hActions } from './h2h.js';
import { actions as chartActions, hideChartTooltip } from './chart.js';
import { actions as playerWeaponActions } from './playerweapons.js';
import { actions as weaponActions } from './weapons.js';
import { actions as activityActions, hideHeatmapTooltip } from './activity.js';
import { loadStreamInfo } from './stream.js';
import { loadPlayerSuggestions, attachSuggestions } from './suggest.js';

// clickable elements say what they do with data-action and any data- args
// one listener runs them so player names never go into onclick strings
const actions = {
  view: el => showView(el.dataset.view),
  player: el => goToPlayer(el.dataset.name),
  ...leaderboardActions,
  ...matchLogActions,
  ...detailActions,
  ...profileActions,
  ...h2hActions,
  ...chartActions,
  ...playerWeaponActions,
  ...weaponActions,
  ...activityActions,
};

document.addEventListener('click', e => {
  // links like watch open normally without toggling their row
  if (e.target.closest('a')) return;
  const el = e.target.closest('[data-action]');
  if (el) actions[el.dataset.action]?.(el, e);
});

// same idea for checkboxes, data-change instead of data-action
document.addEventListener('change', e => {
  const el = e.target.closest('[data-change]');
  if (el) actions[el.dataset.change]?.(el, e);
});

// close tooltips and suggestion boxes when clicking or tapping elsewhere
function dismissPopups(e) {
  const t = e.target;
  if (!t.closest('#activity-heatmap-container')) hideHeatmapTooltip();
  if (!t.closest('.chart-dot')) hideChartTooltip();
  document.querySelectorAll('.suggestions:not([hidden])').forEach(box => {
    if (!box.parentElement.contains(t)) box.hidden = true;
  });
}
document.addEventListener('click', dismissPopups);
document.addEventListener('touchstart', dismissPopups, { passive: true });

// hide icons that fail to load and show their text fallback if there is one
// error doesnt bubble so this has to listen in the capture phase
document.addEventListener('error', e => {
  const img = e.target;
  if (img.tagName !== 'IMG') return;
  img.hidden = true;
  const fallback = img.nextElementSibling;
  if (fallback?.classList.contains('icon-fallback')) fallback.hidden = false;
}, true);

// refit the profile name after resizing stops
let fitTimer;
window.addEventListener('resize', () => {
  clearTimeout(fitTimer);
  fitTimer = setTimeout(fitProfileName, 150);
});

window.addEventListener('hashchange', handleRoute);

attachSuggestions(
  document.getElementById('search-input'),
  document.getElementById('search-suggestions'),
  { onSelect: () => searchPlayer() },
);

startCountdown();
loadStatus();
loadStreamInfo();
loadPlayerSuggestions();
loadLeaderboard('current');
handleRoute();
