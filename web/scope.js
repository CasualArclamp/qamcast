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

// Constellation with persistence. The transmitter only produces a frame every
// 250 ms or so, so redrawing from scratch each time flickers; fading the
// previous draw instead leaves a trail that makes the cloud readable and the
// update look continuous rather than steppy.
function makeConstellation(canvas) {
  let last = null, fresh = false;
  return function draw(points, isFirst) {
    fresh = !!points;
    if (points) last = points;
    const { ctx, w, h } = hidpi(canvas, canvas.clientWidth);

    // Symbols arrive a slice at a time, so the fade has to be gentle enough
    // that earlier slices are still visible when the last one lands -- that
    // is what makes the separate slices read as one cloud. A new frame fades
    // harder, to clear the previous one out of the way.
    ctx.fillStyle = isFirst ? 'rgba(5,8,13,0.40)' : 'rgba(5,8,13,0.10)';
    ctx.fillRect(0, 0, w, h);

    const cx = w / 2, cy = h / 2, s = Math.min(w, h) / 2 / 1.45;
    ctx.strokeStyle = 'rgba(28,36,48,0.7)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(cx, 0); ctx.lineTo(cx, h);
    ctx.moveTo(0, cy); ctx.lineTo(w, cy);
    ctx.stroke();

    if (!last || !last.length) {
      ctx.fillStyle = '#8b949e';
      ctx.font = '12px ui-monospace, monospace';
      ctx.textAlign = 'center';
      ctx.fillText('no signal', cx, cy - 6);
      return;
    }
    // Only paint on a slice that actually arrived; redrawing the same points
    // every animation frame would darken them against the fade and make the
    // cloud pulse.
    if (!fresh) return;
    ctx.fillStyle = 'rgba(88,166,255,0.85)';
    for (let i = 0; i < last.length; i += 2) {
      ctx.fillRect(cx + last[i] * s - 0.75, cy - last[i + 1] * s - 0.75, 1.5, 1.5);
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

function set(id, text, cls) {
  const el = document.getElementById(id);
  if (!el) return;
  if (el.textContent !== String(text)) el.textContent = text;
  if (cls !== undefined) el.className = cls;
}
