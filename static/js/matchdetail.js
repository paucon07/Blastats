// expanded match view used by the match log, profile and h2h
import { esc, fmtDuration, iconUrl, avatarUrl, weaponDisplay } from './util.js';
import { initRoundPlayers } from './replay.js';

// damage sources that arent weapons
const LEGACY_SOURCE_LABELS = { throw: 'THROW', cage: 'BOUNDS' };
const isNonWeaponSource = key => !!LEGACY_SOURCE_LABELS[(key || '').toLowerCase()];
const sourceLabel = key => LEGACY_SOURCE_LABELS[(key || '').toLowerCase()] || weaponDisplay(key);

// frostbyte hits get logged as throw with one of these amounts
const FROSTBYTE_THROW_DMG = new Set([5, 15, 20]);

// frostbyte damage a player took in a round
// null if it cant be worked out
function frostbyteFromEvents(round, receiverNetworkId, oppHadFrostbyte) {
  if (!oppHadFrostbyte) return null;
  const events = round?.events;
  if (!Array.isArray(events) || !events.length) return null;
  let frost = 0;
  for (const e of events) {
    if (!e || e.receiver !== receiverNetworkId) continue;
    if ((e.weapon || '').toLowerCase() !== 'throw') continue;
    if (FROSTBYTE_THROW_DMG.has(Math.round(e.dmg))) frost += e.dmg;
  }
  return frost;
}

// true if frostbyte is in the players deck or loadout
function playerHasFrostbyte(player) {
  if (!player) return false;
  const has = arr => Array.isArray(arr) && arr.some(
    w => w && typeof w.name === 'string' && w.name.toLowerCase() === 'frostbyte');
  return has(player.deck) || has(player.weapons);
}

// one damage source chip
// broken icons get hidden by the image error handler in main.js
// an icon-fallback span right after the img shows instead
function dmgChipHtml(internal, value, hits, oppHadFrostbyte, throwSeparated) {
  const val = Math.round(value);
  const disp = esc(sourceLabel(internal));
  const isThrow = (internal || '').toLowerCase() === 'throw';

  // throw and frostbyte cant be told apart without events
  if (isThrow && oppHadFrostbyte && !throwSeparated) {
    return `<span class="dmg-chip dmg-throw-fb" title="Throw + some of this may be frostbyte, can't differentiate sadly">` +
      `<span class="dmg-name">${disp} /</span><img src="${iconUrl(weaponDisplay('ThrowableBall'))}" alt="">` +
      `<span class="dmg-val">${val}</span></span>`;
  }

  const title = hits ? `${disp}, ${hits} hit${hits === 1 ? '' : 's'}` : disp;
  if (isNonWeaponSource(internal)) {
    return `<span class="dmg-chip noicon" title="${title}"><span class="dmg-name">${disp}</span><span class="dmg-val">${val}</span></span>`;
  }
  return `<span class="dmg-chip" title="${title}">` +
    `<img src="${iconUrl(sourceLabel(internal))}" alt="">` +
    `<span class="dmg-name icon-fallback" hidden>${disp}</span>` +
    `<span class="dmg-val">${val}</span></span>`;
}

// chips for all the damage a player took in a round, biggest first
function damageTakenHtml(round, playerNetworkId, opponent) {
  const oppHadFrostbyte = playerHasFrostbyte(opponent);
  const bucket = round.stats?.[String(playerNetworkId)]?.Damage;
  if (!bucket) return '';

  let items = Object.entries(bucket).filter(([, v]) => v > 0);

  // split frostbyte out of throw when the events allow it
  const frost = frostbyteFromEvents(round, playerNetworkId, oppHadFrostbyte);
  const separated = frost !== null;
  if (separated) {
    const throwTotal = items
      .filter(([k]) => k.toLowerCase() === 'throw')
      .reduce((s, [, v]) => s + v, 0);
    const throwRemainder = Math.max(0, throwTotal - frost);
    items = items.filter(([k]) => k.toLowerCase() !== 'throw');
    if (throwRemainder > 0) items.push(['throw', throwRemainder]);
    if (frost > 0) items.push(['FrostByte', frost]);
  }

  items.sort((a, b) => b[1] - a[1]);
  return items.map(([k, v]) => dmgChipHtml(k, v, null, oppHadFrostbyte, separated)).join('');
}

