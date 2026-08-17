#!/usr/bin/env python3
"""Resolve project/service alias to a standard task preamble for dispatching.

Usage:
    python3 scripts/resolve-project.py <别名或项目/服务名>
    python3 scripts/resolve-project.py <项目名> <服务别名>

Output: a ready-to-paste task preamble block (path + mandatory AGENTS.md
reading instruction). Exit code 1 with candidates when ambiguous/unknown.
"""

import json
import os
import sys

WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECTS_FILE = os.path.join(WORKSPACE, "projects.json")


def load_projects():
    with open(PROJECTS_FILE) as f:
        return json.load(f)["projects"]


def match_project(projects, name):
    name_l = name.lower()
    for pid, p in projects.items():
        if pid.lower() == name_l or name_l in [a.lower() for a in p.get("aliases", [])]:
            return pid, p
    return None, None


def match_service(project, name):
    """Match priority: exact id/alias > prefix/substring. Exact always wins."""
    name_l = name.lower()
    exact = []
    partial = []
    for sid, s in project.get("services", {}).items():
        if sid.lower() == name_l or name_l in [a.lower() for a in s.get("aliases", [])]:
            exact.append((sid, s))
            continue
        # partial match as fallback
        if name_l in sid.lower() or any(name_l in a.lower() for a in s.get("aliases", [])):
            partial.append((sid, s))
    return exact if exact else partial


def build_preamble(pid, project, sid, service):
    lines = [
        f"项目：{pid} · 服务：{sid}",
        f"服务目录：{service['path']}",
    ]
    pam = project.get("project_agents_md")
    if pam:
        lines.append(
            f"必读指令：开工第一步 read `{pam}`，遵守其中编译方案与项目约定，"
            "尤其是编译/部署相关任务；工作目录保持在服务目录内。"
        )
    svc_agents = os.path.join(service["path"], "AGENTS.md")
    if os.path.exists(svc_agents):
        lines.append(f"服务级约定：`{svc_agents}` 也存在，一并阅读。")
    if service.get("note"):
        lines.append(f"备注：{service['note']}")
    return "\n".join(lines)


def main():
    args = sys.argv[1:]
    if not args:
        print("用法: resolve-project.py <项目别名> [服务别名]", file=sys.stderr)
        sys.exit(2)

    projects = load_projects()

    pid, project = match_project(projects, args[0])
    if project is None:
        print(f"未找到项目: {args[0]}", file=sys.stderr)
        print("可用项目: " + ", ".join(projects.keys()), file=sys.stderr)
        sys.exit(1)

    if len(args) == 1:
        # no service given -> use default service
        sid = project.get("current_default_service")
        service = project["services"].get(sid)
        if service is None:
            print(f"项目 {pid} 无默认服务", file=sys.stderr)
            sys.exit(1)
        print(f"[默认服务] {sid}\n")
        print(build_preamble(pid, project, sid, service))
        return

    hits = match_service(project, args[1])
    if len(hits) == 0:
        print(f"项目 {pid} 下未找到服务: {args[1]}", file=sys.stderr)
        print("可用服务: " + ", ".join(project["services"].keys()), file=sys.stderr)
        sys.exit(1)
    if len(hits) > 1:
        print("歧义，命中多个服务: " + ", ".join(h[0] for h in hits), file=sys.stderr)
        sys.exit(1)

    sid, service = hits[0]
    print(build_preamble(pid, project, sid, service))


if __name__ == "__main__":
    main()
