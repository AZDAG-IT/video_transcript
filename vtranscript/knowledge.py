"""Sổ tay tự học cho bước chuẩn hóa: từ điển lỗi ASR hay gặp + ghi chú văn phong theo thể loại video.

Máy tự học sau mỗi video, không cần người duyệt. Luật lõi (prompts.py) không bao giờ bị sửa; kho chỉ thêm gợi ý.

Từ điển (`tu_dien.json`): mỗi mục là một cách viết đúng kèm các biến thể ASR hay nghe nhầm. Bằng chứng lưu theo
từng video (ủng hộ / phản bác), trạng thái tự tính lại mỗi lần đọc:
  - đang dùng: đủ căn cứ (do người dùng khai, gặp ở ≥3 video, hoặc ≥2 video có căn cứ chắc: thấy trên màn hình
    hay model mạnh chắc chắn) và ít phản bác. Được đưa thẳng vào bước sửa từng cụm khi video cùng lĩnh vực;
  - ứng viên: mới gặp. Chỉ đưa vào bước lập từ điển như gợi ý để model tự cân nhắc;
  - loại bỏ: bị phản bác ở ≥2 video và nhiều hơn số video ủng hộ. Không dùng nữa.
Chống khớp bừa: biến thể của tên riêng có chữ hoa thì so đúng hoa/thường ("Gian" không khớp "thời gian"); mọi mục
khớp được đem kèm câu trích cho lượt phân tích (model rẻ) xét theo ngữ cảnh, chỉ mục được xác nhận mới tới bước lập
từ điển. Sau khi chuẩn hóa, code đề xuất mục để học (bản cuối thực sự đã sửa biến thể thành cách viết đúng) và một
lượt model rẻ khác duyệt lại, loại từ thông dụng, sửa văn phong, sửa sai. Một video tính là phản bác khi lượt phân
tích đã xác nhận gợi ý đúng ngữ cảnh mà bản cuối vẫn để nguyên biến thể.

Thể loại mở: ngoài các thể loại có sẵn, lượt phân tích được đặt mã thể loại mới. Mã mới được ghi vào kho kèm mô tả và
đưa lại cho các video sau chọn; ghi chú văn phong học theo mã đó. Máy không tự tạo file luật .md — SO_TAY.md liệt kê
thể loại mới chưa có luật để team bổ sung nếu muốn.

Ghi chú thể loại (`the_loai.json`): quy ước văn phong model rút ra cho từng thể loại. Ghi chú có hiệu lực khi ≥2 video
cùng thể loại xác nhận; mỗi video dùng tối đa 5 ghi chú mạnh nhất, nối sau luật thể loại do team soạn.

Kho mặc định: ~/.config/vtranscript/kien_thuc/ (đổi bằng --knowledge hoặc biến VT_KNOWLEDGE_DIR).
  SO_TAY.md     bản cho người đọc, sinh lại sau mỗi lần cập nhật (đừng sửa tay)
  lich_su/      ảnh chụp kho trước mỗi lần cập nhật, giữ 20 bản gần nhất
  the_loai/     (tùy chọn) luật của team cho từng thể loại: <thể loại>.md đè file có sẵn trong gói

Quản lý:
  vtranscript-sotay xem                     tóm tắt kho
  vtranscript-sotay xoa <từ> [...]          xóa mục và chặn không học lại
  vtranscript-sotay quay-lai [bản]          trả kho về một ảnh chụp trong lich_su/ (mặc định bản gần nhất)
  vtranscript-sotay hoc <thư mục> [...]     học từ các lần chạy đã có (output/<video>)
"""

from __future__ import annotations

import argparse
import difflib
import os
import re
import shutil
import sys
from collections import Counter
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from . import prompts as P
from .prompts import DOMAINS, GENRES
from .util import fold, log, nfc, read_json, sha, write_json, write_text

PKG_PRESETS = Path(__file__).resolve().parent / "the_loai"
KEEP_SNAPSHOTS = 20
MAX_HINTS = 80
MAX_NOTES_PER_GENRE = 30
ACTIVE_NOTES = 5
PROPER = {"person", "organization", "product"}
SLUG = re.compile(r"^[a-z][a-z0-9_]{2,30}$")
TRANG_THAI = {"dang_dung": "đang dùng", "ung_vien": "ứng viên", "loai_bo": "đã loại"}

_TOK = re.compile(r"\w+", re.UNICODE)


def default_dir() -> Path:
    from .config import user_dir

    v = os.environ.get("VT_KNOWLEDGE_DIR", "").strip()
    return Path(v).expanduser() if v else user_dir() / "kien_thuc"


def toks(text: str) -> list[str]:
    return _TOK.findall(nfc(text).lower())


def key_of(text: str) -> str:
    """Dạng so khớp: chữ thường, giữ dấu tiếng Việt, bỏ dấu câu, các từ cách nhau một dấu cách."""
    return " ".join(toks(text))


def _pad(text: str) -> str:
    return " " + key_of(text) + " "


def _has(padded: str, form: str) -> bool:
    k = key_of(form)
    return bool(k) and f" {k} " in padded


