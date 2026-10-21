// same server that served the page, works live and locally
export const API = '';

const ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

// make a value safe for html text or a quoted attribute
// use on anything from players, the api or the url before it hits innerHTML
// not safe inside inline js like onclick, use data attributes for that
export function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, c => ESCAPES[c]);
}

export const iconUrl = name => `${API}/icons/${encodeURIComponent(name)}.png`;
export const avatarUrl = name => `${API}/avatars/${encodeURIComponent(name)}.png`;

// iso timestamp to local time with timezone
export function formatTime(iso, includeDate = true) {
  if (!iso) return '?';
  const opts = { timeZoneName: 'short', hour: '2-digit', minute: '2-digit' };
  if (includeDate) Object.assign(opts, { month: 'short', day: 'numeric', year: 'numeric' });
  return new Date(iso).toLocaleString(undefined, opts);
}

// seconds to m:ss
export function fmtDuration(sec) {
  if (sec == null || isNaN(sec)) return '';
  const s = Math.round(sec);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

export const plural = (n, one, many) => (n === 1 ? one : many);

// mark btn active and clear the rest of its toggle group
export function activate(btn) {
  for (const b of btn.parentElement.querySelectorAll('button')) {
    b.classList.toggle('active', b === btn);
  }
}

// put a fixed tooltip next to the pointer
// flips sides if it would go off screen
export function placeTooltip(tt, event, pad, yOffset = pad) {
  const src = event.touches ? event.touches[0] : event;
  const flipX = src.clientX + pad + tt.offsetWidth > window.innerWidth;
  const flipY = src.clientY + pad + tt.offsetHeight > window.innerHeight;
  tt.style.left = (flipX ? src.clientX - tt.offsetWidth - pad : src.clientX + pad) + 'px';
  tt.style.top = (flipY ? src.clientY - tt.offsetHeight - pad : src.clientY + yOffset) + 'px';
}

// internal weapon name to in game name
export const WEAPON_NAMES = {
  Smg: 'Wildcat', Sniper: 'Viper', Shotgun: 'Hammer', Laser: 'Lance',
  Shield: 'Cortex Shield', Grenade: 'Hammer Nade', BigOrbus: 'Nova',
  Barrier: 'Bunker Barrier', FastLaser: 'Lance Rapide', BulletLauncherOrbus: 'Nova Hurricane',
  CurvedShotgun: 'Hammer Ellipse', CurvedSniper: 'Viper Ellipse', LaserGrenade: 'Lance Nade',
  AoeGrenade: 'Avalanche Nade', AoeBigOrb: 'Majestic', AoeGrenadeTwo: 'Majestic Nade',
  Handgun: 'Raptor', SpiritBomb: 'Super Majestic', FastShield: 'Aegis Shield',
  CurvedHandgun: 'Raptor Ellipse', OrbusGrenade: 'Nova Nade', Vandal: 'Vandal',
  ThrowableBall: 'FrostByte', LobberLauncher: 'Hurricane', TetherProjectileSpeed: 'Quicksilver',
  NovaHelix: 'Nova Helix', Vector: 'Vector', Swarm: 'Swarm', Bow: 'Luma',
};

export const weaponDisplay = internal => WEAPON_NAMES[internal] || internal;
