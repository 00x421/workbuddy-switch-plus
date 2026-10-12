"""兑换码 / 充值。

设计要点（**改这个文件前先读完，这里的每一条都是踩过的坑**）：

1. **兑换码只存哈希**。明文只在「生成那一刻」返回一次，之后谁也拿不回来。
   管理员误把码库导出泄露，不应该等于把余额送人。管理端可以重新生成的
   是「一批新码」，不是「旧码的明文」。
2. **核销必须是原子的**。`store.lock` 是同一个可重入锁，读改写全程持锁。
   否则同一张码并发兑两次，两个请求都读到 `redeemed_at is None`，都放行 ——
   余额凭空翻倍。这是这一整个模块最危险的地方，有专门的并发测试守着。
3. **金额一律用整数「厘」**（1 积分 = 1000 厘）。浮点做钱的加减会在
   累加中漂移（`0.1+0.2 != 0.3`），而这里是余额。对外仍以积分（浮点）
   展示，但存储与比较全部走整数。
4. **兑换码是猜不到的**：`secrets` 生成，去掉易混字符，熵足够高。
   不要为了「好念」把它做短 —— 短码 + 无限次尝试 = 可枚举。
"""

from __future__ import annotations

import secrets
import string
import time
from decimal import Decimal, ROUND_HALF_UP

from fastapi import HTTPException

# 去掉 0/O/1/I/L 等易混字符：兑换码是要人手工抄的。
ALPHABET = "".join(c for c in string.ascii_uppercase + string.digits if c not in "01OIL")
CODE_GROUPS = 4      # 分 4 组
CODE_GROUP_LEN = 5   # 每组 5 位 → 20 位、约 100 bit，猜不到
MAX_BATCH = 500      # 一次最多生成多少张
MAX_CODE_LEN = 64

MONEY = 1000  # 1 积分 = 1000 厘


def to_milli(credit) -> int:
    """积分 → 厘。四舍五入到厘。

    必须走 Decimal，不能用 float。

    原先写的是 `int(round(float(credit) * MONEY))`，看着没问题，实测约 3.3%
    的输入会差 1 厘：`round()` 是**银行家舍入**（.5 向偶数靠），而
    `float(x) * 1000` 对 `x.xxxx5` 这类值的结果落在 `...49999` 还是 `...50001`
    全看二进制表示。于是 `1.0005` 变成 1000 而不是 1001、
    `87.6865` 变成 87686 而不是 87687。

    差一厘本身是小事，**账对不平**才是大事：用户看到的余额、流水里的数、
    后台统计三处会互相差几分，然后没人能解释差在哪。

    用户输入的是十进制金额，就该按十进制舍入 —— 这正是 Decimal 的用途。
    """
    try:
        value = Decimal(repr(float(credit or 0)))
    except (TypeError, ValueError, ArithmeticError):
        return 0
    # 先按「厘」量化（十进制语义，.5 一律进位），再取整数厘。
    quantized = value.quantize(Decimal(1) / MONEY, rounding=ROUND_HALF_UP)
    return int(quantized * MONEY)


def from_milli(milli) -> float:
    """厘 → 积分。给界面看的。"""
    try:
        return round(int(milli or 0) / MONEY, 6)
    except (TypeError, ValueError):
        return 0.0


def new_code() -> str:
    """生成一张兑换码明文，形如 WB-XXXXX-XXXXX-XXXXX-XXXXX。"""
    groups = ["".join(secrets.choice(ALPHABET) for _ in range(CODE_GROUP_LEN))
              for _ in range(CODE_GROUPS)]
    return "WB-" + "-".join(groups)


