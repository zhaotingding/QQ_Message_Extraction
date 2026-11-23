import {button, el} from "./controls.js";

export function topicReader(container, app) {
    let visible = false, generation = 0, timer, pending = false, reports = [];
    const name = el("input", {type: "text", maxLength: 64, ariaLabel: "主题关键词", placeholder: "选择候选，或输入技术主题"});
    const suggestions = el("div", {className: "topic-options"});
    const saved = el("nav", {className: "topic-list", ariaLabel: "已保存的主题报"});
    const detail = el("article", {className: "topic-article"});
    const note = el("p", {className: "reader-meta", role: "status"});
    const generate = button("生成主题报", update);
    const refresh = button("刷新", () => load(true));
    const sidebar = el("aside", {className: "panel topic-side"},
        el("h3", {}, "近期热点"), el("small", {}, "近 14 天日报，优先跨日反复讨论的术语"), suggestions,
        el("label", {}, "自选主题", name), generate, note, el("h3", {}, "我的主题报"), saved);
    container.append(el("div", {className:"topic-layout"}, sidebar, detail));

    const selected = () => reports.find(r => r.topic_key === name.value.trim().normalize("NFKC").toLowerCase()
        || r.name === name.value.trim());
    function choose(value) {
        name.value = value;
        app.state.topicName = value;
        render();
    }
    function render() {
        const item = selected();
        generate.disabled = !name.value.trim() || pending || item?.state === "running";
        generate.textContent = item?.state === "running" ? "正在更新…" : item?.revision ? "增量更新" : "生成主题报";
        saved.replaceChildren(...reports.map(r => button(r.name, () => choose(r.name),
            {className: "topic-entry", ariaPressed: String(item?.topic_key === r.topic_key)})));
        for (const option of suggestions.querySelectorAll("button")) {
            option.setAttribute("aria-pressed", String(option.dataset.name === name.value));
        }
        detail.replaceChildren(el("div", {className:"topic-head"},
            el("h2", {}, item?.name || name.value.trim() || "主题报"), refresh));
        if (!item?.sections.length) {
            detail.append(el("p", {className:"reader-meta"}, item?.note || "选择主题后，汇集相关日报生成第一版。"));
            return;
        }
        const updated = new Date(item.updated_at*1000).toLocaleString("zh-CN", {timeZone: "Asia/Shanghai"});
        detail.append(el("p", {className:"reader-meta"}, "第 " + item.revision + " 版 · " + updated + " · 绿色为本次更新"));
        for (const s of item.sections) {
            detail.append(el("section", {className: "topic-section"},
                el("h3", {className: s.title_new ? "topic-new" : ""}, s.title),
                el("p", {}, s.parts.map(([text, added]) => added ? el("span", {className:"topic-new"}, text) : text))));
        }
    }
    async function load(includeCandidates = false) {
        const id = ++generation;
        clearTimeout(timer);
        try {
            const data = await app.call("reader.topics", {candidates: includeCandidates});
            if (!visible || generation !== id) return;
            reports = data.reports;
            if (includeCandidates) {
                suggestions.replaceChildren(...data.candidates.map(c => {
                    const option = button(c.name, () => choose(c.name), {title: c.days + " 份日报 · 出现 " + c.mentions + " 次"});
                    option.dataset.name = c.name;
                    return option;
                }));
                if (!data.candidates.length) suggestions.textContent = "暂无候选";
                if (!name.value) name.value = app.state.topicName || reports[0]?.name || "";
            }
            render();
            if (selected()?.note && !pending) note.textContent = selected().note;
            if (reports.some(r => r.state === "running")) timer = setTimeout(() => load(), 1500);
        } catch (error) {
            if (visible && generation === id) note.textContent = error.message;
        }
    }
    async function update() {
        const topic = name.value.trim();
        if (!topic || pending) return;
        pending = true;
        render();
        try {
            if (!await app.confirm("更新主题报", "将「" + topic + "」的原主题报和相关日报新材料交给当前摘要 API。材料较多时会分批计费。") || !visible) return;
            const request = {name: topic, confirmed: true};
            let res = await app.call("reader.topicUpdate", request);
            if (res.state === "confirm_retry") {
                if (!await app.confirm("重新提交本批材料", res.note) || !visible) return;
                res = await app.call("reader.topicUpdate", {...request, repeat: true});
            }
            if (!visible) return;
            choose(res.name);
            await load();
            note.textContent = res.note;
        } catch (error) {
            if (visible) note.textContent = error.message;
        } finally {
            pending = false;
            if (visible) render();
        }
    }
    name.oninput = () => { note.textContent = ""; render(); };
    return {
        show() { visible = true; void load(true); },
        hide() { visible = false; generation++; clearTimeout(timer); },
    };
}