def video_id(segments: list[dict]) -> str:
    """Định danh một video theo nội dung transcript thô — học lại cùng video thì ghi đè, không cộng dồn."""
    return sha([s.get("raw", s.get("text", "")) for s in segments])


def _cs_key(text: str) -> str:
    """Như key_of nhưng giữ hoa/thường."""
    return " ".join(_TOK.findall(nfc(text)))


def _case_sensitive(form: str, proper: bool) -> bool:
    return proper and any(c.isupper() for c in form)


def _in(form: str, proper: bool, grams: set[str], cs_grams: set[str]) -> bool:
    if _case_sensitive(form, proper):
        return _cs_key(form) in cs_grams
    return key_of(form) in grams


def _form_re(form: str, proper: bool) -> re.Pattern | None:
    words = _TOK.findall(nfc(form))
    if not words:
        return None
    return re.compile(r"(?<!\w)" + r"\W+".join(map(re.escape, words)) + r"(?!\w)",
                      0 if _case_sensitive(form, proper) else re.IGNORECASE)


def _snip(text: str, m: re.Match | None, width: int = 90, mark: bool = True) -> str:
    text = nfc(text)
    if not m:
        return text[: 2 * width] + ("…" if len(text) > 2 * width else "")
    a, b = max(0, m.start() - width), min(len(text), m.end() + width)
    mid = f"«{m.group(0)}»" if mark else m.group(0)
    return ("…" if a else "") + text[a : m.start()] + mid + text[m.end() : b] + ("…" if b < len(text) else "")


def _contexts(texts: list[str], forms: list[str], proper: bool, n: int = 2) -> list[str]:
    pats = [r for f in forms if (r := _form_re(f, proper))]
    out = []
    for t in texts:
        m = next((m for r in pats if (m := r.search(nfc(t)))), None)
        if m:
            out.append(_snip(t, m))
            if len(out) >= n:
                break
    return out


def _grams(texts: list[str], n_max: int = 5, lower: bool = True) -> set[str]:
    words = [w for t in texts for w in (toks(t) if lower else _TOK.findall(nfc(t)))]
    out: set[str] = set()
    for n in range(1, n_max + 1):
        for i in range(len(words) - n + 1):
            g = words[i : i + n]
            out.add(" ".join(g))
            if 2 <= n <= 3:
                out.add("".join(g))  # "bing x" cũng khớp "bingx"
    return out


# ----------------------------------------------------------------------------------------------
# Trạng thái một mục từ điển (tính từ bằng chứng, không lưu)
# ----------------------------------------------------------------------------------------------


def status(e: dict) -> str:
    """đang dùng: người dùng khai; hoặc lặp lại ở ≥2 video có căn cứ chắc (màn hình / model chắc chắn ở cả 2);
    hoặc ≥3 video. Luôn cần phản bác ít hơn một nửa số ủng hộ. Một video đơn lẻ không bao giờ đủ."""
    sup = e.get("ung_ho", {})
    n, pb = len(sup), len(e.get("phan_bac", {}))
    if pb >= 2 and pb >= n:
        return "loai_bo"
    if pb * 2 >= max(n, 1):
        return "ung_vien"
    if any(r.get("nguoi_dung") for r in sup.values()) or n >= 3:
        return "dang_dung"
    sure = sum(bool(r.get("man_hinh") or r.get("chac")) for r in sup.values())
    return "dang_dung" if n >= 2 and sure >= 2 else "ung_vien"


def distinctive(form: str) -> bool:
    """Biến thể đủ đặc trưng để gợi ý dù mục còn là ứng viên: nhiều từ, có chữ số hoặc chữ hoa. Một từ tiếng Việt
    thường ("đẹp", "cam") khớp ở gần như mọi video nên chỉ được gợi ý khi mục đã 'đang dùng'."""
    return len(toks(form)) >= 2 or any(c.isdigit() or c.isupper() for c in form)


def _clean_form(text: str) -> str:
    return " ".join(nfc(text).replace("‹", " ").replace("›", " ").split()).strip(" .,;:")


def variants(e: dict) -> list[str]:
    c = Counter(v for r in e.get("ung_ho", {}).values() for v in r.get("bien_the", []))
    return [v for v, _ in c.most_common()]


def domains(e: dict) -> list[str]:
    c = Counter(r.get("linh_vuc") for r in e.get("ung_ho", {}).values() if r.get("linh_vuc"))
    return [d for d, _ in c.most_common()]


def _evidence(e: dict) -> str:
    sup = e.get("ung_ho", {}).values()
    out = []
    if any(r.get("nguoi_dung") for r in sup):
        out.append("người dùng khai")
    if any(r.get("man_hinh") for r in sup):
        out.append("thấy trên màn hình")
    if (n := sum(bool(r.get("chac")) for r in sup)):
        out.append(f"model chắc chắn ×{n}")
    if (n := sum(r.get("lan_sua", 0) for r in sup)):
        out.append(f"đã sửa {n} chỗ")
    return ", ".join(out) or "—"


# ----------------------------------------------------------------------------------------------
# Luật thể loại (file .md do team soạn) + ghi chú đã học
# ----------------------------------------------------------------------------------------------


