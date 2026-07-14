#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build bảng điểm SU01 Team 06 → dist/index.html (static, GitHub Pages).

Nguồn dữ liệu: Jira Cloud (project KAN) + GitHub (repo team, nếu khai trong config).
Công thức: docs/onboarding/5-cong-thuc-tinh-diem.md (repo ops-agent-detective).
  - Điểm hoàn thành = size (Story Points S=1/M=3/L=5) × (1 − α)
  - α = phần Agent làm thay, đo qua author `agent-*@team06` hoặc tag [AI-Kiem-soat-task]
  - Điểm hỗ trợ = size phần giúp × 50%, trần 30% tổng điểm tuần (comment `HO-TRO:`)

Chạy: JIRA_EMAIL=... JIRA_API_TOKEN=... [GH_TOKEN=...] python3 build.py
Chỉ dùng stdlib — không cần cài dependency.
"""
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
TZ_VN = timezone(timedelta(hours=7))


def load_config():
    with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
        return json.load(f)


def api_get(url, headers, ok_404=False):
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if ok_404 and e.code == 404:
            return None
        raise


class Jira:
    def __init__(self, cfg):
        self.base = cfg["jira"]["site"].rstrip("/")
        email = os.environ.get("JIRA_EMAIL", "")
        token = os.environ.get("JIRA_API_TOKEN", "")
        if not email or not token:
            raise SystemExit("Thiếu JIRA_EMAIL / JIRA_API_TOKEN trong env")
        auth = base64.b64encode(f"{email}:{token}".encode()).decode()
        self.h = {"Authorization": "Basic " + auth, "Accept": "application/json"}

    def get(self, path, **params):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return api_get(url, self.h)

    def story_points_field(self, override):
        if override:
            return override
        for f in self.get("/rest/api/3/field"):
            if re.search(r"story point|điểm", f.get("name", ""), re.I):
                return f["id"]
        return None

    def search_issues(self, project, sp_field):
        fields = "summary,status,assignee,resolutiondate,duedate,created"
        if sp_field:
            fields += "," + sp_field
        issues, token = [], None
        while True:
            params = {"jql": f"project={project} ORDER BY created ASC",
                      "maxResults": 100, "fields": fields}
            if token:
                params["nextPageToken"] = token
            page = self.get("/rest/api/3/search/jql", **params)
            issues += page.get("issues", [])
            token = page.get("nextPageToken")
            if not token or not page.get("issues"):
                break
        return issues

    def comments(self, key):
        out, start = [], 0
        while True:
            page = self.get(f"/rest/api/3/issue/{key}/comment", startAt=start, maxResults=100)
            out += page.get("comments", [])
            start += len(page.get("comments", []))
            if start >= page.get("total", 0) or not page.get("comments"):
                break
        return out


class GitHub:
    def __init__(self):
        self.token = os.environ.get("GH_TOKEN", "")
        self.h = {"Accept": "application/vnd.github+json"}
        if self.token:
            self.h["Authorization"] = "Bearer " + self.token

    def get(self, path, ok_404=False):
        return api_get("https://api.github.com" + path, self.h, ok_404=ok_404)

    def pulls(self, repo):
        out, page = [], 1
        while True:
            batch = self.get(f"/repos/{repo}/pulls?state=all&per_page=100&page={page}") or []
            out += batch
            if len(batch) < 100:
                break
            page += 1
        return out

    def pr_commits(self, repo, number):
        out, page = [], 1
        while True:
            batch = self.get(f"/repos/{repo}/pulls/{number}/commits?per_page=100&page={page}") or []
            out += batch
            if len(batch) < 100:
                break
            page += 1
        return out

    def commit_stats(self, repo, sha):
        d = self.get(f"/repos/{repo}/commits/{sha}", ok_404=True)
        if not d:
            return 0
        return (d.get("stats") or {}).get("additions", 0) or 0


def adf_text_and_mentions(body):
    """Trích text phẳng + danh sách accountId mention từ ADF comment."""
    texts, mentions = [], []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "text":
                texts.append(node.get("text", ""))
            elif node.get("type") == "mention":
                attrs = node.get("attrs", {})
                mentions.append(attrs.get("id", ""))
                texts.append("@" + attrs.get("text", "").lstrip("@"))
            for c in node.get("content", []) or []:
                walk(c)
        elif isinstance(node, list):
            for c in node:
                walk(c)

    walk(body)
    return "".join(texts), mentions


def week_key(dt):
    y, w, _ = dt.astimezone(TZ_VN).isocalendar()
    return f"{y}-W{w:02d}"


def parse_jira_dt(s):
    if not s:
        return None
    return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(
        tzinfo=timezone(timedelta(hours=int(s[23:26] or 0))) if len(s) > 23 else TZ_VN)


def sp_to_size(v):
    return {1: "S", 3: "M", 5: "L"}.get(int(v) if v else 0)


def compute(cfg):
    jira = Jira(cfg)
    gh = GitHub()
    warnings = []

    sp_field = jira.story_points_field(cfg["jira"].get("story_points_field"))
    if not sp_field:
        warnings.append("Board KAN chưa bật field Story Points (bật Estimation trong "
                        "Project settings → Features) — task chưa có size sẽ tính 0 điểm.")

    issues = jira.search_issues(cfg["jira"]["project"], sp_field)

    # map accountId ↔ member (qua assignee của issues + user search theo email)
    members = cfg["members"]
    by_display = {m["display"]: m for m in members}
    acc2member = {}
    for m in members:
        try:
            found = jira.get("/rest/api/3/user/search", query=m["jira_email"]) or []
            for u in found:
                acc2member[u["accountId"]] = m["display"]
        except Exception:
            pass

    # gom commit theo task từ các repo GitHub khai trong config
    task_commits = {}  # KAN-xx -> list[{agent_for, additions, human_email, login}]
    for repo in cfg["github"]["repos"]:
        try:
            for pr in gh.pulls(repo):
                mkey = re.match(r"(KAN-\d+)", (pr.get("head", {}).get("ref") or "") + " " +
                                (pr.get("title") or ""), re.I)
                if not mkey:
                    continue
                key = mkey.group(1).upper()
                for c in gh.pr_commits(repo, pr["number"]):
                    commit = c.get("commit", {})
                    email = (commit.get("author") or {}).get("email", "")
                    name = (commit.get("author") or {}).get("name", "")
                    msg = commit.get("message", "")
                    additions = gh.commit_stats(repo, c["sha"])
                    entry = {"additions": max(additions, 1), "login": (c.get("author") or {}).get("login")}
                    if cfg["agent"]["supervisor_tag"] in msg:
                        entry["kind"] = "supervisor"
                    elif email == cfg["agent"]["shared_email"] or name.startswith(cfg["agent"]["author_prefix"]):
                        entry["kind"] = "agent"
                    else:
                        entry["kind"] = "human"
                        entry["email"] = email
                    task_commits.setdefault(key, []).append(entry)
        except Exception as e:
            warnings.append(f"Không đọc được repo {repo}: {e}")

    size_pts = cfg["scoring"]["size_points"]
    rows = []          # từng task Done
    weeks = {}         # week -> display -> {"done": x, "support": y}
    support_log = []

    def bucket(week, disp):
        return weeks.setdefault(week, {}).setdefault(disp, {"done": 0.0, "support": 0.0})

    for it in issues:
        f = it["fields"]
        status = (f.get("status") or {}).get("name", "")
        assignee = f.get("assignee") or {}
        disp = acc2member.get(assignee.get("accountId"), assignee.get("displayName") or "—")
        resolved = parse_jira_dt(f.get("resolutiondate"))
        sp = f.get(sp_field) if sp_field else None
        size = sp_to_size(sp)

        # điểm hỗ trợ từ comment HO-TRO (đọc trên mọi task, tính vào tuần comment)
        try:
            for cm in jira.comments(it["key"]):
                text, mentions = adf_text_and_mentions(cm.get("body") or {})
                m = re.search(r"HO-TRO:\s*@?\S[^—-]*[—-]\s*size\s*(S|M|L)", text, re.I)
                if m:
                    helper = acc2member.get(mentions[0]) if mentions else None
                    if helper:
                        s = size_pts[m.group(1).upper()] * cfg["scoring"]["support_rate"]
                        wk = week_key(parse_jira_dt(cm.get("created")))
                        bucket(wk, helper)["support"] += s
                        support_log.append({"task": it["key"], "helper": helper,
                                            "size": m.group(1).upper(), "points": s, "week": wk})
        except Exception:
            pass

        if not resolved or status.lower() not in ("done", "hoàn thành", "xong"):
            continue

        # α theo diff additions trên nhánh task (agent + giám thị / tổng)
        commits = task_commits.get(it["key"], [])
        total = sum(c["additions"] for c in commits)
        agent_part = sum(c["additions"] for c in commits if c["kind"] in ("agent", "supervisor"))
        alpha = round(agent_part / total, 3) if total else 0.0

        due = parse_jira_dt(f.get("duedate") + "T23:59:59" if f.get("duedate") else None)
        late = bool(due and resolved > due)
        pts = (size_pts.get(size, 0)) * (1 - alpha)
        wk = week_key(resolved)
        bucket(wk, disp)["done"] += pts
        rows.append({"task": it["key"], "summary": f.get("summary", ""), "assignee": disp,
                     "size": size or "?", "alpha": alpha, "late": late,
                     "points": round(pts, 2), "week": wk})

    # trần điểm hỗ trợ: support ≤ 30% tổng điểm tuần ⇒ support ≤ 3/7 × done
    cap = cfg["scoring"]["support_cap_ratio"]
    for wk, per in weeks.items():
        for disp, v in per.items():
            max_support = (cap / (1 - cap)) * v["done"]
            if v["support"] > max_support:
                v["support_raw"] = v["support"]
                v["support"] = round(max_support, 2)

    totals = {}
    for wk, per in weeks.items():
        for disp, v in per.items():
            t = totals.setdefault(disp, {"done": 0.0, "support": 0.0})
            t["done"] += v["done"]
            t["support"] += v["support"]

    return {
        "generated_at": datetime.now(TZ_VN).strftime("%d/%m/%Y %H:%M (GMT+7)"),
        "sp_field": sp_field,
        "issue_count": len(issues),
        "members": [{"display": m["display"], "role": m["role"]} for m in members],
        "rows": rows, "weeks": weeks, "totals": totals,
        "support_log": support_log, "warnings": warnings,
    }


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def render(data, cfg):
    members = data["members"]
    totals = data["totals"]
    body_rows = ""
    ranked = sorted(members, key=lambda m: -(totals.get(m["display"], {}).get("done", 0)
                                             + totals.get(m["display"], {}).get("support", 0)))
    for i, m in enumerate(ranked, 1):
        t = totals.get(m["display"], {"done": 0, "support": 0})
        total = t["done"] + t["support"]
        body_rows += (f'<tr><td class="rank">{i}</td><td class="name">{esc(m["display"])}</td>'
                      f'<td>{esc(m["role"])}</td><td class="num">{t["done"]:.2f}</td>'
                      f'<td class="num">{t["support"]:.2f}</td>'
                      f'<td class="num total">{total:.2f}</td></tr>')

    week_sections = ""
    for wk in sorted(data["weeks"], reverse=True):
        per = data["weeks"][wk]
        lines = ""
        for disp in sorted(per, key=lambda d: -(per[d]["done"] + per[d]["support"])):
            v = per[disp]
            lines += (f'<tr><td class="name">{esc(disp)}</td><td class="num">{v["done"]:.2f}</td>'
                      f'<td class="num">{v["support"]:.2f}</td>'
                      f'<td class="num total">{v["done"] + v["support"]:.2f}</td></tr>')
        week_sections += (f'<h3>Tuần {esc(wk)}</h3><table><thead><tr><th>Thành viên</th>'
                          f'<th>Điểm hoàn thành</th><th>Điểm hỗ trợ</th><th>Tổng</th></tr></thead>'
                          f'<tbody>{lines}</tbody></table>')
    if not data["weeks"]:
        week_sections = '<p class="empty">Chưa có task nào Done trên board — bảng tuần sẽ xuất hiện khi Sprint bắt đầu.</p>'

    task_rows = ""
    for r in sorted(data["rows"], key=lambda r: r["task"]):
        alpha_txt = f'Agent gánh {r["alpha"]*100:.0f}%' if r["alpha"] else "tự làm 100%"
        late = ' <span class="late">trễ</span>' if r["late"] else ""
        task_rows += (f'<tr><td>{esc(r["task"])}</td><td>{esc(r["summary"])}{late}</td>'
                      f'<td class="name">{esc(r["assignee"])}</td><td>{esc(r["size"])}</td>'
                      f'<td>{esc(alpha_txt)}</td><td class="num">{r["points"]:.2f}</td>'
                      f'<td>{esc(r["week"])}</td></tr>')
    tasks_html = (f'<table><thead><tr><th>Task</th><th>Tóm tắt</th><th>Người nhận</th><th>Size</th>'
                  f'<th>α (Agent)</th><th>Điểm</th><th>Tuần</th></tr></thead><tbody>{task_rows}</tbody></table>'
                  if task_rows else '<p class="empty">Chưa có task Done.</p>')

    warn_html = ""
    if data["warnings"]:
        warn_html = '<div class="warn"><b>Lưu ý dữ liệu:</b><ul>' + "".join(
            f"<li>{esc(w)}</li>" for w in data["warnings"]) + "</ul></div>"

    html = f"""<!DOCTYPE html>
