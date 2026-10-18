#!/usr/bin/env python3
"""抓取 jason081112/Pre_Alpha 的进度指标，生成 data/pre-alpha.json。

数据源
  * Issues（公开仓库，任何人可读）：phase/P0..P6 标签 -> 分相进度、总进度、最近活动
  * Project Alpha #1：Status 单选字段 -> Todo / In Progress / Done 三态计数
    （用户级 Project V2 没有公开 API，必须带 project scope 的 token）

Token 读取顺序：PREALPHA_TOKEN -> GH_TOKEN -> GITHUB_TOKEN
Project 读取失败时自动降级：status 置 null，其余数据照常输出。
数据无变化时不重写文件（generated_at 保持上次真正变更的时间）。
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

OWNER = "jason081112"
REPO = "Pre_Alpha"
PROJECT_NUMBER = 1
API_URL = "https://api.github.com/graphql"

OUT_PATH = Path(__file__).resolve().parent.parent / "data" / "pre-alpha.json"

PHASES = [
    ("P0", "P0 调研验证"),
    ("P1", "P1 后端跑通"),
    ("P2", "P2 前端跑通"),
    ("P3", "P3 桥接层"),
    ("P4", "P4 交互层"),
    ("P5", "P5 隐私合规"),
    ("P6", "P6 工程基建"),
]
PHASE_PREFIX = "phase/"
STATUS_ORDER = ["Todo", "In Progress", "Done"]

TOKEN = (
    os.environ.get("PREALPHA_TOKEN")
    or os.environ.get("GH_TOKEN")
    or os.environ.get("GITHUB_TOKEN")
)


class ApiError(RuntimeError):
    """GitHub GraphQL 请求最终失败。"""


def graphql(query: str, variables: dict, retries: int = 4) -> dict:
    """执行一次 GraphQL 请求，失败按退避重试，仍失败则抛 ApiError。"""
    if not TOKEN:
        raise ApiError("缺少 token：请设置 PREALPHA_TOKEN 环境变量")

    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    request = urllib.request.Request(
        API_URL,
        data=body,
        headers={
            "Authorization": "bearer " + TOKEN,
            "Content-Type": "application/json",
            "User-Agent": "prealpha-status-snapshot",
        },
        method="POST",
    )

    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("errors"):
                raise ApiError(str(payload["errors"])[:400])
            return payload["data"]
        except (urllib.error.URLError, TimeoutError, ApiError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
    raise ApiError("GraphQL 请求失败：%s" % last_error)


ISSUES_QUERY = """
query($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    totalOpen: issues(states: OPEN) { totalCount }
    totalClosed: issues(states: CLOSED) { totalCount }
    issues(first: 100, after: $after, orderBy: {field: UPDATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        title
        state
        url
        updatedAt
        labels(first: 20) { nodes { name } }
      }
    }
  }
}
"""

PROJECT_QUERY = """
query($login: String!, $number: Int!, $after: String) {
  user(login: $login) {
    projectV2(number: $number) {
      title
      url
      items(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          fieldValues(first: 50) {
            nodes {
              ... on ProjectV2ItemFieldSingleSelectValue {
                name
                field { ... on ProjectV2SingleSelectField { name } }
              }
            }
          }
        }
      }
    }
  }
}
"""


def percent(closed: int, total: int) -> float:
    return round(closed * 100 / total, 1) if total else 0.0


def fetch_issues() -> tuple[dict, list]:
    """返回 (总计 {open, closed}, 全部 issue 节点，按 updatedAt 倒序)。"""
    nodes: list = []
    totals = {"open": 0, "closed": 0}
    after = None
    while True:
        data = graphql(ISSUES_QUERY, {"owner": OWNER, "name": REPO, "after": after})
        repo = data["repository"]
        totals = {
            "open": repo["totalOpen"]["totalCount"],
            "closed": repo["totalClosed"]["totalCount"],
        }
        connection = repo["issues"]
        nodes.extend(connection["nodes"])
        if not connection["pageInfo"]["hasNextPage"]:
            break
        after = connection["pageInfo"]["endCursor"]
    return totals, nodes


def build_phases(nodes: list) -> list:
    counts = {key: {"total": 0, "closed": 0} for key, _ in PHASES}
    for issue in nodes:
        labels = (issue.get("labels") or {}).get("nodes") or []
        for label in labels:
            name = (label or {}).get("name") or ""
            if not name.startswith(PHASE_PREFIX):
                continue
            key = name[len(PHASE_PREFIX):].split("-")[0]
            if key not in counts:
                continue
            counts[key]["total"] += 1
            if issue.get("state") == "CLOSED":
                counts[key]["closed"] += 1

    phases = []
    for key, label in PHASES:
        c = counts[key]
        phases.append({
            "key": key,
            "label": label,
            "total": c["total"],
            "closed": c["closed"],
            "percent": percent(c["closed"], c["total"]),
        })
    return phases


def fetch_project_status() -> tuple[dict | None, str | None]:
    """返回 (Status 计数, project url)。任何失败都降级为 (None, url/None)。"""
    counts: dict[str, int] = {}
    project_url: str | None = None
    after = None
    try:
        while True:
            data = graphql(PROJECT_QUERY, {"login": OWNER, "number": PROJECT_NUMBER, "after": after})
            project = (data.get("user") or {}).get("projectV2")
            if not project:
                return None, None
            project_url = project.get("url")
            connection = project["items"]
            for item in connection["nodes"]:
                field_values = (item.get("fieldValues") or {}).get("nodes") or []
                for value in field_values:
                    field = (value or {}).get("field") or {}
                    if field.get("name") == "Status" and value.get("name"):
                        counts[value["name"]] = counts.get(value["name"], 0) + 1
            if not connection["pageInfo"]["hasNextPage"]:
                break
            after = connection["pageInfo"]["endCursor"]
    except ApiError as exc:
        print("读取 Project 失败，降级处理：%s" % exc, file=sys.stderr)
        return None, project_url

    ordered = {name: counts.get(name, 0) for name in STATUS_ORDER}
    for name, value in counts.items():
        ordered.setdefault(name, value)
    return ordered, project_url


def build_recent(nodes: list, limit: int = 5) -> list:
    recent = []
    for issue in nodes[:limit]:
        recent.append({
            "number": issue["number"],
            "title": issue["title"],
            "state": "closed" if issue.get("state") == "CLOSED" else "open",
            "updated_at": issue.get("updatedAt"),
            "url": issue.get("url"),
        })
    return recent


def main() -> int:
    totals, issues = fetch_issues()

    total = totals["open"] + totals["closed"]
    closed = totals["closed"]

    status, project_url = fetch_project_status()
    if not project_url:
        project_url = "https://github.com/users/%s/projects/%d" % (OWNER, PROJECT_NUMBER)

    document = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": {
            "repo": "https://github.com/%s/%s" % (OWNER, REPO),
            "issues": "https://github.com/%s/%s/issues" % (OWNER, REPO),
            "project": project_url,
        },
        "overall": {
            "total": total,
            "closed": closed,
            "open": totals["open"],
            "percent": percent(closed, total),
        },
        "phases": build_phases(issues),
        "status": status,
        "recent": build_recent(issues),
    }

    comparable = {k: v for k, v in document.items() if k != "generated_at"}
    if OUT_PATH.exists():
        try:
            old = json.loads(OUT_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            old = {}
        if {k: old.get(k) for k in comparable} == comparable:
            print("数据无变化，保留现有快照（generated_at 不变）")
            return 0

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print("已写入 %s（%d/%d 完成，status=%s）" % (OUT_PATH, closed, total, status))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ApiError as exc:
        print("错误：%s" % exc, file=sys.stderr)
        sys.exit(1)
