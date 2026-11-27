// AI Persona System - htmx JavaScript

// Do not use htmx's history cache (htmx-history-cache in localStorage).
// A copy of the screen would stay in the browser without distinguishing users, so after switching users
// "Back" would show the previous user's list. Saving is stopped by hx-history="false" in base.html,
// and copies left by earlier versions are removed here. Going back re-reads from the server.
try {
    localStorage.removeItem('htmx-history-cache');
} catch (e) {
    // Browsers without localStorage have no copy either
}

// htmx event handlers
document.body.addEventListener('htmx:afterSwap', function(evt) {
    // Apply the fade-in animation to new content
    if (evt.detail.target) {
        evt.detail.target.classList.add('fade-in');
    }
});

document.body.addEventListener('htmx:beforeRequest', function(evt) {
    // Handling at request start
    console.log('htmx request started:', evt.detail.pathInfo.requestPath);
});

document.body.addEventListener('htmx:afterRequest', function(evt) {
    // Handling at request completion
    console.log('htmx request completed:', evt.detail.pathInfo.requestPath);
});

// Toast instruction from the server (HX-Trigger: {"showToast": {...}}).
// Errors that a retry may resolve (ErrorKind.TRANSIENT) are shown as a toast
// without rewriting the screen, so the user's input is kept (Issue #117).
// htmx processes HX-Trigger before the swap decision, so this fires on 4xx too.
// The type accepts either the level of the toast contract or the type of the current toast_response().
document.body.addEventListener('showToast', function(evt) {
    const detail = evt.detail || {};
    showFlashMessage(
        detail.message || 'エラーが発生しました。',
        detail.level || detail.type || 'error'
    );
});

// Reflect non-2xx responses in the DOM (Issue #117).
// htmx 1.9.10 does not swap bodies of non-2xx responses, so an error message returned with 4xx/5xx
// never reaches the screen. Only responses where the server explicitly sets X-Render-Response: true
// are swapped.
// Status codes are not allowed wholesale because a generic error partial could flow into hx-target
// (main content or a list) and wipe out the whole form.
// The decision on what may be shown stays on the server side.
document.body.addEventListener('htmx:beforeSwap', function(evt) {
    const xhr = evt.detail.xhr;
    if (xhr && xhr.getResponseHeader('X-Render-Response') === 'true') {
        evt.detail.shouldSwap = true;
        evt.detail.isError = false;
    }
});

document.body.addEventListener('htmx:responseError', function(evt) {
    // Error handling
    console.error('htmx request error:', evt.detail);
    // When the server asks for a toast or for the body to be applied, the message appears through
    // that path, so a generic message is not stacked on top here
    const xhr = evt.detail.xhr;
    if (xhr && (xhr.getResponseHeader('HX-Trigger') ||
                xhr.getResponseHeader('X-Render-Response') === 'true')) {
        return;
    }
    // A failed dialog content load is reported inside the dialog with "内容を読み込めませんでした" and
    // "再読み込み" (Dialog). Do not stack a generic toast on top
    const elt = evt.detail.elt;
    if (elt && elt.matches && elt.matches('[data-dialog-content]')) {
        return;
    }
    showFlashMessage('エラーが発生しました。再度お試しください。', 'error');
});

// =============================================================
// Toast
// Success and info go into #toast-status (role="status") and disappear after 5 seconds. Counting stops while
// the pointer is over the toast or focus is inside it, and restarts from the remaining
// time when it leaves. Errors and warnings go into #toast-alert (role="alert") and stay until the
// close button is pressed. Showing a toast does not move focus.
// All text is inserted with textContent (responses and messages are never interpreted as HTML).
// =============================================================
const TOAST_DURATION_MS = 5000;
const SVG_NS = 'http://www.w3.org/2000/svg';

// Heroicons (outline) paths, same as components/icons.html, to match the success and error partials
const TOAST_STYLES = {
    success: {
        box: 'bg-green-50 border-green-200 text-green-800',
        icon: 'text-green-600',
        d: 'M9 12.75 11.25 15 15 9.75M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z'
    },
    error: {
        box: 'bg-red-50 border-red-200 text-red-800',
        icon: 'text-red-500',
        d: 'm9.75 9.75 4.5 4.5m0-4.5-4.5 4.5M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z'
    },
    warning: {
        box: 'bg-yellow-50 border-yellow-200 text-yellow-800',
        icon: 'text-yellow-600',
        d: 'M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126ZM12 15.75h.007v.008H12v-.008Z'
    },
    info: {
        box: 'bg-blue-50 border-blue-200 text-blue-800',
        icon: 'text-blue-600',
        d: 'm11.25 11.25.041-.02a.75.75 0 0 1 1.063.852l-.708 2.836a.75.75 0 0 0 1.063.853l.041-.021M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Zm-9-3.75h.008v.008H12V8.25Z'
    }
};

function createIcon(className, d) {
    const svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('class', className);
    svg.setAttribute('fill', 'none');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('stroke-width', '1.5');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('aria-hidden', 'true');
    const path = document.createElementNS(SVG_NS, 'path');
    path.setAttribute('stroke-linecap', 'round');
    path.setAttribute('stroke-linejoin', 'round');
    path.setAttribute('d', d);
    svg.appendChild(path);
    return svg;
}

function buildToast(message, style) {
    const toast = document.createElement('div');
    toast.dataset.toast = '';
    toast.dataset.testid = 'toast';
    // The container is pointer-events-none (so elements beneath stay usable),
    // so only the toast itself accepts clicks, which lets the close button be pressed.
    // shadow-lg overlaps the body content, so make the boundary easy to see.
    toast.className =
        `${style.box} border rounded-lg p-4 mb-2 fade-in shadow-lg pointer-events-auto`;

    const wrapper = document.createElement('div');
    wrapper.className = 'flex items-center justify-between gap-3';

    const body = document.createElement('div');
    body.className = 'flex items-center gap-2';
    body.appendChild(createIcon(`w-5 h-5 shrink-0 ${style.icon}`, style.d));
    const text = document.createElement('span');
    text.textContent = message;
    body.appendChild(text);

    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'shrink-0 text-gray-600 hover:text-gray-800';
    close.setAttribute('aria-label', '通知を閉じる');
    close.dataset.testid = 'toast-close-button';
    close.appendChild(createIcon('w-4 h-4', 'M6 18 18 6M6 6l12 12'));
    close.addEventListener('click', () => toast.remove());

    wrapper.appendChild(body);
    wrapper.appendChild(close);
    toast.appendChild(wrapper);
    return toast;
}

// One timer per element. When stopped, only the remaining time is kept.
function startToastCountdown(toast, duration) {
    let remaining = duration;
    let startedAt = 0;
    let timer = null;
    let hovered = false;
    let focused = false;
    const run = () => {
        if (timer !== null) return;
        startedAt = Date.now();
        timer = setTimeout(() => toast.remove(), remaining);
    };
    const pause = () => {
        if (timer === null) return;
        clearTimeout(timer);
        timer = null;
        remaining = Math.max(0, remaining - (Date.now() - startedAt));
    };
    const sync = () => (hovered || focused ? pause() : run());
    toast.addEventListener('mouseenter', () => { hovered = true; sync(); });
    toast.addEventListener('mouseleave', () => { hovered = false; sync(); });
    toast.addEventListener('focusin', () => { focused = true; sync(); });
    toast.addEventListener('focusout', (evt) => {
        if (toast.contains(evt.relatedTarget)) return;
        focused = false;
        sync();
    });
    run();
}

