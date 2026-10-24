"""依赖漏洞扫描（decisions/0025）：对三个锁文件跑 pip-audit，按 audit-allowlist.json 判断。

失败的情况：
- 发现不在白名单里的漏洞；
- 白名单条目过了 review_by（修复版本已满两周，该升级了）。
白名单里的包已经没有漏洞时只提示（该删条目了），不失败。
用法：python scripts/audit_deps.py [--today 2026-10-25]（需要 pip-audit）
"""

import argparse
import datetime
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCKS = ["backend/requirements.lock", "docker/sandbox/requirements.lock", "docker/mcp-gateway/requirements.lock"]


def audit(lock: Path) -> dict[str, list[str]]:
    out = subprocess.run(
        [sys.executable, "-m", "pip_audit", "-r", str(lock), "--no-deps", "--disable-pip", "--progress-spinner", "off",
         "-f", "json"], capture_output=True, text=True)
    if not out.stdout.strip():
        sys.exit(f"pip-audit failed for {lock}:\n{out.stderr}")
    deps = json.loads(out.stdout)["dependencies"]
    return {d["name"].lower(): [v["id"] for v in d["vulns"]] for d in deps if d.get("vulns")}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--today", default=datetime.date.today().isoformat())
    today = datetime.date.fromisoformat(parser.parse_args().today)
    allow = {k: v for k, v in json.loads((ROOT / "scripts" / "audit-allowlist.json").read_text()).items() if not k.startswith("_")}
    failed, seen = False, set()
    for lock in LOCKS:
        for name, ids in audit(ROOT / lock).items():
            seen.add(name)
            entry = allow.get(name)
            if entry is None:
                print(f"FAIL {lock}: {name} 有 {len(ids)} 个漏洞（{', '.join(ids[:3])}…），不在白名单里")
                failed = True
            elif datetime.date.fromisoformat(entry["review_by"]) < today:
                print(f"FAIL {lock}: {name} 的例外已于 {entry['review_by']} 到期：修复版本 {entry['fixed_in']} 应该升级了")
                failed = True
            else:
                print(f"ok   {lock}: {name}（{len(ids)} 个）在白名单里，{entry['review_by']} 前复查：{entry['reason']}")
    for name in sorted(set(allow) - seen):
        print(f"note 白名单里的 {name} 已经没有漏洞，可以删除这一条")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
