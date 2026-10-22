"""群归属迁移、保留消息的清理规则与北京时间基线；仅用临时库和模拟协议。"""
from __future__ import annotations

import asyncio
import atexit
import sqlite3
import sys
import tempfile
import time
from contextlib import ExitStack, closing
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from msgx.models import Msg  # noqa: E402
from msgx.store import SCHEMA, Store  # noqa: E402
from msgx.protocol_session import ProtocolSession  # 当前协议群同步入口。
from msgx.time_rules import BEIJING  # 合成数据采用生产代码约定的北京时间。

fails: list[str] = []
resources = ExitStack()  # 数据库先关闭，再回收包含 WAL/备份的临时目录。
atexit.register(resources.close)  # 断言或异常提前终止脚本时也释放隔离资源。


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'OK  ' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))
    if not ok:
        fails.append(name)


def new_store() -> Store:
    d = Path(resources.enter_context(tempfile.TemporaryDirectory(prefix="msgx_grp_")))
    return resources.enter_context(closing(Store(d / "t.db")))  # 后注册的连接先关闭。


def msg(gid: str, mid: str, text: str = "内容够长的一条消息") -> Msg:
    return Msg(msg_id=mid, group_id=gid, text=text, ts=int(time.time()))


print("=== 老库迁移：没有 bot_uin 也要能升上来 ===")
d = Path(resources.enter_context(tempfile.TemporaryDirectory(prefix="msgx_grp_old_")))
db = d / "old.db"
# 造一个「v0.5.3 的表」：groups 只有 id/name/added_at，且已有数据
raw = sqlite3.connect(str(db))
old_schema = SCHEMA.replace(
    "  added_at   INTEGER DEFAULT 0,\n  bot_uin    TEXT DEFAULT ''\n",
    "  added_at   INTEGER DEFAULT 0\n",
)
assert "bot_uin" not in old_schema, "旧 schema 构造失败，测试前提不成立"
raw.executescript(old_schema)
raw.execute("INSERT INTO groups(id, name, added_at) VALUES('111','老群',1)")
raw.execute(
    "INSERT INTO messages(group_id, msg_id, ts, text) VALUES('111','m1',1,'老消息')"
)
raw.commit()
raw.close()

st = resources.enter_context(closing(Store(db)))  # 构造时应自动补列，结束时关闭连接。
cols = {c[1] for c in st.conn.execute("PRAGMA table_info(groups)")}
check("补齐 bot_uin 列", "bot_uin" in cols, str(sorted(cols)))
check("老群还在（迁移不丢数据）", st.group_names().get("111") == "老群")
check("老消息还在", st.messages_since(0) == 1, str(st.messages_since(0)))
check("老行属主为空（未知，不当成别人的群）",
      st.groups_with_owner()[0]["bot_uin"] == "", repr(st.groups_with_owner()[0]))

print("\n=== 属主写入：空属主不覆盖已知属主 ===")
st = new_store()
st.upsert_group("200", "AI 群", "1443685390")
st.upsert_group("200", "", "")  # 拿不到 get_login_info 的情形
check("已知属主被保住", st.groups_with_owner()[0]["bot_uin"] == "1443685390",
      st.groups_with_owner()[0]["bot_uin"])
st.upsert_group("200", "", "999")
check("有属主时可以改写（换账号登录后要能纠正）",
      st.groups_with_owner()[0]["bot_uin"] == "999")

print("\n=== 消息是最可靠的属主来源 ===")
st = new_store()
st.insert_message(msg("300", "m1"), "群A", "555")
check("入库消息时顺手登记属主", st.groups_with_owner()[0]["bot_uin"] == "555",
      st.groups_with_owner()[0]["bot_uin"])
st.insert_message(msg("300", "m2"), "", "")
check("后续消息不带属主也不抹掉它",
      st.groups_with_owner()[0]["bot_uin"] == "555")

print("\n=== 清理：删别的账号的空群 ===")
st = new_store()
st.upsert_group("1", "有消息的群", "111")
st.upsert_group("2", "别的账号的空群", "222")
st.upsert_group("3", "属主未知的空群", "")
st.insert_message(msg("1", "m1"), "有消息的群", "111")
deleted, protected = st.prune_groups({"1"})  # 当前账号只有群 1
check("空群被删", sorted(deleted) == ["2", "3"], str(deleted))
check("有消息的群被保留（哪怕不在当前账号列表里）", protected == [], str(protected))
check("表里只剩有消息的那个群", list(st.group_names()) == ["1"], str(list(st.group_names())))
check("消息数据没被动过", st.messages_since(0) == 1)

print("\n=== 清理：有消息的群绝不删（幂等、可反复调用）===")
st = new_store()
st.upsert_group("1", "有消息的群", "111")
st.insert_message(msg("1", "m1"), "有消息的群", "111")
d1, p1 = st.prune_groups({"999"})  # 当前账号里没有群 1
check("有消息 ⇒ 进 protected 而不是 deleted", d1 == [] and p1 == ["1"], f"{d1} / {p1}")
check("群还在（摘要调度不会因此失效）", "1" in st.group_names())
d2, p2 = st.prune_groups({"999"})
check("再调一次结果一样（幂等）", (d2, p2) == ([], ["1"]))

print("\n=== 清理：协议端抖动返回空列表时不能照删 ===")
st = new_store()
st.upsert_group("1", "有消息的群", "111")
st.upsert_group("2", "空群", "222")
st.insert_message(msg("1", "m1"), "有消息的群", "111")
pipe = SimpleNamespace(store=st, target_account="111", login_uin=AsyncMock(return_value="111"),
                       call_action=AsyncMock(return_value=[]))  # 模拟协议暂时返回空群列表。
