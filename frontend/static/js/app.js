/* ═══════════════════════════════════════════════════
   F1IQ — Main Application JS
   Handles: WebSocket, all API calls, panel rendering
═══════════════════════════════════════════════════ */

// ── Config ───────────────────────────────────────
const API = (() => {
  const loc = window.location;
  return {
    base: `${loc.protocol}//${loc.host}`,
    ws:   `${loc.protocol === 'https:' ? 'wss' : 'ws'}://${loc.host}`,
  };
})();

// ── App state ────────────────────────────────────
const S = {
  timing:    null,
  strategy:  null,
  predictor: null,
  debrief:   { summary: null, teams: {}, raceReports: {}, selectedRound: null },
  standings: { drivers: null, constructors: null },
  schedule:  null,
  telemDriver: null,
  telemData:   [],
  ws:          null,
  charts:      {},
  posHistory:  {},
  lapHistory:  [],
  loaded:      { strategy: false, predictor: false, debrief: false, standings: false, schedule: false, weekend: false, noSessionAI: false },
};

let DASH_LAYOUT_TEMPLATE = null;

// Team colour map (fallback for standings page)
const TEAM_COLOURS = {
  'red bull':'#3671C6','ferrari':'#CC0020','mclaren':'#CC6000',
  'mercedes':'#009980','aston martin':'#287A5A','alpine':'#0080B0',
  'williams':'#0055AA','haas':'#888888','racing bulls':'#4455DD',
  'rb':'#4455DD','kick sauber':'#228822','sauber':'#228822',
};
function teamColour(name='') {
  const lc = name.toLowerCase();
  for (const [k,v] of Object.entries(TEAM_COLOURS)) if (lc.includes(k)) return v;
  return '#888888';
}

// ── API helpers ──────────────────────────────────
async function api(path) {
  const res = await fetch(API.base + path);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

function loadingHTML(msg = 'Loading...') {
  return `<div class="loading"><div class="spinner"></div>${msg}</div>`;
}
function errHTML(msg) {
  return `<div class="api-err">${msg}</div>`;
}

function isNoSession(data) {
  // Treat sessions where live timing is unavailable as "no session" to avoid
  // showing the UI as if a live race is running.
  return !!(data && (data.no_session || data.live_unavailable));
}

function setWSState(text = 'OFFLINE') {
  const dot = document.getElementById('ws-dot');
  const lbl = document.getElementById('ws-label');
  const pill= document.getElementById('ws-pill');
  if (!dot || !lbl || !pill) return;
  dot.classList.add('off');
  lbl.textContent = text;
  pill.classList.add('offline');
}

// ── WebSocket ─────────────────────────────────────
function connectWS() {
  if (isNoSession(S.timing)) {
    setWSState('NO SESSION');
    return;
  }
  const dot = document.getElementById('ws-dot');
  const lbl = document.getElementById('ws-label');
  const pill= document.getElementById('ws-pill');

  function setOnline() {
    dot.classList.remove('off'); lbl.textContent = 'LIVE';
    pill.classList.remove('offline');
  }
  function setOffline(text = 'RECONNECTING') {
    dot.classList.add('off'); lbl.textContent = text;
    pill.classList.add('offline');
  }

  try {
    S.ws = new WebSocket(`${API.ws}/ws/timing`);
    S.ws.onopen    = setOnline;
    S.ws.onmessage = ev => {
      try {
        const data = JSON.parse(ev.data);
        if (data.type === 'timing') {
          S.timing = data;
          onTimingUpdate(data);
        }
      } catch(e) { console.warn('WS parse:', e); }
    };
    S.ws.onclose = ()=> {
      if (isNoSession(S.timing)) {
        setWSState('NO SESSION');
        return;
      }
      setOffline();
      setTimeout(connectWS, 3000);
    };
    S.ws.onerror = ()=> S.ws.close();
  } catch(e) { setOffline('OFFLINE'); }
}

function onTimingUpdate(data) {
  updateTopBar(data);
  trackHistory(data);
  const active = document.querySelector('.panel.active');
  if (!active) return;
  const id = active.id;
  if (id === 'panel-dash')   updateDash(data);
  if (id === 'panel-tower')  updateTower(data);
}

// ── Top bar ───────────────────────────────────────
function updateTopBar(data) {
  const el = document.getElementById('top-race');
  const sub= document.getElementById('top-sub');
  if (data.meeting_name) {
    el.textContent = data.meeting_name + (data.country ? ` · ${data.country}` : '');
  }
  if (isNoSession(data)) {
    sub.textContent = data.message || 'No live session';
    return;
  }
  const sc = data.safety_car ? ' · 🟡 Safety Car' : data.virtual_sc ? ' · 🟡 VSC' : '';
  sub.textContent = `Lap ${data.lap||0} / ${data.total_laps||'?'}${sc}`;
}

function startClock() {
  setInterval(() => {
    const el = document.getElementById('top-clk');
    if (el) el.textContent = new Date().toLocaleTimeString('en-GB') + ' UTC';
  }, 1000);
}

// ── History tracking ──────────────────────────────
function trackHistory(data) {
  if (isNoSession(data) || !data.drivers?.length) return;
  const lap = data.lap || 0;
  if (!S.lapHistory.includes(lap)) S.lapHistory.push(lap);
  data.drivers.slice(0, 6).forEach(d => {
    if (!S.posHistory[d.name_acronym]) S.posHistory[d.name_acronym] = [];
    const arr = S.posHistory[d.name_acronym];
    if (!arr.length || arr[arr.length-1].lap !== lap)
      arr.push({ lap, pos: d.position });
    if (arr.length > 80) arr.shift();
  });
}

// ── Chart defaults ────────────────────────────────
function cOpts(yLabel = '', extraY = {}) {
  return {
    responsive: true, maintainAspectRatio: false,
    plugins: { legend: { labels: { color:'#58586E', font:{ size:10, family:'Arial' }, boxWidth:12, padding:8 } } },
    scales: {
      x: { ticks:{ color:'#9898AE', font:{ size:10, family:'Arial' }, maxTicksLimit:10 },
           grid:{ color:'rgba(0,0,0,.05)' }, border:{ color:'rgba(0,0,0,.1)' } },
      y: { ticks:{ color:'#9898AE', font:{ size:10, family:'Arial' } },
           grid:{ color:'rgba(0,0,0,.05)' }, border:{ color:'rgba(0,0,0,.1)' },
           title:{ display:!!yLabel, text:yLabel, color:'#9898AE', font:{ size:10, family:'Arial' } },
           ...extraY }
    }
  };
}

// ════════════════════════════════════════════════════
// DASHBOARD
// ════════════════════════════════════════════════════
async function initDash() {
  // Keep a copy of the original dashboard layout so it can be restored after a no-session UI.
  const dashOuter = document.querySelector('.dash-outer');
  if (dashOuter && DASH_LAYOUT_TEMPLATE === null) DASH_LAYOUT_TEMPLATE = dashOuter.innerHTML;

  try {
    const data = await api('/api/live/timing');
    data.type = 'timing';
    S.timing = data;
    onTimingUpdate(data);
    if (isNoSession(data)) {
      setWSState('NO SESSION');
      return;
    }
    connectWS();
  } catch(e) {
    setWSState('OFFLINE');
    document.getElementById('mini-tower').innerHTML = errHTML('Could not load timing: ' + e.message);
  }
}

function updateDash(data) {
  if (isNoSession(data)) {
    setChip('dash-sc-chip', 'No Session', 'ch-gray');
    setChip('dash-lap-chip', 'Next Race', 'ch-blue');
    setText('s-lap', '—');
    setText('s-lap-sub', data.message || 'No live session');
    setText('s-status', 'No Live Session');
    document.getElementById('s-status').style.color = 'var(--text2)';
    setText('s-pits', '—');
    setText('s-temp', '—');
    setText('s-temp-sub', 'Open Race Weekend for the next event');

    // Render a different “no-race” dashboard layout with AI preview content
    if (!S.loaded.noSessionAI) {
      loadNoSessionDashboard();
      S.loaded.noSessionAI = true;
    }

    destroyChart('dashGapChart');
    return;
  }

  // If a live session arrives after the no-session AI dashboard, restore the normal dashboard layout
  if (S.loaded.noSessionAI) {
    const dashOuter = document.querySelector('.dash-outer');
    if (dashOuter && DASH_LAYOUT_TEMPLATE) dashOuter.innerHTML = DASH_LAYOUT_TEMPLATE;
    S.loaded.noSessionAI = false;
  }


  const sc = data.safety_car ? 'Safety Car' : data.virtual_sc ? 'VSC' : data.track_status || 'Green Flag';
  const scCls = data.safety_car ? 'ch-orange' : data.virtual_sc ? 'ch-yellow' : 'ch-green';

  // Status chips
  setChip('dash-sc-chip',  sc, scCls);
  setChip('dash-lap-chip', `Lap ${data.lap} / ${data.total_laps}`, 'ch-blue');

  // Stat strip
  setText('s-lap',      data.lap || '—');
  setText('s-lap-sub',  `of ${data.total_laps || '?'} total`);
  setText('s-status',   sc);
  document.getElementById('s-status').style.color =
    data.safety_car ? 'var(--orange)' : data.virtual_sc ? 'var(--yellow)' : 'var(--green)';

  const pits = (data.drivers || []).filter(d => d.in_pit).length;
  setText('s-pits', pits || '—');

  const wx = data.weather || {};
  const air = wx.air_temperature ?? wx.AirTemp ?? null;
  setText('s-temp',     air !== null ? `${Math.round(air)}°` : '—');
  setText('s-temp-sub', air !== null ? `Track ${Math.round(wx.track_temperature ?? wx.TrackTemp ?? 0)}° · ${(wx.rainfall||wx.Rainfall)?'🌧 Wet':'☀ Dry'}` : '—');

  if (data.drivers) {
    renderMiniTower(data.drivers);
    renderFLCard(data.drivers);
    renderDashProbs(data.drivers);
    renderPitWindows(data.drivers, data.lap, data.total_laps);
    updateGapChart(data.drivers, data.lap);
  }
  if (data.weather) renderWeatherCard(data.weather);
  if (data.race_control_latest) renderRCCard(data.race_control_latest);
}

function renderMiniTower(drivers) {
  document.getElementById('mini-tower').innerHTML = drivers.slice(0, 10).map(d => {
    const cmp = d.tyre_compound || '';
    return `<div class="mini-row${d.position===1?' lead':''}">
      <div class="m-pos${d.position===1?' gold':''}">${d.position}</div>
      <div class="m-stripe" style="background:${d.team_colour}"></div>
      <div class="m-abbr" style="color:${d.team_colour}">${d.name_acronym}</div>
      <div><div class="m-team">${d.team_name}</div></div>
      <div class="m-gap">${d.gap_to_leader}</div>
      <div class="m-tyre ${cmp}" title="${cmp}">${cmp.charAt(0)||'?'}</div>
    </div>`;
  }).join('');
}

async function loadNoSessionDashboard() {
  const dashOuter = document.querySelector('.dash-outer');
  if (!dashOuter) return;

  dashOuter.innerHTML = `
    <div class="ns-hero">
      <div class="ns-hero-content" id="ns-hero-content">
        <div style="text-align:center">
          <div class="ns-status">⏸ Between Sessions</div>
          <div class="ns-hero-race">Next Race</div>
          <div class="ns-countdown" id="ns-countdown">Loading race info…</div>
        </div>
      </div>
    </div>
    <div class="ns-container">
      <div class="ns-grid-top">
        <div class="ns-card ns-card-large" id="ns-card-podium">
          <div class="ns-card-hd">🏆 Predicted Podium</div>
          <div class="ns-card-bd"><div class="loading"><div class="spinner"></div></div></div>
        </div>
        <div class="ns-card ns-card-large" id="ns-card-preview">
          <div class="ns-card-hd">🔮 AI Race Preview</div>
          <div class="ns-card-bd"><div class="loading"><div class="spinner"></div></div></div>
        </div>
      </div>
      <div class="ns-grid-bottom">
        <div class="ns-card" id="ns-card-watch">
          <div class="ns-card-hd">👀 What to Watch</div>
          <div class="ns-card-bd"><div class="loading"><div class="spinner"></div></div></div>
        </div>
        <div class="ns-card" id="ns-card-weather">
          <div class="ns-card-hd">🌤 Weather Risk</div>
          <div class="ns-card-bd"><div class="loading"><div class="spinner"></div></div></div>
        </div>
        <div class="ns-card" id="ns-card-ask">
          <div class="ns-card-hd">💬 Ask the AI</div>
          <div class="ns-card-bd"><div class="loading"><div class="spinner"></div></div></div>
        </div>
      </div>
    </div>`;

  try {
    const data = await api('/api/weekend/next');
    renderNoSessionDashboard(data);
  } catch (e) {
    dashOuter.innerHTML = `<div class="api-err">Could not load AI preview: ${e.message}</div>`;
  }
}

function renderNoSessionDashboard(data) {
  const ai = data.ai_analyst || {};
  const preview = ai.preview || {};
  const predictions = ai.win_predictions || [];
  const watch = data.watch_guide || [];

  // Hero: Next race name and countdown
  const flag = (COUNTRY_FLAGS[data.country?.toLowerCase()] || '🏁');
  const heroContent = document.getElementById('ns-hero-content');
  if (heroContent) {
    heroContent.innerHTML = `
      <div style="text-align:center">
        <div class="ns-status">🏁 RACE WEEK COUNTDOWN</div>
        <div style="font-size:28px;margin-bottom:8px">${flag}</div>
        <div class="ns-hero-race">${data.race_name || 'Upcoming Race'}</div>
        <div class="ns-hero-circuit">${data.circuit || ''} · ${data.country || ''}</div>
        <div class="ns-countdown">${data.race_countdown_display || 'Loading…'}</div>
      </div>`;
  }

  // Podium
  const podiumLabels = ['🥇 POLE', '🥈 P2', '🥉 P3'];
  const top3 = predictions.slice(0, 3);
  const podiumHtml = top3.length ? `
    <div class="ns-podium-grid">
      ${top3.map((p, i) => `
        <div class="ns-podium-card podium-pos-${i+1}">
          <div class="ns-podium-badge">${podiumLabels[i]}</div>
          <div class="ns-podium-code" style="color:${teamColour(p.team)}">${p.code}</div>
          <div class="ns-podium-name">${p.full_name}</div>
          <div class="ns-podium-team">${p.team}</div>
          <div class="ns-podium-prob">${p.win_probability}%</div>
        </div>`).join('')}
    </div>`
    : '<div style="color:var(--text3);font-size:12px;text-align:center;padding:20px">Podium predictions coming…</div>';

  // Preview
  const previewHtml = preview.narrative ? `
    <div class="ns-preview-box">
      <div style="font-size:13px;color:var(--text2);line-height:1.65">${preview.narrative}</div>
      <div style="margin-top:12px;font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:.5px">Model: ${preview.model || 'F1IQ Analyst'} · Confidence: ${Math.round((preview.confidence||0)*100)}%</div>
    </div>`
    : '<div style="color:var(--text3);font-size:12px;padding:20px;text-align:center">Preview coming soon…</div>';

  // Watch guide
  const watchHtml = watch.length ? watch.slice(0, 5).map(w => `<div class="ns-bullet">${w}</div>`).join('')
    : '<div style="color:var(--text3);font-size:12px;padding:10px">No items yet.</div>';

  // Weather
  const weather = ai.weather_risk || {};
  const weatherHtml = `
    <div style="font-size:13px;color:var(--text2);line-height:1.5">${weather.summary || 'Weather analysis coming soon…'}</div>
    <div style="margin-top:10px;font-size:11px;color:var(--text3)">Risk: <strong>${weather.label || 'Unknown'}</strong> · Score: ${weather.score ?? '—'}</div>`;

  // Ask examples
  const askExamples = (ai.ask_examples || []).slice(0, 3);
  const askHtml = askExamples.length ? askExamples.map(q =>
    `<div class="ns-ask-chip" onclick="askAI('${q.replace(/'/g,'\'')}')">${q}</div>`).join('')
    : '<div style="color:var(--text3);font-size:12px;padding:10px">No prompts yet.</div>';

  // Render
  document.getElementById('ns-card-podium').querySelector('.ns-card-bd').innerHTML = podiumHtml;
  document.getElementById('ns-card-preview').querySelector('.ns-card-bd').innerHTML = previewHtml;
  document.getElementById('ns-card-watch').querySelector('.ns-card-bd').innerHTML = watchHtml;
  document.getElementById('ns-card-weather').querySelector('.ns-card-bd').innerHTML = weatherHtml;
  document.getElementById('ns-card-ask').querySelector('.ns-card-bd').innerHTML = askHtml;
}

function renderNoSessionAI(data) {
  const ai = data.ai_analyst || {};
  const preview = ai.preview || {};
  const predictions = ai.win_predictions || [];
  const watch = data.watch_guide || [];
  const matchups = ai.matchups || [];

  // Mini tower: next race summary + next session timetable
  const nextSessions = (data.sessions || []).filter(s => !s.past).slice(0, 3);
  const sessionsHtml = nextSessions.length ? nextSessions.map(s => `
    <div class="mini-row">
      <div class="m-pos" style="opacity:.7">${s.name}</div>
      <div class="m-stripe" style="background:var(--blue)"></div>
      <div class="m-abbr" style="color:var(--text)">${s.local_time || 'TBC'}</div>
      <div class="m-team">${s.display_countdown || '—'}</div>
    </div>`).join('') : '<div style="color:var(--text3);font-size:12px">Next sessions not yet available.</div>';

  document.getElementById('mini-tower').innerHTML = `
    <div class="mini-row">
      <div class="m-pos gold">Next</div>
      <div class="m-stripe" style="background:var(--blue)"></div>
      <div class="m-abbr">${data.race_name || 'Upcoming Race'}</div>
      <div class="m-team">${data.circuit || ''} · ${data.country || ''}</div>
    </div>
    ${sessionsHtml}`;

  // AI Preview
  document.getElementById('fl-card').innerHTML = `
    <div style="font-weight:700;margin-bottom:6px">AI Race Preview</div>
    <div style="font-size:13px;color:var(--text2);line-height:1.5">${preview.narrative || 'No preview available yet.'}</div>
    ${preview.model ? `<div style="margin-top:10px;font-size:11px;color:var(--text3)">Model: ${preview.model} · Confidence: ${Math.round((preview.confidence||0)*100)}%</div>` : ''}
  `;

  // Top 3 predictions
  const podiumLabels = ['🥇 1st', '🥈 2nd', '🥉 3rd'];
  const top3 = predictions.slice(0, 3);
  document.getElementById('dash-probs').innerHTML = top3.length ? `
    <div style="font-weight:700;margin-bottom:8px">Predicted Podium</div>
    ${top3.map((p, i) => `
      <div class="ai-prob-row">
        <div class="ai-prob-head">
          <div class="ai-podium-badge">${podiumLabels[i]}</div>
          <div class="ai-driver-code" style="color:${teamColour(p.team)}">${p.code}</div>
          <div>
            <div class="ai-driver-name">${p.full_name}</div>
            <div class="ai-driver-team">${p.team}</div>
          </div>
          <div class="ai-prob-nums">${p.win_probability}% win</div>
        </div>
        <div class="ai-prob-note">${p.why || ''}</div>
      </div>`).join('')}
  ` : '<div style="color:var(--text3);font-size:12px">Predictions will appear once analysis is ready.</div>';

  // Strategy / Watch guide
  const watchHtml = watch.length ? watch.slice(0, 4).map(w => `<div class="ai-bullet">${w}</div>`).join('')
    : '<div style="color:var(--text3);font-size:12px">No watch guide available yet.</div>';
  document.getElementById('dash-pits').innerHTML = `
    <div style="font-weight:700;margin-bottom:8px">What to Watch</div>
    ${watchHtml}
  `;

  // Weather risk
  const weather = ai.weather_risk || {};
  document.getElementById('weather-card').innerHTML = `
    <div style="font-weight:700;margin-bottom:6px">Weather & Track Risk</div>
    <div style="font-size:13px;color:var(--text2)">${weather.summary || 'No weather risk data yet.'}</div>
    <div style="margin-top:10px;font-size:11px;color:var(--text3)">Risk: ${weather.label || 'Unknown'} · Score: ${weather.score ?? '—'}</div>
  `;

  // AI Q&A prompt
  const examples = (ai.ask_examples || []).slice(0, 3);
  document.getElementById('rc-card').innerHTML = `
    <div style="font-weight:700;margin-bottom:8px">Ask the AI</div>
    <div style="font-size:12px;color:var(--text2)">Try these questions:</div>
    <div style="margin-top:6px;display:flex;flex-direction:column;gap:4px">
      ${examples.map(q => `<div class="chip ch-gray" style="font-size:11px;cursor:pointer" onclick="askAI('${q.replace(/'/g,'\'')}')">${q}</div>`).join('')}
    </div>
    <div style="margin-top:10px;font-size:11px;color:var(--text3)">Go to the Race Weekend tab for full AI analysis.</div>
  `;
}

async function askAI(question) {
  try {
    const res = await fetch('/api/weekend/ask', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question }),
    });
    const json = await res.json();
    alert(`${json.answer}\n\n(Model: ${json.model})`);
  } catch (e) {
    alert(`AI question failed: ${e.message}`);
  }
}

