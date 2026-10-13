// AIペルソナシステム - htmx版 JavaScript

// htmxイベントハンドラ
document.body.addEventListener('htmx:afterSwap', function(evt) {
    // 新しいコンテンツにフェードインアニメーションを適用
    if (evt.detail.target) {
        evt.detail.target.classList.add('fade-in');
    }
});

document.body.addEventListener('htmx:beforeRequest', function(evt) {
    // リクエスト開始時の処理
    console.log('htmx request started:', evt.detail.pathInfo.requestPath);
});

document.body.addEventListener('htmx:afterRequest', function(evt) {
    // リクエスト完了時の処理
    console.log('htmx request completed:', evt.detail.pathInfo.requestPath);
});

// サーバーからのトースト指示（HX-Trigger: {"showToast": {...}}）。
// 再試行で解決しうるエラー（ErrorKind.TRANSIENT）は、画面を書き換えず
// トーストで通知する。入力内容を保持するため（Issue #117）。
// htmx は HX-Trigger をスワップ判定より前に処理するので 4xx でも発火する。
// 種類は約束 C6 の level と、今の toast_response() の type のどちらでも受ける。
document.body.addEventListener('showToast', function(evt) {
    const detail = evt.detail || {};
    showFlashMessage(
        detail.message || 'エラーが発生しました。',
        detail.level || detail.type || 'error'
    );
});

// 非2xx応答のDOM反映（Issue #117）。
// htmx 1.9.10 は 2xx 以外の本文をスワップしないため、4xx/5xx でエラー文言を
// 返しても画面に届かない。サーバーが X-Render-Response: true を明示した応答
// だけをスワップ対象にする。
// ステータスコードで一律に許可しないのは、汎用エラーパーシャルが hx-target
// （本体コンテンツや一覧）に流れ込んでフォームごと消える経路があるため。
// 「表示してよい」判断はサーバー側が持つ。
document.body.addEventListener('htmx:beforeSwap', function(evt) {
    const xhr = evt.detail.xhr;
    if (xhr && xhr.getResponseHeader('X-Render-Response') === 'true') {
        evt.detail.shouldSwap = true;
        evt.detail.isError = false;
    }
});

document.body.addEventListener('htmx:responseError', function(evt) {
    // エラー時の処理
    console.error('htmx request error:', evt.detail);
    // サーバーがトースト表示または本文の反映を指示している場合は、それぞれの
    // 経路で文言が出るため、ここで汎用文言を重ねて出さない
    const xhr = evt.detail.xhr;
    if (xhr && (xhr.getResponseHeader('HX-Trigger') ||
                xhr.getResponseHeader('X-Render-Response') === 'true')) {
        return;
    }
    // ダイアログの中身の読み込みの失敗は、ダイアログの中に「内容を読み込めませんでした」と
    // 「再読み込み」を出して知らせる（Dialog）。汎用のトーストを重ねない
    const elt = evt.detail.elt;
    if (elt && elt.matches && elt.matches('[data-dialog-content]')) {
        return;
    }
    showFlashMessage('エラーが発生しました。再度お試しください。', 'error');
});

// =============================================================
// Toast（U3 WF4、B19〜B22）
// 成功・情報は #toast-status（role="status"）に入れて 5 秒で消す。ポインタが
// 重なっている間とフォーカスが中にある間は数えるのを止め、離れたら残りの
// 時間から数え直す。エラー・警告は #toast-alert（role="alert"）に入れ、
// 閉じるボタンを押すまで残す。表示してもフォーカスは動かさない。
// 文字はすべて textContent で入れる（応答やメッセージを HTML として解釈しない）。
// =============================================================
const TOAST_DURATION_MS = 5000;
const SVG_NS = 'http://www.w3.org/2000/svg';

// components/icons.html と同じ Heroicons（outline）のパス。成功・エラー表示パーシャルと見た目を揃える
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
    // コンテナは pointer-events-none（下の要素を操作できるように）なので、
    // 閉じるボタンを押せるようトースト自身だけクリックを受け付ける。
    // shadow-lg は本文コンテンツの上に重なるため境界を分かりやすくする。
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

// 要素ごとに 1 つのタイマーで数える。止めるときは残りの時間だけを持つ。
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

