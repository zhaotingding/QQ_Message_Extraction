import {el} from "./controls.js";

const readStates = {unread: "未读", skimmed: "粗读", deep: "精读"};
const reportKinds = {daily: "日报", weekly: "周报", monthly: "月报"};
const speechRates = [[-3, "较慢"], [0, "正常"], [3, "较快"], [6, "最快"]];

export function mount(container, app) {
    let report, items = [], busy = false, closed = false;
    let voiceTimer, voiceEpoch = 0, voiceState = "idle", highlighted = -1;
    const reportList = el("div", {className: "reader-list"});
    const outline = el("nav", {className: "reader-outline", ariaLabel: "主题目录"});
    const article = el("article", {className: "reader-article", ariaLabel: "摘要正文"});
    const meta = el("p", {className: "reader-meta"});
    const location = el("p", {className: "reader-location"});
    const historyStatus = el("p", {className: "reader-meta", textContent: "正在读取…"});
    const voiceStatus = el("span", {className: "reader-meta"});
    let appliedKinds = Object.keys(reportKinds);
    const filters = appliedKinds.map(kind => el("input", {type: "checkbox", value: kind, checked: true}));
    const filterGroup = el("div", {className: "reader-filters", role: "group", ariaLabel: "摘要类型"},
        ...filters.map(input => el("label", {}, input, reportKinds[input.value])));
    const showFilters = () => {
        for (const input of filters) input.checked = appliedKinds.includes(input.value);
    };
    const readState = el("select", {ariaLabel: "阅读状态"},
        ...Object.entries(readStates).map(([value, label]) => el("option", {value, textContent: label})));
    const rate = el("select", {ariaLabel: "朗读语速"}, ...speechRates.map(([value, label]) =>
        el("option", {value: String(value), textContent: label})));
    const notes = el("textarea", {rows: 3, ariaLabel: "我的心得", placeholder: "记录我的心得"});
    const btn = (label, fn) => el("button", {
        textContent: label, type: "button",
        onclick: () => Promise.resolve().then(fn).catch(error => app.notify(error.message)),
    });
    const options = () => ({id: report.id, hash: report.review.content_hash});
    const review = async (changes) => {
        const value = await app.call("reader.review", {...options(), ...changes});
        report.review = value;
        const entry = items.find(item => item.id === report.id);
        if (entry) entry.read_state = value.read_state;
        renderList();
        return value;
    };

    // 切换、刷新和关闭前先保存心得。写库失败时保留原文章与草稿。
    const saveNotes = async () => {
        if (!report || notes.value === report.review.notes) return;
        await review({notes: notes.value});
        app.notify("心得已保存");
    };
    const guarded = async (task) => {
        if (closed || busy) return;
        busy = true;
        updateButtons();
        try {
            await task();
        } finally {
            busy = false;
            if (!closed) updateButtons();
        }
    };
    const refresh = btn("刷新", () => guarded(() => loadHistory()));
    const save = btn("保存心得", () => guarded(saveNotes));
    const open = btn("打开完整 HTML", () => report && app.call("reader.openHtml", {id: report.id}));
    const copy = btn("复制全文", async () => {
        if (!report) return;
        await app.call("desktop.copy", {text: report.content});
        app.notify("已复制全文");
    });
    const speak = btn("朗读全文", () => play(0));
    const pause = btn("暂停", () => guarded(async () => {
        await app.call("reader.narration", {op: voiceState === "paused" ? "resume" : "pause"});
    }));
    const stop = btn("停止", () => guarded(stopVoice));
    const markButtons = [["underline", "下划线"], ["blue", "蓝色重点"], ["red", "红色重点"], ["clear", "清除标注"]]
        .map(([level, label]) => btn(label, () => mark(level)));
    const reading = el("div", {className: "reader-reading"},
        el("div", {className: "panel reader-head"},
            meta, location,
            el("div", {className: "toolbar"}, open, copy, el("label", {}, "阅读状态 ", readState), ...markButtons),
            el("div", {className: "toolbar"}, speak, pause, stop, el("label", {}, "语速 ", rate), voiceStatus)),
        article,
        el("div", {className: "panel reader-notes"}, el("label", {textContent: "我的心得"}), notes, save));
    container.append(el("div", {className: "reader-layout"},
        el("aside", {className: "panel reader-side"},
            el("div", {className: "toolbar"}, el("strong", {textContent: "最近摘要"}), refresh),
            filterGroup, historyStatus, reportList, el("strong", {textContent: "主题目录"}), outline),
        reading));

    function updateButtons() {
        refresh.disabled = busy;
        for (const input of filters) input.disabled = busy;
        notes.disabled = save.disabled = readState.disabled = copy.disabled = !report || busy;
        for (const button of markButtons) button.disabled = !report || busy;
        open.disabled = !report?.report_path || busy;
        speak.disabled = !report?.segments.length || busy;
        rate.disabled = !report || busy;
        stop.disabled = busy || !["starting", "playing", "paused"].includes(voiceState);
        pause.disabled = busy || !["playing", "paused"].includes(voiceState);
        pause.textContent = voiceState === "paused" ? "继续" : "暂停";
    }

    function renderList() {
        reportList.replaceChildren(...items.map(item => {
            const button = btn("", () => guarded(() => selectReport(item.id)));
            button.className = "reader-entry";
            button.dataset.readState = item.read_state;
            button.setAttribute("aria-current", item.id === report?.id ? "true" : "false");
            button.append(
                el("span", {className: "reader-entry-meta", textContent: `${item.kind_label} · ${item.date_label} · ${readStates[item.read_state]}`}),
                el("strong", {textContent: item.title || "无标题摘要"}),
                el("span", {className: "reader-meta", textContent: `${item.chars} 字`}));
            return button;
        }));
    }

    function renderArticle() {
        let offset = 0;
        const content = report.blocks.map(block => block.parts.map(([text]) => text).join("") + "\n").join("");
        const characters = Array.from(content);
        // DOM 选区是 UTF-16，后端与 Tk 批注使用 Unicode 字符数。
        const marks = report.review.annotations.filter(mark => characters.slice(mark.start, mark.end).join("") === mark.text);
        const segments = report.segments;
        const lineElements = [], links = [];
        for (const [index, block] of report.blocks.entries()) {
            const line = el(block.kind === "title" ? "h2" : block.kind === "heading" ? "h3" : "div",
                {className: `reader-line reader-${block.kind}`});
            if (block.kind === "heading") {
                line.id = `report-heading-${index}`;
                links.push(btn(block.parts[0][0], () => line.scrollIntoView({block: "start"})));
            }
            for (const [text, bold] of [...block.parts, ["\n", false]]) {
                const end = offset + Array.from(text).length;
                const bounds = new Set([offset, end]);
                for (const part of [...marks, ...segments]) {
                    if (offset < part.start && part.start < end) bounds.add(part.start);
                    if (offset < part.end && part.end < end) bounds.add(part.end);
                }
                const edges = [...bounds].sort((a, b) => a - b);
                for (let j = 0; j < edges.length - 1; j++) {
                    const left = edges[j], right = edges[j + 1];
                    const mark = marks.find(value => value.start <= left && right <= value.end);
                    const segment = segments.findIndex(value => value.start <= left && right <= value.end);
                    const span = el(bold ? "strong" : "span", {
                        className: mark ? `reader-mark-${mark.level}` : "",
                        textContent: characters.slice(left, right).join(""),
                    });
                    if (segment >= 0) span.dataset.speech = String(segment);
                    line.append(span);
                }
                offset = end;
            }
            lineElements.push(line);
        }
        const scroll = article.scrollTop;
        article.replaceChildren(...lineElements);
        article.scrollTop = scroll;
        outline.replaceChildren(...(links.length ? links : [el("span", {className: "reader-meta", textContent: "暂无主题目录"})]));
        drawSpeaking(highlighted, false);
    }

    async function selectReport(id, reload = false) {
        if (id === report?.id && !reload) return;
        await saveNotes();
        await stopVoice();
        const result = await app.call("reader.report", {id});
        if (closed) return;
        report = result;
        meta.textContent = `${result.kind_label} · ${result.window} · ${result.chars} 字`;
        location.textContent = result.report_path || "HTML 文件不可用，正在显示数据库原文";
        notes.value = result.review.notes;
        readState.value = result.review.read_state;
        rate.value = String(result.rate);
        renderArticle();
        article.scrollTop = 0;
        renderList();
    }

    async function loadHistory(kinds = appliedKinds, saveFilter = false) {
        await saveNotes();
        const result = await app.call("reader.history", {kinds});
        if (closed) return;
        if (saveFilter) appliedKinds = await app.call("reader.filters", {kinds});
        items = result;
        historyStatus.textContent = items.length ? `最近 ${items.length} 份 · 按内容日期倒序` :
            kinds.length ? "暂无此类摘要" : "勾选要显示的摘要类型";
        const current = items.find(item => item.id === report?.id);
        if (current && current.content_hash !== report.review.content_hash) {
            await selectReport(current.id, true);
        } else if (!current) {
            if (items.length) {
                const saved = app.state.readerView;
                const selected = items.find(item => item.id === saved?.id) || items[0];
                await selectReport(selected.id);
                if (saved?.id === selected.id && saved.hash === selected.content_hash) {
                    article.scrollTop = saved.scroll;
                }
            }
            else {
                await stopVoice();
                report = null;
                article.replaceChildren();
                outline.replaceChildren();
                notes.value = location.textContent = "";
                meta.textContent = "暂无摘要";
            }
        }
        renderList();
    }

    function selectionOffsets() {
        const selection = window.getSelection();
        if (!selection?.rangeCount || selection.isCollapsed) return null;
        const range = selection.getRangeAt(0);
        if (!article.contains(range.startContainer) || !article.contains(range.endContainer)) return null;
        const prefix = document.createRange();
        prefix.selectNodeContents(article);
        prefix.setEnd(range.startContainer, range.startOffset);
        const start = Array.from(prefix.toString()).length;
        return {start, end: start + Array.from(range.toString()).length};
    }

    // 在按钮接收焦点前保留正文选区；键盘用户可用当前选区触发标注。
    let markSelection;
    for (const button of markButtons) button.addEventListener("pointerdown", () => {
        markSelection = selectionOffsets();
    });
    async function mark(level) {
        if (!report || busy) return;
        const offsets = selectionOffsets() || markSelection;
        markSelection = null;
        if (!offsets) {
            app.notify("请先选中正文文字");
            return;
        }
        await guarded(async () => {
            report.review = await app.call("reader.mark", {...options(), ...offsets, level});
            renderArticle();
            app.notify("批注已保存");
        });
    }

    function drawSpeaking(index, scroll = true) {
        article.querySelectorAll(".reader-speaking").forEach(node => node.classList.remove("reader-speaking"));
        if (index < 0) return;
        const parts = article.querySelectorAll(`[data-speech="${index}"]`);
        for (const part of parts) part.classList.add("reader-speaking");
        if (scroll) parts[0]?.scrollIntoView({block: "nearest"});
    }

    function showVoice(state) {
        voiceState = state.state;
        voiceStatus.textContent = state.note || (voiceState === "starting" ? "正在启动…" :
            ["playing", "paused"].includes(voiceState) ?
                `${voiceState === "paused" ? "已暂停 · " : ""}${state.index + 1}/${state.total} 段` : "");
        const index = ["playing", "paused"].includes(voiceState) ? state.index : -1;
        if (index !== highlighted) {
            highlighted = index;
            drawSpeaking(index);
        }
        updateButtons();
    }

    async function pollVoice(epoch) {
        if (closed || epoch !== voiceEpoch) return;
        try {
            const state = await app.call("reader.narration", {op: "status"});
            if (closed || epoch !== voiceEpoch) return;
            showVoice(state);
            if (["starting", "playing", "paused"].includes(voiceState)) {
                voiceTimer = setTimeout(() => pollVoice(epoch), 180);
            }
        } catch (error) {
            if (!closed && epoch === voiceEpoch) app.notify(error.message);
        }
    }

    async function play(index) {
        if (!report || busy) return;
        await guarded(async () => {
            clearTimeout(voiceTimer);
            const epoch = ++voiceEpoch;
            const state = await app.call("reader.narration", {op: "play", ...options(), index});
            if (closed || epoch !== voiceEpoch) return;
            showVoice(state);
            void pollVoice(epoch);
        });
    }

    async function stopVoice() {
        clearTimeout(voiceTimer);
        voiceEpoch++;
        if (["starting", "playing", "paused"].includes(voiceState)) {
            await app.call("reader.narration", {op: "stop"});
        }
        showVoice({state: "idle"});
    }

    article.addEventListener("dblclick", event => {
        const part = event.target.closest("[data-speech]");
        if (part) play(Number(part.dataset.speech)).catch(error => app.notify(error.message));
    });
    filterGroup.addEventListener("change", () => guarded(async () => {
        const kinds = filters.filter(input => input.checked).map(input => input.value);
        try {
            await loadHistory(kinds, true);
        } finally {
            showFilters();
        }
    }).catch(error => app.notify(error.message)));
    readState.addEventListener("change", () => guarded(async () => {
        try {
            await review({read_state: readState.value});
            app.notify("阅读状态已保存");
        } catch (error) {
            readState.value = report.review.read_state;
            throw error;
        }
    }).catch(error => app.notify(error.message)));
    rate.addEventListener("change", () => guarded(async () => {
        try {
            await app.call("reader.narration", {op: "rate", rate: Number(rate.value)});
            report.rate = Number(rate.value);
        } catch (error) {
            rate.value = String(report.rate);
            throw error;
        }
    }).catch(error => app.notify(error.message)));
    updateButtons();
    guarded(async () => {
        appliedKinds = await app.call("reader.filters");
        showFilters();
        await loadHistory();
    }).catch(error => {
        historyStatus.textContent = error.message;
        app.notify(error.message);
    });

    return async () => {
        if (busy) throw new Error("摘要正在读取或保存，请稍后再切换");
        await guarded(async () => {
            await saveNotes();
            await stopVoice();
            app.state.readerView = {id: report?.id, hash: report?.review.content_hash, scroll: article.scrollTop};
            closed = true;
        });
    };
}