function renderFLCard(drivers) {
  const el = document.getElementById('fl-card');
  let fl = null;
  for (const d of drivers)
    if (d.best_lap_time && (!fl || d.best_lap_time < fl.best_lap_time)) fl = d;
  if (!fl) { el.innerHTML = '<div style="color:var(--text3);font-size:12px">No lap data yet</div>'; return; }
  el.innerHTML = `
    <div class="fl-row">
      <div>
        <div class="fl-abbr" style="color:${fl.team_colour}">${fl.name_acronym}</div>
        <div style="font-size:11px;color:var(--text2)">${fl.full_name}</div>
      </div>
      <div>
        <div class="fl-time">${fl.best_lap_time||'—'}</div>
        <div class="fl-note">${fl.team_name} · ${fl.tyre_compound||''} tyre</div>
      </div>
      <div style="margin-left:auto"><div class="chip ch-purple">FL</div></div>
    </div>
    <div class="sector-bars">
      ${fl.sector_1 ? `<div class="sec-bar-row"><div class="sec-lbl">S1</div><div class="sec-bg"><div class="sec-fill" style="width:88%;background:var(--pur-bdr)"></div></div><div class="sec-time">${fl.sector_1}</div></div>` : ''}
      ${fl.sector_2 ? `<div class="sec-bar-row"><div class="sec-lbl">S2</div><div class="sec-bg"><div class="sec-fill" style="width:94%;background:var(--red-bdr)"></div></div><div class="sec-time" style="color:var(--red);font-weight:700">${fl.sector_2} ★</div></div>` : ''}
      ${fl.sector_3 ? `<div class="sec-bar-row"><div class="sec-lbl">S3</div><div class="sec-bg"><div class="sec-fill" style="width:82%;background:var(--green-bdr)"></div></div><div class="sec-time">${fl.sector_3}</div></div>` : ''}
    </div>`;
}

function renderDashProbs(drivers) {
  const el = document.getElementById('dash-probs');
  const top5 = drivers.slice(0, 5);
  const maxPts = 100;
  el.innerHTML = top5.map((d, i) => {
    const pct = Math.max(2, Math.round(50 - i * 10 + Math.random() * 4));
    return `<div class="prob-row">
      <div class="prob-name" style="color:${d.team_colour}">${d.name_acronym}</div>
      <div class="prob-bg">
        <div class="prob-bar" style="width:${pct}%;background:${d.team_colour}28;border-left:3px solid ${d.team_colour}">
          <span style="color:${d.team_colour}">${pct}%</span>
        </div>
      </div>
    </div>`;
  }).join('');
}

function renderPitWindows(drivers, lap, total) {
  const el = document.getElementById('dash-pits');
  const candidates = drivers.filter(d => d.tyre_health !== null && d.tyre_health < 65).slice(0, 4);
  if (!candidates.length) {
    el.innerHTML = '<div style="color:var(--text3);font-size:12px;padding:4px">All tyres healthy</div>';
    return;
  }
  el.innerHTML = candidates.map(d => {
    const urgency = d.tyre_health < 25 ? 'danger' : d.tyre_health < 50 ? 'alert' : 'optimal';
    const cls  = urgency === 'danger' ? 'ch-red' : urgency === 'alert' ? 'ch-orange' : 'ch-green';
    const lbl  = urgency === 'danger' ? 'CRITICAL' : urgency === 'alert' ? 'ALERT' : 'OK';
    const note = `${d.tyre_compound} · ${d.tyre_age} laps · ${d.tyre_health?.toFixed(0)}% health`;
    return `<div class="pw-row">
      <div class="pw-abbr" style="color:${d.team_colour}">${d.name_acronym}</div>
      <div style="flex:1"><div class="pw-window">P${d.position}</div><div class="pw-note">${note}</div></div>
      <div class="chip ${cls}">${lbl}</div>
    </div>`;
  }).join('');
}

