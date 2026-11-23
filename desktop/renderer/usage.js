import {el} from "./controls.js";

const sumLabel = (value, unit) => (unit === "usd" ? "$" : "¥") + Number(value).toFixed(6);
const native = (row, unit) => row[unit] == null ? "—" : sumLabel(row[unit], unit);
const number = value => value == null ? "—" : new Intl.NumberFormat("zh-CN").format(value);
const beijing = new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit",
    timeZone: "Asia/Shanghai", hourCycle: "h23",
});
const colors = ["#6ea8ff", "#d48c26", "#43ba88", "#a97cf0"];
const priceProvider = row => `${row.provider || "历史未记录"} / ${row.model || "未知"}`;
const groupColumns = [
    ["API / 模型", priceProvider], ["用途", row => row.purpose], ["成功请求", row => number(row.calls), "calls"],
    ["输入 Token", row => number(row.in_tokens), "in_tokens"], ["输出 Token", row => number(row.out_tokens), "out_tokens"],
    ["美元 USD", row => native(row, "usd"), "usd"], ["人民币 CNY", row => native(row, "cny"), "cny"],
];
const detailColumns = [
    ["时间（北京）", row => beijing.format(new Date(row.ts * 1000))], ["API / 模型", priceProvider],
    ["关联消息", row => row.source_label], ["用途", row => row.purpose], ["结果", row => row.status],
    ["输入 Token", row => number(row.in_tokens), "in_tokens"], ["输出 Token", row => number(row.out_tokens), "out_tokens"],
    ["美元 USD", row => native(row, "usd"), "usd"], ["人民币 CNY", row => native(row, "cny"), "cny"],
    ["服务层级", row => row.tier_label], ["缓存读取", row => number(row.cache_hit_tokens), "cache_hit_tokens"],
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
    const currency = el("select", {ariaLabel: "图表币种"},
        el("option", {value: "usd"}, "美元 USD"), el("option", {value: "cny"}, "人民币 CNY"));
    if (!data.daily.some(row => row.usd > 0) && data.daily.some(row => row.cny > 0)) currency.value = "cny";
    const day = el("select", {ariaLabel: "查询每日金额"}, data.days.map(d => el("option", {value: d}, d)));
    day.value = data.days.at(-1);
    const canvas = el("div");
    const legend = el("div", {className: "usage-legend"});
    const note = el("p", {className: "reader-meta", ariaLive: "polite"});
    function svg(tag, attrs, text) {
        const n = document.createElementNS("http://www.w3.org/2000/svg", tag);
        for (const [name, value] of Object.entries(attrs)) n.setAttribute(name, value);
        if (text) n.textContent = text;
        return n;
    }
    function renderDay(date) {
        const unit = currency.value;
        const amounts = data.daily.filter(row => row.day === date && row[unit] > 0);
        const parts = amounts.map(row => priceProvider(row) + " " + sumLabel(row[unit], unit));
        note.textContent = date + " · " + (parts.join("　") || sumLabel(0, unit));
    }
    function draw() {
        const unit = currency.value;
        const chart = svg("svg", {viewBox: "0 0 900 210", role: "img", "aria-label": "按 API 和请求日期统计的每日消费柱状图"});
        const names = [...new Set(data.daily.filter(row => row[unit] > 0).map(priceProvider))];
        const rows = data.days.map(d => data.daily.filter(row => row.day === d && row[unit] > 0));
        const max = Math.max(0, ...rows.map(items => items.reduce((sum, r) => sum + r[unit], 0)));
        for (let i = 0; i <= 3; i++) {
            const y = 172 - 150 * i / 3;
            chart.append(svg("line", {x1: 65, x2: 891, y1: y, y2: y, class: "usage-grid"}),
                svg("text", {x: 62, y: y + 4, "text-anchor": "end"}, Number((max*i/3).toPrecision(3)).toString()));
        }
        rows.forEach((items, i) => {
            const x = 70 + i*27;
            let y = 172;
            for (const row of items) {
                const height = 150 * row[unit] / max;
                y -= height;
                const bar = svg("rect", {x, y, width: 18, height, fill: colors[names.indexOf(priceProvider(row)) % colors.length]});
                bar.append(svg("title", {}, row.day + " · " + priceProvider(row) + " " + sumLabel(row[unit], unit)));
                chart.append(bar);
            }
            const target = svg("rect", {x: x-3, y: 8, width: 26, height: 167, fill: "transparent"});
            target.onpointerenter = () => renderDay(data.days[i]);
            target.onclick = () => { day.value = data.days[i]; renderDay(day.value); };
            chart.append(target);
            if ([0,7,14,21,29].includes(i)) chart.append(svg("text", {x: x+8, y: 198, "text-anchor": "middle"}, data.days[i].slice(5)));
        });
        chart.onpointerleave = () => renderDay(day.value);
        canvas.replaceChildren(chart);
        legend.replaceChildren(...names.map((name,i) => el("span",{style: "--series-color:" + colors[i%colors.length]},name)));
        renderDay(day.value);
    }
    currency.onchange = draw;
    day.onchange = () => renderDay(day.value);
    draw();
    return el("div", {className:"usage-chart"}, el("div",{className:"toolbar"}, currency, day), canvas, legend, note);
}

export function mount(container, app) {
    let closed = false, selection = 0;
    const status = el("p", {className: "reader-meta", ariaLive: "polite"});
    const cards = el("div", {className: "spend-cards"});
    const sources = el("div", {className: "usage-sources"});
    const trend = el("div");
    const totals = usageTable(groupColumns);
    const details = usageTable(detailColumns, loadSource);
    const refresh = el("button", {type: "button", textContent: "刷新消费明细", onclick: () => void load()});
    const sourcePanel = el("section", {className: "panel usage-section", hidden: true},
        el("h3", {textContent: "关联消息"}), sources);
    container.append(el("div", {className: "usage-page"},
        el("section", {className: "panel usage-section"},
            el("div", {className: "toolbar"}, el("h3", {textContent: "消费汇总"}), refresh), status, cards),
        el("section", {className: "panel usage-section"}, el("h3", {textContent: "每日支出 · 近 30 天"}), trend),
        el("section", {className: "panel usage-section"}, el("h3", {textContent: "按 API 和用途"}), totals.node),
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
            status.textContent = "按请求发起日（北京）统计成功且有原币金额的支出。补生成计入实际调用当天。";
            cards.replaceChildren(...[["usd","美元 USD"],["cny","人民币 CNY"]].map(([unit,label]) =>
                el("div", {}, el("span", {}, label), el("strong", {}, sumLabel(data.totals[unit],unit)),
                    el("small", {}, "今日 " + sumLabel(data.totals["today_"+unit],unit)))));
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
