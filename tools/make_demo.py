"""生成一批仿真的 OneBot 群消息事件，用于在 NapCat 就绪前验证全链路。

用法：
    python tools/make_demo.py                 # 只生成 data/demo_events.json
    python tools/make_demo.py --post          # 生成并直接投递到本地服务
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEMO_DIR = ROOT / "data" / "demo"
SELF_ID = "10001"
G1 = "700001"
G2 = "700002"

FAKE_TRACEBACK = """Traceback (most recent call last):
  File "train.py", line 214, in <module>
    main()
  File "train.py", line 198, in main
    model = AutoModelForCausalLM.from_pretrained(
RuntimeError: CUDA out of memory. Tried to allocate 2.40 GiB
GPU 0 has a total capacity of 23.64 GiB of which 1.12 GiB is free
torch.cuda.OutOfMemoryError: CUDA out of memory
"""

FAKE_BENCH = """vLLM 0.9.2  vs  SGLang 0.4.6
Qwen3-32B  A100x2  TP=2
vLLM   : 1842 tok/s
SGLang : 2103 tok/s
"""


def make_images() -> dict:
    DEMO_DIR.mkdir(parents=True, exist_ok=True)
    from PIL import Image, ImageDraw

    out = {}
    for name, text, size in (
        ("traceback.png", FAKE_TRACEBACK, (900, 260)),
        ("bench.png", FAKE_BENCH, (620, 220)),
        ("meme.png", "lol", (200, 160)),
    ):
        img = Image.new("RGB", size, "white")
        draw = ImageDraw.Draw(img)
        y = 12
        for line in text.splitlines():
            draw.text((12, y), line, fill=(20, 20, 20))
            y += 17
        p = DEMO_DIR / name
        img.save(p)
        out[name] = p.as_uri()
    return out


def ev(gid: str, uid: str, name: str, mid: int, when: int, segs: list) -> dict:
    return {
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "time": when,
        "self_id": SELF_ID,
        "group_id": gid,
        "user_id": uid,
        "message_id": str(mid),
        "raw_message": "",
        "sender": {"nickname": name, "card": name},
        "message": segs,
    }


def text(s: str) -> dict:
    return {"type": "text", "data": {"text": s}}


def image(url: str) -> dict:
    return {"type": "image", "data": {"url": url, "file": url}}


def at(qq: str) -> dict:
    return {"type": "at", "data": {"qq": qq}}


def build(imgs: dict) -> list[dict]:
    now = int(time.time()) - 1500
    e: list[dict] = []
    mid = 1000

    def add(gid, uid, name, segs, offset):
        nonlocal mid
        mid += 1
        e.append(ev(gid, uid, name, mid, now + offset, segs))

    # ---- 群 1：AI 底层原理交流 ----
    add(G1, "20001", "阿哲", [text("早")], 0)
    add(G1, "20002", "小林", [text("早，今天有人看新发的那篇吗")], 20)
    add(G1, "20003", "老王", [{"type": "face", "data": {"id": "178"}}], 35)
    add(G1, "20004", "CC", [text("摸鱼中")], 50)
    add(G1, "20005", "David", [{"type": "mface", "data": {"url": imgs["meme.png"]}}], 62)
    add(G1, "20002", "小林", [text("哈哈")], 70)

    add(
        G1,
        "20006",
        "老张",
        [
            text(
                "分享一个刚看到的仓库，把 MoE 的专家路由做了个可视化，跑起来挺方便："
                "https://github.com/example-org/moe-router-viz"
            )
        ],
        120,
    )
    add(G1, "20005", "David", [text("这个我上周试过，torch 版本要求 2.4+")], 150)
    add(G1, "20001", "阿哲", [text("mark")], 160)
    add(G1, "20004", "CC", [text("mark+1")], 170)

    add(
        G1,
        "20007",
        "小赵",
        [
            text("救命，双卡 A100 跑 32B 一直 OOM，按官方脚本改的 TP=2，报错如下"),
            image(imgs["traceback.png"]),
        ],
        240,
    )
    add(
        G1,
        "20008",
        "陈工",
        [
            text(
                "试一下 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True "
                "再把 max_model_len 从 32768 降到 16384，我们这么配就不炸了"
            )
        ],
        300,
    )
    add(G1, "20007", "小赵", [text("管用！降到 16384 直接起来了，多谢")], 420)
    add(G1, "20008", "陈工", [text("嗯，主要是 KV cache 预分配吃满了")], 440)

    add(
        G1,
        "20009",
        "HR-琳达",
        [
            text(
                "打扰一下，我们组在招推理优化方向的工程师，base 北京/远程都可，"
                "做 LLM 推理加速和显存优化，有兴趣的私我，可内推。"
            )
        ],
        520,
    )
    add(G1, "20001", "阿哲", [text("有实习吗")], 540)
    add(G1, "20009", "HR-琳达", [text("有的，2026 届实习也招")], 560)

    add(
        G1,
        "20010",
        "研究员K",
        [
            text(
                "arXiv 上那篇讲长上下文位置编码外推的新工作值得看，实验比我预期的扎实，"
                "在 128k 以上还能稳住，而且没怎么掉短上下文性能：https://arxiv.org/abs/2501.00001 "
                "不过它的基线只比了 RoPE 和 YaRN，没和 NTK-aware 比，这块我觉得证据不够。"
            )
        ],
        640,
    )
    add(G1, "20002", "小林", [text("同感，消融做得有点薄")], 700)

    for uid, name in (("20011", "路人甲"), ("20012", "路人乙"), ("20013", "路人丙")):
        add(
            G1,
            uid,
            name,
            [text("这套 prompt 工程模板合集不错，收藏了 https://github.com/example-org/prompt-templates")],
            760,
        )

    add(G1, "20004", "CC", [text("中午吃啥")], 830)
    add(G1, "20005", "David", [{"type": "face", "data": {"id": "99"}}], 850)

    add(
        G1,
        "20002",
        "小林",
        [
            at(SELF_ID),
            text("你上次说的那个显存碎片整理的工具叫啥来着？我翻不到记录了"),
        ],
        900,
    )

    add(G1, "20003", "老王", [text("下班了各位")], 960)

    # ---- 群 2：模型部署实践 ----
    add(G2, "30001", "老李", [text("问下大家 vllm 部署都用什么镜像")], 60)
    add(G2, "30002", "小孙", [text("官方 vllm/vllm-openai:v0.9.2")], 90)
    add(
        G2,
        "30003",
        "大刘",
        [
            text(
                "踩坑记录：vllm 0.9.2 配 Qwen3 的 tool call 时，"
                "--enable-auto-tool-choice 和 --tool-call-parser hermes 必须同时给，"
                "少一个会静默退化成纯文本输出，不报错，非常坑。"
            )
        ],
        180,
    )
    add(G2, "30001", "老李", [text("收到，感谢")], 210)
    add(G2, "30003", "大刘", [text("另外并发超过 64 记得调 --max-num-seqs，不然排队严重")], 260)
    add(G2, "30004", "小周", [{"type": "face", "data": {"id": "12"}}], 300)
    add(G2, "30005", "阿May", [text("学到了")], 320)
    return e


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--post", action="store_true", help="投递到本地服务")
    ap.add_argument("--port", type=int, default=6701)
    ap.add_argument("--reset", action="store_true", help="投递前先清空数据库")
    args = ap.parse_args()

    if args.reset:
        db = ROOT / "data" / "msgx.db"
        for suffix in ("", "-wal", "-shm"):
            Path(str(db) + suffix).unlink(missing_ok=True)
        print("已清空数据库")

    imgs = make_images()
    events = build(imgs)
    out = ROOT / "data" / "demo_events.json"
    out.write_text(json.dumps(events, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已生成 {len(events)} 条事件 -> {out}")

    if args.post:
        url = f"http://127.0.0.1:{args.port}/inject"
        body = json.dumps(events).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                data = json.loads(r.read().decode("utf-8"))
            print(f"投递成功: {data.get('count')} 条")
        except Exception as exc:  # noqa: BLE001
            print(f"投递失败（服务未启动？）：{exc}")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