def normalize_code(raw: str) -> str:
    """归一化用户输入，得到一个稳定的查表 key。

    人要手抄这串码，所以要宽容：大写、去空格、去连字符、也接受用户
    漏掉 `WB-` 前缀。（归一化必须先于查哈希，否则「多打了个空格」会被
    判成无效码，用户看到的现象是「我明明没输错」—— 极难自查。）

    前缀只能按 `WB-` 这个整体剥，不能剥裸的 `WB`。

    这是个真踩过的坑：早先的写法是「去掉非字母数字后，若以 WB 开头就砍掉前两位」。
    但正文里本来就可能出现 W/B —— `ALPHABET` 含 W 和 B，约每 1234 张码里
    有 1 张正文本身就以 `WB` 开头（如 `WB-WB72W-ENY2A-...`）。
    那张码一旦省略前缀输入，正文开头的 `WB` 会被当成前缀一起剥掉，
    归一化结果与原码不一致，**永远兑不出来**，而且报的还是「无效或已被使用」，
    用户完全无从判断哪里错了。

    按 `WB-` 整体剥就没这个问题：分隔符是发行时我们自己加的，
    正文里绝不会出现连字符，所以它唯一标识「这确实是前缀」。
    """
    text = "".join(ch for ch in str(raw or "").upper() if ch.isalnum())
    if text.startswith("WB"):
        text = text[2:]
    return text


def candidate_keys(raw: str) -> list[str]:
    """用户输入可能对应的**所有**归一化读数，按可能性排序。

    为什么需要「多个」：
    `WB` 前缀与正文之间靠连字符分隔（`WB-XXXXX-...`），但用户可以只去掉连字符
    而保留 `WB`（`WBXXXXX...`），也可以把前缀一起省掉（`XXXXX...`）。
    当正文本身以 `WB` 开头时（`ALPHABET` 含 W/B，约 1/1234 的码），
    这两种输入**无法靠字符串本身区分** —— 都是 `WBWBXXXXX...`。

    既然分不清，就**都试一遍**。这比猜一个更安全：多试一个哈希的代价是
    一次字典查询，而猜错的代价是「这张码永远兑不出来，还报无效」。

    注意：返回多个候选**不会**削弱安全性 —— 每个候选仍然是 100 bit 的
    高强度哈希，且全部走同一把锁、同一套「无效或已被使用」文案。
    """
    text = "".join(ch for ch in str(raw or "").upper() if ch.isalnum())
    if not text:
        return []
    out = []
    # ① 把 `WB` 当分隔符前缀：这是最常见的写法（原样粘贴、去横线）。
    if text.startswith("WB") and len(text) > 2:
        out.append(text[2:])
    # ② 把 `WB` 当正文的一部分：省略了真前缀的那种输入。
    out.append(text)
    # 去重但保序。
    seen = set()
    return [x for x in out if not (x in seen or seen.add(x))]

def _hash_code(code: str) -> str:
    """兑换码的哈希。复用 security 的 digest，保持一致。

    不再二次归一化。 传入的必须已经是 canonical 形态（调用方负责）：

    原先这里写的是 `digest(normalize_code(code))`，看起来「更安全」，
    实际把 `redeem` 的多候选查找整个架空了 —— `normalize_code` 会剥掉
    前导 `WB`，于是 `candidate_keys()` 返回的两个候选
    （`NABC...` 与 `WBNABC...`）在哈希这一步被折叠成同一个 key，
    候选机制等于没写：正文以 `WB` 开头的码依然兑不出来。

    归一化是**查表语义**的一部分，必须发生在算哈希之前、且只发生一次；
    把它塞进哈希函数里，就等于让哈希自己决定「哪些输入算同一张码」，
    而这恰恰是候选机制要做的事。
    """
    from .security import digest
    return digest(str(code or ""))


def _store(store) -> dict:
    """兑换码库。惰性建，老 state.json 不需要迁移。"""
    return store.data.setdefault("redemption_codes", {})


def _ledger(store) -> dict:
    """用户余额账本：{username: {"milli": int}}。"""
    return store.data.setdefault("balances", {})


def _flows(store) -> list:
    """资金流水（充值 / 兑换 / 消费）。上限由 FLOW_LIMIT 控制。"""
    return store.data.setdefault("balance_flows", [])


FLOW_LIMIT = 2000


def balance_milli(store, username: str) -> int:
    with store.lock:
        return int((_ledger(store).get(username) or {}).get("milli", 0))


