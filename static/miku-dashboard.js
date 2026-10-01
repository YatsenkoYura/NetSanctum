(() => {
    if (window.mikuDashboardBound) return;
    window.mikuDashboardBound = true;

    const form = document.getElementById('miku-provider-form');
    if (form && !form.dataset.bound) {
        form.dataset.bound = 'true';
        const status = document.getElementById('miku-provider-status');
        const ACTIVE_MODE = ['border-teal-400', 'text-teal-400'];
        const IDLE_MODE = ['border-zinc-800', 'text-zinc-600'];
        const paintModeRadios = () => {
            form.querySelectorAll('[data-mode-group]').forEach(group => {
                const checked = group.querySelector('input[type="radio"]:checked');
                group.querySelectorAll('[data-mode-option]').forEach(option => {
                    const active = checked && option.dataset.modeOption === checked.value;
                    option.classList.remove(...(active ? IDLE_MODE : ACTIVE_MODE));
                    option.classList.add(...(active ? ACTIVE_MODE : IDLE_MODE));
                });
            });
        };
        form.addEventListener('change', event => {
            if (event.target.matches('input[type="radio"][name$=".mode"]')) paintModeRadios();
        });
        paintModeRadios();
        form.addEventListener('submit', async event => {
            event.preventDefault();
            const data = new FormData(form);
            const provider = kind => ({
                url: data.get(`${kind}.url`) || '',
                model: data.get(`${kind}.model`) || '',
                api_key: data.get(`${kind}.api_key`) || '',
                mode: data.get(`${kind}.mode`) || 'api',
            });
            status.textContent = 'Saving...';
            try {
                const response = await fetch('/api/miku/providers', {
                    method: 'PUT',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({llm: provider('llm'), stt: provider('stt'), tts: provider('tts')}),
                });
                const payload = await response.json();
                if (!response.ok) throw new Error(payload.detail || 'Could not save providers');
                for (const kind of ['llm', 'stt', 'tts']) {
                    form.elements[`${kind}.api_key`].value = '';
                    const marker = form.querySelector(`[data-key-state="${kind}"]`);
                    marker.textContent = payload[kind].api_key_set ? 'saved' : 'not set';
                    marker.className = payload[kind].api_key_set ? 'text-teal-500' : 'text-zinc-700';
                    const savedMode = form.querySelector(`input[name="${kind}.mode"][value="${payload[kind].mode}"]`);
                    if (savedMode) savedMode.checked = true;
                    paintModeRadios();
                }
                status.textContent = 'Saved. New requests use these providers immediately.';
            } catch (error) {
                status.textContent = error.message || 'Could not save providers';
            }
        });
    }

    const memoryBody = document.getElementById('miku-memory-body');
    const cascadesBody = document.getElementById('miku-cascades-body');
    if (!memoryBody || !cascadesBody || memoryBody.dataset.bound) return;
    memoryBody.dataset.bound = 'true';
    cascadesBody.dataset.bound = 'true';

    const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, character => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    })[character]);
    const text = (value, fallback) => (value === null || value === undefined || value === '' ? fallback : String(value));

    async function loadMemory() {
        memoryBody.innerHTML = '<p class="bg-zinc-950 p-4 font-mono text-xs text-zinc-600">Loading…</p>';
        try {
            const response = await fetch('/api/miku/memory');
            const payload = await response.json();
            if (!response.ok) throw new Error(payload.detail || 'Could not read memory');
            const items = Array.isArray(payload.items) ? payload.items : [];
            if (!items.length) {
                memoryBody.innerHTML = '<p class="bg-zinc-950 p-4 font-mono text-xs text-zinc-500 md:col-span-2">Nothing remembered yet. MIKU stores facts and episode summaries on its own.</p>';
                return;
            }
            const card = item => {
                const heading = item.key || item.subject || item.summary;
                const body = item.value && item.value.summary ? item.value.summary : (item.summary || '');
                const forget = item.scope === 'profile' && item.key
                    ? `<button type="button" data-forget-scope="profile" data-forget-key="${escapeHtml(item.key)}" class="mt-2 border border-zinc-700 px-2 py-1 font-mono text-[9px] uppercase text-zinc-500 hover:border-rose-400 hover:text-rose-400">Forget</button>`
                    : item.scope !== 'profile' && item.summary
                    ? `<button type="button" data-forget-scope="episodic" data-forget-summary="${escapeHtml(item.summary)}" class="mt-2 border border-zinc-700 px-2 py-1 font-mono text-[9px] uppercase text-zinc-500 hover:border-rose-400 hover:text-rose-400">Forget</button>`
                    : '';
                return `<article class="bg-zinc-950 p-3">
                    <p class="font-mono text-[9px] uppercase text-teal-400">${escapeHtml(item.scope)}</p>
                    <p class="mt-1 break-words text-sm font-bold text-white">${escapeHtml(text(heading, '—'))}</p>
                    <p class="mt-1 break-words text-xs text-zinc-400">${escapeHtml(text(body, ''))}</p>
                    ${forget}
                </article>`;
            };
            const section = (title, list) => `<div class="space-y-px">
                <p class="bg-zinc-950 px-3 py-2 font-mono text-[9px] uppercase text-zinc-500">${escapeHtml(title)} (${list.length})</p>
                ${list.length ? list.map(card).join('') : '<p class="bg-zinc-950 p-3 text-xs text-zinc-600">—</p>'}
            </div>`;
            memoryBody.innerHTML =
                section('Facts', items.filter(item => item.scope === 'profile')) +
                section('Episodes', items.filter(item => item.scope !== 'profile'));
        } catch (error) {
            memoryBody.innerHTML = `<p class="bg-zinc-950 p-4 font-mono text-xs text-rose-400 md:col-span-2">${escapeHtml(error.message || 'Could not read memory')}</p>`;
        }
    }

    async function loadCascades() {
        cascadesBody.innerHTML = '<p class="border border-zinc-800 bg-zinc-950 p-4 font-mono text-xs text-zinc-600">Loading…</p>';
        try {
            const response = await fetch('/api/miku/cascades?limit=10');
            const payload = await response.json();
            if (!response.ok) throw new Error(payload.detail || 'Could not read the log');
            if (!payload.length) {
                cascadesBody.innerHTML = '<p class="border border-zinc-800 bg-zinc-950 p-4 font-mono text-xs text-zinc-500">No turns recorded yet.</p>';
                return;
            }
            cascadesBody.innerHTML = payload.map(cascade => {
                const steps = Array.isArray(cascade.steps) ? cascade.steps : [];
                const undo = cascade.undo || {};
                const reversible = Array.isArray(undo.reversible) ? undo.reversible : [];
                const irreversible = Array.isArray(undo.irreversible) ? undo.irreversible : [];
                const rows = steps.map(step => {
                    const index = reversible.findIndex(item => item.tool === step.tool);
                    const button = index >= 0
                        ? `<button type="button" data-undo-cascade="${cascade.id}" data-undo-step="${index}" class="border border-rose-400 px-2 py-1 font-mono text-[10px] uppercase text-rose-400 hover:bg-rose-400 hover:text-black">Undo</button>`
                        : '';
                    return `<li class="flex items-start justify-between gap-3 border-t border-zinc-800 py-2">
                        <span class="min-w-0">
                            <span class="font-mono text-[10px] text-zinc-500">${escapeHtml(step.tool)} · ${escapeHtml(step.status)}</span>
                            <span class="block break-words text-xs text-zinc-300">${escapeHtml(text(step.summary, ''))}</span>
                        </span>
                        ${button}
                    </li>`;
                }).join('');
                const badge = irreversible.length
                    ? `<span class="font-mono text-[9px] uppercase text-amber-500">not reversible: ${escapeHtml(irreversible.join(', '))}</span>`
                    : '';
                return `<article class="border border-zinc-800 bg-zinc-950 p-4">
                    <div class="flex flex-wrap items-baseline justify-between gap-2">
                        <p class="break-words text-sm font-bold text-white">${escapeHtml(text(cascade.goal, '—'))}</p>
                        <span class="font-mono text-[9px] uppercase ${cascade.status === 'done' ? 'text-teal-400' : 'text-amber-500'}">${escapeHtml(cascade.status)}</span>
                    </div>
                    <p class="mt-1 font-mono text-[9px] text-zinc-600">${escapeHtml(text(cascade.created_at, ''))}</p>
                    <ul class="mt-2">${rows || '<li class="border-t border-zinc-800 py-2 text-xs text-zinc-600">No steps.</li>'}</ul>
                    ${badge}
                </article>`;
            }).join('');
        } catch (error) {
            cascadesBody.innerHTML = `<p class="border border-zinc-800 bg-zinc-950 p-4 font-mono text-xs text-rose-400">${escapeHtml(error.message || 'Could not read the log')}</p>`;
        }
    }

    cascadesBody.addEventListener('click', async event => {
        const button = event.target.closest('[data-undo-cascade]');
        if (!button) return;
        const cascadeId = button.dataset.undoCascade;
        const stepIndex = button.dataset.undoStep;
        button.disabled = true;
        button.textContent = '…';
        try {
            const response = await fetch(`/api/miku/cascades/${cascadeId}/undo/${stepIndex}`, {method: 'POST'});
            const payload = await response.json();
            if (!response.ok) throw new Error(payload.detail || 'Undo failed');
            await loadCascades();
        } catch (error) {
            button.disabled = false;
            button.textContent = 'Undo';
            window.alert(error.message || 'Undo failed');
        }
    });

    document.getElementById('miku-memory-refresh')?.addEventListener('click', loadMemory);
    document.getElementById('miku-cascades-refresh')?.addEventListener('click', loadCascades);
    memoryBody.addEventListener('click', async event => {
        const button = event.target.closest('[data-forget-scope]');
        if (!button) return;
        const params = new URLSearchParams({scope: button.dataset.forgetScope});
        if (button.dataset.forgetKey) params.set('key', button.dataset.forgetKey);
        if (button.dataset.forgetSummary) params.set('summary', button.dataset.forgetSummary);
        button.disabled = true;
        try {
            const response = await fetch(`/api/miku/memory?${params}`, {method: 'DELETE'});
            const payload = await response.json();
            if (!response.ok) throw new Error(payload.detail || 'Could not forget');
            await loadMemory();
        } catch (error) {
            button.disabled = false;
            window.alert(error.message || 'Could not forget');
        }
    });
    loadMemory();
    loadCascades();
})();
