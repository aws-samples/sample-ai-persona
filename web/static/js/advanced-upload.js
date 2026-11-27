/**
 * Behavior of the advanced-generation (beta) page.
 *
 * - Section A: PUTs the table to signed URLs in 16MB parts (size decided by the server), up to 4 at a time.
 *   A failed part is retried up to 3 times after waiting 1, 2, and 4 seconds; when attempts run out the upload is canceled.
 *   The URLs of parts not yet sent are re-fetched 5 minutes before the URL expires.
 *   File contents are never sent to the app. URLs are not shown on screen or in logs.
 * - Section B: "ペルソナを生成" cannot be pressed until a ready table is selected. The reason is shown as text.
 * - Section C: progress is read with EventSource. On disconnect it reconnects automatically (Last-Event-ID); if it stays disconnected for 2 minutes it tells the user to reload. It closes on done.
 * - Section D: the result HTML is rendered by the server (already auto-escaped). Saving is not possible until a candidate is selected.
 *
 * It does not register on htmx events (htmx handlers are collected in app.js; see .claude/rules/frontend.md).
 * Wait times can be overridden with window.ADVANCED_UPLOAD_TIMING (for component tests).
 */
(function () {
    'use strict';

    var page = document.getElementById('advanced-generation');
    if (!page) return;

    var TIMING = Object.assign({
        retryDelaysMs: [1000, 2000, 4000],
        refreshLeadMs: 5 * 60 * 1000,
        reconnectDelaysMs: [3000, 6000, 12000, 24000, 30000],
        disconnectAfterMs: 2 * 60 * 1000,
        // Re-fetching rows that are being checked (the interval and the give-up time come from the page's data-* attributes, which carry the config values)
        inspectPollMs: (Number(page.dataset.inspectPollSeconds) || 2) * 1000,
        inspectStopMs: (Number(page.dataset.inspectStopSeconds) || 900) * 1000
    }, window.ADVANCED_UPLOAD_TIMING || {});

    var MAX_REFRESHES = 24;
    var MAX_TIMER_MS = 2147483647;
    // The wait for plan confirmation is shown only for generations that check the plan (displayed once that stage is reached)
    var AWAITING = 'awaiting_plan_approval';
    var STAGES = ['preparing', 'planning', AWAITING, 'running', 'synthesizing', 'verifying'];
    var MARKS = { pending: '', current: '●', done: '✓', failed: '×' };
    var STATE_WORDS = { pending: '未', current: '進行中', done: '済', failed: '失敗' };
    // Classes for stage circles, lines, and text (written in full so Tailwind can pick them up)
    var MARK_CLASS = {
        pending: 'relative z-10 mx-auto flex w-6 h-6 items-center justify-center rounded-full border-2 border-gray-300 bg-white text-xs font-bold text-gray-500',
        current: 'relative z-10 mx-auto flex w-6 h-6 items-center justify-center rounded-full border-2 border-blue-600 bg-white text-xs font-bold text-blue-700',
        done: 'relative z-10 mx-auto flex w-6 h-6 items-center justify-center rounded-full border-2 border-blue-600 bg-blue-600 text-xs font-bold text-white',
        failed: 'relative z-10 mx-auto flex w-6 h-6 items-center justify-center rounded-full border-2 border-red-700 bg-red-700 text-xs font-bold text-white'
    };
    var LINE_CLASS = {
        reached: 'absolute top-3 right-1/2 w-full h-0.5 bg-blue-600',
        pending: 'absolute top-3 right-1/2 w-full h-0.5 bg-gray-300'
    };
    // In-progress and failed use a label (filled rounded box) that can be told apart from the stage name
    var STATE_CLASS = {
        current: 'inline-block mt-1 px-2 py-0.5 rounded-full text-xs font-medium bg-blue-600 text-white',
        failed: 'inline-block mt-1 px-2 py-0.5 rounded-full text-xs font-medium bg-red-700 text-white',
        hidden: 'sr-only'
    };
    var METHOD_LABELS = { filter: '絞り込み', compare: '群どうしの比較', cluster: 'クラスタ分析' };
    var FAILURE_TITLES = {
        generation_timeout: '生成が止まりました',
        generation_sandbox_unavailable: '今は高度な生成を使えません',
        generation_expired: 'この生成の結果は残っていません'
    };

    var rows = document.getElementById('advanced-upload-rows');
    var rowTemplate = document.getElementById('advanced-upload-row-template');
    var emptyNote = page.querySelector('[data-testid="advanced-upload-empty"]');
    var announcer = document.getElementById('advanced-upload-announcer');
    var inputs = document.getElementById('advanced-inputs');
    var form = document.getElementById('advanced-generation-form');
    var generateButton = page.querySelector('[data-testid="advanced-generate-button"]');
    var generateReason = page.querySelector('[data-testid="advanced-generate-reason"]');
    var progressArea = document.getElementById('advanced-progress-area');

    var running = false;
    var submitting = false;

    // --- Common ---

    function toast(message) {
        if (typeof window.showFlashMessage === 'function') window.showFlashMessage(message, 'error');
    }

    function sleep(ms) {
        return new Promise(function (resolve) { setTimeout(resolve, ms); });
    }

    function announce(text) {
        if (announcer) announcer.textContent = text;
    }

    function sizeLabel(bytes) {
        return '（' + (bytes / 1000000).toFixed(1) + 'MB）';
    }

    function postForm(url, values) {
        return fetch(url, {
            method: 'POST',
            headers: { 'X-Requested-With': 'XMLHttpRequest' },
            body: new URLSearchParams(values),
            credentials: 'same-origin'
        });
    }

    function postJson(url, body) {
        return fetch(url, {
            method: 'POST',
            headers: { 'X-Requested-With': 'XMLHttpRequest', 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
            credentials: 'same-origin'
        });
    }

    function readJson(response) {
        return response.json().catch(function () { return {}; });
    }

    function parseHtml(html) {
        var holder = document.createElement('template');
        holder.innerHTML = html.trim();
        return holder.content.firstElementChild;
    }

    function updateEmptyNote() {
        if (emptyNote) emptyNote.hidden = rows.querySelector('[data-testid="advanced-upload-row"]') !== null;
    }

    // --- Section B: generate button ---

    // Estimated total character count of the selected documents (a guideline; it does not block on its own)
    function updateDocumentEstimate(chosen) {
        var note = page.querySelector('[data-testid="advanced-document-estimate"]');
        if (!note) return;
        var total = 0;
        var documents = 0;
        chosen.forEach(function (input) {
            var row = input.closest('[data-testid="advanced-upload-row"]');
            if (row && row.dataset.kind === 'document') {
                documents += 1;
                total += Number(row.dataset.estimatedChars) || 0;
            }
        });
        if (!documents) {
            note.hidden = true;
            note.textContent = '';
            return;
        }
        var man = Math.ceil(total / 10000);
        var text = '選んだ文書の合計 約 ' + man + ' 万字の見込み';
        if (man > Number(note.dataset.maxManChars)) {
            text += '（前準備で上限の ' + note.dataset.maxManChars + ' 万字を超えると止まります）';
        }
        note.textContent = text;
        note.hidden = false;
    }

    function updateGenerateState() {
        if (!generateButton) return;
        var chosen = Array.prototype.slice.call(
            document.querySelectorAll('[name="source_file_ids"]:checked')
        );
        var tables = chosen.filter(function (input) { return input.dataset.kind !== 'document'; });
        // up to three tables (the limit comes from the page; the server refuses too)
        var form = generateButton.form;
        var maxTables = Number((form && form.dataset.maxTables) || 3);
        var tooMany = tables.length > maxTables;
        var reason = '';
        if (submitting) {
            reason = '開始しています';
        } else if (running) {
            reason = '実行中の生成があります。終わるか中止してから始めてください';
        } else if (!chosen.length) {
            reason = '準備OK のファイルを選んでください';
        } else if (tooMany) {
            reason = 'CSV は ' + maxTables + ' つまで選べます（文書はいくつでも一緒に選べます）';
        }
        generateButton.disabled = submitting || running || !chosen.length || tooMany;
        generateReason.textContent = reason;
        updateDocumentEstimate(chosen);
    }

    if (form) {
        form.addEventListener('submit', function () {
            submitting = true;
            // Disable the button after htmx has collected the values
            setTimeout(updateGenerateState, 0);
        });
        // Re-enable it when the submit is refused (an error inside the form, a toast)
        var formError = form.querySelector('.advanced-form-error');
        var toasts = document.getElementById('toast-alert');
        var release = function () {
            if (!submitting) return;
            submitting = false;
            updateGenerateState();
        };
        if (formError) new MutationObserver(release).observe(formError, { childList: true });
        if (toasts) new MutationObserver(release).observe(toasts, { childList: true });
    }

    document.addEventListener('change', function (evt) {
        var target = evt.target;
        if (!(target instanceof HTMLInputElement)) return;
        if (target.name === 'source_file_ids') updateGenerateState();
        if (target.matches('[data-testid="advanced-candidate-check"]')) updateSaveState(target.form);
    });

    // --- Section A: upload ---

    // Tables (.csv, .tsv) and documents (.pdf, .docx, .txt, .md). .doc gets a notice
    var TABLE_EXTENSIONS = ['.csv', '.tsv'];
    var DOCUMENT_EXTENSIONS = ['.pdf', '.docx', '.txt', '.md'];

    function extensionOf(name) {
        var dot = name.lastIndexOf('.');
        return dot < 0 ? '' : name.slice(dot);
    }

    function refusal(file, count) {
        var name = file.name.toLowerCase();
        var extension = extensionOf(name);
        var isDocument = DOCUMENT_EXTENSIONS.indexOf(extension) !== -1;
        var maxBytes = Number(isDocument ? page.dataset.maxDocumentBytes : page.dataset.maxBytes);
        var maxSizeMb = isDocument ? page.dataset.maxDocumentSizeMb : page.dataset.maxSizeMb;
        var maxFiles = Number(page.dataset.maxFiles);
        if (extension === '.xlsx' || extension === '.xls') {
            return '上げられません: Excel のファイルは使えません。CSV で保存し直して上げてください';
        }
        if (extension === '.doc') {
            return '上げられません: 古い Word の形式（.doc）は使えません。.docx で保存し直して上げてください';
        }
        if (!isDocument && TABLE_EXTENSIONS.indexOf(extension) === -1) {
            return '上げられません: CSV・TSV か、PDF・Word（.docx）・テキスト（.txt・.md）の文書を選んでください';
        }
        if (file.size > maxBytes) {
            return '上げられません: 1 ファイル ' + maxSizeMb + 'MB までです。分けて上げてください';
        }
        if (count >= maxFiles) {
            return '上げられません: 1 回の生成で使えるのは ' + maxFiles + ' ファイルまでです';
        }
        return null;
    }

    function onFiles(fileList) {
        Array.prototype.forEach.call(fileList, function (file) {
            var count = rows.querySelectorAll('[data-testid="advanced-upload-row"]').length;
            var refused = refusal(file, count);
            if (refused) {
                toast(refused);
                return;
            }
            startUpload(file);
        });
    }

    function newRow(file) {
        var row = rowTemplate.content.firstElementChild.cloneNode(true);
        row.querySelector('[data-role="name"]').textContent = file.name;
        row.querySelector('[data-role="size"]').textContent = sizeLabel(file.size);
        rows.prepend(row);
        updateEmptyNote();
        return row;
    }

    function setStatus(upload, text) {
        var status = upload.row.querySelector('[data-role="status"]');
        if (status) status.textContent = text;
    }

    function startUpload(file) {
        var row = newRow(file);
        postForm('/persona/advanced/uploads', {
            filename: file.name,
            size_bytes: String(file.size),
            content_type: file.type || ''
        }).then(function (response) {
            return readJson(response).then(function (body) {
                if (!response.ok) {
                    toast(body.detail || 'アップロードを始められませんでした');
                    row.remove();
                    updateEmptyNote();
                    return;
                }
                sendParts(file, row, body);
            });
        }).catch(function () {
            toast('アップロードを始められませんでした。通信を確かめてください');
            row.remove();
            updateEmptyNote();
        });
    }

    function sendParts(file, row, session) {
        var upload = {
            id: session.source_file_id,
            file: file,
            row: row,
            partSize: session.part_size_bytes,
            partCount: session.part_count,
            urls: new Map(),
            expiresAt: session.expires_at,
            etags: new Map(),
            sentBytes: 0,
            controller: new AbortController(),
            refreshing: null,
            refreshTimer: null,
            refreshes: 0,
            finished: false
        };
        session.part_urls.forEach(function (p) { upload.urls.set(p.part_number, p.url); });
        row.dataset.sourceFileId = upload.id;
        var cancel = row.querySelector('[data-action="cancel"]');
        if (cancel) cancel.addEventListener('click', function () { cancelUpload(upload); });
        scheduleRefresh(upload);

        var queue = [];
        for (var n = 1; n <= upload.partCount; n += 1) queue.push(n);
        var workers = [];
        var concurrency = Number(page.dataset.concurrency) || 4;
        for (var i = 0; i < concurrency; i += 1) workers.push(worker(upload, queue));
        Promise.all(workers).then(function () {
            finishParts(upload);
            completeUpload(upload);
        }).catch(function (failure) {
            finishParts(upload);
            if (upload.controller.signal.aborted && failure && failure.cancelled) return;
            failUpload(upload, failure && failure.status);
        });
    }

    function worker(upload, queue) {
        var next = function () {
            if (queue.length === 0) return Promise.resolve();
            var number = queue.shift();
            return Promise.resolve(upload.refreshing)
                .then(function () { return sendPart(upload, number); })
                .then(next);
        };
        return next();
    }

    function sendPart(upload, number) {
        var start = (number - 1) * upload.partSize;
        var blob = upload.file.slice(start, start + upload.partSize);
        var lastStatus = 0;
        var attempt = function (index) {
            if (upload.controller.signal.aborted) return Promise.reject({ cancelled: true });
            return fetch(upload.urls.get(number), {
                method: 'PUT',
                body: blob,
                signal: upload.controller.signal
            }).then(function (response) {
                var etag = response.headers.get('ETag');
                if (response.ok && etag) {
                    upload.etags.set(number, etag);
                    upload.sentBytes += blob.size;
                    var percent = Math.floor((upload.sentBytes / upload.file.size) * 100);
                    setStatus(upload, 'アップロード中 ' + percent + '%');
                    return undefined;
                }
                lastStatus = response.status;
                return retry(index);
            }, function () {
                if (upload.controller.signal.aborted) return Promise.reject({ cancelled: true });
                return retry(index);
            });
        };
        var retry = function (index) {
            if (index >= TIMING.retryDelaysMs.length) return Promise.reject({ status: lastStatus });
            return sleep(TIMING.retryDelaysMs[index]).then(function () { return attempt(index + 1); });
        };
        return attempt(0);
    }

    function finishParts(upload) {
        upload.finished = true;
        if (upload.refreshTimer) clearTimeout(upload.refreshTimer);
    }

    function scheduleRefresh(upload) {
        if (upload.finished || upload.refreshes >= MAX_REFRESHES) return;
        var delay = Math.max(0, Date.parse(upload.expiresAt) - TIMING.refreshLeadMs - Date.now());
        if (delay > MAX_TIMER_MS) {
            // setTimeout fires immediately above 2^31-1 ms, so wait in chunks
            upload.refreshTimer = setTimeout(function () { scheduleRefresh(upload); }, MAX_TIMER_MS);
            return;
        }
        upload.refreshTimer = setTimeout(function () { refreshUrls(upload); }, delay);
    }

    function refreshUrls(upload) {
        if (upload.finished) return;
        var numbers = [];
        for (var n = 1; n <= upload.partCount; n += 1) {
            if (!upload.etags.has(n)) numbers.push(n);
        }
        if (numbers.length === 0) return;
        upload.refreshes += 1;
        var previous = upload.expiresAt;
        upload.refreshing = postJson('/persona/advanced/uploads/' + encodeURIComponent(upload.id) + '/part-urls', {
            part_numbers: numbers
        }).then(function (response) {
            return readJson(response).then(function (body) {
                if (!response.ok) return; // When the URLs cannot be re-fetched, the expired send is treated as a retry
                body.part_urls.forEach(function (p) { upload.urls.set(p.part_number, p.url); });
                upload.expiresAt = body.expires_at;
                if (Date.parse(body.expires_at) > Date.parse(previous)) scheduleRefresh(upload);
            });
        }).catch(function () { return undefined; }).then(function () {
            upload.refreshing = null;
        });
    }

    function cancelUpload(upload) {
        upload.controller.abort();
        finishParts(upload);
        fetch('/persona/advanced/uploads/' + encodeURIComponent(upload.id), {
            method: 'DELETE',
            headers: { 'X-Requested-With': 'XMLHttpRequest' },
            credentials: 'same-origin'
        }).catch(function () { return undefined; });
        upload.row.remove();
        updateEmptyNote();
        announce(upload.file.name + ' のアップロードを取り消しました');
    }

    function failUpload(upload, status) {
        upload.controller.abort();
        fetch('/persona/advanced/uploads/' + encodeURIComponent(upload.id), {
            method: 'DELETE',
            headers: { 'X-Requested-With': 'XMLHttpRequest' },
            credentials: 'same-origin'
        }).catch(function () { return undefined; });
        var message = status === 403
            ? '上げられませんでした: 時間がたったため止まりました。もう一度上げてください'
            : '上げられませんでした。通信を確かめて、もう一度上げてください';
        upload.row.dataset.state = 'error';
        setStatus(upload, message);
        var cancel = upload.row.querySelector('[data-action="cancel"]');
        if (cancel) cancel.remove();
        announce(upload.file.name + ': ' + message);
    }

    function completeUpload(upload) {
        upload.row.dataset.state = 'inspecting';
        setStatus(upload, '検査中');
        var cancel = upload.row.querySelector('[data-action="cancel"]');
        if (cancel) cancel.remove();
        var parts = Array.from(upload.etags.entries())
            .sort(function (a, b) { return a[0] - b[0]; })
            .map(function (entry) { return { part_number: entry[0], etag: entry[1] }; });
        postJson('/persona/advanced/uploads/' + encodeURIComponent(upload.id) + '/complete', { parts: parts })
            .then(function (response) {
                return readJson(response).then(function (body) {
                    // An error fixable by correcting input is shown by the row fragment as the error text
                    if (!response.ok && body.kind !== 'validation' && body.kind !== 'capacity') {
                        toast(body.detail || '確定できませんでした。時間をおいて再度お試しください');
                    }
                });
            })
            .catch(function () { toast('確定できませんでした。通信を確かめてください'); })
            .then(function () { return replaceRow(upload); });
    }

    function replaceRow(upload) {
        return fetchRow(upload.id).then(function (fresh) {
            if (!fresh) return; // The row is kept as it is (the failure was shown as a toast)
            upload.row.replaceWith(fresh);
            upload.row = fresh;
            announceRow(fresh);
            watchInspecting(fresh);
        });
    }

    // --- Section A: list of uploaded files ---

    function rowStatusText(row) {
        var status = row.querySelector('[data-testid="advanced-upload-status"]');
        return status ? status.textContent.trim().replace(/\s+/g, ' ') : '';
    }

    function announceRow(row) {
        announce((row.dataset.displayName || '') + ': ' + rowStatusText(row));
    }

    // Re-read the row fragment. Returns null if it cannot be read (another user's, missing, or expired rows show no toast)
    function fetchRow(id) {
        return fetch('/persona/advanced/uploads/' + encodeURIComponent(id), {
            headers: { 'HX-Request': 'true' },
            credentials: 'same-origin'
        }).then(function (response) {
            if (!response.ok) return null;
            return response.text().then(function (html) {
                var fresh = parseHtml(html);
                if (fresh && window.htmx) window.htmx.process(fresh);
                return fresh;
            });
        }).catch(function () { return null; });
    }

    function currentRow(id) {
        return rows.querySelector('[data-testid="advanced-upload-row"][data-source-file-id="' + CSS.escape(id) + '"]');
    }

    // When generation ends, re-read from the server the rows that could not be selected during "generating" and swap them.
    // Without re-reading, they cannot be reused or deleted until the page is reopened
    function refreshInUseRows() {
        if (!rows) return;
        var busy = rows.querySelectorAll('[data-testid="advanced-upload-in-use"]');
        Array.prototype.forEach.call(busy, function (mark) {
            var row = mark.closest('[data-testid="advanced-upload-row"]');
            var id = row && row.dataset.sourceFileId;
            if (!id) return;
            fetchRow(id).then(function (fresh) {
                var current = currentRow(id);
                if (!fresh || !current) return; // If it cannot be read, leave the row as it is
                current.replaceWith(fresh);
                updateGenerateState();
            });
        });
    }

    // Re-read rows that are being checked at each interval; when the give-up time passes, stop and show "re-confirm".
    // Rows being checked after confirmation in a second tab, or on a reopened page, also wait for the check to end here
    var watching = {};

    function watchInspecting(row) {
        if (!row || row.dataset.state !== 'inspecting') return;
        var id = row.dataset.sourceFileId;
        if (!id || watching[id]) return;
        if (row.querySelector('[data-action="recomplete"]')) return; // The server judged that it stalled
        watching[id] = { startedAt: Date.now() };
        var tick = function () {
            var state = watching[id];
            if (!state) return;
            if (Date.now() - state.startedAt >= TIMING.inspectStopMs) {
                delete watching[id];
                stopWatching(currentRow(id));
                return;
            }
            fetchRow(id).then(function (fresh) {
                var row = currentRow(id);
                if (!row) {
                    delete watching[id];
                    return;
                }
                if (fresh) {
                    row.replaceWith(fresh);
                    if (fresh.dataset.state !== 'inspecting') {
                        delete watching[id];
                        announceRow(fresh);
                        updateGenerateState();
                        return;
                    }
                }
                setTimeout(tick, TIMING.inspectPollMs);
            });
        };
        setTimeout(tick, TIMING.inspectPollMs);
    }

    function stopWatching(row) {
        if (!row || row.dataset.state !== 'inspecting') return;
        var status = row.querySelector('[data-testid="advanced-upload-status"]');
        if (status) status.textContent = '検査が止まりました。確定し直してください';
        if (!row.querySelector('[data-action="recomplete"]')) {
            var cell = row.lastElementChild;
            var button = document.createElement('button');
            button.type = 'button';
            button.dataset.action = 'recomplete';
            button.dataset.testid = 'advanced-upload-recomplete';
            button.className = 'text-sm text-blue-700 underline hover:text-blue-900';
            button.textContent = '確定し直す';
            if (cell) cell.appendChild(button);
        }
        announceRow(row);
    }

    // Re-confirm a stalled check (the parts are already one object in S3, so send an empty list)
    function recomplete(row) {
        var id = row.dataset.sourceFileId;
        postJson('/persona/advanced/uploads/' + encodeURIComponent(id) + '/complete', { parts: [] })
            .then(function (response) {
                return readJson(response).then(function (body) {
                    if (!response.ok && body.kind !== 'validation' && body.kind !== 'capacity') {
                        toast(body.detail || '確定できませんでした。時間をおいて再度お試しください');
                    }
                });
            })
            .catch(function () { toast('確定できませんでした。通信を確かめてください'); })
            .then(function () { return fetchRow(id); })
            .then(function (fresh) {
                var current = currentRow(id);
                if (!fresh || !current) return;
                current.replaceWith(fresh);
                announceRow(fresh);
                watchInspecting(fresh);
                updateGenerateState();
            });
    }

    rows.addEventListener('click', function (evt) {
        var target = evt.target instanceof Element ? evt.target.closest('[data-action="recomplete"]') : null;
        if (!target) return;
        var row = target.closest('[data-testid="advanced-upload-row"]');
        if (row) recomplete(row);
    });

    // Delete succeeded (HX-Trigger: advancedSourceDeleted). htmx removes the pressed button's row afterwards
    document.addEventListener('advancedSourceDeleted', function (evt) {
        var row = evt.target instanceof Element ? evt.target.closest('[data-testid="advanced-upload-row"]') : null;
        if (row) announce((row.dataset.displayName || '') + ' を削除しました');
    });

    // When rows are added or removed (delete, check finished), sync the empty notice and the generate button
    new MutationObserver(function () {
        updateEmptyNote();
        updateGenerateState();
    }).observe(rows, { childList: true });

    Array.prototype.forEach.call(rows.querySelectorAll('[data-testid="advanced-upload-row"]'), watchInspecting);

    var fileInput = document.getElementById('advanced-file-input');
    if (fileInput) {
        fileInput.addEventListener('change', function () {
            onFiles(fileInput.files);
            fileInput.value = '';
        });
    }
    var dropZone = page.querySelector('[data-testid="advanced-drop-zone"]');
    if (dropZone) {
        ['dragenter', 'dragover'].forEach(function (name) {
            dropZone.addEventListener(name, function (evt) { evt.preventDefault(); });
        });
        dropZone.addEventListener('drop', function (evt) {
            evt.preventDefault();
            if (evt.dataTransfer) onFiles(evt.dataTransfer.files);
        });
    }

    // --- Section C: progress ---

    function setStages(panel, current, failed) {
        var index = STAGES.indexOf(current);
        if (current === AWAITING) {
            var awaiting = panel.querySelector('[data-stage="' + AWAITING + '"]');
            if (awaiting) awaiting.hidden = false;
        }
        panel.querySelectorAll('[data-stage]').forEach(function (item) {
            var position = STAGES.indexOf(item.dataset.stage);
            var state = 'pending';
            if (current === 'done' || position < index) state = 'done';
            else if (position === index) state = failed ? 'failed' : 'current';
            item.dataset.state = state;
            var mark = item.querySelector('[data-role="mark"]');
            mark.textContent = MARKS[state];
            mark.className = MARK_CLASS[state];
            var line = item.querySelector('[data-role="line"]');
            if (line) line.className = state === 'pending' ? LINE_CLASS.pending : LINE_CLASS.reached;
            var word = item.querySelector('[data-role="state"]');
            if (word) {
                word.textContent = STATE_WORDS[state];
                word.className = STATE_CLASS[state] || STATE_CLASS.hidden;
            }
        });
        updateBar(panel, current, failed);
    }

    // A rough bar based on the number of stages (not the actual ratio). Counts only the stages being shown
    function updateBar(panel, current, failed) {
        var bar = panel.querySelector('[data-testid="advanced-progress-bar"]');
        if (!bar) return;
        var shown = Array.prototype.filter.call(
            panel.querySelectorAll('[data-stage]'), function (item) { return !item.hidden; });
        var total = shown.length;
        var finished = current === 'done';
        var at = 0;
        shown.forEach(function (item, i) { if (item.dataset.stage === current) at = i + 1; });
        var done = finished ? total : Math.max(at - 1, 0);
        var percent = total ? Math.round(done * 100 / total) : 0;
        bar.setAttribute('aria-valuenow', String(percent));
        var fill = bar.querySelector('[data-role="fill"]');
        var label = panel.querySelector('[data-testid="advanced-progress-label"]');
        var count = panel.querySelector('[data-testid="advanced-progress-count"]');
        if (fill) {
            fill.style.width = percent + '%';
            fill.className = 'h-2 rounded transition-all duration-500 ' + (failed ? 'bg-red-700' : (finished ? 'bg-green-700' : 'bg-blue-600'));
        }
        if (!label || !count) return;
        if (finished) {
            label.textContent = '完了';
            count.textContent = total + ' / ' + total + ' 段階';
        } else if (at) {
            var nameNode = shown[at - 1].querySelector('[data-role="label"]');
            var name = nameNode ? nameNode.textContent.trim() : '';
            label.textContent = (failed ? '止まりました: ' : '現在: ') + name;
            count.textContent = at + ' / ' + total + ' 段階';
        } else if (failed) {
            // the failing stage is not one of the shown stages
            label.textContent = '止まりました';
            count.textContent = '';
        }
    }

    function addLog(panel, text) {
        var item = document.createElement('li');
        item.textContent = text;
        var list = panel.querySelector('[data-testid="advanced-log"]');
        list.appendChild(item);
        list.scrollTop = list.scrollHeight;  // the newest line stays in view
    }

    function parseData(evt) {
        try {
            var value = JSON.parse(evt.data || '{}');
            return value && typeof value === 'object' ? value : {};
        } catch (e) {
            return {};
        }
    }

    function clockText(seconds) {
        var minutes = Math.floor(seconds / 60);
        var rest = seconds % 60;
        return (minutes < 10 ? '0' : '') + minutes + ':' + (rest < 10 ? '0' : '') + rest;
    }

    // Elapsed time: advance every second from the seconds the server rendered. A finished generation does not advance.
    // The display is outside aria-live, so the per-second rewrite is not announced.
    // Pause while waiting for plan confirmation and resume to advance again afterwards.
    function startClock(panel) {
        var element = panel.querySelector('[data-testid="advanced-elapsed"]');
        var none = { stop: function () {}, pause: function () {}, resume: function () {} };
        if (!element) return none;
        var clock = element.querySelector('[data-role="clock"]');
        var base = Number(element.dataset.elapsedSeconds) || 0;
        var startedAt = 0;
        var timer = null;
        var stopped = false;
        var pause = function () {
            if (timer === null) return;
            clearInterval(timer);
            timer = null;
            base += Math.floor((Date.now() - startedAt) / 1000);
            element.dataset.running = 'false';
        };
        var resume = function () {
            if (stopped || timer !== null) return;
            startedAt = Date.now();
            element.dataset.running = 'true';
            timer = setInterval(function () {
                clock.textContent = clockText(base + Math.floor((Date.now() - startedAt) / 1000));
            }, 1000);
        };
        if (element.dataset.running === 'true') resume();
        return {
            stop: function () {
                pause();
                stopped = true;
                element.dataset.running = 'false';
            },
            pause: pause,
            resume: resume
        };
    }

    function connect(panel) {
        if (panel.dataset.connected) return;
        panel.dataset.connected = 'true';
        var url = panel.dataset.eventsUrl;
        var state = { lastId: 0, current: null, finished: false, failures: 0, firstFailureAt: null, source: null };
        var reconnecting = panel.querySelector('[data-testid="advanced-reconnecting"]');
        var disconnected = panel.querySelector('[data-testid="advanced-disconnected"]');
        rememberGeneration(panel.dataset.generationId);

        // Accept a continuation of the same connection (frames without an id); discard what is resent after a manual reconnect
        var fresh = function (evt, connection) {
            var raw = evt.lastEventId || '';
            if (raw === connection.previous) return true;
            connection.previous = raw;
            var number = Number(raw);
            if (!raw || isNaN(number)) return true;
            if (number <= state.lastId) return false;
            state.lastId = number;
            return true;
        };

        var clock = startClock(panel);
        var planArea = panel.querySelector('[data-testid="advanced-plan-area"]');
        var cancelArea = panel.querySelector('[data-testid="advanced-cancel-area"]');

        var finish = function () {
            state.finished = true;
            clock.stop();
            running = false;
            reconnecting.hidden = true;
            // A finished generation shows neither cancel nor the plan card
            if (cancelArea) {
                // flex overrides the hidden attribute, so hide it with a class too
                cancelArea.hidden = true;
                cancelArea.classList.add('hidden');
            }
            if (planArea) planArea.innerHTML = '';
            if (state.source) state.source.close();
            updateGenerateState();
            refreshInUseRows();
        };

        var handlers = {
            stage: function (data) {
                state.current = data.status;
                setStages(panel, data.status, false);
                running = true;
                if (inputs) inputs.open = false;
                // Stop the elapsed time while waiting for plan confirmation
                if (data.status === AWAITING) {
                    clock.pause();
                } else {
                    clock.resume();
                    if (data.status !== 'planning' && planArea) planArea.innerHTML = '';
                }
                updateGenerateState();
            },
            progress: function (data) {
                if (data.message) addLog(panel, String(data.message));
            },
            plan: function (data, evt) {
                var raw = String(evt.data || '').trim();
                if (raw.charAt(0) === '<' && planArea) {
                    // The plan card rendered by the server (already auto-escaped)
                    planArea.innerHTML = raw;
                    if (window.htmx) window.htmx.process(planArea);
                    var heading = planArea.querySelector('#advanced-plan-title');
                    if (heading) heading.focus();
                    return;
                }
                var text = '計画: ' + (METHOD_LABELS[data.method] || '分析');
                if (data.interpreted_purpose) text += '（' + String(data.interpreted_purpose) + '）';
                addLog(panel, text);
            },
            cancelled: function () {
                // Cancellation is not styled as an error. The form is left as it is
                setStages(panel, state.current || 'preparing', true);
                var cancelled = panel.querySelector('[data-testid="advanced-cancelled"]');
                cancelled.hidden = false;
                finish();
                if (inputs) inputs.open = true;
                cancelled.querySelector('[data-role="title"]').focus();
            },
            result: function (data, evt) {
                setStages(panel, 'done', false);
                var area = document.getElementById('advanced-result');
                area.innerHTML = evt.data; // Section D rendered by the server (already auto-escaped)
                if (window.htmx) window.htmx.process(area);
                finish();
                if (inputs) inputs.open = false;
                var title = document.getElementById('advanced-result-title');
                if (title) title.focus();
            },
            error: function (data) {
                setStages(panel, state.current || 'preparing', true);
                var failure = panel.querySelector('[data-testid="advanced-failure"]');
                var title = failure.querySelector('[data-role="title"]');
                title.textContent = FAILURE_TITLES[data.error_code] || '生成できませんでした';
                failure.querySelector('[data-role="message"]').textContent = String(data.message || '');
                failure.hidden = false;
                finish();
                if (inputs) inputs.open = true;
                title.focus();
            },
            done: function () {
                finish();
            }
        };

        var open = function () {
            var connection = { previous: '' };
            // When reconnecting itself, pass the last received number in the query (a header cannot be set)
            var target = state.lastId > 0
                ? url + (url.indexOf('?') === -1 ? '?' : '&') + 'last_event_id=' + state.lastId
                : url;
            var source = new EventSource(target);
            state.source = source;
            Object.keys(handlers).forEach(function (name) {
                source.addEventListener(name, function (evt) {
                    // The EventSource's own error on disconnect (not a MessageEvent) is handled by onerror
                    if (!(evt instanceof MessageEvent)) return;
                    if (state.finished && name !== 'done') return;
                    if (!fresh(evt, connection)) return;
                    handlers[name](parseData(evt), evt);
                });
            });
            source.onopen = function () {
                state.failures = 0;
                state.firstFailureAt = null;
                reconnecting.hidden = true;
            };
            source.onerror = function () {
                if (state.finished) {
                    source.close();
                    return;
                }
                var now = Date.now();
                if (state.firstFailureAt === null) state.firstFailureAt = now;
                if (now - state.firstFailureAt >= TIMING.disconnectAfterMs) {
                    source.close();
                    reconnecting.hidden = true;
                    disconnected.hidden = false;
                    return;
                }
                // Reconnect on our own instead of leaving it to the browser (waiting 3, 6, 12... up to 30 seconds).
                // The continuation number is passed in the query, so events resume from where they stopped regardless of the reconnect mechanism
                source.close();
                reconnecting.hidden = false;
                var delays = TIMING.reconnectDelaysMs;
                var delay = delays[Math.min(state.failures, delays.length - 1)];
                state.failures += 1;
                setTimeout(open, delay);
            };
        };

        var reload = panel.querySelector('[data-action="reload"]');
        if (reload) reload.addEventListener('click', function () { window.location.reload(); });
        open();
    }

    function rememberGeneration(generationId) {
        // Keep the generation ID in the URL so the same generation can be reopened after a reload
        if (!generationId || !window.history || !window.history.replaceState) return;
        var current = new URL(window.location.href);
        if (current.searchParams.get('generation') === generationId) return;
        current.searchParams.set('generation', generationId);
        window.history.replaceState(null, '', current.pathname + current.search);
    }

    function connectNewPanel() {
        // Restore the starting display also when only the notice about a running generation arrives
        if (submitting) {
            submitting = false;
            updateGenerateState();
        }
        var panel = document.getElementById('advanced-progress');
        if (!panel || panel.dataset.connected) return;
        connect(panel);
        var title = document.getElementById('advanced-progress-title');
        if (title) title.focus();
    }

    if (progressArea) {
        new MutationObserver(connectNewPanel).observe(progressArea, { childList: true });
        var existing = document.getElementById('advanced-progress');
        if (existing) connect(existing);
    }

    // --- Section D: save ---

    function updateSaveState(saveForm) {
        if (!saveForm) return;
        var ids = Array.from(saveForm.querySelectorAll('[data-testid="advanced-candidate-check"]:checked'))
            .map(function (box) { return box.value; });
        saveForm.querySelector('[data-role="persona-ids"]').value = ids.join(',');
        saveForm.querySelector('[data-testid="advanced-save-button"]').disabled = ids.length === 0;
        saveForm.querySelector('[data-testid="advanced-save-hint"]').textContent =
            ids.length === 0 ? '保存するペルソナを選んでください' : '';
    }

    updateEmptyNote();
    updateGenerateState();
})();