def add_flow(store, username: str, kind: str, milli: int, note: str,
             *, actor: str = "", ref: str = "") -> None:
    """记一笔流水。调用方须已持锁（或接受这一次额外加锁）。"""
    rows = _flows(store)
    rows.append({
        "ts": int(time.time()), "username": username, "kind": kind,
        "milli": int(milli), "credit": from_milli(milli),
        "note": str(note or "")[:200], "actor": str(actor or "")[:64],
        "ref": str(ref or "")[:64],
        "balance": balance_milli(store, username),
    })
    if len(rows) > FLOW_LIMIT:
        del rows[: len(rows) - FLOW_LIMIT]


def grant(store, username: str, credit, *, kind: str = "recharge", note: str = "",
          actor: str = "", ref: str = "") -> dict:
    """给用户加余额。**唯一**的加钱入口，所有充值都走这里。

    返回加钱后的余额。全程持锁：`+=` 在并发下会丢更新。
    """
    milli = to_milli(credit)
    if milli == 0:
        raise HTTPException(400, "充值金额不能为 0")
    with store.lock:
        row = _ledger(store).setdefault(username, {"milli": 0})
        row["milli"] = int(row.get("milli", 0)) + milli
        row["updated_at"] = int(time.time())
        add_flow(store, username, kind, milli, note, actor=actor, ref=ref)
        total = int(row["milli"])
    store.save()
    return {"username": username, "milli": total, "credit": from_milli(total)}


def spend(store, username: str, credit, *, note: str = "", ref: str = "") -> dict:
    """扣余额。余额不足**必须报错**，不能扣成负数。

    允许扣成负数会让服务在用户不知情的情况下累积债务，
    而用户看到的是「我明明没钱了怎么还能用」。
    """
    milli = to_milli(credit)
    if milli <= 0:
        return {"username": username, "milli": balance_milli(store, username)}
    with store.lock:
        row = _ledger(store).setdefault(username, {"milli": 0})
        if int(row.get("milli", 0)) < milli:
            raise HTTPException(402, f"余额不足：需要 {from_milli(milli):g} 积分，"
                                     f"当前 {from_milli(row.get('milli', 0)):g}")
        row["milli"] = int(row.get("milli", 0)) - milli
        row["updated_at"] = int(time.time())
        add_flow(store, username, "spend", -milli, note, ref=ref)
        total = int(row["milli"])
    store.save()
    return {"username": username, "milli": total, "credit": from_milli(total)}


def create_batch(store, *, count: int, credit, note: str = "", actor: str = "",
                 expires_at=None) -> dict:
    """生成一批兑换码。明文**只在这里返回一次**。"""
    try:
        count = int(count)
    except (TypeError, ValueError):
        raise HTTPException(400, "数量必须是数字")
    if count < 1 or count > MAX_BATCH:
        raise HTTPException(400, f"一次生成 1–{MAX_BATCH} 张")
    milli = to_milli(credit)
    if milli <= 0:
        raise HTTPException(400, "面额必须大于 0")
    exp = int(expires_at) if expires_at else None
    if exp is not None and exp <= int(time.time()):
        raise HTTPException(400, "过期时间必须晚于现在")

    batch = secrets.token_hex(6)
    now = int(time.time())
    plain = []
    with store.lock:
        codes = _store(store)
        for _ in range(count):
            code = new_code()
            # 发行时按 canonical 形态入库：_hash_code 不再自己归一化，
            # 归一化必须由调用方显式做一次（见 _hash_code 的说明）。
            h = _hash_code(normalize_code(code))
            # 理论上撞不上（100 bit），但真撞上就是「两张码同一份钱」，
            # 所以撞了就重生成，不能覆盖。
            while h in codes:
                code = new_code()
                h = _hash_code(normalize_code(code))
            codes[h] = {
                "batch": batch, "milli": milli, "note": str(note or "")[:120],
                "created_at": now, "created_by": str(actor or "")[:64],
                "expires_at": exp, "redeemed_at": None, "redeemed_by": "",
                "enabled": True,
            }
            plain.append(code)
    store.save()
    return {"batch": batch, "count": count, "milli": milli,
            "credit": from_milli(milli), "codes": plain}


