class NetSanctumBrowser extends HTMLElement {
    connectedCallback() {
        if (this.dataset.ready) return;
        this.dataset.ready = 'true';
        this.sessionId = null;
        this.timer = null;
        this.generation = 0;
        this.refreshing = false;
        this.render();
    }

    disconnectedCallback() {
        this.generation += 1;
        if (this.timer) clearInterval(this.timer);
        if (this.sessionId) {
            fetch(`/api/browser-runtime/sessions/${encodeURIComponent(this.sessionId)}`, {
                method: 'DELETE',
                keepalive: true,
            }).catch(() => {});
        }
        if (this.dataset.ready) this.finish();
    }

    render() {
        this.classList.add('block', 'space-y-3');
        const help = document.createElement('p');
        help.className = 'text-xs text-zinc-500';
        help.textContent = this.dataset.help || 'The isolated browser runs only while this window is active.';

        this.status = document.createElement('p');
        this.status.className = 'font-mono text-xs text-zinc-500';

        const actions = document.createElement('div');
        actions.className = 'flex flex-wrap gap-2';
        this.startButton = this.button(this.dataset.startLabel || 'Start browser', 'border-red-600 text-red-400');
        this.saveButton = this.button(this.dataset.saveLabel || 'Save session', 'border-emerald-600 text-emerald-400');
        this.closeButton = this.button(this.dataset.closeLabel || 'Close', 'border-zinc-700 text-zinc-400');
        this.saveButton.classList.add('hidden');
        this.closeButton.classList.add('hidden');
        actions.append(this.startButton, this.saveButton, this.closeButton);

        this.shell = document.createElement('div');
        this.shell.className = 'hidden space-y-3';
        this.frame = document.createElement('img');
        this.frame.alt = 'Isolated browser';
        this.frame.draggable = false;
        this.frame.className = 'aspect-[8/5] w-full cursor-crosshair border border-zinc-700 bg-black object-contain';
        const form = document.createElement('form');
        form.className = 'flex flex-wrap gap-2';
        this.textInput = document.createElement('input');
        this.textInput.type = 'password';
        this.textInput.autocomplete = 'off';
        this.textInput.placeholder = this.dataset.inputPlaceholder || 'Text for the selected browser field';
        this.textInput.className = 'min-w-0 flex-1 border border-zinc-800 bg-black px-3 py-2 text-sm text-white outline-none focus:border-red-500';
        const send = this.button(this.dataset.sendLabel || 'Send text', 'border-zinc-700 text-zinc-300');
        send.type = 'submit';
        form.append(this.textInput, send);
        for (const key of ['Tab', 'Enter', 'Backspace']) {
            const keyButton = this.button(key, 'border-zinc-800 text-zinc-400');
            keyButton.addEventListener('click', () => {
                this.command('key', {key}).then(() => this.refresh()).catch(error => this.showError(error));
            });
            form.append(keyButton);
        }
        this.shell.append(this.frame, form);
        this.replaceChildren(help, actions, this.status, this.shell);

        this.startButton.addEventListener('click', () => this.start());
        this.saveButton.addEventListener('click', () => this.save().catch(error => this.showError(error)));
        this.closeButton.addEventListener('click', () => this.close().catch(error => this.showError(error)));
        this.frame.addEventListener('click', event => this.clickFrame(event).catch(error => this.showError(error)));
        this.frame.addEventListener('wheel', event => {
            event.preventDefault();
            this.command('scroll', {delta_y: event.deltaY}).catch(error => this.showError(error));
        }, {passive: false});
        form.addEventListener('submit', async event => {
            event.preventDefault();
            if (!this.textInput.value) return;
            try {
                await this.command('type', {text: this.textInput.value});
                this.textInput.value = '';
                await this.refresh();
            } catch (error) {
                this.showError(error);
            }
        });
    }

    button(label, classes) {
        const button = document.createElement('button');
        button.type = 'button';
        button.textContent = label;
        button.className = `border px-4 py-2 font-mono text-[10px] font-bold uppercase ${classes}`;
        return button;
    }

