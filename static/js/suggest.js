// player name autocomplete for the search box and the h2h box
import { API, esc } from './util.js';

let allPlayers = [];

// every player name for suggestions
export async function loadPlayerSuggestions() {
  try {
    const r = await fetch(`${API}/players?limit=500`);
    const d = await r.json();
    allPlayers = (d.players || []).map(p => p.displayName);
  } catch {}
}

// hook an input up to a suggestion box
// onSelect runs on a picked suggestion or enter
// exclude hides names from the list, e.g. the player themselves in h2h
export function attachSuggestions(input, box, { onSelect, exclude = () => false }) {
  let selected = -1;
  const items = () => box.querySelectorAll('.suggestion-item');

  // highlight item i for arrow key navigation
  const highlight = i => {
    selected = i;
    items().forEach((el, j) => el.classList.toggle('active', j === i));
  };

  const choose = name => {
    input.value = name;
    box.hidden = true;
    onSelect(name);
  };

  // up to 8 names containing what was typed
  input.addEventListener('input', () => {
    const query = input.value.trim().toLowerCase();
    selected = -1;
    const matches = query
      ? allPlayers.filter(n => n.toLowerCase().includes(query) && !exclude(n)).slice(0, 8)
      : [];
    if (!matches.length) { box.hidden = true; return; }
    box.innerHTML = matches.map((name, i) =>
      `<div class="suggestion-item" data-index="${i}" data-name="${esc(name)}">${esc(name)}</div>`).join('');
    box.hidden = false;
  });

  input.addEventListener('keydown', e => {
    const list = items();
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      highlight(Math.min(selected + 1, list.length - 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      highlight(Math.max(selected - 1, 0));
    } else if (e.key === 'Enter') {
      // highlighted suggestion if there is one, otherwise whatever was typed
      const name = selected >= 0 && list[selected] ? list[selected].dataset.name : input.value.trim();
      if (name) choose(name);
    } else if (e.key === 'Escape') {
      box.hidden = true;
    }
  });

  // mousedown not click so it fires before the input loses focus
  box.addEventListener('mousedown', e => {
    const item = e.target.closest('.suggestion-item');
    if (item) choose(item.dataset.name);
  });
  box.addEventListener('mouseover', e => {
    const item = e.target.closest('.suggestion-item');
    if (item) highlight(+item.dataset.index);
  });
}
