/* bsp hostd web UI — vanilla JS, no build step. Talks only to the public API. */
(() => {
  'use strict';

  const $ = (s, el = document) => el.querySelector(s);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const lsGet = (k) => { try { return localStorage.getItem(k); } catch { return null; } };
  const lsSet = (k, v) => { try { localStorage.setItem(k, v); } catch { /* private mode */ } };

  const S = {
    board: null, pins: {}, tools: {}, selected: null, ctlKey: null,
    token: lsGet('bsp.token'), evCount: 0, hist: [], histIdx: -1, refreshTimer: null,
  };
  try { S.hist = JSON.parse(lsGet('bsp.hist') || '[]'); } catch { S.hist = []; }

  // ------------------------------------------------------------------ API
  async function api(method, path, body) {
    const h = { Accept: 'application/json' };
    if (body !== undefined) h['Content-Type'] = 'application/json';
    if (S.token) h.Authorization = 'Bearer ' + S.token;
    let r;
    try {
      r = await fetch(path, { method, headers: h, body: body !== undefined ? JSON.stringify(body) : undefined });
    } catch (e) {
      return { ok: false, error: { code: 'network', message: String(e) } };
    }
    try { return await r.json(); } catch { return { ok: false, error: { code: 'http_' + r.status, message: r.statusText } }; }
  }
  const errText = (e) => `${e?.message || 'error'}${e?.hint ? ' — ' + e.hint : ''}`;

  function toast(msg, kind = 'ok', ms = 4000) {
    const t = document.createElement('div');
    t.className = 'toast ' + kind;
    t.textContent = msg;
    $('#toasts').appendChild(t);
    setTimeout(() => t.remove(), ms);
  }

  // ---------------------------------------------------------------- status
  function renderStatus(st) {
    if (!st) return;
    $('#m-host').textContent = `${st.host} @ ${st.hostname}`;
    $('#m-ver').textContent = 'v' + st.version;
    $('#token-btn').hidden = !st.auth;
    const g = st.modules?.gpio_relay;
    if (!g) return;
    const be = g.backend || {};
    const mb = $('#m-backend');
    mb.textContent = `backend ${be.name}${be.degraded ? ' · DEGRADED' : ''}`;
    mb.className = 'chip ' + (be.degraded ? 'bad' : 'good');
    const pm = g.pinmux || {};
    const mp = $('#m-pinmux');
    mp.textContent = `pinmux ${pm.state}`;
    mp.className = 'chip ' + (pm.state === 'ok' ? 'good' : pm.state === 'disabled' ? '' : 'warn');
    mp.title = pm.error || '';
    const ban = $('#banner');
    if (be.degraded) {
      const le = be.last_error || {};
      ban.hidden = false;
      ban.textContent = `Hardware calls timed out (${be.hung_calls}×, last: ${le.op || ''} ${JSON.stringify(le.args || [])}). Kernel GPIO/pinctrl calls are blocked; the board host may need a reboot.`;
    } else {
      ban.hidden = true;
    }
  }
  async function refreshStatus() {
    const r = await api('GET', '/api/v1/status');
    if (r.ok) renderStatus(r.result);
  }

  // --------------------------------------------------------- header map
  function kindClass(p) {
    if (p.kind === 'power') return /5/.test(p.label) ? 'k-power5' : 'k-power3';
    return 'k-' + p.kind;
  }
  function ledOf(p) {
    if (p.error) return ['e', '!', p.error.message];
    const l = p.line;
    if (!l || !l.claimed) return ['u', '·', 'unclaimed'];
    if (l.direction === 'in') return ['z', 'Z', `input, reads ${l.value}`];
    return l.value ? ['h', 'H', 'output high'] : ['l', 'L', 'output low'];
  }

  function renderBoard() {
    const b = S.board;
    $('#board-title').textContent = `${b.name} · ${b.headers.map((h) => h.id).join(', ')}`;
    const root = $('#headers');
    root.innerHTML = '';
    for (const h of b.headers) {
      const pins = b.pins.filter((p) => p.header === h.id);
      const blk = document.createElement('div');
      blk.className = 'header-block';
      blk.innerHTML = `<div class="header-title">${esc(h.id)} · ${esc(h.title)} · ${esc(h.layout)}</div>`;
      const grid = document.createElement('div');
      grid.className = 'pinmap cols-' + h.cols;
      if (h.cols === 2) {
        for (let r = 0; r < h.rows; r++) {
          const a = pins.find((p) => p.row === r && p.col === 0);
          const c = pins.find((p) => p.row === r && p.col === 1);
          const edge = (r === 0 ? ' top' : '') + (r === h.rows - 1 ? ' bottom' : '');
          grid.append(labelEl(a, 'left'), pinCell(a, 'first' + edge), pinCell(c, 'last' + edge), labelEl(c, 'right'));
        }
      } else {
        grid.style.gridTemplateColumns = `repeat(${h.cols}, auto auto)`;
        for (let r = 0; r < h.rows; r++) {
          for (let c = 0; c < h.cols; c++) {
            const p = pins.find((x) => x.row === r && x.col === c);
            grid.append(pinCell(p, 'first last'), labelEl(p, 'right'));
          }
        }
      }
      blk.appendChild(grid);
      root.appendChild(blk);
    }
  }
  function pinCell(p, cls) {
    const wrap = document.createElement('div');
    wrap.className = 'connector-col ' + cls;
    if (!p) return wrap;
    const el = document.createElement('div');
    el.className = `pin ${kindClass(p)}${p.number === 1 ? ' square' : ''}`;
    el.dataset.key = p.key;
    el.textContent = p.number;
    el.title = `${p.key} ${p.label}${p.alt ? ' (' + p.alt + ')' : ''}`;
    if (p.kind === 'gpio') el.addEventListener('click', () => select(p.key));
    wrap.appendChild(el);
    return wrap;
  }
  function labelEl(p, side) {
    const el = document.createElement('div');
    el.className = `lab ${side}`;
    if (!p) return el;
    el.dataset.lab = p.key;
    el.dataset.side = side;
    if (p.kind === 'gpio') {
      el.classList.add('gpio');
      el.addEventListener('click', () => select(p.key));
    }
    paintLabel(el, p);
    return el;
  }
  function paintLabel(el, p) {
    if (p.kind !== 'gpio') {
      el.innerHTML = `<span class="sig" style="color:var(--muted)">${esc(p.label)}</span>`;
      return;
    }
    const [lc, lt, ltitle] = ledOf(p);
    const ch = p.channel || {};
    const parts = [
      `<span class="led ${lc}" title="${esc(ltitle)}">${lt}</span>`,
      `<span class="sig">${esc(p.label)}</span>`,
    ];
    if (ch.mode && ch.mode !== 'gpio') {
      parts.push(`<span class="mb ${ch.mode}">${ch.mode[0].toUpperCase()}</span>`);
      parts.push(`<span class="st ${['on', 'pressed'].includes(p.state) ? 'on' : ''}">${esc(p.state)}</span>`);
    }
    const al = p.aliases || [];
    if (al.length) parts.push(`<span class="nm" title="${esc(al.join('\n'))}">${esc(al[0])}${al.length > 1 ? ' +' + (al.length - 1) : ''}</span>`);
    if (p.pinmux?.conflict) parts.push(`<b class="warn-mark" title="pad muxed to ${esc(p.pinmux.mux_owner)} (${esc(p.pinmux.function)})">!</b>`);
    if (p.alt) parts.push(`<span class="alt">${esc(p.alt)}</span>`);
    if (el.dataset.side === 'left') parts.reverse();
    el.innerHTML = parts.join('');
    el.classList.toggle('sel', S.selected === p.key);
  }
  function paintPin(p) {
    const lab = document.querySelector(`[data-lab="${CSS.escape(p.key)}"]`);
    if (lab) paintLabel(lab, p);
    const pin = document.querySelector(`.pin[data-key="${CSS.escape(p.key)}"]`);
    if (pin) {
      pin.classList.toggle('sel', S.selected === p.key);
      pin.classList.toggle('muxed', !!p.pinmux?.conflict);
      pin.classList.toggle('prot', !!p.protected);
      pin.classList.toggle('busy', !!p.busy);
    }
  }

  async function refreshPins() {
    const r = await api('GET', '/api/v1/gpio/list?all=true');
    if (!r.ok) return;
    for (const p of r.result) {
      S.pins[p.key] = p;
      paintPin(p);
    }
    if (S.selected) renderInspector();
  }
  function scheduleRefresh() {
    clearTimeout(S.refreshTimer);
    S.refreshTimer = setTimeout(refreshPins, 80);
  }

  // ------------------------------------------------------------ inspector
  function select(key) {
    const prev = S.selected;
    S.selected = key;
    if (prev && S.pins[prev]) paintPin(S.pins[prev]);
    if (S.pins[key]) paintPin(S.pins[key]);
    S.ctlKey = null; // force controls rebuild
    renderInspector();
  }

  function renderInspector() {
    const p = S.pins[S.selected];
    if (!p) return;
    const ins = $('#inspector');
    const ch = p.channel || {};
    const ctlSig = `${p.key}|${ch.mode}|${(p.aliases || []).join(',')}|${ch.active_low}|${ch.drive}|${ch.initial}|${ch.description}|${!!p.pinmux?.conflict}`;
    if (S.ctlKey !== ctlSig) {
      S.ctlKey = ctlSig;
      ins.innerHTML = `<div id="ins-dyn"></div><div class="ins-body"><div id="ins-ctl"></div><div id="ins-cfg"></div></div><div class="equiv" id="equiv"></div>`;
      buildControls(p);
      buildConfig(p);
      if (S.lastEquiv) showEquiv(S.lastEquiv);
    }
    const [lc, lt, ltitle] = ledOf(p);
    const l = p.line || {};
    const mux = p.pinmux;
    $('#ins-dyn').innerHTML = `
      <div class="ins-head">
        <span class="ins-title">P${p.number} · ${esc(p.label)}</span>
        <span class="mb ${ch.mode}">${esc(ch.mode)}</span>
        ${(p.aliases || []).map((a) => `<span class="nm">${esc(a)}</span>`).join('')}
        <span class="ins-sub">${esc(p.key)}${p.alt ? ' · ' + esc(p.alt) : ''}</span>
      </div>
      <div class="statebox">
        <span class="k">state</span><span class="v big-state ${esc(p.state)}">${esc(p.state)}${p.busy ? ' · ' + esc(p.busy) : ''}</span>
        <span class="k">line</span><span class="v"><span class="led ${lc}">${lt}</span> ${esc(ltitle)}${l.owner ? ' · owner ' + esc(l.owner) : ''}</span>
        ${ch.mode !== 'gpio' ? `<span class="k">polarity</span><span class="v">active_${ch.active_low ? 'low' : 'high'} · ${esc(ch.drive)}</span>` : ''}
        <span class="k">config</span><span class="v">${esc(p.origin)}${ch.initial ? ' · initial=' + esc(ch.initial) : ''}</span>
        <span class="k">pinmux</span><span class="v">${mux ? `${esc(mux.mux_owner || 'unclaimed')}${mux.function ? ' · ' + esc(mux.function) : ''}` : 'n/a'}</span>
        ${p.last_action ? `<span class="k">last</span><span class="v">${esc(p.last_action.action)} @ ${new Date(p.last_action.ts * 1000).toLocaleTimeString()}</span>` : ''}
      </div>
      ${mux?.conflict ? `<div class="alert warn">This pad is muxed to <b>${esc(mux.function || mux.mux_owner)}</b> (${esc(mux.mux_owner)}); GPIO writes will not reach the pin. Tick <i>force</i> to write anyway.</div>` : ''}
      ${p.protected ? `<div class="alert err">This pin is protected in the host configuration and cannot be driven.</div>` : ''}
      ${p.error ? `<div class="alert err">${esc(errText(p.error))}</div>` : ''}
      ${ch.description ? `<div class="ins-sub">${esc(ch.description)}</div>` : ''}`;
  }

  function btn(label, cls, onClick, title) {
    const b = document.createElement('button');
    b.className = 'act ' + (cls || '');
    b.textContent = label;
    if (title) b.title = title;
    b.addEventListener('click', onClick);
    return b;
  }
  function numInput(id, val, min = 1, max = 60000) {
    const i = document.createElement('input');
    Object.assign(i, { type: 'number', id, value: val, min, max, className: 'num' });
    return i;
  }
  function row(...els) {
    const d = document.createElement('div');
    d.className = 'btnrow';
    d.append(...els);
    return d;
  }
  function labelled(text, el) {
    const l = document.createElement('label');
    l.className = 'inline';
    l.append(text, el);
    return l;
  }

  function buildControls(p) {
    const ch = p.channel || {};
    const t = p.target;
    const box = $('#ins-ctl');
    const force = document.createElement('input');
    force.type = 'checkbox';
    force.id = 'force';
    const F = () => ({ force: force.checked || undefined });
    box.innerHTML = '<div class="section-title">Control</div>';

    if (ch.mode === 'gpio') {
      box.append(row(
        btn('HIGH', 'hi', () => run('gpio.set', { target: t, level: 'high', ...F() })),
        btn('LOW', 'lo', () => run('gpio.set', { target: t, level: 'low', ...F() })),
        btn('INPUT', 'z', () => run('gpio.set', { target: t, level: 'input', ...F() }), 'Switch to input (Hi-Z)'),
      ));
      const lvl = document.createElement('select');
      lvl.innerHTML = '<option value="low">low</option><option value="high">high</option>';
      const ms = numInput('pulse-ms', 100);
      box.append(row(
        labelled('pulse', lvl), labelled('ms', ms),
        btn('PULSE', 'primary', () => run('gpio.pulse', { target: t, level: lvl.value, ms: +ms.value, ...F() }), 'Pulse, then restore the previous state'),
      ));
    } else if (ch.mode === 'relay') {
      box.append(row(
        btn('ON', 'hi', () => run('relay.on', { target: t, ...F() })),
        btn('OFF', 'lo', () => run('relay.off', { target: t, ...F() })),
        btn('TOGGLE', '', () => run('relay.toggle', { target: t, ...F() })),
      ));
      const ms = numInput('pulse-ms', 200);
      box.append(row(
        labelled('ms', ms),
        btn('PULSE', 'primary', () => run('relay.pulse', { target: t, ms: +ms.value, ...F() }), 'On for ms, then off'),
      ));
    } else if (ch.mode === 'button') {
      const hold = btn('HOLD', 'hold', () => {}, 'Mouse down = press, mouse up = release');
      let down = false;
      const press = (e) => { e.preventDefault(); down = true; hold.classList.add('down'); run('button.press', { target: t, ...F() }); };
      const release = () => { if (!down) return; down = false; hold.classList.remove('down'); run('button.release', { target: t, ...F() }); };
      hold.addEventListener('pointerdown', press);
      ['pointerup', 'pointerleave', 'pointercancel'].forEach((ev) => hold.addEventListener(ev, release));
      box.append(row(
        btn('PRESS', 'hi', () => run('button.press', { target: t, ...F() }), 'Press and hold'),
        btn('RELEASE', 'lo', () => run('button.release', { target: t, ...F() }), 'Release (open drain = Hi-Z)'),
        hold,
      ));
      const ms = numInput('click-ms', 200);
      box.append(row(
        labelled('ms', ms),
        btn('CLICK', 'primary', () => run('button.click', { target: t, ms: +ms.value, ...F() }), 'Press for ms, then release'),
      ));
    }
    const freeBtn = btn('FREE', 'ghost', () => run('gpio.free', { target: t }), 'Free the GPIO (input + unexport)');
    const extra = [freeBtn];
    if (p.pinmux?.conflict) extra.unshift(labelled('force', force));
    box.append(row(...extra));
  }

  function buildConfig(p) {
    const ch = p.channel || {};
    const t = p.target;
    const box = $('#ins-cfg');
    box.innerHTML = '<div class="section-title">Channel configuration</div>';
    const seg = document.createElement('div');
    seg.className = 'seg';
    for (const m of ['gpio', 'relay', 'button']) {
      const b = document.createElement('button');
      b.textContent = m;
      b.className = m + (ch.mode === m ? ' on' : '');
      b.title = { gpio: 'Raw level: high / low / input', relay: 'Relay: on / off, configurable polarity and drive', button: 'Button: open-drain, press pulls low, release is Hi-Z' }[m];
      b.addEventListener('click', () => { if (ch.mode !== m) run('gpio.config', { target: t, mode: m }); });
      seg.appendChild(b);
    }
    const form = document.createElement('div');
    form.className = 'form';
    const desc = Object.assign(document.createElement('input'), { value: ch.description || '' });
    const initial = document.createElement('select');
    const states = S.board.modes[ch.mode]?.states || [];
    initial.innerHTML = `<option value="">(leave as is)</option>` + states.map((s) => `<option ${ch.initial === s ? 'selected' : ''}>${s}</option>`).join('');
    form.append(lab('mode'), seg);
    let al, drive;
    if (ch.mode !== 'gpio') {
      al = Object.assign(document.createElement('input'), { type: 'checkbox', checked: !!ch.active_low });
      drive = document.createElement('select');
      drive.innerHTML = ['push_pull', 'open_drain'].map((d) => `<option ${ch.drive === d ? 'selected' : ''}>${d}</option>`).join('');
      form.append(lab('active_low'), al, lab('drive'), drive);
      form.insertAdjacentHTML('beforeend', '<span class="hint">open_drain: inactive = Hi-Z input; push_pull: inactive actively drives the opposite level</span>');
    }
    form.append(lab('initial'), initial, lab('description'), desc);
    box.append(form, row(
      btn('APPLY', 'primary', () => {
        const args = { target: t, initial: initial.value, description: desc.value };
        if (al) { args.active_low = al.checked; args.drive = drive.value; }
        run('gpio.config', args);
      }),
      btn('RESET', 'ghost', () => run('gpio.config', { target: t, reset: true }), 'Drop the runtime override and return to the host configuration'),
    ));
    box.appendChild(buildAliases(p));
  }
  function buildAliases(p) {
    const box = document.createElement('div');
    box.innerHTML = '<div class="section-title">Aliases</div>';
    const list = document.createElement('div');
    list.className = 'btnrow';
    for (const a of p.aliases || []) {
      const chip = document.createElement('span');
      chip.className = 'alias-chip';
      chip.innerHTML = `<span>${esc(a)}</span>`;
      const x = btn('×', 'ghost small', () => run('gpio.alias.delete', { name: a }), 'Delete this alias');
      chip.appendChild(x);
      list.appendChild(chip);
    }
    if (!(p.aliases || []).length) list.insertAdjacentHTML('beforeend', '<span class="ins-sub">(none)</span>');
    const inp = Object.assign(document.createElement('input'), { placeholder: 'DUT3/reset', spellcheck: false });
    inp.style.width = '150px';
    const add = () => { const v = inp.value.trim(); if (v) run('gpio.alias.add', { name: v, target: p.target }); };
    inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') add(); });
    box.append(list, row(inp, btn('ADD', 'primary', add, 'Bind an alias to this pin; use it as a target afterwards, e.g. button click DUT3/reset')));
    box.insertAdjacentHTML('beforeend', '<div class="ins-sub">Aliases are registered by the components that own them (e.g. the DUT module); this layer only resolves them. Paths with / can be deleted by prefix.</div>');
    return box;
  }
  function lab(text) {
    const l = document.createElement('label');
    l.textContent = text;
    return l;
  }

  // ------------------------------------------------------- run + equivalents
  async function run(name, args) {
    showEquiv(S.lastEquiv = equiv(name, args));
    const r = await api('POST', '/api/v1/tools/' + name, args);
    if (!r.ok) {
      toast(errText(r.error), 'err', 6000);
      scheduleRefresh();
      return null;
    }
    const res = r.result;
    if (name.startsWith('gpio.alias.')) S.ctlKey = null;
    if (res && res.key) {
      S.pins[res.key] = res;
      paintPin(res);
      if (S.selected === res.key) renderInspector();
    }
    scheduleRefresh();
    return res;
  }
  const dash = (s) => s.replace(/_/g, '-');
  const shq = (v) => (/^[\w@%+=:,./-]+$/.test(String(v)) ? String(v) : `'${String(v).replace(/'/g, "'\\''")}'`);
  function equiv(name, args) {
    const t = S.tools[name];
    if (!t) return null;
    const cli = ['bsp', ...name.split('.')];
    for (const p of t.params) {
      const v = args[p.name];
      if (v === undefined || v === null) continue;
      if (p.positional) cli.push(shq(v));
      else if (p.type === 'boolean') cli.push(v ? `--${dash(p.name)}` : `--no-${dash(p.name)}`);
      else cli.push(`--${dash(p.name)}`, shq(v));
    }
    const body = { ...args };
    let path = t.http.path;
    if (path.includes('{target}')) {
      path = path.replace('{target}', String(args.target).split('/').map(encodeURIComponent).join('/'));
      delete body.target;
    }
    Object.keys(body).forEach((k) => body[k] === undefined && delete body[k]);
    const auth = S.token ? ` -H 'Authorization: Bearer ${S.token}'` : '';
    const curl = `curl -X POST ${location.origin}${path}${auth}` +
      (Object.keys(body).length ? ` -H 'Content-Type: application/json' -d '${JSON.stringify(body)}'` : '');
    return { cli: `BSP_URL=${location.origin} ${cli.join(' ')}`, curl };
  }
  function showEquiv(e) {
    const box = $('#equiv');
    if (!box || !e) return;
    box.innerHTML = `<div class="section-title">Equivalent commands (last action)</div>
      <pre>${esc(e.cli)}<button class="ghost small copy" data-copy="cli">copy</button></pre>
      <pre>${esc(e.curl)}<button class="ghost small copy" data-copy="curl">copy</button></pre>`;
    box.querySelectorAll('.copy').forEach((b) => b.addEventListener('click', () => copyText(e[b.dataset.copy])));
  }
  function copyText(text) {
    // navigator.clipboard needs a secure context; plain http://<lan-ip> falls back to execCommand
    if (window.isSecureContext && navigator.clipboard) {
      navigator.clipboard.writeText(text).then(() => toast('copied'), () => toast('copy failed', 'err'));
      return;
    }
    const ta = Object.assign(document.createElement('textarea'), { value: text });
    ta.style.cssText = 'position:fixed;opacity:0';
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch { ok = false; }
    ta.remove();
    toast(ok ? 'copied' : 'copy failed — select the text manually', ok ? 'ok' : 'err');
  }

  // ---------------------------------------------------------------- events
  function evLine(ev) {
    const d = document.createElement('div');
    d.className = 'ev' + (ev.topic.endsWith('error') ? ' err' : ev.topic.includes('config') ? ' cfg' : '');
    const data = Object.entries(ev.data || {})
      .filter(([, v]) => v !== null && v !== undefined)
      .map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : v}`).join(' ');
    d.innerHTML = `<span class="t">${new Date(ev.ts * 1000).toLocaleTimeString()} #${ev.seq}</span> <span class="tp">${esc(ev.topic)}</span> ${esc(data)}`;
    return d;
  }
  function pushEvent(ev) {
    const box = $('#events');
    box.prepend(evLine(ev));
    while (box.childElementCount > 400) box.lastElementChild.remove();
    $('#ev-count').textContent = ++S.evCount;
  }
  async function connectEvents() {
    const r = await api('GET', '/api/v1/events/recent?limit=50');
    let last = 0;
    if (r.ok) for (const ev of r.result) { pushEvent(ev); last = ev.seq; }
    const es = new EventSource('/api/v1/events' + (last ? `?since=${last}` : ''));
    es.onopen = () => { $('#conn-dot').className = 'dot on'; $('#conn-text').textContent = 'live'; };
    es.onerror = () => { $('#conn-dot').className = 'dot off'; $('#conn-text').textContent = 'reconnecting'; };
    es.onmessage = (m) => {
      const ev = JSON.parse(m.data);
      pushEvent(ev);
      if (ev.topic.startsWith('gpio.')) scheduleRefresh();
      if (ev.topic === 'gpio.error' || ev.topic === 'service.started') refreshStatus();
    };
  }

  // --------------------------------------------------------------- console
  function conWrite(text, cls) {
    const out = $('#console-out');
    const s = document.createElement('span');
    if (cls) s.className = cls;
    s.textContent = text;
    out.appendChild(s);
    out.scrollTop = out.scrollHeight;
  }
  async function conRun(line) {
    if (!line.trim()) return;
    S.hist = [line, ...S.hist.filter((h) => h !== line)].slice(0, 100);
    S.histIdx = -1;
    lsSet('bsp.hist', JSON.stringify(S.hist));
    if (line.trim() === 'clear') { $('#console-out').textContent = ''; return; }
    conWrite(`bsp> ${line}\n`, 'cmd');
    const r = await api('POST', '/api/v1/cli', { line });
    if (r.output !== undefined) conWrite(r.output, r.exit_code ? 'err' : '');
    else conWrite(errText(r.error) + '\n', 'err');
    scheduleRefresh();
  }
  function initConsole() {
    const inp = $('#console-input');
    $('#console-form').addEventListener('submit', (e) => { e.preventDefault(); const v = inp.value; inp.value = ''; conRun(v); });
    inp.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowUp' && S.histIdx < S.hist.length - 1) { inp.value = S.hist[++S.histIdx]; e.preventDefault(); }
      if (e.key === 'ArrowDown') { S.histIdx = Math.max(-1, S.histIdx - 1); inp.value = S.histIdx < 0 ? '' : S.hist[S.histIdx]; e.preventDefault(); }
    });
    conWrite('bsp remote CLI: same syntax as the local `bsp` command. Type help for commands, clear to clear, ↑↓ for history.\n');
  }

  // ------------------------------------------------------------------ tabs
  function initTabs() {
    document.querySelectorAll('.tab').forEach((t) => t.addEventListener('click', () => {
      document.querySelectorAll('.tab').forEach((x) => x.classList.toggle('active', x === t));
      document.querySelectorAll('.tabpane').forEach((p) => { p.hidden = p.id !== 'tab-' + t.dataset.tab; });
      if (t.dataset.tab === 'console') $('#console-input').focus();
    }));
  }
  function renderApiTable() {
    const rows = Object.values(S.tools).map((t) =>
      `<tr><td>${esc(t.tool_name)}</td><td>${esc(t.http.method)} ${esc(t.http.path)}</td><td>${esc(t.cli)}</td><td>${esc(t.summary)}</td></tr>`).join('');
    $('#api-table').innerHTML = `<tr><th>AI tool</th><th>HTTP</th><th>CLI</th><th>Description</th></tr>${rows}`;
  }

  // ------------------------------------------------------------------ boot
  async function boot() {
    initTabs();
    initConsole();
    $('#token-btn').addEventListener('click', () => {
      const v = window.prompt('API token (empty to clear)', S.token || '');
      if (v === null) return;
      S.token = v.trim() || null;
      lsSet('bsp.token', S.token || '');
    });
    const [st, board, tools] = await Promise.all([
      api('GET', '/api/v1/status'), api('GET', '/api/v1/board/info'), api('GET', '/api/v1/tools'),
    ]);
    if (!st.ok) {
      $('#conn-dot').className = 'dot off';
      $('#conn-text').textContent = 'offline';
      return;
    }
    renderStatus(st.result);
    (tools.tools || []).forEach((t) => { S.tools[t.name] = t; });
    renderApiTable();
    if (board.ok) {
      S.board = board.result;
      renderBoard();
      await refreshPins();
      setInterval(() => { if (!document.hidden) refreshPins(); }, 1000);
    } else {
      $('#headers').innerHTML = `<div class="empty">gpio_relay module not loaded: ${esc(errText(board.error))}</div>`;
    }
    setInterval(() => { if (!document.hidden) refreshStatus(); }, 5000);
    connectEvents();
  }
  boot();
})();
