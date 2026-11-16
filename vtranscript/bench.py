"""Công cụ thử model: xem số dư key OpenRouter và so chất lượng các lần chạy với một bản tham chiếu.

  vtranscript-bench balance
  vtranscript-bench compare output/<bản tham chiếu> output/_thu/<lần thử> [...]
  vtranscript-bench ref --subs phu_de_nguoi_lam.vtt output/<lần chạy> [...]   (chấm với phụ đề do người làm)
(chưa cài gói thì thay `vtranscript-bench` bằng `python -m vtranscript.bench`)

"Lệch" = tỉ lệ từ khác bản chuẩn hóa của lần chạy tham chiếu (thường là cấu hình best), sau khi bỏ dấu câu,
hoa/thường, tiếng ngập ngừng và số (vì "hai trăm" và "200" là như nhau). Bản tham chiếu không phải sự thật
tuyệt đối: chính nó còn khoảng 2% lỗi, nên chỉ so được lần nào tốt hơn hay kém hơn, cần nghe lại các phút lệch nhiều.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
import unicodedata
import urllib.request
from pathlib import Path

from . import __version__, config
from .util import read_json, ts

FILLERS = set("à ờ ừ ừm ơ ờm hừm hmm uh um ạ ừa ồ ô ê ủa á a ơi dạ vâng".split())
NUM_WORDS = set("không một mốt hai ba bốn tư năm lăm nhăm sáu bảy tám chín mười mươi trăm nghìn ngàn triệu tỷ tỉ "
                "linh lẻ rưỡi".split())
HALLUCINATION = re.compile(r"subscribe|Ghiền Mì Gõ|theo dõi và hẹn gặp|video tiếp theo|bỏ lỡ những video|"
                           r"Cảm ơn các bạn đã|đăng ký kênh", re.I)


def words(text: str) -> list[str]:
    s = unicodedata.normalize("NFC", text.lower())
    s = re.sub(r"(.)\1{2,}", r"\1", s)
    s = re.sub(r"[^\w\s]", " ", s)
    return [t for t in s.split() if t not in FILLERS and t not in NUM_WORDS and not re.search(r"\d", t)]


def diff_count(ref: list[str], hyp: list[str]) -> int:
    n = 0
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, ref, hyp, autojunk=False).get_opcodes():
        if op != "equal":
            n += max(i2 - i1, j2 - j1)
    return n


def pct(ref: str, hyp: str) -> float:
    r = words(ref)
    return 100 * diff_count(r, words(hyp)) / max(len(r), 1)


class Run:
    def __init__(self, d: Path):
        self.dir = d
        w = d / "_work"
        self.norm = read_json(w / "normalized.json")
        self.segs = self.norm["segments"]
        asr = read_json(w / "asr_raw.json") if (w / "asr_raw.json").exists() else {}
        vis = read_json(w / "visual_context.json") if (w / "visual_context.json").exists() else {}
        llm = self.norm.get("llm") or {}
        self.models = {"asr": asr.get("model"), "vision": vis.get("model"),
                       "glossary": llm.get("glossary_model"), "main": llm.get("model")}
        self.cost = None  # None = lần chạy chưa có sổ chi phí (chạy trước khi có tính năng này)
        if (w / "chi_phi.jsonl").exists():
            self.cost = 0.0
            for line in (w / "chi_phi.jsonl").read_text(encoding="utf-8").splitlines():
                r = json.loads(line)
                self.cost += r["asr"]["usd"] + sum(x["usd"] for x in r["llm"])
        s = d / "03_tom_tat.md"
        self.summary = s.read_text(encoding="utf-8") if s.exists() else ""

    def text(self, a: float = 0, b: float = 1e9, key: str = "text") -> str:
        return " ".join(s[key] for s in self.segs if a <= s["start"] < b)


def compare(ref_dir: Path, run_dirs: list[Path]) -> str:
    ref = Run(ref_dir)
    runs = [ref] + [Run(d) for d in run_dirs]
    dur = max(s["end"] for s in ref.segs)
    ref_txt = ref.text()
    # Thuật ngữ để đếm: các mục trong từ điển của bản tham chiếu, xuất hiện ít nhất 3 lần ở bản tham chiếu.
    terms = []
    for e in ref.norm["glossary"].get("entries", []):
        t = e["canonical"]
        c = len(re.findall(r"(?<!\w)" + re.escape(t) + r"(?!\w)", ref_txt, re.I))
        if c >= 3 and t not in [x for x, _ in terms]:
            terms.append((t, c))

    out = [f"# So sánh với bản tham chiếu: {ref_dir}", ""]
    out += ["| | " + " | ".join(r.dir.name for r in runs) + " |", "|---" * (len(runs) + 1) + "|"]

    def row(name, vals):
        out.append(f"| {name} | " + " | ".join(str(v) for v in vals) + " |")

    for k, label in (("asr", "ASR"), ("vision", "Đọc khung hình"), ("glossary", "Lập từ điển"),
                     ("main", "Sửa cụm/soát/tóm tắt")):
        row(label, [r.models[k] or "?" for r in runs])
    row("**Lệch sau chuẩn hóa**", ["(tham chiếu)"] + [f"**{pct(ref_txt, r.text()):.1f}%**" for r in runs[1:]])
    row("Lệch bản thô (ASR)", [f"{pct(ref_txt, r.text(key='raw')):.1f}%" for r in runs])
    lost_rows = []
    for r in runs:
        lost, where = 0, []
        for a in range(0, int(dur), 30):
            n_ref, n_run = len(words(ref.text(a, a + 30))), len(words(r.text(a, a + 30)))
            if n_ref >= 15 and n_run < 0.4 * n_ref:
                lost += n_ref - n_run
                where.append(ts(a)[3:])
        lost_rows.append((lost, where))
    total = len(words(ref_txt))
    row("Nội dung bị mất", [f"{100 * x / total:.1f}%" + (f" ({', '.join(w[:4])})" if w else "") for x, w in lost_rows])
    row("Mục từ điển", [len(r.norm["glossary"].get("entries", [])) for r in runs])
    row("Chỗ cần kiểm tra lại", [len(r.norm.get("uncertain", [])) for r in runs])
    row("Đoạn không sửa được (LLM lỗi)", [len(r.norm.get("fallback_ids", [])) for r in runs])
    row("Câu ảo giác còn sót", [sum(1 for s in r.segs if HALLUCINATION.search(s["text"])) for r in runs])
    row("Tóm tắt (số mục / ký tự)", [f"{len(re.findall(r'^#{2,3} ', r.summary, re.M))} / {len(r.summary):,}"
                                     for r in runs])
    row("Chi phí đã tiêu (không tính cache)", [f"${r.cost:.2f}" if r.cost is not None else "?" for r in runs])

    if terms:
        out += ["", "## Số lần xuất hiện tên riêng, thuật ngữ", "",
                "| | " + " | ".join(r.dir.name for r in runs) + " |", "|---" * (len(runs) + 1) + "|"]
        for t, c in sorted(terms, key=lambda x: -x[1])[:25]:
            vals = [len(re.findall(r"(?<!\w)" + re.escape(t) + r"(?!\w)", r.text(), re.I)) for r in runs]
            mark = " ⚠" if any(v < 0.6 * c for v in vals[1:]) else ""
            row(t + mark, vals)

    for r in runs[1:]:
        mins = []
        for a in range(0, int(dur), 60):
            rt = ref.text(a, a + 60)
            if len(words(rt)) >= 40:
                mins.append((pct(rt, r.text(a, a + 60)), a))
        mins.sort(reverse=True)
        if not mins or mins[0][0] == 0:
            continue
        out += ["", f"## {r.dir.name}: các phút lệch nhiều nhất (nên nghe lại)", ""]
        for p, a in mins[:3]:
            out += [f"- **{ts(a)}** lệch {p:.0f}%",
                    f"    - tham chiếu: {ref.text(a, a + 60)[:300]}",
                    f"    - lần thử: {r.text(a, a + 60)[:300]}"]
    return "\n".join(out) + "\n"


# ----------------------------------------------------------------------------------------------
# Chấm với phụ đề do người làm (vd. phụ đề tiếng Việt thủ công trên YouTube)
# ----------------------------------------------------------------------------------------------

_CUE_TIME = re.compile(r"(\d+):(\d\d):(\d\d)[.,](\d{3})|(\d\d):(\d\d)[.,](\d{3})")


def _secs(m: re.Match) -> float:
    if m.group(1) is not None:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) + int(m.group(4)) / 1000
    return int(m.group(5)) * 60 + int(m.group(6)) + int(m.group(7)) / 1000


def read_subs(path: Path) -> list[tuple[float, float, str]]:
    """Đọc .vtt/.srt -> [(bắt đầu, kết thúc, chữ)]. Bỏ thẻ định dạng, gộp các dòng của một cue."""
    import html

    cues, cur = [], None
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if "-->" in line:
            a, b = line.split("-->", 1)
            ma, mb = _CUE_TIME.search(a), _CUE_TIME.search(b)
            if ma and mb:
                cur = [_secs(ma), _secs(mb), []]
                cues.append(cur)
            continue
        if not line:
            cur = None
        elif cur is not None:
            cur[2].append(html.unescape(re.sub(r"<[^>]+>", "", line)))
    out = []
    for a, b, lines in cues:
        t = " ".join(" ".join(lines).split())
        if t and (not out or out[-1][2] != t):  # phụ đề cuộn tự động lặp lại dòng trước
            out.append((a, b, t))
    return out


def wer(ref: list[str], hyp: list[str]) -> float:
    """Tỉ lệ lỗi từ (Levenshtein cấp từ / số từ tham chiếu)."""
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i] + [0] * len(hyp)
        for j, h in enumerate(hyp, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h))
        prev = cur
    return 100 * prev[-1] / max(len(ref), 1)


_TERM_TOK = re.compile(r"\w+(?:[-'’.]\w+)*", re.UNICODE)


def term_tokens(cues: list[str]) -> list[str]:
    """Tên riêng/thuật ngữ trong phụ đề tham chiếu: từ viết hoa không đứng đầu câu, hoặc có chữ hoa ở giữa (BingX, KPI,
    iPhone). Bỏ từ chỉ có chữ số."""
    out = []
    for text in cues:
        for m in _TERM_TOK.finditer(text):
            t = m.group()
            if t.isdigit() or len(t) < 2:
                continue
            before = text[: m.start()].rstrip()
            initial = not before or before[-1] in ".!?:…-–—\"“"
            if any(c.isupper() for c in t[1:]) or (t[0].isupper() and not initial):
                out.append(t)
    return out


def term_recall(ref_terms: list[str], hyp_text: str) -> tuple[float, list[str]]:
    from collections import Counter

    need, have = Counter(ref_terms), Counter(_TERM_TOK.findall(hyp_text))
    hit = sum(min(c, have[t]) for t, c in need.items())
    missed = [t for t, c in need.most_common() if have[t] < c]
    return 100 * hit / max(sum(need.values()), 1), missed


def ref_eval(subs: Path, run_dirs: list[Path], offset: float = 0.0, dur: float | None = None) -> list[dict]:
    """Chấm các lần chạy với phụ đề người làm. `offset`: giây đầu của đoạn video đã cắt, tính theo phụ đề gốc."""
    runs = [Run(d) for d in run_dirs]
    end_clip = dur if dur is not None else max(s["end"] for s in runs[0].segs)
    cues = [t for a, b, t in read_subs(subs) if offset <= (a + b) / 2 < offset + end_clip]
    ref_text = " ".join(cues)
    ref_words, terms = words(ref_text), term_tokens(cues)
    out = []
    for r in runs:
        segs = [s for s in r.segs if (s["start"] + s["end"]) / 2 < end_clip]
        raw = " ".join(s["raw"] for s in segs)
        norm = " ".join(s["text"] for s in segs)
        rr, mr = term_recall(terms, raw)
        rn, mn = term_recall(terms, norm)
        so_tay = r.norm.get("so_tay") or {}
        out.append({"run": str(r.dir), "ref_words": len(ref_words), "ref_terms": len(terms),
                    "wer_raw": wer(ref_words, words(raw)), "wer_norm": wer(ref_words, words(norm)),
                    "term_raw": rr, "term_norm": rn, "missed_norm": mn[:15], "missed_raw": mr[:15],
                    "genre": r.norm["glossary"].get("genre"), "domain": r.norm["glossary"].get("domain"),
                    "hints": len(so_tay.get("goi_y") or []), "entries": len(r.norm["glossary"].get("entries", [])),
                    "cost": r.cost})
    return out


def ref_table(subs: Path, rows: list[dict]) -> str:
    out = [f"# Chấm với phụ đề người làm: {subs}", "",
           f"Tham chiếu: {rows[0]['ref_words']} từ, {rows[0]['ref_terms']} lượt tên riêng/thuật ngữ.", "",
           "| Lần chạy | WER thô | WER chuẩn hóa | Tên/thuật ngữ đúng (thô) | (chuẩn hóa) | Thể loại / lĩnh vực | Gợi ý sổ tay |",
           "|---|---|---|---|---|---|---|"]
    for r in rows:
        out.append(f"| {Path(r['run']).name} | {r['wer_raw']:.1f}% | **{r['wer_norm']:.1f}%** | {r['term_raw']:.0f}% | "
                   f"**{r['term_norm']:.0f}%** | {r['genre'] or '?'} / {r['domain'] or '?'} | {r['hints']} |")
    for r in rows:
        if r["missed_norm"]:
            out.append(f"\n{Path(r['run']).name} — tên/thuật ngữ còn sai hoặc thiếu: {', '.join(r['missed_norm'])}")
    return "\n".join(out) + "\n"


def balance() -> str:
    from .llm import openrouter_key

    req = urllib.request.Request("https://openrouter.ai/api/v1/key",
                                 headers={"Authorization": f"Bearer {openrouter_key()}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        d = json.load(resp)["data"]
    lim, rem = d.get("limit"), d.get("limit_remaining")
    per = {"daily": "/ngày", "weekly": "/tuần", "monthly": "/tháng"}.get(d.get("limit_reset") or "", "")
    used = {"/ngày": "usage_daily", "/tuần": "usage_weekly", "/tháng": "usage_monthly"}.get(per)
    return (f"Key OpenRouter: đã dùng ${d.get('usage', 0):.2f}"
            + (f" (kỳ này ${d.get(used, 0):.2f})" if used and d.get(used) is not None else "")
            + (f", giới hạn ${lim:.2f}{per}, còn ${rem:.2f}" if lim is not None else ", không đặt giới hạn"))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="vtranscript-bench", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("balance", help="Số dư key OpenRouter")
    c = sub.add_parser("compare", help="So các lần chạy với bản tham chiếu")
    c.add_argument("ref", type=Path, help="Thư mục output của lần chạy tham chiếu")
    c.add_argument("runs", type=Path, nargs="+", help="Thư mục output của các lần thử")
    c.add_argument("--out", type=Path, default=None, help="Ghi bảng so sánh ra file .md")
    r = sub.add_parser("ref", help="Chấm các lần chạy với phụ đề do người làm (.vtt/.srt)")
    r.add_argument("runs", type=Path, nargs="+", help="Thư mục output của các lần chạy cùng một video")
    r.add_argument("--subs", type=Path, required=True, help="Phụ đề tham chiếu")
    r.add_argument("--offset", type=float, default=0.0, help="Video đã cắt bắt đầu từ giây này của phụ đề gốc")
    r.add_argument("--dur", type=float, default=None, help="Độ dài đoạn cắt (mặc định theo transcript)")
    r.add_argument("--json", type=Path, default=None, help="Ghi kết quả dạng JSON")
    r.add_argument("--out", type=Path, default=None, help="Ghi bảng ra file .md")
    a = p.parse_args(argv)
    try:
        config.load()
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    if a.cmd == "balance":
        print(balance())
        return 0
    if a.cmd == "ref":
        rows = ref_eval(a.subs, a.runs, a.offset, a.dur)
        md = ref_table(a.subs, rows)
        if a.json:
            a.json.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    else:
        md = compare(a.ref, a.runs)
    print(md)
    if a.out:
        a.out.write_text(md, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
