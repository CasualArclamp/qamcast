// Scopes and telemetry plumbing, shared by the transmit and receive pages.

// Telemetry carries the constellation and spectrum as base64 int8 rather than
// JSON numbers. 900 points is 1800 values: about 11 kB as JSON floats, 2.4 kB
// packed. At 20 updates a second that difference is what keeps the event
// stream comfortable.
function unpack(b64, scale) {
  if (!b64) return null;
  const bin = atob(b64);
  const out = new Float32Array(bin.length);
  for (let i = 0; i < bin.length; i++) {
    let v = bin.charCodeAt(i);
    if (v > 127) v -= 256;          // char codes are unsigned; int8 is not
    out[i] = v / scale;
  }
  return out;
}

function hidpi(canvas, cssHeight) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth;
  canvas.style.height = cssHeight + 'px';
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(cssHeight * dpr)) {
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(cssHeight * dpr);
  }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w, h: cssHeight };
}

// Symbols arrive a slice at a time, four to a frame, so a single slice is
// not a whole picture. This keeps the last few and redraws all of them fully
// opaque on every new one, clearing first.
//
// It used to fade the previous draw instead. That gives continuity, but the
// trail is drawn at every brightness between full and invisible, and against
// a dense constellation the half-faded symbols read as scatter around the
// real ones -- which is exactly what you are looking at the plot to judge.
// Old points now disappear when they age out of the window rather than
// dimming through it, so every dot on screen is a symbol that was really
// received, at one brightness.
const CONST_SLICES = 4;      // matches scope.SYMBOL_SLICES: one frame's worth

function makeConstellation(canvas) {
  let recent = [], drawn = false;
  return function draw(points, _isFirst) {
    // Nothing new: leave the canvas alone. Redrawing identical content every
    // animation frame would cost work for no change.
    if (!points && drawn) return;
    if (points) {
      recent.push(points);
      while (recent.length > CONST_SLICES) recent.shift();
    }
    const { ctx, w, h } = hidpi(canvas, canvas.clientWidth);
    drawn = true;

    ctx.fillStyle = '#05080d';
    ctx.fillRect(0, 0, w, h);

    const cx = w / 2, cy = h / 2, s = Math.min(w, h) / 2 / 1.45;
    ctx.strokeStyle = 'rgba(28,36,48,0.7)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(cx, 0); ctx.lineTo(cx, h);
    ctx.moveTo(0, cy); ctx.lineTo(w, cy);
    ctx.stroke();

    if (!recent.length) {
      ctx.fillStyle = '#8b949e';
      ctx.font = '12px ui-monospace, monospace';
      ctx.textAlign = 'center';
      ctx.fillText('no signal', cx, cy - 6);
      return;
    }
    ctx.fillStyle = '#58a6ff';
    for (const arr of recent) {
      for (let i = 0; i < arr.length; i += 2) {
        ctx.fillRect(cx + arr[i] * s - 0.75, cy - arr[i + 1] * s - 0.75, 1.5, 1.5);
      }
    }
  };
}

// Spectrum plus scrolling waterfall. Magnitudes arrive as int8 dB, 0 at the top.
function makeSpectrum(canvas, waterfall) {
  const wf = waterfall.getContext('2d', { alpha: false });
  let sized = false, lastMags = null;
  return function draw(mags, band) {
    if (mags) lastMags = mags;
    const m = lastMags;
    const { ctx, w, h } = hidpi(canvas, 110);
    ctx.clearRect(0, 0, w, h);
    if (!m || !m.length) return;

    const floor = -90, ceil = 0;
    const norm = v => Math.max(0, Math.min(1, (v - floor) / (ceil - floor)));

    if (band) {
      ctx.fillStyle = 'rgba(88,166,255,0.07)';
      ctx.fillRect(band[0] * w, 0, (band[1] - band[0]) * w, h);
    }
    ctx.strokeStyle = '#58a6ff';
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (let i = 0; i < m.length; i++) {
      const x = i / (m.length - 1) * w;
      const y = h - norm(m[i]) * h;
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    }
    ctx.stroke();

    // Only scroll the waterfall on genuinely new data, or a slow link would
    // smear identical lines down it and look busier than it is.
    if (!mags) return;
    const dpr = window.devicePixelRatio || 1;
    if (!sized) {
      waterfall.style.height = '110px';
      waterfall.width = Math.round(waterfall.clientWidth * dpr);
      waterfall.height = Math.round(110 * dpr);
      wf.fillStyle = '#05080d';
      wf.fillRect(0, 0, waterfall.width, waterfall.height);
      sized = true;
    }
    const ww = waterfall.width;
    wf.drawImage(waterfall, 0, 1);
    const img = wf.createImageData(ww, 1);
    for (let x = 0; x < ww; x++) {
      const v = norm(m[Math.floor(x / ww * m.length)]);
      const r = v < 0.5 ? 0 : Math.min(255, (v - 0.5) * 660);
      const g = v < 0.25 ? v * 400 : Math.min(255, 100 + v * 200);
      const b = v < 0.5 ? Math.min(255, 60 + v * 390) : Math.max(0, 255 - (v - 0.5) * 400);
      const o = x * 4;
      img.data[o] = r; img.data[o + 1] = g; img.data[o + 2] = b; img.data[o + 3] = 255;
    }
    wf.putImageData(img, 0, 0);
  };
}

