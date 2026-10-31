import importlib.util
from pathlib import Path
import unittest
import numpy as np
spec=importlib.util.spec_from_file_location('fingerprint',Path(__file__).parents[1]/'fingerprint.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

def scan(ids,shift=0):
    aps=[dict(bssid=f'02:00:00:00:{i//256:02x}:{i%256:02x}',rssi_dbm=-45-(i%7)*3+shift,channel=1) for i in ids]
    return dict(aps=aps,result=len(aps))

class Tests(unittest.TestCase):
    def test_identical(self):self.assertAlmostEqual(m.similarity([scan(range(10))])[0][0,0],1)
    def test_disjoint(self):self.assertEqual(m.similarity([scan(range(10))],[scan(range(20,30))])[0][0,0],0)
    def test_single_shared_rejected(self):self.assertEqual(m.similarity([scan(range(10))],[scan([0]+list(range(20,30)))])[0][0,0],0)
    def test_empty_sparse_rejected(self):
        labs,_=m.cluster([scan([]),scan([1]),scan([1,2])]);self.assertTrue(np.all(labs==-1))
    def test_known_three_places_and_unknown(self):
        train=[scan(range(p*20,p*20+12),shift) for p in range(3) for shift in [-2,0,2,1,-1]]
        labels,_=m.cluster(train);self.assertEqual(len(set(labels)),3)
        queries=[scan(range(p*20+1,p*20+12),3) for p in range(3)]+[scan(range(100,112))]
        preds=m.predict(train,labels,queries)
        for p in range(3):self.assertEqual(preds[p]['place'],labels[p*5])
        self.assertEqual(preds[-1]['place'],-1)
    def test_input_order_preserves_pair_partition(self):
        rows=[scan(range(p*20,p*20+12),shift) for p in range(3) for shift in [-2,0,2]]
        a,_=m.cluster(rows);b,_=m.cluster(list(reversed(rows)));b=b[::-1]
        self.assertTrue(np.array_equal(a[:,None]==a[None,:],b[:,None]==b[None,:]))
    def test_no_gps_inference(self):
        train=[scan(range(10)) for _ in range(3)];lab,_=m.cluster(train)
        self.assertNotIn('latitude',m.predict(train,lab,[scan(range(10))])[0])

if __name__=='__main__':unittest.main()
