// Web UI for the traffic light. Settings that live in Python come in as window.TL_CONFIG.
const CFG = window.TL_CONFIG;
let currentModeFromServer = 'unknown';
// Tiles are groups; the Pi only knows the flat modes inside them.
const GROUPS = {
    auto: {name: 'Traffic light', modes: ['auto']},
    weather: {name: 'Weather', modes: ['temp', 'rain', 'uv', 'biergarten', 'space']},
    commute: {name: 'Commute', modes: ['s_bahn', 'stau']},
    alerts: {name: 'Alerts', modes: ['emergency', 'sos']},
    party: {name: 'Party', modes: ['party', 'audio_vu']},
    racing: {name: 'Racing', modes: ['race_start', 'racing', 'hotlap']},
    gremio: {name: 'Grêmio', modes: ['gremio']},
    clock: {name: 'Clock', modes: ['clock']},
};
// Sub-mode name and colour ranges (r/y/g/a = red/yellow/green/all), from the Pi's own settings.
const SUBS = {
    biergarten: {name: 'Biergarten', rule: [['g', '18°C+, clear, from 16h'], ['y', '15–18°C or cloudy'], ['r', 'rain, cold, before 16h']]},
    uv: {name: 'UV', rule: [['g', 'UV under 3'], ['y', '3–5'], ['r', '6+ (blinks at 8+)']]},
    space: {name: 'Space', rule: [['g', 'Kp 3 or less'], ['y', 'Kp 4'], ['r', 'Kp 5+ storm (blinks)']]},
    temp: {name: 'Temperature', chip: 'Temp', rule: [['g', `${CFG.temp_green[0]}–${CFG.temp_green[1]}°C`], ['y', `${CFG.temp_yellow[0]}–${CFG.temp_green[0]} or ${CFG.temp_green[1]}–${CFG.temp_yellow[1]}°C`], ['r', `below ${CFG.temp_yellow[0]} or above ${CFG.temp_yellow[1]}°C`]]},
    rain: {name: 'Rain', rule: [['g', `dry next ${CFG.rain_yellow_hours} h`], ['y', `rain within ${CFG.rain_yellow_hours} h`], ['r', `raining or within ${CFG.rain_red_minutes} min`]]},
    s_bahn: {name: 'S-Bahn', rule: [['g', '13+ min'], ['y', '10–12 min (blinks at 9)'], ['r', 'under 9 min']]},
    stau: {name: 'Stau', rule: [['g', 'delay under 20%'], ['y', '20–45%'], ['r', '45%+']]},
    emergency: {name: 'Blinking', rule: [['y', `blinks every ${CFG.blink_seconds} s`]]},
    sos: {name: 'SOS', rule: [['a', 'Morse SOS on all lamps']]},
    racing: {name: 'Revs', rule: [['g', 'shift light'], ['y', 'shift now (green + yellow)'], ['r', 'redline (all)']]},
    race_start: {name: 'Start', rule: [['a', 'F1 start: 3 lights + beeps, then lights out'], ['y', 'yellow flag'], ['g', 'green flag']]},
    hotlap: {name: 'Hotlap', rule: [['g', 'faster than your best lap'], ['y', 'slower'], ['r', 'lap invalid']]},
    party: {name: 'Music', rule: [['a', 'random lamp on every beat'], ['a', 'dark in silence']]},
    audio_vu: {name: 'VU meter', rule: [['g', 'quiet'], ['y', 'loud (green + yellow)'], ['r', 'very loud (all)']]},
};
const GROUP_OF = {};
Object.entries(GROUPS).forEach(([g, d]) => d.modes.forEach(m => GROUP_OF[m] = g));
let lastSub = {};
try { lastSub = JSON.parse(localStorage.getItem('tl_lastSub') || '{}'); } catch (e) {}
function rememberSub(mode) {
    const g = GROUP_OF[mode];
    if (!g || GROUPS[g].modes.length < 2 || lastSub[g] === mode) return;
    lastSub[g] = mode;
    try { localStorage.setItem('tl_lastSub', JSON.stringify(lastSub)); } catch (e) {}
}
function renderGroups(mode) {
    Object.keys(GROUPS).forEach(g => document.getElementById(`group-${g}`).classList.toggle('active', GROUP_OF[mode] === g));
    const g = GROUP_OF[mode], subs = document.getElementById('subs');
    if (!g || GROUPS[g].modes.length < 2) { subs.classList.add('empty'); return; }
    subs.classList.remove('empty');
    document.getElementById('chips').innerHTML = GROUPS[g].modes.map(m =>
        `<button type="button" class="chip${m === mode ? ' active' : ''}" onclick="selectSub('${m}')">${SUBS[m].chip || SUBS[m].name}</button>`).join('');
    document.querySelector('#chips .active')?.scrollIntoView({inline: 'nearest', block: 'nearest'});
    const dot = {r: '#ff2b2b', y: '#ffcb2e', g: '#2bd672'};
    document.getElementById('rule').innerHTML = SUBS[mode].rule.map(([c, txt]) =>
        `<span><b style="background:${c === 'a' ? 'linear-gradient(#ff2b2b,#ffcb2e,#2bd672)' : dot[c]}"></b>${txt}</span>`).join('');
}
const pad2 = n => n.toString().padStart(2, '0');
function clockNextChime() {
    const d = new Date();
    const next = new Date(d); next.setMinutes((Math.floor(d.getMinutes() / 15) + 1) * 15, 0, 0);
    const h = next.getHours(), h12 = h % 12 || 12;
    const n = next.getMinutes() === 0 ? `4 sweeps + ${h12} blink${h12 === 1 ? '' : 's'}` : `${next.getMinutes() / 15} sweep${next.getMinutes() === 15 ? '' : 's'}`;
    const [qs, qe] = CFG.quiet_hours;
    const quiet = qs > qe ? (h >= qs || h < qe) : (h >= qs && h < qe);
    return `Next: ${pad2(h)}:${pad2(next.getMinutes())} · ${quiet ? 'quiet hours' : n}`;
}
function clockTitle() {
    const d = new Date();
    return `CLOCK <em>· ${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}</em>`;
}
// The Pi only pushes status when something changes, so the clock ticks on its own.
setInterval(() => {
    if (currentModeFromServer !== 'clock') return;
    document.querySelector('#modeText strong').innerHTML = clockTitle();
    document.getElementById('info-display').textContent = clockNextChime();
}, 1000);
function modeTitle(mode) {
    if (mode === 'idle') return 'OFF';
    if (mode === 'clock') return clockTitle();
    const g = GROUP_OF[mode];
    if (!g) return mode.replace('_', ' ').toUpperCase();
    const name = GROUPS[g].name.toUpperCase();
    return GROUPS[g].modes.length > 1 ? `${name} <em>· ${SUBS[mode].name}</em>` : name;
}
const LED_SEGMENTS = Array.from({length: 10}, () => {
    const seg = document.createElement('div');
    seg.className = 'led-seg';
    document.getElementById('ledStrip').appendChild(seg);
    return seg;
});
function barHexForColor(color) {
    if (color === 'red' || color === 'all_on') return '#ff1c1c';
    if (color === 'yellow' || color === 'green-yellow') return '#ffc700';
    if (color === 'green') return '#00ff00';
    return null;
}
function computeBarPct(mode, s_bahn_minutes, space_weather, traffic, party, environment, simhub_rev_pct, audio_level) {
    if (mode === 'racing') {
        if (simhub_rev_pct === undefined || simhub_rev_pct === null) return null;
        return Math.max(0, Math.min(1, simhub_rev_pct / 100));
    }
    if (mode === 'uv') {
        const v = (environment || {}).uv_index;
        return (v === undefined || v === null) ? 1 : Math.max(0, Math.min(1, v / 11));
    }
    if (mode === 'space') {
        const v = (space_weather || {}).kp_index;
        return (v === undefined || v === null) ? 1 : Math.max(0, Math.min(1, v / 9));
    }
    if (mode === 'stau') {
        const v = (traffic || {}).avg_delay;
        return (v === undefined || v === null) ? 1 : Math.max(0, Math.min(1, v / 60));
    }
    if (mode === 'audio_vu') {
        return Math.max(0, Math.min(1, audio_level || 0));
    }
    if (mode === 'hotlap') {
        const d = lastStatus.simhub_delta;
        return (lastStatus.simhub_live && typeof d === 'number' && !lastStatus.simhub_lap_invalid) ? Math.min(1, Math.abs(d)) : null;
    }
    if (mode === 'temp') {
        const v = (environment || {}).temp;
        return (typeof v === 'number') ? Math.max(0, Math.min(1, (v + 5) / 40)) : 1;
    }
    if (mode === 'party') {
        const v = (party || {}).level;
        return (typeof v === 'number') ? Math.max(0, Math.min(1, v)) : null;
    }
    if (mode === 's_bahn') {
        if (s_bahn_minutes === -1) return 1;
        return Math.max(0, Math.min(1, 1 - Math.max(0, Math.min(20, s_bahn_minutes)) / 20));
    }
    return null;
}
// Clock: the bar is the bell volume - tap or drag it, the button under it mutes.
const VOL_HEX = '#8fb8ff';
const SPEAKER = '<svg viewBox="0 0 24 24"><path d="M4 9h4l5-4v14l-5-4H4z"/>';
let volDrag = null;  // volume shown while dragging, before it's sent
function showVolume(v) {
    const raw = v * LED_SEGMENTS.length;
    LED_SEGMENTS.forEach((seg, i) => {
        const lit = i < Math.round(raw);
        seg.classList.toggle('lit', lit);
        seg.style.background = lit ? VOL_HEX : '#2a2d33';
        seg.style.opacity = 1;
    });
    const btn = document.getElementById('volMute');
    btn.innerHTML = SPEAKER + (v > 0 ? '<path d="M16 9.5a3.5 3.5 0 0 1 0 5M18.5 7a7 7 0 0 1 0 10"/></svg>' : '<path d="M16 9l5 6M21 9l-5 6"/></svg>');
    btn.setAttribute('aria-label', v > 0 ? 'Mute the bells' : 'Unmute the bells');
}
function volumeAt(ev) {
    const r = document.getElementById('ledStrip').getBoundingClientRect();
    const f = 1 - (ev.clientY - r.top) / r.height;
    if (f <= 0.04) return 0;
    return Math.max(1, Math.min(LED_SEGMENTS.length, Math.ceil(f * LED_SEGMENTS.length))) / LED_SEGMENTS.length;
}
function sendAction(url) { fetch(url).catch(() => setConnected(false)); }
function sendVolume(v) { sendAction(`/?action=set_volume&value=${Math.round(v * 100)}&preview=1`); }
function toggleMute() { sendAction('/?action=set_volume&mute=toggle&preview=1'); }
(() => {
    const strip = document.getElementById('ledStrip');
    strip.addEventListener('pointerdown', ev => {
        if (currentModeFromServer !== 'clock') return;
        strip.setPointerCapture(ev.pointerId); volDrag = volumeAt(ev); showVolume(volDrag);
    });
    strip.addEventListener('pointermove', ev => { if (volDrag !== null) { volDrag = volumeAt(ev); showVolume(volDrag); } });
    const done = () => { if (volDrag !== null) { sendVolume(volDrag); volDrag = null; } };
    strip.addEventListener('pointerup', done); strip.addEventListener('pointercancel', done);
})();
function updateBar(color, mode, s_bahn_minutes, space_weather, traffic, party, environment, simhub_rev_pct, audio_level) {
    const ledStrip = document.getElementById('ledStrip');
    const clock = mode === 'clock';
    ledStrip.classList.toggle('volume', clock);
    document.getElementById('volMute').classList.toggle('visible', clock);
    if (clock) {
        ledStrip.classList.add('visible');
        if (volDrag === null) showVolume(lastStatus.chime_volume || 0);
        return;
    }
    const pct = computeBarPct(mode, s_bahn_minutes, space_weather, traffic, party, environment, simhub_rev_pct, audio_level);
    if (pct === null) {
        ledStrip.classList.remove('visible');
        return;
    }
    ledStrip.classList.add('visible');
    // In racing mode the bar tracks rev_pct continuously even while the main
    // lights are still black (below the first shift point) - fall back to green.
    const hex = barHexForColor(color) || (mode === 'racing' || mode === 'party' || mode === 'audio_vu' ? '#00ff00' : null);
    const raw = hex ? pct * LED_SEGMENTS.length : 0;
    const fullLit = Math.floor(raw);
    const frac = raw - fullLit;
    LED_SEGMENTS.forEach((seg, i) => {
        const level = i < fullLit ? 1 : (i === fullLit ? frac : 0);
        const lit = level > 0.03;
        seg.classList.toggle('lit', lit);
        seg.style.background = lit ? hex : '#2a2d33';
        seg.style.opacity = lit ? (0.35 + 0.65 * level) : 1;
    });
}
function updateVisuals(color, mode, s_bahn_minutes, race_step, space_weather, traffic, party, s_bahn_transit, environment, gremio, simhub_rev_pct, audio_level) {
    updateBar(color, mode, s_bahn_minutes, space_weather, traffic, party, environment, simhub_rev_pct, audio_level);
    if (currentModeFromServer !== mode) {
        rememberSub(mode);
        renderGroups(mode);
        document.querySelector('#modeText strong').innerHTML = modeTitle(mode);
    }
    currentModeFromServer = mode;
    const infoDisplay = document.getElementById('info-display');
    if (mode === 's_bahn') {
        let base;
        if (s_bahn_minutes === -1) { base = 'No S-Bahn data.'; }
        else { base = `Next train in ${s_bahn_minutes} min.`; }
        if (s_bahn_transit && typeof s_bahn_transit.commute_minutes === 'number') {
            const xfer = s_bahn_transit.transfers ? ` (${s_bahn_transit.transfers} transfer${s_bahn_transit.transfers === 1 ? '' : 's'})` : '';
            base += ` By transit: ${s_bahn_transit.commute_minutes} min${xfer}.`;
        }
        infoDisplay.textContent = base;
    }
    else if (mode === 'biergarten') {
        const e = environment || {};
        if (typeof e.temp === 'number' && e.condition) { infoDisplay.textContent = `${e.temp.toFixed(1)}°C, ${e.condition}`; }
        else { infoDisplay.textContent = 'No weather data.'; }
    }
    else if (mode === 'race_start') {
        const steps = ['Grid ready…', 'Lights on · ● ○ ○', 'Lights on · ● ● ○', 'Lights on · ● ● ●', 'Lights out and away we go!'];
        if (race_step < 5) {
            infoDisplay.textContent = steps[race_step] || '';
        } else if (lastStatus.simhub_live) {
            const names = {red: 'Start lights on', yellow: 'Yellow flag', green: 'Green flag', black: 'Track clear'};
            infoDisplay.textContent = names[lastStatus.simhub_flag] || '';
        } else {
            infoDisplay.textContent = 'Lights out! Tap Start to run again.';
        }
    }
    else if (mode === 'hotlap') {
        const d = lastStatus.simhub_delta;
        if (!lastStatus.simhub_live) infoDisplay.textContent = 'Waiting for SimHub…';
        else if (lastStatus.simhub_lap_invalid) infoDisplay.textContent = 'Lap invalid. Next lap counts again.';
        else if (typeof d !== 'number') infoDisplay.textContent = 'Set a lap first: no reference yet.';
        else infoDisplay.textContent = `${d < 0 ? '−' : '+'}${Math.abs(d).toFixed(2)} s vs your best lap`;
    }
    else if (mode === 'racing' && race_step >= 4) {
        infoDisplay.textContent = (typeof simhub_rev_pct === 'number') ? `Revs: ${Math.round(simhub_rev_pct)}%` : 'Waiting for SimHub…';
    }
    else if (mode === 'space') {
        if (space_weather && space_weather.kp_index !== undefined) { infoDisplay.textContent = `Kp-index: ${space_weather.kp_index} (${space_weather.condition})`; }
        else { infoDisplay.textContent = 'No space weather data.'; }
    }
    else if (mode === 'stau') {
        if (traffic && traffic.commute_time) { infoDisplay.textContent = `By car: ${traffic.commute_time}`; }
        else { infoDisplay.textContent = 'No traffic data.'; }
    }
    else if (mode === 'party') {
        const p = party || {};
        const labels = {silence: 'Waiting for music…', ambient: 'Listening for a beat…', chill: 'Chill', groove: 'Groove', hype: 'Hype'};
        let txt = labels[p.vibe] || 'Starting…';
        if (p.bpm) txt = `${Math.round(p.bpm)} BPM · ${txt} · lock ${Math.round((p.confidence || 0) * 100)}%`;
        infoDisplay.textContent = txt;
    }
    else if (mode === 'uv') {
        const e = environment || {};
        const parts = [];
        if (typeof e.uv_index === 'number') {
            let band = 'low';
            if (e.uv_index >= 8) band = 'very high';
            else if (e.uv_index >= 6) band = 'high';
            else if (e.uv_index >= 3) band = 'moderate';
            parts.push(`UV ${e.uv_index.toFixed(1)} (${band})`);
        } else {
            parts.push('UV n/a');
        }
        if (typeof e.temp === 'number' && e.condition) {
            parts.push(`${e.temp.toFixed(0)}°C ${e.condition}`);
        }
        if (e.aqi_label) {
            parts.push(`Air ${e.aqi_label}`);
        }
        if (e.pollen_level && e.pollen_level !== 'unknown') {
            const dom = e.pollen_dominant ? ` (${e.pollen_dominant})` : '';
            parts.push(`Pollen ${e.pollen_level}${dom}`);
        }
        if (typeof e.is_day === 'boolean') {
            parts.push(e.is_day ? 'Day' : 'Night');
        }
        infoDisplay.textContent = parts.join(' · ');
    }
    else if (mode === 'audio_vu') {
        infoDisplay.textContent = `Audio level: ${Math.round(Math.min(Math.max(audio_level || 0, 0), 1) * 100)}%`;
    }
    else if (mode === 'temp') {
        const e = environment || {};
        if (typeof e.temp !== 'number') infoDisplay.textContent = 'No temperature data.';
        else {
            const parts = [`${Math.round(e.temp)}°C now`];
            if (typeof e.feels_like === 'number') parts.push(`feels ${Math.round(e.feels_like)}°C`);
            if (typeof e.temp_max === 'number') parts.push(`high ${Math.round(e.temp_max)}°C`);
            infoDisplay.textContent = parts.join(' · ');
        }
    }
    else if (mode === 'rain') {
        const e = environment || {};
        if (e.rain_now === undefined) infoDisplay.textContent = 'No forecast data.';
        else if (e.rain_now) infoDisplay.textContent = 'Raining now.';
        else if (typeof e.rain_eta_min === 'number') {
            const m = e.rain_eta_min;
            const when = m < 60 ? `${Math.max(m, 5)} min` : `${(m / 60).toFixed(m < 120 ? 1 : 0)} h`;
            infoDisplay.textContent = `Rain likely in ~${when} (${e.rain_prob_max}%).`;
        }
        else infoDisplay.textContent = `Dry for the next ${CFG.rain_yellow_hours} h (max ${e.rain_prob_max || 0}%).`;
    }
    else if (mode === 'clock') {
        infoDisplay.textContent = clockNextChime();
    }
    else if (mode === 'gremio') {
        const g = gremio || {};
        const parts = [];
        const days = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
        const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
        if (g.result && g.score) {
            const labels = {win: 'V', draw: 'E', loss: 'D'};
            const tag = labels[g.result] || g.result;
            const vs = g.home_away === 'home' ? 'vs' : '@';
            let dateStr = '';
            if (g.date) {
                const d2 = new Date(g.date.length > 10 ? g.date : g.date + 'T12:00Z');
                dateStr = ` · ${days[d2.getDay()]} ${d2.getDate()} ${months[d2.getMonth()]}`;
            }
            parts.push(`Last: ${tag} ${g.score} ${vs} ${g.opponent || '?'}${dateStr}`);
        }
        if (g.next_match_date) {
            const d = new Date(g.next_match_date);
            const hh = d.getHours().toString().padStart(2,'0');
            const mm = d.getMinutes().toString().padStart(2,'0');
            const vs2 = g.next_match_home_away === 'home' ? 'vs' : '@';
            parts.push(`Next: ${vs2} ${g.next_match_opponent || '?'} · ${days[d.getDay()]} ${d.getDate()} ${months[d.getMonth()]} ${hh}:${mm}`);
        }
        if (parts.length) {
            infoDisplay.innerHTML = parts.join('<br>');
        } else {
            infoDisplay.textContent = 'No Grêmio data.';
        }
    }
    else { infoDisplay.textContent = ''; }
    const isRedOn = color === 'red' || color === 'red_and_yellow' || color === 'all_on';
    const isYellowOn = color === 'yellow' || color === 'red_and_yellow' || color === 'all_on' || color === 'green-yellow';
    const isGreenOn = color === 'green' || color === 'all_on' || color === 'green-yellow';
    document.getElementById('red').className = 'light' + (isRedOn ? ' red-on' : '');
    document.getElementById('yellow').className = 'light' + (isYellowOn ? ' yellow-on' : '');
    document.getElementById('green').className = 'light' + (isGreenOn ? (mode === 'gremio' ? ' gremio-win-on' : ' green-on') : '');

    const tlBody = document.getElementById('traffic-light');
    const gremioStars = document.getElementById('gremioStars');
    const gremioFaces = [document.getElementById('gremioFaceRed'), document.getElementById('gremioFaceYellow'), document.getElementById('gremioFaceGreen')];
    gremioFaces.forEach(f => f.classList.remove('visible'));
    if (mode === 'gremio') {
        tlBody.classList.add('gremio-theme');
        gremioStars.classList.add('visible');
        const result = (gremio || {}).result;
        const EXPRESSIONS = {
            happy:   {mouth:'M12,24 Q20,34 28,24', angry:false},
            neutral: {mouth:'M12,25 L28,25', angry:false},
            angry:   {mouth:'M12,29 Q20,20 28,29', angry:true},
        };
        const key = result === 'win' ? 'happy' : result === 'loss' ? 'angry' : (result === 'draw' ? 'neutral' : null);
        const activeFace = isGreenOn ? gremioFaces[2] : isYellowOn ? gremioFaces[1] : isRedOn ? gremioFaces[0] : null;
        if (activeFace && key) {
            const e = EXPRESSIONS[key];
            const eyeY = e.angry ? 15 : 14;
            const brows = e.angry
                ? `<line x1="6" y1="7" x2="14" y2="9" stroke="#fff" stroke-width="3.2" stroke-linecap="round"/><line x1="34" y1="7" x2="26" y2="9" stroke="#fff" stroke-width="3.2" stroke-linecap="round"/>`
                : '';
            activeFace.innerHTML = `${brows}<circle cx="9" cy="${eyeY}" r="3.2" fill="#fff"/><circle cx="31" cy="${eyeY}" r="3.2" fill="#fff"/><path d="${e.mouth}" fill="none" stroke="#fff" stroke-width="3.2" stroke-linecap="round"/>`;
            activeFace.classList.add('visible');
        }
    } else {
        tlBody.classList.remove('gremio-theme');
        gremioStars.classList.remove('visible');
    }
}
function handleLightClick(color) { sendAction(`/?action=set_color&color=${color}`); }
let lastModeClickTime = 0;
function handleModeClick(mode) {
    const now = Date.now();
    if (now - lastModeClickTime < 400) return;
    lastModeClickTime = now;
    sendAction(`/?action=set_mode&mode=${mode}`);
}
function handleGroupClick(g) {
    // Tapping the active group turns it off (the Pi toggles a mode set twice);
    // otherwise start the sub-mode used last.
    // A remembered sub-mode that no longer exists falls back to the first one.
    const remembered = GROUPS[g].modes.includes(lastSub[g]) ? lastSub[g] : GROUPS[g].modes[0];
    const mode = GROUP_OF[currentModeFromServer] === g ? currentModeFromServer : remembered;
    handleModeClick(mode);
}
function selectSub(mode) {
    if (mode === 'race_start' && currentModeFromServer === 'race_start') {
        sendAction('/?action=race_start');
    } else if (mode !== currentModeFromServer) {
        handleModeClick(mode);
    }
}
function setConnected(ok) {
    const pill = document.getElementById('connPill');
    const label = document.getElementById('connLabel');
    pill.className = ok ? 'conn-pill live' : 'conn-pill lost';
    label.textContent = ok ? 'Live' : 'Reconnecting…';
}
let lastStatus = {};
function applyStatus(status) {
    lastStatus = status;
    updateVisuals(status.color, status.mode, status.s_bahn_minutes, status.race_step, status.space_weather, status.traffic, status.party, status.s_bahn_transit, status.environment, status.gremio, status.simhub_rev_pct, status.audio_level);
}
let eventSource = null;
function connectEvents() {
    eventSource = new EventSource('/events');
    eventSource.onmessage = (evt) => {
        try { applyStatus(JSON.parse(evt.data)); } catch (e) {}
        setConnected(true);
    };
    eventSource.onopen = () => setConnected(true);
    eventSource.onerror = () => setConnected(false);
}
connectEvents();