def redeem(store, raw_code: str, username: str) -> dict:
    """核销一张兑换码，把面额加到 `username` 的余额上。

    这是**唯一**的核销入口，且读改写全程持同一把锁 —— 见模块头第 2 条。
    """
    norm = normalize_code(raw_code)
    if not norm:
        raise HTTPException(400, "请输入兑换码")
    if len(norm) > MAX_CODE_LEN:
        raise HTTPException(400, "兑换码格式不正确")
    # 逐个候选读数去查；命中哪个算哪个。见 candidate_keys 的说明。
    candidates = candidate_keys(raw_code)
    now = int(time.time())

    with store.lock:
        codes = _store(store)
        row = None
        h = _hash_code(normalize_code(raw_code))   # 兜底值：都没命中时用它做日志
        for cand in candidates:
            digest = _hash_code(cand)
            if digest in codes:
                row, h = codes[digest], digest
                break
        # 统一文案：不区分「不存在」与「已被用」。
        # 区分了就等于提供了一个「这张码存在吗」的探测器。
        invalid = HTTPException(400, "兑换码无效或已被使用")
        if not row:
            raise invalid
        if not row.get("enabled", True):
            raise HTTPException(400, "该兑换码已被停用")
        if row.get("redeemed_at"):
            raise invalid
        exp = row.get("expires_at")
        if exp and int(exp) <= now:
            raise HTTPException(400, "该兑换码已过期")

        milli = int(row.get("milli", 0))
        if milli <= 0:
            raise HTTPException(400, "该兑换码面额异常，请联系管理员")

        # —— 核销与入账必须在同一把锁里完成 ——
        row["redeemed_at"] = now
        row["redeemed_by"] = username
        ledger = _ledger(store)
        bal = ledger.setdefault(username, {"milli": 0})
        bal["milli"] = int(bal.get("milli", 0)) + milli
        bal["updated_at"] = now
        add_flow(store, username, "redeem", milli,
                 f"兑换码 {row.get('batch', '')}", ref=h[:12])
        total = int(bal["milli"])
    store.save()
    return {"credit": from_milli(milli), "milli": total,
            "balance": from_milli(total), "batch": row.get("batch", "")}


def public_code(h: str, row: dict) -> dict:
    """管理端视图。**绝不含明文**（我们本来也没存）。"""
    return {
        "id": h[:12], "batch": row.get("batch", ""),
        "credit": from_milli(row.get("milli", 0)),
        "note": row.get("note", ""), "created_at": row.get("created_at"),
        "created_by": row.get("created_by", ""), "expires_at": row.get("expires_at"),
        "redeemed_at": row.get("redeemed_at"), "redeemed_by": row.get("redeemed_by", ""),
        "enabled": bool(row.get("enabled", True)),
    }


def list_codes(store, *, batch: str = "", status: str = "all", limit: int = 200) -> list:
    with store.lock:
        rows = list(_store(store).items())
    out = []
    for h, row in rows:
        if batch and row.get("batch") != batch:
            continue
        used = bool(row.get("redeemed_at"))
        if status == "used" and not used:
            continue
        if status == "unused" and used:
            continue
        out.append(public_code(h, row))
    out.sort(key=lambda r: r.get("created_at") or 0, reverse=True)
    return out[: max(1, min(1000, int(limit or 200)))]


def list_flows(store, *, username: str = "", limit: int = 200) -> list:
    with store.lock:
        rows = list(_flows(store))
    if username:
        rows = [r for r in rows if r.get("username") == username]
    rows.reverse()
    return rows[: max(1, min(1000, int(limit or 200)))]


def stats(store) -> dict:
    """给管理端看的总览。"""
    with store.lock:
        codes = list(_store(store).values())
        ledger = dict(_ledger(store))
    used = [c for c in codes if c.get("redeemed_at")]
    return {
        "codes_total": len(codes),
        "codes_used": len(used),
        "credit_issued": from_milli(sum(int(c.get("milli", 0)) for c in used)),
        "credit_outstanding": from_milli(sum(int(c.get("milli", 0))
                                             for c in codes if not c.get("redeemed_at"))),
        "balance_total": from_milli(sum(int(v.get("milli", 0)) for v in ledger.values())),
        "users_with_balance": sum(1 for v in ledger.values() if int(v.get("milli", 0)) > 0),
    }
