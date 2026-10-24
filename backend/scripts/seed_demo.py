"""客服场景演示数据（阶段 6.3）：部门、账号、客服知识库（部门共享），可选构建图谱、预先问几个问题。

全部通过 HTTP 接口完成（和浏览器走同一条路径：登录、Cookie、CSRF、权限），所以它本身也是一次端到端检查。
可以重复执行：已存在的部门、账号、知识库、文档会跳过。

用法（服务已启动；管理员账号用 python -m agentnexus.cli create-user 创建）：
  ADMIN_PASSWORD=... DEMO_PASSWORD=... python backend/scripts/seed_demo.py [--base http://localhost:8080] [--graph] [--ask]
  --graph：构建知识图谱（真实模型下会花钱，先打印预估费用）
  --ask：用客服专员账号问几个问题（真实模型下会花钱，每个约 $0.0005）
不带这两个参数时不调用模型。
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx

DATA = json.loads((Path(__file__).resolve().parents[1] / "eval" / "retrieval_zh_v0.json").read_text())

# 30 条政策按主题合成 5 篇文档（带标题，入库时按标题切块），再加一篇工单流程
GROUPS = {
    "售后政策.md": ("售后政策", ["return_policy", "refund_timing", "exchange_policy", "warranty", "damaged_package", "price_protection"]),
    "物流与发票.md": ("物流与发票", ["logistics_tracking", "overseas_shipping", "presale_shipping", "change_address", "store_pickup",
                                 "invoice_normal", "invoice_special"]),
    "账号与安全.md": ("账号与安全", ["password_reset", "two_factor", "account_deletion", "change_phone", "privacy_request", "data_export"]),
    "会员与优惠.md": ("会员与优惠", ["subscription_renewal", "cancel_subscription", "coupon_rules", "points_rules", "gift_card",
                                 "student_discount", "installment"]),
    "服务说明.md": ("服务说明", ["service_hours", "complaint", "enterprise_purchase", "api_rate_limit"]),
}

TICKET_SOP = """# 客服工单处理流程

## 工单分级
- P1（紧急）：资金损失、账号被盗、大面积无法下单。15 分钟内响应，2 小时内给出处理方案。
- P2（重要）：单个订单的退款、物流异常、发票错误。2 小时内响应，1 个工作日内处理完毕。
- P3（一般）：咨询类问题、功能建议。1 个工作日内响应。

## 处理步骤
1. 核实身份：确认下单手机号或账号，涉及资金时需要二次验证。
2. 查询依据：先在客服知识库中检索相关政策，回复时注明依据的条款。
3. 处理与记录：在工单里记录处理结论和依据；需要其他部门协助时转派，并告知用户预计时间。
4. 回访：P1、P2 工单处理完成后 24 小时内回访，确认问题已解决。