function renderWeatherCard(wx) {
  const el = document.getElementById('weather-card');
  const air = (wx.air_temperature ?? wx.AirTemp ?? '—');
  const trk = (wx.track_temperature ?? wx.TrackTemp ?? '—');
  const hum = (wx.humidity ?? wx.Humidity ?? '—');
  const ws  = (wx.wind_speed ?? wx.WindSpeed ?? '—');
  const wd  = (wx.wind_direction ?? wx.WindDirection ?? '—');
  const rain = (wx.rainfall || wx.Rainfall);
  el.innerHTML = `
    <div class="wx-row"><span class="wx-k">Air Temperature</span><span class="wx-v">${fmt(air, 1)} °C</span></div>
    <div class="wx-row"><span class="wx-k">Track Temperature</span><span class="wx-v">${fmt(trk, 1)} °C</span></div>
    <div class="wx-row"><span class="wx-k">Humidity</span><span class="wx-v">${fmt(hum, 0)}%</span></div>
    <div class="wx-row"><span class="wx-k">Wind</span><span class="wx-v">${fmt(ws, 1)} km/h · ${Math.round(wd)}°</span></div>
    <div class="wx-row"><span class="wx-k">Conditions</span><span class="wx-v" style="color:var(${rain?'--blue':'--green'})">${rain?'🌧 Wet':'☀ Dry'}</span></div>`;
}

function renderRCCard(msgs) {
  const el = document.getElementById('rc-card');
  if (!msgs?.length) { el.innerHTML = '<div style="color:var(--text3);font-size:12px;padding:4px">No messages</div>'; return; }
  el.innerHTML = msgs.slice(0, 5).map(m => {
    const flag = m.flag || m.category || 'MSG';
    const cls = flag==='GREEN'?'rc-green':flag==='YELLOW'?'rc-yellow':flag==='RED'?'rc-red':
                (flag==='SC'||flag==='VSC'||flag==='SafetyCar')?'rc-sc':flag==='BLUE'?'rc-blue':'rc-default';
    const lap = m.lap_number ? `L${m.lap_number} ` : '';
    return `<div class="rc-item"><span class="rc-flag ${cls}">${flag}</span><span>${lap}${m.message||''}</span></div>`;
  }).join('');
}

function updateGapChart(drivers, lap) {
  const ctx = document.getElementById('dashGapChart');
  if (!ctx) return;

  const top5 = drivers.slice(1, 6); // exclude leader
  if (!S.charts.gap) {
    S.charts.gap = new Chart(ctx.getContext('2d'), {
      type: 'line',
      data: {
        labels: [lap || 1],
        datasets: top5.map(d => ({
          label: d.name_acronym,
          data: [parseGap(d.gap_to_leader)],
          borderColor: d.team_colour, backgroundColor: 'transparent',
          tension: 0.4, borderWidth: 1.5, pointRadius: 0,
        }))
      },
      options: cOpts('Gap to Leader (s)')
    });
  } else {
    const chart = S.charts.gap;
    if (!chart.data.labels.includes(lap)) {
      chart.data.labels.push(lap);
      chart.data.datasets.forEach((ds, i) => {
        ds.data.push(parseGap(top5[i]?.gap_to_leader));
      });
      if (chart.data.labels.length > 60) {
        chart.data.labels.shift();
        chart.data.datasets.forEach(ds => ds.data.shift());
      }
      chart.update('none');
    }
  }
}

// ════════════════════════════════════════════════════
// LIVE TOWER
// ════════════════════════════════════════════════════
function updateTower(data) {
  const body = document.getElementById('tower-body');
  if (isNoSession(data)) {
    setChip('tower-sc-chip', 'No Session', 'ch-gray');
    setChip('tower-lap-chip', 'Next Race', 'ch-blue');
    setText('tower-updated', data.message || 'No live timing');
    body.innerHTML = '<div class="empty-state"><strong>No Active Session</strong>The live tower will populate automatically when timing is available.</div>';
    return;
  }
  setChip('tower-sc-chip',  data.safety_car ? 'Safety Car' : data.virtual_sc ? 'VSC' : data.track_status || 'Green Flag',
           data.safety_car ? 'ch-orange' : data.virtual_sc ? 'ch-yellow' : 'ch-green');
  setChip('tower-lap-chip', `Lap ${data.lap} / ${data.total_laps}`, 'ch-blue');
  setText('tower-updated', 'Updated ' + new Date().toLocaleTimeString('en-GB'));

  if (!data.drivers?.length) return;
  body.innerHTML = data.drivers.map(d => {
    const pc     = d.position===1?'p1':d.position===2?'p2':d.position===3?'p3':'';
    const cmp    = d.tyre_compound || '';
    const health = d.tyre_health ?? 75;
    const hc     = health > 60 ? 'var(--green)' : health > 30 ? '#A07800' : 'var(--red)';
    return `<div class="d-row${d.position===1?' lead':''}${d.in_pit?' pitrow':''}${d.retired?' dnfrow':''}">
      <div class="d-pos ${pc}">${d.position}</div>
      <div class="d-stripe" style="background:${d.team_colour}"></div>
      <div><div class="d-abbr" style="color:${d.team_colour}">${d.name_acronym}</div></div>
      <div><div class="d-nm">${d.full_name}</div><div class="d-tm">${d.team_name}</div></div>
      <div><div class="mono ${d.position===1?'gold':''}">${d.gap_to_leader||'—'}</div><div class="col-sub">gap</div></div>
      <div><div class="mono">${d.interval||'—'}</div><div class="col-sub">interval</div></div>
      <div><div class="mono ${d.position<=3?'purple':''}">${d.best_lap_time||'—'}</div><div class="col-sub">best</div></div>
      <div class="tyre-wrap">
        <div class="tyre-b ${cmp}" title="${cmp}">${cmp.charAt(0)||'?'}</div>
        <div>
          <div class="tyre-age">${d.tyre_age??0} laps</div>
          <div class="tyre-bar-bg"><div class="tyre-fill" style="width:${health}%;background:${hc}"></div></div>
        </div>
      </div>
      <div class="ind-grp">
        ${d.drs_open?'<div class="ind ind-drs">DRS</div>':''}
        ${d.in_pit?'<div class="ind ind-pit">PIT</div>':''}
      </div>
      <div><div class="mono ${d.position<=3?'green':''}">${d.last_lap_time||'—'}</div><div class="col-sub">last</div></div>
    </div>`;
  }).join('');
}

// ════════════════════════════════════════════════════
// PIT STRATEGY
// ════════════════════════════════════════════════════
async function loadStrategy() {
  if (S.loaded.strategy) return;
  const body = document.getElementById('strat-body');
  body.innerHTML = loadingHTML('Loading strategy data...');
  try {
    const data = await api('/api/live/strategy');
    S.strategy = data; S.loaded.strategy = true;
    renderStrategy(data);
  } catch(e) { body.innerHTML = errHTML('Strategy load failed: ' + e.message); }
}

function renderStrategy(strategies) {
  const body = document.getElementById('strat-body');
  if (!strategies?.length) { body.innerHTML = '<div class="empty-state"><strong>No Strategy Data</strong>Session may not be active yet</div>'; return; }

  const T = strategies[0]?.total_laps || 60;
  setChip('strat-lap-chip', `Lap ${strategies[0]?.current_lap||'?'} / ${T}`, 'ch-blue');

  body.innerHTML = strategies.map(s => {
    const stintHTML = (s.stints||[]).map(st => {
      const cmp = (st.compound||'').toLowerCase();
      const l = ((st.start_lap||0) / T * 100).toFixed(1);
      const w = Math.max(1, (st.lap_count||0) / T * 100).toFixed(1);
      return `<div class="stint-seg ${cmp}" style="left:${l}%;width:${w}%" title="${st.compound} · ${st.lap_count} laps">${(st.compound||'').charAt(0)}</div>`;
    }).join('')
    + (s.pit_laps_done||[]).map(p => `<div class="pit-pin" style="left:${(p/T*100).toFixed(1)}%" title="Pit lap ${p}"></div>`).join('')
    + `<div class="lap-needle" style="left:${((s.current_lap||0)/T*100).toFixed(1)}%"></div>`;

    const tacHTML = (s.tactics||[]).map(t => {
      const cls = t==='undercut'?'tac-undercut':t==='overcut'?'tac-overcut':t==='stay'?'tac-stay':t.includes('pit')?'tac-pitnow':'tac-alert';
      const lbl = {undercut:'Undercut',overcut:'Overcut',stay:'Stay Out','pit-now':'Pit Now','pit_now':'Pit Now',alert:'Alert'}[t] || t;
      return `<span class="tac ${cls}">${lbl}</span>`;
    }).join('');

    return `<div class="strat-card">
      <div class="strat-hd">
        <div style="width:5px;height:36px;border-radius:2px;background:${s.team_colour};flex-shrink:0"></div>
        <div>
          <div class="strat-name" style="color:${s.team_colour}">${s.name_acronym}
            <span style="color:var(--text);font-size:13px;font-weight:600">&nbsp;${s.full_name}</span>
          </div>
          <div class="strat-team">${s.team_name}</div>
        </div>
        <div style="margin-left:auto;display:flex;align-items:center;gap:8px">
          <span style="font-size:11px;color:var(--text2)">${(s.pit_laps_done||[]).length} stop${(s.pit_laps_done||[]).length!==1?'s':''}</span>
          <span class="chip ch-blue">P${s.current_position}</span>
        </div>
      </div>
      <div class="strat-bd">
        <div class="stint-row">
          <div class="stint-lbl">Lap 1 → ${T}</div>
          <div class="stint-track">${stintHTML}</div>
          <div class="stint-cur">Lap ${s.current_lap}</div>
        </div>
        <div class="rec-box ${s.recommendation_type||'optimal'}">
          <div class="rec-title">${s.recommendation_title||'—'}</div>
          <div class="rec-text">${s.recommendation_text||'—'}</div>
          <div class="tac-row">${tacHTML}</div>
        </div>
      </div>
    </div>`;
  }).join('');
}

// ════════════════════════════════════════════════════
// WIN PREDICTOR
// ════════════════════════════════════════════════════
async function loadPredictor() {
  if (S.loaded.predictor) return;
  document.getElementById('full-probs').innerHTML = loadingHTML();
  try {
    const data = await api('/api/live/predictor');
    S.predictor = data; S.loaded.predictor = true;
    renderPredictor(data);
  } catch(e) {
    document.getElementById('full-probs').innerHTML = errHTML('Predictor failed: ' + e.message);
  }
}

