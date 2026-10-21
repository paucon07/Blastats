// per player weapon cards, used on the profile and in h2h
import { esc, iconUrl, weaponDisplay, plural } from './util.js';

// weapons section on the profile
export function renderPlayerWeapons(p) {
  const el = document.getElementById('player-weapons');
  if (!el) return;
  const ws = p.weaponStats;
  if (!ws?.sampleMatches) { el.innerHTML = ''; return; }
  // some spectated matches have loadouts but no damage
  const dmgNote = ws.damageMatches < ws.sampleMatches ? `, damage from ${ws.damageMatches}` : '';
  el.innerHTML = `
    <div class="section-header compact spaced">
      <div class="section-title">Weapons</div>
      <div class="section-sub">${ws.sampleMatches} spectated ${plural(ws.sampleMatches, 'match', 'matches')}${dmgNote}</div>
    </div>
    ${weaponCardsHtml(ws, 'wp')}`;
}

// use rate, damage dealt and damage received cards
// idPrefix keeps ids unique when the profile and h2h both show cards
export function weaponCardsHtml(ws, idPrefix) {
  const total = ws.sampleMatches || 0;
  return `<div class="wp-grid">
    ${weaponListCard('Use Rate', ws.mostUsed || [], 'used', total, idPrefix)}
    ${weaponListCard('Average Damage Dealt', ws.mostDamageDealt || [], 'dealt', total, idPrefix)}
    ${weaponListCard('Average Damage Received', ws.mostDamageReceived || [], 'received', total, idPrefix)}
  </div>`;
}

// one card, shows 3 rows with a button for the rest
function weaponListCard(title, items, kind, total, idPrefix) {
  if (!items.length) {
    return `<div class="wp-card">
      <div class="wp-card-title">${title}</div>
      <div class="wp-empty">${kind === 'used' ? 'No loadout data yet' : 'No damage data yet'}</div>
    </div>`;
  }
  const id = `${idPrefix}-${kind}`;
  const more = items.length > 3
    ? `<button class="wp-more" data-action="weapon-list-more" data-target="${id}">Show all ${items.length}</button>`
    : '';
  return `<div class="wp-card">
    <div class="wp-card-title">${title}</div>
    <div class="wp-list" id="${id}">${items.map(it => weaponRow(it, kind, total)).join('')}</div>
    ${more}
  </div>`;
}

const metric = (val, label, cls = '') =>
  `<span class="wp-metric"><span class="wp-metric-val ${cls}">${esc(val)}</span><span class="wp-metric-label">${label}</span></span>`;

// one weapon row
// used shows usage and win rate, the damage cards show per round
function weaponRow(it, kind, total) {
  const disp = weaponDisplay(it.weapon);
  let metrics;
  if (kind === 'used') {
    const usage = total ? (it.count / total * 100).toFixed(1) : '0.0';
    const wrCls = it.winRate == null ? '' : (it.winRate >= 50 ? 'wpn-good' : 'wpn-bad');
    metrics = metric(usage + '%', 'Used') +
              metric(it.winRate == null ? '-' : it.winRate.toFixed(1) + '%', 'Win', wrCls);
  } else {
    metrics = metric(it.perRound == null ? '-' : it.perRound, 'Per Round');
  }
  return `<div class="wp-row">
    <span class="wp-ico"><img src="${iconUrl(disp)}" alt=""></span>
    <span class="wp-name">${esc(disp)}</span>
    <span class="wp-metrics">${metrics}</span>
  </div>`;
}

export const actions = {
  // show all or back to 3
  'weapon-list-more': btn => {
    const list = document.getElementById(btn.dataset.target);
    if (!list) return;
    const expanded = list.classList.toggle('expanded');
    btn.textContent = expanded ? 'Show less' : `Show all ${list.children.length}`;
  },
};
