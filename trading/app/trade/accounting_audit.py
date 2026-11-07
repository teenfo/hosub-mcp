"""Offline, evidence-scoped historical cost repair. Never submits orders."""
import json
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo
from . import ledger

REVISION = "2026-11-07-history-v1"

def plan(positions, fills, orders):
    by_id = {p["id"]: p for p in positions}
    links = {}
    for order in orders:
        try:
            day = datetime.fromisoformat(order["created"]).astimezone(ZoneInfo("Asia/Seoul")).date().isoformat()
            no = json.loads(order.get("result") or "{}").get("ord_no")
            if no and order.get("link_pos"):
                key = (day, str(no))
                links.setdefault(key, set()).add(order["link_pos"])
        except (ValueError, TypeError):
            continue
    groups = defaultdict(list)
    for f in fills:
        groups[(f["env"], f["d"], f["code"], f["side"], f["ord_no"])].append(f)
    patches, unresolved = [], []
    for p in positions:
        if p["status"] != "closed":
            continue
        sym = p.get("exec_symbol") or p["symbol"]
        # Substituted short signals use a different cost basis; do not mix them.
        if sym != p["symbol"] or p["side"] != "long":
            unresolved.append({"id": p["id"], "reason": "unsupported_basis"})
            continue
        day = (p.get("closed") or "")[:10]
        buys = groups.get((p["env"], p["opened"][:10], sym, "buy", p.get("ord_no")), [])
        sells = []
        for (env, fd, code, side, no), rows in groups.items():
            if (env, fd, code, side) != (p["env"], day, sym, "sell"):
                continue
            owner = links.get((fd, no))
            if owner == {p["id"]}:
                sells.extend(rows)
            elif owner is None and no == p.get("exit_ord_no"):
                # An unowned exit number must not be claimed by two position rows.
                claimants = [x for x in positions if x["status"] == "closed"
                             and x["env"] == env and (x.get("closed") or "")[:10] == fd
                             and x.get("exit_ord_no") == no]
                if len(claimants) == 1:
                    sells.extend(rows)
        bq, sq = sum(f["qty"] for f in buys), sum(f["qty"] for f in sells)
        owner = links.get((day, p.get("exit_ord_no")), set())
        patch, reason = {}, []
        # A synthetic residual that reused another position's sell order has no own
        # realized trade. Only void it if that owner has matching broker evidence.
        if p["rule"] == "residual" and not sells and len(owner) == 1 and p["id"] not in owner:
            root = by_id.get(next(iter(owner)))
            evidence = groups.get((p["env"], day, sym, "sell", p.get("exit_ord_no")), [])
            if root and root["status"] == "closed" and root["env"] == p["env"] and evidence and (root.get("exec_symbol") or root["symbol"]) == sym:
                patch = {"status": "void", "exit_reason": "accounting_duplicate"}
                reason = ["sell_order_owned_by:" + root["id"]]
        else:
            if buys and bq == p["qty"] and p.get("entry_fee_krw") is None:
                claimants = [x for x in positions if x["status"] != "void" and
                             x["env"] == p["env"] and x["opened"][:10] == p["opened"][:10]
                             and x.get("ord_no") == p.get("ord_no")]
                if len(claimants) == 1:
                    patch.update(entry_fee_krw=sum(f["commission"] for f in buys),
                                 entry=sum(f["qty"]*f["price"] for f in buys)/bq,
                                 fill_confirmed=1)
                    reason.append("matching_buy_order")
            recorded = p["exit_qty"] if p.get("exit_qty") is not None else p["qty"]
            incomplete = (p.get("exit_fee_krw") is None or p.get("tax_krw") is None
                          or not p.get("exit_fill_confirmed"))
            if sells and 0 < sq <= p["qty"] and (incomplete or sq >= recorded):
                values = dict(exit_fee_krw=sum(f["commission"] for f in sells),
                              tax_krw=sum(f["tax"] for f in sells),
                              exit=sum(f["qty"]*f["price"] for f in sells)/sq,
                              exit_qty=sq if sq < p["qty"] else None,
                              exit_fill_confirmed=1)
                changes = {k:v for k,v in values.items() if p.get(k) != v}
                if changes:
                    patch.update(changes)
                    reason.append("owned_sell_orders")
        if patch:
            patches.append({"id":p["id"], "patch":patch, "reason":reason})
        elif not all(p.get(k) is not None for k in ("entry_fee_krw","exit_fee_krw","tax_krw")):
            unresolved.append({"id":p["id"], "reason":"insufficient_or_conflicting_evidence",
                               "buy_qty":bq,"sell_qty":sq})
    return {"patches": patches, "unresolved": unresolved}