// フラッシュメッセージ表示（呼び方は従来どおり）
function showFlashMessage(message, type = 'info') {
    const kind = TOAST_STYLES[type] ? type : 'info';
    const persistent = kind === 'error' || kind === 'warning';
    const container = document.getElementById(persistent ? 'toast-alert' : 'toast-status');
    if (!container) return;
    const toast = buildToast(String(message), TOAST_STYLES[kind]);
    container.appendChild(toast);
    if (!persistent) startToastCountdown(toast, TOAST_DURATION_MS);
}

// ファイル名更新
function updateFileName(input, targetId) {
    const fileName = input.files[0]?.name || '';
    const display = document.getElementById(targetId || 'selected-file');
    if (display) {
        display.textContent = fileName ? `選択中: ${fileName}` : '';
    }
}

// ドラッグ＆ドロップ設定
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

// 確認ダイアログ
function confirmAction(message) {
    return confirm(message);
}

// ローカルストレージ操作
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

// ローカルタイムゾーン変換
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
// ペルソナアバター（DiceBear notionists）
// DiceBear バンドルを読み込んだページでのみ動作。未読込時は頭文字フォールバック。
// =============================================================

// seed+size をキーにした生成済みSVGのキャッシュ（同一画面内の再生成を防ぐ）
const _personaAvatarCache = {};

/**
 * ペルソナID(seed)から DiceBear アバターSVGを生成して返す。
 * DiceBear/DOMPurify 未読込・生成失敗時は null（呼び出し側で頭文字にフォールバック）。
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

// アップロード済みアイコン画像の URL（ペルソナID → URL）。
// サーバーが data-avatar-url を出力した要素から集める。メッセージ行など
// ペルソナ情報を持たない要素も、同じページで登録された URL を引ける。
const _personaAvatarUrls = {};

/**
 * ペルソナのアイコン画像 URL を登録する。空文字は「画像なし（自動アバター）」として登録を消す。
 * 同一オリジンの相対パスのみ受け付ける（サーバー生成の /persona/{id}/avatar を想定）。
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
 * アバター枠の中身（HTML文字列）を返す。
 * アイコン画像が登録されていれば <img>、無ければ DiceBear SVG、どちらも無ければ null。
 * <img> の読み込み失敗時は下の error リスナーが DiceBear に差し戻す。
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

// アイコン画像の読み込み失敗（削除済み・期限切れ等）は DiceBear に差し戻す。
// error はバブリングしないためキャプチャで受ける。
document.addEventListener('error', function(evt) {
    const img = evt.target;
    if (!(img instanceof HTMLImageElement) || !img.classList.contains('persona-avatar-photo')) return;
    const seed = img.dataset.avatarFallbackSeed || '';
    const size = parseInt(img.dataset.avatarFallbackSize || '64', 10);
    registerPersonaAvatarUrl(seed, '');
    const svg = personaAvatarSvg(seed, size);
    if (svg) {
        img.outerHTML = svg; // personaAvatarSvg 内で DOMPurify 済み
    } else {
        img.remove();
    }
}, true);

/**
 * 単一アバター枠を描画する。アイコン画像があれば画像、無ければ DiceBear SVG、
 * どちらも使えなければ頭文字+カラー円。
 * el: data-avatar-seed / data-avatar-name / data-avatar-color（任意で data-avatar-url）を持つ要素。
 */
function fillPersonaAvatar(el) {
    if (!el || el.dataset.avatarFilled) return;
    const seed = el.dataset.avatarSeed || '';
    const size = parseInt(el.dataset.avatarSize || '48', 10);
    // 装飾画像。名前は隣のテキストで読み上げられるため、スクリーンリーダーからは隠す
    el.setAttribute('aria-hidden', 'true');
    const inner = personaAvatarInner(seed, size);
    if (inner) {
        el.innerHTML = inner; // <img> は属性をエスケープ済み、SVG は DOMPurify 済み
        el.classList.add('persona-avatar-img');
    } else {
        // フォールバック: 頭文字 + 既存カラークラス
        const name = el.dataset.avatarName || '';
        const color = el.dataset.avatarColor || 'blue';
        const initial = name ? name.charAt(0) : '';
        el.classList.add('persona-avatar-' + color);
        el.textContent = initial;
    }
    el.dataset.avatarFilled = '1';
}

