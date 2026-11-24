import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import kis_intraday as scanner

opens = datetime(2026, 11, 18, 9, tzinfo=scanner.KST)

class Clock(datetime):
    current = opens - timedelta(seconds=1)

    @classmethod
    def now(cls, tz=None):
        return cls.current.astimezone(tz) if tz else cls.current

class Socket:
    def __init__(self):
        self.pending = list(scanner.FEEDS['KRX'])
        self.minutes = iter([5, 6, 25, 56])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def send(self, message):
        pass

    async def pong(self, message):
        pass

    async def recv(self, **kwargs):
        await asyncio.sleep(0.02)
        if self.pending:
            return json.dumps({'header': {'tr_id': self.pending.pop(), 'tr_key': '005930'}, 'body': {'rt_cd': '0'}})
        Clock.current = opens + timedelta(minutes=next(self.minutes))
        return json.dumps({'header': {'tr_id': 'PINGPONG'}})

companies = {}
evidence = []
def refresh():
    return {'005930': {'checked_at': 'refreshed'}}, [{'kind': 'event', 'assessment': 'risk',
            'symbol': '005930', 'market': 'KRX', 'available_at': opens.isoformat()}]

def evaluate(*args, **kwargs):
    assert companies['005930']['checked_at'] == 'refreshed'
    assert evidence[0]['assessment'] == 'risk'
    return 'event_risk', None

with TemporaryDirectory() as directory, patch.object(scanner, 'datetime', Clock), patch.object(scanner, 'connect', return_value=Socket()), patch.object(scanner, 'evaluate_candidate', side_effect=evaluate):
    asyncio.run(scanner.collect('fake', 'KRX', ['005930'], opens, lambda candidates: None,
                                Path(directory) / 'KRX.jsonl', evidence,
                                refresh_context=refresh, companies=companies))
print('Concurrent refresh applied before signal evaluation while websocket messages continued')
