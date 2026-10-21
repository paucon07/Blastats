import { fmtDuration } from './util.js';

// hp as a number from 0 to 100, 100 if missing
const clampHp = v => {
  const n = Number(v);
  return isFinite(n) ? Math.max(0, Math.min(100, n)) : 100;
};

// start a player for every replayable round under root, once each
export function initRoundPlayers(root = document) {
  root.querySelectorAll('.round-block[data-rp]').forEach(el => {
    if (el._rpInit) return;
    el._rpInit = true;
    try { new RoundPlayer(el, JSON.parse(decodeURIComponent(el.dataset.rp))); }
    catch (e) { console.warn('RoundPlayer init failed', e); }
  });
}

// plays a round back by replaying damage events onto the hp bars
class RoundPlayer {
  constructor(el, cfg) {
    this.el = el;
    this.dur = cfg.dur || 1;
    this.MAX = 100;
    this.events = (cfg.events || []).slice().sort((a, b) => a.t - b.t);
    this.p1 = cfg.p1id;
    this.p2 = cfg.p2id;
    this.end = { [this.p1]: clampHp(cfg.p1end), [this.p2]: clampHp(cfg.p2end) };
    this.speed = 1;
    this.playing = false;
    // starts parked at the end so the bars show the final hp
    this.ended = true;
    this.t = this.dur;
    this.idx = this.events.length;
    this.last = 0;
    this.hp = { [this.p1]: this.end[this.p1], [this.p2]: this.end[this.p2] };
    // chip is the white bar that trails behind hp after a hit
    this.chip = { [this.p1]: this.hp[this.p1], [this.p2]: this.hp[this.p2] };

    this.bars = {};
    el.querySelectorAll('.hp-row[data-pid]').forEach(row => {
      this.bars[+row.dataset.pid] = {
        row,
        track: row.querySelector('.hp-track'),
        fill: row.querySelector('.hp-fill'),
        chip: row.querySelector('.hp-chip'),
        val: row.querySelector('.hp-val'),
      };
    });

    this.playBtn = el.querySelector('.rp-play');
    this.clock = el.querySelector('.rp-clock');
    this.scrub = el.querySelector('.rp-scrub');
    this.scrubFill = el.querySelector('.rp-scrub-fill');

    this.playBtn?.addEventListener('click', () => this.toggle());
    el.querySelectorAll('.rp-speed').forEach(b => b.addEventListener('click', () => {
      this.speed = +b.dataset.s;
      el.querySelectorAll('.rp-speed').forEach(x => x.classList.toggle('on', x === b));
    }));
    // click on the scrub bar to jump there
    this.scrub?.addEventListener('click', e => {
      const r = this.scrub.getBoundingClientRect();
      const was = this.playing;
      this.pause();
      this.seek(Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)) * this.dur);
      if (was) this.play();
    });

    this.p1color = cfg.p1color;
    this.p2color = cfg.p2color;
    this.buildPlaybackView();

    this._loop = ts => this.frame(ts);
    this.render();
  }

  toggle() { this.playing ? this.pause() : this.play(); }

  play() {
    // restart from 0 if it already finished
    if (this.ended) this.reset();
    this.playing = true;
    this.ended = false;
    this.last = 0;
    if (this.playBtn) this.playBtn.innerHTML = '&#10073;&#10073;';
    requestAnimationFrame(this._loop);
  }

  pause() {
    this.playing = false;
    if (this.playBtn) this.playBtn.innerHTML = '&#9654;';
  }

  // back to the start with both players on full hp
  reset() {
    this.t = 0;
    this.idx = 0;
    this.ended = false;
    this.hp[this.p1] = this.hp[this.p2] = this.MAX;
    this.chip[this.p1] = this.chip[this.p2] = this.MAX;
  }

  // apply every event up to time t
  // pops shows the floating damage numbers
  applyUntil(t, pops) {
    while (this.idx < this.events.length && this.events[this.idx].t <= t) {
      const e = this.events[this.idx++];
      const r = e.receiver;
      if (this.hp[r] == null) continue;
      const old = this.hp[r];
      this.hp[r] = Math.max(0, old - e.dmg);
      this.chip[r] = Math.max(this.chip[r], old);
      if (pops) this.popup(e);
    }
  }

  // slide the chip bar down towards hp
  easeChips(dt) {
    const k = 1 - Math.exp(-7 * dt);
    for (const p of [this.p1, this.p2]) {
      if (this.chip[p] > this.hp[p]) {
        let n = this.chip[p] + (this.hp[p] - this.chip[p]) * k;
        // snap once its close enough
        if (n - this.hp[p] < 0.2) n = this.hp[p];
        this.chip[p] = n;
      } else {
        this.chip[p] = this.hp[p];
      }
    }
  }

  // floating -dmg number at the end of the hp bar
  popup(e) {
    const b = this.bars[e.receiver];
    if (!b?.track || !b.row) return;
    const tr = b.track.getBoundingClientRect();
    const rr = b.row.getBoundingClientRect();
    // bar not on screen
    if (!tr.width) return;
    const pop = document.createElement('div');
    pop.className = 'rp-pop';
    pop.style.left = ((tr.left - rr.left) + tr.width * Math.max(0, this.hp[e.receiver]) / 100) + 'px';
    pop.style.top = ((tr.top - rr.top) + tr.height / 2) + 'px';
    pop.textContent = `-${Math.round(e.dmg)}`;
    b.row.appendChild(pop);
    setTimeout(() => pop.remove(), 850);
  }

  // swap the plain scrub bar for a damage waveform
  // p1 hits above the line, p2 hits below
  buildPlaybackView() {
    const scrub = this.scrub;
    if (!scrub) return;
    const evs = this.events.filter(e =>
      e && e.dmg > 0 && (e.receiver === this.p1 || e.receiver === this.p2));
    if (!evs.length) return;

    scrub.classList.add('has-wave');

    // match the hp bar colour, fall back to the theme accents
    const colorFor = (pid, fallback) => {
      const fill = this.bars[pid]?.fill;
      if (fill) {
        const bg = getComputedStyle(fill).backgroundColor;
        if (bg && bg !== 'transparent' && !/^rgba?\(0,\s*0,\s*0,\s*0\)$/.test(bg)) return bg;
      }
      return fallback;
    };
    const col1 = colorFor(this.p1, this.p1color === 'pink' ? 'var(--accent2)' : 'var(--accent)');
    const col2 = colorFor(this.p2, this.p2color === 'blue' ? 'var(--accent)' : 'var(--accent2)');

    const W = 1000, H = 100, MID = 50, MAXBAR = 40, BURST_GAP = 0.3;
    const barW = 3.4, barGap = 0.3;

    // hits within burst gap seconds of each other share a column
    const cols = [];
    for (const up of [true, false]) {
      const side = evs.filter(e => (e.receiver === this.p1) === up).sort((a, b) => a.t - b.t);
      let cur = null;
      for (const e of side) {
        if (cur && e.t - cur.lastT <= BURST_GAP) {
          cur.hits.push(e.dmg); cur.sumT += e.t; cur.n++; cur.lastT = e.t;
        } else {
          cur = { up, hits: [e.dmg], sumT: e.t, n: 1, lastT: e.t };
          cols.push(cur);
        }
      }
    }

    // biggest hit gets the full bar height
    const maxDmg = Math.max(...evs.map(e => e.dmg)) || 1;
    const scale = MAXBAR / maxDmg;

    const rects = cols.map(c => {
      // centre the column on the average hit time, kept inside the bar
      const originX = Math.max(0, Math.min(W, ((c.sumT / c.n) / this.dur) * W));
      const groupW = c.hits.length * barW + (c.hits.length - 1) * barGap;
      const x0 = Math.max(0, Math.min(W - groupW, originX - groupW / 2));
      const fill = c.up ? col1 : col2;
      return c.hits.map((dmg, i) => {
        const x = x0 + i * (barW + barGap);
        const h = Math.max(2.5, dmg * scale);
        const y = c.up ? MID - h : MID;
        return `<rect x="${x.toFixed(2)}" y="${y.toFixed(2)}" width="${barW}" height="${h.toFixed(2)}" rx="0.6" fill="${fill}"/>`;
      }).join('');
    }).join('');

    // faded copy underneath, full colour copy clipped to the playhead
    const clipId = 'rpwave-' + Math.random().toString(36).slice(2, 9);
    const wrap = document.createElement('div');
    wrap.className = 'rp-wave-wrap';
    wrap.innerHTML =
      `<svg class="rp-wave" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">` +
        `<defs><clipPath id="${clipId}"><rect x="0" y="0" width="0" height="${H}"/></clipPath></defs>` +
        `<g opacity="0.3">${rects}</g>` +
        `<g clip-path="url(#${clipId})">${rects}</g>` +
        `<line x1="0" y1="${MID}" x2="${W}" y2="${MID}" stroke="var(--text-dim)" stroke-width="1" vector-effect="non-scaling-stroke" opacity="0.5"/>` +
        `<line class="rp-wave-head" x1="0" x2="0" y1="0" y2="${H}" stroke="var(--text-bright)" stroke-width="1.5" vector-effect="non-scaling-stroke" opacity="0.85"/>` +
      `</svg>`;

    scrub.insertBefore(wrap, scrub.firstChild);
    this.waveClip = wrap.querySelector('clipPath rect');
    this.waveHead = wrap.querySelector('.rp-wave-head');
  }

  // push current state to the bars, clock and playhead
  render() {
    for (const p of [this.p1, this.p2]) {
      const b = this.bars[p];
      if (!b) continue;
      const h = Math.max(0, this.hp[p]);
      const c = Math.max(h, this.chip[p]);
      if (b.fill) b.fill.style.width = (h / this.MAX * 100) + '%';
      if (b.chip) b.chip.style.width = (c / this.MAX * 100) + '%';
      if (b.val) b.val.textContent = Math.round(h);
    }
    // show the full round length when parked at the end
    if (this.clock) {
      this.clock.textContent = (!this.playing && this.t >= this.dur)
        ? fmtDuration(this.dur)
        : Math.floor(this.t / 60) + ':' + String(Math.floor(this.t % 60)).padStart(2, '0');
    }
    if (this.scrubFill) this.scrubFill.style.width = (this.t / this.dur * 100) + '%';

    if (this.waveClip) {
      const x = Math.max(0, Math.min(1000, (this.t / this.dur) * 1000)).toFixed(1);
      this.waveClip.setAttribute('width', x);
      this.waveHead?.setAttribute('x1', x);
      this.waveHead?.setAttribute('x2', x);
    }
  }

  // one animation frame
  frame(ts) {
    if (!this.playing) return;
    if (!this.last) this.last = ts;
    // cap dt so a background tab doesnt jump ahead
    const dt = Math.min(0.1, (ts - this.last) / 1000);
    this.last = ts;
    this.t += dt * this.speed;

    if (this.t >= this.dur) {
      // finish on the recorded end hp in case events dont add up exactly
      this.t = this.dur;
      this.applyUntil(this.t, true);
      this.hp[this.p1] = this.end[this.p1];
      this.hp[this.p2] = this.end[this.p2];
      this.chip[this.p1] = this.hp[this.p1];
      this.chip[this.p2] = this.hp[this.p2];
      this.ended = true;
      this.pause();
      this.render();
      return;
    }
    this.applyUntil(this.t, true);
    this.easeChips(dt);
    this.render();
    requestAnimationFrame(this._loop);
  }

  // jump to time t without animating
  seek(t) {
    this.reset();
    this.applyUntil(t, false);
    this.t = t;
    this.chip[this.p1] = this.hp[this.p1];
    this.chip[this.p2] = this.hp[this.p2];
    this.render();
  }
}
