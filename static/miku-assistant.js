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
    let activeRequestId;
    let reconnectTimer;
    let recognition;
    let recorder;
    let recorderStream;
    let recording = false;
    let recordingMode;
    let discardRecording = false;
    // Persisted per browser: voice/wake toggles survive module navigation.
    let wakeEnabled = localStorage.getItem('miku-wake-enabled') === '1';
    let voiceEnabled = localStorage.getItem('miku-voice-enabled') === '1';
    let runtime = {llm: false, stt: false, tts: false, modes: {llm: 'api', stt: 'api', tts: 'api'}};
    // Transcript cache per tab session: re-rendered on every module page so
    // the chat does not reset when MIKU sends the user to another module.
    const transcriptKey = `miku-transcript:${contextIdSafe()}`;
    const TRANSCRIPT_LIMIT = 30;
    let transcriptRestored = false;

    function contextIdSafe() {
        try {
            return sessionStorage.getItem('miku-context-id') || 'default';
        } catch (_error) {
            return 'default';
        }
    }

    function loadTranscript() {
        try {
            const raw = sessionStorage.getItem(transcriptKey);
            if (!raw) return [];
            const items = JSON.parse(raw);
            return Array.isArray(items) ? items.slice(-TRANSCRIPT_LIMIT) : [];
        } catch (_error) {
            return [];
        }
    }

    function saveTranscript(items) {
        try {
            sessionStorage.setItem(transcriptKey, JSON.stringify(items.slice(-TRANSCRIPT_LIMIT)));
        } catch (_error) {
            // Storage full or unavailable: chat still works, just not restored.
        }
    }

    function sttMode() {
        return (runtime.modes && runtime.modes.stt) || 'api';
    }

    function ttsMode() {
        return (runtime.modes && runtime.modes.tts) || 'api';
    }

    async function speakWithBrowserVoice(text) {
        await new Promise(resolve => {
            const utterance = new SpeechSynthesisUtterance(text);
            utterance.addEventListener('end', resolve, {once: true});
            utterance.addEventListener('error', resolve, {once: true});
            window.speechSynthesis.cancel();
            window.speechSynthesis.speak(utterance);
        });
    }
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

    let transcript = loadTranscript();
    let restoringTranscript = false;

    function persistTranscript() {
        if (!restoringTranscript) saveTranscript(transcript);
    }

    function line(text, tone = 'normal') {
        const row = document.createElement('p');
        row.className = `font-mono text-xs leading-relaxed ${tone === 'error' ? 'text-red-400' : 'text-zinc-300'}`;
        row.textContent = text;
        output.appendChild(row);
        output.scrollTop = output.scrollHeight;
        if (!restoringTranscript) {
            transcript.push({k: 'line', text, tone});
            persistTranscript();
        }
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

    async function confirmAction(action, button) {
        button.disabled = true;
        button.textContent = 'Confirming...';
        const response = await fetch('/api/miku/actions/confirm', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({confirmation_token: action.confirmation_token}),
        });
        const payload = await response.json();
        if (!response.ok) {
            button.disabled = false;
            button.textContent = 'Confirm';
            throw new Error(payload.detail || 'Action failed');
        }
        button.textContent = 'Confirmed';
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

    // Streaming TTS: sentence chunks arrive as speech.chunk events and play
    // back-to-back, so the first sentence sounds while the rest synthesizes.
    let speechQueue = [];
    let speechAudio;
    let speechRequestId;

    function duckWakeWhileSpeaking() {
        speaking += 1;
        if (recognition && wakeEnabled) {
            try { recognition.stop(); } catch (_error) { /* already stopped */ }
        }
    }

    function unduckWakeAfterSpeaking() {
        speaking = Math.max(0, speaking - 1);
        if (wakeAlive()) scheduleWakeRestart(300);
    }

    function playNextSpeechChunk() {
        if (speechAudio || speechQueue.length === 0) return;
        const chunk = speechQueue.shift();
        try {
            const bytes = Uint8Array.from(atob(chunk.audio), c => c.charCodeAt(0));
            const url = URL.createObjectURL(new Blob([bytes], {type: chunk.mediaType || 'audio/mpeg'}));
            const audio = new Audio(url);
            speechAudio = audio;
            const done = () => {
                URL.revokeObjectURL(url);
                if (speechAudio === audio) speechAudio = undefined;
                if (speechQueue.length === 0) unduckWakeAfterSpeaking();
                else playNextSpeechChunk();
            };
            audio.addEventListener('ended', done, {once: true});
            audio.addEventListener('error', done, {once: true});
            audio.play().catch(done);
        } catch (_error) {
            if (speechQueue.length === 0) unduckWakeAfterSpeaking();
            else playNextSpeechChunk();
        }
    }

    function stopSpeech() {
        speechQueue = [];
        if (speechAudio) {
            try { speechAudio.pause(); } catch (_error) { /* already stopped */ }
            speechAudio = undefined;
        }
        if (window.speechSynthesis?.speaking) window.speechSynthesis.cancel();
        if (speaking > 0) {
            speaking = 0;
            if (wakeAlive()) scheduleWakeRestart(300);
        }
        speechRequestId = undefined;
    }

    async function speak(text) {
        if (!voiceEnabled || !text) return;
        // Client TTS mode: the browser speaks, the server is never involved.
        if (ttsMode() === 'client') {
            if (!('speechSynthesis' in window)) {
                line('Browser speech synthesis is unavailable.', 'error');
                return;
            }
            duckWakeWhileSpeaking();
            try {
                await speakWithBrowserVoice(text);
            } finally {
                unduckWakeAfterSpeaking();
            }
            return;
        }
        if (socket?.readyState === WebSocket.OPEN && runtime.tts) {
            stopSpeech();
            duckWakeWhileSpeaking();
            speechRequestId = window.crypto?.randomUUID?.() || `${Date.now()}:${Math.random()}`;
            try {
                socket.send(JSON.stringify({type: 'speak', request_id: speechRequestId, message: text}));
            } catch (_error) {
                unduckWakeAfterSpeaking();
                speechRequestId = undefined;
            }
            return;
        }
        duckWakeWhileSpeaking();
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
            speechAudio = audio;
            const done = () => {
                URL.revokeObjectURL(url);
                if (speechAudio === audio) speechAudio = undefined;
            };
            audio.addEventListener('ended', done, {once: true});
            audio.addEventListener('error', done, {once: true});
            await audio.play();
            await new Promise(resolve => {
                audio.addEventListener('ended', resolve, {once: true});
                audio.addEventListener('error', resolve, {once: true});
            });
        } catch (_error) {
            if ('speechSynthesis' in window) {
                await speakWithBrowserVoice(text);
            }
        } finally {
            speechAudio = undefined;
            unduckWakeAfterSpeaking();
        }
    }

    function renderReply(payload, opts = {}) {
        const silent = !!opts.silent;
        const segments = payload.segments?.length ? payload.segments : [{text: payload.text, speak: true}];
        if (!silent) {
            transcript.push({
                k: 'reply',
                segments,
                references: payload.references || [],
                warnings: payload.warnings || [],
            });
            persistTranscript();
        }
        const stash = restoringTranscript;
        restoringTranscript = true;
        try {
            for (const segment of segments) line(segment.text);
        } finally {
            restoringTranscript = stash;
        }
        (payload.references || []).forEach((item, index) => {
            const row = document.createElement('div');
            row.className = 'flex flex-wrap items-center gap-2 border-l-2 border-zinc-800 py-1 pl-3';
            const text = document.createElement('span');
            text.className = 'min-w-0 flex-1 font-mono text-xs text-zinc-300';
            const details = [item.module_id, item.subtitle].filter(Boolean).join(' · ');
            text.textContent = `${index + 1}. ${item.title}${details ? `\n${details}` : ''}\nresult:${index + 1}`;
            text.classList.add('whitespace-pre-line');
            row.appendChild(text);
            if (moduleTarget(item)) row.appendChild(actionButton('Open', () => openReference(item)));
            if (item.playable) row.appendChild(actionButton('Play', () => openReference(item, true)));
            output.appendChild(row);
        });
        for (const warning of payload.warnings || []) line(`Warning: ${warning}`, 'error');
        if (payload.pending_action) {
            const row = document.createElement('div');
            row.className = 'flex items-center gap-3 border border-amber-900 bg-amber-950/20 p-3';
            const summary = document.createElement('span');
            summary.className = 'flex-1 font-mono text-[10px] text-amber-200';
            summary.textContent = `${payload.pending_action.label}: ${payload.pending_action.summary}`;
            const confirm = actionButton('Confirm', () => confirmAction(payload.pending_action, confirm).catch(error => line(error.message, 'error')), true);
            row.append(summary, confirm);
            output.appendChild(row);
        }
        if (!silent && payload.client_action && payload.references?.length === 1) {
            openReference(payload.references[0], payload.client_action === 'play');
        }
        if (!silent) speak(segments.filter(segment => segment.speak).map(segment => segment.text).join(' '));
        output.scrollTop = output.scrollHeight;
    }

    function restoreTranscript() {
        if (transcriptRestored) return;
        transcriptRestored = true;
        if (!transcript.length) return;
        restoringTranscript = true;
        try {
            for (const item of transcript) {
                if (item.k === 'line') line(item.text, item.tone || 'normal');
                else if (item.k === 'reply') renderReply(item, {silent: true});
            }
        } finally {
            restoringTranscript = false;
        }
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
            if (payload.event === 'turn.partial') {
                const phase = payload.data?.phase;
                if (phase === 'transcript' && payload.data?.text) line(`~ ${payload.data.text}`);
                else if (phase === 'acknowledgement' && payload.data?.text) status.textContent = payload.data.text;
                else if (phase === 'tool_result' && typeof payload.data?.result_count === 'number') {
                    status.textContent = `Found ${payload.data.result_count}`;
                }
            }
            if (payload.event === 'turn.result') renderReply(payload.data);
            if (payload.event === 'speech.chunk' && payload.request_id === speechRequestId) {
                speechQueue.push({
                    audio: payload.data?.audio || '',
                    mediaType: payload.data?.media_type || 'audio/mpeg',
                });
                playNextSpeechChunk();
            }
            if (payload.event === 'speech.cancelled' || payload.event === 'speech.error') {
                if (!payload.request_id || payload.request_id === speechRequestId) stopSpeech();
                if (payload.event === 'speech.error') line('Speech synthesis failed', 'error');
            }
            if (payload.event === 'turn.completed') {
                activeRequestId = undefined;
                status.textContent = 'Realtime';
            }
            if (payload.event === 'turn.cancelled') {
                if (payload.request_id === activeRequestId) activeRequestId = undefined;
                status.textContent = 'Realtime';
            }
            if (payload.event === 'turn.error') {
                if (!payload.request_id || payload.request_id === activeRequestId) activeRequestId = undefined;
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
        stopSpeech();
        status.textContent = 'Working';
        if (socket?.readyState === WebSocket.OPEN) {
            // Barge-in: cancel the in-flight turn before starting a new one.
            if (activeRequestId) {
                try {
                    socket.send(JSON.stringify({type: 'cancel', request_id: activeRequestId}));
                } catch (_error) {
                    // The socket may already be closing; fall through to the new turn.
                }
            }
            const requestId = window.crypto?.randomUUID?.() || `${Date.now()}:${Math.random()}`;
            activeRequestId = requestId;
            socket.send(JSON.stringify({type: 'query', request_id: requestId, message}));
        } else {
            restFallback(message);
        }
    }

    function blobToBase64(blob) {
        return new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => {
                const url = typeof reader.result === 'string' ? reader.result : '';
                resolve(url.includes(',') ? url.split(',')[1] : '');
            };
            reader.onerror = () => reject(reader.error || new Error('Audio encoding failed'));
            reader.readAsDataURL(blob);
        });
    }

    async function submitVoice(blob) {
        const mime = blob.type || 'audio/webm';
        if (socket?.readyState === WebSocket.OPEN) {
            setOpen(true);
            status.textContent = 'Transcribing';
            try {
                const audio = await blobToBase64(blob);
                if (!audio) throw new Error('Audio encoding failed');
                if (activeRequestId) {
                    try {
                        socket.send(JSON.stringify({type: 'cancel', request_id: activeRequestId}));
                    } catch (_error) {
                        // Ignore cancel failures; the new turn supersedes the old one.
                    }
                }
                const requestId = window.crypto?.randomUUID?.() || `${Date.now()}:${Math.random()}`;
                activeRequestId = requestId;
                socket.send(JSON.stringify({
                    type: 'voice',
                    request_id: requestId,
                    audio,
                    audio_content_type: mime,
                }));
            } catch (error) {
                line(error.message || 'Voice message failed', 'error');
                status.textContent = 'Realtime';
            }
            return;
        }
        if (sttMode() === 'client') {
            line('Client speech mode needs browser recognition; server transcription is off.', 'error');
            return;
        }
        try {
            if (blob.size > maxAudioBytes) throw new Error('Audio utterance is too large.');
            const response = await fetch('/api/miku/transcribe', {method: 'POST', headers: {'Content-Type': mime}, body: blob});
            const payload = await response.json();
            if (!response.ok) throw new Error(payload.detail || 'Speech recognition failed');
            submitMessage(payload.text);
        } catch (error) {
            line(error.message || 'Speech recognition failed', 'error');
        }
    }

    // Wake-word V1: continuous browser recognition gated on miku/мику.
    // Bare wake opens an 8s command window; TTS playback ducks the listener
    // so MIKU never answers herself. Only mic-permission errors stop wake.
    const WAKE_PATTERN = /(miku|мику)/i;
    const COMMAND_WINDOW_MS = 8000;
    let commandWindowUntil = 0;
    let wakeRetryDelay = 300;
    let speaking = 0;

    function wakeAlive() {
        return wakeEnabled && speaking === 0 && !recording;
    }

    function scheduleWakeRestart(delay) {
        window.clearTimeout(wakeRestartTimer);
        wakeRetryDelay = delay;
        wakeRestartTimer = window.setTimeout(() => {
            if (!wakeAlive()) return;
            try {
                startRecognition();
            } catch (_error) {
                scheduleWakeRestart(Math.min(wakeRetryDelay * 2, 5000));
            }
        }, delay);
    }

    function handleHeardText(finalText) {
        if (!wakeEnabled) {
            submitMessage(finalText);
            return;
        }
        const wake = finalText.match(WAKE_PATTERN);
        if (wake) {
            const command = finalText.slice(wake.index + wake[0].length).replace(/^[,\s:;-]+/, '').trim();
            wakeRetryDelay = 300;
            if (command) {
                submitMessage(command);
            } else {
                commandWindowUntil = Date.now() + COMMAND_WINDOW_MS;
                line('Слушаю…');
                status.textContent = 'Hearing';
            }
            return;
        }
        if (Date.now() < commandWindowUntil) {
            commandWindowUntil = 0;
            submitMessage(finalText);
        }
    }

    function startRecognition() {
        if (!SpeechRecognition) return false;
        recognition = new SpeechRecognition();
        let receivedSpeech = false;
        let recognitionError;
        recognition.lang = document.documentElement.lang === 'ru' ? 'ru-RU' : 'en-US';
        recognition.interimResults = false;
        recognition.continuous = wakeEnabled;
        recognition.addEventListener('speechstart', () => { status.textContent = 'Hearing'; });
        recognition.addEventListener('result', event => {
            let finalText = '';
            for (let index = event.resultIndex; index < event.results.length; index += 1) {
                if (event.results[index].isFinal) finalText += event.results[index][0].transcript;
            }
            if (!finalText) return;
            receivedSpeech = true;
            handleHeardText(finalText.trim());
        });
        recognition.addEventListener('error', event => {
            recognitionError = event.error;
            if (event.error === 'aborted') return;
            if (event.error === 'not-allowed' || event.error === 'service-not-allowed') {
                line('Microphone permission was denied.', 'error');
                wakeEnabled = false;
                wakeButton.textContent = 'Wake: off';
                persistToggles();
                return;
            }
            // Transient (no-speech/network/audio-capture): back off and retry.
            scheduleWakeRestart(Math.min(wakeRetryDelay * 2 || 600, 5000));
        });
        recognition.addEventListener('end', () => {
            recording = false;
            recordingMode = undefined;
            micButton.textContent = 'Push to talk';
            window.clearTimeout(wakeRestartTimer);
            if (wakeAlive()) scheduleWakeRestart(300);
            else if (!wakeEnabled && !receivedSpeech && !recognitionError) {
                line('No speech was detected.', 'error');
            }
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
                // Socket path sends a voice message (transcribe -> turn);
                // REST fallback transcribes then submits as text.
                await submitVoice(blob);
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
            // Client STT mode: only browser recognition, the server has nothing to call.
            if (sttMode() === 'client') {
                if (!startRecognition()) line('Browser speech recognition is unavailable.', 'error');
            } else if (runtime.stt) await startRecorder();
            else if (!startRecognition()) await startRecorder();
        } catch (error) {
            line(error.message || 'Microphone is unavailable', 'error');
            micButton.textContent = 'Push to talk';
        }
    });
    function persistToggles() {
        try {
            localStorage.setItem('miku-wake-enabled', wakeEnabled ? '1' : '0');
            localStorage.setItem('miku-voice-enabled', voiceEnabled ? '1' : '0');
        } catch (_error) {
            // Private mode: toggles still work for this page.
        }
    }

    wakeButton.addEventListener('click', () => {
        if (!SpeechRecognition) return line('Wake word needs browser speech recognition.', 'error');
        wakeEnabled = !wakeEnabled;
        wakeButton.textContent = `Wake: ${wakeEnabled ? 'miku' : 'off'}`;
        persistToggles();
        if (recordingMode === 'recognition') recognition.stop();
        else if (wakeEnabled) startRecognition();
    });
    voiceButton.addEventListener('click', () => {
        voiceEnabled = !voiceEnabled;
        voiceButton.textContent = `Voice: ${voiceEnabled ? 'on' : 'off'}`;
        persistToggles();
        if (!voiceEnabled) stopSpeech();
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

    function applyPersistedToggles() {
        wakeButton.textContent = `Wake: ${wakeEnabled && SpeechRecognition ? 'miku' : 'off'}`;
        if (wakeEnabled && !SpeechRecognition) wakeEnabled = false;
        voiceButton.textContent = `Voice: ${voiceEnabled ? 'on' : 'off'}`;
    }

    window.mikuAssistant = {open: () => setOpen(true), close: () => setOpen(false), submit: submitMessage};
    fetch('/api/miku/runtime').then(response => response.ok ? response.json() : Promise.reject()).then(payload => { runtime = payload; }).catch(() => {});
    applyPersistedToggles();
    restoreTranscript();
    connect();
    // Resume always-listening quietly after navigation; failures stay silent
    // here since the user already opted in on a previous page.
    if (wakeEnabled && SpeechRecognition && !recording) {
        try {
            startRecognition();
        } catch (_error) {
            // Mic will be retried on the next manual toggle.
        }
    }
    if (window.location.pathname === '/miku/dashboard') setOpen(true);
})();