// weapons a player grabbed in a round
function grabsHtml(round, playerNetworkId) {
  const bucket = round.stats?.[String(playerNetworkId)]?.Grab;
  if (!bucket) return '';
  const items = Object.entries(bucket).filter(([, v]) => v > 0).sort((a, b) => b[1] - a[1]);
  if (!items.length) return '';
  const chips = items.map(([k, count]) => {
    const disp = weaponDisplay(k);
    return `<span class="grab-chip" title="${esc(disp)}">` +
      `<img src="${iconUrl(disp)}" alt="">` +
      `<span class="grab-name icon-fallback" hidden>${esc(disp)}</span>` +
      `<span class="grab-x">×${esc(count)}</span></span>`;
  }).join('');
  return `<div class="grab-line"><span class="grab-label">Grabbed</span>${chips}</div>`;
}

// weapon row with icon name and attachment
// size lg is the bigger one on profile loadout cards
export function weaponIconHtml(w, size = '') {
  return `
    <div class="mw-weapon${size ? ' ' + size : ''}">
      <div class="mw-icon"><img src="${iconUrl(w.name)}" alt=""></div>
      <span class="mw-name">${esc((w.name || '').toUpperCase())}</span>
      ${w.attachment ? `<div class="mw-att"><img src="${iconUrl(w.attachment)}" alt=""></div>` : ''}
    </div>`;
}

// blue or pink from the character name
function playerColor(player) {
  const c = (player?.character || '').toLowerCase();
  if (c.endsWith('pink')) return 'pink';
  if (c.endsWith('blue')) return 'blue';
  return '';
}

// one player column with avatar, name, lp, ping and loadout
function playerColHtml(player, isWinner) {
  if (!player) return '';
  const weapons = (player.weapons || []).map(w => weaponIconHtml(w)).join('');
  const ping = player.ping != null ? `<span class="md-ping">${esc(player.ping)} ms</span>` : '';
  const name = esc(player.displayName || '?');
  const avatar = player.character ? `<img class="md-avatar" src="${avatarUrl(player.character)}" alt="">` : '';
  return `
    <div class="md-player">
      <div class="md-player-head">
        ${avatar}
        <div class="md-player-id">
          <div class="md-player-name${isWinner ? ' win' : ''}" data-action="player" data-name="${name}" title="View ${name}">${name}${isWinner ? ' <span class="md-crown">▲</span>' : ''}</div>
          <div class="md-player-sub">${esc(player.leaguePoints || 0)} LP ${ping}</div>
        </div>
      </div>
      <div class="md-loadout">${weapons || '<span class="md-empty">no loadout recorded</span>'}</div>
    </div>`;
}

// end of round hp bar
// pid lets the replay find it
function hpBarHtml(name, hp, color, isWinner, pid) {
  const known = hp != null && !isNaN(hp);
  const pct = known ? Math.max(0, Math.min(100, hp)) : 0;
  return `
    <div class="hp-row"${pid != null ? ` data-pid="${esc(pid)}"` : ''}>
      <span class="hp-name${isWinner ? ' win' : ''}">${esc(name || '?')}</span>
      <div class="hp-track">
        <div class="hp-chip"></div>
        <div class="hp-fill ${color}" style="width:${pct}%"></div>
      </div>
      <span class="hp-val">${known ? esc(hp) : '-'}</span>
    </div>`;
}

