from datetime import date

from jobbank.fetchers.github_boards import parse_board_markdown

TODAY = date(2026, 7, 15)

SPEEDYAPPLY = """
# FAANG+
| Company | Position | Location | Salary | Posting | Age |
| --- | --- | --- | --- | --- | --- |
| <a href="https://www.nvidia.com"><strong>NVIDIA</strong></a> | Performance Engineer Intern - Fall 2027 | St. Louis, MO | $62/hr | <a href="https://nvidia.wd5.myworkdayjobs.com/job/JR2015779"><img src="https://i.imgur.com/x.png" alt="Apply" width="70"/></a> | 9d |
"""

VANSH = """
| Company | Role | Location | Application/Link | Date Posted |
| ------- | ---- | -------- | ---------------- | ----------- |
| Rakuten International | Software Engineer Intern | San Mateo, California | <a href="https://rakuten.wd1.myworkdayjobs.com/job/1033171?utm_source=github"><img src="https://i.imgur.com/u.png" width="118" alt="Apply"></a> | Jul 09 |
| ↳ | Backend Intern | NYC | <a href="https://rakuten.example.com/2"><img src="https://i.imgur.com/u.png" alt="Apply"></a> | Jul 08 |
| Closed Corp | ML Intern | Remote | 🔒 | Jul 01 |
"""

JOBRIGHT = """
| Company | Job Title | Location | Work Model | Date Posted |
| ------- | --------- | -------- | ---------- | ----------- |
| **[Elevated Web Design](https://www.elevatedwebdesign.com/)** | **[Software Engineering Internship](https://jobright.ai/jobs/info/6a5791a1?utm_campaign=1079&utm_source=git)** | United States | Remote | Jul 15 |
"""


def test_speedyapply_format():
    jobs = parse_board_markdown(SPEEDYAPPLY, "github:speedyapply", today=TODAY)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.company == "NVIDIA"
    assert job.title.startswith("Performance Engineer Intern")
    assert job.url == "https://nvidia.wd5.myworkdayjobs.com/job/JR2015779"
    assert job.posted == date(2026, 7, 6)  # 9d before TODAY
    assert job.salary == "$62/hr"


def test_vansh_continuation_and_closed():
    jobs = parse_board_markdown(VANSH, "github:vanshb03", today=TODAY)
    assert len(jobs) == 2  # closed row dropped
    assert jobs[0].company == "Rakuten International"
    assert jobs[1].company == "Rakuten International"  # continuation arrow resolved
    assert jobs[1].title == "Backend Intern"
    assert jobs[0].posted == date(2026, 7, 9)


def test_jobright_title_link():
    jobs = parse_board_markdown(JOBRIGHT, "github:jobright-ai", today=TODAY)
    assert len(jobs) == 1
    assert jobs[0].company == "Elevated Web Design"
    assert jobs[0].url.startswith("https://jobright.ai/jobs/info/6a5791a1")
    assert jobs[0].extra["work_model"] == "Remote"