def run(apply=False):
    with ledger._conn() as conn:
        positions = [dict(r) for r in conn.execute("SELECT * FROM positions")]
        fills = [dict(r) for r in conn.execute("SELECT * FROM broker_fills")]
        orders = [dict(r) for r in conn.execute(
            "SELECT * FROM orders WHERE kind='exit' AND status='sent'")]
        report = plan(positions, fills, orders)
        if not apply:
            return report
        conn.execute("CREATE TABLE IF NOT EXISTS accounting_repairs "
                     "(revision TEXT, position_id TEXT, before_json TEXT, after_json TEXT, "
                     "reason TEXT, PRIMARY KEY(revision,position_id))")
        by_id = {p["id"]:p for p in positions}
        for item in report["patches"]:
            patch = item["patch"]
            conn.execute("INSERT OR IGNORE INTO accounting_repairs VALUES (?,?,?,?,?)",
                         (REVISION,item["id"],json.dumps(by_id[item["id"]]),json.dumps(patch),
                          json.dumps(item["reason"])))
            conn.execute("UPDATE positions SET "+",".join(k+"=?" for k in patch)+" WHERE id=?",
                         [*patch.values(),item["id"]])
        report["linked"] = sum(ledger.link_residual_parent(conn,r[0]) for r in
            conn.execute("SELECT id FROM positions WHERE rule='residual' AND status!='void' ORDER BY opened").fetchall())
    report["recalculated"] = ledger.apply_actual_costs()
    return report

def daily_evidence(day, closed, broker, fills):
    """Explain differences without inventing per-trade allocations or causes."""
    if broker is None or any(broker.get(k) is None for k in ("realized","commission","tax")):
        return {"status":"broker_unavailable"}
    envs = {p.get("env") for p in closed if p.get("env")}
    if len(envs) > 1 or (envs and broker.get("env") not in envs):
        return {"status":"mixed_environment"}
    rows = [f for f in fills if f.get("env") == broker.get("env")]
    fee = sum(f["commission"] for f in rows)
    tax = sum(f["tax"] for f in rows)
    a = {"status":"available", "broker_commission_krw":broker["commission"],
         "stored_fill_commission_krw":fee,
         "commission_difference_krw":broker["commission"]-fee,
         "broker_tax_krw":broker["tax"],"stored_fill_tax_krw":tax,
         "tax_difference_krw":broker["tax"]-tax,
         "detail_status":"broker_detail_required"}
    # Only a same-day flat, fully matched inventory permits a gross cash comparison.
    balances = defaultdict(int)
    for f in rows:
        balances[f["code"]] += f["qty"]*(1 if f["side"]=="buy" else -1)
    complete = bool(rows and closed) and all(
        p["opened"][:10] == day and p.get("costs",{}).get("cost_status")=="actual"
        for p in closed) and all(q==0 for q in balances.values())
    if complete:
        for code in balances:
            sold = sum(f["qty"] for f in rows if f["code"]==code and f["side"]=="sell")
            recorded = sum((p["exit_qty"] if p["exit_qty"] is not None else p["qty"])
                           for p in closed if (p.get("exec_symbol") or p["symbol"])==code)
            if sold != recorded:
                complete = False
    if complete:
        gross = sum(p["costs"]["gross_pnl_krw"] for p in closed)
        ledger_fee = sum(p["costs"]["commission_krw"] for p in closed)
        ledger_tax = sum(p["costs"]["tax_krw"] for p in closed)
        broker_gross = broker["realized"]+broker["commission"]+broker["tax"]
        a.update(comparison_scope="same_day_flat", broker_implied_gross_krw=broker_gross,
                 ledger_gross_krw=round(gross,2),
                 gross_basis_difference_krw=round(broker_gross-gross,2),
                 realized_commission_difference_krw=round(broker["commission"]-ledger_fee,2),
                 realized_tax_difference_krw=round(broker["tax"]-ledger_tax,2),
                 net_difference_krw=round(broker["realized"]-(gross-ledger_fee-ledger_tax),2))
    return a
