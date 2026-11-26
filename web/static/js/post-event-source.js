/**
 * PostEventSource: reads SSE sent in a POST response, used like EventSource.
 *
 * Entry points that run AI and save the result (discussion and report generation) start with a POST that passes the CSRF check. With a GET EventSource, an <img src> or a link navigation from the same origin could start them with the viewer's privileges.
 *
 * - The body is application/x-www-form-urlencoded and X-Requested-With is set (passes the CSRF check in web/middleware.py)
 * - Unlike EventSource, it does not reconnect automatically on disconnect (reconnecting would run the work again)
 * - If the response is not a 2xx text/event-stream (CSRF rejection, redirect on login expiry, and so on), it calls onerror and closes. It also calls onerror and closes when the stream ends
 *   (same as EventSource; callers ignore it after seeing the completion marker)
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
            if (line.charAt(0) === ':') return;  // Comment line
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