<html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bảng điểm SU01 — Team 06</title>
<style>
  :root {{ --ink:#232F3E; --accent:#FF9900; --bg:#fafafa; --line:#e4e4e4; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; font-family:'Segoe UI',system-ui,-apple-system,sans-serif; background:var(--bg); color:var(--ink); }}
  header {{ background:var(--ink); color:#fff; padding:28px 24px 22px; }}
  header h1 {{ margin:0 0 4px; font-size:26px; }}
  header h1 span {{ color:var(--accent); }}
  header p {{ margin:0; opacity:.75; font-size:14px; }}
  main {{ max-width:960px; margin:0 auto; padding:24px 16px 60px; }}
  h2 {{ font-size:19px; border-left:4px solid var(--accent); padding-left:10px; margin:34px 0 12px; }}
  h3 {{ font-size:15px; margin:22px 0 8px; }}
  table {{ width:100%; border-collapse:collapse; background:#fff; box-shadow:0 1px 3px rgba(35,47,62,.08); font-size:14px; }}
  th {{ background:var(--ink); color:#fff; text-align:left; padding:9px 12px; font-weight:600; }}
  td {{ padding:9px 12px; border-bottom:1px solid var(--line); }}
  tr:last-child td {{ border-bottom:none; }}
  .num {{ text-align:right; font-variant-numeric:tabular-nums; }}
  .total {{ font-weight:700; }}
  .rank {{ width:36px; color:#888; }}
  .name {{ font-weight:600; }}
  .late {{ background:#c0392b; color:#fff; border-radius:3px; padding:1px 6px; font-size:11px; }}
  .empty {{ color:#777; background:#fff; border:1px dashed var(--line); padding:16px; border-radius:6px; }}
  .warn {{ background:#fff7e8; border:1px solid var(--accent); border-radius:6px; padding:10px 14px; margin:18px 0; font-size:13px; }}
  .rules {{ background:#fff; border:1px solid var(--line); border-radius:6px; padding:14px 18px; font-size:13.5px; line-height:1.55; }}
  .rules code {{ background:#f1f1f1; padding:1px 5px; border-radius:3px; }}
  footer {{ text-align:center; color:#999; font-size:12px; padding:18px; }}
</style></head><body>
<header><h1>Bảng điểm thi đua <span>SU01 — Team 06</span></h1>
<p>Ops Agent Detective · cập nhật tự động hằng đêm từ Jira + GitHub · số liệu tính đến {esc(data["generated_at"])}</p></header>
<main>
{warn_html}
<h2>Tổng điểm</h2>
<table><thead><tr><th></th><th>Thành viên</th><th>Role</th><th>Điểm hoàn thành</th><th>Điểm hỗ trợ</th><th>Tổng</th></tr></thead>
<tbody>{body_rows}</tbody></table>

<h2>Theo tuần</h2>
{week_sections}

<h2>Chi tiết task đã Done</h2>
{tasks_html}

<h2>Luật chơi (tóm tắt)</h2>
<div class="rules">
<b>Điểm hoàn thành</b> = size task (Story Points <code>S=1 · M=3 · L=5</code>) × (1 − α).<br>
<b>α</b> = phần việc Agent làm thay, đo bằng diff attribution trên nhánh task —
commit author <code>agent-*</code> / <code>agent@team06</code>, hoặc tag <code>[AI-Kiem-soat-task]</code>
(Agent giám thị làm nốt task trễ). Dùng Agent không bị âm điểm.<br>
<b>Điểm hỗ trợ</b> = size phần giúp × 50% — dấu vết máy đọc được: commit trên nhánh task người khác,
PR review, hoặc comment Jira <code>HO-TRO: @người-giúp — size S</code>; trần ≤ 30% tổng điểm tuần.<br>
Task đủ điều kiện tính điểm khi chuyển <b>Done</b> trên board KAN; size khoá lúc bắt đầu làm
(đổi scope thật → comment <code>SIZE-DOI:</code>).<br>
Chi tiết đầy đủ: tài liệu <i>5 · Công thức tính điểm</i> trong kho onboarding của team.
</div>
</main>
<footer>Trang tĩnh build bởi GitHub Actions (cron hằng đêm) · dữ liệu Jira board KAN · {esc(data["issue_count"])} issue được quét</footer>
</body></html>"""
    return html


def main():
    cfg = load_config()
    data = compute(cfg)
    os.makedirs(os.path.join(HERE, "dist"), exist_ok=True)
    with open(os.path.join(HERE, "dist", "index.html"), "w", encoding="utf-8") as f:
        f.write(render(data, cfg))
    with open(os.path.join(HERE, "dist", "data.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"OK: dist/index.html ({data['issue_count']} issues, "
          f"{len(data['rows'])} done, warnings={len(data['warnings'])})")


if __name__ == "__main__":
    main()