function renderPredictor(data) {
  const preds = data.predictions || [];
  const hasData = preds.length > 0;
  const conf = hasData ? Math.round(((data.model_confidence ?? 0) * 100)) : 0;

  if (!hasData) {
    setChip('pred-conf-chip', 'No live prediction', 'ch-grey');
    document.getElementById('full-probs').innerHTML = `<div class="empty">No live prediction data available.</div>`;
    destroyChart('featureChart');
    destroyChart('tyreChart');
    destroyChart('posChart');
    return;
  }

  const sorted = [...preds].sort((a, b) => (b.win_probability || 0) - (a.win_probability || 0));
  const podium = sorted.slice(0, 3);
  const podiumLabels = ['🥇 1st', '🥈 2nd', '🥉 3rd'];

  setChip('pred-conf-chip', `Confidence ${conf}%`, 'ch-green');
  document.getElementById('full-probs').innerHTML = `
    <div class="podium-block">
      ${podium.map((p, i) => `
        <div class="podium-row podium-${i+1}">
          <div class="podium-badge">${podiumLabels[i]}</div>
          <div class="podium-name" style="color:${p.team_colour}">${p.name_acronym}</div>
          <div class="podium-team" style="color:var(--text3);font-size:11px;margin-left:auto">${p.team_name}</div>
          <div class="podium-prob">${Math.round(p.win_probability)}%</div>
        </div>`).join('')}
    </div>
    <div class="prob-list">
      ${sorted.map(p => `
        <div class="prob-row2">
          <div class="prob-nm2" style="color:${p.team_colour}">${p.name_acronym}</div>
          <div class="prob-bg2">
            <div class="prob-bar2" style="width:${p.win_probability}%;background:${p.team_colour}28;border-left:3px solid ${p.team_colour}">
              <span style="color:${p.team_colour}">${p.win_probability}%</span>
            </div>
          </div>
          <div class="prob-pct2">${p.win_probability}%</div>
        </div>`).join('')}
    </div>`;

  // Feature weights
  const fw = data.feature_weights || {};
  destroyChart('featureChart');
  S.charts.feature = new Chart(
    document.getElementById('featureChart').getContext('2d'), {
      type: 'bar',
      data: {
        labels: Object.keys(fw).map(k => k.replace(/_/g,' ')),
        datasets: [{ label:'Weight %', data: Object.values(fw),
          backgroundColor: Object.keys(fw).map((_,i)=>`hsl(${i*40+200},55%,65%)`),
          borderColor:     Object.keys(fw).map((_,i)=>`hsl(${i*40+200},55%,45%)`),
          borderWidth:1.5, borderRadius:3 }]
      },
      options: { ...cOpts('Weight (%)'), indexAxis:'y' }
    }
  );

  // Tyre life
  const top6 = preds.slice(0, 6);
  destroyChart('tyreChart');
  S.charts.tyre = new Chart(
    document.getElementById('tyreChart').getContext('2d'), {
      type: 'bar',
      data: {
        labels: top6.map(p=>p.name_acronym),
        datasets: [{ label:'Laps on Tyre', data: top6.map(p=>p.tyre_age||0),
          backgroundColor: top6.map(p=>p.team_colour+'44'),
          borderColor: top6.map(p=>p.team_colour),
          borderWidth:1.5, borderRadius:3 }]
      },
      options: cOpts('Laps')
    }
  );

  // Position history
  const labels = S.lapHistory.slice(-50);
  if (!labels.length) labels.push(1);
  destroyChart('posChart');
  S.charts.pos = new Chart(
    document.getElementById('posChart').getContext('2d'), {
      type: 'line',
      data: {
        labels,
        datasets: preds.slice(0, 5).map(p => ({
          label: p.name_acronym,
          data:  (S.posHistory[p.name_acronym]||[]).slice(-50).map(x=>x.pos),
          borderColor: p.team_colour, backgroundColor:'transparent',
          tension:.3, borderWidth:1.5, pointRadius:0,
        }))
      },
      options: cOpts('Position', { reverse:true, min:1, max:10 })
    }
  );

  // Lap time delta (simulated from position history)
  destroyChart('lapDeltaChart');
  S.charts.lapDelta = new Chart(
    document.getElementById('lapDeltaChart').getContext('2d'), {
      type: 'line',
      data: {
        labels,
        datasets: preds.slice(1, 6).map(p => ({
          label: p.name_acronym,
          data: (S.posHistory[p.name_acronym]||[]).slice(-50).map((x,i) => +(x.pos * 2.1 + Math.random()*.3).toFixed(3)),
          borderColor: p.team_colour, backgroundColor:'transparent',
          tension:.4, borderWidth:1.5, pointRadius:0,
        }))
      },
      options: cOpts('Gap to Leader (s)')
    }
  );
}

// ════════════════════════════════════════════════════
// STANDINGS
// ════════════════════════════════════════════════════
async function loadStandings() {
  if (S.loaded.standings) return;
  const dc = document.getElementById('drivers-table-wrap');
  const cc = document.getElementById('constructors-table-wrap');
  dc.innerHTML = loadingHTML('Loading 2026 standings...');
  cc.innerHTML = loadingHTML();
  try {
    const [drivers, constructors] = await Promise.all([
      api('/api/standings/drivers?year=2026'),
      api('/api/standings/constructors?year=2026'),
    ]);
    S.standings = { drivers, constructors };
    S.loaded.standings = true;
    renderDriverStandings(drivers);
    renderConstructorStandings(constructors);
    // Update standings chips
    const chip = document.getElementById('standings-round-chip');
    if (chip) chip.textContent = 'After Round 2 · China';
  } catch(e) {
    dc.innerHTML = errHTML('Failed: ' + e.message);
    cc.innerHTML = '';
  }
}

function renderDriverStandings(drivers) {
  const maxPts = drivers[0]?.points || 1;
  document.getElementById('drivers-table-wrap').innerHTML = `
    <table class="standings-table">
      <thead><tr>
        <th>P</th><th>Driver</th><th>Team</th><th>W</th><th style="text-align:right">Points</th>
      </tr></thead>
      <tbody>${drivers.map(d => {
        const pc = d.position===1?'p1':d.position===2?'p2':d.position===3?'p3':'';
        const tc = teamColour(d.team);
        const barW = Math.round(d.points / maxPts * 100);
        return `<tr>
          <td class="pos-cell ${pc}">${d.position}</td>
          <td><strong>${d.full_name}</strong></td>
          <td style="font-size:11px;color:var(--text2)">
            <span class="team-swatch" style="background:${tc}"></span>${d.team}
          </td>
          <td>${d.wins > 0 ? `<span class="wins-badge">${d.wins}W</span>` : '—'}</td>
          <td class="pts-cell">
            ${d.points}
            <span class="points-bar-bg"><span class="points-bar" style="width:${barW}%;background:${tc}"></span></span>
          </td>
        </tr>`;
      }).join('')}</tbody>
    </table>`;
}

function renderConstructorStandings(constructors) {
  const maxPts = constructors[0]?.points || 1;
  document.getElementById('constructors-table-wrap').innerHTML = `
    <table class="standings-table">
      <thead><tr>
        <th>P</th><th>Constructor</th><th>Nat.</th><th>W</th><th style="text-align:right">Points</th>
      </tr></thead>
      <tbody>${constructors.map(c => {
        const pc = c.position===1?'p1':c.position===2?'p2':c.position===3?'p3':'';
        const tc = teamColour(c.name);
        const barW = Math.round(c.points / maxPts * 100);
        return `<tr>
          <td class="pos-cell ${pc}">${c.position}</td>
          <td>
            <span class="team-swatch" style="background:${tc}"></span>
            <strong>${c.name}</strong>
          </td>
          <td style="font-size:11px;color:var(--text2)">${c.nationality}</td>
          <td>${c.wins > 0 ? `<span class="wins-badge">${c.wins}W</span>` : '—'}</td>
          <td class="pts-cell">
            ${c.points}
            <span class="points-bar-bg"><span class="points-bar" style="width:${barW}%;background:${tc}"></span></span>
          </td>
        </tr>`;
      }).join('')}</tbody>
    </table>`;
}

// ════════════════════════════════════════════════════
// SCHEDULE
// ════════════════════════════════════════════════════
async function loadSchedule() {
  if (S.loaded.schedule) return;
  document.getElementById('schedule-grid').innerHTML = loadingHTML('Loading 2026 calendar...');
  try {
    // Use the rich weekend schedule endpoint which has session details
    const data = await api('/api/weekend/schedule');
    S.schedule = data; S.loaded.schedule = true;
    renderSchedule(data);
  } catch(e) {
    document.getElementById('schedule-grid').innerHTML = errHTML('Failed: ' + e.message);
  }
}

function renderSchedule(races) {
  const today = new Date().toISOString().slice(0, 10);
  let nextIdx = -1;
  for (let i = 0; i < races.length; i++) {
    if (races[i].status !== 'completed' && races[i].race_date >= today) { nextIdx = i; break; }
  }

  const SESSION_TYPE_COLOURS = {
    FP1:'#0055BB', FP2:'#0055BB', FP3:'#0055BB',
    SQ:'#B04000', SPRINT:'#B04000', Q:'#6A28A8', RACE:'#E8002D',
  };

  document.getElementById('schedule-grid').innerHTML = races.map((r, i) => {
    const isNext = i === nextIdx;
    const raceDate = r.race_date || r.date || '';
    const cls = r.status === 'completed' ? 'completed' : r.status === 'live' ? 'live' : isNext ? 'next' : '';
    const flag = raceFlag(r.country || '');

    // Status chip
    let statusChip = '';
    if (r.status === 'live')        statusChip = `<span class="chip ch-red">LIVE NOW</span>`;
    else if (r.status === 'completed') statusChip = r.winner ? `<span class="chip ch-gray">✓ ${r.winner}</span>` : `<span class="chip ch-gray">Completed</span>`;
    else if (isNext)                statusChip = `<span class="chip ch-blue">NEXT RACE</span>`;

    // Sprint badge
    const sprintBadge = r.sprint ? `<span class="chip ch-orange" style="font-size:9px">⚡ Sprint</span>` : '';

    // Special note
    const noteBadge = r.note ? `<span class="chip ch-yellow" style="font-size:9px" title="${r.note}">⚠ Special</span>` : '';

    // Session dots — compact visual for what's in this weekend
    const sessions = r.sessions || [];
    const sessDotsHTML = sessions.map(s => {
      const col = SESSION_TYPE_COLOURS[s.type] || '#888';
      const label = s.type === 'SPRINT' ? 'SPR' : s.type === 'SQ' ? 'SQ' : s.type;
      return `<span title="${s.name} · ${s.date} ${s.local_time}" style="display:inline-flex;align-items:center;gap:2px;font-size:9px;font-weight:700;padding:1px 5px;border-radius:2px;background:${col}18;border:1px solid ${col}44;color:${col};font-family:'Courier New',monospace">${label}</span>`;
    }).join('');

    const dateStr = raceDate ? new Date(raceDate + 'T12:00:00').toLocaleDateString('en-GB', { day:'numeric', month:'short', year:'numeric' }) : '—';

    return `<div class="race-card ${cls}" onclick="go('weekend', document.querySelector('.nav-tab:nth-child(7)'))">
      <div class="race-round ${r.status==='live'?'live-round':''}">R${r.round}</div>
      <div class="race-info-col">
        <div class="race-name-big">${flag} ${r.race_name}</div>
        <div class="race-circuit">${r.circuit}${r.locality ? ` · ${r.locality}` : ''}</div>
        <div style="display:flex;gap:4px;flex-wrap:wrap;margin-top:6px">${sessDotsHTML}</div>
        <div style="display:flex;gap:4px;margin-top:5px">${sprintBadge}${noteBadge}</div>
      </div>
      <div class="race-date-col">
        <div class="race-date">${dateStr}</div>
        <div class="race-time">${r.utc_offset ? r.utc_offset + ' local' : ''}</div>
        <div style="margin-top:6px;display:flex;flex-direction:column;gap:3px;align-items:flex-end">${statusChip}</div>
      </div>
    </div>`;
  }).join('');
}

function countryFlag(country) {
  return raceFlag(country);
}

// ════════════════════════════════════════════════════
// DEBRIEF
// ════════════════════════════════════════════════════
async function loadDebrief(requestedRound = null) {
  if (S.loaded.debrief && requestedRound === null && S.debrief.summary) return;
  document.getElementById('team-list').innerHTML = loadingHTML('Loading completed race debriefs...');
  document.getElementById('db-panel').innerHTML = loadingHTML('Loading race debrief...');
  try {
    const summary = await api(debriefPath('/api/debrief/summary', requestedRound));
    S.debrief.summary = summary;
    S.debrief.selectedRound = summary?.round || null;
    S.loaded.debrief = true;
    renderDebriefList(summary);
    if (summary?.available) await showRaceDebrief();
    else document.getElementById('db-panel').innerHTML = errHTML(summary?.error || 'No completed race available.');
  } catch (e) {
    document.getElementById('team-list').innerHTML = errHTML('Failed: ' + e.message);
    document.getElementById('db-panel').innerHTML = errHTML('Could not load race debrief: ' + e.message);
  }
}

function switchItab(el, id) {
  el.parentElement.querySelectorAll('.itab').forEach(t=>t.classList.remove('active'));
  el.closest('.db-body').querySelectorAll('.itab-pane').forEach(p=>p.classList.remove('active'));
  el.classList.add('active');
  document.getElementById(id)?.classList.add('active');
}

function secsToLap(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(Number(seconds))) return 'n/a';
  const total = Number(seconds);
  const mins = Math.floor(total / 60);
  const secs = total - mins * 60;
  return `${mins}:${secs.toFixed(3).padStart(6, '0')}`;
}

function debriefPath(path, roundNum = null, extra = {}) {
  const params = new URLSearchParams();
  if (roundNum !== null && roundNum !== undefined && roundNum !== '') params.set('round_num', String(roundNum));
  Object.entries(extra).forEach(([key, value]) => {
    if (value !== null && value !== undefined && value !== '') params.set(key, String(value));
  });
  const qs = params.toString();
  return qs ? `${path}?${qs}` : path;
}

function debriefCacheKey(kind, slug, roundNum = null) {
  return `${kind}::${slug || ''}::${roundNum || ''}`;
}

function activeDebriefItem(itemId) {
  document.querySelectorAll('#team-list .t-item').forEach(el => {
    el.classList.toggle('active', el.id === itemId);
  });
}

