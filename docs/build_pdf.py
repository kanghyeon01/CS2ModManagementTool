"""Builds docs/CS2ModManagementTool-안내서.pdf from the Markdown documents.

Not part of the tool (it needs extra packages): run it on any PC with
    pip install markdown playwright && python -m playwright install chromium   (or Node: npm i playwright)
    python docs/build_pdf.py [--fonts DIR]
DIR may hold NotoSansKR.ttf and a monospace Korean font (e.g. NanumGothicCoding); without them
the system fonts are used.
"""

import argparse
import datetime
import html
import re
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parents[1]

CHAPTERS = [
    ("개요", "README.md"),
    ("설치 안내", "docs/SETUP.md"),
    ("시놀로지 NAS 설치", "deploy/synology/README.md"),
    ("운영 안내", "docs/OPERATIONS.md"),
    ("보안", "docs/SECURITY.md"),
    ("구조", "docs/ARCHITECTURE.md"),
    ("모드 추가하기", "docs/ADDING_A_MOD.md"),
]

CSS = """
@page { size: A4; margin: 18mm 16mm 20mm 16mm; }
body { font-family: 'Doc Sans', 'Noto Sans KR', 'WenQuanYi Zen Hei', sans-serif; font-size: 10.2pt; line-height: 1.62; color: #1d2330; }
h1 { font-size: 20pt; margin: 0 0 6mm; padding-bottom: 2.5mm; border-bottom: 2px solid #2f5d9e; color: #173a6b; }
h2 { font-size: 14pt; margin: 7mm 0 2.5mm; color: #173a6b; break-after: avoid; }
h3 { font-size: 11.5pt; margin: 5mm 0 2mm; break-after: avoid; }
p, li { orphans: 2; widows: 2; }
code, pre { font-family: 'Doc Mono', 'NanumGothicCoding', monospace; font-size: 8.8pt; }
code { background: #eef1f6; padding: 0.3mm 1.2mm; border-radius: 1mm; }
pre { background: #f4f6fa; border: 1px solid #d9dfe9; border-radius: 1.5mm; padding: 2.5mm 3mm; white-space: pre-wrap; word-break: break-all; break-inside: avoid; }
pre code { background: none; padding: 0; }
table { border-collapse: collapse; width: 100%; margin: 3mm 0; font-size: 9.2pt; break-inside: auto; }
th, td { border: 1px solid #cdd5e1; padding: 1.5mm 2mm; vertical-align: top; text-align: left; }
th { background: #e6ecf5; }
tr { break-inside: avoid; }
blockquote { margin: 3mm 0; padding: 2mm 4mm; border-left: 3px solid #2f5d9e; background: #f2f6fc; }
.chapter { break-before: page; }
.cover { height: 250mm; display: flex; flex-direction: column; justify-content: center; }
.cover .title { font-size: 30pt; font-weight: 700; color: #173a6b; }
.cover .sub { font-size: 14pt; margin-top: 4mm; color: #3b4a63; }
.cover .meta { margin-top: 18mm; font-size: 10.5pt; color: #55627a; }
.toc ol { font-size: 12pt; line-height: 2.1; }
a { color: inherit; text-decoration: none; }
.flow { margin: 3mm 0 5mm; break-inside: avoid; }
.flow .tier-label { font-size: 9pt; color: #55627a; margin-bottom: 1.5mm; }
.flow .row { display: flex; gap: 2.5mm; align-items: stretch; }
.flow .box { border: 1.5px solid #2f5d9e; border-radius: 2mm; padding: 2mm 3mm; background: #f2f6fc; font-size: 9pt; line-height: 1.5; flex: 1; }
.flow .box.phone { border-color: #3f8a5a; background: #f0f8f2; }
.flow .link { align-self: center; text-align: center; color: #2f5d9e; font-size: 14pt; line-height: 1.1; }
.flow .link small { font-size: 7.5pt; }
.flow .arrow { text-align: center; margin: 2.5mm 0; font-size: 9pt; color: #173a6b; }
.flow .arrow::before { content: "▼ "; } .flow .arrow::after { content: " ▼"; }
.flow .pc { flex: none; }
.flow .steps div { display: flex; gap: 2mm; padding: 0.6mm 0; border-top: 1px dashed #c6d1e2; }
.flow .steps div:first-child { border-top: none; }
.flow .steps span { min-width: 9mm; font-weight: 700; color: #2f5d9e; }
"""


