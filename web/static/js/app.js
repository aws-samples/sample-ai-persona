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
document.body.addEventListener('showToast', function(evt) {
    const detail = evt.detail || {};
    showFlashMessage(detail.message || 'エラーが発生しました。', detail.type || 'error');
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
    showFlashMessage('エラーが発生しました。再度お試しください。', 'error');
});

// フラッシュメッセージ表示
function showFlashMessage(message, type = 'info') {
    const container = document.getElementById('flash-messages');
    if (!container) return;
    
    const colors = {
        success: 'bg-green-50 border-green-200 text-green-800',
        error: 'bg-red-50 border-red-200 text-red-800',
        warning: 'bg-yellow-50 border-yellow-200 text-yellow-800',
        info: 'bg-blue-50 border-blue-200 text-blue-800'
    };
    
    // components/icons.html と同じ Heroicons（outline）のパス。成功・エラー表示パーシャルと見た目を揃える
    const icons = {
        success: { color: 'text-green-600', d: 'M9 12.75 11.25 15 15 9.75M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z' },
        error: { color: 'text-red-500', d: 'm9.75 9.75 4.5 4.5m0-4.5-4.5 4.5M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z' },
        warning: { color: 'text-yellow-600', d: 'M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126ZM12 15.75h.007v.008H12v-.008Z' },
        info: { color: 'text-blue-600', d: 'm11.25 11.25.041-.02a.75.75 0 0 1 1.063.852l-.708 2.836a.75.75 0 0 0 1.063.853l.041-.021M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Zm-9-3.75h.008v.008H12V8.25Z' }
    };
    const icon = icons[type] || icons.info;
    
    const div = document.createElement('div');
    // コンテナは pointer-events-none（下の要素を操作できるように）なので、
    // 閉じるボタンを押せるようトースト自身だけクリックを受け付ける。
    // shadow-lg は本文コンテンツの上に重なるため境界を分かりやすくする。
    div.className =
        `${colors[type]} border rounded-lg p-4 mb-2 fade-in shadow-lg pointer-events-auto`;

    const wrapper = document.createElement('div');
    wrapper.className = 'flex items-center justify-between gap-3';

    const body = document.createElement('div');
    body.className = 'flex items-center gap-2';

    const SVG_NS = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('class', `w-5 h-5 shrink-0 ${icon.color}`);
    svg.setAttribute('fill', 'none');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('stroke-width', '1.5');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('aria-hidden', 'true');
    const path = document.createElementNS(SVG_NS, 'path');
    path.setAttribute('stroke-linecap', 'round');
    path.setAttribute('stroke-linejoin', 'round');
    path.setAttribute('d', icon.d);
    svg.appendChild(path);

    const span = document.createElement('span');
    span.textContent = message;

    body.appendChild(svg);
    body.appendChild(span);

    const btn = document.createElement('button');
    btn.className = 'text-gray-500 hover:text-gray-700';
    btn.textContent = '×';
    btn.addEventListener('click', () => div.remove());

    wrapper.appendChild(body);
    wrapper.appendChild(btn);
    div.appendChild(wrapper);
    
    container.appendChild(div);
    
    // 5秒後に自動削除
    setTimeout(() => {
        div.remove();
    }, 5000);
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