function renderDebriefList(summary) {
  const constructors = summary?.constructors || [];
  const completedRaces = summary?.completed_races || [];
  const currentRound = summary?.round || '';
  if (!constructors.length && !completedRaces.length) {
    document.getElementById('team-list').innerHTML = '<div style="color:var(--text3);font-size:12px;padding:8px 4px">No completed race available for debrief analysis.</div>';
    return;
  }

  const raceOptions = completedRaces.map(item => `
    <option value="${item.round}" ${Number(item.round) === Number(currentRound) ? 'selected' : ''}>
      R${item.round} · ${item.race_name}
    </option>`).join('');

  document.getElementById('team-list').innerHTML = `
    <div class="debrief-select-wrap">
      <div class="debrief-select-label">Completed Race</div>
      <select class="debrief-select" onchange="changeDebriefRace(this.value)">
        ${raceOptions}
      </select>
    </div>
    <div class="debrief-meta">
      <div class="debrief-meta-k">Selected Debrief</div>
      <div class="debrief-meta-v">${summary.race?.name || 'Completed race'} · R${summary.round || '—'}</div>
      <div class="debrief-meta-sub">${summary.race?.date || 'Date unavailable'}${summary.race?.country ? ` · ${summary.race.country}` : ''}</div>
    </div>
    <div class="t-item active" id="ti-race" onclick="showRaceDebrief()">
      <div class="t-dot debrief-race-dot"></div>
      <div>
        <div class="t-name">Race Debrief</div>
        <div class="t-sub">Default full-race summary before team drill-down</div>
      </div>
      <div class="t-pts">R${summary.round || '—'}</div>
    </div>` + constructors.map((c, i) => `
    <div class="t-item" id="ti-${i}" onclick="showDebrief(${i})">
      <div class="t-dot" style="background:${teamColour(c.name)}"></div>
      <div>
        <div class="t-name">${c.name}</div>
        <div class="t-sub">${c.race_points || 0} pts in race · ${c.best_finish ? `best P${c.best_finish}` : 'no finish data'}</div>
      </div>
      <div class="t-pts">P${c.position}</div>
    </div>`).join('');
}

async function changeDebriefRace(roundNum) {
  const parsed = Number(roundNum);
  if (!Number.isFinite(parsed)) return;
  S.debrief.selectedRound = parsed;
  await loadDebrief(parsed);
}

async function showRaceDebrief() {
  const summary = S.debrief.summary;
  if (!summary?.available) return;
  activeDebriefItem('ti-race');

  const panel = document.getElementById('db-panel');
  const roundNum = summary.round || null;
  const cacheKey = debriefCacheKey('race', 'default', roundNum);
  const race = summary.race || {};
  panel.innerHTML = `
    <div class="db-band">
      <div style="width:5px;height:52px;background:var(--blue);border-radius:3px;flex-shrink:0"></div>
      <div style="flex:1">
        <div class="db-tname">${race.name || 'Race Debrief'}</div>
        <div style="font-size:11px;color:var(--text2)">
          ${race.date || 'Date unavailable'} · ${race.circuit || 'Circuit unavailable'}${race.country ? ` · ${race.country}` : ''}
        </div>
      </div>
      <div class="db-head-stat">
        <div class="db-head-stat-k">Round</div>
        <div class="db-head-stat-v">R${summary.round || '—'}</div>
      </div>
    </div>
    <div class="db-body">${loadingHTML('Building race debrief...')}</div>`;

  try {
    const report = S.debrief.raceReports[cacheKey] || await api(debriefPath('/api/debrief/race', roundNum));
    S.debrief.raceReports[cacheKey] = report;
    if (!report?.available) {
      panel.innerHTML = errHTML(report?.error || 'Race debrief unavailable.');
      return;
    }

    const podiumCards = (report.podium || []).map(item => `
      <div class="dbc">
        <div class="dbc-nm">${item.full_name}</div>
        <div class="dbc-r"><span class="dbc-k">Finish</span><span class="dbc-v">P${item.position || '—'}</span></div>
        <div class="dbc-r"><span class="dbc-k">Team</span><span class="dbc-v">${item.team || 'n/a'}</span></div>
        <div class="dbc-r"><span class="dbc-k">Grid</span><span class="dbc-v">${item.grid_position ? `P${item.grid_position}` : 'n/a'}</span></div>
        <div class="dbc-r"><span class="dbc-k">Points</span><span class="dbc-v">${fmt(item.points, 0)}</span></div>
      </div>`).join('');
    const storylines = (report.storylines || []).map(item => `<div class="db-note-row">${item}</div>`).join('');
    const retirements = (report.retirements || []).length
      ? report.retirements.map(item => `<div class="iss-item"><span class="iss-tag iss-p2">OUT</span><span>${item.full_name} · ${item.team} · ${item.status}</span></div>`).join('')
      : '<div style="color:var(--text3);font-size:12px">No retirements were recorded in the available result feed.</div>';
    const gainers = (report.biggest_gainers || []).length
      ? report.biggest_gainers.map(item => `<div class="db-pill good">${item.driver} +${item.delta}</div>`).join('')
      : '<div style="color:var(--text3);font-size:12px">No positive grid-to-flag swings were recorded.</div>';
    const losers = (report.biggest_losers || []).length
      ? report.biggest_losers.map(item => `<div class="db-pill bad">${item.driver} ${item.delta}</div>`).join('')
      : '<div style="color:var(--text3);font-size:12px">No negative grid-to-flag swings were recorded.</div>';
    const constructorRows = (report.constructor_points || []).slice(0, 8).map(item => `
      <div class="champ-row">
        <div class="champ-pos">P${item.position || '—'}</div>
        <div class="champ-name">${item.name}</div>
        <div class="champ-pts">${fmt(item.race_points, 0)} pts</div>
      </div>`).join('');
    const champ = report.championship_impact || {};
    const champDrivers = (champ.drivers || []).map(d => `
      <div class="champ-row">
        <div class="champ-pos">P${d.position || '—'}</div>
        <div class="champ-name">${d.full_name}</div>
        <div class="champ-pts">${fmt(d.points, 0)}</div>
      </div>`).join('');
    const champConstructors = (champ.constructors || []).map(c => `
      <div class="champ-row">
        <div class="champ-pos">P${c.position || '—'}</div>
        <div class="champ-name">${c.name}</div>
        <div class="champ-pts">${fmt(c.points, 0)}</div>
      </div>`).join('');

    panel.innerHTML = `
      <div class="db-band">
        <div style="width:5px;height:52px;background:var(--blue);border-radius:3px;flex-shrink:0"></div>
        <div style="flex:1">
          <div class="db-tname">${report.race?.name || race.name || 'Race Debrief'}</div>
          <div style="font-size:11px;color:var(--text2)">
            ${report.race?.date || race.date || 'Date unavailable'} · ${report.race?.circuit || race.circuit || 'Circuit unavailable'}${report.race?.country ? ` · ${report.race.country}` : ''}
          </div>
        </div>
        <div class="db-head-stat">
          <div class="db-head-stat-k">Winner</div>
          <div class="db-head-stat-v">${report.podium?.[0]?.driver || '—'}</div>
        </div>
      </div>
      <div class="db-body">
        <div class="itabs">
          <div class="itab active" onclick="switchItab(this,'race-overview')">Overview</div>
          <div class="itab" onclick="switchItab(this,'race-order')">Finish Order</div>
          <div class="itab" onclick="switchItab(this,'race-strategy')">Strategy</div>
          <div class="itab" onclick="switchItab(this,'race-champ')">Championship</div>
        </div>
        <div class="itab-pane active" id="race-overview">
          <div class="db-kpi-grid">
            <div class="db-kpi-card">
              <div class="db-kpi-k">Executive Summary</div>
              <div class="db-kpi-copy">${report.executive_summary || 'Race summary unavailable.'}</div>
            </div>
            <div class="db-kpi-card">
              <div class="db-kpi-k">Race Weather</div>
              <div class="db-kpi-copy">${report.weather?.summary || 'Weather unavailable.'}</div>
            </div>
          </div>
          <div class="db-note-card">
            <div class="sub-sec-t">Race Storylines</div>
            <div class="db-story-list">${storylines}</div>
          </div>
          <div class="db-two-col">
            <div class="db-note-card">
              <div class="sub-sec-t">Biggest Gainers</div>
              <div class="db-pill-row">${gainers}</div>
            </div>
            <div class="db-note-card">
              <div class="sub-sec-t">Biggest Losers</div>
              <div class="db-pill-row">${losers}</div>
            </div>
          </div>
          <div class="sub-sec-t">Retirements</div>
          <div class="iss-list">${retirements}</div>
        </div>
        <div class="itab-pane" id="race-order">
          <div class="sub-sec-t">Podium</div>
          <div class="d-cmp">${podiumCards || '<div style="color:var(--text3);font-size:12px">No podium data available.</div>'}</div>
          <div class="sub-sec-t" style="margin-top:12px">Constructor Race Points</div>
          <div class="db-note-card">${constructorRows || '<div style="color:var(--text3);font-size:12px">No constructor race points available.</div>'}</div>
        </div>
        <div class="itab-pane" id="race-strategy">
          <div class="db-kpi-grid">
            <div class="db-kpi-card">
              <div class="db-kpi-k">Field Strategy Shape</div>
              <div class="db-card-title">${report.strategy_overview?.label || 'Unavailable'}</div>
              <div class="db-kpi-copy">${report.strategy_overview?.summary || 'No strategy summary available.'}</div>
            </div>
            <div class="db-kpi-card">
              <div class="db-kpi-k">Execution Notes</div>
              <div class="act-list">${(report.strategy_overview?.notes || []).map(item => `<div class="act-item"><span class="act-arrow">→</span><span>${item}</span></div>`).join('') || '<div style="color:var(--text3);font-size:12px">No strategy notes available.</div>'}</div>
            </div>
          </div>
        </div>
        <div class="itab-pane" id="race-champ">
          <div class="champ-context">
            <div class="champ-narrative">${champ.summary || 'Championship context unavailable.'}</div>
            <div>
              <div class="champ-half-title">Drivers</div>
              ${champDrivers || '<div style="color:var(--text3);font-size:12px">No driver standings available.</div>'}
            </div>
            <div>
              <div class="champ-half-title">Constructors</div>
              ${champConstructors || '<div style="color:var(--text3);font-size:12px">No constructor standings available.</div>'}
            </div>
          </div>
        </div>
      </div>`;
  } catch (e) {
    panel.innerHTML = errHTML('Could not load race debrief: ' + e.message);
  }
}

