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

document.body.addEventListener('htmx:responseError', function(evt) {
    // エラー時の処理
    console.error('htmx request error:', evt.detail);
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
    
    const icons = {
        success: '✅',
        error: '❌',
        warning: '⚠️',
        info: 'ℹ️'
    };
    
    const div = document.createElement('div');
    div.className = `${colors[type]} border rounded-lg p-4 mb-4 fade-in`;

    const wrapper = document.createElement('div');
    wrapper.className = 'flex items-center justify-between';

    const span = document.createElement('span');
    span.textContent = `${icons[type]} ${message}`;

    const btn = document.createElement('button');
    btn.className = 'text-gray-500 hover:text-gray-700';
    btn.textContent = '×';
    btn.addEventListener('click', () => div.remove());

    wrapper.appendChild(span);
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

/**
 * 単一アバター枠を描画する。DiceBear が使えれば SVG、無ければ頭文字+カラー円。
 * el: data-avatar-seed / data-avatar-name / data-avatar-color を持つ要素。
 */
function fillPersonaAvatar(el) {
    if (!el || el.dataset.avatarFilled) return;
    const seed = el.dataset.avatarSeed || '';
    const size = parseInt(el.dataset.avatarSize || '48', 10);
    const svg = personaAvatarSvg(seed, size);
    if (svg) {
        el.innerHTML = svg; // personaAvatarSvg 内で DOMPurify 済み
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
 */
function renderPersonaAvatars(root) {
    const scope = root || document;
    const els = scope.querySelectorAll('[data-avatar-seed]:not([data-avatar-filled])');
    const obs = getAvatarObserver();
    els.forEach(el => {
        if (obs) obs.observe(el);
        else fillPersonaAvatar(el);
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