// Flash message display (called the same way as before)
function showFlashMessage(message, type = 'info') {
    const kind = TOAST_STYLES[type] ? type : 'info';
    const persistent = kind === 'error' || kind === 'warning';
    const container = document.getElementById(persistent ? 'toast-alert' : 'toast-status');
    if (!container) return;
    const toast = buildToast(String(message), TOAST_STYLES[kind]);
    container.appendChild(toast);
    if (!persistent) startToastCountdown(toast, TOAST_DURATION_MS);
}

// File name update
function updateFileName(input, targetId) {
    const fileName = input.files[0]?.name || '';
    const display = document.getElementById(targetId || 'selected-file');
    if (display) {
        display.textContent = fileName ? `選択中: ${fileName}` : '';
    }
}

// Drag-and-drop setup
function setupDragAndDrop(dropZoneSelector, fileInputSelector, targetId) {
    const dropZone = document.querySelector(dropZoneSelector);
    const fileInput = document.querySelector(fileInputSelector);
    
    if (!dropZone || !fileInput) return;
    
    ['dragenter', 'dragover', 'dragleave', 'drop'].forEach(eventName => {
        dropZone.addEventListener(eventName, preventDefaults, false);
    });
    
    function preventDefaults(e) {
        e.preventDefault();
        e.stopPropagation();
    }
    
    ['dragenter', 'dragover'].forEach(eventName => {
        dropZone.addEventListener(eventName, () => {
            dropZone.classList.add('border-blue-400', 'bg-blue-50');
        });
    });
    
    ['dragleave', 'drop'].forEach(eventName => {
        dropZone.addEventListener(eventName, () => {
            dropZone.classList.remove('border-blue-400', 'bg-blue-50');
        });
    });
    
    dropZone.addEventListener('drop', (e) => {
        const files = e.dataTransfer.files;
        if (files.length) {
            fileInput.files = files;
            updateFileName(fileInput, targetId);
        }
    });
}

// Confirmation dialog
function confirmAction(message) {
    return confirm(message);
}

// Local storage operations
const storage = {
    set: (key, value) => {
        try {
            localStorage.setItem(key, JSON.stringify(value));
        } catch (e) {
            console.error('localStorage error:', e);
        }
    },
    get: (key, defaultValue = null) => {
        try {
            const item = localStorage.getItem(key);
            return item ? JSON.parse(item) : defaultValue;
        } catch (e) {
            console.error('localStorage error:', e);
            return defaultValue;
        }
    },
    remove: (key) => {
        try {
            localStorage.removeItem(key);
        } catch (e) {
            console.error('localStorage error:', e);
        }
    }
};