async function showDebrief(idx) {
  activeDebriefItem(`ti-${idx}`);
  const t = S.debrief.summary?.constructors?.[idx];
  if (!t) return;

  const tc = teamColour(t.name);
  const panel = document.getElementById('db-panel');
  const race = S.debrief.summary?.race || {};
  const roundNum = S.debrief.summary?.round || null;
  const cacheKey = debriefCacheKey('team', t.name, roundNum);

  panel.innerHTML = `
    <div class="db-band">
      <div style="width:5px;height:52px;background:${tc};border-radius:3px;flex-shrink:0"></div>
      <div style="flex:1">
        <div class="db-tname" style="color:${tc}">${t.name}</div>
        <div style="font-size:11px;color:var(--text2)">
          ${race.name || 'Latest completed race'} · ${race.circuit || 'Circuit unavailable'}${race.country ? ` · ${race.country}` : ''}
        </div>
      </div>
      <div class="db-head-stat">
        <div class="db-head-stat-k">Race Score</div>
        <div class="db-head-stat-v">${t.race_points || 0} pts</div>
      </div>
    </div>
    <div class="db-body">${loadingHTML('Building team debrief...')}</div>`;

  try {
    const report = S.debrief.teams[cacheKey] || await api(debriefPath('/api/debrief/team', roundNum, { team_name: t.name }));
    S.debrief.teams[cacheKey] = report;
    if (!report?.available) {
      panel.innerHTML = errHTML(report?.error || 'Debrief data unavailable.');
      return;
    }

    const overList = (report.overperformers || []).length
      ? report.overperformers.map(item => `<div class="db-pill good">${item.driver} ${item.delta > 0 ? `+${item.delta}` : item.delta}</div>`).join('')
      : '<div style="color:var(--text3);font-size:12px">No overperformance flag from grid-versus-finish data.</div>';
    const underList = (report.underperformers || []).length
      ? report.underperformers.map(item => `<div class="db-pill bad">${item.driver} ${item.delta}</div>`).join('')
      : '<div style="color:var(--text3);font-size:12px">No underperformance flag from grid-versus-finish data.</div>';
    const driverCards = (report.driver_reviews || []).map(driver => `
      <div class="dbc">
        <div class="dbc-nm" style="color:${tc}">${driver.full_name}</div>
        <div class="dbc-r"><span class="dbc-k">Finish</span><span class="dbc-v">${driver.finish_position ? `P${driver.finish_position}` : driver.status || 'n/a'}</span></div>
        <div class="dbc-r"><span class="dbc-k">Grid</span><span class="dbc-v">${driver.grid_position ? `P${driver.grid_position}` : 'n/a'}</span></div>
        <div class="dbc-r"><span class="dbc-k">Swing</span><span class="dbc-v">${driver.delta === null || driver.delta === undefined ? 'flat' : (driver.delta > 0 ? `+${driver.delta}` : driver.delta)}</span></div>
        <div class="dbc-r"><span class="dbc-k">Points</span><span class="dbc-v">${fmt(driver.points, 0)}</span></div>
        <div class="dbc-r"><span class="dbc-k">Median Pace</span><span class="dbc-v">${driver.average_lap_s ? secsToLap(driver.average_lap_s) : 'n/a'}</span></div>
        <div class="dbc-r"><span class="dbc-k">Pit Stops</span><span class="dbc-v">${driver.pit_stops ?? 'n/a'}</span></div>
        <div class="db-card-copy">${driver.note}</div>
      </div>`).join('');
    const strategyNotes = (report.strategy_audit?.notes || []).map(note => `<div class="db-note-row">${note}</div>`).join('');
    const incidentList = (report.incident_explanations || []).map(item => `<div class="iss-item"><span class="iss-tag iss-p2">NOTE</span><span>${item}</span></div>`).join('');
    const actionList = (report.action_items || []).map(item => `<div class="act-item"><span class="act-arrow">→</span><span>${item}</span></div>`).join('');
    const askChips = (report.ask_suggestions || []).map(q => `<button class="ask-chip" onclick='useDebriefQuestion(${JSON.stringify(q)}, ${JSON.stringify(t.name)}, ${report.round || 'null'})'>${q}</button>`).join('');
    const champ = report.championship_impact || {};
    const champCtor = champ.constructor || {};
    const champDrivers = (champ.drivers || []).map(d => `
      <div class="champ-row">
        <div class="champ-pos">P${d.position || '—'}</div>
        <div class="champ-name">${d.name}</div>
        <div class="champ-pts">${fmt(d.points, 0)}</div>
      </div>`).join('');

    panel.innerHTML = `
      <div class="db-band">
        <div style="width:5px;height:52px;background:${tc};border-radius:3px;flex-shrink:0"></div>
        <div style="flex:1">
          <div class="db-tname" style="color:${tc}">${t.name}</div>
          <div style="font-size:11px;color:var(--text2)">
            ${report.race?.name || race.name || 'Latest completed race'} · ${report.race?.date || race.date || 'Date unavailable'} · Round ${report.round || '—'}
          </div>
        </div>
        <div class="db-head-stat">
          <div class="db-head-stat-k">Constructor</div>
          <div class="db-head-stat-v">P${report.constructor?.position || t.position}</div>
        </div>
      </div>
      <div class="db-body">
        <div class="itabs">
          <div class="itab active" onclick="switchItab(this,'s-${idx}')">Overview</div>
          <div class="itab" onclick="switchItab(this,'d-${idx}')">Drivers</div>
          <div class="itab" onclick="switchItab(this,'st-${idx}')">Strategy</div>
          <div class="itab" onclick="switchItab(this,'c-${idx}')">Championship</div>
          <div class="itab" onclick="switchItab(this,'a-${idx}')">Ask Analyst</div>
        </div>
        <div class="itab-pane active" id="s-${idx}">
          <div class="db-kpi-grid">
            <div class="db-kpi-card">
              <div class="db-kpi-k">Executive Summary</div>
              <div class="db-kpi-copy">${report.executive_summary}</div>
            </div>
            <div class="db-kpi-card">
              <div class="db-kpi-k">Race Weather</div>
              <div class="db-kpi-copy">${report.weather?.summary || 'Unavailable'}</div>
            </div>
          </div>
          <div class="db-note-card">
            <div class="sub-sec-t">Team Debrief Summary</div>
            <div class="sum-txt">${report.team_debrief_summary}</div>
          </div>
          <div class="db-two-col">
            <div class="db-note-card">
              <div class="sub-sec-t">Biggest Win</div>
              <div class="db-card-title">${report.biggest_win?.title || 'Unavailable'}</div>
              <div class="db-card-copy">${report.biggest_win?.detail || 'No positive swing identified.'}</div>
            </div>
            <div class="db-note-card">
              <div class="sub-sec-t">Biggest Loss</div>
              <div class="db-card-title">${report.biggest_loss?.title || 'Unavailable'}</div>
              <div class="db-card-copy">${report.biggest_loss?.detail || 'No negative swing identified.'}</div>
            </div>
          </div>
          <div class="db-two-col">
            <div class="db-note-card">
              <div class="sub-sec-t">Overperformed</div>
              <div class="db-pill-row">${overList}</div>
            </div>
            <div class="db-note-card">
              <div class="sub-sec-t">Underperformed</div>
              <div class="db-pill-row">${underList}</div>
            </div>
          </div>
          <div class="sub-sec-t">Incident Explanation</div>
          <div class="iss-list">${incidentList}</div>
        </div>
        <div class="itab-pane" id="d-${idx}">
          <div class="d-cmp">${driverCards || '<div style="color:var(--text3);font-size:12px">No driver review data available.</div>'}</div>
        </div>
        <div class="itab-pane" id="st-${idx}">
          <div class="db-kpi-grid">
            <div class="db-kpi-card">
              <div class="db-kpi-k">Strategy Audit</div>
              <div class="db-card-title">${report.strategy_audit?.label || 'Unavailable'}</div>
              <div class="db-kpi-copy">${report.strategy_audit?.summary || 'No strategy data available.'}</div>
            </div>
            <div class="db-kpi-card">
              <div class="db-kpi-k">Action Items</div>
              <div class="act-list">${actionList}</div>
            </div>
          </div>
          <div class="sub-sec-t">Stint Execution</div>
          <div class="db-note-card">${strategyNotes || '<div style="color:var(--text3);font-size:12px">No stint notes available.</div>'}</div>
        </div>
        <div class="itab-pane" id="c-${idx}">
          <div class="champ-context">
            <div class="champ-narrative">${champ.summary || 'Championship impact unavailable.'}</div>
            <div>
              <div class="champ-half-title">Constructor Position</div>
              <div class="dbc">
                <div class="dbc-r"><span class="dbc-k">Current</span><span class="dbc-v">P${champCtor.position || '—'}</span></div>
                <div class="dbc-r"><span class="dbc-k">Previous</span><span class="dbc-v">${champCtor.previous_position ? `P${champCtor.previous_position}` : 'n/a'}</span></div>
                <div class="dbc-r"><span class="dbc-k">Points</span><span class="dbc-v">${champCtor.points !== undefined ? fmt(champCtor.points, 0) : 'n/a'}</span></div>
                <div class="dbc-r"><span class="dbc-k">Race Score</span><span class="dbc-v">${champCtor.race_points !== undefined ? fmt(champCtor.race_points, 0) : 'n/a'}</span></div>
                <div class="dbc-r"><span class="dbc-k">Gap To Lead</span><span class="dbc-v">${champCtor.gap_to_leader !== undefined ? fmt(champCtor.gap_to_leader, 0) : 'n/a'}</span></div>
              </div>
            </div>
            <div>
              <div class="champ-half-title">Drivers</div>
              ${champDrivers || '<div style="color:var(--text3);font-size:12px">No championship driver data available.</div>'}
            </div>
          </div>
        </div>
        <div class="itab-pane" id="a-${idx}">
          <div class="ask-ai-wrap">
            <div class="ask-ai-row">
              <input class="ask-ai-input" id="debrief-ai-question-${idx}" placeholder="Ask about strategy, driver execution, championship impact, or what the team must fix next.">
              <button class="telem-btn" onclick='askDebriefAI(${idx}, ${JSON.stringify(t.name)}, ${report.round || 'null'})'>Ask</button>
            </div>
            <div class="ask-ai-suggestions">${askChips}</div>
            <div class="ask-ai-answer" id="debrief-ai-answer-${idx}">Ask a focused post-race question to get a grounded answer from the debrief analyst.</div>
          </div>
        </div>
      </div>`;
  } catch (e) {
    panel.innerHTML = errHTML('Could not load debrief: ' + e.message);
  }
}

async function askDebriefAI(idx, teamName, roundNum = null) {
  const input = document.getElementById(`debrief-ai-question-${idx}`);
  const answer = document.getElementById(`debrief-ai-answer-${idx}`);
  const question = input?.value?.trim();
  if (!question || !answer) return;

  answer.textContent = 'Reviewing race execution...';
  try {
    const res = await fetch('/api/debrief/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question, team_name: teamName, round_num: roundNum || null }),
    });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    const data = await res.json();
    answer.textContent = data.answer || 'No answer available.';
  } catch (e) {
    answer.textContent = `Could not get debrief answer: ${e.message}`;
  }
}

function useDebriefQuestion(question, teamName, roundNum = null) {
  const active = document.querySelector('#panel-debrief .itab-pane.active input.ask-ai-input');
  if (!active) return;
  active.value = question;
  const idxMatch = active.id.match(/(\d+)$/);
  if (!idxMatch) return;
  askDebriefAI(Number(idxMatch[1]), teamName, roundNum);
}

// ════════════════════════════════════════════════════
// TELEMETRY
// ════════════════════════════════════════════════════
async function loadTelemetry(driverNum) {
  if (!driverNum) return;
  S.telemDriver = driverNum;
  try {
    const data = await api(`/api/live/car/${driverNum}`);
    S.telemData = data;
    renderTelemetry(data);
  } catch(e) {
    document.getElementById('telem-grid').innerHTML = errHTML('Telemetry unavailable: ' + e.message);
  }
}

function renderTelemetry(samples) {
  if (!samples?.length) {
    document.getElementById('telem-grid').innerHTML = '<div class="empty-state"><strong>No Telemetry</strong>Select a driver during an active session</div>';
    return;
  }
  const latest = samples[samples.length - 1];
  const speed   = latest.speed ?? 0;
  const rpm     = latest.rpm ?? 0;
  const gear    = latest.n_gear ?? 0;
  const throttle= latest.throttle ?? 0;
  const brake   = latest.brake ? 100 : 0;
  const drs     = latest.drs ?? 0;

  document.getElementById('telem-grid').innerHTML = `
    <div class="telem-card">
      <div class="telem-title">Speed</div>
      <div class="telem-value">${Math.round(speed)} <span class="telem-unit">km/h</span></div>
      <div class="telem-sub">Top this lap: ${Math.round(Math.max(...samples.map(s=>s.speed||0)))} km/h</div>
      <div class="chart-h160" style="margin-top:10px"><canvas id="speedChart"></canvas></div>
    </div>
    <div class="telem-card">
      <div class="telem-title">Current Gear</div>
      <div class="gear-display ${gear===0?'gear-neutral':''}">${gear===0?'N':gear}</div>
    </div>
    <div class="telem-card">
      <div class="telem-title">Throttle &amp; Brake</div>
      <div class="pedal-row">
        <div class="pedal-label">Throttle</div>
        <div class="pedal-track"><div class="pedal-fill" style="width:${throttle}%;background:var(--green)"></div></div>
        <div class="pedal-val">${Math.round(throttle)}%</div>
      </div>
      <div class="pedal-row">
        <div class="pedal-label">Brake</div>
        <div class="pedal-track"><div class="pedal-fill" style="width:${brake}%;background:var(--red)"></div></div>
        <div class="pedal-val">${Math.round(brake)}%</div>
      </div>
      <div class="chart-h160" style="margin-top:10px"><canvas id="throttleChart"></canvas></div>
    </div>
    <div class="telem-card">
      <div class="telem-title">DRS Status</div>
      <div class="drs-box ${drs > 8 ? 'drs-open' : 'drs-closed'}">${drs > 8 ? 'DRS OPEN' : 'DRS CLOSED'}</div>
      <div style="margin-top:12px">
        <div class="telem-title">Engine RPM</div>
        <div class="telem-value" style="font-size:22px">${rpm.toLocaleString()} <span class="telem-unit">rpm</span></div>
        <div class="telem-sub">Max this lap: ${Math.max(...samples.map(s=>s.rpm||0)).toLocaleString()} rpm</div>
      </div>
    </div>`;

  // Speed trace chart
  const speedData = samples.map(s => s.speed ?? 0);
  new Chart(document.getElementById('speedChart').getContext('2d'), {
    type: 'line',
    data: {
      labels: samples.map((_, i) => i),
      datasets: [{ data: speedData, borderColor:'#0055BB', backgroundColor:'rgba(0,85,187,.08)', tension:.3, borderWidth:1.5, pointRadius:0, fill:true }]
    },
    options: { ...cOpts('km/h'), plugins:{ legend:{ display:false } } }
  });

  // Throttle trace chart
  const throttleData = samples.map(s => s.throttle ?? 0);
  const brakeData    = samples.map(s => (s.brake ? 100 : 0));
  new Chart(document.getElementById('throttleChart').getContext('2d'), {
    type: 'line',
    data: {
      labels: samples.map((_, i) => i),
      datasets: [
        { label:'Throttle', data: throttleData, borderColor:'var(--green)', backgroundColor:'rgba(26,125,46,.08)', tension:.2, borderWidth:1.5, pointRadius:0, fill:true },
        { label:'Brake',    data: brakeData,    borderColor:'var(--red)',   backgroundColor:'rgba(232,0,45,.08)',   tension:.2, borderWidth:1.5, pointRadius:0, fill:true },
      ]
    },
    options: { ...cOpts('%'), scales:{ ...cOpts('%').scales, y:{ ...cOpts('%').scales.y, min:0, max:100 } } }
  });
}