// Telemetry arrives faster than a browser needs to repaint, so state is
// stashed and drawn once per animation frame. Painting straight from the
// event handler would queue redundant work behind the display's refresh.
function connect(onState) {
  let pending = null, queued = false;
  const es = new EventSource('/events');
  es.onmessage = e => {
    try { pending = JSON.parse(e.data); } catch (_) { return; }
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => {
      queued = false;
      const s = pending;
      if (!s) return;
      s.constellation = unpack(s.const, s.const_scale || 64);
      s.spectrum = unpack(s.spec, 1);
      onState(s);
    });
  };
  es.onerror = () => { const d = document.getElementById('conn'); if (d) d.textContent = 'reconnecting'; };
  return es;
}

async function control(cmd) {
  const r = await fetch('/control', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(cmd),
  });
  return r.json();
}

// Colour a stat by how healthy it is. The thresholds live at the call sites,
// because what counts as good is a property of the reading, not of the widget
// -- and several of them are only meaningful relative to something else, like
// Es/N0 against the MODCOD's own requirement.
function grade(id, level) {
  const el = document.getElementById(id);
  if (!el) return;
  el.className = level || '';
  if (el.parentElement && el.parentElement.parentElement
      && el.parentElement.parentElement.classList.contains('stats')) {
    el.parentElement.className = level || '';
  }
}

// v at or above `good` is good, at or above `ok` is fair, below that is bad.
function high(v, good, ok) {
  if (v == null || !isFinite(v)) return '';
  return v >= good ? 'good' : v >= ok ? 'warn' : 'bad';
}
// The same for readings where smaller is better; `v` may be signed.
function low(v, good, ok) {
  if (v == null || !isFinite(v)) return '';
  const a = Math.abs(v);
  return a <= good ? 'good' : a <= ok ? 'warn' : 'bad';
}

// One toggle for every explanatory note on the page, remembered across
// reloads. The prose is worth reading once and in the way thereafter.
function initExplain() {
  const el = document.getElementById('explain');
  const paint = () => {
    if (el) el.textContent = document.body.classList.contains('explain')
        ? 'hide notes' : 'notes';
  };
  let on = false;
  try { on = localStorage.getItem('qamcast.explain') === '1'; } catch (_) {}
  document.body.classList.toggle('explain', on);
  paint();
  if (!el) return;
  el.onclick = e => {
    e.preventDefault();
    const now = !document.body.classList.contains('explain');
    document.body.classList.toggle('explain', now);
    try { localStorage.setItem('qamcast.explain', now ? '1' : '0'); } catch (_) {}
    paint();
  };
}

function set(id, text, cls) {
  const el = document.getElementById(id);
  if (!el) return;
  if (el.textContent !== String(text)) el.textContent = text;
  if (cls !== undefined) el.className = cls;
  // Stat cells clip with an ellipsis rather than reflowing the grid, so keep
  // the full text reachable on hover. Only for stats: everything else either
  // has room or wraps.
  if (el.tagName === 'B' && el.parentElement
      && el.parentElement.parentElement
      && el.parentElement.parentElement.classList.contains('stats')) {
    el.title = String(text);
  }
}
