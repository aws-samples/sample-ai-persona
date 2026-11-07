/**
 * PostEventSource: EventSource と同じ使い方で、POST の応答に流れる SSE を読む。
 *
 * AI を動かして保存する入口（議論・レポートの生成）は、CSRF の検査を受ける POST で
 * 始める（NFR4.13）。GET の EventSource だと、同じオリジンに置かれた <img src> や
 * リンクの遷移からも閲覧者の権限で始められてしまうため。
 *
 * - 本文は application/x-www-form-urlencoded、X-Requested-With を付けて送る
 *   （web/middleware.py の CSRF 検査を通る）
 * - EventSource と違い、切れても自動で再接続しない（再接続すると処理がもう一度走る）
 * - 応答が 2xx の text/event-stream でなければ（CSRF の拒否、ログイン切れの転送など）
 *   onerror を呼んで閉じる。ストリームが終わったときも onerror を呼んで閉じる
 *   （EventSource と同じ。呼び出し側は完了の印を見て無視する）
 */
(function () {
    'use strict';

    var CONNECTING = 0;
    var OPEN = 1;
    var CLOSED = 2;

    function PostEventSource(url, params) {
        this.url = url;
        this.readyState = CONNECTING;
        this.onopen = null;
        this.onmessage = null;
        this.onerror = null;
        this._listeners = {};
        this._controller = new AbortController();
        this._start(params);
    }

    PostEventSource.CONNECTING = CONNECTING;
    PostEventSource.OPEN = OPEN;
    PostEventSource.CLOSED = CLOSED;

    PostEventSource.prototype.addEventListener = function (type, listener) {
        (this._listeners[type] = this._listeners[type] || []).push(listener);
    };

    PostEventSource.prototype.close = function () {
        if (this.readyState === CLOSED) return;
        this.readyState = CLOSED;
        this._controller.abort();
    };

    PostEventSource.prototype._emit = function (type, event) {
        var handler = this['on' + type];
        if (typeof handler === 'function') handler.call(this, event);
        (this._listeners[type] || []).slice().forEach(function (listener) {
            listener.call(this, event);
        }, this);
    };

    PostEventSource.prototype._fail = function () {
        if (this.readyState === CLOSED) return;
        this.readyState = CLOSED;
        this._controller.abort();
        this._emit('error', { type: 'error' });
    };

    PostEventSource.prototype._start = function (params) {
        var self = this;
        var body = params instanceof URLSearchParams ? params : new URLSearchParams(params || {});
        fetch(this.url, {
            method: 'POST',
            headers: {
                'X-Requested-With': 'XMLHttpRequest',
                'Accept': 'text/event-stream',
            },
            body: body,
            credentials: 'same-origin',
            signal: this._controller.signal,
        }).then(function (response) {
            var type = response.headers.get('Content-Type') || '';
            if (!response.ok || !response.body || type.indexOf('text/event-stream') !== 0) {
                self._fail();
                return undefined;
            }
            if (self.readyState === CLOSED) return undefined;
            self.readyState = OPEN;
            self._emit('open', { type: 'open' });
            return self._read(response.body.getReader());
        }).catch(function () {
            self._fail();
        });
    };

    PostEventSource.prototype._read = function (reader) {
        var self = this;
        var decoder = new TextDecoder();
        var buffer = '';
        var eventName = '';
        var dataLines = [];

        function dispatch() {
            if (dataLines.length > 0 && self.readyState !== CLOSED) {
                self._emit(eventName || 'message', {
                    type: eventName || 'message',
                    data: dataLines.join('\n'),
                });
            }
            eventName = '';
            dataLines = [];
        }

        function handleLine(line) {
            if (line === '') { dispatch(); return; }
            if (line.charAt(0) === ':') return;  // コメント行
            var colon = line.indexOf(':');
            var field = colon === -1 ? line : line.slice(0, colon);
            var value = colon === -1 ? '' : line.slice(colon + 1);
            if (value.charAt(0) === ' ') value = value.slice(1);
            if (field === 'event') eventName = value;
            else if (field === 'data') dataLines.push(value);
        }

        function pump(result) {
            if (self.readyState === CLOSED) return undefined;
            if (result.done) {
                buffer += decoder.decode();
                if (buffer) handleLine(buffer.replace(/\r$/, ''));
                dispatch();
                self._fail();
                return undefined;
            }
            buffer += decoder.decode(result.value, { stream: true });
            var lines = buffer.split('\n');
            buffer = lines.pop();
            lines.forEach(function (line) { handleLine(line.replace(/\r$/, '')); });
            return reader.read().then(pump);
        }

        return reader.read().then(pump);
    };

    window.PostEventSource = PostEventSource;
})();