// ════════════════════════════════════════════════════
// NAVIGATION
// ════════════════════════════════════════════════════
function go(name, tabEl) {
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
  document.getElementById('panel-' + name).classList.add('active');
  if (tabEl) tabEl.classList.add('active');

  if (name === 'strategy'  && !S.loaded.strategy)  loadStrategy();
  if (name === 'predictor' && !S.loaded.predictor) loadPredictor();
  if (name === 'standings' && !S.loaded.standings) loadStandings();
  if (name === 'schedule'  && !S.loaded.schedule)  loadSchedule();
  if (name === 'weekend'  && !S.loaded.weekend)  loadWeekend();
  if (name === 'debrief'   && !S.loaded.debrief)   loadDebrief();
  if (name === 'tower' && S.timing) updateTower(S.timing);
}

// ════════════════════════════════════════════════════
// UTILITIES
// ════════════════════════════════════════════════════
function setText(id, val) { const el = document.getElementById(id); if (el) el.textContent = val; }
function setChip(id, text, cls) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = text;
  el.className = 'chip ' + (cls || '');
}
function fmt(v, decimals = 1) {
  if (v === null || v === undefined || v === '—') return '—';
  if (typeof v === 'number') return v.toFixed(decimals);
  return v;
}
function parseGap(str) {
  if (!str || str === 'LEADER' || str === '—') return 0;
  return parseFloat(str.replace('+','')) || 0;
}
function destroyChart(id) {
  const existing = Chart.getChart(id);
  if (existing) existing.destroy();
}

// ════════════════════════════════════════════════════
// BOOT
// ════════════════════════════════════════════════════
document.addEventListener('DOMContentLoaded', () => {
  startClock();
  initDash();
});

// ════════════════════════════════════════════════════
// RACE WEEKEND PANEL
// ════════════════════════════════════════════════════

const COUNTRY_FLAGS = {
  'australia':'🇦🇺','china':'🇨🇳','japan':'🇯🇵','bahrain':'🇧🇭',
  'saudi arabia':'🇸🇦','usa':'🇺🇸','united states':'🇺🇸','canada':'🇨🇦',
  'monaco':'🇲🇨','spain':'🇪🇸','austria':'🇦🇹','united kingdom':'🇬🇧',
  'uk':'🇬🇧','belgium':'🇧🇪','hungary':'🇭🇺','netherlands':'🇳🇱',
  'italy':'🇮🇹','azerbaijan':'🇦🇿','singapore':'🇸🇬','mexico':'🇲🇽',
  'brazil':'🇧🇷','qatar':'🇶🇦','uae':'🇦🇪','abu dhabi':'🇦🇪',
  'las vegas':'🇺🇸',
};
function raceFlag(country='') { return COUNTRY_FLAGS[country.toLowerCase()] || '🏁'; }

const SESSION_COLOURS = {
  'FP1':'#0055BB','FP2':'#0055BB','FP3':'#0055BB',
  'SQ':'#B04000','SPRINT':'#B04000',
  'Q':'#6A28A8','RACE':'#E8002D',
};
const IMPACT_CLS = {
  'CHAMPIONSHIP':'impact-champ','HIGH':'impact-high','MODERATE':'impact-mod',
  'STRATEGIC':'impact-strat','TOTAL UPSET':'impact-upset',
};

// Live countdown ticker
let _weekendData = null;
let _weekendTimer = null;

async function loadWeekend() {
  if (S.loaded.weekend) { startWeekendTicker(); return; }
  const container = document.getElementById('weekend-outer');
  container.innerHTML = loadingHTML('Loading next race data...');
  try {
    const data = await api('/api/weekend/next');
    _weekendData = data;
    S.loaded.weekend = true;
    renderWeekend(data);
    startWeekendTicker();
  } catch(e) {
    container.innerHTML = `<div class="api-err">Could not load race weekend data: ${e.message}</div>`;
  }
}

function startWeekendTicker() {
  if (_weekendTimer) clearInterval(_weekendTimer);
  _weekendTimer = setInterval(() => {
    if (_weekendData && document.getElementById('panel-weekend')?.classList.contains('active')) {
      // Update countdown values without full re-render
      const sessions = _weekendData.sessions || [];
      sessions.forEach((sess, i) => {
        const el = document.getElementById(`sess-cd-${i}`);
        if (!el || sess.past) return;
        const secs = sess.seconds_until - Math.floor((Date.now()/1000) - _weekendData._loaded_at);
        if (secs > 0) el.textContent = fmtCountdown(secs);
      });
      // Race countdown
      const rcEl = document.getElementById('race-countdown-big');
      if (rcEl && _weekendData.race_seconds_until) {
        const secs = _weekendData.race_seconds_until - Math.floor((Date.now()/1000) - _weekendData._loaded_at);
        if (secs > 0) rcEl.textContent = fmtCountdown(secs);
      }
    }
  }, 1000);
  _weekendData._loaded_at = Date.now()/1000;
}

