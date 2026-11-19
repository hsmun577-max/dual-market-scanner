from datetime import datetime,timedelta,time
from zoneinfo import ZoneInfo
import unittest
from delivery_policy import plan,delivery_status
from industry_flow import industries,number,format_report


class DeliveryTests(unittest.TestCase):
    def check(self,market,day,opening,target):
        zone=ZoneInfo('America/New_York' if market.startswith('US') else 'Asia/Seoul')
        session=datetime.combine(datetime.fromisoformat(day).date(),time.fromisoformat(opening),zone)
        now=datetime.combine(session.date(),time.fromisoformat(target),zone)
        window=plan(market,now,session)
        self.assertEqual(datetime.fromisoformat(window['target_at']),now)
        self.assertTrue(delivery_status(market,now,session)[0])
        self.assertFalse(delivery_status(market,now-timedelta(seconds=1),session)[0])
        self.assertFalse(delivery_status(market,now+timedelta(minutes=2),session)[0])
    def test_nxt_window(self): self.check('NXT','2026-11-20','09:00','07:50')
    def test_krx_window(self): self.check('KRX','2026-11-20','09:00','08:50')
    def test_special_krx_open(self): self.check('KRX','2026-11-19','09:00','09:50')
    def test_no_nxt_on_csat(self):
        now=datetime(2026,11,19,7,50,tzinfo=ZoneInfo('Asia/Seoul'))
        self.assertIsNone(plan('NXT',now,now.replace(hour=10,minute=0)))
    def test_us_premarket(self): self.check('US_PRE','2026-11-20','09:30','03:50')
    def test_us_regular(self): self.check('US','2026-11-20','09:30','09:20')
    def test_us_dst_conversion(self):
        for day,hour in [('2026-07-15',22),('2026-11-20',23)]:
            now=datetime.fromisoformat(day+'T09:20:00').replace(tzinfo=ZoneInfo('America/New_York'))
            self.assertEqual(now.astimezone(ZoneInfo('Asia/Seoul')).hour,hour)
    def test_no_implicit_daily_send(self):
        self.assertEqual(delivery_status(None,datetime.now(ZoneInfo('Asia/Seoul')))[0],False)


class IndustryTests(unittest.TestCase):
    def test_excludes_listing_groups_and_manufacturing_parent(self):
        rows = b''.join(b'0'+code+name.encode('cp949').ljust(40,b' ')+b'\n' for code,name in [
            (b'0005','기술성장기업'),(b'0006','제조'),(b'0007','제약')])
        self.assertEqual([row['name'] for row in industries(rows)],['제약'])

    def test_master_layout_uses_byte_width_not_overlapping_name(self):
        data=b'0'+b'0005'+'음식료품'.encode('cp949').ljust(40,b' ')+b'\n'
        self.assertEqual(industries(data)[0]['name'],'음식료품')
    def test_invalid_number_is_missing(self):
        self.assertIsNone(number('')); self.assertEqual(number('-1,234'),-1234.)
    def test_missing_flow_not_no_flow(self):
        text=format_report(dict(business_date='20261119',observed_at='2026-11-19T16:00:00+09:00',rows=[]))
        self.assertIn('확인 불가',text)
        self.assertNotIn('유입 없음\n',text)


if __name__=='__main__': unittest.main()