// 可視範囲に入ったアバター枠だけ生成する（一覧の大量生成によるブロックを防ぐ）
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
 * root 配下の未処理アバター枠 [data-avatar-seed] を遅延生成にのせる。
 * IntersectionObserver 非対応環境では即時生成にフォールバック。
 * data-avatar-eager 属性を持つ要素は即時生成する（内部スクロールコンテナ内など、
 * viewport ベースの IntersectionObserver が可視判定できない場所向け）。
 */
function renderPersonaAvatars(root) {
    const scope = root || document;
    // 描画より先に、スコープ内の画像 URL を登録する（同じペルソナのメッセージ行等で引けるように）
    const withUrl = scope.querySelectorAll ? scope.querySelectorAll('[data-avatar-seed][data-avatar-url]') : [];
    withUrl.forEach(el => registerPersonaAvatarUrl(el.dataset.avatarSeed, el.dataset.avatarUrl));
    const els = scope.querySelectorAll('[data-avatar-seed]:not([data-avatar-filled])');
    const obs = getAvatarObserver();
    els.forEach(el => {
        if (el.dataset.avatarEager !== undefined) {
            fillPersonaAvatar(el);  // 即時生成（キャッシュが効くため低コスト）
        } else if (obs) {
            obs.observe(el);
        } else {
            fillPersonaAvatar(el);
        }
    });
}
window.renderPersonaAvatars = renderPersonaAvatars;

// htmxで動的に追加された要素にも対応
document.body.addEventListener('htmx:afterSwap', function(evt) {
    convertAllTimes(evt.detail.target);
    renderPersonaAvatars(evt.detail.target);
});

// =============================================================
// 画面の共通部品（U3）
// まとまりごとに 1 回だけ document に登録し、イベントの委譲で受ける
// （htmx で差し替わった要素にも効く）。まとまりどうしは関数を直接呼ばず、
// イベント（dialogClose、listReload、showToast、dialog:closed）だけでつながる。
// 1 つの登録の失敗でほかが止まらないよう、登録はそれぞれ try で包む。
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
// NavMenu（C1、WF1、B1・B2）
// 開閉の状態は base.html の Alpine（openMenu）のまま。ここはキー操作と
// 「外へ出たら閉じる」だけを足す。nav は data-nav-menu の要素。
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
        // 待っている間に閉じられていたら何もしない
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
            // クリックへの変換を止め、開くときは最初の項目へ移す（マウスのクリックは Alpine のまま）
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

    // Space はキーを離したときにクリックになるため、こちらでも止める
    document.addEventListener('keyup', function(evt) {
        if (evt.key !== ' ' || !(evt.target instanceof Element)) return;
        if (evt.target.closest('[data-nav-menu] [data-nav-toggle]')) evt.preventDefault();
    });

    // Tab などでボタンと項目の外へフォーカスが出たら閉じる（フォーカスは動かさない）
    document.addEventListener('focusin', function(evt) {
        document.querySelectorAll('[data-nav-menu]').forEach((nav) => {
            const toggle = openToggleOf(nav);
            if (toggle && !isWithin(toggle, evt.target)) close(nav, false);
        });
    });

    // 外側のクリックで閉じる。開くボタンのクリックは Alpine が先に処理している
    document.addEventListener('click', function(evt) {
        document.querySelectorAll('[data-nav-menu]').forEach((nav) => {
            const toggle = openToggleOf(nav);
            if (toggle && !isWithin(toggle, evt.target)) close(nav, false);
        });
    });

    // 開いたまま幅が 640px を跨いだら閉じる
    BREAKPOINT.addEventListener('change', function() {
        document.querySelectorAll('[data-nav-menu]').forEach((nav) => close(nav, false));
    });
});

