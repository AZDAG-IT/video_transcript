"""Prompt và JSON schema cho các bước dùng LLM.

Chuẩn hóa chạy 4 vòng (+ 1 lượt duyệt khi học):
  0. ANALYZE   — model rẻ: thể loại/lĩnh vực/giọng, lọc gợi ý sổ tay theo câu trích (chống khớp bừa).
  1. GLOSSARY  — đọc toàn bộ transcript thô + chữ trên màn hình -> lập từ điển tên riêng/thuật ngữ và các biến thể bị nghe nhầm.
  2. CORRECT   — hiệu đính từng cụm đoạn (có ngữ cảnh trước/sau + từ điển), giữ nguyên id từng đoạn để khớp timestamp.
  3. REVIEW    — soát toàn cục trên bản đã sửa: tính nhất quán giữa các cụm, lỗi sót, sửa quá tay.
  VET          — model rẻ: duyệt mục/ghi chú code đề xuất trước khi ghi vào sổ tay tự học.
"""

from __future__ import annotations

import json

LOW_CONF_OPEN, LOW_CONF_CLOSE = "‹", "›"

# Thể loại video (chọn luật riêng ở vtranscript/the_loai/<mã>.md) và lĩnh vực (để sổ tay tự học không đem lỗi của
# lĩnh vực này áp sang lĩnh vực khác). Bước lập từ điển tự nhận diện.
GENRES = {
    "hop": "họp nội bộ, họp với khách hàng/đối tác",
    "dao_tao": "đào tạo nội bộ, hướng dẫn quy trình/sản phẩm cho nhân viên",
    "bai_giang": "bài giảng, khóa học, hướng dẫn kỹ thuật, giải bài",
    "phong_van": "phỏng vấn, podcast, talkshow, tọa đàm",
    "livestream": "livestream bán hàng, giới thiệu sản phẩm trực tiếp",
    "tin_tuc": "tin tức, phóng sự, thời sự",
    "vlog": "vlog, review, giải trí, hài",
    "khac": "khác",
}
DOMAINS = {
    "kinh_doanh": "kinh doanh, bán hàng, marketing, nhân sự",
    "tai_chinh": "tài chính, ngân hàng, chứng khoán, crypto",
    "cong_nghe": "công nghệ thông tin, phần mềm, phần cứng, AI",
    "y_te": "y tế, sức khỏe, dược",
    "giao_duc": "giáo dục, kỹ năng (chung)",
    "khoa_hoc": "khoa học tự nhiên (toán, lý, hóa, sinh)",
    "ky_thuat": "kỹ thuật, sản xuất, xây dựng",
    "phap_luat": "pháp luật, hành chính",
    "xa_hoi": "thời sự, chính trị, xã hội, môi trường",
    "giai_tri": "giải trí, âm nhạc, phim, game",
    "the_thao": "thể thao",
    "du_lich_am_thuc": "du lịch, ẩm thực",
    "doi_song": "đời sống, gia đình, làm đẹp, thời trang",
    "nong_nghiep": "nông nghiệp",
    "bat_dong_san": "bất động sản",
    "khac": "khác",
}


def _arr(item: dict | None = None) -> dict:
    return {"type": "array", "items": item or {"type": "string"}}


