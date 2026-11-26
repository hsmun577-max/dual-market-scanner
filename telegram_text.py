"""Keep multi-company Telegram reports within the message length limit.

Telegram refuses a message longer than 4096 characters, so a long report is sent in
parts. The split has to keep a company's own lines together: cutting inside a name's
block left a part starting with "  20일 평균 거래대금 …" under no company at all
(2026-11-25 08:50 preopen message, whose second part opened mid-entry). Blocks are the
blank-line separated groups the reports already build, so the split happens between
them, and only a block longer than the whole limit is broken line by line.
"""


def rest_note(total, shown, label="건"):
    """'외 2건' — 잘린 목록이 조용히 사라지지 않게 알린다.

    2026-11-26 07:50 발송본은 `confirmed[:5]`로 상위 5건만 싣고 6번째(SK텔레콤)를
    아무 말 없이 버렸다. 화면에 없는 종목을 화면에 있는 것처럼 읽게 두지 않는다.
    """
    try:
        missing = int(total or 0) - int(shown or 0)
    except (TypeError, ValueError):
        return ""
    return f"외 {missing}{label}" if missing > 0 else ""


def _blocks(text):
    block = []
    for line in text.splitlines(keepends=True):
        if line.strip():
            block.append(line)
            continue
        if block:
            yield "".join(block)
            block = []
        yield line
    if block:
        yield "".join(block)


def split_telegram_text(text, limit=3800):
    chunks = []
    current = ""
    for block in _blocks(text):
        if len(block) > limit:
            # A single oversized block is broken on its own line boundaries.
            if current.strip():
                chunks.append(current.rstrip())
                current = ""
            for line in block.splitlines(keepends=True):
                while len(line) > limit:
                    chunks.append(line[:limit])
                    line = line[limit:]
                if len(current) + len(line) > limit:
                    chunks.append(current.rstrip())
                    current = ""
                current += line
            continue
        if len(current) + len(block) > limit:
            chunks.append(current.rstrip())
            current = ""
        current += block
    if current.strip():
        chunks.append(current.rstrip())
    return chunks
