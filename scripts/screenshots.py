"""生成 README 截图（阶段 6.3）。用本机 Chrome（Playwright 的 channel="chrome"，不额外下载浏览器）。

前提：服务已启动，并执行过 backend/scripts/seed_demo.py --graph --ask（客服知识库、图谱和几轮问答）。
用法：DEMO_PASSWORD=... python scripts/screenshots.py [--base http://localhost:8080] [--research]
  --research：新建一个调研对话截取“确认计划”卡片（真实模型下会调用一次模型制定计划，约 $0.0005），截完取消
输出：docs/images/*.png
"""

import argparse
import os
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

OUT = Path(__file__).resolve().parents[1] / "docs" / "images"
SIZE = {"width": 1440, "height": 900}


def login(page: Page, base: str, name: str, password: str) -> None:
    # 走接口登录：会话 Cookie 存进浏览器上下文，之后打开页面就是已登录状态
    r = page.request.post(f"{base}/api/auth/login", data={"name": name, "password": password}, headers={"Origin": base})
    assert r.ok, (name, r.status, r.text())
    page.goto(base)
    page.wait_for_load_state("networkidle")


def nav(page: Page, label: str) -> None:
    page.get_by_role("button", name=label, exact=True).click()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(600)


def shot(page: Page, name: str) -> None:
    page.wait_for_timeout(500)  # 等过渡动画结束（截到主题切换的过渡帧踩过坑）
    page.screenshot(path=str(OUT / f"{name}.png"))
    print("saved", OUT / f"{name}.png")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8080")
    parser.add_argument("--research", action="store_true")
    args = parser.parse_args()
    pw, base = os.environ["DEMO_PASSWORD"], args.base
    OUT.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)

        def context(scheme="light"):
            return browser.new_context(viewport=SIZE, color_scheme=scheme, locale="zh-CN")

        # 1. 登录页
        page = context().new_page()
        page.goto(base)
        page.wait_for_load_state("networkidle")
        shot(page, "login")

        # 2. 客服专员：带引用的问答
        page = context().new_page()
        login(page, base, "kefu_zhang", pw)
        page.get_by_text("客户咨询：退货与发票").first.click()
        page.wait_for_load_state("networkidle")
        page.locator(".chat .scroll").evaluate("el => el.scrollTo(0, 0)")
        page.locator(".source").first.hover()  # 悬停第一条引用，显示原文
        page.wait_for_timeout(500)
        shot(page, "chat")

        # 3. 深度调研：确认计划（可选，会调用一次模型）
        if args.research:
            page.get_by_role("button", name="深度调研").first.click()
            page.wait_for_load_state("networkidle")
            box = page.get_by_placeholder("描述调研任务", exact=False)
            box.fill("整理客服知识库里关于退货、退款和发票的规则，写成一份新员工培训材料")
            box.press("Enter")
            page.get_by_text("请确认调研计划").wait_for(timeout=60_000)
            page.mouse.move(900, 860)  # 移开鼠标：否则“深度调研”按钮的提示框会留在截图里
            shot(page, "research-plan")
            page.get_by_role("button", name="取消", exact=True).click()  # 不执行，避免继续花费
            page.wait_for_timeout(1000)

        # 4. 知识库（所有者视角）与知识图谱
        page = context().new_page()
        login(page, base, "kefu_li", pw)
        nav(page, "知识库")
        page.get_by_role("button", name="客服知识库").click()
        page.wait_for_load_state("networkidle")
        shot(page, "knowledge")
        page.get_by_role("tab", name="知识图谱").click()
        page.wait_for_timeout(3000)  # 等布局稳定
        shot(page, "graph")

        # 5. 用量看板（管理员、全站、深色模式）
        page = context("dark").new_page()
        login(page, base, "admin", pw)
        nav(page, "用量")
        page.get_by_text("全站", exact=True).click()
        page.wait_for_timeout(1500)
        shot(page, "usage-dark")

        # 6. 调研报告（研发部 bob 之前生成的报告：来源列表与下载卡片）
        page = context().new_page()
        login(page, base, "bob", pw)
        page.locator(".list-panel .item").filter(has_text="Redis").first.click()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(800)
        shot(page, "research-report")

        browser.close()


if __name__ == "__main__":
    main()
