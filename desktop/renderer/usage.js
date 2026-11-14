import {el} from "./controls.js";

const money = value => value == null ? "未知" : Number(value).toFixed(6);
const amount = (value, unknown) => money(value) + (unknown ? ` + ${unknown} 次未知` : "");
const beijing = new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit",
    timeZone: "Asia/Shanghai", hourCycle: "h23",
});
const colors = ["#6ea8ff", "#d48c26", "#43ba88", "#a97cf0"];
const priceProvider = row => `${row.provider || "历史未记录"} / ${row.model || "未知"}`;
const groupColumns = [
    ["用途", row => row.purpose], ["消息类型", row => row.source_type], ["请求数", row => row.calls, "calls"],
    ["输入 Token", row => row.in_tokens, "in_tokens"], ["输出 Token", row => row.out_tokens, "out_tokens"],
    ["总金额（元）", row => amount(row.cost_cny, row.unknown_calls), "cost_cny"],
    ["今日消费（元）", row => amount(row.today_cost_cny, row.today_unknown_calls), "today_cost_cny"],
    ["平台 / 模型", priceProvider], ["美元", row => amount(row.cost_usd, row.unknown_usd_calls), "cost_usd"],
];
const detailColumns = [
    ["时间（北京）", row => beijing.format(new Date(row.ts * 1000))],
    ["用途", row => row.purpose], ["关联消息", row => row.source_label],
    ["输入", row => row.in_tokens, "in_tokens"], ["输出", row => row.out_tokens, "out_tokens"],
    ["缓存读命中", row => row.cache_hit_tokens, "cache_hit_tokens"],
    ["总金额（元）", row => money(row.cost_cny), "cost_cny"],
    ["结果", row => row.status], ["今日消费（元）", row => money(row.today_cost_cny), "today_cost_cny"],
    ["平台 / 模型", priceProvider], ["美元", row => money(row.cost_usd), "cost_usd"],
    ["服务层级", row => row.tier_label], ["缓存写入", row => row.cache_write_tokens, "cache_write_tokens"],
];

function usageTable(columns, onSelect) {
    let rows = [], ordering;
    const table = el("table", {className: "usage-table"});
    const sidebar = el("div", {className: "usage-table-wrap"}, table);
    function render() {
        // 排序按账本原值；价格未知的行始终排在底部。
        const shown = [...rows];
        if (ordering) {
            const [key, descending] = ordering;
            shown.sort((a, b) => a[key] == null ? (b[key] == null ? 0 : 1) :
                b[key] == null ? -1 : descending ? b[key] - a[key] : a[key] - b[key]);
        }
        const headers = columns.map(([label, , key]) => {
            if (!key) return el("th", {scope: "col", textContent: label});
            const selected = ordering?.[0] === key;
            const descending = selected && ordering[1];
            return el("th", {scope: "col", ariaSort: selected ? (descending ? "descending" : "ascending") : "none"},
                el("button", {type: "button", textContent: label + (selected ? (descending ? " ↓" : " ↑") : ""),
                    onclick: () => { ordering = [key, !descending]; render(); }}));
        });
        const body = shown.map(row => {
            const cells = columns.map(([, value], index) => {
                const text = String(value(row) ?? "未知");
                if (onSelect && index === 2) {
                    return el("td", {}, el("button", {type: "button", className: "usage-source",
                        textContent: text, onclick: () => onSelect(row)}));
                }
                return el("td", {textContent: text});
            });
            return el("tr", {}, ...cells);
        });
        if (!body.length) body.push(el("tr", {}, el("td", {colSpan: columns.length, textContent: "暂无记录"})));
        table.replaceChildren(el("thead", {}, el("tr", {}, ...headers)), el("tbody", {}, ...body));
    }
    return {node: sidebar, set(value) { rows = value; render(); }};
}

function trendChart(data) {
    const series = Object.entries(data.trend);
    const note = el("p", {className: "reader-meta", ariaLive: "polite"});
    if (!series.length) return el("p", {textContent: "暂无消费趋势"});
    const ns = "http://www.w3.org/2000/svg";
    function svg(tag, attrs, content = "") {
        const node = document.createElementNS(ns, tag);
        for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
        if (content) node.textContent = content;
        return node;
    }
    const chart = svg("svg", {viewBox: "0 0 860 225", role: "img", "aria-label": "最近三十天的消费趋势"});
    const width = 780, left = 58, top = 18, height = 177;
    const days = series[0][1];
    const max = Math.max(0.000001, ...series.flatMap(([, rows]) => rows.map(row => row.cost_cny ?? 0)));
    const x = index => left + width * index / (days.length - 1);
    const y = value => top + height * (1 - value / max);
    for (let row = 0; row <= 4; row++) {
        const pos = top + row * height / 4;
        chart.append(svg("line", {x1: left, x2: left + width, y1: pos, y2: pos, class: "usage-grid"}));
        chart.append(svg("text", {x: left - 6, y: pos + 4, "text-anchor": "end"}, (max * (1 - row / 4)).toFixed(4)));
    }
    for (const [index, [, rows]] of series.entries()) {
        let path = "", previousKnown = false;
        for (const [i, row] of rows.entries()) {
            if (row.cost_cny === null) {
                const px = x(i), py = y(0) - 3 - index * 7;
                chart.append(svg("path", {d: `M ${px-3} ${py-3} l 6 6 m -6 0 l 6 -6`, stroke: "#d48c26"}));
                previousKnown = false;
            } else {
                const px = x(i), py = y(row.cost_cny);
                path += `${previousKnown ? "L" : "M"} ${px} ${py} `;
                chart.append(svg("circle", {cx: px, cy: py, r: 2, fill: colors[index]}));
                previousKnown = true;
            }
        }
        chart.append(svg("path", {d: path, fill: "none", stroke: colors[index], "stroke-width": 2}));
    }
    for (const index of [0, 7, 14, 21, 29]) {
        chart.append(svg("text", {x: x(index), y: 215, "text-anchor": "middle"}, days[index].day.slice(5)));
    }
    const unknown = series.reduce((sum, [, rows]) => sum + rows.reduce((a, row) => a + row.unknown_calls, 0), 0);
    const explanation = "最近 30 天 · 北京时间" + (unknown ? ` · ${unknown} 次费用未知，橙色叉号表示当日费用全部未知` : "");
    const date = el("select", {ariaLabel: "查询每日金额"}, ...days.map((day, i) =>
        el("option", {value: String(i), textContent: day.day})));
    date.value = String(days.length - 1);
    const describe = index => { note.textContent = data.trend_notes[index]; };
    date.addEventListener("change", () => describe(Number(date.value)));
    chart.addEventListener("pointermove", event => {
        const point = new DOMPoint(event.clientX, event.clientY).matrixTransform(chart.getScreenCTM().inverse());
        const relative = (point.x - left) / width;
        describe(Math.round(Math.max(0, Math.min(1, relative)) * (days.length - 1)));
    });
    chart.addEventListener("pointerleave", () => describe(Number(date.value)));
    describe(days.length - 1);
    return el("div", {className: "usage-chart"},
        el("div", {className: "toolbar"}, el("span", {textContent: explanation}), date),
        chart,
        el("div", {className: "usage-legend"}, ...series.map(([name], i) =>
            el("span", {style: `--series-color: ${colors[i]}`, textContent: name}))),
        note);
}

