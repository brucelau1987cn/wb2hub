import pathlib
import shutil
import subprocess
import unittest

class PanelEvidenceTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node needed for isolated UI helper test')
    def test_activity_labels_preserve_separate_evidence(self):
        source=(pathlib.Path(__file__).resolve().parents[1]/'dashboard.html').read_text()
        helper=source.split('function activityStatusOf(a){',1)[1].split('\nfunction activityStatusChip',1)[0]
        js="const ACTIVITY_TODAY={byUid:{}};const todayStamp=()=> '2026-11-16';\nfunction activityStatusOf(a){"+helper+"\n"
        js+="""
        const assert=require('node:assert/strict');
        const a={uid:'test',realm:'intl',dailyChatState:{date:todayStamp(),activityVerified:true,webCompleted:false,rewardVerified:null}};
        let s=activityStatusOf(a);
        assert.match(s.state,/网页待完成/);
        assert.match(s.message,/连登.*已核验/);
        assert.match(s.message,/奖励.*未核验/);
        a.dailyChatState.webCompleted=true;
        s=activityStatusOf(a);assert.match(s.state,/网页已完成/);assert.match(s.message,/奖励.*未核验/);
        console.log('panel state evidence OK');
        """
        r=subprocess.run(['node','-e',js],text=True,capture_output=True)
        self.assertEqual(r.returncode,0,r.stderr)