def preset_path(genre: str, root: Path | None) -> Path | None:
    for d in ([Path(root) / "the_loai"] if root else []) + [PKG_PRESETS]:
        f = d / f"{genre}.md"
        if f.is_file():
            return f
    return None


def load_preset(genre: str, root: Path | None) -> str:
    f = preset_path(genre, root)
    if not f:
        return ""
    lines = f.read_text(encoding="utf-8").splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]  # bỏ tiêu đề
    return "\n".join(lines).strip()


def known_genres(kb: "Knowledge | None") -> dict[str, str]:
    """Thể loại có sẵn + thể loại máy đã tự đặt ở các video trước ("khac" luôn đứng cuối)."""
    out = {k: v for k, v in GENRES.items() if k != "khac"}
    for g, r in (kb.data.get("the_loai_moi", {}) if kb else {}).items():
        out.setdefault(g, f"{r.get('mo_ta') or g} (thể loại máy tự đặt, {len(r.get('video', {}))} video)")
    out["khac"] = GENRES["khac"] + " / không đủ thông tin"
    return out


def resolve_genre(slug: str, known: dict[str, str]) -> str:
    """Chuẩn hóa mã thể loại model trả về; mã mới gần trùng mã đã có thì gộp vào mã đã có."""
    g = "_".join(f for w in re.split(r"[\W_]+", nfc(slug or "")) if (f := re.sub(r"[^a-z0-9]", "", fold(w))))
    if g in known:
        return g
    if not SLUG.match(g):
        return "khac"
    near = max(known, key=lambda k: difflib.SequenceMatcher(None, g, k).ratio())
    return near if difflib.SequenceMatcher(None, g, near).ratio() >= 0.8 else g


def genre_label(genre: str, kb: "Knowledge | None" = None) -> str:
    if genre in GENRES:
        return GENRES[genre]
    r = (kb.data.get("the_loai_moi", {}) if kb else {}).get(genre) or {}
    return f"{genre}: {r['mo_ta']}" if r.get("mo_ta") else genre


def genre_rules(genre: str, kb: "Knowledge | None") -> str:
    """Khối luật riêng cho thể loại, chèn vào system prompt bước sửa từng cụm."""
    preset = load_preset(genre, kb.root if kb else None)
    notes = kb.active_notes(genre) if kb else []
    if not preset and not notes:
        return ""
    out = f"\nLUẬT RIÊNG CHO THỂ LOẠI NÀY ({genre_label(genre, kb)})\n"
    if preset:
        out += preset + "\n"
    if notes:
        out += "Ghi chú văn phong rút ra từ các video cùng thể loại trước đây:\n" + "\n".join(f"- {t}" for t in notes) + "\n"
    return out


# ----------------------------------------------------------------------------------------------
# Khối prompt từ kho
# ----------------------------------------------------------------------------------------------


def hints_block(hits: list[dict]) -> str:
    """Gợi ý cho bước lập từ điển: các mục trong kho có biến thể/cách viết xuất hiện trong transcript này."""
    if not hits:
        return ""
    rows = []
    for h in hits:
        v = ", ".join(f"“{x}”" for x in h["bien_the_gap"][:6]) or "(lần này chỉ thấy cách viết đúng)"
        rows.append(f"- {h['canonical']} [{h['loai']}] ← {v} · lĩnh vực: {', '.join(h['linh_vuc']) or '?'} · "
                    f"{h['n']} video ủng hộ, {h['pb']} phản bác · {TRANG_THAI[h['trang_thai']]}")
    return ("## Sổ tay: lỗi ASR đã gặp ở các video trước (đã lọc: có xuất hiện và hợp ngữ cảnh video này)\n"
            "Chỉ là GỢI Ý. Mục nào đúng thì đưa vào entries (variants là những biến thể THỰC SỰ có trong transcript). "
            "Chỗ nào cụm đó mang nghĩa khác (từ thông thường) thì không tính là biến thể.\n"
            + "\n".join(rows) + "\n\n")


def correct_block(hits: list[dict], domain: str, gl: dict) -> str:
    """Mục 'đang dùng' cùng lĩnh vực mà bước lập từ điển không nhắc tới — đưa thêm vào bước sửa từng cụm."""
    have = {fold(e.get("canonical", "")) for e in gl.get("entries", [])}
    rows = []
    for h in hits:
        if h["trang_thai"] != "dang_dung" or not h["bien_the_gap"] or fold(h["canonical"]) in have:
            continue
        if h["linh_vuc"] and domain not in h["linh_vuc"]:
            continue
        rows.append(f"- {h['canonical']} ← " + ", ".join(f"“{x}”" for x in h["bien_the_gap"]))
    if not rows:
        return ""
    return ("## Sổ tay (đã kiểm chứng ở nhiều video trước, cùng lĩnh vực) — chỉ sửa khi đúng ngữ cảnh câu\n"
            + "\n".join(rows) + "\n\n")


