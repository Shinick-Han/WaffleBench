import copy
import unittest

import numpy as np

from workflow_learning.experiment import encode, split


class WorkflowBoundaryTests(unittest.TestCase):
    def rows(self):
        return [dict(id=str(i),day=f'2021-01-{i//2+1:02d}',submitted=f'2021-01-{i//2+1:02d}T00:00:00',
                     type='t1',package='p1',quantity=2.,recorded_working_hours=i+1,tasks=i+3)
                for i in range(20)]

    def test_future_duration_and_job_identifiers_never_enter_features(self):
        a=self.rows();b=copy.deepcopy(a)
        for j in b:
            j.update(id='changed',recorded_working_hours=1000000,tasks=900,finish='2099-01-01')
        np.testing.assert_array_equal(encode(a),encode(b))

    def test_chronological_day_groups_do_not_cross_splits(self):
        pools,cutoffs=split(self.rows())
        days={k:{j['day'] for j in v} for k,v in pools.items()}
        ids={k:{j['id'] for j in v} for k,v in pools.items()}
        self.assertFalse(days['train'] & days['tune'])
        self.assertFalse(days['tune'] & days['development_evaluation'])
        self.assertFalse(ids['train'] & ids['development_evaluation'])
        self.assertLess(max(days['train']),min(days['tune']))
        self.assertLess(max(days['tune']),min(days['development_evaluation']))


if __name__=='__main__':unittest.main()
