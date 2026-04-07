
const AppState = {
    config: {
        key: localStorage.getItem('s1_key') || '',
        url: localStorage.getItem('s1_url') || 'https://api.deepseek.com',
        model: localStorage.getItem('s1_model') || 'deepseek-chat'
    },
    advanced: {
        batchSize: parseInt(localStorage.getItem('s1_adv_batch')) || 7,
        reqDelay: parseInt(localStorage.getItem('s1_adv_delay')) || 1500
    },
    asrConfig: {
        key: localStorage.getItem('s1_asr_key') || '',
        url: localStorage.getItem('s1_asr_url') || 'https://api.groq.com/openai/v1',
        model: localStorage.getItem('s1_asr_model') || 'whisper-large-v3'
    },
    // [V45.0] 自定义快捷键映射
    keyMap: JSON.parse(localStorage.getItem('s1_keymap')) || {
        'play': { label: '播放/暂停', key: ' ', ctrl: false, alt: false, shift: false },
        'undo': { label: '撤销', key: 'z', ctrl: true, alt: false, shift: false },
        'redo': { label: '重做', key: 'y', ctrl: true, alt: false, shift: false },
        'save': { label: '保存项目', key: 's', ctrl: true, alt: false, shift: false },
        'split': { label: '切刀 (拆分)', key: 'k', ctrl: true, alt: false, shift: false },
        'merge': { label: '合并字幕', key: 'm', ctrl: true, alt: false, shift: false },
        'delete': { label: '删除选中', key: 'delete', ctrl: true, alt: false, shift: false },
        'retranslate': { label: '单句重译', key: 'r', ctrl: false, alt: true, shift: false },
        'record': { label: '智能补录', key: 'c', ctrl: false, alt: true, shift: false },
        'nav_prev': { label: '上一条', key: 'arrowup', ctrl: true, alt: false, shift: false },
        'nav_next': { label: '下一条', key: 'arrowdown', ctrl: true, alt: false, shift: false },
        'seek_back': { label: '快退', key: 'arrowleft', ctrl: true, alt: false, shift: false },
        'seek_fwd': { label: '快进', key: 'arrowright', ctrl: true, alt: false, shift: false }
    },
    software: 'pr',
    font: "'Microsoft YaHei', sans-serif",
    fontSize: 60,
    maxChars: 24,
    isCalibrating: false,
    isBilingual: false, // [V45.0] 双语模式
    subtitles: [],
    isProcessing: false,
    processingRange: null,
    activeIndex: -1,
    isPlaying: false,
    animationFrameId: null,
    overflowIndices: [],
    missingIndices: [],
    hasVideo: false,
    isAudioMode: false,
    videoEl: null,
    currentVideoFile: null,
    originalFileName: 'export',
    history: [],
    historyIndex: -1,
    lastSaved: null,
    isRecording: false,
    recordingStartMs: null,
    virtual: {
        rowHeight: 105,
        visibleCount: 0,
        startIndex: 0,
        endIndex: 0,
        buffer: 3
    },
    audioCtx: null,
    analyser: null,
    audioSource: null,
    visualizerCanvas: null,
    visualizerCtx: null,
    activeNumberKey: null,
    isCtrlPressed: false
};

let selectedIndices = new Set();
let lastClickedIndex = -1;
let CACHED_EMPTY_STATE = '';
let dragCounter = 0;
let isLocalScrubbing = false;

const debounce = (func, wait) => {
    let timeout;
    return function (...args) {
        const context = this;
        clearTimeout(timeout);
        timeout = setTimeout(() => func.apply(context, args), wait);
    };
};
const debouncedUpdateFinal = debounce(() => updateFinalOutput(selectedIndices.size > 1), 300);
const debouncedSave = debounce(() => saveProject(false), 2000);

const SOFTWARE_CONFIG = {
    pr: { min: 30, max: 150, val: 60, limit: 24, scale: 1.0, class: 'style-pr' },
    capcut: { min: 1, max: 20, val: 8, limit: 17, scale: 11.3, class: 'style-capcut' }
};