def vet(cands: list[dict], notes_new: list[str], topic: str, genre: str, llm, cache_dir: Path | None) -> dict:
    """Lượt model rẻ duyệt đề xuất trước khi ghi vào kho. Trả về {"entries": {key: biến thể giữ lại},
    "notes": [ghi chú được học], "bo": [(cách viết, lý do)]}. Mục do người dùng khai không cần duyệt.
    Lỗi gọi model thì không học gì từ video này (thà bỏ sót còn hơn học sai)."""
    from .llm import LLMError

    out = {"entries": {c["key"]: c["bien_the"] for c in cands if c["nguoi_dung"]}, "notes": [], "bo": [], "bo_ghi_chu": 0}
    ids = {f"m{i + 1}": c for i, c in enumerate(c for c in cands if not c["nguoi_dung"])}
    nids = {f"g{i + 1}": t for i, t in enumerate(notes_new)}
    if not ids and not nids:
        return out
    prompt = P.vet_prompt(topic, genre, [dict(c, id=i) for i, c in ids.items()],
                          [{"id": i, "text": t} for i, t in nids.items()])
    f = Path(cache_dir) / f"vet_{sha(P.VET_SYSTEM, prompt, llm.model)}.json" if cache_dir else None
    try:
        if f and f.is_file():
            res = read_json(f)
        else:
            log(f"  Sổ tay: duyệt {len(ids)} mục, {len(nids)} ghi chú đề xuất…")
            res = llm.ask(prompt, system=P.VET_SYSTEM, schema=P.VET_SCHEMA, effort="medium", label="duyệt sổ tay")
            if f:
                write_json(f, res)
    except LLMError as e:
        log(f"  ! Duyệt sổ tay lỗi, không học mục nào từ video này: {e}")
        out["bo"] = [(c["canonical"], "lỗi gọi model duyệt") for c in ids.values()]
        return out
    done = set()
    for x in res.get("entries") or []:
        c = ids.get(x.get("id"))
        if not c or x.get("id") in done:
            continue
        done.add(x["id"])
        keep_keys = {key_of(v) for v in x.get("variants_keep") or []}
        keep = [v for v in c["bien_the"] if key_of(v) in keep_keys]
        if x.get("verdict") == "hoc" and (keep or not c["bien_the"] or c["man_hinh"]):
            out["entries"][c["key"]] = keep
        else:
            out["bo"].append((c["canonical"], x.get("reason") or ("không còn biến thể hợp lệ" if
                                                                  x.get("verdict") == "hoc" else "")))
    out["bo"] += [(c["canonical"], "model duyệt không trả lời") for i, c in ids.items() if i not in done]
    ok_notes = {x.get("id") for x in res.get("notes") or [] if x.get("verdict") == "hoc"}
    out["notes"] = [t for i, t in nids.items() if i in ok_notes]
    out["bo_ghi_chu"] = len(nids) - len(out["notes"])
    return out


# ----------------------------------------------------------------------------------------------
# Kho
# ----------------------------------------------------------------------------------------------


