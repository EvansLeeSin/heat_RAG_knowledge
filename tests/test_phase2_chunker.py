"""阶段二验证：清洗、条款切分逻辑（离线，无需数据库/Docling）。"""

import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from app.services.parser import ParsedBlock
from app.services.chunker import chunk_blocks, clean_blocks

blocks = [
    ParsedBlock("heading", "北京市供热突发事件应急预案", 1),
    ParsedBlock("paragraph", "首页 版权所有"),
    ParsedBlock("heading", "三、应急处置", 1),
    ParsedBlock(
        "paragraph",
        "第十二条 发生供热管网泄漏后，供热单位应当立即启动应急预案，组织抢修队伍赶赴现场，"
        "先采取关阀降压、截断泄漏管段等控制措施，防止事态扩大。" + "补充说明。涉及一次网、二次网与换热站。" * 40,
    ),
    ParsedBlock("paragraph", "第十三条 供热单位应当在事故发生后一小时内向城市管理部门报告。"),
    ParsedBlock("heading", "四、后期处置", 1),
    ParsedBlock("paragraph", "第十四条 事故处置结束后，供热单位应当开展调查评估。"),
]

cleaned = clean_blocks(blocks)
print("cleaned blocks:", [(b.kind, b.text[:15]) for b in cleaned])
assert len(cleaned) == 6, f"噪声块应被移除，实际 {len(cleaned)}"

chunks = chunk_blocks(blocks)
for i, c in enumerate(chunks):
    print(
        f"chunk {i}: section={c.section_path!r} article={c.article_no!r} "
        f"heading={c.heading!r} len={len(c.content)}"
    )

# 断言：条款号保留、章节路径保留、长条款被拆分
assert any(c.article_no and c.article_no.startswith("第十二条") for c in chunks)
assert any(c.section_path == "三、应急处置" for c in chunks)
assert any(len(c.content) > 600 for c in chunks), "长条款应被拆分为多个 Chunk"

# 长文本拆分测试
from app.services.chunker import _split_long_text

long_text = "一次网泄漏处置。" * 300  # ~2400 字
pieces = _split_long_text(long_text)
assert all(len(p) <= 1200 for p in pieces), "拆分结果不超过最大长度"
print("long text split into", len(pieces), "pieces")

print("PHASE2_CHUNKER_TESTS_OK")
