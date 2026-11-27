from datetime import datetime, timezone
from unittest.mock import patch

from issuer_catalyst_discovery import merge_candidates, read_rss


def test_published_issuer_news_is_selected_without_chart_confirmation():
    at = datetime(2026, 11, 24, tzinfo=timezone.utc)
    issuer = dict(symbol='310210.KQ', name='보로노이', indicators=dict(
        price=105000, average_turnover_20d=100_000_000_000, below_ma20=True))
    chart_pick = dict(symbol='000001.KS', discovery_tracks=['chart'])
    content = b'<rss><channel>' + ''.join(
        f'<item><title>{title}</title><pubDate>{date}</pubDate>'
        f'<link>https://example.com/{index}</link></item>'
        for index, (title, date) in enumerate([
            ('보로노이, 국가신약개발사업 과제 선정', 'Mon, 23 Nov 2026 09:39:00 +0900'),
            ('보로노이, 공급계약 체결', 'Wed, 25 Nov 2026 09:39:00 +0900'),
            ('보로노이, 정부과제 미선정', 'Mon, 23 Nov 2026 09:39:00 +0900'),
        ])).encode() + b'</channel></rss>'
    with patch('issuer_catalyst_discovery.requests.get') as get:
        get.return_value.content = content
        events, coverage = read_rss('news', 'KR', 'https://example.com', [issuer], at)
    assert coverage == 'ok'
    assert [event['url'] for event in events] == ['https://example.com/0']
    assert merge_candidates([chart_pick], [chart_pick, issuer], events, 'KR') == [chart_pick, issuer]
    assert chart_pick['discovery_tracks'] == ['chart']
    assert issuer['discovery_tracks'] == ['issuer_catalyst']
    assert 'preopen_signal' not in issuer