// --- Timeline Logic Module (V44.6 Enhanced + V45.0 Rolling Edit) ---
const Timeline = {
    pxPerSec: 20,
    container: null,
    content: null,
    ruler: null,
    track: null,
    playhead: null,

    // 状态
    isDragging: false,
    isScrubbing: false,
    dragTarget: null,

    init() {
        this.container = document.getElementById('timelineScrollContainer');
        this.content = document.getElementById('timelineContent');
        this.ruler = document.getElementById('timelineRuler');
        this.track = document.getElementById('trackContainer');
        this.playhead = document.getElementById('timelinePlayhead');

        this.track.addEventListener('mousedown', (e) => this.onBlockMouseDown(e));
        this.ruler.addEventListener('mousedown', (e) => this.startScrubbing(e));

        document.addEventListener('mousemove', (e) => {
            if (this.isDragging) this.onBlockMouseMove(e);
            if (this.isScrubbing) this.onScrubMove(e);
        });

        document.addEventListener('mouseup', (e) => {
            if (this.isDragging) this.onBlockMouseUp(e);
            if (this.isScrubbing) this.onScrubEnd(e);
        });

        const slider = document.getElementById('zoomSlider');
        slider.addEventListener('input', (e) => {
            this.pxPerSec = parseInt(e.target.value);
            this.render();
        });

        this.container.addEventListener('wheel', (e) => {
            if (e.altKey) {
                e.preventDefault();
                const rect = this.container.getBoundingClientRect();
                const mouseX = e.clientX - rect.left + this.container.scrollLeft;
                const timeAtMouse = mouseX / this.pxPerSec;

                const delta = e.deltaY > 0 ? 0.9 : 1.1;
                this.pxPerSec = Math.max(1, Math.min(200, this.pxPerSec * delta));
                slider.value = this.pxPerSec;

                const newScroll = (timeAtMouse * this.pxPerSec) - (e.clientX - rect.left);
                this.render();
                this.container.scrollLeft = newScroll;
            }
        }, { passive: false });

        this.content.addEventListener('mousedown', (e) => {
            if (e.target.id === 'timelineContent') {
                const rect = this.content.getBoundingClientRect();
                const offsetX = e.clientX - rect.left;
                const ms = (offsetX / this.pxPerSec) * 1000;
                if (AppState.hasVideo) {
                    AppState.videoEl.currentTime = ms / 1000;
                    if (!AppState.isPlaying) onVideoTimeUpdate();
                }
            }
        });
    },

    render() {
        const durationMs = (AppState.hasVideo && AppState.videoEl.duration)
            ? AppState.videoEl.duration * 1000
            : (AppState.subtitles.length > 0 ? AppState.subtitles[AppState.subtitles.length - 1].endMs + 10000 : 60000);

        const totalWidth = (durationMs / 1000) * this.pxPerSec;
        this.content.style.width = `${Math.max(this.container.clientWidth, totalWidth + 300)}px`;

        this.drawRuler(Math.max(this.container.clientWidth, totalWidth + 300));
        this.renderBlocks();
        this.updatePlayhead();
    },

    renderBlocks() {
        this.track.innerHTML = '';
        const frag = document.createDocumentFragment();

        AppState.subtitles.forEach((sub, idx) => {
            const left = (sub.startMs / 1000) * this.pxPerSec;
            const width = ((sub.endMs - sub.startMs) / 1000) * this.pxPerSec;

            const el = document.createElement('div');
            let classes = `tl-block`;
            if (selectedIndices.has(idx)) classes += ' selected';
            if (idx === AppState.activeIndex) classes += ' active';
            if (AppState.overflowIndices.includes(idx)) el.style.borderColor = '#ef4444';

            el.className = classes;
            el.style.left = `${left}px`;
            el.style.width = `${Math.max(4, width)}px`;
            el.dataset.index = idx;

            const text = sub.cn || sub.text || "";

            el.innerHTML = `
                        <div class="tl-handle tl-handle-l" data-action="resize-l"></div>
                        <div class="truncate px-1 select-none pointer-events-none" style="font-size:${this.pxPerSec < 15 ? '0' : '11px'}">${text}</div>
                        <div class="tl-handle tl-handle-r" data-action="resize-r"></div>
                    `;
            frag.appendChild(el);
        });
        this.track.appendChild(frag);
    },

    drawRuler(width) {
        this.ruler.width = width;
        this.ruler.height = 24;
        const ctx = this.ruler.getContext('2d');
        ctx.fillStyle = '#1a1a1a';
        ctx.fillRect(0, 0, width, 24);

        let stepSec = 1;
        if (this.pxPerSec < 10) stepSec = 10;
        else if (this.pxPerSec < 40) stepSec = 5;
        else stepSec = 1;

        const stepPx = stepSec * this.pxPerSec;
        ctx.fillStyle = '#71717a';
        ctx.font = '10px monospace';
        ctx.textBaseline = 'top';

        for (let x = 0; x < width; x += stepPx) {
            ctx.fillRect(Math.floor(x) + 0.5, 14, 1, 10);
            const sec = x / this.pxPerSec;
            const min = Math.floor(sec / 60);
            const s = Math.floor(sec % 60);
            const timeStr = `${String(min).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
            ctx.fillText(timeStr, x + 3, 2);
            if (stepPx > 20) {
                const subStep = stepPx / 5;
                for (let j = 1; j < 5; j++) ctx.fillRect(Math.floor(x + j * subStep) + 0.5, 20, 1, 4);
            }
        }
    },

    updatePlayhead() {
        if (!AppState.hasVideo) return;
        const currentMs = AppState.videoEl.currentTime * 1000;
        const left = (currentMs / 1000) * this.pxPerSec;
        this.playhead.style.transform = `translateX(${left}px)`;
        if (AppState.isPlaying && !this.isDragging && !this.isScrubbing) {
            const scrollL = this.container.scrollLeft;
            const clientW = this.container.clientWidth;
            if (left > scrollL + clientW * 0.9) this.container.scrollTo({ left: left - clientW * 0.2, behavior: 'smooth' });
        }
    },

    startScrubbing(e) {
        if (!AppState.hasVideo) return;
        this.isScrubbing = true;
        this.onScrubMove(e);
        document.body.style.cursor = 'ew-resize';
    },

    onScrubMove(e) {
        if (!this.isScrubbing) return;
        e.preventDefault();
        const rect = this.content.getBoundingClientRect();
        let offsetX = e.clientX - rect.left;
        offsetX = Math.max(0, Math.min(offsetX, rect.width));
        const seconds = offsetX / this.pxPerSec;
        AppState.videoEl.currentTime = seconds;
        if (!AppState.isPlaying) onVideoTimeUpdate();
    },

    onScrubEnd(e) {
        this.isScrubbing = false;
        document.body.style.cursor = '';
    },

    // --- [V45.0] 滚动编辑支持 ---
    onBlockMouseDown(e) {
        const block = e.target.closest('.tl-block');
        if (!block) return;

        e.stopPropagation();
        const index = parseInt(block.dataset.index);
        const action = e.target.dataset.action || 'move';

        let isRolling = false;
        let neighborIndex = -1;

        if (e.ctrlKey && action !== 'move') {
            if (action === 'resize-r' && index < AppState.subtitles.length - 1) {
                isRolling = true;
                neighborIndex = index + 1;
            } else if (action === 'resize-l' && index > 0) {
                isRolling = true;
                neighborIndex = index - 1;
            }
        }

        if (!e.ctrlKey && !selectedIndices.has(index)) {
            toggleSelection(index, null);
        }

        this.isDragging = true;
        this.dragTarget = {
            type: action,
            index: index,
            startX: e.clientX,
            initialStart: AppState.subtitles[index].startMs,
            initialEnd: AppState.subtitles[index].endMs,
            isRolling: isRolling,
            neighborIndex: neighborIndex,
            neighborInitialStart: isRolling ? AppState.subtitles[neighborIndex].startMs : 0,
            neighborInitialEnd: isRolling ? AppState.subtitles[neighborIndex].endMs : 0
        };

        document.body.style.cursor = isRolling ? 'ew-resize' : (action === 'move' ? 'grabbing' : 'col-resize');
    },

    onBlockMouseMove(e) {
        if (!this.isDragging || !this.dragTarget) return;
        e.preventDefault();

        const { type, index, startX, initialStart, initialEnd, isRolling, neighborIndex } = this.dragTarget;
        const deltaPx = e.clientX - startX;
        const deltaMs = (deltaPx / this.pxPerSec) * 1000;

        const sub = AppState.subtitles[index];
        const minDuration = 200;

        if (isRolling) {
            // 滚动编辑
            if (type === 'resize-r') {
                let newBoundary = initialEnd + deltaMs;
                const minLimit = initialStart + minDuration;
                const maxLimit = this.dragTarget.neighborInitialEnd - minDuration;
                newBoundary = Math.max(minLimit, Math.min(newBoundary, maxLimit));

                sub.endMs = newBoundary;
                AppState.subtitles[neighborIndex].startMs = newBoundary;
            } else if (type === 'resize-l') {
                let newBoundary = initialStart + deltaMs;
                const minLimit = this.dragTarget.neighborInitialStart + minDuration;
                const maxLimit = initialEnd - minDuration;
                newBoundary = Math.max(minLimit, Math.min(newBoundary, maxLimit));

                sub.startMs = newBoundary;
                AppState.subtitles[neighborIndex].endMs = newBoundary;
            }
            this.updateBlockDOM(index);
            this.updateBlockDOM(neighborIndex);
            return;
        }

        const prevSub = AppState.subtitles[index - 1];
        const nextSub = AppState.subtitles[index + 1];
        const GAP = 50;
        const minLimit = prevSub ? prevSub.endMs + GAP : 0;
        const maxLimit = nextSub ? nextSub.startMs - GAP : Infinity;

        if (type === 'move') {
            const duration = initialEnd - initialStart;
            let newStart = initialStart + deltaMs;
            newStart = Math.max(minLimit, Math.min(newStart, maxLimit - duration));
            sub.startMs = newStart;
            sub.endMs = newStart + duration;

        } else if (type === 'resize-l') {
            let newStart = initialStart + deltaMs;
            newStart = Math.max(minLimit, Math.min(newStart, initialEnd - minDuration));
            sub.startMs = newStart;

        } else if (type === 'resize-r') {
            let newEnd = initialEnd + deltaMs;
            newEnd = Math.min(maxLimit, Math.max(newEnd, initialStart + minDuration));
            sub.endMs = newEnd;
        }

        this.updateBlockDOM(index);
    },

    updateBlockDOM(idx) {
        const sub = AppState.subtitles[idx];
        const block = this.track.querySelector(`.tl-block[data-index="${idx}"]`);
        if (block) {
            const left = (sub.startMs / 1000) * this.pxPerSec;
            const width = ((sub.endMs - sub.startMs) / 1000) * this.pxPerSec;
            block.style.left = `${left}px`;
            block.style.width = `${width}px`;

            const elS = document.getElementById(`time-start-${idx}`);
            const elE = document.getElementById(`time-end-${idx}`);
            if (elS) elS.value = formatTimeMs(sub.startMs);
            if (elE) elE.value = formatTimeMs(sub.endMs);
        }
    },

    onBlockMouseUp(e) {
        if (!this.isDragging) return;
        this.isDragging = false;
        this.dragTarget = null;
        document.body.style.cursor = '';

        recordState();
        renderList();
        updateFinalOutput();
        this.render();
    }
};

// --- 初始化 ---
window.onload = () => {
    AppState.videoEl = document.getElementById('mainVideo');
    AppState.visualizerCanvas = document.getElementById('visualizerCanvas');
    AppState.visualizerCtx = AppState.visualizerCanvas.getContext('2d');
    const emptyEl = document.getElementById('emptyState');
    if (emptyEl) CACHED_EMPTY_STATE = emptyEl.outerHTML;

    // 加载配置
    document.getElementById('apiKeyInput').value = AppState.config.key;
    document.getElementById('apiBaseInput').value = AppState.config.url;
    document.getElementById('apiModelInput').value = AppState.config.model;
    document.getElementById('asrKeyInput').value = AppState.asrConfig.key || AppState.config.key;
    document.getElementById('asrBaseInput').value = AppState.asrConfig.url;
    document.getElementById('asrModelInput').value = AppState.asrConfig.model;
    document.getElementById('advBatchSize').value = AppState.advanced.batchSize;
    document.getElementById('advReqDelay').value = AppState.advanced.reqDelay;
    updateAdvDisplay();
    updateApiStatus();
    setSoftware('pr');

    // 事件绑定
    document.getElementById('inputSize').addEventListener('input', handleSliderInput);
    document.getElementById('inputLimit').addEventListener('input', handleSliderInput);
    document.getElementById('fontSelect').addEventListener('change', forceCalibration);
    document.getElementById('cardContainer').addEventListener('scroll', onScrollList);
    window.addEventListener('resize', () => { onScrollList(); resizeVisualizer(); });

    AppState.videoEl.addEventListener('timeupdate', () => { if (!AppState.isPlaying) onVideoTimeUpdate(); });
    AppState.videoEl.addEventListener('ended', () => { stopPlayback(); updatePlayIcon(); });

    loadProject();
    resizeVisualizer();

    document.addEventListener('keydown', handleGlobalKeydown);
    document.addEventListener('keyup', handleGlobalKeyup);

    document.body.addEventListener('dragenter', (e) => { e.preventDefault(); dragCounter++; document.getElementById('globalDropOverlay').classList.remove('hidden'); });
    document.body.addEventListener('dragover', (e) => { e.preventDefault(); });
    document.body.addEventListener('dragleave', (e) => { e.preventDefault(); dragCounter--; if (dragCounter === 0) document.getElementById('globalDropOverlay').classList.add('hidden'); });
    document.body.addEventListener('drop', (e) => {
        e.preventDefault(); e.stopPropagation(); dragCounter = 0;
        document.getElementById('globalDropOverlay').classList.add('hidden');
        if (e.dataTransfer.files.length) {
            for (const file of e.dataTransfer.files) {
                const name = file.name.toLowerCase();
                if (file.type.startsWith('video/') || file.type.startsWith('audio/') || ['.mp4', '.mov', '.mkv', '.flv', '.mp3', '.wav', '.m4a', '.aac'].some(ext => name.endsWith(ext))) loadMedia(file);
                else if (name.endsWith('.srt') || name.endsWith('.txt')) tryLoadSRT(file);
            }
        }
    });

    document.getElementById('rawTextArea').addEventListener('input', () => {
        const text = document.getElementById('rawTextArea').value;
        if (text) {
            AppState.subtitles = parseSRT(text);
            recordState();
            renderList();
            checkAllOverflows();
            updateFinalOutput();
            renderTimelineVisuals();
        }
    });

    forceCalibration();
    setInterval(() => saveProject(false), 30000);

    Timeline.init();
};

// --- [V45.0] 快捷键系统核心 ---
function matchKey(e, actionId) {
    const config = AppState.keyMap[actionId];
    if (!config) return false;

    const inputKey = e.key.toLowerCase();
    const keyMatch = (config.key === 'delete')
        ? (inputKey === 'delete' || inputKey === 'backspace')
        : (inputKey === config.key.toLowerCase());

    if (config.key === ' ' && e.code === 'Space') return !e.ctrlKey && !e.altKey;

    return keyMatch && e.ctrlKey === config.ctrl && e.altKey === config.alt && e.shiftKey === config.shift;
}

function handleGlobalKeydown(e) {
    if (e.isComposing) return;

    const activeTag = document.activeElement.tagName.toUpperCase();
    const isInput = activeTag === 'INPUT' || activeTag === 'TEXTAREA' || document.activeElement.isContentEditable;

    if (matchKey(e, 'save')) { e.preventDefault(); saveProject(true); return; }
    if (e.key === 'Escape') { toggleShortcuts(false); return; }
    if (e.key === '?' && !isInput) { e.preventDefault(); toggleShortcuts(true); return; }

    // 输入框内忽略逻辑
    if (isInput) return;

    if (e.key === 'Control' || e.key === 'Meta') {
        if (!AppState.isCtrlPressed) {
            AppState.isCtrlPressed = true;
            if (AppState.activeIndex !== -1) {
                const sub = AppState.subtitles[AppState.activeIndex];
                renderMonitorText(sub.cn || sub.text, false);
            }
        }
    }

    // 高阶剪刀 (Ctrl + Num + SplitKey)
    const splitKey = AppState.keyMap['split'].key.toLowerCase();
    if (e.ctrlKey && e.key.toLowerCase() === splitKey && AppState.activeNumberKey) {
        e.preventDefault(); splitSubtitle(); return;
    }

    if (e.key >= '1' && e.key <= '9') { AppState.activeNumberKey = parseInt(e.key); }

    // 动作映射
    if (matchKey(e, 'play')) { e.preventDefault(); togglePlayback(); return; }
    if (matchKey(e, 'undo')) { e.preventDefault(); undo(); return; }
    if (matchKey(e, 'redo')) { e.preventDefault(); redo(); return; }

    if (matchKey(e, 'seek_back')) { e.preventDefault(); seekRelative(-2); return; }
    if (matchKey(e, 'seek_fwd')) { e.preventDefault(); seekRelative(2); return; }
    if (matchKey(e, 'nav_prev')) { e.preventDefault(); navigateList(-1); return; }
    if (matchKey(e, 'nav_next')) { e.preventDefault(); navigateList(1); return; }

    if (matchKey(e, 'split')) { e.preventDefault(); splitSubtitle(); return; }
    if (matchKey(e, 'merge')) { e.preventDefault(); smartMerge(); return; }
    if (matchKey(e, 'delete')) { e.preventDefault(); deleteSelectedSubtitles(); return; }
    if (matchKey(e, 'retranslate')) { e.preventDefault(); if (selectedIndices.size > 0) retranslateSingle(Array.from(selectedIndices)[0]); return; }
    if (matchKey(e, 'record')) { e.preventDefault(); toggleRecording(); return; }

    // 保留的硬编码 (非通用)
    if (e.ctrlKey && e.key === '[') { e.preventDefault(); if (AppState.activeIndex !== -1 && !AppState.isRecording) setSubtitleStart(AppState.activeIndex, getCurMs()); return; }
    if (e.ctrlKey && e.key === ']') { e.preventDefault(); if (AppState.activeIndex !== -1 && !AppState.isRecording) setSubtitleEnd(AppState.activeIndex, getCurMs()); return; }
}

function handleGlobalKeyup(e) {
    if (e.key === 'Control' || e.key === 'Meta') {
        AppState.isCtrlPressed = false;
        if (AppState.activeIndex !== -1) {
            const sub = AppState.subtitles[AppState.activeIndex];
            renderMonitorText(sub.cn || sub.text, false);
        }
    }
    if (e.key >= '1' && e.key <= '9') { AppState.activeNumberKey = null; }
}

// --- 快捷键面板逻辑 ---
function toggleShortcuts(show) {
    const el = document.getElementById('shortcutModal');
    if (show) {
        el.classList.add('show');
        renderShortcutUI();
    } else {
        el.classList.remove('show');
    }
}

function renderShortcutUI() {
    const list = document.getElementById('shortcutList');
    list.innerHTML = '';

    Object.entries(AppState.keyMap).forEach(([id, config]) => {
        const row = document.createElement('div');
        row.className = 'shortcut-row';

        const displayKey = config.key === ' ' ? 'Space' : config.key.toUpperCase();

        row.innerHTML = `
                    <div class="text-xs text-gray-400 font-medium">${config.label}</div>
                    <input type="text" readonly value="${displayKey}" class="key-input" onfocus="captureShortcut('${id}', this)" onblur="this.classList.remove('recording')">
                    <div class="flex gap-2">
                        <label class="flex items-center gap-1 text-[10px] text-gray-500 cursor-pointer">
                            <input type="checkbox" ${config.ctrl ? 'checked' : ''} onchange="updateKeyMod('${id}', 'ctrl', this.checked)"> Ctrl
                        </label>
                        <label class="flex items-center gap-1 text-[10px] text-gray-500 cursor-pointer">
                            <input type="checkbox" ${config.alt ? 'checked' : ''} onchange="updateKeyMod('${id}', 'alt', this.checked)"> Alt
                        </label>
                        <label class="flex items-center gap-1 text-[10px] text-gray-500 cursor-pointer">
                            <input type="checkbox" ${config.shift ? 'checked' : ''} onchange="updateKeyMod('${id}', 'shift', this.checked)"> Shift
                        </label>
                    </div>
                `;
        list.appendChild(row);
    });
}

function captureShortcut(id, input) {
    input.classList.add('recording');
    input.value = "请按键...";

    const handler = (e) => {
        e.preventDefault();
        e.stopPropagation();

        // 忽略修饰键本身
        if (['Control', 'Alt', 'Shift', 'Meta'].includes(e.key)) return;

        const key = e.key.toLowerCase();
        const displayKey = key === ' ' ? 'Space' : key.toUpperCase();

        AppState.keyMap[id].key = key;
        input.value = displayKey;
        input.blur(); // 结束录制
        saveShortcuts();
    };

    input.onkeydown = handler;
    input.onblur = () => {
        input.onkeydown = null;
        input.classList.remove('recording');
        // 恢复显示
        const k = AppState.keyMap[id].key;
        input.value = k === ' ' ? 'Space' : k.toUpperCase();
    };
}

function updateKeyMod(id, mod, val) {
    AppState.keyMap[id][mod] = val;
    saveShortcuts();
}

function saveShortcuts() {
    localStorage.setItem('s1_keymap', JSON.stringify(AppState.keyMap));
}

function resetShortcuts() {
    if (!confirm("恢复默认快捷键？")) return;
    localStorage.removeItem('s1_keymap');
    location.reload();
}

// --- 双语预览 ---
function toggleBilingual() {
    AppState.isBilingual = !AppState.isBilingual;
    const btn = document.getElementById('btnBilingual');
    if (AppState.isBilingual) {
        btn.classList.add('text-indigo-400', 'border-indigo-500');
        btn.innerHTML = '<i class="fas fa-language"></i> 双语: 开';
    } else {
        btn.classList.remove('text-indigo-400', 'border-indigo-500');
        btn.innerHTML = '<i class="fas fa-language"></i> 双语: 关';
    }
    if (AppState.activeIndex !== -1) {
        const sub = AppState.subtitles[AppState.activeIndex];
        renderMonitorText(sub.cn || sub.text, false);
    }
}

function getCurMs() { return AppState.hasVideo ? Math.floor(AppState.videoEl.currentTime * 1000) : 0; }
function seekRelative(sec) { if (!AppState.hasVideo) return; AppState.videoEl.currentTime = Math.max(0, Math.min(AppState.videoEl.duration, AppState.videoEl.currentTime + sec)); if (!AppState.isPlaying) onVideoTimeUpdate(); }
function navigateList(dir) {
    if (AppState.subtitles.length === 0) return;
    let base = AppState.activeIndex;
    if (base === -1 && selectedIndices.size > 0) base = Array.from(selectedIndices)[0];
    if (base === -1) base = 0;
    let next = Math.max(0, Math.min(AppState.subtitles.length - 1, base + dir));
    previewItem(next); selectedIndices.clear(); selectedIndices.add(next);
    updateSelectionUI(); updateRowClass(document.getElementById(`card-${next}`), next);
}

function updateAdvDisplay() {
    document.getElementById('displayBatchSize').innerText = document.getElementById('advBatchSize').value;
    document.getElementById('displayReqDelay').innerText = document.getElementById('advReqDelay').value + 'ms';
}

function onScrollList() {
    if (AppState.subtitles.length === 0) return;
    const container = document.getElementById('cardContainer');
    const scrollTop = container.scrollTop;
    const height = container.clientHeight;
    const totalCount = AppState.subtitles.length;

    let start = Math.floor(scrollTop / AppState.virtual.rowHeight) - AppState.virtual.buffer;
    let end = Math.ceil((scrollTop + height) / AppState.virtual.rowHeight) + AppState.virtual.buffer;
    start = Math.max(0, start); end = Math.min(totalCount, end);

    if (start !== AppState.virtual.startIndex || end !== AppState.virtual.endIndex) {
        AppState.virtual.startIndex = start; AppState.virtual.endIndex = end;
        renderVisibleItems();
    }
}

function renderList() {
    const container = document.getElementById('cardContainer');
    const sizer = document.getElementById('virtualSizer');
    const emptyEl = document.getElementById('emptyState');
    container.querySelectorAll('.list-row').forEach(el => el.remove());
    if (!AppState.subtitles.length) {
        sizer.style.height = '0px'; if (emptyEl) emptyEl.classList.remove('hidden'); return;
    }
    if (emptyEl) emptyEl.classList.add('hidden');
    sizer.style.height = `${AppState.subtitles.length * AppState.virtual.rowHeight}px`;
    AppState.virtual.startIndex = -1; onScrollList();
    Timeline.render();
}

function renderVisibleItems() {
    const container = document.getElementById('cardContainer');
    const existingRows = new Map();
    container.querySelectorAll('.list-row').forEach(el => { existingRows.set(parseInt(el.dataset.index), el); });

    const neededIndices = new Set();
    for (let i = AppState.virtual.startIndex; i < AppState.virtual.endIndex; i++) neededIndices.add(i);
    existingRows.forEach((el, idx) => { if (!neededIndices.has(idx)) el.remove(); });

    const frag = document.createDocumentFragment();
    for (let i = AppState.virtual.startIndex; i < AppState.virtual.endIndex; i++) {
        if (existingRows.has(i)) { updateRowClass(existingRows.get(i), i); continue; }

        const s = AppState.subtitles[i];
        if (!s) continue;

        const d = document.createElement('div');
        d.id = `card-${i}`; d.dataset.index = i; d.className = "list-row";
        d.style.top = `${i * AppState.virtual.rowHeight}px`;
        updateRowClass(d, i);

        const displayText = (s.text && s.text.trim()) ? s.text : '<span class="text-gray-600 italic opacity-50">(原文...)</span>';
        const displayCn = (s.cn && s.cn.trim()) ? s.cn : '...';

        d.innerHTML = `
                    <div class="row-handle" onclick="toggleSelection(${i}, event)">
                        <span class="text-gray-600 text-[10px] font-mono opacity-50">#${s.id}</span>
                        <div class="overflow-icon text-red-500 text-xs ml-1 opacity-0 transition-opacity"><i class="fas fa-exclamation-circle"></i></div>
                    </div>

                    <div class="row-content">
                        <div class="flex items-center justify-between mb-1 gap-1">
                            <div class="flex items-center gap-0.5">
                                <button class="btn-icon-tiny" onclick="event.stopPropagation(); setSubtitleStart(${i}, Math.floor(AppState.videoEl.currentTime*1000))">[</button>
                                <input id="time-start-${i}" class="time-input" value="${formatTimeMs(s.startMs)}" onblur="manualTimeEdit(${i}, 'start', this.value)" onclick="event.stopPropagation()">
                            </div>
                            <div class="h-[1px] w-2 bg-gray-700"></div>
                            <div class="flex items-center gap-0.5">
                                <input id="time-end-${i}" class="time-input" value="${formatTimeMs(s.endMs)}" onblur="manualTimeEdit(${i}, 'end', this.value)" onclick="event.stopPropagation()">
                                <button class="btn-icon-tiny" onclick="event.stopPropagation(); setSubtitleEnd(${i}, Math.floor(AppState.videoEl.currentTime*1000))">]</button>
                            </div>
                        </div>
                        <div class="text-[10px] text-gray-500 mb-1 truncate cursor-text hover:text-gray-300 transition-colors" contenteditable="true" onblur="manualRawEdit(${i}, this.innerText)">${displayText}</div>
                        <div id="sub-text-${i}" class="text-sm editable-target editable-textarea ${s.cn ? 'text-indigo-300' : 'text-gray-500 italic'}" contenteditable="true" onblur="manualEditBlur(${i})" oninput="manualEdit(${i}, this.innerText)">${displayCn}</div>
                    </div>

                    <div class="scrubber-track" 
                        onmousedown="startLocalScrub(event, ${i})" 
                        onmousemove="updateScrubTooltip(event, ${i})"
                        onmouseleave="hideScrubTooltip(event)">
                        <div id="scrubber-prog-${i}" class="scrubber-progress"></div>
                        <div id="scrubber-tip-${i}" class="scrubber-tooltip">0.0s</div>
                    </div>

                    <div class="row-handle relative group/right" onclick="toggleSelection(${i}, event)">
                        <button class="w-6 h-6 rounded hover:bg-white/10 btn-refine" title="AI 精修 / 重译 (Alt+R)" onclick="event.stopPropagation(); retranslateSingle(${i})">
                            <i class="fas fa-sync-alt text-xs"></i>
                        </button>
                    </div>
                `;
        frag.appendChild(d);
    }
    container.appendChild(frag);
}

function updateRowClass(el, idx) {
    el.classList.remove('selected', 'active', 'playing', 'overflow-error', 'missing-error', 'translating');
    if (selectedIndices.has(idx)) el.classList.add('selected');
    if (idx === AppState.activeIndex) el.classList.add('active');
    if (AppState.overflowIndices.includes(idx)) el.classList.add('overflow-error');
    if (AppState.missingIndices.includes(idx)) el.classList.add('missing-error');
    if (isInProcessingRange(idx)) el.classList.add('translating');

    if (AppState.isPlaying) {
        const currentMs = AppState.hasVideo ? AppState.videoEl.currentTime * 1000 : 0;
        const s = AppState.subtitles[idx];
        if (s && currentMs >= s.startMs && currentMs < s.endMs) el.classList.add('playing');
    }
}

function startLocalScrub(e, index) {
    if (!AppState.hasVideo) return;
    e.stopPropagation();
    isLocalScrubbing = true;
    handleScrubLogic(e, index);
    const moveHandler = (evt) => handleScrubLogic(evt, index);
    const upHandler = () => {
        isLocalScrubbing = false;
        document.removeEventListener('mousemove', moveHandler);
        document.removeEventListener('mouseup', upHandler);
        hideScrubTooltip(null, index);
    };
    document.addEventListener('mousemove', moveHandler);
    document.addEventListener('mouseup', upHandler);
}

function handleScrubLogic(e, index) {
    const track = e.target.closest('.scrubber-track') || document.querySelector(`#card-${index} .scrubber-track`);
    if (!track) return;
    const rect = track.getBoundingClientRect();
    const pct = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    const sub = AppState.subtitles[index];
    const duration = sub.endMs - sub.startMs;
    const targetMs = sub.startMs + (duration * pct);
    AppState.videoEl.currentTime = targetMs / 1000;
    if (!AppState.isPlaying) {
        updateLocalTimelineUI(targetMs);
        onVideoTimeUpdate();
    }
    showTooltip(track, pct, index, (duration * pct) / 1000);
}

function updateScrubTooltip(e, index) {
    if (isLocalScrubbing) return;
    const track = e.currentTarget;
    const rect = track.getBoundingClientRect();
    const pct = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    const sub = AppState.subtitles[index];
    const offsetSec = ((sub.endMs - sub.startMs) * pct) / 1000;
    showTooltip(track, pct, index, offsetSec);
}

function showTooltip(track, pct, index, seconds) {
    const tip = document.getElementById(`scrubber-tip-${index}`);
    if (tip) {
        tip.style.opacity = '1';
        tip.style.left = `${pct * 100}%`;
        tip.innerText = `+${seconds.toFixed(1)}s`;
    }
}

function hideScrubTooltip(e, index) {
    if (isLocalScrubbing) return;
    let tip;
    if (index !== undefined) {
        tip = document.getElementById(`scrubber-tip-${index}`);
    } else if (e && e.target) {
        const track = e.target.closest('.scrubber-track');
        if (track) tip = track.querySelector('.scrubber-tooltip');
    }
    if (tip) tip.style.opacity = '0';
}

function updateLocalTimelineUI(currentMs) {
    let targetIndices = [];
    if (AppState.activeIndex !== -1) targetIndices.push(AppState.activeIndex);
    if (selectedIndices.size === 1) targetIndices.push(Array.from(selectedIndices)[0]);
    targetIndices = [...new Set(targetIndices)];

    targetIndices.forEach(idx => {
        const sub = AppState.subtitles[idx];
        const bar = document.getElementById(`scrubber-prog-${idx}`);
        if (sub && bar) {
            const duration = sub.endMs - sub.startMs;
            let pct = (currentMs - sub.startMs) / duration;
            pct = Math.max(0, Math.min(1, pct)) * 100;
            bar.style.width = `${pct}%`;
        }
    });
}

function deleteSelectedSubtitles() {
    if (selectedIndices.size === 0) return;
    if (!confirm(`确认删除选中的 ${selectedIndices.size} 条字幕吗？`)) return;
    recordState();
    const sorted = Array.from(selectedIndices).sort((a, b) => b - a);
    sorted.forEach(idx => AppState.subtitles.splice(idx, 1));
    selectedIndices.clear();
    sortSubtitles();
    renderList();
    renderTimelineVisuals();
    updateFinalOutput();
    showToast("🗑️ 已删除字幕");
}

function splitSubtitle() {
    if (!AppState.hasVideo) return showToast("请先加载视频");

    const currentMs = Math.floor(AppState.videoEl.currentTime * 1000);
    const idx = AppState.subtitles.findIndex(s => currentMs > s.startMs && currentMs < s.endMs);
    if (idx === -1) return showToast("当前位置无字幕");

    const originalSub = AppState.subtitles[idx];
    const isTargetingText = (!originalSub.cn || originalSub.cn.trim() === "");
    const contentToSplit = isTargetingText ? originalSub.text : originalSub.cn;

    let splitIndex = -1;
    let partA = "", partB = "";

    if (AppState.activeNumberKey) {
        let count = 0;
        for (let i = 0; i < contentToSplit.length; i++) {
            const char = contentToSplit[i];
            if (char === ' ' || ['?', '!', '？', '！', '。', '.'].includes(char)) {
                const isPunctuation = ['?', '!', '？', '！', '。', '.'].includes(char);
                if (char === ' ' || (isPunctuation && i + 1 < contentToSplit.length)) {
                    count++;
                    if (count === AppState.activeNumberKey) {
                        splitIndex = (char === ' ') ? i : i + 1;
                        break;
                    }
                }
            }
        }
    } else {
        const match = contentToSplit.match(/[ \.,!\?，。！？]/);
        if (match) {
            splitIndex = match[0] === ' ' ? match.index : match.index + 1;
        } else {
            splitIndex = -1;
        }
    }

    if (splitIndex !== -1) {
        partA = contentToSplit.substring(0, splitIndex).trim();
        partB = contentToSplit.substring(splitIndex).trim();
    } else {
        partA = contentToSplit;
        partB = "";
    }

    recordState();
    const newSub = {
        id: originalSub.id + 1,
        startMs: currentMs,
        endMs: originalSub.endMs,
        text: "",
        cn: ""
    };
    originalSub.endMs = currentMs;

    if (isTargetingText) {
        originalSub.text = partA;
        newSub.text = partB;
    } else {
        originalSub.cn = partA;
        newSub.cn = partB;
        newSub.text = originalSub.text;
    }

    AppState.subtitles.splice(idx + 1, 0, newSub);
    sortSubtitles(); renderList(); checkAllOverflows(); updateFinalOutput(); renderTimelineVisuals();
    flashRow(idx, 'green'); flashRow(idx + 1, 'green');

    const mode = AppState.activeNumberKey ? `按第 ${AppState.activeNumberKey} 处拆分` : "拆分";
    showToast(`✂️ 已${mode}`);

    const newCurrentIdx = AppState.subtitles.findIndex(s => currentMs >= s.startMs && currentMs < s.endMs);
    if (newCurrentIdx !== -1) {
        previewItem(newCurrentIdx);
        selectedIndices.clear(); selectedIndices.add(newCurrentIdx);
        updateRowClass(document.getElementById(`card-${newCurrentIdx}`), newCurrentIdx);
    }
}

function smartMerge() {
    let targets = [];
    if (selectedIndices.size > 1) { targets = Array.from(selectedIndices).sort((a, b) => a - b); } else {
        if (!AppState.hasVideo) return showToast("请先加载视频");
        if (AppState.activeIndex === -1) return showToast("请先选择一条字幕");
        if (AppState.activeIndex >= AppState.subtitles.length - 1) return showToast("已经是最后一条");
        targets = [AppState.activeIndex, AppState.activeIndex + 1];
    }
    for (let i = 0; i < targets.length - 1; i++) if (targets[i + 1] !== targets[i] + 1) return showToast("只能合并相邻的字幕");
    recordState();
    const firstIdx = targets[0]; const primarySub = AppState.subtitles[firstIdx]; const lastIdx = targets[targets.length - 1];
    primarySub.endMs = AppState.subtitles[lastIdx].endMs;
    let mergedText = ""; let mergedCn = "";
    targets.forEach((idx, i) => { const s = AppState.subtitles[idx]; const sep = i === 0 ? "" : " "; if (s.text) mergedText += sep + s.text; if (s.cn) mergedCn += sep + s.cn; });
    primarySub.text = mergedText; primarySub.cn = mergedCn;
    for (let i = targets.length - 1; i > 0; i--) AppState.subtitles.splice(targets[i], 1);
    sortSubtitles(); renderList(); updateFinalOutput(); renderTimelineVisuals();

    selectedIndices.clear(); selectedIndices.add(firstIdx);
    previewItem(firstIdx);
    updateSelectionUI(); showToast(`🔗 已合并 ${targets.length} 条`);
}

function isInProcessingRange(index) { if (!AppState.processingRange) return false; return index >= AppState.processingRange.start && index <= AppState.processingRange.end; }
function sortSubtitles() { AppState.subtitles.sort((a, b) => a.startMs - b.startMs); AppState.subtitles.forEach((s, i) => s.id = i + 1); }
function toggleRecording() {
    if (!AppState.hasVideo) return alert("请先加载媒体文件！");
    if (AppState.isProcessing) return alert("AI 翻译期间无法补录");
    const btn = document.getElementById('btnCapture'); const txt = document.getElementById('captureText');
    const currentMs = Math.floor(AppState.videoEl.currentTime * 1000);
    if (!AppState.isRecording) {
        AppState.isRecording = true; AppState.recordingStartMs = currentMs;
        btn.classList.add('btn-capture-active'); txt.innerText = "点击结束";
        if (AppState.videoEl.paused) togglePlayback();
    } else {
        const endMs = currentMs;
        if (endMs <= AppState.recordingStartMs) { showToast("无效时间"); resetRecordingUI(); return; }
        handleSubtitleOverwrite(AppState.recordingStartMs, endMs);
        if (!AppState.videoEl.paused) togglePlayback();
        resetRecordingUI(); showToast("字幕已补录");
    }
}
function resetRecordingUI() { AppState.isRecording = false; AppState.recordingStartMs = null; const btn = document.getElementById('btnCapture'); const txt = document.getElementById('captureText'); btn.classList.remove('btn-capture-active'); txt.innerText = "补录"; }

function handleSubtitleOverwrite(newStart, newEnd) {
    recordState();
    const newSub = { id: 0, startMs: newStart, endMs: newEnd, text: "", cn: "" };
    const newList = [];
    for (let i = 0; i < AppState.subtitles.length; i++) {
        const sub = AppState.subtitles[i];
        if (sub.endMs <= newStart) { newList.push(sub); continue; }
        if (sub.startMs >= newEnd) { newList.push(sub); continue; }
        if (sub.startMs >= newStart && sub.endMs <= newEnd) { continue; }
        if (sub.startMs < newEnd && sub.endMs > newEnd) { sub.startMs = newEnd; newList.push(sub); continue; }
        if (sub.startMs < newStart && sub.endMs > newStart) { sub.endMs = newStart; newList.push(sub); continue; }
        if (sub.startMs < newStart && sub.endMs > newEnd) { const subB = JSON.parse(JSON.stringify(sub)); sub.endMs = newStart; subB.startMs = newEnd; newList.push(sub); newList.push(subB); continue; }
    }
    AppState.subtitles = newList; AppState.subtitles.push(newSub); sortSubtitles();
    renderList(); renderTimelineVisuals(); updateFinalOutput();
    const newIdx = AppState.subtitles.findIndex(s => s === newSub);
    if (newIdx !== -1) { previewItem(newIdx); setTimeout(() => { const t = document.getElementById(`sub-text-${newIdx}`); if (t) t.focus(); }, 100); }
}

function resetEditorState() { AppState.subtitles = []; AppState.history = []; AppState.historyIndex = -1; AppState.activeIndex = -1; selectedIndices.clear(); document.getElementById('rawTextArea').value = ""; renderList(); renderMonitorText("", false); updateSelectionUI(); }
function tryLoadSRT(file) { if (AppState.subtitles.length > 0) { if (!confirm("⚠️ 导入新文件将覆盖当前进度，继续？")) return; } resetEditorState(); loadFile(file); }

async function startVideoTranscription() {
    if (!AppState.hasVideo || !AppState.currentVideoFile) return alert("请先加载媒体文件！");
    if (AppState.currentVideoFile.size > 500 * 1024 * 1024) return alert("⚠️ 文件过大 (>500MB)，建议使用本地提取工具");
    const key = AppState.asrConfig.key || AppState.config.key;
    if (!key) return alert("请先在设置中配置 Whisper API Key");
    if (AppState.subtitles.length > 0) { if (!confirm("⚠️ 新的识别将覆盖当前字幕，是否继续？")) return; }

    resetEditorState();
    const btn = document.getElementById('btnTranscribe'); const prog = document.getElementById('transcribeProgress'); const progText = document.getElementById('transcribeText');
    try {
        btn.classList.add('hidden'); prog.classList.remove('hidden'); progText.innerText = "提取音频...";
        const audioBlob = await extractAudioFromVideo(AppState.currentVideoFile);
        progText.innerText = `上传中 (${(audioBlob.size / 1024 / 1024).toFixed(1)}MB)...`;
        const jsonData = await callWhisperAPI(audioBlob);
        progText.innerText = "处理中...";
        if (!jsonData.segments) throw new Error("API返回格式错误");
        AppState.subtitles = jsonData.segments.map((seg, index) => ({ id: index + 1, startMs: Math.floor(seg.start * 1000), endMs: Math.floor(seg.end * 1000), text: seg.text.trim(), cn: '' }));
        const rawSrt = AppState.subtitles.map(s => `${s.id}\n${formatSrtTime(s.startMs)} --> ${formatSrtTime(s.endMs)}\n${s.text}\n`).join('\n');
        document.getElementById('rawTextArea').value = rawSrt; recordState(); renderList(); checkAllOverflows(); updateFinalOutput(); renderTimelineVisuals(); showToast("识别完成");
    } catch (e) { alert("失败: " + e.message); } finally { btn.classList.remove('hidden'); prog.classList.add('hidden'); }
}

async function extractAudioFromVideo(file) {
    if (file.type.startsWith('audio/')) return file;
    const ctx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
    const arrayBuffer = await file.arrayBuffer();
    const audioBuffer = await ctx.decodeAudioData(arrayBuffer);
    return new Promise((resolve, reject) => {
        const channelData = audioBuffer.getChannelData(0);
        const workerCode = `self.onmessage = function(e) { self.postMessage(wav(e.data.buffer, e.data.sampleRate)); }; function wav(samples, sampleRate) { const buffer = new ArrayBuffer(44 + samples.length * 2); const view = new DataView(buffer); write(view, 0, 'RIFF'); view.setUint32(4, 36 + samples.length * 2, true); write(view, 8, 'WAVE'); write(view, 12, 'fmt '); view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true); view.setUint32(24, sampleRate, true); view.setUint32(28, sampleRate * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true); write(view, 36, 'data'); view.setUint32(40, samples.length * 2, true); let offset = 44; for (let i = 0; i < samples.length; i++) { let s = Math.max(-1, Math.min(1, samples[i])); s = s < 0 ? s * 0x8000 : s * 0x7FFF; view.setInt16(offset, s, true); offset += 2; } return buffer; } function write(v, o, s) { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); }`;
        const worker = new Worker(URL.createObjectURL(new Blob([workerCode], { type: "application/javascript" })));
        worker.onmessage = (e) => { resolve(new Blob([e.data], { type: "audio/wav" })); worker.terminate(); };
        worker.onerror = (e) => { reject(e); worker.terminate(); };
        worker.postMessage({ buffer: channelData, sampleRate: 16000 });
    });
}
async function callWhisperAPI(audioBlob) {
    const formData = new FormData(); formData.append("file", audioBlob, "audio.wav"); formData.append("model", AppState.asrConfig.model); formData.append("response_format", "verbose_json");
    const lang = document.getElementById('inputLang').value; if (lang) formData.append("language", lang);
    const key = AppState.asrConfig.key || AppState.config.key; const res = await fetch(`${AppState.asrConfig.url.replace(/\/$/, '')}/audio/transcriptions`, { method: "POST", headers: { "Authorization": `Bearer ${key}` }, body: formData });
    if (!res.ok) throw new Error(await res.text()); return await res.json();
}

function recordState() { const s = JSON.parse(JSON.stringify(AppState.subtitles)); if (AppState.historyIndex < AppState.history.length - 1) AppState.history = AppState.history.slice(0, AppState.historyIndex + 1); AppState.history.push(s); if (AppState.history.length > 50) AppState.history.shift(); AppState.historyIndex = AppState.history.length - 1; updateHistoryButtons(); setStatus('editing'); debouncedSave(); }
function undo() { if (AppState.historyIndex > 0) { AppState.historyIndex--; restoreSnapshot(AppState.history[AppState.historyIndex]); } updateHistoryButtons(); }
function redo() { if (AppState.historyIndex < AppState.history.length - 1) { AppState.historyIndex++; restoreSnapshot(AppState.history[AppState.historyIndex]); } updateHistoryButtons(); }
function restoreSnapshot(data) { AppState.subtitles = JSON.parse(JSON.stringify(data)); renderList(); checkAllOverflows(); updateFinalOutput(selectedIndices.size > 1); renderTimelineVisuals(); if (AppState.activeIndex !== -1) previewItem(AppState.activeIndex); }
function updateHistoryButtons() { document.getElementById('btnUndo').classList.toggle('disabled', AppState.historyIndex <= 0); document.getElementById('btnRedo').classList.toggle('disabled', AppState.historyIndex >= AppState.history.length - 1); }
function saveProject(m = false) { if (AppState.subtitles.length === 0) return; const d = { subtitles: AppState.subtitles, history: AppState.history, historyIndex: AppState.historyIndex, context: document.getElementById('inputContext').value, timestamp: Date.now() }; localStorage.setItem('s1_project_backup', JSON.stringify(d)); setStatus('saved'); if (m) showToast("进度已保存"); }
function loadProject() { const r = localStorage.getItem('s1_project_backup'); if (!r) return; try { const d = JSON.parse(r); if (Date.now() - d.timestamp < 12 * 3600 * 1000) { AppState.subtitles = d.subtitles; AppState.history = d.history; AppState.historyIndex = d.historyIndex; document.getElementById('inputContext').value = d.context || ""; document.getElementById('emptyState').classList.add('hidden'); renderList(); updateFinalOutput(); renderTimelineVisuals(); updateHistoryButtons(); setStatus('saved'); } } catch (e) { } }
function setStatus(s) { const i = document.getElementById('saveIndicator'); const t = document.getElementById('lastSavedTime'); if (s === 'saved') { i.className = 'status-indicator status-saved'; t.innerText = '已保存 ' + new Date().toLocaleTimeString(); } else { i.className = 'status-indicator status-editing'; t.innerText = '未保存...'; } }
function showToast(m) { const t = document.getElementById('toast'); t.innerHTML = `<i class="fas fa-check-circle mr-2"></i>${m}`; t.classList.add('show'); setTimeout(() => t.classList.remove('show'), 2000); }

function setSubtitleStart(idx, timeMs) { if (isInProcessingRange(idx)) return alert("AI 正在翻译此行"); recordState(); const sub = AppState.subtitles[idx]; if (timeMs >= sub.endMs) return; if (idx > 0 && AppState.subtitles[idx - 1].endMs >= timeMs) AppState.subtitles[idx - 1].endMs = Math.max(AppState.subtitles[idx - 1].startMs, timeMs - 50); sub.startMs = timeMs; updateRowTimeUI(idx); flashRow(idx, 'green'); updateFinalOutput(selectedIndices.size > 1); renderTimelineVisuals(); renderMonitorText(sub.cn || sub.text, false); }
function setSubtitleEnd(idx, timeMs) { if (isInProcessingRange(idx)) return alert("AI 正在翻译此行"); recordState(); const sub = AppState.subtitles[idx]; if (timeMs <= sub.startMs) return; if (idx < AppState.subtitles.length - 1 && AppState.subtitles[idx + 1].startMs <= timeMs) AppState.subtitles[idx + 1].startMs = timeMs + 50; sub.endMs = timeMs; updateRowTimeUI(idx); flashRow(idx, 'green'); updateFinalOutput(selectedIndices.size > 1); renderTimelineVisuals(); renderMonitorText("", false); }
function manualTimeEdit(idx, type, valStr) { if (isInProcessingRange(idx)) return; recordState(); const ms = parseTimeMs(valStr); if (type === 'start') setSubtitleStart(idx, ms); else setSubtitleEnd(idx, ms); }
function applyGlobalOffset() { const val = parseInt(document.getElementById('offsetInput').value); if (!val) return; recordState(); AppState.subtitles.forEach(s => { s.startMs = Math.max(0, s.startMs + val); s.endMs = Math.max(0, s.endMs + val); }); renderList(); updateFinalOutput(selectedIndices.size > 1); renderTimelineVisuals(); showToast(`已应用 ${val}ms 偏移`); }
function manualEditBlur(i) { recordState(); }
function manualRawEdit(i, txt) { if (txt.includes('(原文...)')) txt = ""; AppState.subtitles[i].text = txt.trim(); recordState(); }
function manualEdit(i, txt) {
    if (isInProcessingRange(i)) return;
    AppState.subtitles[i].cn = txt;
    if (i === AppState.activeIndex) renderMonitorText(txt.trim(), false);
    debouncedUpdateFinal();
    checkAllOverflows();
    setStatus('editing');
    debouncedSave();
}

function updateRowTimeUI(idx) { const s = AppState.subtitles[idx]; const elS = document.getElementById(`time-start-${idx}`); const elE = document.getElementById(`time-end-${idx}`); if (elS) elS.value = formatTimeMs(s.startMs); if (elE) elE.value = formatTimeMs(s.endMs); }
function flashRow(idx, color) { const row = document.getElementById(`card-${idx}`); if (!row) return; const cls = color === 'green' ? 'animate-flash-green' : 'animate-flash-red'; row.classList.add(cls); setTimeout(() => row.classList.remove(cls), 500); }

function loadMedia(file) {
    if (!file) return;
    AppState.currentVideoFile = file;
    AppState.originalFileName = file.name.substring(0, file.name.lastIndexOf('.')) || file.name;

    const url = URL.createObjectURL(file);
    if (AppState.animationFrameId) cancelAnimationFrame(AppState.animationFrameId);
    AppState.hasVideo = true;
    AppState.videoEl.src = url;
    if (!AppState.audioCtx) {
        const AudioContext = window.AudioContext || window.webkitAudioContext;
        AppState.audioCtx = new AudioContext();
        AppState.analyser = AppState.audioCtx.createAnalyser();
        AppState.analyser.fftSize = 256;
        if (!AppState.audioSource) {
            AppState.audioSource = AppState.audioCtx.createMediaElementSource(AppState.videoEl);
            AppState.audioSource.connect(AppState.analyser);
            AppState.audioSource.connect(AppState.audioCtx.destination);
        }
    }
    const isAudio = file.type.startsWith('audio/') || ['.mp3', '.wav', '.m4a', '.aac'].some(ext => file.name.toLowerCase().endsWith(ext));
    AppState.isAudioMode = isAudio;
    if (isAudio) {
        AppState.videoEl.style.display = 'none';
        document.getElementById('audioPlaceholder').classList.remove('hidden');
        document.getElementById('audioPlaceholder').style.display = 'flex';
        document.getElementById('audioFileName').innerText = file.name;
    } else {
        AppState.videoEl.style.display = 'block';
        document.getElementById('audioPlaceholder').classList.add('hidden');
    }
    ['btnTranscribe', 'btnCapture', 'btnSplit', 'btnMerge'].forEach(id => { const b = document.getElementById(id); b.classList.remove('opacity-50', 'cursor-not-allowed'); b.title = ""; });
    document.getElementById('videoModeBadge').classList.remove('hidden');
    document.getElementById('simModeBadge').classList.add('hidden');
    AppState.videoEl.onloadedmetadata = () => { renderTimelineVisuals(); Timeline.render(); };
}

function resizeVisualizer() { if (AppState.visualizerCanvas) { AppState.visualizerCanvas.width = AppState.visualizerCanvas.clientWidth; AppState.visualizerCanvas.height = AppState.visualizerCanvas.clientHeight; } }
function drawVisualizer() { if (!AppState.isPlaying || !AppState.analyser) return; const bufferLength = AppState.analyser.frequencyBinCount; const dataArray = new Uint8Array(bufferLength); AppState.analyser.getByteFrequencyData(dataArray); const ctx = AppState.visualizerCtx; const width = AppState.visualizerCanvas.width; const height = AppState.visualizerCanvas.height; const barWidth = (width / bufferLength) * 2.5; let barHeight; let x = 0; ctx.clearRect(0, 0, width, height); for (let i = 0; i < bufferLength; i++) { barHeight = dataArray[i] / 2; const r = barHeight + 25 * (i / bufferLength); const g = 250 * (i / bufferLength); const b = 50; ctx.fillStyle = `rgb(${r},${g},${b})`; ctx.fillRect(x, height - barHeight, barWidth, barHeight); x += barWidth + 1; } if (AppState.isPlaying) requestAnimationFrame(drawVisualizer); }

function togglePlayback() {
    if (AppState.hasVideo) {
        if (AppState.videoEl.paused) {
            if (AppState.audioCtx && AppState.audioCtx.state === 'suspended') AppState.audioCtx.resume();
            AppState.videoEl.play();
            startPlaybackLoop();
            drawVisualizer();
        } else {
            AppState.videoEl.pause();
            stopPlayback();
        }
    } else {
        if (AppState.isPlaying) { stopPlayback(); } else { if (!AppState.subtitles.length) return; startPlaybackLoop(true); }
    }
}

function startPlaybackLoop(isSim = false) { AppState.isPlaying = true; updatePlayIcon(); let startMs = 0; let startTimeReal = Date.now(); if (isSim) { startMs = AppState.activeIndex !== -1 ? AppState.subtitles[AppState.activeIndex].startMs : AppState.subtitles[0].startMs; } const loop = () => { if (!AppState.isPlaying) return; let currentMs = 0; if (!isSim && AppState.hasVideo) { currentMs = AppState.videoEl.currentTime * 1000; if (AppState.videoEl.ended) { stopPlayback(); return; } } else { currentMs = startMs + (Date.now() - startTimeReal); if (currentMs > AppState.subtitles[AppState.subtitles.length - 1].endMs + 2000) { stopPlayback(); return; } } updateSyncUI(currentMs); if (AppState.hasVideo && AppState.videoEl.duration) { const pct = (AppState.videoEl.currentTime / AppState.videoEl.duration) * 100; document.getElementById('playProgress').style.width = `${pct}%`; } AppState.animationFrameId = requestAnimationFrame(loop); }; AppState.animationFrameId = requestAnimationFrame(loop); }

function stopPlayback() {
    AppState.isPlaying = false;
    if (AppState.animationFrameId) cancelAnimationFrame(AppState.animationFrameId);
    AppState.animationFrameId = null;
    updatePlayIcon();
    document.querySelectorAll('.playing').forEach(e => e.classList.remove('playing'));

    if (AppState.hasVideo) {
        AppState.videoEl.pause();

        const currentMs = AppState.videoEl.currentTime * 1000;
        const idx = AppState.subtitles.findIndex(s => currentMs >= s.startMs && currentMs < s.endMs);

        if (idx !== -1) {
            previewItem(idx);
            selectedIndices.clear();
            selectedIndices.add(idx);
            updateRowClass(document.getElementById(`card-${idx}`), idx);
            updateSelectionUI();
        }
    }
}

function onVideoTimeUpdate() { if (!AppState.hasVideo) return; const currentMs = AppState.videoEl.currentTime * 1000; updateSyncUI(currentMs); const pct = (AppState.videoEl.currentTime / AppState.videoEl.duration) * 100; document.getElementById('playProgress').style.width = `${pct}%`; }
function updateSyncUI(currentMs) {
    document.getElementById('timeDisplay').innerText = formatTimeMs(currentMs);
    const activeSubIndex = AppState.subtitles.findIndex(s => currentMs >= s.startMs && currentMs <= s.endMs);
    const oldBlock = document.querySelector('.subtitle-block.active'); if (oldBlock) oldBlock.classList.remove('active');
    const block = document.getElementById(`timeline-block-${activeSubIndex}`); if (block) block.classList.add('active');
    document.querySelectorAll('.playing').forEach(el => el.classList.remove('playing'));
    if (activeSubIndex !== -1) {
        const sub = AppState.subtitles[activeSubIndex];
        renderMonitorText(sub.cn || sub.text, false);
        const row = document.getElementById(`card-${activeSubIndex}`); if (row) { row.classList.add('playing'); }
    } else { renderMonitorText("", false); }
    updateLocalTimelineUI(currentMs);
    Timeline.updatePlayhead();
}
function updatePlayIcon() { document.getElementById('playIcon').className = AppState.isPlaying ? "fas fa-pause text-xs" : "fas fa-play text-xs"; }
function seekVideo(e) { if (!AppState.hasVideo || !AppState.videoEl.duration) return; const rect = e.currentTarget.getBoundingClientRect(); const pct = (e.clientX - rect.left) / rect.width; AppState.videoEl.currentTime = pct * AppState.videoEl.duration; if (!AppState.isPlaying) onVideoTimeUpdate(); }
function parseTimeMs(t) { if (!t) return 0; const p = t.trim().split(/[:,.]/); if (p.length < 4) return 0; return (parseInt(p[0]) * 3600000) + (parseInt(p[1]) * 60000) + (parseInt(p[2]) * 1000) + parseInt(p[3]); }
function formatTimeMs(ms) { const d = new Date(ms); return `${String(Math.floor(ms / 3600000)).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')}:${String(d.getUTCSeconds()).padStart(2, '0')}.${String(d.getUTCMilliseconds()).padStart(3, '0')}`; }
function formatSrtTime(ms) { return formatTimeMs(ms).replace('.', ','); }
function parseSRT(t) { const r = []; const p = /(\d+)\s+([\d:,.]+)[\s-]+>[\s-]+([\d:,.]+)\s+([\s\S]*?)(?=\n\n|\n*$)/g; const norm = t.replace(/\r\n/g, '\n').replace(/-->/g, '-->'); let m; while (m = p.exec(norm + "\n\n")) { r.push({ id: m[1], startMs: parseTimeMs(m[2]), endMs: parseTimeMs(m[3]), text: [...new Set(m[4].trim().split('\n'))].join(' '), cn: '' }); } return r; }

function updateFinalOutput(isClip = false) {
    const list = isClip ? Array.from(selectedIndices).sort((a, b) => a - b).map(i => AppState.subtitles[i]) : AppState.subtitles;
    document.getElementById('finalSrtOutput').value = list.map((s, i) => `${isClip ? i + 1 : s.id}\n${formatSrtTime(s.startMs)} --> ${formatSrtTime(s.endMs)}\n${(s.cn || s.text).trim()}\n\n`).join('');
}

function exportData() {
    const c = document.getElementById('finalSrtOutput').value;
    if (!c) return;
    const blob = new Blob(['\uFEFF' + c], { type: 'text/srt;charset=utf-8' });
    const u = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = u;
    a.download = `${AppState.originalFileName}_translated.srt`;
    a.click();
}

function previewItem(i) {
    AppState.isCalibrating = false;
    AppState.activeIndex = i;
    document.getElementById('calibrationBadge').classList.add('hidden');

    const container = document.getElementById('cardContainer');
    const targetTop = i * AppState.virtual.rowHeight;
    const centerOffset = (container.clientHeight / 2) - (AppState.virtual.rowHeight / 2);
    container.scrollTo({ top: targetTop - centerOffset, behavior: 'smooth' });

    renderVisibleItems();
    const sub = AppState.subtitles[i];
    renderMonitorText(sub.cn || sub.text, false);

    if (AppState.hasVideo) {
        const currentSec = AppState.videoEl.currentTime;
        if (currentSec < sub.startMs / 1000 - 0.1 || currentSec > sub.endMs / 1000 + 0.1) {
            AppState.videoEl.currentTime = (sub.startMs + 50) / 1000;
        }
    }
}

function renderTimelineVisuals() { const c = document.getElementById('timelineVisuals'); c.innerHTML = ''; let totalMs = (AppState.hasVideo && AppState.videoEl.duration) ? AppState.videoEl.duration * 1000 : (AppState.subtitles.length > 0 ? AppState.subtitles[AppState.subtitles.length - 1].endMs + 5000 : 0); if (!totalMs) return; const frag = document.createDocumentFragment(); AppState.subtitles.forEach((s, i) => { const b = document.createElement('div'); b.className = 'subtitle-block'; b.id = `timeline-block-${i}`; b.style.left = `${(s.startMs / totalMs) * 100}%`; b.style.width = `${((s.endMs - s.startMs) / totalMs) * 100}%`; frag.appendChild(b); }); c.appendChild(frag); }
function setSoftware(sw) { AppState.software = sw; const preset = SOFTWARE_CONFIG[sw]; document.querySelectorAll('.sw-tab').forEach(el => el.classList.remove('active')); document.getElementById(`btn-${sw}`).classList.add('active'); const s = document.getElementById('inputSize'); const l = document.getElementById('inputLimit'); s.min = preset.min; s.max = preset.max; s.value = preset.val; l.value = preset.limit; handleSliderInput(); }
function handleSliderInput() { AppState.fontSize = parseInt(document.getElementById('inputSize').value); AppState.maxChars = parseInt(document.getElementById('inputLimit').value); document.getElementById('displaySize').innerText = AppState.fontSize; document.getElementById('displayLimit').innerText = `${AppState.maxChars} 字`; checkAllOverflows(); forceCalibration(); }

function checkAllOverflows() {
    AppState.overflowIndices = [];
    AppState.missingIndices = [];
    const limit = AppState.maxChars;

    AppState.subtitles.forEach((s, i) => {
        const text = s.cn || s.text || "";
        if (text.length > limit) AppState.overflowIndices.push(i);
        if (s.text && s.text.trim().length > 0 && (!s.cn || s.cn.trim().length === 0)) {
            AppState.missingIndices.push(i);
        }
    });

    renderVisibleItems();

    const overflowNav = document.getElementById('overflowNav');
    const missingNav = document.getElementById('missingNav');

    if (AppState.overflowIndices.length > 0) {
        overflowNav.classList.remove('hidden');
        document.getElementById('overflowCount').innerText = `${AppState.overflowIndices.length} 处溢出`;
    } else { overflowNav.classList.add('hidden'); }

    if (AppState.missingIndices.length > 0) {
        missingNav.classList.remove('hidden');
        document.getElementById('missingCount').innerText = `${AppState.missingIndices.length} 处漏译`;
    } else { missingNav.classList.add('hidden'); }
}

function jumpToNextOverflow() { let t = AppState.overflowIndices.find(idx => idx > AppState.activeIndex); if (t === undefined) t = AppState.overflowIndices[0]; if (t !== undefined) previewItem(t); }
function jumpToNextMissing() { let t = AppState.missingIndices.find(idx => idx > AppState.activeIndex); if (t === undefined) t = AppState.missingIndices[0]; if (t !== undefined) previewItem(t); }

function toggleSelection(index, event) {
    if (event) event.stopPropagation();
    if (event && event.shiftKey && lastClickedIndex !== -1) {
        const s = Math.min(lastClickedIndex, index); const e = Math.max(lastClickedIndex, index);
        selectedIndices.clear(); for (let i = s; i <= e; i++) selectedIndices.add(i);
    } else if (event && (event.ctrlKey || event.metaKey)) {
        if (selectedIndices.has(index)) selectedIndices.delete(index); else selectedIndices.add(index); lastClickedIndex = index;
    } else {
        selectedIndices.clear(); selectedIndices.add(index); lastClickedIndex = index; previewItem(index);
    }
    updateSelectionUI(); renderVisibleItems(); Timeline.render();
}
function resetSelection() { selectedIndices.clear(); if (AppState.activeIndex !== -1) { selectedIndices.add(AppState.activeIndex); previewItem(AppState.activeIndex); } updateSelectionUI(); renderVisibleItems(); Timeline.render(); }
function updateSelectionUI() { const isClip = selectedIndices.size > 1; document.getElementById('modeTitle').innerText = isClip ? "✂️ 拆条" : "全片"; document.getElementById('selectionCount').innerText = `选 ${selectedIndices.size}`; document.getElementById('selectionCount').classList.toggle('hidden', !isClip); document.getElementById('btnExitClip').classList.toggle('hidden', !isClip); document.getElementById('btnExport').innerHTML = isClip ? '<i class="fas fa-cut mr-1"></i> 导出片段' : '<i class="fas fa-file-download mr-1"></i> 导出全文'; updateFinalOutput(isClip); }
function toggleSettings() { document.getElementById('settingsPanel').classList.toggle('hidden'); }
function saveSettings() {
    const key = document.getElementById('apiKeyInput').value.trim();
    const url = document.getElementById('apiBaseInput').value.trim();
    const model = document.getElementById('apiModelInput').value.trim();
    const asrKey = document.getElementById('asrKeyInput').value.trim();
    const asrUrl = document.getElementById('asrBaseInput').value.trim();
    const asrModel = document.getElementById('asrModelInput').value.trim();
    const advBatch = parseInt(document.getElementById('advBatchSize').value);
    const advDelay = parseInt(document.getElementById('advReqDelay').value);
    localStorage.setItem('s1_key', key); localStorage.setItem('s1_url', url); localStorage.setItem('s1_model', model); localStorage.setItem('s1_asr_key', asrKey); localStorage.setItem('s1_asr_url', asrUrl); localStorage.setItem('s1_asr_model', asrModel); localStorage.setItem('s1_adv_batch', advBatch); localStorage.setItem('s1_adv_delay', advDelay);
    AppState.config = { key, url, model }; AppState.asrConfig = { key: asrKey, url: asrUrl, model: asrModel }; AppState.advanced = { batchSize: advBatch, reqDelay: advDelay };
    updateApiStatus(); toggleSettings(); showToast("配置已保存");
}
function updateApiStatus() { const dot = document.getElementById('statusDot'); const text = document.getElementById('statusText'); if (AppState.config.key) { dot.className = "w-1.5 h-1.5 rounded-full bg-emerald-500 shadow-[0_0_8px_rgba(16,185,129,0.5)]"; text.innerText = "API 就绪"; text.classList.replace('text-gray-400', 'text-emerald-500'); } else { dot.className = "w-1.5 h-1.5 rounded-full bg-red-500"; text.innerText = "未连接"; } }
function toggleLoading(l) { document.getElementById('btnStart').classList.toggle('hidden', l); document.getElementById('btnStop').classList.toggle('hidden', !l); }
function stopTranslation() { AppState.isProcessing = false; toggleLoading(false); }
function copyTitle() { navigator.clipboard.writeText(document.getElementById('titleBox').innerText); alert("已复制"); }
function clearSelection(e) { if (e.target.id === 'cardContainer' && selectedIndices.size > 1) { selectedIndices.clear(); updateSelectionUI(); renderVisibleItems(); } }
async function loadLocalFonts() { if (!('queryLocalFonts' in window)) return alert("需 Chrome 电脑版"); try { const f = await window.queryLocalFonts(); const s = document.getElementById('fontSelect'); s.innerHTML = s.querySelector('optgroup').outerHTML; const g = document.createElement('optgroup'); g.label = "本地";[...new Set(f.map(x => x.family))].sort().forEach(n => { const o = document.createElement('option'); o.text = n; o.value = `"${n}", sans-serif`; g.appendChild(o); }); s.appendChild(g); alert("字体已加载"); } catch (e) { } }

function loadFile(file) {
    const r = new FileReader();
    r.onload = (e) => {
        const raw = e.target.result;
        document.getElementById('rawTextArea').value = raw;
        AppState.subtitles = parseSRT(raw);
        document.getElementById('fileStatus').innerText = `${file.name}`;
        AppState.originalFileName = file.name.substring(0, file.name.lastIndexOf('.')) || file.name;
        recordState();
        renderList();
        checkAllOverflows();
        updateFinalOutput();
        forceCalibration();
    };
    r.readAsText(file);
}
function forceCalibration() { if (AppState.isPlaying) return; AppState.isCalibrating = true; document.getElementById('calibrationBadge').classList.remove('hidden'); let text = "牛逼".repeat(Math.ceil(AppState.maxChars / 2)).substring(0, AppState.maxChars); AppState.font = document.getElementById('fontSelect').value; renderMonitorText(text, true); }

// [V45.0] 双语渲染支持
function renderMonitorText(text, isCalibration) {
    let sub = null;
    if (!isCalibration && AppState.activeIndex !== -1) {
        sub = AppState.subtitles[AppState.activeIndex];
    }

    const cleanCn = text ? text.trim() : "";
    const cleanRaw = (sub && sub.text) ? sub.text.trim() : "";

    const m = document.getElementById('previewMonitor');
    const l = document.getElementById('subtitleLayer');
    const p = SOFTWARE_CONFIG[AppState.software];
    const scale = m.offsetWidth / 1920;
    const size = AppState.fontSize * p.scale * scale;

    l.className = p.class;
    l.style.fontFamily = AppState.font;
    l.style.fontSize = `${size}px`;

    if (AppState.isBilingual && !isCalibration && cleanRaw) {
        l.style.display = "flex";
        l.style.flexDirection = "column";
        l.style.alignItems = "center";
        l.style.gap = `${4 * scale}px`;

        const cnHtml = `<span style="display:block;">${cleanCn}</span>`;
        const enHtml = `<span style="display:block; font-size: 0.6em; opacity: 0.9; color: #fbbf24; text-shadow: 1px 1px 2px black;">${cleanRaw}</span>`;

        l.innerHTML = cnHtml + enHtml;
    } else {
        l.style.display = "block";
        if (AppState.isCtrlPressed && !isCalibration) {
            let html = "";
            let count = 1;
            const parts = cleanCn.split(/([ \.,!\?，。！？])/);
            for (let i = 0; i < parts.length; i++) {
                const part = parts[i];
                if (/[ \.,!\?，。！？]/.test(part)) {
                    html += part + `<sup class="split-marker">[${count}]</sup>`;
                    count++;
                } else {
                    html += part;
                }
            }
            l.innerHTML = html;
        } else {
            l.innerText = cleanCn;
        }
    }

    if (isCalibration) l.classList.add('mode-calibration'); else { l.classList.remove('mode-calibration'); document.getElementById('calibrationBadge').classList.add('hidden'); }
    checkOverflow(l);
}

function checkOverflow(l) { const s = window.getComputedStyle(l); const isOverflow = l.clientHeight > (parseFloat(s.lineHeight) * (AppState.isBilingual ? 2.5 : 1.2)); const w = document.getElementById('overflowWarning'); const m = document.getElementById('previewMonitor'); if (isOverflow) { w.classList.remove('hidden'); m.style.borderColor = '#ef4444'; l.classList.add('mode-overflow'); return true; } else { w.classList.add('hidden'); m.style.borderColor = '#27272a'; l.classList.remove('mode-overflow'); return false; } }
window.onresize = () => { if (AppState.isCalibrating) forceCalibration(); onScrollList(); };

function cleanArtifacts(t) {
    if (!t) return "";
    let x = t.replace(/^\s*(?:\[\d+\]|\d+[.、:]|【\d+】)\s*/, '');
    x = x.replace(/——/g, ' ').replace(/—/g, ' ').replace(/--/g, ' ');

    if (document.getElementById('checkClean').checked) {
        x = x.replace(/，/g, ' ').replace(/,/g, ' ').replace(/、/g, ' ').replace(/[。.]$/g, '').replace(/[。.]/g, ' ').replace(/\s+/g, ' ').trim();
    }
    return x;
}

function updateTranslationVisuals() {
    document.querySelectorAll('.translating').forEach(el => el.classList.remove('translating'));
    if (AppState.processingRange) {
        for (let i = AppState.processingRange.start; i <= AppState.processingRange.end; i++) {
            const row = document.getElementById(`card-${i}`); if (row) row.classList.add('translating');
        }
    }
}

async function startTranslation() {
    if (!AppState.config.key) return alert("请配置 API Key");
    if (AppState.subtitles.length === 0) return alert("请先导入 SRT 文件！");

    const ctx = document.getElementById('inputContext');
    if (!ctx.value.trim()) { ctx.classList.add('border-red-500'); return alert("请填写语境！"); }
    ctx.classList.remove('border-red-500');

    recordState();
    AppState.isProcessing = true;
    toggleLoading(true);

    const BATCH = AppState.advanced.batchSize;
    const DELAY = AppState.advanced.reqDelay;
    const inputCtx = ctx.value;
    const glossary = document.getElementById('glossary').value;

    try {
        for (let i = 0; i < AppState.subtitles.length; i += BATCH) {
            if (!AppState.isProcessing) break;

            document.getElementById('progressText').innerText = `AI 深度翻译中... (${i}/${AppState.subtitles.length})`;

            const chunk = AppState.subtitles.slice(i, i + BATCH);
            AppState.processingRange = { start: i, end: Math.min(i + BATCH - 1, AppState.subtitles.length - 1) };
            updateTranslationVisuals();

            const prevContext = i > 0 ? AppState.subtitles.slice(Math.max(0, i - 10), i).map(s => s.cn || s.text).join(' | ') : "（无上文）";
            const nextContext = AppState.subtitles.slice(i + BATCH, i + BATCH + 10).map(s => s.text).join(' | ');

            const prompt = `
# Role
你是一个只输出压缩 JSON 的翻译程序。

# Task
将 [Current Batch] 翻译为中文。

# Context
Video Context: ${inputCtx} 
Glossary: ${glossary}       

# Rules
1. **Minified JSON Only**: 输出必须是**单行**、**无空格**、**无换行**的压缩 JSON 字符串。
2. **No Extra Text**: 严禁 Markdown，严禁解释。
3. **Format**: [{"id":1,"cn":"译文"}]
4. **Logic**: 结合上下文进行翻译，同时意译俚语，不要使用破折号。

# Example
In: [{"id":1,"text":"Hi"}]
Out: [{"id":1,"cn":"嗨"}]
`;

            // 第二个参数 null, 第三个参数 0 或不写，即为压缩模式
            const userPayload = JSON.stringify({
                context_before: prevContext,
                current_batch: chunk.map((s, idx) => ({ id: i + idx, text: s.text.replace(/"/g, "'") })),
                context_after: nextContext
            });

            let success = false; let retry = 0;
            while (!success && retry < 5) {
                try {
                    const res = await callAI(prompt, userPayload, 0.2);
                    let cleanRes = res.trim();
                    if (cleanRes.startsWith('```json')) cleanRes = cleanRes.replace(/^```json/, '').replace(/```$/, '');

                    const parsed = JSON.parse(cleanRes);
                    if (!Array.isArray(parsed)) throw new Error("Format error");

                    const map = {};
                    parsed.forEach(p => { if (p.cn) map[p.id] = cleanArtifacts(p.cn); });

                    chunk.forEach((s, idx) => { if (map[i + idx]) s.cn = map[i + idx]; });
                    success = true;
                    if (DELAY > 0) await new Promise(r => setTimeout(r, DELAY));
                } catch (e) {
                    retry++; const waitTime = 3000 * retry;
                    console.log(`Retry ${retry}...`, e);
                    document.getElementById('progressText').innerText = `⚠️ 网络波动，重试中 (${retry})...`;
                    await new Promise(r => setTimeout(r, waitTime));
                }
            }
            if (!success) { chunk.forEach(item => { item.cn = "【AI请求失败】"; }); }

            chunk.forEach((s, idx) => {
                const el = document.getElementById(`sub-text-${i + idx}`);
                if (el) { el.innerText = s.cn; el.classList.replace('text-gray-500', 'text-indigo-300'); }
            });

            AppState.processingRange = null;
            updateTranslationVisuals();
            checkAllOverflows();
            debouncedUpdateFinal();
            saveProject(false);
        }
        recordState();
    } catch (e) { alert("中断: " + e.message); } finally {
        AppState.isProcessing = false; AppState.processingRange = null;
        updateTranslationVisuals(); toggleLoading(false);
        document.getElementById('progressText').innerText = "字幕序列 (按Ctrl/Shift多选)";
        showToast("✅ AI 翻译全部完成");
    }
}

async function coreRepairSubtitle(index) {
    const sub = AppState.subtitles[index];
    const startIdx = Math.max(0, index - 2);
    const endIdx = Math.min(AppState.subtitles.length - 1, index + 2);
    const contextChunk = AppState.subtitles.slice(startIdx, endIdx + 1);

    const prompt = `你是一个字幕修复师。请根据上下文，翻译中间的那一句（ID=${sub.id}）。\n如果原文有断句错误，请根据语境逻辑输出完整的中文句子。\n不要输出任何解释，只输出译文。`;
    const payload = JSON.stringify({
        context_before: contextChunk.filter(s => s.id < sub.id).map(s => s.text),
        target_id: sub.id,
        target_text: sub.text,
        context_after: contextChunk.filter(s => s.id > sub.id).map(s => s.text)
    }, null, 2);

    const res = await callAI(prompt, payload, 0.1);
    const cleanCN = cleanArtifacts(res.trim().replace(/^["']|["']$/g, ''));

    if (cleanCN) {
        sub.cn = cleanCN;
        return true;
    }
    return false;
}

async function retranslateSingle(index) {
    if (!AppState.config.key) return alert("请配置 API Key");
    const btn = document.querySelector(`#card-${index} .fa-sync-alt`) || document.querySelector(`#card-${index} .fa-magic`);
    if (btn) btn.classList.add('fa-spin');
    try {
        const success = await coreRepairSubtitle(index);
        if (success) {
            recordState();
            renderList(); checkAllOverflows(); updateFinalOutput();
            showToast("✨ AI 精修完成");
        } else {
            showToast("⚠️ AI 返回空值");
        }
    } catch (e) {
        alert("修复失败: " + e.message);
    } finally {
        if (btn) btn.classList.remove('fa-spin');
    }
}

async function batchFixMissing() {
    if (AppState.missingIndices.length === 0) return showToast("当前无漏译");
    if (!AppState.config.key) return alert("请配置 API Key");

    const total = AppState.missingIndices.length;
    if (!confirm(`确认自动修补 ${total} 处漏译吗？\n这将逐个请求 API，可能需要一定时间。`)) return;

    toggleLoading(true);
    const delay = AppState.advanced.reqDelay || 1000;
    let successCount = 0;

    const targets = [...AppState.missingIndices];

    try {
        for (let i = 0; i < targets.length; i++) {
            const idx = targets[i];
            document.getElementById('progressText').innerText = `🔨 正在修补漏译 (${i + 1}/${total})...`;
            previewItem(idx);
            try {
                const result = await coreRepairSubtitle(idx);
                if (result) successCount++;
            } catch (err) { console.error(`Index ${idx} repair failed`, err); }
            if (i < targets.length - 1) await new Promise(r => setTimeout(r, delay));
        }
        recordState();
        showToast(`✅ 批量修补完成 (成功 ${successCount}/${total})`);
    } catch (e) {
        alert("批量修补中断: " + e.message);
    } finally {
        toggleLoading(false);
        checkAllOverflows();
        updateFinalOutput();
        document.getElementById('progressText').innerText = "字幕序列 (Ctrl+↑/↓导航)";
    }
}

async function startAiClipping() {
    if (!AppState.subtitles[0]?.cn) return alert("请先翻译");
    const btn = document.getElementById('btnAiClip'); btn.disabled = true; btn.innerText = "分析中...";
    try {
        const sample = [...AppState.subtitles.slice(0, 20), ...AppState.subtitles.slice(-20)].map((s, i) => s.cn).join('\n');
        const res = await callAI(`你是一个新闻导播。阅读字幕采样，挑出 3-5 个最具新闻价值的片段。返回严格JSON: [{"title":"标题","start":0,"end":10}]`, sample, 0.3);
        const clips = JSON.parse(res.replace(/```json|```/g, '').trim());
        const box = document.getElementById('clipSuggestions'); box.innerHTML = ''; box.classList.remove('hidden');
        clips.forEach(c => { const d = document.createElement('div'); d.className = "p-2 rounded text-xs text-gray-300 cursor-pointer mb-1 flex justify-between hover:bg-white/5"; d.innerHTML = `<span>${c.title}</span><span class="text-gray-500 text-[10px]">${c.start}-${c.end}</span>`; d.onclick = () => { selectedIndices.clear(); for (let i = c.start; i <= c.end; i++) selectedIndices.add(i); previewItem(c.start); updateSelectionUI(); document.getElementById('titleBox').innerText = c.title; }; box.appendChild(d); });
    } catch (e) { alert("分析失败"); } finally { btn.disabled = false; btn.innerText = 'AI 智能拆条'; }
}
async function regenerateTitle() { const t = await callAI(`体育主编，两段式爆款标题，30字内。`, selectedIndices.size > 1 ? document.getElementById('finalSrtOutput').value : AppState.subtitles.slice(0, 60).map(s => s.cn).join('\n'), 0.7); document.getElementById('titleBox').innerText = t.replace(/["《》]/g, ''); }
async function callAI(sys, user, temp = 0.3) {
    const cfg = AppState.config;

    // 构造 payload
    const payload = {
        model: cfg.model,
        messages: [
            { role: "system", content: sys },
            { role: "user", content: user }
        ],
        temperature: temp
    };

    // 【尝试添加这一行】
    // 大部分支持 Gemini 的中转站（如 OneAPI/NewAPI）都兼容这个参数
    // 如果你发现请求报错 400，请注释掉下面这行即可
    payload.response_format = { type: "json_object" };

    const res = await fetch(`${cfg.url.replace(/\/$/, '')}/chat/completions`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'Authorization': `Bearer ${cfg.key}`
        },
        body: JSON.stringify(payload)
    });

    if (!res.ok) throw new Error(`API ${res.status}`);
    const d = await res.json();
    return d.choices[0].message.content;
}