class Knowledge:
    def __init__(self, root: Path | None = None):
        self.root = Path(root or default_dir()).expanduser().resolve()
        self.dict_path = self.root / "tu_dien.json"
        self.notes_path = self.root / "the_loai.json"
        self.load()

    # --- đọc/ghi ---

    def load(self) -> None:
        self.data = read_json(self.dict_path) if self.dict_path.is_file() else {}
        self.data.setdefault("phien_ban", 1)
        self.data.setdefault("muc", {})
        self.data.setdefault("video", {})
        self.data.setdefault("chan", [])
        self.data.setdefault("the_loai_moi", {})
        self.notes = read_json(self.notes_path) if self.notes_path.is_file() else {}

    def save(self) -> None:
        write_json(self.dict_path, self.data)
        write_json(self.notes_path, self.notes)
        write_text(self.root / "SO_TAY.md", self.render())

    @contextmanager
    def _locked(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.root / ".khoa", "w") as f:
            try:
                import fcntl

                fcntl.flock(f, fcntl.LOCK_EX)
            except ImportError:  # Windows: không khóa
                pass
            self.load()
            yield

    def _snapshot(self) -> None:
        if not self.dict_path.is_file() and not self.notes_path.is_file():
            return
        d = self.root / "lich_su" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        d.mkdir(parents=True, exist_ok=True)
        for f in (self.dict_path, self.notes_path):
            if f.is_file():
                shutil.copy2(f, d / f.name)
        for old in self.snapshots()[:-KEEP_SNAPSHOTS]:
            shutil.rmtree(old, ignore_errors=True)

    def snapshots(self) -> list[Path]:
        d = self.root / "lich_su"
        return sorted(p for p in d.iterdir() if p.is_dir()) if d.is_dir() else []

    # --- dùng kho khi chuẩn hóa ---

    def match(self, texts: list[str], limit: int = MAX_HINTS) -> list[dict]:
        """Các mục (chưa bị loại) có biến thể hoặc cách viết đúng xuất hiện trong transcript."""
        grams, cs = _grams(texts), _grams(texts, lower=False)
        hits = []
        for k, e in self.data["muc"].items():
            st = status(e)
            if st == "loai_bo":
                continue
            proper = e.get("loai") in PROPER
            found = [v for v in variants(e) if len(key_of(v)) >= 2 and _in(v, proper, grams, cs)]
            if st != "dang_dung":
                found = [v for v in found if distinctive(v)]
            canon = e["canonical"]
            if not found and not (_in(canon, proper, grams, cs) or _in(canon.replace(" ", ""), proper, grams, cs)):
                continue
            sup = e.get("ung_ho", {})
            hits.append({"key": k, "canonical": canon, "loai": e.get("loai", "other"), "bien_the_gap": found,
                         "trang_thai": st, "n": len(sup), "pb": len(e.get("phan_bac", {})), "linh_vuc": domains(e),
                         "lan_sua": sum(r.get("lan_sua", 0) for r in sup.values()),
                         "ngu_canh": _contexts(texts, found or [canon], proper)})
        hits.sort(key=lambda h: (h["trang_thai"] != "dang_dung", not h["bien_the_gap"], -h["n"], -h["lan_sua"]))
        return hits[:limit]

    def notes_block(self, genre: str) -> str:
        """Ghi chú đã học của đúng thể loại video này, cho bước lập từ điển xác nhận hoặc thêm mới."""
        head = f"## Ghi chú văn phong đã học cho thể loại {genre_label(genre, self)}"
        top = sorted(self.notes.get(genre, {}).items(), key=lambda kv: (-len(kv[1]["video"]), kv[1].get("tao", "")))
        if not top:
            return head + "\n(chưa có)\n\n"
        return head + " ([id] (số video xác nhận) nội dung)\n" + \
            "\n".join(f"- [{nid}] ({len(n['video'])} video) {n['text']}" for nid, n in top[:15]) + "\n\n"

    def active_notes(self, genre: str) -> list[str]:
        notes = [n for n in self.notes.get(genre, {}).values() if len(n["video"]) >= 2]
        notes.sort(key=lambda n: (-len(n["video"]), n.get("tao", "")))
        return [n["text"] for n in notes[:ACTIVE_NOTES]]

    # --- học ---

    def _forget(self, vid: str) -> None:
        for k in list(self.data["muc"]):
            e = self.data["muc"][k]
            e.get("ung_ho", {}).pop(vid, None)
            e.get("phan_bac", {}).pop(vid, None)
            if not e.get("ung_ho") and not e.get("phan_bac"):
                del self.data["muc"][k]
        for g in list(self.notes):
            for nid in list(self.notes[g]):
                self.notes[g][nid]["video"].pop(vid, None)
                if not self.notes[g][nid]["video"]:
                    del self.notes[g][nid]
            if not self.notes[g]:
                del self.notes[g]
        for g in list(self.data["the_loai_moi"]):
            self.data["the_loai_moi"][g].get("video", {}).pop(vid, None)
            if not self.data["the_loai_moi"][g].get("video"):
                del self.data["the_loai_moi"][g]
        self.data["video"].pop(vid, None)

    def candidates(self, norm: dict, visual: dict | None, user_gl: list[dict] | None = None) -> list[dict]:
        """Mục từ điển đề xuất học từ một video (chưa duyệt): biến thể có trong bản thô và bản cuối thực sự đã sửa;
        hoặc luôn viết đúng nhưng chính tả có căn cứ chắc (màn hình / người dùng khai)."""
        gl = norm.get("glossary") or {}
        segs = norm["segments"]
        texts = [(s.get("raw", ""), s.get("text", "")) for s in segs]
        pairs = [(_pad(r), _pad(f)) for r, f in texts]
        all_raw = " ".join(r for r, _ in pairs)
        all_final = " ".join(f for _, f in pairs)
        vis_blob = _pad(" | ".join(x for k in ("people", "organizations", "products", "terms", "slide_lines")
                                   for x in (visual or {}).get(k) or [] if isinstance(x, str)))
        user_keys = {fold(u["canonical"]) for u in user_gl or []}
        out, keys = [], set()
        for e in gl.get("entries", []):
            canon = _clean_form(e.get("canonical") or "")
            ck, k = key_of(canon), fold(canon)
            if len(ck) < 2 or not k or k in self.data["chan"] or k in keys:
                continue
            user = k in user_keys
            if e.get("confidence") == "low" and not user:
                continue
            vs = list(dict.fromkeys(_clean_form(v) for v in e.get("variants") or []
                                    if key_of(v) not in ("", ck) and len(key_of(v)) >= 2 and _has(all_raw, v)))
            fix_idx = [i for i, (r, f) in enumerate(pairs) if _has(f, canon) and any(_has(r, v) for v in vs)]
            screen = _has(vis_blob, canon)
            if vs and not fix_idx:
                continue  # từ điển nói có lỗi nhưng bản cuối không sửa: chưa đủ căn cứ
            if not vs and (not (screen or user) or not _has(all_final, canon)):
                continue  # luôn viết đúng: chỉ giữ khi chắc chắn về chính tả (màn hình / người dùng khai)
            if vs:
                pv = [r for v in vs if (r := _form_re(v, False))]
                pc = _form_re(canon, False)
                ex = [(_snip(texts[i][0], next((m for r in pv if (m := r.search(nfc(texts[i][0])))), None)),
                       _snip(texts[i][1], pc.search(nfc(texts[i][1])) if pc else None)) for i in fix_idx[:2]]
            else:
                pc = _form_re(canon, False)
                ex = [(_snip(r, None), _snip(f, m)) for r, f in texts if pc and (m := pc.search(nfc(f)))][:1]
            keys.add(k)
            out.append({"key": k, "canonical": canon, "loai": e.get("type") or "other", "bien_the": vs,
                        "lan_sua": len(fix_idx), "man_hinh": screen, "nguoi_dung": user,
                        "chac": e.get("confidence") == "high", "vi_du": ex})
        return out

    def learn(self, norm: dict, visual: dict | None, video_name: str, user_gl: list[dict] | None = None,
              llm=None, cache_dir: Path | None = None, genre: str | None = None, domain: str | None = None) -> dict:
        """Cập nhật kho từ kết quả chuẩn hóa của một video. Học lại cùng video thì thay bằng chứng cũ.
        `llm` (model rẻ): duyệt các mục/ghi chú đề xuất trước khi ghi. None = không duyệt (chỉ dùng khi thử)."""
        gl = norm.get("glossary") or {}
        st_in = norm.get("so_tay") or {}
        segs = norm["segments"]
        vid = video_id(segs)
        genre = genre or st_in.get("the_loai") or gl.get("genre") or "khac"
        domain = domain or st_in.get("linh_vuc") or gl.get("domain") or "khac"
        accepted = set(st_in.get("ap_dung") or [])
        today = datetime.now().strftime("%Y-%m-%d")
        pairs = [(_pad(s.get("raw", "")), _pad(s.get("text", ""))) for s in segs]
        notes_new = [t for x in (gl.get("genre_notes_new") or [])[:2]
                     if 10 <= len(t := " ".join(nfc(x).split())) <= 300]
        cands = self.candidates(norm, visual, user_gl)
        if llm is not None:
            v = vet(cands, notes_new, gl.get("topic", ""), genre_label(genre, self), llm, cache_dir)
        else:
            v = {"entries": {c["key"]: c["bien_the"] for c in cands}, "notes": notes_new, "bo": [], "bo_ghi_chu": 0}
        stats = Counter(de_xuat=len(cands), bi_loai=len(v["bo"]), ghi_chu_bi_loai=v.get("bo_ghi_chu", 0))
        if cache_dir:  # để kiểm tra lại máy đã học/loại gì từ video này
            write_json(Path(cache_dir) / "so_tay_hoc.json",
                       {"de_xuat": cands, "hoc": v["entries"], "bo": v["bo"], "ghi_chu_hoc": v["notes"],
                        "ghi_chu_de_xuat": notes_new})

        with self._locked():
            self._snapshot()
            self._forget(vid)
            seen = set()
            for c in cands:
                k = c["key"]
                if k not in v["entries"] or k in self.data["chan"]:
                    continue
                vs = v["entries"][k]
                fixes = sum(1 for r, f in pairs if _has(f, c["canonical"]) and any(_has(r, x) for x in vs)) if vs else 0
                item = self.data["muc"].get(k)
                if item is None:
                    item = self.data["muc"][k] = {"canonical": c["canonical"], "loai": c["loai"],
                                                  "ung_ho": {}, "phan_bac": {}}
                    stats["moi"] += 1
                elif c["man_hinh"] or c["nguoi_dung"]:
                    item["canonical"] = c["canonical"]
                item["ung_ho"][vid] = {"bien_the": vs, "man_hinh": c["man_hinh"], "nguoi_dung": c["nguoi_dung"],
                                       "chac": c["chac"], "lan_sua": fixes, "linh_vuc": domain, "ngay": today}
                seen.add(k)
                stats["ung_ho"] += 1

            # Phản bác: lượt phân tích đã xác nhận gợi ý đúng ngữ cảnh video này, vậy mà bản cuối vẫn để nguyên biến thể.
            for k in accepted - seen:
                item = self.data["muc"].get(k)
                if not item:
                    continue
                ck = item["canonical"]
                unfixed = [x for x in variants(item)
                           if any(_has(r, x) and _has(f, x) and not _has(f, ck) for r, f in pairs)]
                if unfixed:
                    item["phan_bac"][vid] = {"bien_the": unfixed, "linh_vuc": domain, "ngay": today}
                    stats["phan_bac"] += 1

            if genre not in GENRES:
                r = self.data["the_loai_moi"].setdefault(genre, {"mo_ta": "", "video": {}, "tao": today})
                r["mo_ta"] = r.get("mo_ta") or (st_in.get("mo_ta_the_loai") or "")[:200]
                r["video"][vid] = today
            notes = self.notes.setdefault(genre, {})
            for nid in gl.get("genre_notes_endorsed") or []:
                if nid in notes:
                    notes[nid]["video"][vid] = today
                    stats["ghi_chu_xac_nhan"] += 1
            for text in v["notes"]:
                dup = max(notes.items(), default=None,
                          key=lambda kv: difflib.SequenceMatcher(None, kv[1]["text"].lower(), text.lower()).ratio())
                if dup and difflib.SequenceMatcher(None, dup[1]["text"].lower(), text.lower()).ratio() > 0.75:
                    dup[1]["video"][vid] = today
                    stats["ghi_chu_xac_nhan"] += 1
                else:
                    notes[f"{genre}-{sha(text)[:6]}"] = {"text": text, "video": {vid: today}, "tao": today}
                    stats["ghi_chu_moi"] += 1
            if len(notes) > MAX_NOTES_PER_GENRE:
                keep = sorted(notes.items(), key=lambda kv: (-len(kv[1]["video"]), kv[1].get("tao", "")))
                self.notes[genre] = dict(keep[:MAX_NOTES_PER_GENRE])
            if not self.notes.get(genre):
                self.notes.pop(genre, None)

            self.data["video"][vid] = {"ten": video_name, "the_loai": genre, "linh_vuc": domain, "ngay": today,
                                       "so_muc": stats["ung_ho"]}
            self.save()
        return dict(stats)

    def remove(self, word: str) -> bool:
        with self._locked():
            k = fold(word)
            self._snapshot()
            found = self.data["muc"].pop(k, None) is not None
            if k not in self.data["chan"]:
                self.data["chan"].append(k)
            self.save()
        return found

    def restore(self, name: str | None = None) -> Path:
        snaps = self.snapshots()
        if not snaps:
            raise FileNotFoundError("Chưa có ảnh chụp nào trong lich_su/")
        snap = next((s for s in snaps if s.name == name), None) if name else snaps[-1]
        if snap is None:
            raise FileNotFoundError(f"Không có bản {name} trong lich_su/")
        with self._locked():
            for f in (self.dict_path, self.notes_path):
                src = snap / f.name
                if src.is_file():
                    shutil.copy2(src, f)
                elif f.exists():
                    f.unlink()
            self.load()
            self.save()
        return snap

    # --- bản cho người đọc ---

    def render(self) -> str:
        by_status: dict[str, list] = {"dang_dung": [], "ung_vien": [], "loai_bo": []}
        for e in self.data["muc"].values():
            by_status[status(e)].append(e)
        vids = self.data["video"]
        L = ["# Sổ tay chuẩn hóa (tự học)", "",
             "> File sinh tự động sau mỗi video, đừng sửa tay. Xóa một mục: `vtranscript-sotay xoa <từ>`. "
             "Trả kho về bản trước: `vtranscript-sotay quay-lai`.", "",
             f"Cập nhật {datetime.now():%Y-%m-%d %H:%M} · {len(vids)} video đã học · "
             f"{len(by_status['dang_dung'])} mục đang dùng, {len(by_status['ung_vien'])} ứng viên, "
             f"{len(by_status['loai_bo'])} đã loại.", ""]
        heads = {"dang_dung": "Từ điển — đang dùng (đưa thẳng vào bước sửa khi cùng lĩnh vực)",
                 "ung_vien": "Từ điển — ứng viên (chỉ gợi ý cho bước lập từ điển)",
                 "loai_bo": "Từ điển — đã loại (bị phản bác nhiều)"}
        for st, items in by_status.items():
            L += [f"## {heads[st]}", ""]
            if not items:
                L += ["Chưa có.", ""]
                continue
            L += ["| Cách viết đúng | Loại | Biến thể ASR hay nghe nhầm | Lĩnh vực | Ủng hộ | Phản bác | Căn cứ |",
                  "|---|---|---|---|---|---|---|"]
            items.sort(key=lambda e: (-len(e.get("ung_ho", {})), e["canonical"].lower()))
            for e in items:
                v = ", ".join(variants(e)[:8]) or "—"
                L.append(f"| **{e['canonical']}** | {e.get('loai', '')} | {v} | {', '.join(domains(e)) or '—'} | "
                         f"{len(e.get('ung_ho', {}))} | {len(e.get('phan_bac', {}))} | {_evidence(e)} |".replace("\n", " "))
            L.append("")
        L += ["## Ghi chú văn phong theo thể loại", "",
              "Ghi chú có hiệu lực khi ≥2 video xác nhận; mỗi video dùng tối đa 5 ghi chú mạnh nhất.", ""]
        if not self.notes:
            L += ["Chưa có.", ""]
        for g, notes in sorted(self.notes.items()):
            L += [f"### {genre_label(g, self)}", ""]
            for nid, n in sorted(notes.items(), key=lambda kv: -len(kv[1]["video"])):
                on = "có hiệu lực" if len(n["video"]) >= 2 else "chờ xác nhận"
                L.append(f"- `{nid}` ({len(n['video'])} video, {on}) {n['text']}")
            L.append("")
        L += ["## Luật thể loại do team soạn", "",
              "Sửa luật cho một thể loại: chép file trong gói vào `the_loai/<thể loại>.md` của kho này rồi sửa.", ""]
        for g in GENRES:
            f = preset_path(g, self.root)
            if f:
                src = "kho" if f.parent != PKG_PRESETS else "gói"
                L.append(f"- {GENRES[g]}: `{g}.md` ({src})")
        new = self.data.get("the_loai_moi") or {}
        if new:
            L += ["", "### Thể loại mới do máy tự đặt", "",
                  "Máy gặp video không hợp thể loại nào có sẵn nên đặt mã mới. Ghi chú văn phong vẫn được học theo mã "
                  "này; muốn thêm luật của team thì tạo `the_loai/<mã>.md` trong kho.", ""]
            for g, r in sorted(new.items(), key=lambda kv: -len(kv[1].get("video", {}))):
                f = preset_path(g, self.root)
                L.append(f"- `{g}` — {r.get('mo_ta') or '(chưa có mô tả)'} · {len(r.get('video', {}))} video · "
                         + (f"có luật `{f.name}`" if f else "chưa có luật của team"))
        L += ["", "## Video đã học", ""]
        if vids:
            L += ["| Ngày | Video | Thể loại | Lĩnh vực | Mục ủng hộ |", "|---|---|---|---|---|"]
            for v in sorted(vids.values(), key=lambda v: v["ngay"], reverse=True):
                L.append(f"| {v['ngay']} | {v['ten']} | {v['the_loai']} | {v['linh_vuc']} | {v.get('so_muc', 0)} |")
        else:
            L.append("Chưa có.")
        if self.data["chan"]:
            L += ["", "## Từ bị chặn (không học lại)", "", ", ".join(self.data["chan"])]
        return "\n".join(L) + "\n"