// one round block with hp, replay controls and damage
function roundHtml(r, p1, p2, c1, c2) {
  const p1win = r.winner === p1?.displayName;
  const dur = fmtDuration(r.durationSeconds);
  const hasHp = r.p1Health != null || r.p2Health != null;
  const events = r.events || [];
  const canReplay = hasHp && events.length > 0;

  const p1dmg = damageTakenHtml(r, p1?.networkId, p2);
  const p2dmg = damageTakenHtml(r, p2?.networkId, p1);
  const dmgWeapons = dmg => dmg
    ? `<div class="dmg-weapons"><span class="dmg-label">Damage taken</span>${dmg}</div>`
    : `<div class="dmg-weapons"><span class="dmg-empty">Flawless round</span></div>`;
  const dmgSide = (p, color, dmg) => `
    <div class="dmg-side">
      <span class="dmg-player ${color}">${esc(p?.displayName || '?')}</span>
      ${dmgWeapons(dmg)}
      ${grabsHtml(r, p?.networkId)}
    </div>`;
  const dmgSection = (p1dmg || p2dmg)
    ? `<div class="round-dmg">${dmgSide(p1, c1, p1dmg)}${dmgSide(p2, c2, p2dmg)}</div>`
    : '';

  // replay data for RoundPlayer
  // encodeURIComponent turns quotes into %22 so it cant break out of the attribute
  const cfg = canReplay ? encodeURIComponent(JSON.stringify({
    events,
    dur: r.durationSeconds || events.reduce((max, e) => Math.max(max, e.t), 0) || 1,
    p1id: p1?.networkId, p2id: p2?.networkId,
    p1end: r.p1Health, p2end: r.p2Health,
    p1color: c1, p2color: c2,
  })) : null;

  const controls = canReplay ? `
    <div class="rp-controls">
      <button class="rp-play" aria-label="Play">&#9654;</button>
      <div class="rp-speeds">${[1, 2, 5, 10].map(s => `<button class="rp-speed${s === 1 ? ' on' : ''}" data-s="${s}">${s}x</button>`).join('')}</div>
      <div class="rp-clock">0:00</div>
    </div>` : '';
  const scrub = canReplay ? '<div class="rp-scrub"><div class="rp-scrub-fill"></div></div>' : '';

  // older rounds have no hp so just say who won
  const health = hasHp
    ? hpBarHtml(p1?.displayName, r.p1Health, c1, p1win, p1?.networkId) +
      hpBarHtml(p2?.displayName, r.p2Health, c2, !p1win, p2?.networkId)
    : `<div class="round-winline">${esc(r.winner || '')} won</div>`;

  return `
    <div class="round-block"${cfg ? ` data-rp="${cfg}"` : ''}>
      <div class="round-head">
        <span class="round-num">Round ${esc(r.round)}</span>
        ${dur && !canReplay ? `<span class="round-dur">⏱ ${dur}</span>` : ''}
        ${r.endedByForfeit ? '<span class="round-tag">forfeit</span>' : ''}
      </div>
      ${controls}
      ${health}
      ${scrub}
      ${dmgSection}
    </div>`;
}

// full detail panel for a spectated match
export function matchDetailHtml(m) {
  const p1 = m.players?.[0];
  const p2 = m.players?.[1];
  const c1 = playerColor(p1) || 'blue';
  const c2 = playerColor(p2) || 'pink';

  const meta = m.region ? `<div class="md-meta"><span class="md-badge">${esc(m.region)}</span></div>` : '';

  const players = (p1 || p2) ? `
    <div class="md-players">
      ${playerColHtml(p1, p1?.displayName === m.winner)}
      ${playerColHtml(p2, p2?.displayName === m.winner)}
    </div>` : '';

  // full rounds if we have them, otherwise just the winners list
  let rounds = '';
  if (m.rounds?.length) {
    rounds = `<div class="md-rounds">${m.rounds.map(r => roundHtml(r, p1, p2, c1, c2)).join('')}</div>`;
  } else if (m.roundWinners?.length) {
    rounds = `<div class="md-rounds-legacy">Round winners: ${m.roundWinners.map(esc).join(', ')}</div>`;
  }

  return `<div class="match-detail">${meta}${players}${rounds}</div>`;
}

// open or close the detail row a match row points at with data-target
function toggleDetail(row) {
  const detail = document.getElementById(row.dataset.target);
  if (!detail) return;
  const opening = detail.hidden;
  detail.hidden = !opening;
  row.classList.toggle('expanded', opening);
  // replays only get built the first time a match is opened
  if (opening) initRoundPlayers(detail);
}

export const actions = {
  'toggle-detail': toggleDetail,
};
