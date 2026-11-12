/* DeepSeek balance & usage chip — Hermes WebUI extension.
 *
 * Chip: balance | today's spend | today's total tokens.
 * Card: two tabs (today's 24 hours / last 30 days) with per-model stacked
 * token-usage bars, plus per-bucket cost and the input / cached-input / output
 * token split in the tooltip.
 *
 * Data comes from this extension's loopback sidecar through the same-origin
 * WebUI proxy; the DeepSeek API key and platform token never reach the browser.
 * Pure DOM injection. All render writes are change-guarded: core re-measures
 * .composer-left on mutations, so pointless writes would churn the composer.
 *
 * IMPORTANT: never toggle visibility from JS based on the composer fit stage —
 * core CSS hides .provider-quota-chip in the burger stage. Let it.
 */
(() => {
  'use strict';

  if (window.__hermesDeepSeekQuotaLoaded) return;
  window.__hermesDeepSeekQuotaLoaded = true;

  const SUMMARY_URL = '/api/extensions/deepseek-quota/sidecar/api/summary';
  const USAGE_URL = (range) =>
    '/api/extensions/deepseek-quota/sidecar/api/usage?range=' + encodeURIComponent(range);
  const POLL_MS = 60 * 1000;
  const USAGE_POLL_MS = 120 * 1000;
  const STALE_S = 5 * 60;
  const CHIP_ID = 'deepseekQuotaChip';
  const MOBILE_ROW_ID = 'deepseekQuotaMobileRow';
  const CARD_ID = 'deepseekQuotaCard';
  const TIP_ID = 'deepseekQuotaTip';
  const PALETTE = ['#4d6bfe', '#14b8a6', '#a855f7', '#f59e0b', '#ec4899', '#64748b'];

  const hoverEnabled = !!(window.matchMedia && window.matchMedia('(hover: hover)').matches);

  let chip = null;
  let labelEl = null;
  let mRow = null;
  let mVal = null;
  let mDot = null;
  let card = null;
  let cardEls = null;
  let cardAnchor = null;
  let panelObserver = null;
  let closeTimer = null;
  let tip = null;
  let lastData = null;
  let lastStale = false;
  let activeRange = 'today';
  const usageCache = {};
  const usageInFlight = {};
  let usageTimer = null;
  let summaryInFlight = false;
  let booted = false;

  // ── helpers ─────────────────────────────────────────────────────────────
  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function setText(node, s) { if (node && node.textContent !== s) node.textContent = s; }
  function setHidden(node, v) { if (node && node.hidden !== v) node.hidden = v; }

  function fmtTokens(value) {
    const v = Number(value);
    if (!Number.isFinite(v) || v <= 0) return '0';
    const trim = (x) => {
      const r = Math.round(x * 10) / 10;
      return r % 1 === 0 ? String(Math.round(r)) : r.toFixed(1);
    };
    if (v >= 1e9) return trim(v / 1e9) + 'B';
    if (v >= 1e6) return trim(v / 1e6) + 'M';
    if (v >= 1e3) return trim(v / 1e3) + 'K';
    return String(Math.round(v));
  }

  function fmtMoney(value, currency) {
    const v = Number(value);
    if (!Number.isFinite(v)) return '—';
    const sym = (currency || 'CNY').toUpperCase() === 'USD' ? '$' : '¥';
    if (v > 0 && v < 0.01) return '<' + sym + '0.01';
    return sym + v.toFixed(2);
  }

  function fmtPct(ratio) {
    const p = Math.round(ratio * 1000) / 10;
    return (p % 1 === 0 ? String(Math.round(p)) : p.toFixed(1)) + '%';
  }

  function timeStr(epoch) {
    const n = Number(epoch);
    if (!Number.isFinite(n)) return '';
    const d = new Date(n * 1000);
    const hh = String(d.getHours()).padStart(2, '0');
    const mm = String(d.getMinutes()).padStart(2, '0');
    return hh + ':' + mm;
  }

  function severityFrom(data) {
    if (!data || data.ok !== true) return 'err';
    const b = data.balance;
    if (!b) return 'err';
    if (Number(b.total) < 0.1) return 'hot';
    if (Number(b.total) < 1) return 'warn';
    return '';
  }

  function chipText(data) {
    if (!data || data.ok !== true) return 'DeepSeek ⚠';
    const parts = [];
    const b = data.balance;
    if (b) parts.push('余额 ' + fmtMoney(b.total, b.currency));
    else if (data.balance_error) parts.push('余额 ⚠');
    if (data.usage_ok && data.today) {
      parts.push('今日 ' + fmtMoney(data.today.cost, data.today.currency));
      parts.push(fmtTokens(data.today.tokens) + ' tok');
    } else {
      parts.push('用量未配置');
    }
    return parts.join(' · ');
  }

  // ── tooltip ─────────────────────────────────────────────────────────────
  function ensureTip() {
    if (tip && document.contains(tip)) return tip;
    tip = el('div', 'deepseek-quota-tip');
    tip.id = TIP_ID;
    tip.hidden = true;
    document.body.appendChild(tip);
    return tip;
  }

  function hideTip() { if (tip) tip.hidden = true; }

  function splitRow(label, value) {
    const row = el('div', 'dsq-split-row');
    row.appendChild(el('span', 'dsq-split-k', label));
    row.appendChild(el('b', 'dsq-split-v', value));
    return row;
  }

  function showTip(index, col) {
    const entry = usageCache[activeRange];
    const data = entry && entry.data;
    if (!data || data.ok !== true || !Array.isArray(data.slots)) return;
    const slot = data.slots[index];
    if (!slot) return;
    ensureTip();
    tip.textContent = '';

    const head = el('div', 'dsq-tip-head');
    if (activeRange === 'today') {
      const h = Number(slot.label);
      const next = String((h + 1) % 24).padStart(2, '0');
      head.textContent = String(slot.label).padStart(2, '0') + ':00 – ' + next + ':00';
    } else {
      head.textContent = slot.label;
    }
    tip.appendChild(head);

    const sum = el('div', 'dsq-tip-sum');
    sum.textContent = fmtTokens(slot.total) + ' tok · ' + fmtMoney(slot.cost, data.currency);
    tip.appendChild(sum);

    if (slot.input != null || slot.cache != null || slot.output != null) {
      const input = Number(slot.input) || 0;
      const cache = Number(slot.cache) || 0;
      const output = Number(slot.output) || 0;
      const prompt = input + cache;
      const split = el('div', 'dsq-tip-split');
      split.appendChild(splitRow('输入 (未缓存)', fmtTokens(input)));
      split.appendChild(splitRow('缓存输入',
        fmtTokens(cache) + (prompt > 0 ? ' · ' + fmtPct(cache / prompt) : '')));
      split.appendChild(splitRow('输出', fmtTokens(output)));
      tip.appendChild(split);
    }

    const models = Object.keys(slot.models || {})
      .filter((name) => Number(slot.models[name]) > 0)
      .sort((a, b) => Number(slot.models[b]) - Number(slot.models[a]));
    const order = {};
    (data.models || []).forEach((m, i) => { order[m.name] = i; });
    const maxWidth = Math.min(300, window.innerWidth - 16);
    tip.style.maxWidth = maxWidth + 'px';
    for (const name of models) {
      const row = el('div', 'dsq-tip-row');
      const dot = el('i', 'dsq-dot');
      dot.style.background = PALETTE[(order[name] != null ? order[name] : models.indexOf(name)) % PALETTE.length];
      row.appendChild(dot);
      row.appendChild(el('span', 'dsq-tip-name', name));
      row.appendChild(el('b', 'dsq-tip-val', fmtTokens(slot.models[name])));
      tip.appendChild(row);
    }
    if (!models.length) {
      tip.appendChild(el('div', 'dsq-tip-empty', '本时段无调用'));
    }

    tip.hidden = false;
    const r = col.getBoundingClientRect();
    const tw = tip.offsetWidth;
    const th = tip.offsetHeight;
    let left = r.left + r.width / 2 - tw / 2;
    left = Math.max(8, Math.min(left, window.innerWidth - tw - 8));
    let top = r.top - th - 8;
    if (top < 8) top = r.bottom + 8;
    if (top + th > window.innerHeight - 8) top = Math.max(8, window.innerHeight - th - 8);
    tip.style.left = left + 'px';
    tip.style.top = top + 'px';
  }

  // ── detail card ─────────────────────────────────────────────────────────
  function ensureCard() {
    if (card && document.contains(card)) return card;
    card = el('div', 'deepseek-quota-card');
    card.id = CARD_ID;
    card.setAttribute('role', 'dialog');
    card.setAttribute('aria-label', 'DeepSeek usage');
    card.hidden = true;
    card.addEventListener('mouseenter', () => {
      if (closeTimer) { clearTimeout(closeTimer); closeTimer = null; }
    });
    card.addEventListener('mouseleave', scheduleClose);

    const head = el('div', 'dsq-head');
    const brand = el('div', 'dsq-brand');
    brand.appendChild(el('span', 'dsq-mark', 'DS'));
    brand.appendChild(el('span', 'dsq-brand-text', 'DeepSeek 用量'));
    const updated = el('span', 'dsq-updated', '');
    head.appendChild(brand);
    head.appendChild(updated);
    card.appendChild(head);

    const bal = el('div', 'dsq-balance');
    const balMain = el('div', 'dsq-bal-main');
    balMain.appendChild(el('span', 'dsq-k', '账户余额'));
    const balValue = el('span', 'dsq-bal-value', '—');
    balMain.appendChild(balValue);
    const balSub = el('div', 'dsq-bal-sub', '');
    bal.appendChild(balMain);
    bal.appendChild(balSub);
    card.appendChild(bal);

    const tabs = el('div', 'dsq-tabs');
    const tabToday = el('button', 'dsq-tab is-active', '今日 24 小时');
    tabToday.type = 'button';
    tabToday.dataset.range = 'today';
    const tab30 = el('button', 'dsq-tab', '近 30 天');
    tab30.type = 'button';
    tab30.dataset.range = '30d';
    tabs.appendChild(tabToday);
    tabs.appendChild(tab30);
    card.appendChild(tabs);

    const sum = el('div', 'dsq-sum');
    const sumTok = el('span', 'dsq-sum-tok', '—');
    const sumCost = el('span', 'dsq-sum-cost', '');
    sum.appendChild(sumTok);
    sum.appendChild(sumCost);
    card.appendChild(sum);

    const sumSplit = el('div', 'dsq-sum-split', '');
    card.appendChild(sumSplit);

    const chart = el('div', 'dsq-chart');
    const grid = el('div', 'dsq-grid');
    grid.appendChild(el('i'));
    grid.appendChild(el('i'));
    grid.appendChild(el('i'));
    const bars = el('div', 'dsq-bars');
    chart.appendChild(grid);
    chart.appendChild(bars);
    card.appendChild(chart);

    const xaxis = el('div', 'dsq-xaxis');
    const xa = [el('span'), el('span'), el('span')];
    xa.forEach((s) => xaxis.appendChild(s));
    card.appendChild(xaxis);

    const legend = el('div', 'dsq-legend');
    card.appendChild(legend);

    const note = el('div', 'dsq-note', '');
    card.appendChild(note);

    tabs.addEventListener('click', (e) => {
      const btn = e.target && e.target.closest ? e.target.closest('.dsq-tab') : null;
      if (!btn) return;
      const range = btn.dataset.range === '30d' ? '30d' : 'today';
      selectRange(range);
    });

    document.body.appendChild(card);

    cardEls = { updated, balValue, balSub, tabs, tabToday, tab30, sumTok, sumCost, sumSplit, bars, xa, legend, note };
    return card;
  }

  function positionCard(anchor) {
    const r = anchor.getBoundingClientRect();
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const cw = Math.min(380, vw - 16);
    card.style.maxWidth = cw + 'px';
    const ch = card.offsetHeight;
    const left = Math.min(Math.max(8, r.left), Math.max(8, vw - cw - 8));
    let top;
    if (r.top >= ch + 12) top = r.top - ch - 8;
    else if (vh - r.bottom >= ch + 12) top = r.bottom + 8;
    else top = Math.max(8, Math.min(r.top - ch - 8, vh - ch - 8));
    card.style.left = left + 'px';
    card.style.top = top + 'px';
  }

  function buildLegend(models, currency) {
    const legend = cardEls.legend;
    legend.textContent = '';
    const top = models.slice(0, 4);
    top.forEach((m, i) => {
      const item = el('span', 'dsq-legend-item');
      const dot = el('i', 'dsq-dot');
      dot.style.background = PALETTE[i % PALETTE.length];
      item.appendChild(dot);
      item.appendChild(el('span', 'dsq-legend-name', m.name || 'unknown'));
      item.appendChild(el('span', 'dsq-legend-val', fmtTokens(m.tokens)));
      legend.appendChild(item);
    });
    if (models.length > 4) {
      legend.appendChild(el('span', 'dsq-legend-more', '+' + (models.length - 4)));
    }
    if (!models.length) legend.appendChild(el('span', 'dsq-legend-more', '暂无模型明细'));
  }

  function renderUsageLoading() {
    if (!cardEls) return;
    setText(cardEls.sumTok, '加载中…');
    setText(cardEls.sumCost, '');
    setText(cardEls.sumSplit, '');
    cardEls.bars.textContent = '';
    cardEls.legend.textContent = '';
    cardEls.xa.forEach((s) => setText(s, ''));
    setText(cardEls.note, '正在向平台查询用量…');
    cardEls.note.classList.remove('err');
  }

  function renderUsage(data) {
    if (!cardEls) return;
    hideTip();
    if (!data || data.ok !== true) {
      setText(cardEls.sumTok, '—');
      setText(cardEls.sumCost, '');
      setText(cardEls.sumSplit, '');
      cardEls.bars.textContent = '';
      cardEls.legend.textContent = '';
      cardEls.xa.forEach((s) => setText(s, ''));
      setText(cardEls.note, (data && data.error) ? data.error : '用量数据不可用');
      cardEls.note.classList.add('err');
      return;
    }

    const slots = Array.isArray(data.slots) ? data.slots : [];
    const models = Array.isArray(data.models) ? data.models : [];
    const currency = data.currency || 'CNY';
    const totals = data.totals || { tokens: 0, cost: 0 };
    setText(cardEls.sumTok, '合计 ' + fmtTokens(totals.tokens) + ' tok');
    setText(cardEls.sumCost, fmtMoney(totals.cost, currency));
    if (totals.input != null || totals.cache != null || totals.output != null) {
      const input = Number(totals.input) || 0;
      const cache = Number(totals.cache) || 0;
      const output = Number(totals.output) || 0;
      const prompt = input + cache;
      setText(cardEls.sumSplit,
        '输入 ' + fmtTokens(input) + ' · 缓存输入 ' + fmtTokens(cache) +
        (prompt > 0 ? ' (' + fmtPct(cache / prompt) + ')' : '') +
        ' · 输出 ' + fmtTokens(output));
    } else {
      setText(cardEls.sumSplit, '');
    }
    cardEls.note.classList.remove('err');
    setText(cardEls.note, '更新于 ' + timeStr(data.fetched_at) + ' · 数据源 platform.deepseek.com');

    // model order/color
    const order = {};
    models.forEach((m, i) => { order[m.name] = i; });
    const extra = [];
    slots.forEach((s) => {
      Object.keys(s.models || {}).forEach((name) => {
        if (order[name] == null) { order[name] = models.length + extra.length; extra.push(name); }
      });
    });

    const max = Math.max(1, ...slots.map((s) => Number(s.total) || 0));
    const bars = cardEls.bars;
    bars.textContent = '';
    const frag = document.createDocumentFragment();
    slots.forEach((slot, index) => {
      const col = el('div', 'dsq-col');
      const stack = el('div', 'dsq-stack');
      const total = Number(slot.total) || 0;
      const h = total > 0 ? Math.max(2, Math.round((total / max) * 82)) : 0;
      stack.style.height = h + 'px';
      const names = Object.keys(slot.models || {}).sort((a, b) => (order[a] || 0) - (order[b] || 0));
      for (const name of names) {
        const value = Number(slot.models[name]) || 0;
        if (value <= 0) continue;
        const seg = el('div', 'dsq-seg');
        seg.style.background = PALETTE[(order[name] || 0) % PALETTE.length];
        seg.style.flexGrow = String(Math.max(value, 1));
        stack.appendChild(seg);
      }
      col.appendChild(stack);
      col.setAttribute('aria-label', (activeRange === 'today' ? slot.label + ':00' : slot.label) +
        ' · ' + fmtTokens(total) + ' tok · ' + fmtMoney(slot.cost, currency) +
        (slot.input != null
          ? ' · 输入 ' + fmtTokens(slot.input) + ' / 缓存输入 ' + fmtTokens(slot.cache) +
            ' / 输出 ' + fmtTokens(slot.output)
          : ''));
      col.addEventListener('mouseenter', () => showTip(index, col));
      col.addEventListener('mouseleave', hideTip);
      col.addEventListener('click', (e) => { e.stopPropagation(); showTip(index, col); });
      frag.appendChild(col);
    });
    bars.appendChild(frag);

    const labelOf = (slot) => {
      if (!slot) return '';
      return activeRange === 'today' ? String(slot.label).padStart(2, '0') + ':00' : slot.label;
    };
    setText(cardEls.xa[0], labelOf(slots[0]));
    setText(cardEls.xa[1], labelOf(slots[Math.floor((slots.length - 1) / 2)]));
    setText(cardEls.xa[2], labelOf(slots[slots.length - 1]));

    buildLegend(models.concat(extra.map((name) => ({ name, tokens: 0 }))), currency);
    // Chart content changed the card height after openCard positioned it once.
    if (card && !card.hidden && cardAnchor) positionCard(cardAnchor);
  }

  function selectRange(range, force) {
    activeRange = range === '30d' ? '30d' : 'today';
    if (cardEls) {
      cardEls.tabToday.classList.toggle('is-active', activeRange === 'today');
      cardEls.tab30.classList.toggle('is-active', activeRange === '30d');
    }
    const cached = usageCache[activeRange];
    const fresh = cached && (Date.now() / 1000 - cached.at) < 180;
    if (cached && fresh && !force) renderUsage(cached.data);
    else {
      renderUsageLoading();
      loadUsage(activeRange, force === true);
    }
  }

  function loadUsage(range, force) {
    if (usageInFlight[range]) return;
    usageInFlight[range] = true;
    const url = USAGE_URL(range) + (force ? '&refresh=1' : '');
    fetch(url, { cache: 'no-store' })
      .then((r) => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then((data) => {
        usageCache[range] = { at: Date.now() / 1000, data };
        if (range === activeRange && card && !card.hidden) renderUsage(data);
      })
      .catch((e) => {
        if (range === activeRange && card && !card.hidden) {
          renderUsage({ ok: false, error: String((e && e.message) || e) });
        }
      })
      .finally(() => { usageInFlight[range] = false; });
  }

  function startUsageTimer() {
    stopUsageTimer();
    usageTimer = setInterval(() => {
      if (card && !card.hidden) loadUsage(activeRange, true);
    }, USAGE_POLL_MS);
  }

  function stopUsageTimer() {
    if (usageTimer) { clearInterval(usageTimer); usageTimer = null; }
  }

  function renderSummary(data) {
    const nowS = Date.now() / 1000;
    lastStale = !data || !Number.isFinite(Number(data.fetched_at)) ||
      (nowS - Number(data.fetched_at)) > STALE_S;
    if (data) lastData = data;
    const sev = severityFrom(data);
    const text = chipText(data);

    if (ensureChip()) {
      setHidden(chip, false);
      chip.classList.remove('warn', 'hot', 'err');
      if (sev) chip.classList.add(sev);
      setText(labelEl, (lastStale && data && data.ok ? '?' : '') + text);
    }

    if (ensureMobileRow()) {
      setText(mVal, (lastStale && data && data.ok ? '?' : '') + text);
      if (mDot) {
        mDot.style.background = sev === 'hot' ? '#ef4444' : sev === 'warn' ? '#f59e0b' :
          sev === 'err' ? '#94a3b8' : '#4d6bfe';
      }
    }

    if (cardEls && card && !card.hidden) {
      const b = data && data.balance;
      setText(cardEls.balValue, b ? fmtMoney(b.total, b.currency) : '—');
      setText(cardEls.balSub, b
        ? '赠送 ' + fmtMoney(b.granted, b.currency) + ' · 充值 ' + fmtMoney(b.topped_up, b.currency)
        : (data && (data.balance_error || data.usage_error)) || '');
      setText(cardEls.updated, data && data.fetched_at ? '更新于 ' + timeStr(data.fetched_at) : '');
      if (!data || data.ok !== true) {
        setText(cardEls.note, (data && (data.balance_error || data.usage_error)) || '加载失败');
        cardEls.note.classList.add('err');
      }
      if (cardAnchor) positionCard(cardAnchor);
    }
  }

  function openCard(anchor) {
    ensureCard();
    cardAnchor = anchor;
    card.hidden = false;
    positionCard(anchor);
    renderSummary(lastData);
    selectRange(activeRange, false);
    startUsageTimer();
    loadSummary(true);
  }

  function closeCard() {
    if (closeTimer) { closeTimer = null; }
    if (card) card.hidden = true;
    stopUsageTimer();
    hideTip();
    cardAnchor = null;
  }

  function scheduleClose() {
    if (closeTimer) clearTimeout(closeTimer);
    closeTimer = setTimeout(() => { closeTimer = null; closeCard(); }, 150);
  }

  function toggleCard(anchor) {
    if (card && !card.hidden && cardAnchor === anchor) closeCard();
    else openCard(anchor);
  }

  // ── anchors ─────────────────────────────────────────────────────────────
  function wireAnchor(node) {
    node.setAttribute('aria-label', 'DeepSeek balance and usage');
    if (hoverEnabled) {
      node.addEventListener('mouseenter', () => {
        if (closeTimer) { clearTimeout(closeTimer); closeTimer = null; }
        openCard(node);
      });
      node.addEventListener('mouseleave', scheduleClose);
    }
    node.addEventListener('click', () => {
      if (!hoverEnabled) toggleCard(node);
      else loadSummary(true);
    });
  }

  function ensureChip() {
    if (chip && document.contains(chip)) return true;
    const left = document.querySelector('.composer-footer .composer-left');
    if (!left) return false;
    chip = el('button', 'provider-quota-chip deepseek-quota-chip');
    chip.type = 'button';
    chip.id = CHIP_ID;
    chip.hidden = true;
    const dot = el('span', 'provider-quota-chip-dot');
    dot.setAttribute('aria-hidden', 'true');
    labelEl = el('span', 'provider-quota-chip-label', 'DeepSeek');
    chip.appendChild(dot);
    chip.appendChild(labelEl);
    wireAnchor(chip);
    const anchor = left.querySelector('#providerQuotaChip');
    if (anchor && anchor.parentElement === left) anchor.after(chip);
    else left.appendChild(chip);
    return true;
  }

  function ensureMobileRow() {
    if (mRow && document.contains(mRow)) return true;
    const panel = document.getElementById('composerMobileConfigPanel');
    if (!panel) return false;
    mRow = el('button', 'composer-mobile-config-action');
    mRow.type = 'button';
    mRow.id = MOBILE_ROW_ID;
    wireAnchor(mRow);
    const icon = el('span', 'composer-model-icon');
    icon.setAttribute('aria-hidden', 'true');
    mDot = el('span', 'provider-quota-chip-dot');
    mDot.style.boxShadow = 'none';
    icon.appendChild(mDot);
    const copy = el('span', 'composer-mobile-config-copy');
    copy.appendChild(el('span', 'composer-mobile-config-kicker', 'DeepSeek'));
    mVal = el('span', 'composer-mobile-config-value', '—');
    copy.appendChild(mVal);
    mRow.appendChild(icon);
    mRow.appendChild(copy);
    const anchor = document.getElementById('composerMobileQuotaAction');
    if (anchor && anchor.parentElement === panel) anchor.after(mRow);
    else panel.appendChild(mRow);
    watchMobilePanel(panel);
    return true;
  }

  // Core closes the mobile config panel by removing its .open class (context
  // button, outside tap, or opening another dropdown). Without this the usage
  // card would stay floating after the panel it was opened from disappears.
  function watchMobilePanel(panel) {
    if (panelObserver) panelObserver.disconnect();
    panelObserver = new MutationObserver(() => {
      if (panel.classList.contains('open')) return;
      if (card && !card.hidden && cardAnchor === mRow) closeCard();
    });
    panelObserver.observe(panel, { attributes: true, attributeFilter: ['class'] });
  }

  // ── data ────────────────────────────────────────────────────────────────
  async function loadSummary(force) {
    if (summaryInFlight) return;
    summaryInFlight = true;
    try {
      const url = SUMMARY_URL + (force ? '?refresh=1' : '');
      const r = await fetch(url, { cache: 'no-store' });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      renderSummary(await r.json());
    } catch (e) {
      renderSummary({ ok: false, error: String((e && e.message) || e), fetched_at: Date.now() / 1000 });
    } finally {
      summaryInFlight = false;
    }
  }

  function boot() {
    if (booted) return;
    if (!ensureChip()) return;
    booted = true;
    ensureMobileRow();
    loadSummary(false);
    setInterval(() => loadSummary(false), POLL_MS);
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') loadSummary(false);
    });
    window.addEventListener('scroll', () => { closeCard(); }, { passive: true, capture: true });
    window.addEventListener('resize', () => { closeCard(); }, { passive: true });
  }

  const bootObserver = new MutationObserver(() => {
    if (booted) { bootObserver.disconnect(); return; }
    boot();
    if (booted) bootObserver.disconnect();
  });
  bootObserver.observe(document.body, { childList: true, subtree: true });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
