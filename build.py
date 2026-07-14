#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build bảng điểm SU01 Team 06 → dist/index.html (static, GitHub Pages).

Nguồn dữ liệu: Jira Cloud (project OAD) + GitHub (repo team, nếu khai trong config).
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
import unicodedata
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
        warnings.append("Board chưa bật field Story Points (bật Estimation trong "
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
    task_commits = {}  # OAD-xx -> list[{agent_for, additions, human_email, login}]
    for repo in cfg["github"]["repos"]:
        try:
            for pr in gh.pulls(repo):
                proj = cfg["jira"]["project"]
                mkey = re.match(rf"({proj}-\d+)", (pr.get("head", {}).get("ref") or "") + " " +
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
        "members": [{"display": m["display"], "role": m["role"],
                     "github": m.get("github"), "ho_ten": m["ho_ten"]} for m in members],
        "rows": rows, "weeks": weeks, "totals": totals,
        "support_log": support_log, "warnings": warnings,
    }


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

DEMO_TASKS = [
    # (task, tóm tắt, display, size, alpha, trễ, tuần)
    ("OAD-1", "Router structured output tiếng Việt", "tuannlc", "M", 0.25, False, "2026-W29"),
    ("OAD-2", "Tool adapter mock đơn hàng", "vuongnq", "S", 0.0, False, "2026-W29"),
    ("OAD-3", "Skeleton BE Spring Boot + Flyway", "danhlt", "M", 0.4, True, "2026-W29"),
    ("OAD-4", "FE timeline vụ điều tra", "vivt", "M", 0.15, False, "2026-W30"),
    ("OAD-5", "Golden tests router (bộ eval)", "locntx", "S", 0.0, False, "2026-W30"),
    ("OAD-6", "Chat SSE + nút feedback 👍/👎", "anhdn", "M", 0.3, False, "2026-W30"),
    ("OAD-7", "Dashboard bảng lệch baseline", "sontk", "S", 0.5, True, "2026-W30"),
    ("OAD-8", "CaseRepo + migration V1", "khoald", "L", 0.2, False, "2026-W31"),
    ("OAD-9", "Cron patrol 07:30 quét baseline", "tuannlc", "M", 0.1, False, "2026-W31"),
    ("OAD-10", "Playbook don-ket-sau-thanh-toan", "vuongnq", "M", 0.35, False, "2026-W31"),
    ("OAD-11", "Orchestrator vòng lặp ≤10 bước", "tuannlc", "L", 0.3, False, "2026-W32"),
    ("OAD-12", "Validator chặn số ngoài tool", "locntx", "M", 0.0, True, "2026-W32"),
]
DEMO_SUPPORT = [
    # (task được giúp, người giúp, size, tuần)
    ("OAD-3", "tuannlc", "S", "2026-W29"),
    ("OAD-6", "vivt", "S", "2026-W30"),
    ("OAD-8", "danhlt", "S", "2026-W31"),
]


def inject_demo(data, cfg):
    """Board chưa có task Done → đổ DỮ LIỆU MẪU (deterministic) để xem giao diện.

    Tự biến mất: khi Jira có task Done thật, compute() trả rows ≠ rỗng và hàm này
    không được gọi nữa.
    """
    size_pts = cfg["scoring"]["size_points"]
    cap = cfg["scoring"]["support_cap_ratio"]
    weeks, rows, support_log = {}, [], []

    def bucket(week, disp):
        return weeks.setdefault(week, {}).setdefault(disp, {"done": 0.0, "support": 0.0})

    for task, summary, disp, size, alpha, late, wk in DEMO_TASKS:
        pts = size_pts[size] * (1 - alpha)
        bucket(wk, disp)["done"] += pts
        rows.append({"task": task, "summary": summary, "assignee": disp, "size": size,
                     "alpha": alpha, "late": late, "points": round(pts, 2), "week": wk})
    for task, helper, size, wk in DEMO_SUPPORT:
        s = size_pts[size] * cfg["scoring"]["support_rate"]
        bucket(wk, helper)["support"] += s
        support_log.append({"task": task, "helper": helper, "size": size,
                            "points": s, "week": wk})
    for wk, per in weeks.items():
        for disp, v in per.items():
            max_support = (cap / (1 - cap)) * v["done"]
            if v["support"] > max_support:
                v["support"] = round(max_support, 2)
    totals = {}
    for wk, per in weeks.items():
        for disp, v in per.items():
            t = totals.setdefault(disp, {"done": 0.0, "support": 0.0})
            t["done"] += v["done"]
            t["support"] += v["support"]
    data.update(rows=rows, weeks=weeks, totals=totals, support_log=support_log, demo=True)
    return data


# ── Giao diện leaderboard ──────────────────────────────────────────────────
# Màu data đã validate (dataviz 6-checks, light #fff): cam đậm #D97706 (hoàn
# thành) + xanh #0972D3 (hỗ trợ). Cam brand #FF9900 chỉ làm accent trang trí.
C_DONE = "#D97706"
C_SUPPORT = "#0972D3"


def initials_of(ho_ten):
    """'Nguyễn Lê Cao Tuấn' → 'TN' (chữ cái đầu TÊN + chữ cái đầu HỌ, bỏ dấu)."""
    s = unicodedata.normalize("NFD", ho_ten or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).replace("đ", "d").replace("Đ", "D")
    parts = [p for p in s.split() if p]
    if not parts:
        return "?"
    return (parts[-1][0] + parts[0][0]).upper() if len(parts) > 1 else parts[0][0].upper()


def avatar_html(m, size):
    initials = esc(initials_of(m.get("ho_ten") or m["display"]))
    fallback = (f'<span class="avi" style="width:{size}px;height:{size}px;'
                f'line-height:{size}px;font-size:{int(size*0.34)}px">{initials}</span>')
    if m.get("github"):
        return (f'<span class="av" style="width:{size}px;height:{size}px">'
                f'<img src="https://github.com/{esc(m["github"])}.png?size={size*2}" alt="{esc(m["display"])}"'
                f' width="{size}" height="{size}" loading="lazy"'
                f' onerror="this.style.display=\'none\';this.nextElementSibling.style.display=\'inline-block\'">'
                f'<span class="avi" style="display:none;width:{size}px;height:{size}px;'
                f'line-height:{size}px;font-size:{int(size*0.34)}px">{initials}</span></span>')
    return f'<span class="av" style="width:{size}px;height:{size}px">{fallback}</span>'


def stacked_bar(done, support, max_total):
    """Thanh ngang chồng 2 đoạn (div), khe 2px, đầu bo 4px phía data-end."""
    if max_total <= 0:
        return '<div class="track"></div>'
    wd = done / max_total * 100
    ws = support / max_total * 100
    segs = ""
    if done > 0:
        segs += (f'<span class="seg" data-tip="Điểm hoàn thành: {done:.2f}" '
                 f'style="width:{wd:.2f}%;background:{C_DONE}"></span>')
    if support > 0:
        segs += (f'<span class="seg" data-tip="Điểm hỗ trợ: {support:.2f}" '
                 f'style="width:{ws:.2f}%;background:{C_SUPPORT}"></span>')
    return f'<div class="track">{segs}</div>'


def render(data, cfg):
    members = {m["display"]: m for m in data["members"]}
    totals = data["totals"]

    def tot(d):
        t = totals.get(d, {"done": 0, "support": 0})
        return t["done"], t["support"], t["done"] + t["support"]

    roster_idx = {m["display"]: i for i, m in enumerate(data["members"])}
    ranked = sorted(data["members"],
                    key=lambda m: (-tot(m["display"])[2], roster_idx[m["display"]]))
    max_total = max((tot(m["display"])[2] for m in ranked), default=0)
    has_points = max_total > 0

    # ── Podium top 3 ──
    medals = ["🥇", "🥈", "🥉"]
    order = [1, 0, 2]  # hiển thị 2-1-3
    pods = ""
    top3 = ranked[:3]
    for pos in order:
        if pos >= len(top3):
            continue
        m = top3[pos]
        d, s, t = tot(m["display"])
        big = pos == 0
        pods += (f'<div class="pod {"pod1" if big else ""}">'
                 f'<div class="medal">{medals[pos]}</div>'
                 f'{avatar_html(m, 96 if big else 72)}'
                 f'<div class="pname">{esc(m["display"])}</div>'
                 f'<div class="prole">{esc(m["role"])}</div>'
                 f'<div class="ppts">{t:.2f}<small> điểm</small></div></div>')

    # ── Bảng xếp hạng + bar chart ngang ──
    rows = ""
    for i, m in enumerate(ranked, 1):
        d, s, t = tot(m["display"])
        rows += (f'<div class="lrow"><span class="lrank r{i if i<=3 else "x"}">{i}</span>'
                 f'{avatar_html(m, 40)}'
                 f'<span class="lname">{esc(m["display"])}<em>{esc(m["role"])}</em></span>'
                 f'{stacked_bar(d, s, max_total)}'
                 f'<span class="lpts">{t:.2f}</span></div>')

    legend = (f'<div class="legend"><span><i style="background:{C_DONE}"></i>Điểm hoàn thành</span>'
              f'<span><i style="background:{C_SUPPORT}"></i>Điểm hỗ trợ</span></div>')

    empty_note = ('' if has_points else
                  '<p class="empty">Chưa có điểm — board chưa có task Done. '
                  'Sprint bắt đầu là bảng tự nhảy số mỗi đêm.</p>')

    # ── Xu hướng theo tuần (bar dọc, stack 2 đoạn, khe 2px) ──
    wk_keys = sorted(data["weeks"])
    if wk_keys:
        wk_totals = []
        for wk in wk_keys:
            per = data["weeks"][wk]
            dsum = sum(v["done"] for v in per.values())
            ssum = sum(v["support"] for v in per.values())
            wk_totals.append((wk, dsum, ssum))
        wmax = max((d + s) for _, d, s in wk_totals) or 1
        cols = ""
        for wk, d, s in wk_totals:
            hd = d / wmax * 100
            hs = s / wmax * 100
            val = f'<span class="wval">{d + s:.1f}</span>' if (d + s) > 0 else ""
            cols += (f'<div class="wcol" data-tip="{esc(wk)} — hoàn thành {d:.2f} · hỗ trợ {s:.2f}">'
                     f'{val}<div class="wstack">'
                     f'<span style="height:{hs:.1f}%;background:{C_SUPPORT}"></span>'
                     f'<span style="height:{hd:.1f}%;background:{C_DONE}"></span>'
                     f'</div><span class="wlab">{esc(wk[5:])}</span></div>')
        trend = f'<div class="wchart">{cols}</div>{legend}'
    else:
        trend = '<p class="empty">Biểu đồ tuần sẽ hiện khi có task Done đầu tiên.</p>'

    # ── Bảng chi tiết (table view cho accessibility) ──
    tbody = ""
    for i, m in enumerate(ranked, 1):
        d, s, t = tot(m["display"])
        tbody += (f'<tr><td class="rank">{i}</td><td class="name">{esc(m["display"])}</td>'
                  f'<td>{esc(m["role"])}</td><td class="num">{d:.2f}</td>'
                  f'<td class="num">{s:.2f}</td><td class="num total">{t:.2f}</td></tr>')

    week_sections = ""
    for wk in sorted(data["weeks"], reverse=True):
        per = data["weeks"][wk]
        lines = ""
        for disp in sorted(per, key=lambda dd: -(per[dd]["done"] + per[dd]["support"])):
            v = per[disp]
            lines += (f'<tr><td class="name">{esc(disp)}</td><td class="num">{v["done"]:.2f}</td>'
                      f'<td class="num">{v["support"]:.2f}</td>'
                      f'<td class="num total">{v["done"] + v["support"]:.2f}</td></tr>')
        week_sections += (f'<h3>Tuần {esc(wk)}</h3><table><thead><tr><th>Thành viên</th>'
                          f'<th>Điểm hoàn thành</th><th>Điểm hỗ trợ</th><th>Tổng</th></tr></thead>'
                          f'<tbody>{lines}</tbody></table>')

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
    if data.get("demo"):
        warn_html += ('<div class="warn demo">🧪 <b>DỮ LIỆU MẪU</b> — board chưa có task Done thật '
                      'nên trang đang hiển thị số liệu demo để xem giao diện. Khi Sprint chạy và có '
                      'task Done đầu tiên, dữ liệu thật tự thay thế ở lần build kế tiếp.</div>')
    if data["warnings"]:
        warn_html += '<div class="warn"><b>Lưu ý dữ liệu:</b><ul>' + "".join(
            f"<li>{esc(w)}</li>" for w in data["warnings"]) + "</ul></div>"

    html = f"""<!DOCTYPE html>
<html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bảng xếp hạng SU01 — Team 06</title>
<style>
  :root {{ --ink:#232F3E; --accent:#FF9900; --bg:#f4f5f7; --line:#e4e4e4;
           --done:{C_DONE}; --support:{C_SUPPORT}; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; font-family:'Segoe UI',system-ui,-apple-system,sans-serif; background:var(--bg); color:var(--ink); }}
  header {{ background:linear-gradient(135deg,#1b2532 0%,#232F3E 60%,#2d3c50 100%); color:#fff; padding:34px 24px 84px; text-align:center; }}
  header h1 {{ margin:0 0 6px; font-size:28px; letter-spacing:.3px; }}
  header h1 span {{ color:var(--accent); }}
  header p {{ margin:0; opacity:.72; font-size:13.5px; }}
  main {{ max-width:1020px; margin:0 auto; padding:0 16px 60px; }}
  .card {{ background:#fff; border-radius:12px; box-shadow:0 2px 10px rgba(35,47,62,.10); padding:22px 24px; margin-bottom:26px; }}
  h2 {{ font-size:18px; border-left:4px solid var(--accent); padding-left:10px; margin:4px 0 16px; }}
  h3 {{ font-size:15px; margin:22px 0 8px; }}
  /* podium */
  .podwrap {{ display:flex; justify-content:center; align-items:flex-end; gap:26px; margin-top:-64px; flex-wrap:wrap; }}
  .pod {{ background:#fff; border-radius:14px; box-shadow:0 4px 16px rgba(35,47,62,.16); padding:16px 22px 14px; text-align:center; min-width:150px; }}
  .pod1 {{ padding:22px 28px 18px; border-top:3px solid var(--accent); transform:translateY(-14px); }}
  .medal {{ font-size:26px; line-height:1; margin-bottom:8px; }}
  .pod1 .medal {{ font-size:34px; }}
  .pname {{ font-weight:700; margin-top:10px; font-size:16px; }}
  .prole {{ color:#8a94a6; font-size:12px; margin-top:2px; }}
  .ppts {{ font-weight:800; font-size:22px; margin-top:8px; font-variant-numeric:tabular-nums; }}
  .ppts small {{ font-weight:500; font-size:12px; color:#8a94a6; }}
  /* avatar */
  .av {{ display:inline-block; border-radius:50%; overflow:hidden; background:#e9ecf1; vertical-align:middle; flex:none; }}
  .av img {{ display:block; border-radius:50%; }}
  .avi {{ display:inline-block; border-radius:50%; background:var(--ink); color:#fff; font-weight:700; text-align:center; }}
  .pod .av, .pod .avi {{ box-shadow:0 0 0 3px #fff, 0 0 0 5px var(--accent); }}
  /* leaderboard rows */
  .lrow {{ display:flex; align-items:center; gap:14px; padding:10px 4px; border-bottom:1px solid var(--line); }}
  .lrow:last-child {{ border-bottom:none; }}
  .lrank {{ width:30px; height:30px; border-radius:50%; background:#eef1f5; color:#5a6577; font-weight:700; font-size:13.5px; display:flex; align-items:center; justify-content:center; flex:none; }}
  .lrank.r1 {{ background:var(--accent); color:#fff; }}
  .lrank.r2 {{ background:#aab4c2; color:#fff; }}
  .lrank.r3 {{ background:#d9a06b; color:#fff; }}
  .lname {{ width:130px; flex:none; font-weight:700; font-size:14.5px; }}
  .lname em {{ display:block; font-style:normal; font-weight:400; color:#8a94a6; font-size:11.5px; }}
  .track {{ flex:1; height:16px; background:#eef1f5; border-radius:4px; display:flex; gap:2px; overflow:hidden; }}
  .seg {{ height:100%; border-radius:0 4px 4px 0; min-width:3px; }}
  .seg:first-child {{ border-radius:4px 0 0 4px; }}
  .seg:only-child {{ border-radius:4px; }}
  .lpts {{ width:64px; flex:none; text-align:right; font-weight:800; font-variant-numeric:tabular-nums; }}
  .legend {{ display:flex; gap:22px; margin-top:14px; font-size:13px; color:#5a6577; }}
  .legend i {{ display:inline-block; width:12px; height:12px; border-radius:3px; margin-right:6px; vertical-align:-1px; }}
  /* weekly trend */
  .wchart {{ display:flex; align-items:flex-end; gap:18px; height:220px; padding:8px 6px 0; overflow-x:auto; }}
  .wcol {{ display:flex; flex-direction:column; align-items:center; justify-content:flex-end; height:100%; min-width:46px; }}
  .wstack {{ display:flex; flex-direction:column; justify-content:flex-end; gap:2px; height:170px; width:30px; }}
  .wstack span {{ width:100%; border-radius:4px 4px 0 0; min-height:2px; }}
  .wlab {{ margin-top:6px; font-size:11.5px; color:#8a94a6; }}
  .wval {{ font-size:12px; font-weight:700; margin-bottom:4px; font-variant-numeric:tabular-nums; }}
  /* tables */
  table {{ width:100%; border-collapse:collapse; background:#fff; font-size:14px; }}
  th {{ background:var(--ink); color:#fff; text-align:left; padding:9px 12px; font-weight:600; }}
  td {{ padding:9px 12px; border-bottom:1px solid var(--line); }}
  tr:last-child td {{ border-bottom:none; }}
  .num {{ text-align:right; font-variant-numeric:tabular-nums; }}
  .total {{ font-weight:700; }}
  .rank {{ width:36px; color:#888; }}
  .name {{ font-weight:600; }}
  .late {{ background:#c0392b; color:#fff; border-radius:3px; padding:1px 6px; font-size:11px; }}
  .empty {{ color:#777; background:#fafbfc; border:1px dashed var(--line); padding:16px; border-radius:8px; }}
  .warn.demo {{ background:#eef6ff; border-color:#0972D3; }}
  .warn {{ background:#fff7e8; border:1px solid var(--accent); border-radius:8px; padding:10px 14px; margin:18px 0; font-size:13px; }}
  .rules {{ font-size:13.5px; line-height:1.55; }}
  .rules code {{ background:#f1f1f1; padding:1px 5px; border-radius:3px; }}
  footer {{ text-align:center; color:#999; font-size:12px; padding:18px; }}
  #tip {{ position:fixed; z-index:9; background:var(--ink); color:#fff; font-size:12.5px; padding:6px 10px;
          border-radius:6px; pointer-events:none; opacity:0; transition:opacity .12s; max-width:260px; }}
  @media (max-width:640px) {{
    .podwrap {{ gap:12px; }} .pod {{ min-width:118px; padding:12px 14px 10px; }}
    .lname {{ width:92px; }} .lpts {{ width:52px; }}
  }}
</style></head><body>
<header><h1>🏆 Bảng xếp hạng <span>SU01 — Team 06</span></h1>
<p>Ops Agent Detective · cập nhật tự động hằng đêm từ Jira + GitHub · số liệu tính đến {esc(data["generated_at"])}</p></header>
<main>
<div class="podwrap">{pods}</div>
{warn_html}
<div class="card">
<h2>Xếp hạng tổng</h2>
{empty_note}
{rows}
{legend}
</div>

<div class="card">
<h2>Xu hướng theo tuần</h2>
{trend}
</div>

<div class="card">
<h2>Bảng số liệu</h2>
<table><thead><tr><th></th><th>Thành viên</th><th>Role</th><th>Điểm hoàn thành</th><th>Điểm hỗ trợ</th><th>Tổng</th></tr></thead>
<tbody>{tbody}</tbody></table>
{week_sections}
</div>

<div class="card">
<h2>Chi tiết task đã Done</h2>
{tasks_html}
</div>

<div class="card rules">
<h2>Luật chơi (tóm tắt)</h2>
<b>Điểm hoàn thành</b> = size task (Story Points <code>S=1 · M=3 · L=5</code>) × (1 − α).<br>
<b>α</b> = phần việc Agent làm thay, đo bằng diff attribution trên nhánh task —
commit author <code>agent-*</code> / <code>agent@team06</code>, hoặc tag <code>[AI-Kiem-soat-task]</code>
(Agent giám thị làm nốt task trễ). Dùng Agent không bị âm điểm.<br>
<b>Điểm hỗ trợ</b> = size phần giúp × 50% — dấu vết máy đọc được: commit trên nhánh task người khác,
PR review, hoặc comment Jira <code>HO-TRO: @người-giúp — size S</code>; trần ≤ 30% tổng điểm tuần.<br>
Task đủ điều kiện tính điểm khi chuyển <b>Done</b> trên board Jira; size khoá lúc bắt đầu làm
(đổi scope thật → comment <code>SIZE-DOI:</code>).<br>
Chi tiết đầy đủ: tài liệu <i>5 · Công thức tính điểm</i> trong kho onboarding của team.
</div>
</main>
<div id="tip"></div>
<script>
var tip=document.getElementById('tip');
document.addEventListener('mousemove',function(e){{
  var el=e.target.closest('[data-tip]');
  if(el){{tip.textContent=el.getAttribute('data-tip');tip.style.opacity=1;
    tip.style.left=Math.min(e.clientX+14,window.innerWidth-tip.offsetWidth-8)+'px';
    tip.style.top=(e.clientY+16)+'px';}}
  else tip.style.opacity=0;
}});
</script>
<footer>Trang tĩnh build bởi GitHub Actions (cron hằng đêm) · dữ liệu Jira board Jira · {esc(data["issue_count"])} issue được quét</footer>
</body></html>"""
    return html


def main():
    cfg = load_config()
    data = compute(cfg)
    if not data["rows"] and os.environ.get("DEMO_WHEN_EMPTY", "1") != "0":
        inject_demo(data, cfg)
    os.makedirs(os.path.join(HERE, "dist"), exist_ok=True)
    with open(os.path.join(HERE, "dist", "index.html"), "w", encoding="utf-8") as f:
        f.write(render(data, cfg))
    with open(os.path.join(HERE, "dist", "data.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"OK: dist/index.html ({data['issue_count']} issues, "
          f"{len(data['rows'])} done, warnings={len(data['warnings'])})")


if __name__ == "__main__":
    main()