// -------------------------------------------------------------
// Dialog（C3、WF2、B4〜B6・B16・B18・B22）
// components/dialog.html の dialog() が描いた [data-dialog] を開閉する。
// 開閉の状態はここだけが持ち、hidden 属性で表す。開いている間は、ダイアログから
// <body> までの祖先の兄弟に inert を付ける（ダイアログ自身・祖先・トーストの
// 入れ物には付けない。もともと inert の要素は覚えず、閉じても外さない）。
// 重ねて開かない。開く要求が来たら先のダイアログを閉じてから開く。
// -------------------------------------------------------------
registerBlock('Dialog', function() {
    const FOCUSABLE = 'a[href], button, input, select, textarea, [tabindex]';
    const FIELDS = 'input:not([type="hidden"]), select, textarea';
    const SUBMITS = 'button[type="submit"], input[type="submit"]';

    // 開いているダイアログ: { el, opener, inerted, disabled, submitting, promise, resolve }
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

    function focusInitial(el) {
        const field = Array.from(panelOf(el).querySelectorAll(FIELDS)).find(isUsable);
        const target = field || document.getElementById(`${el.id}-title`);
        if (target) target.focus();
    }

    // --- 背景の inert ---------------------------------------------
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

    // --- 保存中 -----------------------------------------------------
    // 保存ボタン（送信ボタンと、要求を出したボタン自身）を無効にする
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

    // --- 中身の読み込み ---------------------------------------------
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

    // 失敗の文と「再読み込み」は固定文言。応答の本文・状態コード・URL は表示に使わない
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

    // --- 開く・閉じる -----------------------------------------------
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
        // inert が残ると画面全体が操作できなくなるため、最初に外す
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

    // JavaScript から開く（インタビューの保存名など）。閉じたら押したボタンの
    // data-dialog-close の値（Escape・× は null）で解決する
    window.openDialog = function(id) {
        return open(document.getElementById(id), document.activeElement);
    };

    // --- イベント -----------------------------------------------------
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
        // 循環の順: パネルの中 → トーストの閉じるボタン（あれば）→ パネルの最初へ戻る。
        // 閉じないエラー・警告のトーストに、キーボードだけでも届くようにする（B22）
        const items = focusables(current.el).concat(toastCloseButtons());
        if (!items.length) {
            evt.preventDefault();
            return;
        }
        const active = document.activeElement;
        const index = items.indexOf(active);
        if (index === -1 && panelOf(current.el).contains(active) && !evt.shiftKey) {
            return; // 見出しなど、循環に入らない要素からはブラウザの順のまま次へ
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

    // パネルの中で最後にフォーカスがあった要素（トーストを閉じたあとに戻す先）
    document.addEventListener('focusin', function(evt) {
        if (current && panelOf(current.el).contains(evt.target)) current.lastFocus = evt.target;
    });

    // ダイアログを開いたままトーストを閉じたら、フォーカスをダイアログへ戻す。
    // トーストは自分のクリックで要素ごと消えるため、消える前（捕捉の段階）に見分ける
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

    // サーバーが保存の成功を知らせる（HX-Trigger: {"dialogClose": {"id": "<id>"}}）
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
        // htmx が差し込んだダイアログ（データセット編集など）は差し込まれたら開く
        const inserted = root.matches('[data-dialog-autoopen][hidden]')
            ? root
            : root.querySelector('[data-dialog-autoopen][hidden]');
        if (!inserted) return;
        const config = evt.detail && evt.detail.requestConfig;
        open(inserted, (config && config.elt) || document.activeElement);
    });

    // 開いているダイアログ（かその祖先）が htmx の差し替えで消えるときも inert を外す
    document.body.addEventListener('htmx:beforeCleanupElement', function(evt) {
        const removing = evt.target;
        if (!current || !(removing instanceof Element)) return;
        if (removing === current.el || removing.contains(current.el)) {
            close(null, { removing: removing });
        }
    });

    // ページを移るときは、inert を残したまま履歴に保存しない
    const dismiss = () => close(null, { restoreFocus: false });
    document.body.addEventListener('htmx:beforeHistorySave', dismiss);
    window.addEventListener('pagehide', dismiss);
});

// -------------------------------------------------------------
// ListTabs（C4、WF5、B8〜B10）
// 左右の矢印でフォーカスを移すだけ（通信しない）。Enter はリンクの既定の動作、
// Space はここでクリックにして htmx で切り替える。切り替えの間は一覧部分に
// aria-busy、切り替えたら選択状態を移して件数を読む。フォーカスはタブに残す。
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

    document.body.addEventListener('htmx:afterSwap', function(evt) {
        const config = evt.detail.requestConfig;
        const tab = config && config.elt;
        if (tab instanceof Element && tab.matches(TAB)) select(tab);
    });
});

