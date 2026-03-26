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
function updateFileName(input) {
    const fileName = input.files[0]?.name || '';
    const display = document.getElementById('selected-file');
    if (display) {
        display.textContent = fileName ? `選択中: ${fileName}` : '';
    }
}

// ドラッグ＆ドロップ設定
function setupDragAndDrop(dropZoneSelector, fileInputSelector) {
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
            updateFileName(fileInput);
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

// htmxで動的に追加された要素にも対応
document.body.addEventListener('htmx:afterSwap', function(evt) {
    convertAllTimes(evt.detail.target);
});

// ページ読み込み完了時の処理
document.addEventListener('DOMContentLoaded', function() {
    // ドラッグ＆ドロップの設定
    setupDragAndDrop('.border-dashed', '#file-input');
    
    // ローカルタイムゾーン変換
    convertAllTimes();
    
    console.log('AIペルソナシステム (htmx版) initialized');
});