## 升级规则
- 用户对处理结果不满意，或同一问题第二次来电，升级给客服主管。
- 涉及政策之外的补偿，需要客服主管审批。
"""

DEPARTMENTS = ["客服部", "研发部"]
USERS = [  # (用户名, 角色, 部门)
    ("kefu_zhang", "member", "客服部"),
    ("kefu_li", "manager", "客服部"),
    ("dev_wang", "member", "研发部"),
]
KB_OWNER, KB_NAME = "kefu_li", "客服知识库"
QUESTIONS = ["买的衣服不合适，签收第 5 天了还能退吗？", "退款多久能到账？", "我要开增值税专用发票，需要提供什么？"]


def documents() -> dict[str, str]:
    docs = {}
    for filename, (title, ids) in GROUPS.items():
        parts = [f"# {title}"]
        for doc_id in ids:
            text = DATA["docs"][doc_id]
            heading, _, body = text.partition("：")
            parts.append(f"## {heading}\n\n{body or text}")
        docs[filename] = "\n\n".join(parts) + "\n"
    docs["客服工单处理流程.md"] = TICKET_SOP
    return docs


class Client:
    def __init__(self, base: str, name: str, password: str):
        # 带 Origin：和浏览器一样经过 CSRF 检查
        self.http = httpx.Client(base_url=base, timeout=60, headers={"Origin": base})
        r = self.http.post("/api/auth/login", json={"name": name, "password": password})
        if r.status_code != 200:
            sys.exit(f"login as {name} failed: {r.status_code} {r.text}")
        self.me = r.json()

    def get(self, path, **kw):
        r = self.http.get(path, **kw)
        r.raise_for_status()
        return r.json()

    def post(self, path, ok=(200, 201, 202), **kw):
        r = self.http.post(path, **kw)
        if r.status_code not in ok:
            raise RuntimeError(f"POST {path} -> {r.status_code} {r.text}")
        return r.json() if r.content else None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8080")
    parser.add_argument("--graph", action="store_true")
    parser.add_argument("--ask", action="store_true")
    args = parser.parse_args()
    admin_pw, demo_pw = os.environ.get("ADMIN_PASSWORD"), os.environ.get("DEMO_PASSWORD")
    if not admin_pw or not demo_pw:
        sys.exit("set ADMIN_PASSWORD and DEMO_PASSWORD")

    admin = Client(args.base, os.environ.get("ADMIN_NAME", "admin"), admin_pw)
    depts = {d["name"]: d["id"] for d in admin.get("/api/departments")}
    for name in DEPARTMENTS:
        if name not in depts:
            depts[name] = admin.post("/api/admin/departments", json={"name": name})["id"]
    existing = {u["name"] for u in admin.get("/api/admin/users")}
    for name, role, dept in USERS:
        if name not in existing:
            admin.post("/api/admin/users", json={"name": name, "password": demo_pw, "role": role, "department_id": depts[dept]})
    print(f"部门 {DEPARTMENTS}，账号 {[u[0] for u in USERS]}（密码为 DEMO_PASSWORD）")

    owner = Client(args.base, KB_OWNER, demo_pw)
    kb = next((k for k in owner.get("/api/knowledge-bases") if k["name"] == KB_NAME and k["user_id"] == owner.me["id"]), None)
    if kb is None:
        kb = owner.post("/api/knowledge-bases", json={"name": KB_NAME, "description": "售后、物流、发票、账号、会员等客服政策"})
    have = {d["filename"] for d in owner.get(f"/api/knowledge-bases/{kb['id']}/documents")}
    for filename, text in documents().items():
        if filename not in have:
            owner.post(f"/api/knowledge-bases/{kb['id']}/documents", files={"file": (filename, text.encode())})
    if kb["visibility"] != "department":
        r = owner.http.patch(f"/api/knowledge-bases/{kb['id']}", json={"visibility": "department"})
        r.raise_for_status()
    while any(d["status"] in ("pending", "processing") for d in owner.get(f"/api/knowledge-bases/{kb['id']}/documents")):
        time.sleep(1)
    docs = owner.get(f"/api/knowledge-bases/{kb['id']}/documents")
    print(f"知识库「{KB_NAME}」：{len(docs)} 篇文档，"
          f"{sum(d['status'] == 'ready' for d in docs)} 篇可检索，已共享给客服部（只读）")

    if args.graph:
        est = owner.get(f"/api/knowledge-bases/{kb['id']}/graph/estimate")
        print(f"图谱：需调用模型 {est['extract_count']} 次，预估 ${est['estimated_cost_usd']}")
        if est["extract_count"] or not owner.get(f"/api/knowledge-bases/{kb['id']}/graph/builds"):
            owner.post(f"/api/knowledge-bases/{kb['id']}/graph/builds", ok=(201, 202, 409))
            while (b := owner.get(f"/api/knowledge-bases/{kb['id']}/graph/builds")[0])["status"] in ("pending", "running"):
                time.sleep(2)
            print(f"图谱构建：{b['status']}，{b.get('entity_count')} 个实体、{b.get('relation_count')} 条关系，"
                  f"实际 ${b.get('cost_usd')}")

    if args.ask:
        agent = Client(args.base, "kefu_zhang", demo_pw)
        conv = agent.post("/api/conversations", json={"title": "客户咨询：退货与发票"})
        agent.http.put(f"/api/conversations/{conv['id']}/knowledge-bases", json={"knowledge_base_ids": [kb["id"]]})
        for q in QUESTIONS:
            run = agent.post(f"/api/conversations/{conv['id']}/messages", json={"content": q})
            while agent.get(f"/api/runs/{run['id']}")["status"] in ("queued", "running"):
                time.sleep(0.5)
            reply = agent.get(f"/api/conversations/{conv['id']}/messages")[-1]
            cited = [c["filename"] for c in reply.get("citations") or [] if c["cited"]]
            print(f"问：{q}\n  答：{reply['content'][:80]}…\n  引用：{cited}")


if __name__ == "__main__":
    main()
