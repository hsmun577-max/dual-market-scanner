"""Keep multi-company Telegram reports within the message length limit.

Telegram refuses a message longer than 4096 characters, so a long report is sent in
parts. The split has to keep a company's own lines together: cutting inside a name's
block left a part starting with "  20일 평균 거래대금 …" under no company at all
(2026-11-25 08:50 preopen message, whose second part opened mid-entry). Blocks are the
blank-line separated groups the reports already build, so the split happens between
them, and only a block longer than the whole limit is broken line by line.
"""


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
