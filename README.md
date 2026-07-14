# SU01 Team 06 — Scoreboard

Bảng điểm thi đua nội bộ của Team 06 (dự án Ops Agent Detective). Trang tĩnh,
build tự động hằng đêm bằng GitHub Actions, host trên GitHub Pages.

> Đây là công cụ nội bộ theo dõi đóng góp — KHÔNG phải app dự thi.

## Cách hoạt động

```
GitHub Actions (cron 00:15 VN, hằng đêm)
  → build.py đọc Jira Cloud (board OAD) + GitHub API (repo team)
  → tính điểm theo công thức 5-cong-thuc-tinh-diem.md
  → sinh dist/index.html → deploy GitHub Pages
```

- **Điểm hoàn thành** = size task (Story Points S=1/M=3/L=5) × (1 − α).
- **α** = phần Agent làm thay — commit author `agent-*` / `agent@team06`,
  hoặc commit tag `[AI-Kiem-soat-task]` (Agent giám thị).
- **Điểm hỗ trợ** = size phần giúp × 50% (comment Jira `HO-TRO: @ai — size S`),
  trần ≤ 30% điểm tuần.
- Tên hiển thị công khai dạng viết tắt (`tuannlc`, `khoald`…) — không lộ tên đầy đủ.

## Chạy local

```bash
JIRA_EMAIL=<email> JIRA_API_TOKEN=<token> python3 build.py
# mở dist/index.html
```

Chỉ dùng Python stdlib, không cần cài gì thêm.

## Cấu hình

`config.json`:

- `members` — roster: tên, display, email Jira, GitHub user, git email (điền dần).
- `github.repos` — danh sách `owner/repo` của team để đo α (thêm khi tạo repo BE/FE).
- `scoring` — trọng số size, tỷ lệ hỗ trợ, trần hỗ trợ.

Secrets trên repo (Actions): `JIRA_EMAIL`, `JIRA_API_TOKEN`.
