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

// A perceptually ordered colour ramp for the waterfall, built once.
//
// The ramp this replaces was hand-rolled and saturated: everything above about
// 70% of the range came out the same yellow. A QAM spectrum is flat across its
// passband, so the entire occupied band was one solid block of colour with no
// structure in it at all -- the waterfall was showing the band edges and
// nothing else. These anchors rise steadily in lightness, so equal steps in dB
// stay distinguishable all the way to the top.
const HEAT = (() => {
  const stops = [
    [0.00,   4,   4,  14], [0.15,  32,  16,  76], [0.30,  95,  20, 110],
    [0.45, 156,  40,  92], [0.60, 208,  73,  56], [0.75, 240, 129,  25],
    [0.88, 249, 191,  60], [1.00, 252, 250, 210],
  ];
  const lut = new Uint8Array(256 * 3);
  for (let i = 0; i < 256; i++) {
    const t = i / 255;
    let k = 0;
    while (k < stops.length - 2 && t > stops[k + 1][0]) k++;
    const a = stops[k], b = stops[k + 1];
    const f = (t - a[0]) / (b[0] - a[0] || 1);
    lut[i * 3]     = a[1] + (b[1] - a[1]) * f;
    lut[i * 3 + 1] = a[2] + (b[2] - a[2]) * f;
    lut[i * 3 + 2] = a[3] + (b[3] - a[3]) * f;
  }
  return lut;
})();

// Magnitudes arrive normalised so that 0 dB is the peak of the moment. Showing
// 90 dB below that spends most of the scale on an empty noise floor and
// squashes the part being looked at into the top tenth; 70 keeps the roll-off
// and the stopband while leaving the passband room to show its shape.
const SPEC_FLOOR = -70;
const SPEC_H = 150;             // taller than it was, to leave room for a scale

// Symbols arrive a slice at a time, four to a frame, so a single slice is
// not a whole picture. This keeps the last few and redraws all of them on
// every new one, clearing first.
//
// It used to fade the previous draw instead. That gives continuity, but the
// trail is drawn at every brightness between full and invisible, and against
// a dense constellation the half-faded symbols read as scatter around the
// real ones -- which is exactly what you are looking at the plot to judge.
// Old points disappear when they age out of the window rather than dimming
// through it, so every dot on screen is a symbol that was really received.
//
// Brightness does vary, but with *density* rather than age: the dots are drawn
// partly transparent, so overlapping ones accumulate. Every dot is still a
// real symbol, and where they pile up is now visible instead of being
// flattened into one opaque blob.
const CONST_SLICES = 4;      // matches scope.SYMBOL_SLICES: one frame's worth

function makeConstellation(canvas) {
  let recent = [], ideal = null, lastEvm, drawn = false;
  return function draw(points, _isFirst, opts) {
    opts = opts || {};
    if (opts.ideal && opts.ideal.length) ideal = opts.ideal;
    // Nothing new: leave the canvas alone. Redrawing identical content every
    // animation frame would cost work for no change. The rings move with the
    // MODCOD, so a change of requirement counts as new.
    if (!points && drawn && opts.evmDb === lastEvm) return;
    lastEvm = opts.evmDb;
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

    // Where the symbols should have landed, and how far they may stray before
    // this MODCOD stops working. With both drawn, the question the plot
    // answers becomes "is the cloud inside the circles", which needs no
    // arithmetic -- rather than "is 42.6 more than 13.4", which does.
    if (ideal && ideal.length) {
      const r = opts.evmDb != null && opts.evmDb > 0
          ? Math.pow(10, -opts.evmDb / 20) * s : 0;
      ctx.strokeStyle = 'rgba(139,148,158,0.32)';
      for (let i = 0; i < ideal.length; i += 2) {
        const x = cx + ideal[i] * s, y = cy - ideal[i + 1] * s;
        ctx.beginPath();
        ctx.moveTo(x - 3, y); ctx.lineTo(x + 3, y);
        ctx.moveTo(x, y - 3); ctx.lineTo(x, y + 3);
        ctx.stroke();
        if (r > 1.5) {
          ctx.beginPath();
          ctx.arc(x, y, r, 0, 6.2832);
          ctx.stroke();
        }
      }
    }

    if (!recent.length) {
      ctx.fillStyle = '#8b949e';
      ctx.font = '12px ui-monospace, monospace';
      ctx.textAlign = 'center';
      ctx.fillText('no signal', cx, cy - 6);
      return;
    }
    // Partly transparent, so density shows. A lone symbol is still plainly
    // visible; three or more in one place reach full brightness.
    ctx.fillStyle = 'rgba(88,166,255,0.45)';
    const d = Math.max(1.5, Math.min(2.5, w / 160));
    for (const arr of recent) {
      for (let i = 0; i < arr.length; i += 2) {
        ctx.fillRect(cx + arr[i] * s - d / 2, cy - arr[i + 1] * s - d / 2, d, d);
      }
    }
  };
}

