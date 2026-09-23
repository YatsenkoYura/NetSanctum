(() => {
    if (window.mikuAssistant) return;

    const drawer = document.getElementById('miku-drawer');
    const launcher = document.getElementById('miku-launcher');
    const closeButton = document.getElementById('miku-close');
    const form = document.getElementById('miku-form');
    const input = document.getElementById('miku-input');
    const output = document.getElementById('miku-output');
    const status = document.getElementById('miku-status');
    const micButton = document.getElementById('miku-mic');
    const wakeButton = document.getElementById('miku-wake');
    const voiceButton = document.getElementById('miku-voice');
    if (!drawer || !form) return;

    let socket;
    let reconnectTimer;
    let recognition;
    let recorder;
    let recorderStream;
    let recording = false;
    let recordingMode;
    let discardRecording = false;
    let wakeEnabled = false;
    let voiceEnabled = false;
    let runtime = {llm: false, stt: false, tts: false};
    let wakeRestartTimer;
    let pageSuspended = false;
    const maxAudioBytes = 4 * 1024 * 1024;
    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    const contextId = sessionStorage.getItem('miku-context-id') || window.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    sessionStorage.setItem('miku-context-id', contextId);

    function setOpen(open) {
        drawer.classList.toggle('hidden', !open);
        launcher.classList.toggle('hidden', open);
        drawer.setAttribute('aria-hidden', String(!open));
        launcher.setAttribute('aria-expanded', String(open));
        if (open) input.focus();
    }

    function line(text, tone = 'normal') {
        const row = document.createElement('p');
        row.className = `font-mono text-xs leading-relaxed ${tone === 'error' ? 'text-red-400' : 'text-zinc-300'}`;
        row.textContent = text;
        output.appendChild(row);
        output.scrollTop = output.scrollHeight;
    }

    function actionButton(label, handler, danger = false) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = `border px-2 py-1 font-mono text-[9px] font-bold uppercase ${danger ? 'border-amber-700 text-amber-400' : 'border-zinc-700 text-teal-400'}`;
        button.textContent = label;
        button.addEventListener('click', handler);
        return button;
    }

    function moduleTarget(item) {
        if (item.open_url) return item.open_url;
        const dashboards = {
            music: '/music/dashboard',
            video_archiver: '/video-archiver/dashboard',
            youtube: '/youtube/dashboard',
            alllib: '/alllib/dashboard',
            vault: '/vault/dashboard',
        };
        return dashboards[item.module_id] || item.resource_url;
    }

    function openReference(item, play = false) {
        const target = moduleTarget(item);
        if (!target) return;
        const url = new URL(target, window.location.href);
        if (play) url.searchParams.set('miku_play', '1');
        const href = url.pathname + url.search;
        if (!window.netSanctumNavigate?.(href)) window.location.assign(url.href);
    }

    async function confirmAction(action) {
        const response = await fetch('/api/miku/actions/confirm', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({confirmation_token: action.confirmation_token}),
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.detail || 'Action failed');
        line(payload.message);
        if (payload.task_id) pollJob(payload.task_id);
    }

    async function pollJob(taskId) {
        try {
            const response = await fetch(`/api/miku/jobs/${encodeURIComponent(taskId)}`);
            if (response.status === 404) return line(`Job ${taskId} completed or expired.`);
            if (!response.ok) throw new Error('Job status unavailable');
            const job = await response.json();
            line(`Job ${job.task_id}: ${job.status}${job.progress ? ` · ${job.progress}` : ''}`);
            window.setTimeout(() => pollJob(taskId), 3000);
        } catch (error) {
            line(error.message || 'Job status unavailable', 'error');
        }
    }

    async function speak(text) {
        if (!voiceEnabled || !text) return;
        try {
            if (!runtime.tts) throw new Error('TTS unavailable');
            const response = await fetch('/api/miku/speech', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({text}),
            });
            if (!response.ok) throw new Error('TTS unavailable');
            const url = URL.createObjectURL(await response.blob());
            const audio = new Audio(url);
            audio.addEventListener('ended', () => URL.revokeObjectURL(url), {once: true});
            await audio.play();
        } catch (_error) {
            if ('speechSynthesis' in window) {
                window.speechSynthesis.cancel();
                window.speechSynthesis.speak(new SpeechSynthesisUtterance(text));
            }
        }
    }

    function renderReply(payload) {
        const segments = payload.segments?.length ? payload.segments : [{text: payload.text, speak: true}];
        for (const segment of segments) line(segment.text);
        for (const item of payload.references || []) {
            const row = document.createElement('div');
            row.className = 'flex flex-wrap items-center gap-2 border-l-2 border-zinc-800 pl-3';
            const text = document.createElement('span');
            text.className = 'min-w-0 flex-1 font-mono text-xs text-zinc-300';
            text.textContent = `${item.ref} · ${item.title}`;
            row.appendChild(text);
            if (moduleTarget(item)) row.appendChild(actionButton('Open module', () => openReference(item)));
            if (item.playable) row.appendChild(actionButton('Play there', () => openReference(item, true)));
            output.appendChild(row);
        }
        for (const warning of payload.warnings || []) line(`Warning: ${warning}`, 'error');
        if (payload.pending_action) {
            const row = document.createElement('div');
            row.className = 'flex items-center gap-3 border border-amber-900 bg-amber-950/20 p-3';
            const summary = document.createElement('span');
            summary.className = 'flex-1 font-mono text-[10px] text-amber-200';
            summary.textContent = payload.pending_action.summary;
            row.append(summary, actionButton('Confirm', () => confirmAction(payload.pending_action).catch(error => line(error.message, 'error')), true));
            output.appendChild(row);
        }
        if (payload.client_action && payload.references?.[0]) openReference(payload.references[0], payload.client_action === 'play');
        speak(segments.filter(segment => segment.speak).map(segment => segment.text).join(' '));
        output.scrollTop = output.scrollHeight;
    }

    async function restFallback(message) {
        try {
            const response = await fetch('/api/miku/query', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({message, context_id: contextId}),
            });
            const payload = await response.json();
            if (!response.ok) throw new Error(payload.detail || 'Request failed');
            renderReply(payload);
        } catch (error) {
            line(error.message || 'Request failed', 'error');
        } finally {
            status.textContent = 'Ready';
        }
    }

    function connect() {
        if (pageSuspended || socket?.readyState === WebSocket.OPEN || socket?.readyState === WebSocket.CONNECTING) return;
        const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws';
        const activeSocket = new WebSocket(`${scheme}://${window.location.host}/api/miku/ws?context_id=${encodeURIComponent(contextId)}`);
        socket = activeSocket;
        activeSocket.addEventListener('message', event => {
            const payload = JSON.parse(event.data);
            if (payload.event === 'session.ready') status.textContent = runtime.llm ? 'Realtime · LLM' : 'Realtime';
            if (payload.event === 'turn.started') status.textContent = 'Working';
            if (payload.event === 'turn.result') renderReply(payload.data);
            if (payload.event === 'turn.completed') status.textContent = 'Realtime';
            if (payload.event === 'turn.error') {
                line(payload.data.message || payload.data.code || 'Request failed', 'error');
                status.textContent = 'Realtime';
            }
        });
        activeSocket.addEventListener('close', () => {
            if (socket === activeSocket) socket = undefined;
            if (pageSuspended) return;
            status.textContent = 'REST fallback';
            window.clearTimeout(reconnectTimer);
            reconnectTimer = window.setTimeout(connect, 2000);
        });
    }

    function submitMessage(message) {
        message = message.trim();
        if (!message) return;
        setOpen(true);
        line(`> ${message}`);
        input.value = '';
        status.textContent = 'Working';
        if (socket?.readyState === WebSocket.OPEN) {
            const requestId = window.crypto?.randomUUID?.() || `${Date.now()}:${Math.random()}`;
            socket.send(JSON.stringify({type: 'query', request_id: requestId, message}));
        } else {
            restFallback(message);
        }
    }

    function startRecognition() {
        if (!SpeechRecognition) return false;
        recognition = new SpeechRecognition();
        let receivedSpeech = false;
        let recognitionError;
        recognition.lang = document.documentElement.lang === 'ru' ? 'ru-RU' : 'en-US';
        recognition.interimResults = true;
        recognition.continuous = wakeEnabled;
        recognition.addEventListener('speechstart', () => { status.textContent = 'Hearing'; });
        recognition.addEventListener('result', event => {
            let finalText = '';
            for (let index = event.resultIndex; index < event.results.length; index += 1) {
                if (event.results[index].isFinal) finalText += event.results[index][0].transcript;
            }
            if (!finalText) return;
            receivedSpeech = true;
            const match = wakeEnabled ? finalText.match(/(?:miku|мику)[,\s]*(.+)/i) : [null, finalText];
            if (match) submitMessage(match[1]);
        });
        recognition.addEventListener('error', event => {
            recognitionError = event.error;
            if (event.error === 'aborted') return;
            const messages = {
                'not-allowed': 'Microphone permission was denied.',
                'service-not-allowed': 'Browser speech recognition is blocked.',
                'audio-capture': 'No microphone is available.',
                'network': 'Browser speech recognition service is unavailable.',
                'no-speech': 'No speech was detected.',
            };
            line(messages[event.error] || `Speech recognition failed: ${event.error}`, 'error');
            if (wakeEnabled) {
                wakeEnabled = false;
                wakeButton.textContent = 'Wake: off';
            }
        });
        recognition.addEventListener('end', () => {
            recording = false;
            recordingMode = undefined;
            micButton.textContent = 'Push to talk';
            window.clearTimeout(wakeRestartTimer);
            if (wakeEnabled) wakeRestartTimer = window.setTimeout(startRecognition, 300);
            else if (!receivedSpeech && !recognitionError) line('No speech was detected.', 'error');
            status.textContent = socket?.readyState === WebSocket.OPEN ? 'Realtime' : 'REST fallback';
        });
        recording = true;
        recordingMode = 'recognition';
        status.textContent = 'Listening';
        try {
            recognition.start();
        } catch (error) {
            recording = false;
            recordingMode = undefined;
            recognition = undefined;
            throw error;
        }
        return true;
    }

    async function startRecorder() {
        const stream = await navigator.mediaDevices.getUserMedia({audio: true});
        discardRecording = false;
        recorderStream = stream;
        const chunks = [];
        let recordedBytes = 0;
        const currentRecorder = new MediaRecorder(stream);
        recorder = currentRecorder;
        currentRecorder.addEventListener('dataavailable', event => {
            recordedBytes += event.data.size;
            if (recordedBytes <= maxAudioBytes) chunks.push(event.data);
            else if (currentRecorder.state === 'recording') currentRecorder.stop();
        });
        currentRecorder.addEventListener('stop', async () => {
            stream.getTracks().forEach(track => track.stop());
            if (discardRecording) {
                recording = false;
                recordingMode = undefined;
                recorder = undefined;
                recorderStream = undefined;
                micButton.textContent = 'Push to talk';
                return;
            }
            try {
                if (recordedBytes > maxAudioBytes) throw new Error('Audio utterance is too large.');
                const blob = new Blob(chunks, {type: currentRecorder.mimeType || 'audio/webm'});
                const response = await fetch('/api/miku/transcribe', {method: 'POST', headers: {'Content-Type': blob.type}, body: blob});
                const payload = await response.json();
                if (!response.ok) throw new Error(payload.detail || 'Speech recognition failed');
                submitMessage(payload.text);
            } catch (error) {
                line(error.message || 'Speech recognition failed', 'error');
            } finally {
                recording = false;
                recordingMode = undefined;
                recorder = undefined;
                recorderStream = undefined;
                micButton.textContent = 'Push to talk';
            }
        });
        currentRecorder.start(250);
        window.setTimeout(() => currentRecorder.state === 'recording' && currentRecorder.stop(), 30000);
        recording = true;
        recordingMode = 'recorder';
    }

    launcher.addEventListener('click', () => setOpen(true));
    closeButton.addEventListener('click', () => setOpen(false));
    form.addEventListener('submit', event => { event.preventDefault(); submitMessage(input.value); });
    micButton.addEventListener('click', async () => {
        if (recording) {
            if (recordingMode === 'recognition') recognition?.stop();
            if (recordingMode === 'recorder' && recorder?.state === 'recording') recorder.stop();
            return;
        }
        micButton.textContent = 'Stop';
        try {
            if (runtime.stt) await startRecorder();
            else if (!startRecognition()) await startRecorder();
        } catch (error) {
            line(error.message || 'Microphone is unavailable', 'error');
            micButton.textContent = 'Push to talk';
        }
    });
    wakeButton.addEventListener('click', () => {
        if (!SpeechRecognition) return line('Wake word needs browser speech recognition.', 'error');
        wakeEnabled = !wakeEnabled;
        wakeButton.textContent = `Wake: ${wakeEnabled ? 'miku' : 'off'}`;
        if (recordingMode === 'recognition') recognition.stop();
        else if (wakeEnabled) startRecognition();
    });
    voiceButton.addEventListener('click', () => {
        voiceEnabled = !voiceEnabled;
        voiceButton.textContent = `Voice: ${voiceEnabled ? 'on' : 'off'}`;
        if (!voiceEnabled) window.speechSynthesis?.cancel();
    });

    window.addEventListener('pagehide', () => {
        pageSuspended = true;
        window.clearTimeout(reconnectTimer);
        window.clearTimeout(wakeRestartTimer);
        if (recordingMode === 'recognition') recognition?.abort();
        if (recordingMode === 'recorder') {
            discardRecording = true;
            recorderStream?.getTracks().forEach(track => track.stop());
            if (recorder?.state === 'recording') recorder.stop();
        }
        const activeSocket = socket;
        socket = undefined;
        if (activeSocket?.readyState === WebSocket.OPEN || activeSocket?.readyState === WebSocket.CONNECTING) {
            try {
                activeSocket.close(1000, 'pagehide');
            } catch (_error) {
                // The browser may already have detached a connecting socket for BFCache.
            }
        }
    });
    window.addEventListener('pageshow', () => {
        pageSuspended = false;
        connect();
    });

    window.mikuAssistant = {open: () => setOpen(true), close: () => setOpen(false), submit: submitMessage};
    fetch('/api/miku/runtime').then(response => response.ok ? response.json() : Promise.reject()).then(payload => { runtime = payload; }).catch(() => {});
    connect();
    if (window.location.pathname === '/miku/dashboard') setOpen(true);
})();