export function mount(container, app) {
    let closed = false, selection = 0;
    const status = el("p", {className: "reader-meta", ariaLive: "polite"});
    const utc = el("p", {className: "reader-meta"});
    const sources = el("div", {className: "usage-sources"});
    const trend = el("div");
    const totals = usageTable(groupColumns);
    const details = usageTable(detailColumns, loadSource);
    const refresh = el("button", {type: "button", textContent: "刷新消费明细", onclick: () => void load()});
    const sourcePanel = el("section", {className: "panel usage-section", hidden: true},
        el("h3", {textContent: "关联消息"}), sources);
    container.append(el("div", {className: "usage-page"},
        el("section", {className: "panel usage-section"},
            el("div", {className: "toolbar"}, el("h3", {textContent: "消费汇总"}), refresh), status, utc, totals.node),
        el("section", {className: "panel usage-section"}, el("h3", {textContent: "消费趋势"}), trend),
        el("section", {className: "panel usage-section"}, el("h3", {textContent: "最近请求"}),
            el("p", {className: "reader-meta", textContent: "显示最近 500 次请求。点击关联消息查看原文，点击数字表头排序。"}), details.node),
        sourcePanel));

    async function loadSource(row) {
        const request = ++selection;
        const heading = el("p", {textContent: `${row.purpose} · ${priceProvider(row)} · ${row.tier_label}`});
        const pricing = row.pricing || {};
        const prices = el("p", {className: "reader-meta", textContent: Object.keys(pricing).length ?
            `基准单价 / 百万 Token · 输入 ${pricing.input_price ?? "未知"} / 缓存 ${pricing.cached_price ?? "未知"} / 输出 ${pricing.output_price ?? "未知"} ${pricing.currency ?? ""}。金额已按档位折算。` : ""});
        const lines = el("div", {textContent: "正在读取关联消息…"});
        sources.replaceChildren(heading, prices, lines);
        sourcePanel.hidden = false;
        sourcePanel.scrollIntoView({block: "nearest"});
        try {
            const result = await app.call("usage.sources", {id: row.id});
            if (closed || request !== selection) return;
            lines.replaceChildren(el("p", {textContent: `共关联 ${result.total} 条，显示前 ${result.shown_limit} 条中的 ${result.items.length} 条。已清理的消息无法查看。`}),
                ...result.items.map(item => el("div", {className: "usage-message"},
                    el("strong", {textContent: `群 ${item.group_id} · ${item.sender_name} · 消息 ${item.msg_id}（#${item.id}）`}),
                    el("p", {textContent: item.text}))));
        } catch (error) {
            if (!closed && request === selection) lines.textContent = `读取失败 · ${error.message}`;
        }
    }

    async function load() {
        if (refresh.disabled) return;
        refresh.disabled = true;
        status.textContent = "正在读取本地账本…";
        try {
            const data = await app.call("usage.details");
            if (closed) return;
            status.textContent = `累计 ¥${money(data.visible_cost_cny ?? 0)} · 今日 ¥${money(data.today_cost_cny ?? 0)}（北京）· 金额为本地估算，未知费用单列`;
            const labels = data.openai_utc_days.slice(0, 3).map(day =>
                `${day.day} $${amount(day.cost_usd, Math.max(day.unknown_calls, day.unknown_usd_calls))}`);
            utc.textContent = `OpenAI UTC 日用量 · ${labels.join(" · ") || "暂无记录"}。UTC 00:00 对应北京 08:00，平台账单可包含其他程序的调用。`;
            totals.set(data.groups);
            details.set(data.items);
            trend.replaceChildren(trendChart(data));
        } catch (error) {
            if (!closed) status.textContent = `读取消费明细失败 · ${error.message}`;
        } finally {
            refresh.disabled = false;
        }
    }

    void load();
    return () => { closed = true; };
}
