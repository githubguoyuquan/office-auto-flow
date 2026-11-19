import unittest

from table_recognition import recognize


FIELDS = {'编号':['Code(编号)'], '名称':['Description(描述)'], '颜色':['Color']}


class TableRecognitionTests(unittest.TestCase):
    def test_transposed_oa_table_retains_source_coordinates(self):
        result = recognize([['Code（编号）','001','002'], ['Description(描述)','纽扣','蕾丝']], FIELDS)
        item = next(c for c in result['candidates'] if c.orientation=='columns' and c.header_end==0)
        self.assertEqual(item.records, (('001','纽扣'),('002','蕾丝')))
        self.assertEqual(item.sources[0], ((0,1),(1,1)))

    def test_merged_parent_and_two_level_headers(self):
        result = recognize([['基本信息',None],['编号','名称'],['001','纽扣']], FIELDS, [(0,0,0,1)])
        self.assertTrue(any(c.records==(('001','纽扣'),) for c in result['candidates']))

    def test_parallel_tables_are_flagged_not_overwritten(self):
        result = recognize([['编号','名称','编号','名称'],['001','甲','002','乙']], FIELDS)
        self.assertEqual(result['status'],'review')
        item = next(c for c in result['candidates'] if c.orientation=='rows' and c.header_end==0)
        self.assertEqual(item.records[0],('001','甲','002','乙'))
        self.assertTrue(item.issues)

    def test_repeated_header_not_exported_as_data(self):
        result = recognize([['编号','名称'],['001','甲'],['编号','名称'],['002','乙']], FIELDS)
        item = next(c for c in result['candidates'] if c.orientation=='rows' and c.header_end==0)
        self.assertEqual(item.records,(('001','甲'),))
        self.assertTrue(item.issues)

    def test_merged_data_is_not_silently_filled(self):
        result = recognize([['编号','名称'],['001','甲'],[None,'乙']], FIELDS,[(1,0,2,0)])
        item = next(c for c in result['candidates'] if c.orientation=='rows' and c.header_end==0)
        self.assertEqual(item.records[1],('','乙'))

    def test_alias_collision_and_invalid_merges(self):
        with self.assertRaisesRegex(ValueError,'冲突'):
            recognize([['编号','名称']],{'甲':['Code'],'乙':['ＣＯＤＥ']})
        with self.assertRaisesRegex(ValueError,'范围无效'):
            recognize([['编号','名称']], FIELDS,[(0,0,3,1)])

    def test_unknown_table_requires_review(self):
        self.assertEqual(recognize([['x','y'],['1','2']],FIELDS)['status'],'unrecognized')


if __name__ == '__main__':
    unittest.main()