# The README's text diagram does not line up in print (Hangul is wider than box characters).
FLOW = """
<div class="flow">
  <div class="tier-label">항상 켜져 있는 기기 (2대로 이중화)</div>
  <div class="row">
    <div class="box"><b>시놀로지 NAS</b><br>C 감시기 (3 MB)<br>파이썬 컨트롤러 · 주</div>
    <div class="link">⇄<br><small>서로 감시</small></div>
    <div class="box"><b>데비안/우분투 서버</b><br>C 감시기<br>파이썬 컨트롤러 · 보조</div>
    <div class="box phone"><b>휴대폰 (디스코드)</b><br>알림 · ✅/❌ 반응으로 승인<br>!status · !maintenance</div>
  </div>
  <div class="arrow">① Steam 빌드 변경 · 포럼/GitHub 새 글 감지 &nbsp;→&nbsp; ② Wake-on-LAN</div>
  <div class="box pc"><b>메인 PC (멀티부팅)</b>
    <div class="steps">
      <div><span>리눅스</span>작업이 있으면 Windows로 한 번 재부팅 (UEFI BootNext 또는 grub-reboot)</div>
      <div><span>③</span>Steam이 새 빌드를 설치할 때까지 대기, 옛 게임 DLL 보관</div>
      <div><span>④</span>디컴파일 비교로 "게임 API에서 바뀐 것" 정리</div>
      <div><span>⑤</span>모드 빌드 → 실패하면 Claude Code(헤드리스)가 수정</div>
      <div><span>⑥</span>게임 안 스모크 테스트 (하네스 모드가 도시 로드 · 오류 수집)</div>
      <div><span>⑦</span>보안 정책 검사 → 버전 올림 → Paradox Mods 업로드 → GitHub 푸시</div>
      <div><span>⑧</span>결과 보고 후 종료</div>
    </div>
  </div>
</div>
"""


def _blank_line_before_lists(text: str) -> str:
    """GitHub shows a list right under a paragraph line; Python-Markdown needs a blank line first."""
    out, fenced = [], False
    item = re.compile(r"^\s*(?:[-*]|\d+\.)\s")
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif not fenced and item.match(line) and out and out[-1].strip() and not item.match(out[-1]) \
                and not out[-1].startswith(("    ", "\t", "|")) and not out[-1].lstrip().startswith(">"):
            out.append("")
        out.append(line)
    return "\n".join(out)


def chapter_html(path: Path) -> str:
    text = _blank_line_before_lists(path.read_text(encoding="utf-8"))
    body = markdown.markdown(text, extensions=["tables", "fenced_code", "sane_lists"])
    body = re.sub(r"<pre><code>[^<]*항상 켜져 있는 기기.*?</code></pre>", lambda m: FLOW, body, count=1, flags=re.S)
    # Links between the Markdown files mean nothing in print: keep the words only.
    body = re.sub(r'<a href="[^"]*">(.*?)</a>', r"\1", body)
    return body


def build(out: Path, fonts: Path = None) -> Path:
    faces = ""
    if fonts:
        for family, names in (("Doc Sans", ["NotoSansKR.ttf"]), ("Doc Mono", ["NanumGothicCoding-Regular.ttf", "D2Coding.ttf"])):
            for n in names:
                if (fonts / n).is_file():
                    faces += "@font-face { font-family: '%s'; src: url('%s'); font-weight: 100 900; }\n" % (family, (fonts / n).resolve().as_uri())
                    break
    today = datetime.date.today().isoformat()
    parts = [
        '<section class="cover"><div class="title">CS2 Mod Management Tool</div>'
        '<div class="sub">게임 업데이트에 맞춰 모드를 무인으로 고치고, 시험하고, 배포하는 도구<br>설치 · 운영 안내서</div>'
        '<div class="meta">작성일 %s<br>저장소 github.com/kanghyeon01/CS2ModManagementTool</div></section>' % today,
        '<section class="chapter toc"><h1>차례</h1><ol>%s</ol></section>' % "".join("<li>%s</li>" % html.escape(t) for t, _ in CHAPTERS),
    ]
    for title, rel in CHAPTERS:
        parts.append('<section class="chapter">%s</section>' % chapter_html(ROOT / rel))
    page = "<!doctype html><html lang='ko'><head><meta charset='utf-8'><style>%s%s</style></head><body>%s</body></html>" % (faces, CSS, "".join(parts))
    tmp = out.with_suffix(".html")
    tmp.write_text(page, encoding="utf-8")
    footer = ("<div style='font-size:8pt;width:100%;text-align:center;color:#7a869a'>"
              "CS2 Mod Management Tool · <span class='pageNumber'></span> / <span class='totalPages'></span></div>")
    opts = {"path": str(out), "format": "A4", "printBackground": True, "displayHeaderFooter": True,
            "headerTemplate": "<span></span>", "footerTemplate": footer,
            "margin": {"top": "16mm", "bottom": "18mm", "left": "15mm", "right": "15mm"}}
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        # Node's Playwright instead (npm i playwright)
        import json
        import subprocess

        js = ("const {chromium}=require('playwright');(async()=>{const b=await chromium.launch();const p=await b.newPage();"
              "await p.goto(%s,{waitUntil:'networkidle'});await p.pdf(%s);await b.close();})().catch(e=>{console.error(e);process.exit(1)});"
              % (json.dumps(tmp.resolve().as_uri()), json.dumps(opts)))
        subprocess.run(["node", "-e", js], check=True)
    else:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            pg = browser.new_page()
            pg.goto(tmp.resolve().as_uri(), wait_until="networkidle")
            pg.pdf(path=str(out), format="A4", print_background=True, display_header_footer=True, header_template="<span></span>",
                   footer_template=footer, margin=opts["margin"])
            browser.close()
    tmp.unlink()
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--fonts", type=Path)
    ap.add_argument("--out", type=Path, default=ROOT / "docs" / "CS2ModManagementTool-안내서.pdf")
    a = ap.parse_args()
    print(build(a.out, a.fonts))