// Local timezone conversion
function formatLocalTime(el) {
    const iso = el.getAttribute('datetime');
    if (!iso) return;
    const d = new Date(iso);
    if (isNaN(d)) return;
    const fmt = el.dataset.fmt || 'datetime';
    const opts = fmt === 'time'
        ? {hour: '2-digit', minute: '2-digit'}
        : fmt === 'date'
        ? {year: 'numeric', month: '2-digit', day: '2-digit'}
        : {year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit'};
    el.textContent = d.toLocaleString('ja-JP', opts);
}

function convertAllTimes(root) {
    (root || document).querySelectorAll('time[datetime]').forEach(formatLocalTime);
}

// =============================================================
// Persona avatar (DiceBear notionists)
// Works only on pages that loaded the DiceBear bundle. Falls back to the initial when not loaded.
// =============================================================

// Cache of generated SVGs keyed by seed+size (avoids regenerating within the same screen)
const _personaAvatarCache = {};

/**
 * Generate and return a DiceBear avatar SVG from the persona ID (seed).
 * Returns null when DiceBear/DOMPurify is not loaded or generation fails (the caller falls back to the initial).
 */
function personaAvatarSvg(seed, size) {
    try {
        if (!window.DiceBear || !window.DiceBear.createAvatar) return null;
        const key = String(seed || '') + '@' + (size || 64);
        if (_personaAvatarCache[key] !== undefined) return _personaAvatarCache[key];
        const raw = window.DiceBear.createAvatar(window.DiceBear.styles.notionists, {
            seed: String(seed || ''),
            size: size || 64,
        }).toString();
        const clean = window.DOMPurify
            ? window.DOMPurify.sanitize(raw, { USE_PROFILES: { svg: true, svgFilters: true } })
            : null;
        _personaAvatarCache[key] = clean;
        return clean;
    } catch (e) {
        return null;
    }
}
window.personaAvatarSvg = personaAvatarSvg;

// URLs of uploaded icon images (persona ID -> URL).
// Collected from elements where the server outputs data-avatar-url. Elements without
// persona info, such as message rows, can look up URLs registered on the same page.
const _personaAvatarUrls = {};

/**
 * Register the icon image URL of a persona. An empty string means "no image (auto avatar)" and removes the registration.
 * Only same-origin relative paths are accepted (a server-generated /persona/{id}/avatar is expected).
 */
function registerPersonaAvatarUrl(seed, url) {
    const key = String(seed || '');
    if (!key) return;
    if (url && url.charAt(0) === '/' && url.charAt(1) !== '/') {
        _personaAvatarUrls[key] = url;
    } else {
        delete _personaAvatarUrls[key];
    }
}
window.registerPersonaAvatarUrl = registerPersonaAvatarUrl;

function _escapeAttr(value) {
    return String(value).replace(/&/g, '&amp;').replace(/"/g, '&quot;')
        .replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

/**
 * Return the content of an avatar frame (HTML string).
 * An <img> if an icon image is registered, otherwise a DiceBear SVG, otherwise null.
 * If the <img> fails to load, the error listener below falls back to DiceBear.
 */
function personaAvatarInner(seed, size) {
    const url = _personaAvatarUrls[String(seed || '')];
    if (url) {
        return `<img src="${_escapeAttr(url)}" alt="" loading="lazy" decoding="async"`
            + ` class="persona-avatar-photo" data-avatar-fallback-seed="${_escapeAttr(seed)}"`
            + ` data-avatar-fallback-size="${_escapeAttr(size || 64)}">`;
    }
    return personaAvatarSvg(seed, size);
}
window.personaAvatarInner = personaAvatarInner;

// When an icon image fails to load (deleted, expired, and so on), fall back to DiceBear.
// error does not bubble, so it is received in the capture phase.
document.addEventListener('error', function(evt) {
    const img = evt.target;
    if (!(img instanceof HTMLImageElement) || !img.classList.contains('persona-avatar-photo')) return;
    const seed = img.dataset.avatarFallbackSeed || '';
    const size = parseInt(img.dataset.avatarFallbackSize || '64', 10);
    registerPersonaAvatarUrl(seed, '');
    const svg = personaAvatarSvg(seed, size);
    if (svg) {
        img.outerHTML = svg; // Already sanitized by DOMPurify inside personaAvatarSvg
    } else {
        img.remove();
    }
}, true);

/**
 * Render a single avatar frame. Image if there is an icon image, otherwise a DiceBear SVG,
 * and if neither is usable, an initial on a colored circle.
 * el: element with data-avatar-seed / data-avatar-name / data-avatar-color (and optionally data-avatar-url).
 */
function fillPersonaAvatar(el) {
    if (!el || el.dataset.avatarFilled) return;
    const seed = el.dataset.avatarSeed || '';
    const size = parseInt(el.dataset.avatarSize || '48', 10);
    // Decorative image. The name is read from the adjacent text, so hide it from screen readers
    el.setAttribute('aria-hidden', 'true');
    const inner = personaAvatarInner(seed, size);
    if (inner) {
        el.innerHTML = inner; // <img> attributes are escaped and the SVG is sanitized by DOMPurify
        el.classList.add('persona-avatar-img');
    } else {
        // Fallback: initial + existing color classes
        const name = el.dataset.avatarName || '';
        const color = el.dataset.avatarColor || 'blue';
        const initial = name ? name.charAt(0) : '';
        el.classList.add('persona-avatar-' + color);
        el.textContent = initial;
    }
    el.dataset.avatarFilled = '1';
}

// Generate only the avatar frames that enter the viewport (avoids blocking from generating many in a list)
let _avatarObserver = null;
function getAvatarObserver() {
    if (_avatarObserver || typeof IntersectionObserver === 'undefined') return _avatarObserver;
    _avatarObserver = new IntersectionObserver((entries, obs) => {
        entries.forEach(entry => {
            if (entry.isIntersecting) {
                fillPersonaAvatar(entry.target);
                obs.unobserve(entry.target);
            }
        });
    }, { rootMargin: '200px' });
    return _avatarObserver;
}

/**
 * Put unprocessed avatar frames [data-avatar-seed] under root on lazy generation.
 * Falls back to immediate generation where IntersectionObserver is unsupported.
 * Elements with data-avatar-eager are generated immediately (for places where a viewport-based IntersectionObserver cannot judge visibility, such as inside an inner scroll container).
 */
function renderPersonaAvatars(root) {
    const scope = root || document;
    // Before drawing, register the image URLs in scope (so message rows of the same persona can look them up)
    const withUrl = scope.querySelectorAll ? scope.querySelectorAll('[data-avatar-seed][data-avatar-url]') : [];
    withUrl.forEach(el => registerPersonaAvatarUrl(el.dataset.avatarSeed, el.dataset.avatarUrl));
    const els = scope.querySelectorAll('[data-avatar-seed]:not([data-avatar-filled])');
    const obs = getAvatarObserver();
    els.forEach(el => {
        if (el.dataset.avatarEager !== undefined) {
            fillPersonaAvatar(el);  // Immediate generation (cheap thanks to the cache)
        } else if (obs) {
            obs.observe(el);
        } else {
            fillPersonaAvatar(el);
        }
    });
}
window.renderPersonaAvatars = renderPersonaAvatars;

// Also handle elements added dynamically by htmx
document.body.addEventListener('htmx:afterSwap', function(evt) {
    convertAllTimes(evt.detail.target);
    renderPersonaAvatars(evt.detail.target);
});

// =============================================================
// Shared screen components
// Each group registers once on document and receives events by delegation
// (so it also works on elements swapped by htmx). Groups do not call each other's functions
// directly; they are connected only by events (dialogClose, listReload, showToast, dialog:closed).
// Each registration is wrapped in try so one failure does not stop the others.
// =============================================================
function registerBlock(name, register) {
    try {
        register();
    } catch (e) {
        console.error(`[${name}] の登録に失敗しました`, e);
    }
}

function isShown(el) {
    return !!el && el.getClientRects().length > 0;
}

// -------------------------------------------------------------
// NavMenu
// The open state stays with Alpine (openMenu) in base.html. This adds only key handling
// and "close when focus leaves". nav is the data-nav-menu element.
// -------------------------------------------------------------
registerBlock('NavMenu', function() {
    const BREAKPOINT = window.matchMedia('(min-width: 640px)');

    const stateOf = (nav) => (window.Alpine && nav ? window.Alpine.$data(nav) : null);
    const listOf = (toggle) => document.getElementById(toggle.getAttribute('aria-controls'));
    const itemsOf = (toggle) => {
        const list = listOf(toggle);
        return list ? Array.from(list.querySelectorAll('[data-nav-item]')).filter(isShown) : [];
    };
    const openToggleOf = (nav) => {
        const state = stateOf(nav);
        if (!state || !state.openMenu) return null;
        return Array.from(nav.querySelectorAll('[data-nav-toggle]'))
            .find((t) => t.dataset.navToggle === state.openMenu) || null;
    };
    const isWithin = (toggle, node) => {
        const list = listOf(toggle);
        return toggle.contains(node) || (!!list && list.contains(node));
    };

    function focusFirstItem(toggle, attempt = 0) {
        // Do nothing if it was closed while waiting
        if (openToggleOf(toggle.closest('[data-nav-menu]')) !== toggle) return;
        const first = itemsOf(toggle)[0];
        if (first) {
            first.focus();
        } else if (attempt < 5) {
            requestAnimationFrame(() => focusFirstItem(toggle, attempt + 1));
        }
    }

    function open(toggle) {
        const state = stateOf(toggle.closest('[data-nav-menu]'));
        if (!state) return;
        state.openMenu = toggle.dataset.navToggle;
        window.Alpine.nextTick(() => focusFirstItem(toggle));
    }

    function close(nav, returnFocus) {
        const toggle = openToggleOf(nav);
        if (!toggle) return;
        stateOf(nav).openMenu = null;
        if (returnFocus) toggle.focus();
    }

    function onToggleKey(evt, toggle) {
        const nav = toggle.closest('[data-nav-menu]');
        if (evt.key === 'ArrowDown') {
            evt.preventDefault();
            open(toggle);
        } else if (evt.key === 'Enter' || evt.key === ' ') {
            // Stop the conversion to a click and, when opening, move to the first item (mouse clicks stay with Alpine)
            evt.preventDefault();
            if (openToggleOf(nav) === toggle) {
                close(nav, false);
            } else {
                open(toggle);
            }
        } else if (evt.key === 'Escape') {
            close(nav, true);
        }
    }

    function onItemKey(evt, item) {
        const nav = item.closest('[data-nav-menu]');
        const toggle = openToggleOf(nav);
        if (!toggle || !isWithin(toggle, item)) return;
        if (evt.key === 'Escape') {
            evt.preventDefault();
            close(nav, true);
            return;
        }
        if (evt.key !== 'ArrowDown' && evt.key !== 'ArrowUp') return;
        evt.preventDefault();
        const items = itemsOf(toggle);
        const step = evt.key === 'ArrowDown' ? 1 : -1;
        const next = (items.indexOf(item) + step + items.length) % items.length;
        items[next].focus();
    }

    document.addEventListener('keydown', function(evt) {
        const target = evt.target instanceof Element ? evt.target : null;
        if (!target || !target.closest('[data-nav-menu]')) return;
        const toggle = target.closest('[data-nav-toggle]');
        if (toggle) {
            onToggleKey(evt, toggle);
            return;
        }
        const item = target.closest('[data-nav-item]');
        if (item) onItemKey(evt, item);
    });

    // Space becomes a click on key release, so stop it here as well
    document.addEventListener('keyup', function(evt) {
        if (evt.key !== ' ' || !(evt.target instanceof Element)) return;
        if (evt.target.closest('[data-nav-menu] [data-nav-toggle]')) evt.preventDefault();
    });

    // Close when focus leaves the button and items (Tab and so on). Focus is not moved
    document.addEventListener('focusin', function(evt) {
        document.querySelectorAll('[data-nav-menu]').forEach((nav) => {
            const toggle = openToggleOf(nav);
            if (toggle && !isWithin(toggle, evt.target)) close(nav, false);
        });
    });

    // Close on an outside click. Alpine handles the open button's click first
    document.addEventListener('click', function(evt) {
        document.querySelectorAll('[data-nav-menu]').forEach((nav) => {
            const toggle = openToggleOf(nav);
            if (toggle && !isWithin(toggle, evt.target)) close(nav, false);
        });
    });

    // Close if the width crosses 640px while open
    BREAKPOINT.addEventListener('change', function() {
        document.querySelectorAll('[data-nav-menu]').forEach((nav) => close(nav, false));
    });
});

// -------------------------------------------------------------
// Dialog
// Opens and closes the [data-dialog] rendered by dialog() in components/dialog.html.
// Only this code owns the open state, expressed with the hidden attribute. While open, inert is put on the
// siblings of the ancestors from the dialog up to <body> (not on the dialog itself, its ancestors, or the toast
// container; elements already inert are not remembered and are not restored on close).
// Dialogs are not stacked. A request to open closes the earlier dialog first.
// -------------------------------------------------------------
registerBlock('Dialog', function() {
    const FOCUSABLE = 'a[href], button, input, select, textarea, [tabindex]';
    const FIELDS = 'input:not([type="hidden"]), select, textarea';
    const SUBMITS = 'button[type="submit"], input[type="submit"]';

    // Open dialog: { el, opener, inerted, disabled, submitting, promise, resolve }
    let current = null;

    const panelOf = (el) => el.querySelector('[data-dialog-panel]');
    const contentOf = (el) => el.querySelector('[data-dialog-content]');
    const isContentLoader = (node) => !!node && !!node.matches && node.matches('[data-dialog-content]');
    const isUsable = (node) => isShown(node) && !node.disabled && !node.closest('[inert]');
    const insideOpenPanel = (node) =>
        !!current && !!node && !isContentLoader(node) && panelOf(current.el).contains(node);

    function focusables(el) {
        return Array.from(panelOf(el).querySelectorAll(FOCUSABLE))
            .filter((node) => node.tabIndex >= 0 && isUsable(node));
    }

    // Default focus: [data-dialog-initial-focus] ([キャンセル] for confirmations of irreversible actions)
    // -> first input -> heading
    function focusInitial(el) {
        const panel = panelOf(el);
        const preferred = Array.from(panel.querySelectorAll('[data-dialog-initial-focus]')).find(isUsable);
        const field = preferred || Array.from(panel.querySelectorAll(FIELDS)).find(isUsable);
        const target = field || document.getElementById(`${el.id}-title`);
        if (target) target.focus();
    }

    // --- Inert background ---
    function inertBackground(el) {
        const toasts = document.getElementById('flash-messages');
        const added = [];
        const mark = (node) => {
            if (node === toasts || node.hasAttribute('inert')) return;
            if (toasts && node.contains(toasts)) {
                Array.from(node.children).forEach(mark);
                return;
            }
            node.setAttribute('inert', '');
            added.push(node);
        };
        for (let node = el; node !== document.body && node.parentElement; node = node.parentElement) {
            Array.from(node.parentElement.children).forEach((sibling) => {
                if (sibling !== node) mark(sibling);
            });
        }
        return added;
    }

    function releaseInert(state) {
        state.inerted.forEach((node) => node.removeAttribute('inert'));
        state.inerted = [];
    }

    // --- Saving ---
    // Disable the save buttons (the submit button and the button that issued the request)
    function startSubmitting(state, requester) {
        state.submitting = true;
        const buttons = Array.from(panelOf(state.el).querySelectorAll(SUBMITS));
        if (requester instanceof HTMLButtonElement) buttons.push(requester);
        buttons.forEach((button) => {
            if (button.disabled) return;
            button.disabled = true;
            state.disabled.push(button);
        });
    }

    function endSubmitting(state) {
        state.submitting = false;
        state.disabled.forEach((button) => { button.disabled = false; });
        state.disabled = [];
    }

    // --- Loading content ---
    function setBusy(el, busy) {
        const panel = panelOf(el);
        if (busy) {
            panel.setAttribute('aria-busy', 'true');
        } else {
            panel.removeAttribute('aria-busy');
        }
    }

    function loadContent(el) {
        const content = contentOf(el);
        const loading = document.createElement('p');
        loading.className = 'text-sm text-gray-600';
        loading.textContent = '読み込み中...';
        content.replaceChildren(loading);
        content.dataset.dialogLoading = '';
        setBusy(el, true);
        const title = document.getElementById(`${el.id}-title`);
        if (title) title.focus();
        htmx.trigger(content, 'dialogLoad');
    }

    function finishLoad(content) {
        delete content.dataset.dialogLoading;
        const el = content.closest('[data-dialog]');
        setBusy(el, false);
        if (current && current.el === el) focusInitial(el);
    }

    // The failure text and "再読み込み" are fixed strings. The response body, status code, and URL are not used for display
    function buildLoadFailure() {
        const box = document.createElement('div');
        box.className = 'bg-red-50 border border-red-200 rounded-lg p-4';
        box.setAttribute('role', 'alert');
        const message = document.createElement('p');
        message.className = 'text-red-800 text-sm';
        message.textContent = '内容を読み込めませんでした';
        const reload = document.createElement('button');
        reload.type = 'button';
        reload.dataset.dialogReload = '';
        reload.dataset.testid = 'dialog-reload-button';
        reload.className = 'mt-3 text-sm text-red-700 hover:text-red-900 underline';
        reload.textContent = '再読み込み';
        box.appendChild(message);
        box.appendChild(reload);
        return box;
    }

    function failLoad(content) {
        delete content.dataset.dialogLoading;
        const el = content.closest('[data-dialog]');
        setBusy(el, false);
        content.replaceChildren(buildLoadFailure());
        if (current && current.el === el) content.querySelector('[data-dialog-reload]').focus();
    }

    // --- Open and close ---
    function open(el, opener) {
        if (!el || !el.hasAttribute('data-dialog')) return Promise.resolve(null);
        if (current && current.el === el) return current.promise;
        if (current) close(null, { restoreFocus: false });
        const state = { el, opener: opener || null, inerted: [], disabled: [], submitting: false };
        state.promise = new Promise((resolve) => { state.resolve = resolve; });
        current = state;
        el.hidden = false;
        state.inerted = inertBackground(el);
        if (contentOf(el)) {
            loadContent(el);
        } else {
            focusInitial(el);
        }
        return state.promise;
    }

    function returnFocus(state, removing) {
        const opener = state.opener;
        const gone = !opener || !opener.isConnected || (removing && removing.contains(opener));
        if (!gone && isUsable(opener)) {
            opener.focus();
            return;
        }
        const main = document.getElementById('main');
        if (main) main.focus();
    }

    function close(value, { restoreFocus = true, removing = null } = {}) {
        const state = current;
        if (!state) return;
        current = null;
        // Remove inert first, because leftover inert makes the whole screen unusable
        releaseInert(state);
        endSubmitting(state);
        state.el.hidden = true;
        if (restoreFocus) returnFocus(state, removing);
        state.el.dispatchEvent(new CustomEvent('dialog:closed', {
            bubbles: true,
            detail: { id: state.el.id, value: value }
        }));
        if (state.el.hasAttribute('data-dialog-remove-on-close')) state.el.remove();
        state.resolve(value);
    }

    // Open from JavaScript (such as the interview save name). On close, it resolves with the pressed button's
    // data-dialog-close value (null for Escape or x)
    window.openDialog = function(id) {
        return open(document.getElementById(id), document.activeElement);
    };

    // --- Events ---
    document.addEventListener('click', function(evt) {
        const target = evt.target instanceof Element ? evt.target : null;
        if (!target) return;
        const opener = target.closest('[data-dialog-open]');
        if (opener) {
            open(document.getElementById(opener.dataset.dialogOpen), opener);
            return;
        }
        if (!current || !current.el.contains(target)) return;
        const closer = target.closest('[data-dialog-close]');
        if (closer) {
            if (!current.submitting) close(closer.dataset.dialogClose || null);
            return;
        }
        if (target.closest('[data-dialog-reload]')) {
            loadContent(current.el);
            return;
        }
        if (target.matches('[data-dialog-backdrop]') && !current.submitting &&
                current.el.hasAttribute('data-dialog-backdrop-close')) {
            close(null);
        }
    });

    document.addEventListener('keydown', function(evt) {
        if (!current) return;
        if (evt.key === 'Escape') {
            evt.preventDefault();
            if (!current.submitting) close(null);
            return;
        }
        if (evt.key !== 'Tab') return;
        // Cycle order: inside the panel -> the toast close button (if any) -> back to the start of the panel.
        // Make error and warning toasts, which do not close on their own, reachable by keyboard alone
        const items = focusables(current.el).concat(toastCloseButtons());
        if (!items.length) {
            evt.preventDefault();
            return;
        }
        const active = document.activeElement;
        const index = items.indexOf(active);
        if (index === -1 && panelOf(current.el).contains(active) && !evt.shiftKey) {
            return; // From elements that are not in the cycle, such as headings, follow the browser's order to the next
        }
        evt.preventDefault();
        const step = evt.shiftKey ? -1 : 1;
        const from = index === -1 ? (evt.shiftKey ? 0 : -1) : index;
        items[(from + step + items.length) % items.length].focus();
    });

    function toastCloseButtons() {
        const toasts = document.getElementById('flash-messages');
        if (!toasts) return [];
        return Array.from(toasts.querySelectorAll('[data-toast] button')).filter(isUsable);
    }

    // Last focused element inside the panel (where to return after closing a toast)
    document.addEventListener('focusin', function(evt) {
        if (current && panelOf(current.el).contains(evt.target)) current.lastFocus = evt.target;
    });

    // When a toast is closed while the dialog stays open, return focus to the dialog.
    // The toast element disappears on its own click, so detect it before that (capture phase)
    document.addEventListener('click', function(evt) {
        if (!current || !(evt.target instanceof Element)) return;
        if (!evt.target.closest('#flash-messages [data-toast] button')) return;
        const state = current;
        setTimeout(() => {
            if (current !== state || panelOf(state.el).contains(document.activeElement)) return;
            const back = state.lastFocus;
            if (back && back.isConnected && isUsable(back)) {
                back.focus();
            } else {
                focusInitial(state.el);
            }
        }, 0);
    }, true);

    // The server reports a successful save (HX-Trigger: {"dialogClose": {"id": "<id>"}})
    document.body.addEventListener('dialogClose', function(evt) {
        const detail = evt.detail || {};
        const id = detail.id || detail.value;
        if (current && (!id || current.el.id === id)) close(null);
    });

    document.body.addEventListener('htmx:beforeRequest', function(evt) {
        if (insideOpenPanel(evt.detail.elt)) startSubmitting(current, evt.detail.elt);
    });

    document.body.addEventListener('htmx:afterRequest', function(evt) {
        if (!insideOpenPanel(evt.detail.elt)) return;
        const state = current;
        endSubmitting(state);
        if (evt.detail.successful && state.el.hasAttribute('data-dialog-close-on-success')) {
            close(null);
        }
    });

    ['htmx:responseError', 'htmx:sendError', 'htmx:timeout'].forEach(function(name) {
        document.body.addEventListener(name, function(evt) {
            const elt = evt.detail && evt.detail.elt;
            if (isContentLoader(elt) && 'dialogLoading' in elt.dataset) failLoad(elt);
        });
    });

    document.body.addEventListener('htmx:afterSettle', function(evt) {
        const root = (evt.detail && evt.detail.elt) || evt.target;
        if (!(root instanceof Element)) return;
        if (isContentLoader(root) && 'dialogLoading' in root.dataset) {
            finishLoad(root);
            return;
        }
        // Dialogs inserted by htmx (such as dataset editing) open when inserted
        const inserted = root.matches('[data-dialog-autoopen][hidden]')
            ? root
            : root.querySelector('[data-dialog-autoopen][hidden]');
        if (!inserted) return;
        const config = evt.detail && evt.detail.requestConfig;
        open(inserted, (config && config.elt) || document.activeElement);
    });

    // Also remove inert when the open dialog (or an ancestor) is removed by an htmx swap
    document.body.addEventListener('htmx:beforeCleanupElement', function(evt) {
        const removing = evt.target;
        if (!current || !(removing instanceof Element)) return;
        if (removing === current.el || removing.contains(current.el)) {
            close(null, { removing: removing });
        }
    });

    // When navigating away, do not save to history with inert left on
    const dismiss = () => close(null, { restoreFocus: false });
    document.body.addEventListener('htmx:beforeHistorySave', dismiss);
    window.addEventListener('pagehide', dismiss);
});

// -------------------------------------------------------------
// ListTabs
// Left/right arrows only move focus (no request). Enter is the link's default action,
// and Space becomes a click here to switch via htmx. During the switch the list part gets
// aria-busy, then the selected state moves and the count is announced. Focus stays on the tab.
// -------------------------------------------------------------
registerBlock('ListTabs', function() {
    const TAB = '[data-list-tabs] [role="tab"]';
    const SELECTED = ['border-blue-600', 'font-semibold', 'text-gray-900'];
    const UNSELECTED = ['border-transparent', 'text-gray-600', 'hover:text-gray-900'];
    const tabsOf = (tab) => Array.from(tab.closest('[role="tablist"]').querySelectorAll('[role="tab"]'));

    function select(tab) {
        tabsOf(tab).forEach((other) => {
            const on = other === tab;
            other.setAttribute('aria-selected', on ? 'true' : 'false');
            other.tabIndex = on ? 0 : -1;
            SELECTED.forEach((name) => other.classList.toggle(name, on));
            UNSELECTED.forEach((name) => other.classList.toggle(name, !on));
        });
        const panel = document.getElementById(tab.getAttribute('aria-controls'));
        if (panel) {
            panel.setAttribute('aria-labelledby', tab.id);
            panel.setAttribute('data-reload-url', tab.getAttribute('hx-get'));
        }
        const status = tab.closest('[data-list-tabs]').querySelector('[data-list-tabs-status]');
        if (status && status.textContent !== tab.dataset.countLabel) {
            status.textContent = tab.dataset.countLabel || '';
        }
    }

    document.addEventListener('keydown', function(evt) {
        const tab = evt.target instanceof Element ? evt.target.closest(TAB) : null;
        if (!tab) return;
        if (evt.key === 'ArrowRight' || evt.key === 'ArrowLeft') {
            evt.preventDefault();
            const tabs = tabsOf(tab);
            const step = evt.key === 'ArrowRight' ? 1 : -1;
            const next = tabs[(tabs.indexOf(tab) + step + tabs.length) % tabs.length];
            tabs.forEach((other) => { other.tabIndex = other === next ? 0 : -1; });
            next.focus();
        } else if (evt.key === ' ') {
            evt.preventDefault();
            tab.click();
        }
    });

    // Remember the last request issued per list part and do not swap in responses older than that.
    // Tabs abort the previous request with hx-sync, but a listReload re-read (htmx.ajax) is
    // a separate request from tabs, so a late re-read is discarded here so it cannot overwrite the new tab.
    // This is registered after the X-Render-Response allowance (htmx:beforeSwap above), so this one wins.
    const latest = new WeakMap();
    const PANEL = '[data-list-tabpanel]';

    document.body.addEventListener('htmx:beforeRequest', function(evt) {
        const panel = evt.detail.target;
        if (panel instanceof Element && panel.matches(PANEL)) latest.set(panel, evt.detail.xhr);
    });

    document.body.addEventListener('htmx:beforeSwap', function(evt) {
        const panel = evt.detail.target;
        if (!(panel instanceof Element) || !panel.matches(PANEL)) return;
        if (latest.has(panel) && latest.get(panel) !== evt.detail.xhr) evt.detail.shouldSwap = false;
    });

    document.body.addEventListener('htmx:afterSwap', function(evt) {
        const config = evt.detail.requestConfig;
        const tab = config && config.elt;
        if (tab instanceof Element && tab.matches(TAB)) select(tab);
    });
});

// -------------------------------------------------------------
// BusyRegion
// While htmx swaps or appends a list, set aria-busy="true" on that list.
// Targets are the tab list part ([data-list-tabpanel]) and the list that receives
// the "show more" row ([data-busy-target]).
// -------------------------------------------------------------
registerBlock('BusyRegion', function() {
    const BUSY = '[data-list-tabpanel], [data-busy-target]';
    const isBusyRegion = (node) => node instanceof Element && node.matches(BUSY);
    // Each list holds its in-flight requests. aria-busy is not removed until the last one finishes
    // (so pressing tabs in succession does not clear "loading" when an earlier request finishes first)
    const pending = new WeakMap();

    function finish(evt) {
        const region = evt.detail.target;
        if (!isBusyRegion(region)) return;
        const requests = pending.get(region);
        if (requests) requests.delete(evt.detail.xhr);
        if (!requests || requests.size === 0) region.removeAttribute('aria-busy');
    }

    document.body.addEventListener('htmx:beforeRequest', function(evt) {
        const region = evt.detail.target;
        if (!isBusyRegion(region)) return;
        if (!pending.has(region)) pending.set(region, new Set());
        pending.get(region).add(evt.detail.xhr);
        region.setAttribute('aria-busy', 'true');
    });

    ['htmx:afterRequest', 'htmx:sendAbort'].forEach((name) => {
        document.body.addEventListener(name, finish);
    });

    // Do not save to history with the loading mark left on (it would look like loading when going back)
    document.body.addEventListener('htmx:beforeHistorySave', function() {
        document.querySelectorAll(`[aria-busy]:is(${BUSY})`)
            .forEach((region) => region.removeAttribute('aria-busy'));
    });
});

// -------------------------------------------------------------
// ListReload
// On HX-Trigger: {"listReload": {"target": "#<id>"}}, re-read the data-reload-url of that element itself
// (a URL rendered by the server that includes the current tab).
// Only target is read from the event value; other fields such as a URL are not used.
// target is not evaluated as a selector; only characters usable as an id are accepted.
// If the same response has dialogClose, the re-read is deferred to the next task so it runs after closing.
// -------------------------------------------------------------
registerBlock('ListReload', function() {
    const TARGET = /^#[A-Za-z0-9_-]+$/;

    function reload(target) {
        if (typeof target !== 'string' || !TARGET.test(target)) {
            console.warn('listReload: target の形が正しくないため読み直しません');
            return;
        }
        const panel = document.getElementById(target.slice(1));
        const url = panel ? panel.getAttribute('data-reload-url') : null;
        if (!url) {
            console.warn('listReload: 読み直す一覧が画面にありません');
            return;
        }
        htmx.ajax('GET', url, { target: panel, swap: 'innerHTML' });
    }

    // Either {"target": "#id"} or {"targets": ["#a", "#b"]}. targets points at regions that differ per screen
    // (such as a list and a detail), so those not on this screen are skipped silently.
    function present(target) {
        return typeof target === 'string' && TARGET.test(target)
            && document.getElementById(target.slice(1)) !== null;
    }

    document.body.addEventListener('listReload', function(evt) {
        const detail = evt.detail || {};
        if (Array.isArray(detail.targets)) {
            const targets = detail.targets.filter(present);
            setTimeout(() => targets.forEach(reload), 0);
            return;
        }
        setTimeout(() => reload(detail.target), 0);
    });
});

// -------------------------------------------------------------
// BulkSelect
// The action bar [data-bulk-select] and row checkboxes [data-bulk-row] sit in the same list.
// "Select all" is unchecked at 0 selected, indeterminate for some, and checked for all. Pressing it selects
// all when unchecked or indeterminate, and clears all when all are selected. Rows added by "show more" join unselected.
// -------------------------------------------------------------
registerBlock('BulkSelect', function() {
    // Walk up from the bar and use the rows of the first ancestor that contains rows
    function rowsOf(bar) {
        for (let scope = bar.parentElement; scope; scope = scope.parentElement) {
            const rows = scope.querySelectorAll('[data-bulk-row]');
            if (rows.length) return Array.from(rows);
        }
        return [];
    }

    function barOf(row) {
        for (let scope = row.parentElement; scope; scope = scope.parentElement) {
            const bar = scope.querySelector('[data-bulk-select]');
            if (bar) return bar;
        }
        return null;
    }

    function update(bar) {
        const rows = rowsOf(bar);
        const selected = rows.filter((row) => row.checked).length;
        const all = bar.querySelector('[data-bulk-all]');
        all.checked = rows.length > 0 && selected === rows.length;
        all.indeterminate = selected > 0 && selected < rows.length;
        const action = bar.querySelector('[data-bulk-action]');
        if (action) action.disabled = selected === 0;
        const count = bar.querySelector('[data-bulk-count]');
        const text = `${selected} 件を選択中`;
        if (count && count.textContent.trim() !== text) count.textContent = text;
    }

    document.addEventListener('change', function(evt) {
        const input = evt.target;
        if (!(input instanceof HTMLInputElement)) return;
        if (input.matches('[data-bulk-all]')) {
            const bar = input.closest('[data-bulk-select]');
            const rows = rowsOf(bar);
            const selectAll = !(rows.length > 0 && rows.every((row) => row.checked));
            rows.forEach((row) => { row.checked = selectAll; });
            update(bar);
        } else if (input.matches('[data-bulk-row]')) {
            const bar = barOf(input);
            if (bar) update(bar);
        }
    });

    // Recount when htmx adds or replaces rows
    document.body.addEventListener('htmx:afterSettle', function() {
        document.querySelectorAll('[data-bulk-select]').forEach(update);
    });
});

// -------------------------------------------------------------
// DepartmentPicker
// A department that starts selected or unselected toggles on each press (browser default). A department
// that starts intermediate cycles selected -> unselected -> intermediate (original), and while intermediate
// its department id is sent through the {name}_unchanged hidden input (the browser does not send an intermediate checkbox).
// Saving is disabled until something changes, and groups that will be removed are announced with their count.
// It does not know about Dialog (even when used inside one, this code only talks to the form).
// -------------------------------------------------------------
registerBlock('DepartmentPicker', function() {
    const PICKER = '[data-department-picker]';
    const NEXT = { mixed: 'checked', checked: 'unchecked', unchecked: 'mixed' };
    const optionsOf = (picker) => Array.from(picker.querySelectorAll('[data-department-option]'));
    const isOption = (node) => node instanceof HTMLInputElement && node.matches('[data-department-option]');

    function stateOf(option) {
        if (option.dataset.initial === 'mixed') return option.dataset.state || 'mixed';
        return option.checked ? 'checked' : 'unchecked';
    }

    function syncUnchanged(option, picker) {
        let hidden = option.parentElement.querySelector('input[data-department-unchanged]');
        if (stateOf(option) === 'mixed') {
            if (hidden) return;
            hidden = document.createElement('input');
            hidden.type = 'hidden';
            hidden.name = `${picker.dataset.name}_unchanged`;
            hidden.value = option.value;
            hidden.dataset.departmentUnchanged = '';
            option.after(hidden);
        } else if (hidden) {
            hidden.remove();
        }
    }

    function applyState(option, state, picker) {
        option.dataset.state = state;
        option.checked = state === 'checked';
        option.indeterminate = state === 'mixed';
        syncUnchanged(option, picker);
    }

    function removingText(picker) {
        const removed = optionsOf(picker)
            .filter((o) => o.dataset.initial !== 'unchecked' && stateOf(o) === 'unchecked')
            .map((o) => `${o.dataset.departmentName}（${o.dataset.sharedCount} 件）`);
        if (!removed.length) return '';
        const lead = picker.dataset.mode === 'scope'
            ? '保存すると利用範囲から外れるグループ'
            : '保存すると共有が外れるグループ';
        return `${lead}: ${removed.join('、')}`;
    }

    function saveButtonsOf(picker) {
        // A section that saves more than the departments keeps its own button state
        if ('departmentFree' in picker.dataset) return [];
        const form = picker.form || picker.closest('form');
        return form ? Array.from(form.querySelectorAll('button[type="submit"], input[type="submit"]')) : [];
    }

    function refresh(picker) {
        const options = optionsOf(picker);
        const changed = options.some((o) => stateOf(o) !== o.dataset.initial);
        saveButtonsOf(picker).forEach((button) => { button.disabled = !options.length || !changed; });
        const removing = picker.querySelector('[data-department-removing]');
        const text = removingText(picker);
        if (removing && removing.textContent !== text) removing.textContent = text;
    }

    function init(picker) {
        // Initialized state is tracked by an attribute (so state is not reset on every htmx update of another region)
        if ('departmentReady' in picker.dataset) return;
        picker.dataset.departmentReady = 'true';
        optionsOf(picker).forEach((o) => {
            if (o.dataset.initial === 'mixed') applyState(o, 'mixed', picker);
        });
        refresh(picker);
    }

    // Whether saving leaves one or more targets shared with none of the selectable groups.
    // If any selected department remains, it remains for every target. If only intermediate ones remain, then unless
    // the total of remaining counts reaches the number of targets, some target is certain to remain in none
    function needsConfirm(picker) {
        if ('departmentFree' in picker.dataset) return false;
        const options = optionsOf(picker);
        if (!options.length || options.some((o) => stateOf(o) === 'checked')) return false;
        const kept = options.filter((o) => stateOf(o) === 'mixed')
            .reduce((sum, o) => sum + Number(o.dataset.sharedCount || 0), 0);
        return kept < Number(picker.dataset.targetCount || 1);
    }

    function toggleConfirm(picker, show) {
        const confirm = picker.querySelector('[data-department-confirm]');
        if (!confirm) return;
        confirm.hidden = !show;
        if (show) {
            confirm.querySelector('[data-department-confirm-back]').focus();
        } else {
            const first = optionsOf(picker)[0];
            if (first) first.focus();
        }
    }

    document.addEventListener('click', function(evt) {
        const target = evt.target instanceof Element ? evt.target : null;
        if (!target) return;
        const picker = target.closest(PICKER);
        if (!picker) return;
        if (isOption(target)) {
            if (target.dataset.initial === 'mixed') applyState(target, NEXT[stateOf(target)], picker);
            refresh(picker);
        } else if (target.closest('[data-department-confirm-back]')) {
            toggleConfirm(picker, false);
        } else if (target.closest('[data-department-confirm-ok]')) {
            const form = picker.form || picker.closest('form');
            picker.querySelector('[data-department-confirm]').hidden = true;
            picker.dataset.confirmed = 'true';
            if (form) form.requestSubmit();
        }
    });

    // Insert the confirmation before submit (stop it in the capture phase, before htmx receives the form's submit)
    document.addEventListener('submit', function(evt) {
        const form = evt.target;
        if (!(form instanceof HTMLFormElement)) return;
        const picker = form.querySelector(PICKER);
        if (!picker) return;
        if (picker.dataset.confirmed === 'true') {
            delete picker.dataset.confirmed;
            return;
        }
        if (!needsConfirm(picker)) return;
        evt.preventDefault();
        evt.stopImmediatePropagation();
        toggleConfirm(picker, true);
    }, true);

    document.querySelectorAll(PICKER).forEach(init);
    document.body.addEventListener('htmx:afterSettle', function() {
        document.querySelectorAll(PICKER).forEach(init);
    });
});

// -------------------------------------------------------------
// AssetScope
// Dialog [data-asset-scope-form] for the usage scope of an organization asset. If a department that was in the scope at open time
// (an option whose data-initial is not unchecked) is removed, [data-scope-remove-warning]
// (role="alert") is shown before saving, and hidden again when restored. Department selection
// behavior (intermediate, confirmation, save button) stays with DepartmentPicker.
// -------------------------------------------------------------
registerBlock('AssetScope', function() {
    const FORM = '[data-asset-scope-form]';

    function refresh(form) {
        const warning = form.querySelector('[data-scope-remove-warning]');
        if (!warning) return;
        const removing = Array.from(form.querySelectorAll('[data-department-option]'))
            .some((o) => o.dataset.initial !== 'unchecked' && !o.checked);
        if (warning.hidden === !removing) return;
        warning.hidden = !removing;
    }

    document.addEventListener('change', function(evt) {
        const target = evt.target instanceof Element ? evt.target : null;
        const form = target ? target.closest(FORM) : null;
        if (form) refresh(form);
    });
});

// -------------------------------------------------------------
// ShareDialog
// Decides, per open button, where the content of the single share dialog #share-dialog on the page is loaded from.
// One item uses data-share-url; bulk builds /share/{type}/dialog?ids=... from the action bar
// (the kind in data-bulk-select) and the selected rows. It is swapped before opening (capture phase).
// Cancel is also disabled while saving. On partial failure ([data-share-result]) or lost permission
// (HX-Trigger: shareReloadOnClose), the list is re-read on close, and on partial failure
// only the failed targets are reselected. The re-read URL uses only data-reload-url of the list part.
// -------------------------------------------------------------
registerBlock('ShareDialog', function() {
    const DIALOG_ID = 'share-dialog';
    const TARGET = /^#[A-Za-z0-9_-]+$/;
    const state = { panel: null, failed: null, reload: false, opener: null, refocus: null };

    const loaderOf = () => document.querySelector(`#${DIALOG_ID} [data-share-content]`);

    function setUrl(url) {
        const loader = loaderOf();
        if (!loader || !url || loader.getAttribute('hx-get') === url) return;
        loader.setAttribute('hx-get', url);
        htmx.process(loader);
    }

    function rowsNear(bar) {
        for (let scope = bar.parentElement; scope; scope = scope.parentElement) {
            const rows = scope.querySelectorAll('[data-bulk-row]');
            if (rows.length) return Array.from(rows);
        }
        return [];
    }

    function bulkUrl(action) {
        const bar = action.closest('[data-bulk-select]');
        if (!bar) return null;
        const ids = rowsNear(bar).filter((row) => row.checked)
            .map((row) => `ids=${encodeURIComponent(row.value)}`);
        return `/share/${encodeURIComponent(bar.dataset.bulkSelect)}/dialog?${ids.join('&')}`;
    }

    // Decide the load target before Dialog (bubble phase) opens and reads the content
    document.addEventListener('click', function(evt) {
        const target = evt.target instanceof Element ? evt.target : null;
        const opener = target && target.closest(`[data-dialog-open="${DIALOG_ID}"]`);
        if (!opener) return;
        const url = opener.dataset.shareUrl ||
            (opener.matches('[data-bulk-action]') ? bulkUrl(opener) : null);
        setUrl(url);
        state.panel = opener.closest('[data-list-tabpanel]');
        state.opener = opener.dataset.shareUrl
            ? `[data-share-url="${CSS.escape(opener.dataset.shareUrl)}"]`
            : '[data-bulk-action]';
        state.failed = null;
        state.reload = false;
    }, true);

    // If the list is re-read right after closing, the open button that got the focus back is replaced
    // and disappears. Return to the same button in the re-read list (as is if the button remains)
    function restoreFocus(panel) {
        const selector = state.refocus;
        state.refocus = null;
        const active = document.activeElement;
        if (!selector || (active && active !== document.body && active.isConnected &&
                active.id !== 'main')) return;
        const next = panel.querySelector(selector);
        if (next && !next.disabled) next.focus();
    }

    document.body.addEventListener('htmx:afterSettle', function(evt) {
        const root = evt.detail && evt.detail.elt;
        if (root instanceof Element && root.matches('[data-list-tabpanel]')) restoreFocus(root);
    });

    const isShareForm = (elt) => !!elt && !!elt.matches && elt.matches('[data-share-form]');
    const cancelButtons = (form) => Array.from(form.querySelectorAll('[data-dialog-close]'));

    document.body.addEventListener('htmx:beforeRequest', function(evt) {
        if (isShareForm(evt.detail.elt)) cancelButtons(evt.detail.elt).forEach((b) => { b.disabled = true; });
    });
    document.body.addEventListener('htmx:afterRequest', function(evt) {
        if (isShareForm(evt.detail.elt)) cancelButtons(evt.detail.elt).forEach((b) => { b.disabled = false; });
    });

    // When a partial-failure result is inserted, remember the failed targets
    document.body.addEventListener('htmx:afterSettle', function() {
        const result = document.querySelector(`#${DIALOG_ID} [data-share-result]`);
        if (!result) return;
        state.failed = new Set((result.dataset.failedIds || '').split(',').filter(Boolean));
    });

    // Permission was lost (403). Re-read the list on close
    document.body.addEventListener('shareReloadOnClose', function(evt) {
        const target = evt.detail ? evt.detail.target : undefined;
        if (typeof target === 'string' && TARGET.test(target)) {
            state.panel = document.getElementById(target.slice(1)) || state.panel;
        }
        state.reload = true;
    });

    function reselect(panel, failed) {
        const rows = Array.from(panel.querySelectorAll('[data-bulk-row]'));
        rows.forEach((row) => { row.checked = failed.has(row.value); });
        if (rows.length) rows[0].dispatchEvent(new Event('change', { bubbles: true }));
    }

    document.body.addEventListener('dialog:closed', function(evt) {
        if (!evt.detail || evt.detail.id !== DIALOG_ID) return;
        const panel = state.panel;
        const failed = state.failed;
        const reload = state.reload || !!failed;
        state.failed = null;
        state.reload = false;
        state.refocus = state.opener;
        const url = panel ? panel.getAttribute('data-reload-url') : null;
        if (!reload || !url) return;
        htmx.ajax('GET', url, { target: panel, swap: 'innerHTML' }).then(function() {
            if (failed) reselect(panel, failed);
        });
    });
});

// -------------------------------------------------------------
// ConversationStopped
// When a conversation can no longer continue (SSE stopped, or a conversation_stopped failure),
// insert the frame from the screen's <template id="conversation-stopped-template">
// and move focus to its heading (no reason is shown). The caller keeps the text in the input.
// -------------------------------------------------------------
function showConversationStopped(container) {
    const template = document.getElementById('conversation-stopped-template');
    if (!container || !template) return null;
    let panel = container.querySelector('[data-conversation-stopped]');
    if (!panel) {
        container.appendChild(template.content.cloneNode(true));
        panel = container.querySelector('[data-conversation-stopped]');
    }
    const heading = panel && panel.querySelector('[data-conversation-stopped-heading]');
    if (heading) heading.focus();
    return panel;
}
window.showConversationStopped = showConversationStopped;

// Processing on page load complete
document.addEventListener('DOMContentLoaded', function() {
    // Drag-and-drop setup
    setupDragAndDrop('#interview-drop-zone', '#file-input', 'selected-file');
    setupDragAndDrop('#report-drop-zone', '#report-file-input', 'selected-report-file');
    
    // Local timezone conversion
    convertAllTimes();

    // Persona avatar rendering (works only on pages that loaded DiceBear)
    renderPersonaAvatars();

    console.log('AIペルソナシステム (htmx版) initialized');
});