// Spectrum plus scrolling waterfall. Magnitudes arrive as int8 dB, already
// normalised so that 0 is the peak of the moment.
function makeSpectrum(canvas, waterfall) {
  const wf = waterfall.getContext('2d', { alpha: false });
  let sized = false, lastMags = null, peak = null;
  const norm = v => Math.max(0, Math.min(1, (v - SPEC_FLOOR) / (0 - SPEC_FLOOR)));

  return function draw(mags, band, opts) {
    opts = opts || {};
    if (mags) lastMags = mags;
    const m = lastMags;
    const { ctx, w, h } = hidpi(canvas, SPEC_H);
    ctx.clearRect(0, 0, w, h);
    if (!m || !m.length) return;

    if (band) {
      ctx.fillStyle = 'rgba(88,166,255,0.07)';
      ctx.fillRect(band[0] * w, 0, (band[1] - band[0]) * w, h);
    }

    // A trace with no scale on it is a shape, not a measurement.
    ctx.font = '9px ui-monospace, monospace';
    ctx.lineWidth = 1;
    ctx.textAlign = 'left';
    for (let db = -20; db > SPEC_FLOOR; db -= 20) {
      const y = Math.round(h - norm(db) * h) + 0.5;
      ctx.strokeStyle = 'rgba(48,54,61,0.8)';
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
      ctx.fillStyle = '#6e7681';
      ctx.fillText(db + ' dB', 3, y - 2);
    }
    if (opts.nyquist) {
      const khz = opts.nyquist / 1000;
      const step = khz > 40 ? 10 : khz > 15 ? 5 : 2;
      ctx.textAlign = 'center';
      for (let f = step; f < khz - step / 4; f += step) {
        const x = Math.round(f / khz * w) + 0.5;
        ctx.strokeStyle = 'rgba(48,54,61,0.5)';
        ctx.beginPath(); ctx.moveTo(x, h - 5); ctx.lineTo(x, h); ctx.stroke();
        ctx.fillStyle = '#6e7681';
        ctx.fillText(f + 'k', x, h - 7);
      }
    }
    // The band edges the profile was built around. On an FM path the upper one
    // is what keeps the signal below the 19 kHz stereo pilot, which is the
    // whole reason FM44 stops where it does -- worth being able to see.
    if (band) {
      ctx.strokeStyle = 'rgba(88,166,255,0.45)';
      for (const edge of band) {
        const x = Math.round(edge * w) + 0.5;
        ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke();
      }
    }

    // Peak hold, decaying slowly. The cheapest way to catch something that
    // only appears now and then -- interference, or a neighbour keying up.
    if (mags) {
      if (!peak || peak.length !== m.length) peak = Float32Array.from(m);
      else for (let i = 0; i < m.length; i++) peak[i] = Math.max(m[i], peak[i] - 0.35);
    }
    if (peak) {
      ctx.strokeStyle = 'rgba(139,148,158,0.45)';
      ctx.beginPath();
      for (let i = 0; i < peak.length; i++) {
        const x = i / (peak.length - 1) * w, y = h - norm(peak[i]) * h;
        i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
      }
      ctx.stroke();
    }

    ctx.strokeStyle = '#58a6ff';
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
      waterfall.style.height = SPEC_H + 'px';
      waterfall.width = Math.round(waterfall.clientWidth * dpr);
      waterfall.height = Math.round(SPEC_H * dpr);
      wf.fillStyle = '#05080d';
      wf.fillRect(0, 0, waterfall.width, waterfall.height);
      sized = true;
    }
    const ww = waterfall.width;
    wf.drawImage(waterfall, 0, 1);
    const img = wf.createImageData(ww, 1);
    for (let x = 0; x < ww; x++) {
      const v = norm(m[Math.floor(x / ww * m.length)]);
      const c = Math.max(0, Math.min(255, Math.round(v * 255))) * 3;
      const o = x * 4;
      img.data[o] = HEAT[c]; img.data[o + 1] = HEAT[c + 1];
      img.data[o + 2] = HEAT[c + 2]; img.data[o + 3] = 255;
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
      // The ideal points ride in the same packed form, sent once per frame
      // rather than per slice; the scope caches the last one it saw.
      s.idealPoints = unpack(s.ideal, s.const_scale || 64);
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
