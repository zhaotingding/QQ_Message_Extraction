"""清理 groups 表里「别的账号留下的群」。

## 它解决什么

`groups` 表是**历次登录的并集**（v0.5.4 之前没有「属主」这一列）：
谁登进来一次，它的全部群就永久留在库里。2026-09-21 实测 —— 误登录一次主号，
33 个群里灌进来 30 个一条消息都没有的群，用户在下拉框里再也找不到自己关心的那 3 个。

## 判据（刻意保守）

**只删「一条消息都没有」的群。** 有消息的群一律保留，理由是：
`groups` 表是**摘要调度的遍历源**（`summarize.py` 用 `group_names()` 遍历），
删掉一个有消息的群会让它的摘要**静默失效** —— 不报错、不告警，就是不再出摘要。
「有消息」是既成事实，比任何推断都可靠。

界面上那个「只保留当前账号的群」按钮是同一件事的更精确版本（它会先问协议端
「当前登录的是哪个账号」再删）；本工具是**服务没在跑时**也能用的离线版本。

## 用法

    python tools\\prune_groups.py                 # 默认 dry run，只打印会删什么
    python tools\\prune_groups.py --apply          # 真删（先自动备份整个库）
    python tools\\prune_groups.py --keep 123,456   # 额外保住的群号
    python tools\\prune_groups.py --apply --older-than 2026-09-21

默认 **dry run**：删数据这件事，看一眼再决定，比事后恢复便宜得多。
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJ = HERE.parent
if str(PROJ / "src") not in sys.path:
    sys.path.insert(0, str(PROJ / "src"))

from msgx.config import load  # noqa: E402


def db_path() -> Path:
    from msgx.config import abs_path

    try:
        cfg = load()
        return abs_path(cfg, "db_path")
    except Exception:  # noqa: BLE001
        return PROJ / "data" / "msgx.db"


def backup(p: Path) -> Path | None:
    """整个库 + WAL 一起备份。WAL 里可能有还没 checkpoint 的消息。"""
    if not p.exists():
        return None
    d = p.parent / "backup"
    d.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = d / f"{p.stem}-before-prune-groups-{stamp}{p.suffix}"
    shutil.copy2(p, out)
    for suffix in ("-wal", "-shm"):
        side = Path(str(p) + suffix)
        if side.exists():
            shutil.copy2(side, Path(str(out) + suffix))
    return out


def rows(conn: sqlite3.Connection) -> list[dict]:
    cols = {c[1] for c in conn.execute("PRAGMA table_info(groups)")}
    owner = "g.bot_uin" if "bot_uin" in cols else "'' AS bot_uin"
    sql = (
        f"SELECT g.id, g.name, g.added_at, {owner},"
        " (SELECT COUNT(*) FROM messages m WHERE m.group_id = g.id) AS n,"
        " (SELECT MAX(m.ts) FROM messages m WHERE m.group_id = g.id) AS last_ts"
        " FROM groups g ORDER BY g.id"
    )
    return [dict(r) for r in conn.execute(sql)]


def main() -> int:
    ap = argparse.ArgumentParser(description="清理不属于当前账号的空群（默认 dry run）")
    ap.add_argument("--apply", action="store_true", help="真的删除（默认只打印）")
    ap.add_argument("--keep", default="", help="额外保留的群号，逗号分隔")
    ap.add_argument(
        "--older-than",
        default="",
        help="只删这个日期（YYYY-MM-DD，本地时间）之前入库的群，例如 2026-09-21",
    )
    args = ap.parse_args()

    p = db_path()
    if not p.exists():
        print(f"找不到数据库：{p}")
        return 1
    keep = {x.strip() for x in args.keep.split(",") if x.strip()}
    cutoff = None
    if args.older_than:
        try:
            t = time.strptime(args.older_than, "%Y-%m-%d")
            cutoff = int(time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 23, 59, 59, 0, 0, -1)))
        except ValueError:
            print(f"--older-than 格式应为 YYYY-MM-DD，收到：{args.older_than}")
            return 1

    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    all_rows = rows(conn)
    conn.close()
    if not all_rows:
        print("groups 表是空的，没什么可清理。")
        return 0

    if "bot_uin" not in {c[1] for c in sqlite3.connect(str(p)).execute("PRAGMA table_info(groups)")}:
        print("⚠ 这个库还没有 bot_uin 列（属于 v0.5.4 之前）。属主未知，因此只按「有没有消息」判定。\n")

    doomed, kept = [], []
    for r in all_rows:
        if r["id"] in keep:
            kept.append((r, "你指定要保留"))
        elif r["n"] > 0:
            kept.append((r, f"有 {r['n']} 条消息"))
        elif cutoff is not None and int(r["added_at"] or 0) > cutoff:
            kept.append((r, "比 --older-than 更新"))
        else:
            doomed.append(r)

    print(f"数据库            : {p}")
    print(f"groups 表共       : {len(all_rows)} 个群")
    print(f"将删除（0 条消息）: {len(doomed)} 个")
    print(f"保留              : {len(kept)} 个")
    if doomed:
        print("\n--- 将删除 ---")
        for r in doomed:
            print(f"  {r['id']:<12} {str(r['name'] or '(未命名)'):<30}"
                  f" 属主 {r['bot_uin'] or '未知':<12} 入库于 "
                  f"{time.strftime('%m-%d %H:%M', time.localtime(int(r['added_at'] or 0)))}")
    if kept:
        print("\n--- 保留 ---")
        for r, why in kept[:40]:
            print(f"  {r['id']:<12} {str(r['name'] or '(未命名)'):<30} {why}")
        if len(kept) > 40:
            print(f"  …… 另有 {len(kept) - 40} 个")

    if not doomed:
        print("\n没有需要删除的群。")
        return 0
    if not args.apply:
        print("\n这是 dry run，**什么都没有改**。确认无误后加 --apply 真删。")
        return 0

    b = backup(p)
    if b is None:
        print("备份失败（数据库不存在？），为安全起见中止。")
        return 1
    print(f"\n已备份到：{b}")
    w = sqlite3.connect(str(p))
    try:
        w.executemany("DELETE FROM groups WHERE id=?", [(r["id"],) for r in doomed])
        w.commit()
    finally:
        w.close()
    print(f"已删除 {len(doomed)} 个群条目。**消息数据没有任何改动。**")
    print("若服务正在运行，界面上的群列表点一下「刷新群列表」即可看到结果。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
