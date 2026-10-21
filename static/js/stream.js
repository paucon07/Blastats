import { API, esc } from './util.js';

let streamInfo = null;

// current youtube stream id and start time
export async function loadStreamInfo() {
  try {
    const r = await fetch(`${API}/stream`);
    streamInfo = await r.json();
  } catch {
    streamInfo = null;
  }
}

// link to the point in the stream where a match happened or null
function getYouTubeLink(timestamp) {
  if (!streamInfo?.videoId || !streamInfo?.streamStartTime) return null;
  const matchTime = new Date(timestamp);
  const streamStart = new Date(streamInfo.streamStartTime);

  // only link matches from the last 12 hours
  if (Date.now() - matchTime > 12 * 60 * 60 * 1000) return null;

  const offset = Math.floor((matchTime - streamStart) / 1000);
  // match was before this stream started
  if (offset < 0) return null;
  return `https://youtube.com/live/${streamInfo.videoId}?t=${offset}`;
}

// watch link for a spectated match or empty if theres nothing to link
export function watchLinkHtml(m, block = false) {
  const link = m.spectated ? getYouTubeLink(m.timestamp) : null;
  if (!link) return '';
  return `<a class="watch-link${block ? ' block' : ''}" href="${esc(link)}" target="_blank" rel="noopener">▶ WATCH</a>`;
}
