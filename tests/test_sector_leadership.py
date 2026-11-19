import unittest
from industry_flow import rank_leading_sectors,format_report
from sector_flow import format_sector_header

class SectorLeadershipTests(unittest.TestCase):
    def row(self,name,net,excess,share,up,down):
        return dict(name=name,exchange='KSP',combined_net=net,return_pct=excess+1,
                    market_excess_pct=excess,turnover_share_pct=share,advances=up,declines=down)
    def test_largest_net_buy_alone_does_not_beat_broad_price_and_turnover_leadership(self):
        rows=[self.row('방어업종',1000,-.5,1,2,8),
              self.row('오락·문화',10,8,20,9,1),
              self.row('전기·전자',20,3,15,8,2)]
        self.assertEqual([r['name'] for r in rank_leading_sectors({'rows':rows})],['오락·문화','전기·전자'])
        self.assertEqual(format_report({'rows':rows}),'오늘의 주요 섹터\\n1위 오락·문화\\n2위 전기·전자')
    def test_missing_breadth_and_duplicates_do_not_create_two_fake_leaders(self):
        row=self.row('제약',10,1,4,7,3)
        missing=self.row('미확보',100,9,9,0,0)
        self.assertEqual(len(rank_leading_sectors({'rows':[row,row,missing]})),1)
    def test_fallback_only_two_unique_names(self):
        rows=[dict(name=n,market='KR',return_pct=i,market_excess_pct=i,turnover_ratio=1)
              for i,n in enumerate(['A','B','B','C'])]
        self.assertEqual(format_sector_header(rows,'KR'),['오늘의 주요 섹터','1위 C','2위 B'])

if __name__=='__main__': unittest.main()