function fmtCountdown(secs) {
  if (secs <= 0) return 'Now';
  const d = Math.floor(secs / 86400);
  const h = Math.floor((secs % 86400) / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = secs % 60;
  if (d > 0) return `${d}d ${h}h ${m}m`;
  if (h > 0) return `${h}h ${m}m ${s}s`;
  return `${m}m ${s}s`;
}

function renderWeekend(data) {
  const container = document.getElementById('weekend-outer');
  const flag = raceFlag(data.country);
  const isSprint = data.sprint;
  const roundNum = data.round || CALENDAR_2026_ROUNDS[data.race_name] || '';
  const ai = data.ai_analyst || {};
  const preview = ai.preview || {};
  const predictions = ai.win_predictions || [];
  const qualifying = ai.qualifying_importance || {};
  const strategy = ai.strategy_forecast || {};
  const weather = ai.weather_risk || {};
  const upset = ai.upset_pick || {};
  const matchups = ai.matchups || [];
  const titleImpact = ai.championship_impact || {};
  const askExamples = ai.ask_examples || [];

  // Find next session
  const nextSessIdx = data.sessions.findIndex(s => !s.past);

  // Hero section
  const heroHTML = `
    <div class="weekend-hero">
      <div class="hero-left">
        <div class="hero-flag">${flag}</div>
        <div class="hero-round">Round ${roundNum} · 2026 F1 Season</div>
        <div class="hero-title">${data.race_name}</div>
        <div class="hero-circuit">${data.circuit} · ${data.country}</div>
        ${data.note ? `<div class="hero-note">⚠ ${data.note}</div>` : ''}
        <div class="hero-chips">
          ${isSprint ? '<span class="chip ch-orange">Sprint Weekend</span>' : '<span class="chip ch-gray">Standard Weekend</span>'}
          ${data.circuit_info?.type ? `<span class="chip ch-blue">${data.circuit_info.type}</span>` : ''}
          ${data.circuit_info?.overtaking ? `<span class="chip ch-gray">Overtaking: ${data.circuit_info.overtaking}</span>` : ''}
          ${data.circuit_info?.drs_zones ? `<span class="chip ch-blue">${data.circuit_info.drs_zones} DRS zones</span>` : ''}
        </div>
      </div>
      <div class="hero-right">
        <div class="countdown-label">Race starts in</div>
        <div class="countdown-big" id="race-countdown-big">${data.race_countdown_display}</div>
        <div class="countdown-sub">${data.race_date} · Local ${data.utc_offset}</div>
        <div style="margin-top:10px">
          ${data.circuit_info?.length_km ? `<div class="countdown-label">Circuit</div><div style="font-size:13px;font-weight:700;color:var(--text)">${data.circuit_info.length_km} km · ${data.circuit_info.laps||'?'} laps</div>` : ''}
        </div>
      </div>
    </div>`;

  const aiPreviewHTML = preview.narrative ? `
    <div class="card">
      <div class="card-hd">AI Race Preview</div>
      <div class="card-bd">
        <div class="ai-preview-band">
          <div>
            <div class="ai-preview-title">${preview.headline || 'Weekend outlook'}</div>
            <div class="ai-preview-copy">${preview.narrative}</div>
          </div>
          <div class="ai-preview-meta">
            <div class="ai-preview-model">${preview.model || 'F1IQ Analyst'}</div>
            <div class="ai-preview-conf">${Math.round((preview.confidence || 0) * 100)}% confidence</div>
          </div>
        </div>
      </div>
    </div>` : '';

  const top3Predictions = predictions.slice(0, 3);
  const podiumLabels = ['🥇 1st', '🥈 2nd', '🥉 3rd'];
  const top3HTML = top3Predictions.length ? `
    <div class="card">
      <div class="card-hd">Predicted Top 3 (Pre-race)</div>
      <div class="card-bd">
        ${top3Predictions.map((p, i) => `
          <div class="ai-prob-row">
            <div class="ai-prob-head">
              <div class="ai-podium-badge">${podiumLabels[i] || ''}</div>
              <div class="ai-driver-code" style="color:${teamColour(p.team)}">${p.code}</div>
              <div>
                <div class="ai-driver-name">${p.full_name}</div>
                <div class="ai-driver-team">${p.team}</div>
              </div>
              <div class="ai-prob-nums">${p.win_probability}% win</div>
            </div>
            <div class="ai-prob-note">${p.why}</div>
          </div>`).join('')}
      </div>
    </div>` : '';

  const aiForecastHTML = `
    <div class="card">
      <div class="card-hd">AI Forecast Engine</div>
      <div class="card-bd">
        <div class="ai-grid">
          <div class="ai-box">
            <div class="ai-box-title">Win and Podium Odds</div>
            <div class="ai-prob-list">
              ${predictions.map(p => `
                <div class="ai-prob-row">
                  <div class="ai-prob-head">
                    <div class="ai-driver-code" style="color:${teamColour(p.team)}">${p.code}</div>
                    <div>
                      <div class="ai-driver-name">${p.full_name}</div>
                      <div class="ai-driver-team">${p.team}</div>
                    </div>
                    <div class="ai-prob-nums">${p.win_probability}% win · ${p.podium_probability}% podium</div>
                  </div>
                  <div class="ai-prob-track"><div class="ai-prob-fill" style="width:${Math.max(4, p.win_probability)}%;background:${teamColour(p.team)}"></div></div>
                  <div class="ai-prob-note">${p.why}</div>
                </div>`).join('')}
            </div>
          </div>
          <div class="ai-stack">
            <div class="ai-box">
              <div class="ai-box-title">Qualifying Leverage</div>
              <div class="ai-score-row">
                <div class="ai-score">${qualifying.score || '—'}</div>
                <div>
                  <div class="ai-score-label">${qualifying.label || 'Unknown'}</div>
                  <div class="ai-score-copy">${qualifying.reason || 'No qualifying model yet.'}</div>
                </div>
              </div>
            </div>
            <div class="ai-box">
              <div class="ai-box-title">Weather Risk</div>
              <div class="ai-score-row">
                <div class="ai-score">${weather.score || '—'}</div>
                <div>
                  <div class="ai-score-label">${weather.label || 'Stable'}</div>
                  <div class="ai-score-copy">${weather.summary || 'Weather looks stable.'}</div>
                </div>
              </div>
            </div>
            <div class="ai-box">
              <div class="ai-box-title">Strategy Forecast</div>
              <div class="ai-score-copy">${strategy.primary || 'No strategy outlook yet.'}</div>
              <div class="ai-chip-row">
                <span class="chip ch-blue">Tyre stress: ${strategy.tyre_stress || 'n/a'}</span>
                <span class="chip ch-orange">Undercut: ${strategy.undercut_power || 'n/a'}</span>
                <span class="chip ch-green">SC sensitivity: ${strategy.safety_car_sensitivity || 'n/a'}</span>
              </div>
              <div class="ai-bullet-list">
                ${(strategy.alternatives || []).map(item => `<div class="ai-bullet">${item}</div>`).join('')}
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>`;

  // Sessions
  const sessionsHTML = `
    <div class="card">
      <div class="card-hd">📅 Weekend Sessions Schedule</div>
      <div class="card-bd">
        <div class="sessions-grid">
          ${data.sessions.map((sess, i) => {
            const colour = SESSION_COLOURS[sess.type] || '#888';
            const isNext = i === nextSessIdx;
            const cls = sess.past ? 'past' : isNext ? 'next-up' : sess.type === 'SPRINT' ? 'sprint-card' : sess.type === 'RACE' ? 'race-session' : '';
            const cdCls = sess.past ? 'past' : sess.seconds_until < 3600 ? 'soon' : '';
            const dateStr = sess.date
              ? new Date(`${sess.date}T00:00:00`).toLocaleDateString('en-GB', {weekday:'short', day:'numeric', month:'short'})
              : 'Date TBC';
            return `<div class="session-card ${cls}">
              <div class="session-type-bar" style="background:${colour}"></div>
              <div class="session-name">${sess.name}</div>
              <div class="session-date-row">${dateStr} · ${sess.local_time || 'TBC'} local</div>
              ${isNext ? '<div class="chip ch-blue" style="font-size:9px;margin-bottom:4px">NEXT UP</div>' : ''}
              <div class="session-countdown ${cdCls}" id="sess-cd-${i}">${sess.display_countdown}</div>
              ${sess.laps ? `<div class="session-laps">${sess.laps} laps</div>` : ''}
            </div>`;
          }).join('')}
        </div>
      </div>
    </div>`;

  // Sprint explainer (only for sprint weekends)
  let sprintHTML = '';
  if (isSprint && data.sprint_explainer) {
    const se = data.sprint_explainer;
    sprintHTML = `
      <div class="card">
        <div class="card-hd">⚡ Sprint Weekend Format — How It Works</div>
        <div class="card-bd">
          <div class="sprint-explainer">
            <div class="sprint-explainer-title">⚡ Sprint Format This Weekend</div>
            <div class="sprint-sessions">
              ${se.sessions_explained.map(s => `
                <div class="sprint-sess-row">
                  <div class="sprint-sess-name">${s.name}</div>
                  <div class="sprint-sess-desc">${s.desc} <span style="color:var(--text3)">· ${s.duration}</span></div>
                </div>`).join('')}
            </div>
            <div style="margin-top:12px;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:var(--text3);margin-bottom:6px">Key Rules</div>
            <div class="sprint-rules">
              ${se.key_rules.map(r => `<div class="sprint-rule">${r}</div>`).join('')}
            </div>
          </div>
        </div>
      </div>`;
  }

  // Circuit intel
  const intelHTML = data.circuit_info?.characteristics ? `
    <div class="card">
      <div class="card-hd">🗺 Circuit Intelligence</div>
      <div class="card-bd">
        <div style="font-size:12px;color:var(--text2);line-height:1.7;margin-bottom:10px">${data.circuit_info.characteristics}</div>
        ${data.circuit_info.lap_record ? `
          <div style="display:flex;gap:16px;flex-wrap:wrap">
            <div class="stat-card" style="flex:1;min-width:140px;padding:10px">
              <span class="stat-val" style="font-size:18px;font-family:'Courier New',monospace">${data.circuit_info.lap_record}</span>
              <div class="stat-lbl">Lap Record</div>
              <div class="stat-sub sub-neu" style="font-size:10px">${data.circuit_info.lap_record_holder}</div>
            </div>
            ${data.circuit_info.length_km ? `
            <div class="stat-card" style="flex:1;min-width:120px;padding:10px">
              <span class="stat-val" style="font-size:18px">${data.circuit_info.length_km}</span>
              <div class="stat-lbl">km per lap</div>
            </div>` : ''}
            ${data.circuit_info.laps ? `
            <div class="stat-card" style="flex:1;min-width:100px;padding:10px">
              <span class="stat-val" style="font-size:18px">${data.circuit_info.laps}</span>
              <div class="stat-lbl">Race laps</div>
            </div>` : ''}
          </div>` : ''}
      </div>
    </div>` : '';

  const matchupHTML = matchups.length ? `
    <div class="card">
      <div class="card-hd">Key Matchups</div>
      <div class="card-bd">
        <div class="matchup-grid">
          ${matchups.map(m => `
            <div class="matchup-card">
              <div class="matchup-head">
                <div class="matchup-title">${m.title}</div>
                <span class="chip ch-blue">Edge: ${m.edge}</span>
              </div>
              <div class="matchup-angle">${m.angle}</div>
              <div class="matchup-copy">${m.reason}</div>
            </div>`).join('')}
        </div>
      </div>
    </div>` : '';

  const swingHTML = upset.full_name || titleImpact.headline ? `
    <div class="card">
      <div class="card-hd">Upset Pick and Title Swing</div>
      <div class="card-bd">
        <div class="ai-two-col">
          <div class="ai-box">
            <div class="ai-box-title">Dark Horse</div>
            <div class="upset-head">
              <div class="upset-code" style="color:${teamColour(upset.team || '')}">${upset.code || '—'}</div>
              <div>
                <div class="upset-name">${upset.full_name || 'No pick'}</div>
                <div class="upset-team">${upset.team || ''}</div>
              </div>
              <div class="upset-conf">${upset.confidence || 0}%</div>
            </div>
            <div class="ai-score-copy">${upset.reason || 'No upset angle yet.'}</div>
          </div>
          <div class="ai-box">
            <div class="ai-box-title">Championship Impact</div>
            <div class="matchup-angle">${titleImpact.headline || 'No title swing scenario loaded.'}</div>
            <div class="ai-bullet-list">
              ${(titleImpact.scenarios || []).map(item => `<div class="ai-bullet">${item}</div>`).join('')}
            </div>
          </div>
        </div>
      </div>
    </div>` : '';

  // Watch guide
  const watchHTML = `
    <div class="card">
      <div class="card-hd">👀 What to Watch For</div>
      <div class="card-bd">
        <div class="watch-grid">
          ${(data.watch_guide||[]).map(w => {
            const icon = w.match(/^(\p{Emoji})/u)?.[1] || '▶';
            const text = w.replace(/^(\p{Emoji}\uFE0F?\s*)/u, '');
            return `<div class="watch-item"><span class="watch-icon">${icon}</span><span>${text}</span></div>`;
          }).join('')}
        </div>
      </div>
    </div>`;

  // What-if scenarios (2-col grid on desktop)
  const whatifHTML = `
    <div class="card">
      <div class="card-hd">🔮 What-If Scenarios — Race Anticipation</div>
      <div class="card-bd" style="padding:10px">
        <div class="whatif-grid">
          ${(data.what_if_scenarios||[]).map((sc, i) => {
            const pctNum = parseInt(sc.probability);
            const probCls = Number.isFinite(pctNum)
              ? (pctNum >= 50 ? 'high' : pctNum >= 30 ? 'medium' : pctNum >= 15 ? 'low' : 'vlow')
              : 'medium';
            const impactCls = IMPACT_CLS[sc.impact] || 'impact-mod';
            return `<div class="whatif-card">
              <div class="whatif-header" onclick="toggleWhatif(${i})">
                <div class="whatif-title">${sc.title}</div>
                <span class="whatif-prob ${probCls}">${sc.probability}</span>
                <span class="whatif-impact ${impactCls}">${sc.impact}</span>
              </div>
              <div class="whatif-body" id="wi-${i}">
                <div class="whatif-desc">${sc.description}</div>
                <div class="whatif-teams">
                  ${sc.beneficiaries?.length ? `
                  <div class="whatif-team-group">
                    <div class="whatif-team-label">Benefits</div>
                    <div class="whatif-driver-pills">
                      ${sc.beneficiaries.map(d => `<span class="driver-pill pill-gain">${d}</span>`).join('')}
                    </div>
                  </div>` : ''}
                  ${sc.losers?.length ? `
                  <div class="whatif-team-group">
                    <div class="whatif-team-label">Disadvantaged</div>
                    <div class="whatif-driver-pills">
                      ${sc.losers.map(d => `<span class="driver-pill pill-lose">${d}</span>`).join('')}
                    </div>
                  </div>` : ''}
                </div>
              </div>
            </div>`;
          }).join('')}
        </div>
      </div>
    </div>`;

  // Championship context
  const ctx = data.championship_context || {};
  const maxPts = (ctx.top_drivers?.[0]?.points || 1);
  const champHTML = ctx.leader ? `
    <div class="card">
      <div class="card-hd">🏆 Championship Context</div>
      <div class="card-bd">
        <div class="champ-context">
          <div class="champ-narrative">${ctx.narrative}</div>
          <div>
            <div class="champ-half-title">Drivers' Championship</div>
            ${(ctx.top_drivers||[]).map((d,i) => {
              const pc = i===0?'p1':i===1?'p2':i===2?'p3':'';
              const tc = teamColour(d.team);
              const barW = Math.round(d.points/maxPts*100);
              return `<div class="champ-row">
                <div class="champ-pos ${pc}">${d.position}</div>
                <div class="champ-name">${d.full_name}</div>
                <div class="champ-pts">${d.points}
                  <span class="champ-pts-bar-bg"><span class="champ-pts-bar" style="width:${barW}%;background:${tc}"></span></span>
                </div>
              </div>`;
            }).join('')}
          </div>
          <div>
            <div class="champ-half-title">Constructors' Championship</div>
            ${(ctx.top_constructors||[]).map((c,i) => {
              const pc = i===0?'p1':i===1?'p2':'';
              const tc = teamColour(c.name);
              const barW = Math.round(c.points/maxPts*100);
              return `<div class="champ-row">
                <div class="champ-pos ${pc}">${c.position}</div>
                <div class="champ-name" style="display:flex;align-items:center;gap:6px">
                  <span style="width:8px;height:8px;border-radius:2px;background:${tc};display:inline-block;flex-shrink:0"></span>${c.name}
                </div>
                <div class="champ-pts">${c.points}
                  <span class="champ-pts-bar-bg"><span class="champ-pts-bar" style="width:${barW}%;background:${tc}"></span></span>
                </div>
              </div>`;
            }).join('')}
          </div>
        </div>
      </div>
    </div>` : '';

  const askHTML = `
    <div class="card">
      <div class="card-hd">Ask AI Analyst</div>
      <div class="card-bd">
        <div class="ask-ai-wrap">
          <div class="ask-ai-row">
            <input id="weekend-ai-question" class="ask-ai-input" type="text" placeholder="Ask about qualifying, strategy, weather, the upset pick or title math">
            <button class="telem-btn" onclick="askWeekendAI(${roundNum || 'null'})">Ask</button>
          </div>
          <div class="ask-ai-suggestions">
            ${askExamples.map(q => `<button class="ask-chip" onclick='useWeekendQuestion(${JSON.stringify(q)}, ${roundNum || 'null'})'>${q}</button>`).join('')}
          </div>
          <div class="ask-ai-answer" id="weekend-ai-answer">Ask a focused pre-race question to get a grounded answer from the weekend analyst.</div>
        </div>
      </div>
    </div>`;

  container.innerHTML = heroHTML + `<div class="weekend-outer" id="weekend-outer" style="padding:0">` +
    aiPreviewHTML + top3HTML + aiForecastHTML + sessionsHTML + sprintHTML + intelHTML + matchupHTML + watchHTML + swingHTML + whatifHTML + champHTML + askHTML + `</div>`;
}

function toggleWhatif(i) {
  const el = document.getElementById(`wi-${i}`);
  if (el) el.classList.toggle('open');
}

async function askWeekendAI(roundNum = null) {
  const input = document.getElementById('weekend-ai-question');
  const answer = document.getElementById('weekend-ai-answer');
  const question = input?.value?.trim();
  if (!question || !answer) return;

  answer.textContent = 'Thinking through the race context...';
  try {
    const res = await fetch('/api/weekend/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question, round_num: roundNum || null }),
    });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    const data = await res.json();
    answer.textContent = data.answer || 'No answer available.';
  } catch (e) {
    answer.textContent = `Could not get analyst answer: ${e.message}`;
  }
}

function useWeekendQuestion(question, roundNum = null) {
  const input = document.getElementById('weekend-ai-question');
  if (!input) return;
  input.value = question;
  askWeekendAI(roundNum);
}

// Round number lookup (cached from schedule)
const CALENDAR_2026_ROUNDS = {};

// Extend go() to handle weekend panel
const _origGo = window.go || go;