// -------------------------------------------------------------
// BusyRegion（B18、NFR6.12）
// htmx が一覧を差し替える・足す間、その一覧に aria-busy="true" を付ける。
// 対象はタブの一覧部分（[data-list-tabpanel]）と、「もっと見る」の行を受ける
// 一覧（[data-busy-target]）。
// -------------------------------------------------------------
registerBlock('BusyRegion', function() {
    const BUSY = '[data-list-tabpanel], [data-busy-target]';
    const isBusyRegion = (node) => node instanceof Element && node.matches(BUSY);

    document.body.addEventListener('htmx:beforeRequest', function(evt) {
        if (isBusyRegion(evt.detail.target)) evt.detail.target.setAttribute('aria-busy', 'true');
    });

    document.body.addEventListener('htmx:afterRequest', function(evt) {
        if (isBusyRegion(evt.detail.target)) evt.detail.target.removeAttribute('aria-busy');
    });

    // 読み込み中の印を残したまま履歴に保存しない（戻ったときに読み込み中に見えるため）
    document.body.addEventListener('htmx:beforeHistorySave', function() {
        document.querySelectorAll(`[aria-busy]:is(${BUSY})`)
            .forEach((region) => region.removeAttribute('aria-busy'));
    });
});

// -------------------------------------------------------------
// ListReload（約束 C6、NFR4.10）
// HX-Trigger: {"listReload": {"target": "#<id>"}} を受けて、その要素自身の
// data-reload-url（サーバーが描いた、今のタブを含む URL）を読み直す。
// イベントの値からは target だけを読み、URL などほかの項目は使わない。
// target はセレクターとして評価せず、id として使える文字だけを受ける。
// 同じ応答に dialogClose があれば、閉じてから読み直すよう次のタスクに回す。
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

    document.body.addEventListener('listReload', function(evt) {
        const target = evt.detail ? evt.detail.target : undefined;
        setTimeout(() => reload(target), 0);
    });
});

// -------------------------------------------------------------
// BulkSelect（C5、WF6、B10・B11）
// 操作バー [data-bulk-select] と行の選択欄 [data-bulk-row] は同じ一覧の中に置く。
// 「すべて選択」は 0 件で未選択、一部で中間、全件で選択。押すと、未選択・中間なら
// 全件を選び、全件選択なら全件を外す。「もっと見る」で増えた行は未選択で加わる。
// -------------------------------------------------------------
registerBlock('BulkSelect', function() {
    // バーから祖先をたどり、行を含む最初の要素の行を対象にする
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

    // htmx で行が増えた・差し替わったら数え直す
    document.body.addEventListener('htmx:afterSettle', function() {
        document.querySelectorAll('[data-bulk-select]').forEach(update);
    });
});

// -------------------------------------------------------------
// DepartmentPicker（C6、WF7、B12・B24）
// 始まりが選択・未選択の部署は押すたびに入れ替わる（ブラウザのまま）。始まりが
// 中間の部署は 選択 → 未選択 → 中間（元のまま）と回り、中間の間は
// {name}_unchanged の隠し入力で部署 id を送る（中間の選択欄はブラウザが送らない）。
// 何も変えないうちは保存を無効にし、外れる部署は件数とともに読み上げる。
// Dialog の存在は知らない（中で使われても、ここはフォームとだけやりとりする）。
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
            ? '保存すると利用範囲から外れる部署'
            : '保存すると共有が外れる部署';
        return `${lead}: ${removed.join('、')}`;
    }

    function saveButtonsOf(picker) {
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
        if (picker.dataset.departmentReady) return;
        picker.dataset.departmentReady = '';
        optionsOf(picker).forEach((o) => {
            if (o.dataset.initial === 'mixed') applyState(o, 'mixed', picker);
        });
        refresh(picker);
    }

    // 保存すると、選べる部署のどれにも共有しない対象が 1 件以上できるか。
    // 選択の部署が 1 つでも残れば全対象に残る。中間だけが残るときは、残る件数の
    // 合計が対象の件数に満たなければ、どれにも残らない対象が必ずある
    function needsConfirm(picker) {
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

    // 送信の前に確認を挟む（htmx がフォームの submit を受けるより先に、捕捉の段階で止める）
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

// ページ読み込み完了時の処理
document.addEventListener('DOMContentLoaded', function() {
    // ドラッグ＆ドロップの設定
    setupDragAndDrop('#interview-drop-zone', '#file-input', 'selected-file');
    setupDragAndDrop('#report-drop-zone', '#report-file-input', 'selected-report-file');
    
    // ローカルタイムゾーン変換
    convertAllTimes();

    // ペルソナアバター描画（DiceBear 読込ページのみ動作）
    renderPersonaAvatars();

    console.log('AIペルソナシステム (htmx版) initialized');
});