# ----------------------------------------------------------------------------------------------
# Lệnh quản lý: vtranscript-sotay
# ----------------------------------------------------------------------------------------------


def _learn_dir(kb: Knowledge, d: Path, genre: str | None, domain: str | None) -> dict:
    w = d / "_work"
    norm = read_json(w / "normalized.json")
    visual = read_json(w / "visual_context.json") if (w / "visual_context.json").is_file() else None
    meta = d / "02_transcript_chuan_hoa.json"
    name = read_json(meta).get("video", d.name) if meta.is_file() else d.name
    llm = None
    try:
        from .llm import make_llm

        llm = make_llm(os.environ.get("VT_LLM_BACKEND", "auto"), os.environ.get("VT_LLM_MODEL") or None)
    except Exception as e:  # noqa: BLE001 - không có key/model thì học không duyệt
        print(f"  (không gọi được model duyệt, học không duyệt: {e})", file=sys.stderr)
    return kb.learn(norm, visual, name, llm=llm, cache_dir=w / "normalize", genre=genre, domain=domain)


def main(argv=None) -> int:
    from . import config

    p = argparse.ArgumentParser(prog="vtranscript-sotay", description="Quản lý sổ tay tự học của bước chuẩn hóa.")
    p.add_argument("--knowledge", type=Path, default=None, help="Thư mục kho (mặc định VT_KNOWLEDGE_DIR hoặc "
                                                                 "~/.config/vtranscript/kien_thuc)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("xem", help="Tóm tắt kho")
    x = sub.add_parser("xoa", help="Xóa mục và chặn không học lại")
    x.add_argument("tu", nargs="+")
    q = sub.add_parser("quay-lai", help="Trả kho về một ảnh chụp trong lich_su/")
    q.add_argument("ban", nargs="?", help="Tên thư mục ảnh chụp (mặc định bản gần nhất)")
    h = sub.add_parser("hoc", help="Học từ các lần chạy đã có")
    h.add_argument("dirs", nargs="+", type=Path, help="Thư mục kết quả (có _work/normalized.json)")
    h.add_argument("--the-loai", default=None,
                   help="Ghi đè thể loại (lần chạy cũ chưa có thông tin này)")
    h.add_argument("--linh-vuc", choices=list(DOMAINS), default=None, help="Ghi đè lĩnh vực")
    a = p.parse_args(argv)
    config.load()
    kb = Knowledge(a.knowledge)

    if a.cmd == "hoc":
        for d in a.dirs:
            if not (d / "_work" / "normalized.json").is_file():
                print(f"Bỏ qua {d}: không có _work/normalized.json", file=sys.stderr)
                continue
            st = _learn_dir(kb, d, a.the_loai, a.linh_vuc)
            print(f"{d.name}: {st.get('ung_ho', 0)} mục ủng hộ ({st.get('moi', 0)} mới), "
                  f"{st.get('phan_bac', 0)} phản bác, {st.get('ghi_chu_moi', 0)} ghi chú mới")
    elif a.cmd == "xoa":
        for w in a.tu:
            print(f"{w}: {'đã xóa' if kb.remove(w) else 'không có trong kho'}, đã chặn học lại")
    elif a.cmd == "quay-lai":
        try:
            snap = kb.restore(a.ban)
        except FileNotFoundError as e:
            print(e, file=sys.stderr)
            return 1
        print(f"Đã trả kho về bản {snap.name}")
    kb.load()
    c = Counter(status(e) for e in kb.data["muc"].values())
    print(f"Kho: {kb.root}\n  {len(kb.data['video'])} video · {c['dang_dung']} mục đang dùng, {c['ung_vien']} ứng viên, "
          f"{c['loai_bo']} đã loại · {sum(len(n) for n in kb.notes.values())} ghi chú thể loại\n"
          f"  Xem chi tiết: {kb.root / 'SO_TAY.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
