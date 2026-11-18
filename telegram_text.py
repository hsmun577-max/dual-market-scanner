"""Keep multi-company Telegram reports within the message length limit."""


def split_telegram_text(text, limit=3800):
    chunks = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if current:
                chunks.append(current.rstrip())
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            chunks.append(current.rstrip())
            current = ""
        current += line
    if current.strip():
        chunks.append(current.rstrip())
    return chunks