    async start() {
        const generation = ++this.generation;
        if (this.timer) clearInterval(this.timer);
        this.status.textContent = 'Starting isolated Chromium...';
        this.startButton.disabled = true;
        try {
            const response = await fetch('/api/browser-runtime/sessions', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    policy_id: this.dataset.policyId,
                    locale: this.dataset.locale || 'en-US',
                    restore_snapshot: this.dataset.restoreSnapshot !== 'false',
                    mode: 'interactive',
                }),
            });
            const payload = await response.json();
            if (!this.isConnected || generation !== this.generation) {
                if (response.ok && payload.session_id) {
                    fetch(`/api/browser-runtime/sessions/${encodeURIComponent(payload.session_id)}`, {method: 'DELETE'}).catch(() => {});
                }
                return;
            }
            if (!response.ok) return this.showError(new Error(payload.detail || 'Could not start browser'));
            this.sessionId = payload.session_id;
            this.shell.classList.remove('hidden');
            this.closeButton.classList.remove('hidden');
            await this.refresh();
            if (!this.isConnected || generation !== this.generation || !this.sessionId) return;
            this.timer = setInterval(() => this.refresh(), 900);
            this.dispatchEvent(new CustomEvent('netsanctum:browser-started', {bubbles: true, detail: payload}));
        } catch (error) {
            if (this.isConnected && generation === this.generation) this.showError(error);
        } finally {
            if (this.isConnected && generation === this.generation) this.startButton.disabled = false;
        }
    }

    async refresh() {
        if (!this.sessionId || this.refreshing) return;
        this.refreshing = true;
        try {
            const sessionId = this.sessionId;
            this.frame.src = `/api/browser-runtime/sessions/${encodeURIComponent(sessionId)}/frame?t=${Date.now()}`;
            const response = await fetch(`/api/browser-runtime/sessions/${encodeURIComponent(sessionId)}`);
            const payload = await response.json();
            if (sessionId !== this.sessionId) return;
            if (!response.ok) return this.endWithError(payload.detail || 'Browser session ended');
            this.status.textContent = payload.title || payload.url;
            this.saveButton.classList.toggle('hidden', !payload.can_snapshot);
        } catch (error) {
            this.endWithError(error.message || 'Browser session ended');
        } finally {
            this.refreshing = false;
        }
    }

    async command(path, body) {
        if (!this.sessionId) throw new Error('Browser session is not active');
        const response = await fetch(`/api/browser-runtime/sessions/${encodeURIComponent(this.sessionId)}/${path}`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body),
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.detail || 'Browser command failed');
        return payload;
    }

    async clickFrame(event) {
        if (!this.sessionId) return;
        const rect = this.frame.getBoundingClientRect();
        await this.command('click', {
            x: (event.clientX - rect.left) * 1280 / rect.width,
            y: (event.clientY - rect.top) * 800 / rect.height,
        });
        this.textInput.focus();
    }

    async save() {
        const payload = await this.command('snapshot', {});
        this.dispatchEvent(new CustomEvent('netsanctum:browser-snapshot-saved', {
            bubbles: true,
            detail: payload,
        }));
        await this.close();
    }

    async close() {
        this.generation += 1;
        try {
            if (this.sessionId) {
                const response = await fetch(`/api/browser-runtime/sessions/${encodeURIComponent(this.sessionId)}`, {method: 'DELETE'});
                if (!response.ok) {
                    const payload = await response.json();
                    throw new Error(payload.detail || 'Could not close browser session');
                }
            }
            this.finish();
        } catch (error) {
            this.showError(error);
            throw error;
        } finally {
            if (!this.sessionId) this.finish();
        }
    }

    finish() {
        if (this.timer) clearInterval(this.timer);
        this.timer = null;
        this.sessionId = null;
        this.shell.classList.add('hidden');
        this.saveButton.classList.add('hidden');
        this.closeButton.classList.add('hidden');
    }

    endWithError(message) {
        this.finish();
        this.status.textContent = message;
        this.status.classList.add('text-red-400');
    }

    showError(error) {
        this.status.textContent = error.message;
        this.status.classList.add('text-red-400');
    }
}

if (!customElements.get('netsanctum-browser')) {
    customElements.define('netsanctum-browser', NetSanctumBrowser);
}