def _obj(props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


UNCERTAIN_ITEM = _obj(
    {
        "id": {"type": "integer"},
        "text": {"type": "string", "description": "cụm từ (trong bản đã sửa) còn chưa chắc chắn"},
        "note": {"type": "string", "description": "vì sao chưa chắc, các phương án khác"},
    }
)

VISUAL_SCHEMA = _obj(
    {
        "people": _arr(),
        "organizations": _arr(),
        "products": _arr(),
        "terms": _arr(),
        "slide_lines": _arr(),
    }
)

GLOSSARY_SCHEMA = _obj(
    {
        "topic": {"type": "string"},
        "setting": {"type": "string"},
        "style_notes": {"type": "string"},
        "speakers": _arr(),
        "entries": _arr(
            _obj(
                {
                    "canonical": {"type": "string"},
                    "variants": _arr(),
                    "type": {"type": "string", "enum": ["person", "organization", "product", "term", "acronym", "other"]},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "note": {"type": "string"},
                }
            )
        ),
        "genre_notes_endorsed": _arr(),
        "genre_notes_new": _arr(),
    }
)

CORRECT_SCHEMA = _obj(
    {
        "segments": _arr(_obj({"id": {"type": "integer"}, "text": {"type": "string"}})),
        "uncertain": _arr(UNCERTAIN_ITEM),
    }
)

REVIEW_SCHEMA = _obj(
    {
        "edits": _arr(
            _obj(
                {
                    "id": {"type": "integer"},
                    "find": {"type": "string", "description": "chuỗi con NGUYÊN VĂN đang có trong đoạn"},
                    "replace": {"type": "string"},
                    "reason": {"type": "string"},
                }
            )
        ),
        "uncertain": _arr(UNCERTAIN_ITEM),
    }
)

# ----------------------------------------------------------------------------------------------
# 0. Đọc chữ trên màn hình
# ----------------------------------------------------------------------------------------------

VISUAL_SYSTEM = """Bạn đọc ảnh chụp màn hình từ một video (họp online, đào tạo, bài giảng, slide, demo phần mềm...) \
và trích xuất CHÍNH XÁC TỪNG KÝ TỰ những chữ có ích cho việc sửa lỗi chính tả transcript của video đó.

Cần lấy:
- people: tên người (tên hiển thị của người tham gia cuộc họp, tên người xuất hiện trên slide/tài liệu).
- organizations: tên công ty, tổ chức, phòng ban, thương hiệu, đối tác.
- products: tên sản phẩm, dự án, chương trình, sự kiện, tính năng, gói dịch vụ.
- terms: thuật ngữ chuyên môn, từ viết tắt, cụm từ tiếng Anh chuyên ngành.
- slide_lines: từng dòng chữ nội dung trên slide/tài liệu đang chia sẻ (tiêu đề, gạch đầu dòng, ghi chú).

Quy tắc:
- Giữ nguyên chính tả, dấu tiếng Việt, hoa/thường, ký hiệu như trên ảnh.
- BỎ QUA chữ của giao diện phần mềm: menu trình duyệt/ứng dụng (File, Edit, View, Bookmarks, Chia sẻ, \
Trình chiếu, Chỉnh sửa, Định dạng...), thanh công cụ, URL, đồng hồ, số người tham gia, thông báo hệ thống.
- Không suy đoán chữ bị mờ/không đọc được. Không lặp lại cùng một mục.
"""


def visual_prompt(times: list[str]) -> str:
    return (
        f"Các ảnh trên được chụp tại các thời điểm {', '.join(times)} của video. "
        "Trích xuất theo đúng schema (people, organizations, products, terms, slide_lines)."
    )


# ----------------------------------------------------------------------------------------------
# Khối ngữ cảnh dùng chung
# ----------------------------------------------------------------------------------------------


def user_glossary_block(user_gl: list[dict]) -> str:
    if not user_gl:
        return ""
    lines = []
    for e in user_gl:
        v = f"  (thường bị nghe thành: {', '.join(e['variants'])})" if e.get("variants") else ""
        lines.append(f"- {e['canonical']}{v}")
    return "## Từ điển do người dùng cung cấp — ƯU TIÊN TUYỆT ĐỐI về cách viết\n" + "\n".join(lines) + "\n\n"


def visual_block(visual: dict | None, max_slide_lines: int = 120) -> str:
    if not visual:
        return ""
    parts = []
    for key, name in (
        ("people", "Tên người"),
        ("organizations", "Tổ chức/thương hiệu"),
        ("products", "Sản phẩm/chương trình/sự kiện"),
        ("terms", "Thuật ngữ"),
    ):
        if visual.get(key):
            parts.append(f"- {name}: " + "; ".join(visual[key]))
    lines = visual.get("slide_lines") or []
    if lines:
        parts.append("- Các dòng chữ trên slide:\n" + "\n".join(f"    · {x}" for x in lines[:max_slide_lines]))
    if not parts:
        return ""
    return "## Chữ đọc được trên màn hình video (nguồn đáng tin về CHÍNH TẢ tên riêng & thuật ngữ)\n" + "\n".join(parts) + "\n\n"


def glossary_block(gl: dict) -> str:
    out = []
    if gl.get("topic"):
        out.append(f"## Bối cảnh video\n{gl['topic']}\n{gl.get('setting', '')}".strip())
    if gl.get("style_notes"):
        out.append(f"## Quy ước viết cho văn bản này\n{gl['style_notes']}")
    if gl.get("speakers"):
        out.append("## Người nói / tham gia\n" + ", ".join(gl["speakers"]))
    entries = gl.get("entries") or []
    if entries:
        rows = []
        for e in entries:
            v = ", ".join(f"“{x}”" for x in e.get("variants") or [])
            conf = "" if e.get("confidence") == "high" else f" (độ tin cậy: {e.get('confidence')})"
            rows.append(f"- {e['canonical']} [{e.get('type')}]{conf}" + (f" ← {v}" if v else ""))
        out.append("## Từ điển chuẩn hóa (cách viết đúng ← các biến thể nghe nhầm đã gặp)\n" + "\n".join(rows))
    return "\n\n".join(out) + "\n\n"


# ----------------------------------------------------------------------------------------------
# 1. Lập từ điển
# ----------------------------------------------------------------------------------------------

GLOSSARY_SYSTEM = """Bạn là chuyên gia hiệu đính transcript tạo bởi hệ thống nhận dạng giọng nói (ASR). \
Văn bản ASR thường viết sai tên người, tên công ty/thương hiệu/sản phẩm, thuật ngữ chuyên ngành, và đặc biệt là \
các từ tiếng Anh chen trong câu tiếng Việt: chúng bị phiên âm thành âm tiết tiếng Việt ("pát nơ" = partner), bị nghe \
thành một từ tiếng Việt có nghĩa nhưng lạc ngữ cảnh, hoặc bị viết sai chính tả tiếng Anh.

Nhiệm vụ ở bước này: đọc TOÀN BỘ transcript để hiểu bối cảnh, rồi lập TỪ ĐIỂN CHUẨN HÓA cho bước sửa lỗi phía sau.
- topic: chủ đề, mục đích video (2–4 câu, cụ thể: tổ chức nào, nói về sản phẩm/quy trình/chương trình gì).
- setting: hình thức (họp online, đào tạo nội bộ, livestream...), ai là người trình bày chính nếu suy ra được, \
giọng vùng miền nổi bật của người nói (Bắc/Trung/Nam) nếu nhận ra.
- style_notes: quy ước viết riêng cho văn bản này (thuật ngữ tiếng Anh nào giữ nguyên, cách viết hoa tên phòng ban, \
cách viết số liệu/tiền tệ/mốc thời gian...).
- speakers: tên những người nói/tham gia, đúng chính tả (ưu tiên theo tên hiển thị trên màn hình).
- entries: mỗi mục là MỘT tên riêng hoặc thuật ngữ quan trọng có xuất hiện trong transcript (viết đúng hoặc bị nghe nhầm):
  • canonical: cách viết chuẩn. Thứ tự ưu tiên: từ điển người dùng > chữ trên màn hình > cách viết chính thức phổ biến.
  • variants: TẤT CẢ dạng sai/biến thể THỰC SỰ có trong transcript, chép NGUYÊN VĂN (để bước sau nhận ra). \
Rỗng nếu transcript luôn viết đúng.
  • type, confidence, note (căn cứ ngắn gọn, nêu id đoạn ví dụ).

Cách suy luận:
- "Nghe lại bằng mắt": một cụm âm tiết vô nghĩa hoặc lạc ngữ cảnh thường là từ tiếng Anh/tên riêng bị phiên âm. \
Đọc to cụm đó, đối chiếu với thuật ngữ của lĩnh vực, chữ trên slide, tên người tham gia.
- Cùng một thực thể có thể bị nghe sai nhiều kiểu ở nhiều chỗ — gom tất cả về một canonical.
- Viết tắt (KPI, AI, CEO...) thường bị ASR viết thành chữ thường hoặc phiên âm từng chữ cái ("kê pi ai").
- Giọng địa phương làm ASR nghe nhầm theo kiểu riêng (v/d/gi, s/x, tr/ch, dấu hỏi/ngã, âm cuối n/ng). Từ địa phương \
người nói thực sự dùng ("hông", "vô", "mô", "răng"...) KHÔNG phải lỗi, không đưa vào từ điển.
- KHÔNG đưa từ thông dụng đã viết đúng vào từ điển. KHÔNG bịa thực thể không có trong transcript. \
Mục chỉ là phỏng đoán thì đặt confidence "low" và giải thích trong note.
- Từ trong ‹ › là chỗ ASR kém tự tin — rất đáng ngờ, nên xem kỹ.
- Nếu có mục "Sổ tay" (lỗi đã gặp ở video trước, đã được lọc theo ngữ cảnh video này): mục nào thực sự xuất hiện thì \
đưa vào entries; cụm nào ở đây mang nghĩa khác thì bỏ qua. Sổ tay chỉ là gợi ý, transcript và màn hình của video này mới \
là căn cứ.

Ghi chú văn phong theo thể loại (giúp các video sau cùng thể loại):
- genre_notes_endorsed: id (trong ngoặc vuông) các ghi chú ĐÃ CÓ của thể loại này mà video này cũng tuân theo. \
Rỗng nếu không có.
- genre_notes_new: tối đa 2 quy ước MỚI về chính tả/văn phong transcript, đúng cho MỌI video cùng thể loại (không chứa \
tên riêng hay chi tiết của riêng video này, không trùng ghi chú đã có). Chỉ thêm khi thật sự hữu ích; thường để rỗng.
"""


def glossary_prompt(lang_name: str, lines: list[str], visual: dict | None, user_gl: list[dict],
                    so_tay: str = "") -> str:
    return (
        f"Ngôn ngữ chính của video: {lang_name}.\n\n"
        + user_glossary_block(user_gl)
        + visual_block(visual)
        + so_tay
        + "## Transcript ASR thô (mỗi dòng: [id] nội dung)\n"
        + "\n".join(lines)
    )


# ----------------------------------------------------------------------------------------------
# 0b. Phân tích video (model rẻ): thể loại, lĩnh vực, giọng, và lọc gợi ý sổ tay theo ngữ cảnh
# ----------------------------------------------------------------------------------------------

ANALYZE_SCHEMA = _obj(
    {
        "topic": {"type": "string"},
        "genre": {"type": "string", "description": "mã thể loại có sẵn, hoặc mã mới chữ thường không dấu nối bằng _"},
        "genre_desc": {"type": "string", "description": "mô tả ngắn nếu genre là mã mới, ngược lại để rỗng"},
        "domain": {"type": "string", "enum": list(DOMAINS)},
        "dialect": {"type": "string"},
        "hints": _arr(_obj({
            "id": {"type": "string"},
            "verdict": {"type": "string", "enum": ["ap_dung", "khong_lien_quan"]},
            "note": {"type": "string"},
        })),
    }
)

ANALYZE_SYSTEM = """Bạn phân loại một video từ transcript ASR (nhận dạng giọng nói tự động, có thể sai chính tả) và chữ \
đọc được trên màn hình, rồi kiểm tra các gợi ý từ sổ tay lỗi ASR của những video trước.

Trả về:
- topic: 1–2 câu, video nói về gì.
- genre: thể loại (HÌNH THỨC video, không phải chủ đề). Chọn đúng một mã trong danh sách được cung cấp. Chỉ khi KHÔNG mã \
nào hợp thì đặt mã mới: chữ thường không dấu, nối bằng "_", 1–3 từ, mô tả hình thức chứ không phải chủ đề \
(ví dụ "doc_truyen", "hoi_dap_truc_tiep"), và ghi genre_desc. Dùng "khac" khi không đủ thông tin để biết.
- domain: lĩnh vực nội dung, chọn một mã trong danh sách.
- dialect: giọng vùng miền nổi bật của người nói (Bắc/Trung/Nam/nhiều giọng/không rõ) và kiểu nhầm lẫn ASR nổi bật nếu \
thấy (ví dụ "giọng Nam: lẫn v/d, dấu hỏi/ngã").
- hints: với MỖI gợi ý sổ tay (id dạng h1, h2…), quyết định:
  • ap_dung: trong các câu trích từ video NÀY, cụm được đánh dấu «…» đúng là cách ASR nghe nhầm thực thể/thuật ngữ đó \
(hoặc thực thể đó thực sự được nhắc tới), và hợp với chủ đề video.
  • khong_lien_quan: cụm «…» ở đây là từ thông thường mang nghĩa khác ("thời gian" không phải tên người "Gian"), \
hoặc thực thể đó không liên quan tới video này. Nếu không chắc thì chọn khong_lien_quan.
  note: căn cứ ngắn (≤15 từ).
"""


def analyze_prompt(lang_name: str, excerpt: list[str], visual_summary: str, genres: dict[str, str],
                   hints: list[dict]) -> str:
    out = [f"Ngôn ngữ chính: {lang_name}.", "",
           "## Thể loại có sẵn (mã: mô tả)", *[f"- {k}: {v}" for k, v in genres.items()], "",
           "## Lĩnh vực (mã: mô tả)", *[f"- {k}: {v}" for k, v in DOMAINS.items()], ""]
    if visual_summary:
        out += ["## Chữ trên màn hình (trích)", visual_summary, ""]
    if hints:
        out.append("## Gợi ý từ sổ tay cần kiểm tra")
        for h in hints:
            out.append(f"- {h['id']}: {h['canonical']} [{h['loai']}] ← biến thể đã gặp: "
                       + (", ".join(f"“{v}”" for v in h["bien_the_gap"]) or "(chỉ thấy cách viết đúng)"))
            out += [f"    câu trong video này: {c}" for c in h["ngu_canh"]]
        out.append("")
    out += ["## Transcript ASR (trích; \"…\" là chỗ bỏ bớt)", *excerpt]
    return "\n".join(out)


# ----------------------------------------------------------------------------------------------
# Duyệt trước khi ghi vào sổ tay (model rẻ)
# ----------------------------------------------------------------------------------------------

VET_SCHEMA = _obj(
    {
        "entries": _arr(_obj({
            "id": {"type": "string"},
            "verdict": {"type": "string", "enum": ["hoc", "bo"]},
            "variants_keep": _arr(),
            "reason": {"type": "string"},
        })),
        "notes": _arr(_obj({
            "id": {"type": "string"},
            "verdict": {"type": "string", "enum": ["hoc", "bo"]},
            "reason": {"type": "string"},
        })),
    }
)

VET_SYSTEM = """Bạn kiểm duyệt những gì một hệ thống tự học được phép ghi vào SỔ TAY LỖI ASR dùng chung cho các video \
sau. Mỗi mục đề xuất có dạng: cách viết đúng ← các biến thể ASR (nhận dạng giọng nói) đã viết sai trong video vừa xử lý, \
kèm câu ví dụ trước (thô) và sau khi sửa. Sổ tay sẽ gợi ý sửa các biến thể này ở video khác, nên mục sai hay mục quá \
chung sẽ gây sửa nhầm hàng loạt. Thà bỏ sót còn hơn học sai.

Với mỗi mục (entries):
- hoc: cách viết đúng là tên riêng / thương hiệu / sản phẩm / thuật ngữ / viết tắt cụ thể, viết đúng chính tả; và mỗi \
biến thể giữ lại là lỗi NGHE NHẦM thật của ASR (gần âm với cách viết đúng), câu ví dụ cho thấy sửa là đúng, và nhiều \
khả năng lặp lại ở video khác cùng chủ đề. Mục không có biến thể (chỉ xác nhận chính tả) được học khi là tên riêng/thuật \
ngữ cụ thể, không phải từ thông dụng.
- bo khi: cách viết đúng là từ/cụm thông dụng; biến thể là từ tiếng Việt bình thường hay đúng ở ngữ cảnh khác \
("thời gian", "cam", "đẹp", "là"); thay đổi chỉ là văn phong, diễn đạt lại, dấu câu, viết hoa, định dạng số; sửa sai \
hoặc đáng ngờ theo câu ví dụ; lựa chọn chỉ đúng riêng cho câu đó.
- variants_keep: các biến thể nên giữ (chép nguyên văn trong danh sách được đưa), bỏ biến thể là từ thông dụng hay \
không gần âm. Mục "bo" thì để rỗng.

Với mỗi ghi chú thể loại (notes): hoc khi đó là quy ước chính tả/văn phong transcript dùng được cho MỌI video cùng thể \
loại, không chứa tên riêng/chi tiết riêng của video này, và không yêu cầu diễn đạt lại hay bỏ lời người nói. Ngược lại bo.
reason: ngắn (≤15 từ).
"""


def vet_prompt(topic: str, genre: str, entries: list[dict], notes: list[dict]) -> str:
    out = [f"Video vừa xử lý: {topic}", f"Thể loại: {genre}", "", "## Mục từ điển đề xuất"]
    for e in entries:
        flags = ", ".join(x for x, ok in (("thấy trên màn hình", e["man_hinh"]), ("model lập từ điển chắc chắn", e["chac"]))
                          if ok) or "không có căn cứ phụ"
        out.append(f"- {e['id']}: {e['canonical']} [{e['loai']}] ← "
                   + (", ".join(f"“{v}”" for v in e["bien_the"]) or "(không có biến thể, chỉ xác nhận chính tả)")
                   + f" · sửa {e['lan_sua']} chỗ · {flags}")
        for raw, fin in e["vi_du"]:
            out += [f"    thô : {raw}", f"    sửa : {fin}"]
    if not entries:
        out.append("(không có)")
    out += ["", "## Ghi chú thể loại đề xuất"]
    out += [f"- {n['id']}: {n['text']}" for n in notes] or ["(không có)"]
    return "\n".join(out)


# ----------------------------------------------------------------------------------------------
# 2. Hiệu đính từng cụm
# ----------------------------------------------------------------------------------------------

_CORRECT_BASE = """Bạn là biên tập viên hiệu đính transcript tự động (ASR) cho video {lang_name} thuộc mọi thể loại \
(họp, đào tạo, bài giảng, phỏng vấn, livestream, tin tức, vlog...), người nói có thể mang giọng bất kỳ vùng miền nào và chen \
thuật ngữ tiếng Anh. Mục tiêu: văn bản ghi ĐÚNG những gì người nói THỰC SỰ nói, đúng chính tả, nhất quán, dễ đọc khi làm \
phụ đề.

CẦN SỬA
1. Từ bị nghe nhầm thành từ đồng âm/gần âm làm câu sai nghĩa hoặc vô nghĩa trong ngữ cảnh — sửa thành từ người nói \
thực sự dùng (dựa vào nghĩa của cả câu, đoạn trước/sau, chủ đề).
2. Từ/cụm tiếng Anh bị phiên âm hoặc viết sai → viết lại đúng chính tả tiếng Anh gốc ("pát nơ" → "partner", \
"phi bách" → "feedback", "ôn bốt" → "onboard", "kê pi ai" → "KPI"). GIỮ tiếng Anh như người nói, KHÔNG dịch sang tiếng Việt \
và KHÔNG dịch tiếng Việt sang tiếng Anh.
3. Tên riêng (người, công ty, thương hiệu, sản phẩm, sự kiện, chương trình) → đúng cách viết trong từ điển / trên màn hình, \
nhất quán trong toàn bộ văn bản.
4. Chính tả tiếng Việt: sai dấu thanh (hỏi/ngã...), s/x, ch/tr, d/gi/r, n/l, c/k/q, âm cuối n/ng, c/t; từ bị dính hoặc tách sai. \
Từ địa phương người nói thực sự dùng ("hông", "vô", "mô", "răng", "nhứt"...) là lời thật — giữ nguyên, chỉ sửa khi ASR \
nghe nhầm thành một từ khác.
5. Số, ngày giờ, tiền, phần trăm, đơn vị: dùng chữ số khi tự nhiên ("hai mươi phần trăm" → "20%", "ba triệu" → "3 triệu"). \
Viết tắt viết hoa đúng (KPI, AI, CEO...).
6. Chấm câu và viết hoa: thêm dấu câu hợp lý, viết hoa đầu câu và tên riêng.
7. Ảo giác của ASR — câu không ai nói nhưng hay bị chèn vào chỗ im lặng/nhạc ("Hãy subscribe cho kênh...", \
"Cảm ơn các bạn đã theo dõi", "Hẹn gặp lại trong video tiếp theo", "Ghiền Mì Gõ", "Vietsub by...", "Hãy đăng ký kênh") \
và cụm bị lặp lại liên tiếp bất thường → xóa phần ảo giác (để text rỗng "" nếu cả đoạn là ảo giác; nếu lặp thì giữ 1 lần).
{filler_rule}{genre_rules}
NGUYÊN TẮC BẮT BUỘC
- Trung thành với lời nói: KHÔNG diễn đạt lại, KHÔNG tóm tắt, KHÔNG thêm ý, KHÔNG đổi thứ tự, KHÔNG "trau chuốt văn". \
Văn nói giữ là văn nói. Chỉ sửa chỗ sai hoặc khó đọc.
- Mọi chỉnh sửa phải có căn cứ (ngữ cảnh, chủ đề, từ điển, chữ trên màn hình, độ gần về phát âm). Khi có nhiều phương án, \
chọn phương án hợp lý nhất và ghi vào "uncertain" (id, cụm từ trong bản đã sửa, lý do + phương án khác). \
Đừng ghi "uncertain" cho những chỗ đã chắc chắn.
- Từ đặt trong ‹ › là chỗ ASR kém tự tin → kiểm tra kỹ (nhưng có thể vẫn đúng). TUYỆT ĐỐI không để ký hiệu ‹ › trong kết quả.
- Trả về ĐÚNG từng id đã nhận, đúng thứ tự, không gộp, không tách, không bỏ sót. ASR có thể cắt một câu ra hai đoạn — \
giữ nguyên ranh giới, sửa từng phần cho khớp.
- Phần "NGỮ CẢNH TRƯỚC/SAU" chỉ để hiểu, KHÔNG trả về.
"""

_FILLER_CLEAN = (
    "8. Chỉ bỏ tiếng ngập ngừng (\"ờ\", \"ừm\", \"à à\") và chỗ nói lắp/lặp từ liền nhau (\"thì thì\" → \"thì\", "
    "\"cái cái\" → \"cái\"). KHÔNG bỏ các từ đơn lẻ của văn nói như \"là\", \"thì\", \"mà\", \"cái\", \"cũng\", \"này\", "
    "\"đấy\", \"kiểu\" — đó là cách người Việt nói bình thường, xóa đi là sửa quá tay. Giữ lại tiểu từ có sắc thái "
    "(\"nhé\", \"nha\", \"ạ\", \"đúng không\").\n"
)
_FILLER_VERBATIM = "8. Giữ nguyên từ đệm và chỗ nói lắp (chế độ nguyên văn), chỉ sửa chính tả của chúng.\n"


def correct_system(lang_name: str, verbatim: bool, genre_rules: str = "") -> str:
    """`genre_rules`: luật riêng của thể loại + ghi chú đã học (knowledge.genre_rules). Luật lõi ở trên không đổi."""
    return _CORRECT_BASE.format(lang_name=lang_name, filler_rule=_FILLER_VERBATIM if verbatim else _FILLER_CLEAN,
                                genre_rules=genre_rules)


def correct_prompt(
    context: str, before: list[str], chunk: list[dict], after: list[str]
) -> str:
    seg_lines = "\n".join(json.dumps({"id": s["id"], "text": s["text"]}, ensure_ascii=False) for s in chunk)
    out = context
    if before:
        out += "## NGỮ CẢNH TRƯỚC (chỉ để hiểu, KHÔNG trả về)\n" + "\n".join(before) + "\n\n"
    out += (
        f"## CÁC ĐOẠN CẦN HIỆU ĐÍNH — trả về đủ {len(chunk)} đoạn, id {chunk[0]['id']}–{chunk[-1]['id']}\n"
        + seg_lines
        + "\n\n"
    )
    if after:
        out += "## NGỮ CẢNH SAU (chỉ để hiểu, KHÔNG trả về)\n" + "\n".join(after) + "\n"
    return out


# ----------------------------------------------------------------------------------------------
# 3. Soát toàn cục
# ----------------------------------------------------------------------------------------------

REVIEW_SYSTEM = """Bạn là người soát lỗi VÒNG CUỐI cho transcript {lang_name} đã được hiệu đính. Bản này được hiệu đính theo \
từng cụm nhỏ riêng lẻ, nên có thể còn: (1) cùng một tên/thuật ngữ nhưng viết KHÔNG NHẤT QUÁN giữa các cụm; (2) từ vẫn \
vô nghĩa/lạc ngữ cảnh do ASR nghe nhầm mà vòng trước bỏ sót; (3) từ tiếng Anh bị phiên âm còn sót; (4) ảo giác/lặp còn sót; \
(5) SỬA QUÁ TAY: vòng trước đã đổi nghĩa so với lời nói gốc (xem dòng "ASR:" bên dưới mỗi đoạn có thay đổi để so).

Nhiệm vụ: nhìn toàn cục và trả về danh sách chỉnh sửa TỐI THIỂU dạng find/replace:
- id: đoạn cần sửa; find: chuỗi con NGUYÊN VĂN đang có trong đoạn đó (đủ dài để chỉ khớp đúng chỗ cần sửa); replace: chuỗi thay thế; \
reason: lý do ngắn.
- Muốn xóa thì replace = "". Không sửa văn phong, không sửa chỗ đã đúng, không diễn đạt lại.
- Chỗ vẫn còn nghi ngờ mà không đủ căn cứ để sửa → đưa vào "uncertain".
- Không có lỗi thì trả về danh sách rỗng.
"""


def review_prompt(context: str, rows: list[str]) -> str:
    return (
        context
        + "## TRANSCRIPT ĐÃ HIỆU ĐÍNH (mỗi đoạn: [id] nội dung; dòng 'ASR:' là bản gốc khi đoạn đó đã bị sửa)\n"
        + "\n".join(rows)
    )


# ----------------------------------------------------------------------------------------------
# 4. Tổng hợp nội dung
# ----------------------------------------------------------------------------------------------

SUMMARY_SYSTEM = """Bạn là chuyên viên tổng hợp nội dung các buổi họp, đào tạo, bài giảng. Từ transcript (đã chuẩn hóa) \
bạn viết bản TỔNG HỢP NỘI DUNG & Ý CHÍNH bằng {out_lang}, định dạng Markdown, dành cho người không xem video nhưng cần \
nắm đầy đủ và chính xác nội dung.

Yêu cầu:
- Chỉ dùng thông tin có trong transcript; không bịa thêm số liệu, tên, kết luận. Chỗ transcript mơ hồ thì nói rõ là chưa rõ.
- Dùng đúng tên riêng và thuật ngữ như trong transcript.
- Gắn mốc thời gian [hh:mm:ss] cho từng phần để người đọc tra lại video.
- Cụ thể: giữ lại số liệu, ví dụ, quy trình từng bước, mẹo, lưu ý quan trọng mà người nói nêu.

Cấu trúc:
# <Tiêu đề phản ánh nội dung video>
## Tổng quan
(3–5 câu: video là gì, ai trình bày cho ai, mục tiêu, kết quả chính)
## Ý chính
(5–10 gạch đầu dòng quan trọng nhất)
## Nội dung chi tiết theo từng phần
### [hh:mm:ss] Tên phần
(các ý, ví dụ, số liệu, quy trình của phần đó)
## Thuật ngữ & khái niệm quan trọng
(thuật ngữ — giải thích ngắn theo cách dùng trong video)
## Kết luận, việc cần làm & lưu ý
(những việc người nghe cần làm, deadline, trách nhiệm nếu có)
## Hỏi – đáp
(chỉ khi trong video có phần hỏi đáp; tóm tắt câu hỏi và câu trả lời)
"""


def summary_prompt(context: str, lines: list[str]) -> str:
    return context + "## TRANSCRIPT ĐÃ CHUẨN HÓA\n" + "\n".join(lines)