result = asyncio.run(ProtocolSession.refresh_group_names(pipe, prune=True))
check("空协议列表不会删除任何已有群", set(st.group_names()) == {"1", "2"})
check("空协议列表如实返回零个群且无删除", result["count"] == 0 and result["deleted"] == [])

print("\n=== messages_since / hour_baseline：看门狗的判据 ===")
st = new_store()
now = int(time.time())
st.conn.execute(
    "INSERT INTO messages(group_id, msg_id, ts, text) VALUES('g','a',?,'正常消息')", (now,)
)
st.conn.execute(
    "INSERT INTO messages(group_id, msg_id, ts, text) VALUES('g','p',?, '【链路自检】探针')",
    (now,),
)
st.conn.commit()
check("messages_since 排除探针行", st.messages_since(now - 60) == 1, str(st.messages_since(now - 60)))
check("更早的起点能数到全部真实消息", st.messages_since(0) == 1)
check("未来的起点数不到东西", st.messages_since(now + 3600) == 0)

# hour_baseline：造 3 天，同一小时分别 0/2/10 条 ⇒ 中位数 2
st = new_store()
hour = 12  # 使用固定北京时间，避免克隆者的系统时区影响用例。
hour_start = int(datetime(2026, 10, 10, hour, tzinfo=BEIJING).timestamp())
for off, n in ((0, 0), (86400, 2), (2 * 86400, 10)):
    for i in range(n):
        st.conn.execute(
            "INSERT INTO messages(group_id, msg_id, ts, text) VALUES('g',?,?,'x')",
            (f"{off}-{i}", hour_start - off + 60),
        )
st.conn.commit()
with patch("msgx.message_store.time.time", return_value=hour_start + 1800), \
        patch("time.localtime", side_effect=AssertionError("小时基线不得读取主机时区")):
    base = st.hour_baseline(hour, days=7)  # 冻结当前时间，让三天样本始终在查询窗口中。
    empty_hour = st.hour_baseline((hour + 5) % 24)
check("取中位数（不被某天刷屏抬高）", base == 2, str(base))
check("其它小时没有基线 ⇒ 0（深夜不误报的前提）", empty_hour == 0, str(empty_hour))

print("\n=== 协议端脚本解析：重启脚本 ===")
from msgx import napcat  # noqa: E402
from msgx.napcat import restart_script  # noqa: E402

check("未配置时返回 None", restart_script({}) is None)
check("相对路径拼到项目根",
      str(restart_script({"napcat": {"restart_script": "tools/x.bat"}})).endswith("tools\\x.bat")
      or str(restart_script({"napcat": {"restart_script": "tools/x.bat"}})).endswith("tools/x.bat"),
      str(restart_script({"napcat": {"restart_script": "tools/x.bat"}})))
check("绝对路径原样返回",
      str(restart_script({"napcat": {"restart_script": "D:\\a\\b.bat"}})) == "D:\\a\\b.bat")
check("默认配置里指向 tools/重启协议端.bat",
      str(restart_script({"napcat": {"restart_script": "tools/重启协议端.bat"}})).endswith("重启协议端.bat"))

print("\n=== 重置协议端：不合格的输入必须在提权之前就被挡下 ===")
# 这些分支都能**在不弹 UAC、不动任何进程**的前提下走完 —— 正好适合做回归。
# 一旦有人把校验挪到提权之后，「点了没反应」这类悬案就会回来。
r1 = napcat.restart({})
check("没配 restart_script ⇒ 明确报错",
      r1["ok"] is False and "restart_script" in r1["error"], r1["error"])
r2 = napcat.restart({"napcat": {"restart_script": "tools/重启协议端.bat", "dir": "Z:/__msgx_nope__"}})
check("NapCat 目录里没有 launcher.bat ⇒ 拒绝（宁可报错，也不能悄悄拉起另一个 NapCat）",
      r2["ok"] is False and "launcher.bat" in r2["error"], r2["error"])

print("\n=== 上游健康：qq_msg_db_age 不猜 ===")
import os  # noqa: E402

empty = Path(resources.enter_context(tempfile.TemporaryDirectory(prefix="msgx_noqq_")))
with patch.dict(os.environ, {"QQ_DATA_ROOT": str(empty)}):  # 结束后还原原环境变量。
    check("找不到 QQ 数据目录时返回 None（而不是 0 或极大值）",
          napcat.qq_msg_db_age() is None, repr(napcat.qq_msg_db_age()))
    # 造一个假的账号目录与 -wal 文件
    wal = empty / "12345" / "nt_qq" / "nt_db" / "nt_msg.db-wal"
    wal.parent.mkdir(parents=True)
    wal.write_bytes(b"x")
    age = napcat.qq_msg_db_age()
    check("刚写过的 -wal ⇒ 年龄接近 0", age is not None and age < 5, repr(age))
    old = time.time() - 3600
    os.utime(wal, (old, old))
    age2 = napcat.qq_msg_db_age()
    check("一小时前写过 ⇒ 年龄约 3600 秒", age2 is not None and 3590 < age2 < 3700, repr(age2))

resources.close()  # 检查结果输出前回收所有测试目录，避免开发测试持续积累数据。
print()
if fails:
    print(f"有 {len(fails)} 项失败：")
    for f in fails:
        print("  -", f)
    raise SystemExit(1)
print("全部通过")
